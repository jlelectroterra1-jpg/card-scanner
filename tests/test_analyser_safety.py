"""Analyser cut safety - regression tests for suggestions that must never happen again.

* a high-impact engine is never swapped for a cheap one-off just because both "draw"
* cards the analyser doesn't understand are protected, not cut
* core strategy pieces, sole-role / unique-function cards and Game Changers stay
* pinned ("Never cut") cards never appear as OUT
* every cut has positive evidence that outweighs its protection
* cut / replacement / swap confidence are separate; weak swaps aren't shown as strong
* "cards worth considering" when a replacement is good but no cut is confident
* the package size is a maximum, never a target ("No high-confidence swaps found" is fine)
* strategy uncertainty and your own strategy tags
* every "works with N cards" claim comes with the N cards
* realistic decks (tests/real_decks.py): no must-keep card is ever suggested as a cut

    python -m unittest tests.test_analyser_safety -v
"""
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import analyser  # noqa: E402
import cardroles  # noqa: E402
import real_decks  # noqa: E402
import synergy  # noqa: E402
import userdb  # noqa: E402
from decks import DeckStore  # noqa: E402
from userdb import UserDB  # noqa: E402

CARDS_DB = os.path.join(ROOT, "data", "cards.db")


def has_cards():
    if not os.path.exists(CARDS_DB):
        return False
    con = sqlite3.connect(CARDS_DB)
    ok = con.execute("SELECT 1 FROM sqlite_master WHERE name = 'oracle_cards'").fetchone() is not None
    con.close()
    return ok


def oracle(name):
    con = sqlite3.connect(CARDS_DB)
    con.row_factory = sqlite3.Row
    info = dict(con.execute("SELECT * FROM oracle_cards WHERE name = ?", (name,)).fetchone())
    con.close()
    return info, cardroles.classify(info)


SETTINGS = dict(mode="all", goal="improve", tags=None, budget_usd=None)


@unittest.skipUnless(has_cards(), "needs data/cards.db with oracle_cards")
class FunctionTests(unittest.TestCase):
    def test_role_subtypes(self):
        want = {"Beast Whisperer": ("Card Draw", "creature-engine draw"), "Night's Whisper": ("Card Draw", "burst draw"),
                "Rhystic Study": ("Card Draw", "repeatable draw"), "Sol Ring": ("Ramp", "mana rock"),
                "Cultivate": ("Ramp", "land ramp"), "Birds of Paradise": ("Ramp", "mana creature"),
                "Swords to Plowshares": ("Removal", "exile"), "Grave Pact": ("Removal", "repeatable removal"),
                "Teferi's Protection": ("Protection", "phase out"), "Craterhoof Behemoth": ("Finisher", "overrun"),
                "Lightning Greaves": ("Protection", "hexproof/shroud")}
        for name, (role, sub) in want.items():
            with self.subTest(name):
                info, roles = oracle(name)
                self.assertIn(sub, synergy.functions(info, roles).get(role, []))

    def test_self_exile_transform_is_not_a_board_wipe(self):
        """Mass Polymorph / Synthetic Destiny exile YOUR creatures to transform them - they were
        once called "redundant board wipes" in a real Jace deck."""
        for name in ("Mass Polymorph", "Synthetic Destiny"):
            self.assertNotIn("Board Wipe", oracle(name)[1], name)
        for name in ("Wrath of God", "Farewell", "Evacuation", "Pernicious Deed", "Cyclonic Rift"):
            self.assertIn("Board Wipe", oracle(name)[1], name)

    def test_engine_is_not_interchangeable_with_a_one_off(self):
        """High-impact value engine -> cheap generic draw spell: never, even though both 'draw'."""
        engine, burst = oracle("Beast Whisperer"), oracle("Night's Whisper")
        ok, why = synergy.compatible(*engine, *burst, "Card Draw")
        self.assertFalse(ok)
        self.assertIn("engine", why)
        archon, cheap = oracle("Archon of Cruelty"), oracle("Divination")
        self.assertFalse(synergy.compatible(*archon, *cheap, "Card Draw")[0])
        self.assertTrue(synergy.compatible(*burst, *oracle("Divination"), "Card Draw")[0])

    def test_interaction_graph_finds_the_engine_with_reasons(self):
        names = ["Krenko, Mob Boss", "Goblin Bombardment", "Impact Tremors", "Skirk Prospector", "Lightning Bolt"]
        g = synergy.InteractionGraph({n: oracle(n) for n in names}, ["Krenko, Mob Boss"])
        bomb = dict(g.interactions("Goblin Bombardment"))
        self.assertIn("Krenko, Mob Boss", bomb)
        self.assertTrue("tokens" in bomb["Krenko, Mob Boss"] or "sacrifice" in bomb["Krenko, Mob Boss"])
        self.assertTrue(g.with_commander("Impact Tremors"))
        self.assertTrue(g.with_commander("Skirk Prospector"))
        self.assertEqual(g.interactions("Lightning Bolt"), [])  # no made-up synergy


