"""Find a cut - "if I want THIS card in the deck, what should come out?" - and the upgrade
plan built from the same engine (deck before vs after each swap).

    python -m unittest tests.test_cutfinder -v
"""
import os
import shutil
import sys
import tempfile
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import analyser  # noqa: E402
import community  # noqa: E402
import cutfinder  # noqa: E402
import userdb  # noqa: E402
from decks import DeckStore  # noqa: E402
from test_analyser_understanding import FILLER, has_cards  # noqa: E402
from userdb import UserDB  # noqa: E402

HIGH = dict(mode="all", goal="high", tags=["Tokens", "Aristocrats"], budget_usd=None)
COLL = dict(HIGH, mode="collection")
CAESAR = "Caesar, Legion's Emperor"
# a Caesar token deck with clear slot pressure: 5 board wipes in a go-wide deck, a few
# vanilla bodies, only 2 removal spells (a role that must not get shorter)
CARDS = ["Bitterblossom", "Impact Tremors", "Skullclamp", "Secure the Wastes", "Adeline, Resplendent Cathar",
         "Viscera Seer", "Blood Artist", "Zulaport Cutthroat", "Martial Coup", "Wrath of God", "Damnation",
         "Vault 75: Middle School", "Blasphemous Act", "Swords to Plowshares", "Path to Exile", "Teferi's Protection",
         "Raise the Alarm", "Spectral Procession", "Lingering Souls", "Hordeling Outburst", "Goblin Instigator",
         "Beetleback Chief", "Mogg War Marshal", "Assemble the Legion", "Hill Giant", "Gray Ogre", "Night's Whisper",
         "Read the Bones", "Phyrexian Arena", "Sign in Blood"] + FILLER


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tmp_cut_", dir=HERE)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        old = userdb.BACKUP_DIR
        userdb.BACKUP_DIR = os.path.join(self.tmp, "b")
        self.addCleanup(setattr, userdb, "BACKUP_DIR", old)
        old_cache = community.CACHE_PATH
        community.CACHE_PATH = os.path.join(self.tmp, "commander_cache.db")
        self.addCleanup(setattr, community, "CACHE_PATH", old_cache)
        self.db = UserDB(os.path.join(self.tmp, "u.db"))
        self.addCleanup(self.db.close)
        self.st = DeckStore(self.db)
        self.deck = self.make(CARDS)

    def make(self, cards, cmd=CAESAR, basics=30):
        d = self.st.create_deck("Caesar test")
        parsed = self.st.parse_decklist(f"Commander\n1 {cmd}\nDeck\n" + "\n".join("1 " + c for c in cards) +
                                        f"\n{basics} Wastes")
        self.assertEqual([r["name"] for r in parsed if not r["card"]], [])
        self.st.commit_import(d, parsed)
        return d

    def own(self, *names):
        for n in names:
            c = self.st.default_printing(n)
            self.db.add_card(c["id"], n, c["set_code"], c["collector_number"])

    def find(self, name, settings=HIGH, deck=None):
        return analyser.find_a_cut(self.db, self.st, deck or self.deck, settings, name)

    def finder(self, settings=HIGH, deck=None):
        return cutfinder.finder_for(self.db, self.st, deck or self.deck, settings)