@unittest.skipUnless(has_cards(), "needs data/cards.db with oracle_cards")
class RealDeckTests(unittest.TestCase):
    """The four realistic decks, analysed once."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="tmp_safety_", dir=HERE)
        cls._old = userdb.BACKUP_DIR
        userdb.BACKUP_DIR = os.path.join(cls.tmp, "b")
        cls.db = UserDB(os.path.join(cls.tmp, "u.db"))
        cls.st = DeckStore(cls.db)
        cls.ids, cls.res = {}, {}
        for name in real_decks.DECKS:
            cls.ids[name], missing = real_decks.build(cls.db, cls.st, name)
            assert not missing, (name, missing)
            cls.res[name] = analyser.analyse(cls.db, cls.st, cls.ids[name], SETTINGS)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        userdb.BACKUP_DIR = cls._old
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def review(self, deck, card):
        return next(v for v in self.res[deck]["cut_review"] if v["name"] == card)

    def outs(self, deck):
        return {s["out"] for s in self.res[deck]["swaps"]}

    # ---- the most important rule
    def test_no_must_keep_card_is_ever_suggested_as_a_cut(self):
        for deck, spec in real_decks.DECKS.items():
            with self.subTest(deck):
                self.assertEqual(sorted(set(spec["must_keep"]) & self.outs(deck)), [])
                for card in spec["must_keep"]:
                    st = self.review(deck, card)["status"] if any(
                        v["name"] == card for v in self.res[deck]["cut_review"]) else "n/a"
                    self.assertFalse(st.startswith(("suggested", "possible")), f"{deck}: {card} is a cut candidate")

    def test_archon_of_cruelty_is_protected(self):
        v = self.review("Meren Aristocrats", "Archon of Cruelty")
        self.assertFalse(v["status"].startswith(("suggested", "possible")))
        texts = " ".join(t for t, _ in v["protection"])
        self.assertIn("engine", texts)

    def test_misunderstood_cards_are_protected_not_cut(self):
        for deck, card in (("Krenko Goblins", "Shared Animosity"), ("Meren Aristocrats", "Living Death"),
                           ("Meren Aristocrats", "Buried Alive"), ("Krenko Goblins", "Wheel of Misfortune")):
            with self.subTest(card):
                v = self.review(deck, card)
                self.assertNotIn(card, self.outs(deck))
                self.assertFalse(any(t.startswith("No clear deck job") for t, _ in v["evidence"]), v["evidence"])
                if not any(r.confidence >= 0.6 for r in oracle(card)[1].values()):
                    self.assertTrue(any("can't tell what this card does" in t for t, _ in v["protection"]))

    def test_unique_function_and_game_changers_are_protected(self):
        tp = self.review("Atraxa Superfriends", "Teferi's Protection")
        self.assertTrue(any(t.startswith("Only card in the deck doing") for t, _ in tp["protection"]), tp["protection"])
        for card in ("Smothering Tithe", "Rhystic Study", "Cyclonic Rift", "Demonic Tutor"):
            v = self.review("Atraxa Superfriends", card)
            self.assertTrue(any("Game Changer" in t for t, _ in v["protection"]), card)

    def test_every_swap_uses_a_slot_the_deck_can_spare(self):
        """A swap no longer needs the OUT card to be 'bad' - but the bulk plan only takes a slot the
        deck can spare (real doubts, an over-covered job, or a clearly better like-for-like card),
        never a valued card the review has no doubts about, and always improves the deck."""
        import cutfinder
        for deck, res in self.res.items():
            cf, _ = cutfinder.finder_for(self.db, self.st, self.ids[deck], SETTINGS)
            for s in res["swaps"]:
                self.assertTrue(s["why_out"] and s["gains"])
                self.assertGreaterEqual(s["net_value"], cutfinder.OPTIONAL)
                if cf.valued(s["out"]):
                    self.assertTrue(cf.doubted(s["out"]), (deck, s["out"]))

    def test_off_colour_cards_are_found(self):
        self.assertIn("Lightning Bolt", self.outs("Atraxa Superfriends"))
        self.assertIn("Counterspell", self.outs("Krenko Goblins"))

    def test_high_confidence_only_when_the_card_leaving_is_in_doubt(self):
        for deck, res in self.res.items():
            for s in res["swaps"]:
                if s["tier"] == "HIGH":
                    v = self.review(deck, s["out"])
                    self.assertGreater(v["E"], 0, (deck, s["out"]))

    def test_synergy_claims_are_evidenced(self):
        for res in self.res.values():
            for item in [dict(why=s["why_in"], links=s["in_links"], total=s["in_links_total"]) for s in res["swaps"]] + \
                        [dict(why=c["why"], links=c["links"], total=c["links_total"]) for c in res["considered"]]:
                for line in item["why"]:
                    m = re.search(r"Works with (\d+) cards", line)
                    if m:
                        self.assertEqual(int(m.group(1)), item["total"] if item["total"] < 1000 else 0)
                        self.assertTrue(item["links"])
                        examples = re.findall(r"e\.g\. (.*)\)", line)[0]
                        named = [l["card"] for l in item["links"] if l["card"] in examples]
                        self.assertTrue(named, examples)  # the examples are real linked cards
                        self.assertEqual(len(examples), len(", ".join(sorted(named, key=examples.index))), examples)

    def test_cards_worth_considering_without_a_forced_cut(self):
        res = self.res["Talrand Spells"]
        self.assertTrue(res["considered"])
        self.assertFalse({c["name"] for c in res["considered"]} & {s["into"] for s in res["swaps"]})
        for c in res["considered"]:  # need-driven tiers, no fixed count (see test_recommend)
            self.assertIn(c["tier"], ("EXCELLENT", "STRONG", "WORTH", "NICHE"))
            self.assertTrue(c["labels"])

    def test_cost_reducing_cards_are_not_called_expensive(self):
        v = self.review("Krenko Goblins", "Blasphemous Act")
        self.assertFalse(any("Costs 9 mana" in t for t, _ in v["evidence"]))

    def test_debug_review_text(self):
        text = analyser.describe_review([dict(name=v["name"], status=v["status"], E=v["E"], P=v["P"],
                                              cut_confidence=v["cut_confidence"], evidence=v["evidence"],
                                              protection=v["protection"]) for v in self.res["Meren Aristocrats"]["cut_review"]])
        self.assertIn("Card: Archon of Cruelty", text)
        self.assertIn("protection  +", text)


@unittest.skipUnless(has_cards(), "needs data/cards.db with oracle_cards")
class PinAndStrategyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tmp_safety2_", dir=HERE)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        old = userdb.BACKUP_DIR
        userdb.BACKUP_DIR = os.path.join(self.tmp, "b")
        self.addCleanup(setattr, userdb, "BACKUP_DIR", old)
        self.db = UserDB(os.path.join(self.tmp, "u.db"))
        self.addCleanup(self.db.close)
        self.st = DeckStore(self.db)

    def test_pinned_cards_never_appear_as_cuts(self):
        deck, _ = real_decks.build(self.db, self.st, "Atraxa Superfriends")
        res = analyser.analyse(self.db, self.st, deck, SETTINGS)
        outs = [s["out"] for s in res["swaps"]]
        self.assertTrue(outs)
        fp = analyser.current_fingerprint(self.db, deck, SETTINGS)
        for card in outs:
            self.db.protect_card(deck, card)
        self.assertNotEqual(fp, analyser.current_fingerprint(self.db, deck, SETTINGS))  # analysis now out of date
        res2 = analyser.analyse(self.db, self.st, deck, SETTINGS)
        self.assertFalse(set(outs) & {s["out"] for s in res2["swaps"]})
        for card in outs:
            v = next(v for v in res2["cut_review"] if v["name"] == card)
            self.assertEqual(v["status"], "pinned")
        self.assertEqual(sorted(res2["protected"]), sorted(outs))
        self.db.protect_card(deck, outs[0], False)  # Unprotect
        self.assertNotIn(outs[0], self.db.protected_cards(deck))

    def test_everything_protected_gives_no_swaps_but_still_ideas(self):
        deck, _ = real_decks.build(self.db, self.st, "Krenko Goblins")
        for r in self.st.deck_cards(deck):
            self.db.protect_card(deck, r["card_name"])
        res = analyser.analyse(self.db, self.st, deck, SETTINGS)
        self.assertEqual(res["swaps"], [])  # "No high-confidence swaps found" - a valid result
        self.assertTrue(res["considered"])

    def test_manual_strategy_wins(self):
        deck, _ = real_decks.build(self.db, self.st, "Krenko Goblins")
        res = analyser.analyse(self.db, self.st, deck, dict(SETTINGS, tags=["Graveyard", "Reanimator"]))
        self.assertEqual(res["active_themes"], ["Graveyard", "Reanimator"])
        self.assertFalse(res["strategy_uncertain"])
        for s in res["swaps"]:
            self.assertFalse(any("tokens strategy" in w or "tribal strategy" in w for w in s["why_in"]), s["why_in"])

    def test_uncertain_strategy_means_no_strong_suggestions(self):
        deck = self.st.create_deck("Unclear")
        cards = ["Sol Ring", "Arcane Signet", "Llanowar Elves", "Grizzly Bears", "Colossal Dreadmaw", "Giant Growth",
                 "Divination", "Naturalize", "Rampant Growth", "Craw Wurm", "Hill Giant", "Shock"]
        text = "Commander\n1 Isamaru, Hound of Konda\nDeck\n" + "\n".join("1 " + c for c in cards) + "\n30 Plains"
        self.st.commit_import(deck, self.st.parse_decklist(text))
        res = analyser.analyse(self.db, self.st, deck, SETTINGS)
        self.assertTrue(res["strategy_uncertain"])
        self.assertTrue(res["strategy_note"])
        for s in res["swaps"]:
            if s["tier"] == "HIGH":  # unclear strategy: only removing a card that can't be played here
                self.assertTrue(any("can't be played here" in g for g in s["gains"]), s)


if __name__ == "__main__":
    unittest.main()