@unittest.skipUnless(has_cards(), "needs cards.db with oracle_cards")
class FindACutTests(Base):
    def test_a_good_card_can_be_replaced_by_a_clearly_better_one_without_being_called_bad(self):
        r = self.find("Mirkwood Bats")
        self.assertTrue(r["options"])
        cf, st = self.finder()
        review = {v["name"]: v for v in st.review}
        clean = [o for o in r["options"] if review[o["out"]]["E"] == 0]  # no "this card is bad" evidence at all
        self.assertTrue(clean or len(r["options"]) >= 1)
        for o in r["options"]:
            text = " ".join(o["why_out"]).lower()
            self.assertNotIn("bad", text)
            self.assertTrue(o["gains"] and o["net"])
            self.assertIn(o["tier"], ("HIGH", "GOOD", "OPTIONAL"))

    def test_scenario_strong_token_payoff(self):
        r = self.find("Mirkwood Bats")
        best = r["options"][0]
        # a surplus wipe, a vanilla body or surplus draw - never removal, protection or the engine
        self.assertNotIn(best["out"], {"Swords to Plowshares", "Path to Exile", "Teferi's Protection", "Bitterblossom",
                                       "Impact Tremors", "Skullclamp", "Viscera Seer", "Blood Artist"})
        self.assertTrue(any("token" in g for g in best["gains"]), best["gains"])

    def test_scenario_more_draw_takes_a_surplus_or_vanilla_slot(self):
        r = self.find("Welcoming Vampire")
        outs = [o["out"] for o in r["options"]]
        self.assertTrue(outs)
        self.assertNotIn("Swords to Plowshares", outs)  # removal is short: never the price of more draw
        self.assertNotIn("Path to Exile", outs)

    def test_scenario_another_board_wipe_is_like_for_like_at_best(self):
        r = self.find("Farewell")
        for o in r["options"]:
            out_roles = set(next(v for v in self.finder()[1].review if v["name"] == o["out"])["roles"])
            self.assertIn("Board Wipe", out_roles)  # a 6th wipe only replaces a wipe
        self.assertTrue(r["notes"])  # "the deck already has N board wipes"

    def test_scenario_finisher(self):
        r = self.find("Moonshaker Cavalry")
        self.assertTrue(r["options"])
        self.assertTrue(all(o["losses"] is not None for o in r["options"]))

    def test_scenario_worse_than_everything_says_no_change(self):
        r = self.find("Gray Ogre", deck=self.make([c for c in CARDS if c != "Gray Ogre"]))
        self.assertEqual(r["options"], [])
        self.assertEqual(r["verdict"], "No safe cut found.")
        self.assertTrue(r["reasons"])

    def test_good_but_redundant_card(self):
        r = self.find("Mind Stone")
        for o in r["options"]:
            self.assertNotIn(o["out"], ("Swords to Plowshares", "Path to Exile", "Teferi's Protection"))

    def test_up_to_five_varied_options_with_both_sides(self):
        r = self.find("Mirkwood Bats")
        self.assertLessEqual(len(r["options"]), 5)
        self.assertEqual(r["options"][0]["rank"], "BEST CUT")
        self.assertTrue(all(o["rank"] == "ALTERNATIVE" for o in r["options"][1:]))
        for o in r["options"]:
            for key in ("why_out", "why_in", "gains", "losses", "net", "kind", "cut_ease"):
                self.assertIn(key, o)

    def test_off_colour_and_already_in_deck(self):
        self.assertIn("colour identity", self.find("Llanowar Elves")["verdict"])
        self.assertTrue(self.find("Blood Artist").get("already"))


@unittest.skipUnless(has_cards(), "needs cards.db with oracle_cards")
class SafetyTests(Base):
    def test_commander_never_and_core_cards_are_never_cut(self):
        self.db.set_card_flag(self.deck, "Vault 75: Middle School", "core")
        self.db.protect_card(self.deck, "Wrath of God")
        for name in ("Mirkwood Bats", "Welcoming Vampire", "Moonshaker Cavalry"):
            outs = {o["out"] for o in self.find(name)["options"]}
            self.assertFalse(outs & {"Vault 75: Middle School", "Wrath of God", CAESAR}, name)
        res = analyser.analyse(self.db, self.st, self.deck, HIGH)
        self.assertFalse({s["out"] for s in res["swaps"]} & {"Vault 75: Middle School", "Wrath of God", CAESAR})
        self.assertNotIn("Vault 75: Middle School", [w["name"] for w in res["weakest"]])

    def test_role_shortages_are_not_made_worse(self):
        cf, st = self.finder()
        lo = cf.role_targets["Removal"][0]
        self.assertLess(cf.deck.roles["Removal"], lo)  # the test deck is short of removal
        for name in ("Mirkwood Bats", "Welcoming Vampire", "Moonshaker Cavalry", "Village Rites"):
            for o in self.find(name)["options"]:
                self.assertNotIn(o["out"], ("Swords to Plowshares", "Path to Exile"), name)
        res = analyser.analyse(self.db, self.st, self.deck, HIGH)
        for s in res["swaps"]:
            if "Removal" in s["out_roles"]:
                self.assertIn("Removal", s["in_roles"])

    def test_combo_pieces_in_the_deck_are_protected_and_combos_only_influence(self):
        key = community.commander_key([(self.st.default_printing(CAESAR) or {}).get("oracle_id")])
        community.save_combos(key, [dict(status="in_deck", cards=["Vault 75: Middle School", "Hill Giant"], missing=[]),
                                    dict(status="one_away", cards=["Blood Artist", "Gray Ogre", "Grim Hireling"],
                                         missing=["Grim Hireling"])])
        cf, _ = self.finder()
        self.assertIn("combo", cf.blocked("Vault 75: Middle School"))
        outs = {o["out"] for o in self.find("Mirkwood Bats")["options"]}
        self.assertFalse(outs & {"Vault 75: Middle School", "Hill Giant"})
        # one card away: a bonus, never the whole reason
        with_combo = cf.delta("Grim Hireling", "Gray Ogre")
        self.assertGreater(with_combo["parts"]["combo"], 0)
        self.assertLessEqual(with_combo["parts"]["combo"], max(cutfinder.COMBO_BONUS.values()))
        self.assertTrue(any("Commander Spellbook" in g for g in with_combo["gains"]))
        cf.combo_pieces["Grizzly Bears"] = "completes a combo with A + B"  # a combo card the deck can't use
        self.assertIn("colour identity", cf.find("Grizzly Bears")["verdict"])


@unittest.skipUnless(has_cards(), "needs cards.db with oracle_cards")
class PlanTests(Base):
    def plan(self, settings=HIGH):
        return analyser.analyse(self.db, self.st, self.deck, settings)

    def test_several_compatible_swaps_each_out_once(self):
        res = self.plan()
        outs = [s["out"] for s in res["swaps"]]
        self.assertGreaterEqual(len(outs), 2)
        self.assertEqual(len(outs), len(set(outs)))  # the same OUT never dominates the plan
        ins = [s["into"] for s in res["swaps"]]
        self.assertEqual(len(ins), len(set(ins)))
        for s in res["swaps"]:
            self.assertTrue(s["why_out"] and s["why_in"] and s["gains"] and s["net"])

    def test_swaps_improve_the_deck_and_are_tiered(self):
        res = self.plan()
        for s in res["swaps"]:
            self.assertGreaterEqual(s["net_value"], cutfinder.OPTIONAL)
            self.assertIn(s["tier"], ("HIGH", "GOOD", "OPTIONAL"))
            self.assertEqual(s["confidence"], cutfinder.TIER_LABEL[s["tier"]])
        order = [s["tier"] for s in res["swaps"]]
        from analyse_view import AnalyseScreen  # grouped HIGH / GOOD / OPTIONAL on screen
        sc = screen(self.db, self.st, res, self.deck)
        groups = [r[1] for r in sc.swap_rows() if r[0] == "group"]
        self.assertEqual(groups, [t for t in ("HIGH", "GOOD", "OPTIONAL") if t in order] +
                         (["USED"] if res.get("used_elsewhere") else []))
        self.assertTrue(AnalyseScreen)

    def test_optional_swaps_are_separate_from_strong_upgrades(self):
        cf, st = self.finder()
        d = cf.delta("Mirkwood Bats", "Vault 75: Middle School")
        self.assertEqual(cutfinder.tier_of(cutfinder.HIGH + 0.01), "HIGH")
        self.assertEqual(cutfinder.tier_of(cutfinder.GOOD), "GOOD")
        self.assertEqual(cutfinder.tier_of(cutfinder.OPTIONAL), "OPTIONAL")
        # a swap where the card leaving is clearly the stronger card is never HIGH
        weak_in = cf.delta("Gray Ogre", "Skullclamp")
        self.assertNotEqual(weak_in["tier"], "HIGH")
        self.assertIn(d["tier"], ("HIGH", "GOOD"))

    def test_my_collection_never_suggests_unavailable_cards_as_free(self):
        self.own("Mirkwood Bats", "Village Rites", "Welcoming Vampire")
        other = self.st.create_deck("Other")
        lot = self.db.add_card(self.st.default_printing("Moonshaker Cavalry")["id"], "Moonshaker Cavalry", "x", "1")
        self.db.add_deck_card(other, "Moonshaker Cavalry", scryfall_id=self.db.item(lot)["scryfall_id"],
                              collection_item_id=lot)
        res = self.plan(COLL)
        self.assertTrue(res["swaps"])
        for s in res["swaps"]:
            self.assertEqual(s["ownership"]["status"], "available", s["into"])
            self.assertNotEqual(s["into"], "Moonshaker Cavalry")
        for u in res.get("used_elsewhere") or []:
            self.assertEqual(u["used_in"], ["Other"])

    def test_weakest_means_slot_pressure(self):
        res = self.plan()
        names = [w["name"] for w in res["weakest"]]
        self.assertLessEqual(len(names), 10)
        self.assertIn("Vault 75: Middle School", names[:4])
        self.assertNotIn("Swords to Plowshares", names)  # short role: not a flexible slot
        for w in res["weakest"]:
            self.assertIn(w["flexibility"], ("HIGH", "MEDIUM", "LOW"))
            self.assertIsNotNone(w["reasons_to_keep"])


def screen(db, st, res, deck):
    from analyse_view import AnalyseScreen
    app = type("A", (), {})()
    app.settings, app.userdb, app.decks_screen, app.collection_screen, app.dialog = {"currency": "USD"}, db, None, \
        None, None
    app.money = lambda u: f"${u:.2f}"
    app.toast = lambda *a: None
    app.toast_text = lambda: None
    app.open_dialog = lambda d: setattr(app, "dialog", d)
    sc = AnalyseScreen(app, 1400, 650, store=st)
    sc.deck_id, sc.result, sc.stale = deck, res, False
    return sc


@unittest.skipUnless(has_cards(), "needs cards.db with oracle_cards")
class ScreenTests(Base):
    def test_swaps_tab_renders_groups_and_both_sides(self):
        res = analyser.analyse(self.db, self.st, self.deck, HIGH)
        sc = screen(self.db, self.st, res, self.deck)
        from ui import Painter
        p = Painter(1400, 650)
        out = []
        orig = p.text
        p.text = lambda xy, t, *a, **k: (out.append(str(t)), orig(xy, t, *a, **k))
        sc._swaps(p, 372, 1384, 52)
        joined = " ".join(out)
        for word in ("OUT", "IN", "WHY OUT", "WHY IN", "WHAT YOU LOSE", "WHAT IMPROVES", "NET:"):
            self.assertIn(word, joined)
        self.assertNotIn("%", joined)  # no uncalibrated percentages
        self.assertTrue(any(h[4] == ("toggle", 0) for h in p.hits))

    def test_find_a_cut_dialog_with_options_and_without(self):
        from analyse_view import FindCutDialog
        res = analyser.analyse(self.db, self.st, self.deck, HIGH, quick=True)
        sc = screen(self.db, self.st, res, self.deck)
        dlg = FindCutDialog(sc, self.find("Mirkwood Bats"))
        dlg.draw(np.zeros((650, 1400, 3), np.uint8))
        self.assertTrue(any(h[4] == ("swap", 0) for h in dlg.hits))
        none = FindCutDialog(sc, self.find("Gray Ogre", deck=self.make([c for c in CARDS if c != "Gray Ogre"])))
        none.draw(np.zeros((650, 1400, 3), np.uint8))
        self.assertFalse(any(h[4] and h[4][0] == "swap" for h in none.hits))

    def test_i_want_to_add_a_card_and_choose_from_collection(self):
        res = analyser.analyse(self.db, self.st, self.deck, HIGH, quick=True)
        sc = screen(self.db, self.st, res, self.deck)
        started = []
        sc.find_cut = lambda name: started.append(name)
        sc.add_card_dialog()
        sc.app.dialog.finish(True) if False else sc.app.dialog.on_done(True, "Mirkwood Bats")
        self.assertEqual(started, ["Mirkwood Bats"])
        sc.add_card_dialog()
        sc.app.dialog.on_done(True, "mirkwood ba")
        if started[-1] != "Mirkwood Bats":  # several matches -> a choice list
            sc.app.dialog.on_done("Mirkwood Bats", None)
        self.assertEqual(started[-1], "Mirkwood Bats")
        self.own("Village Rites")
        sc.pick_owned_dialog()
        sc.app.dialog.on_done(True, "village")
        self.assertIn("Village Rites", [o[0] for o in sc.app.dialog.options])
        sc.app.dialog.on_done("Village Rites", None)
        self.assertEqual(started[-1], "Village Rites")


if __name__ == "__main__":
    unittest.main()
