"""Deck UNDERSTANDING - the analyser should know what the commander and deck are trying to do,
so the importance of a card depends on the deck:

* archetypes (tokens, aristocrats, voltron, spellslinger, reanimator, tribal, landfall,
  control, +1/+1 counters, artifacts, enchantress): a key piece is valued much more in a deck
  built around it than in a same-colour deck that isn't
* commander profiles (mechanics, plan, engine chain, needs) for any commander
* new roles / subroles (trigger + token doublers, anthems, finishers that don't say "win",
  tutors, wheels), interaction strengths, engines
* deck-specific target ranges with reasons, analysis confidence
* "Find a card to replace", "Find my weakest cards", Core / Not important feedback
* price / print count are not quality; the zero-swaps and out-of-date screens

    python -m unittest tests.test_analyser_understanding -v
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import analyser  # noqa: E402
import cardroles  # noqa: E402
import commander  # noqa: E402
import synergy  # noqa: E402
import userdb  # noqa: E402
from decks import DeckStore  # noqa: E402
from userdb import UserDB  # noqa: E402

CARDS_DB = os.path.join(ROOT, "data", "cards.db")
SET = dict(mode="collection", goal="improve", tags=None, budget_usd=None)
FILLER = ["Sol Ring", "Arcane Signet", "Mind Stone", "Commander's Sphere", "Wayfarer's Bauble", "Thought Vessel"]

# archetype: (commander built for it, cards that make the plan, KEY card,
#             same-colour commander NOT built for it, unrelated cards for that deck)
ARCHETYPES = {
    "Tokens": ("Rhys the Redeemed", ["Secure the Wastes", "Raise the Alarm", "Intangible Virtue", "Divine Visitation",
                                     "Cathars' Crusade", "Parallel Lives", "Spectral Procession", "Avenger of Zendikar"],
               "Anointed Procession", "Selvala, Explorer Returned", ["Wrath of God", "Kodama's Reach", "Cultivate"]),
    "Aristocrats": ("Teysa Karlov", ["Blood Artist", "Zulaport Cutthroat", "Bastion of Remembrance", "Bitterblossom",
                                     "Carrion Feeder", "Grave Pact", "Pitiless Plunderer", "Cruel Celebrant"],
                    "Viscera Seer", "Kambal, Consul of Allocation", ["Wrath of God", "Night's Whisper", "Doom Blade"]),
    "Voltron": ("Rafiq of the Many", ["Swiftfoot Boots", "Rancor", "Ethereal Armor", "All That Glitters",
                                      "Bonesplitter", "Puresteel Paladin", "Sigarda's Aid", "Shielded by Faith"],
                "Sword of Fire and Ice", "Arcades, the Strategist", ["Wall of Omens", "Counterspell", "Cultivate"]),
    "Spellslinger": ("Talrand, Sky Summoner", ["Ponder", "Preordain", "Opt", "Counterspell", "Mana Leak",
                                               "Archmage Emeritus", "Murmuring Mystic", "Frantic Search"],
                     "Brainstorm", "Arcanis the Omnipotent", ["Frost Titan", "Sphinx of Uthuun", "Air Elemental"]),
    "Reanimator": ("Meren of Clan Nel Toth", ["Entomb", "Buried Alive", "Stitcher's Supplier", "Satyr Wayfinder",
                                              "Victimize", "Living Death", "Golgari Grave-Troll", "Animate Dead"],
                   "Reanimate", "Glissa, the Traitor", ["Cultivate", "Doom Blade", "Thragtusk"]),
    "Tribal": ("Edgar Markov", ["Stromkirk Captain", "Bloodline Keeper", "Cordial Vampire", "Vampire Nocturnus",
                                "Sorin, Vengeful Bloodlord", "Indulgent Aristocrat", "Kalitas, Traitor of Ghet",
                                "Captivating Vampire"],
               "Legion Lieutenant", "Kaalia of the Vast", ["Wrath of God", "Lightning Bolt", "Night's Whisper"]),
    "Landfall": ("Tatyova, Benthic Druid", ["Cultivate", "Kodama's Reach", "Rampant Growth", "Explore",
                                            "Azusa, Lost but Seeking", "Avenger of Zendikar", "Rampaging Baloths",
                                            "Roil Elemental"],
                 "Lotus Cobra", "Edric, Spymaster of Trest", ["Counterspell", "Brainstorm", "Llanowar Elves"]),
    "Counters": ("Ezuri, Claw of Progress", ["Experiment One", "Champion of Lambholt", "Inexorable Tide",
                                             "Vorinclex, Monstrous Raider", "Corpsejack Menace", "Gyre Sage",
                                             "Kalonian Hydra", "Master Biomancer"],
                 "Hardened Scales", "Edric, Spymaster of Trest", ["Counterspell", "Brainstorm", "Cultivate"]),
    "Artifacts": ("Urza, Lord High Artificer", ["Ornithopter", "Etherium Sculptor", "Thopter Spy Network",
                                                "Whir of Invention", "Sai, Master Thopterist", "Thopter Foundry",
                                                "Cranial Plating", "Retrofitter Foundry"],
                  "Reckless Fireweaver" if False else "Thopter Assembly", "Arcanis the Omnipotent",
                  ["Frost Titan", "Counterspell", "Brainstorm"]),
    "Enchantress": ("Sythis, Harvest's Hand", ["Argothian Enchantress", "Setessan Champion", "Sanctum Weaver",
                                               "Rancor", "Utopia Sprawl", "Wild Growth", "Sigil of the Empty Throne",
                                               "Ghostly Prison"],
                    "Enchantress's Presence", "Selvala, Explorer Returned", ["Wrath of God", "Cultivate", "Explore"]),
}


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
    r = con.execute("SELECT * FROM oracle_cards WHERE name = ?", (name,)).fetchone()
    con.close()
    return dict(r)


class DBCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tmp_underst_", dir=HERE)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        old = userdb.BACKUP_DIR
        userdb.BACKUP_DIR = os.path.join(self.tmp, "b")
        self.addCleanup(setattr, userdb, "BACKUP_DIR", old)
        self.db = UserDB(os.path.join(self.tmp, "u.db"))
        self.addCleanup(self.db.close)
        self.st = DeckStore(self.db)

    def deck(self, name, cmd, cards, basics=20):
        deck = self.st.create_deck(name)
        text = f"Commander\n1 {cmd}\nDeck\n" + "\n".join("1 " + c for c in cards) + f"\n{basics} Wastes"
        parsed = self.st.parse_decklist(text)
        missing = [r["name"] for r in parsed if not r["card"]]
        self.assertEqual(missing, [], f"{name}: unknown cards")
        self.st.commit_import(deck, parsed)
        return deck

    def review(self, deck_id, settings=SET):
        st = analyser.prepare(self.db, self.st, deck_id, settings)
        return st, {v["name"]: v for v in st.review}


@unittest.skipUnless(has_cards(), "needs cards.db with oracle_cards")
class ArchetypeImportanceTests(DBCase):
    """The same card matters more in the deck built around it."""

    def check(self, archetype):
        home_cmd, plan, key, plain_cmd, plain_cards = ARCHETYPES[archetype]
        home = self.deck(f"{archetype} deck", home_cmd, plan + [key] + FILLER)
        plain = self.deck(f"plain {archetype}", plain_cmd, plain_cards + [key] + FILLER)
        _, rh = self.review(home)
        _, rp = self.review(plain)
        vh, vp = rh[key], rp[key]
        self.assertFalse(vh["eligible"], f"{key} must not be a cut in a {archetype} deck: {vh['evidence']}")
        self.assertGreater(vh["P"], vp["P"] + 0.5,
                           f"{key}: protection {vh['P']} in the {archetype} deck vs {vp['P']} elsewhere")
        self.assertGreater(vh["strong"] + vh["moderate"], vp["strong"] + vp["moderate"])
        return vh

    def test_token_engine_valued_in_token_decks(self):
        v = self.check("Tokens")
        self.assertTrue(v["engines"])

    def test_sacrifice_outlet_valued_in_aristocrats(self):
        v = self.check("Aristocrats")
        self.assertIn("Sacrifice engine", v["engines"])

    def test_equipment_matters_in_voltron(self):
        self.check("Voltron")

    def test_cheap_instants_matter_in_spellslinger(self):
        v = self.check("Spellslinger")
        self.assertNotEqual(v["commander_synergy"], "none")

    def test_reanimation_matters_in_reanimator(self):
        self.check("Reanimator")

    def test_tribal_lords_matter_in_tribal(self):
        self.check("Tribal")

    def test_land_triggers_matter_in_landfall(self):
        v = self.check("Landfall")
        self.assertIn("Landfall engine", v["engines"])

    def test_counters_payoffs_matter_in_counters(self):
        self.check("Counters")

    def test_artifact_payoffs_matter_in_artifacts(self):
        self.check("Artifacts")

    def test_enchantress_matters_in_enchantress(self):
        self.check("Enchantress")

    def test_control_deck_wants_more_interaction_than_a_token_deck(self):
        control = self.deck("control", "Grand Arbiter Augustin IV",
                            ["Counterspell", "Mana Leak", "Supreme Verdict", "Wrath of God", "Swords to Plowshares",
                             "Path to Exile", "Dovin's Veto", "Cyclonic Rift"] + FILLER)
        tokens = self.deck("tokens", "Rhys the Redeemed", ARCHETYPES["Tokens"][1] + FILLER)
        sc, _ = self.review(control)
        stt, _ = self.review(tokens)
        self.assertGreater(sc.targets["Board Wipe"]["target"][1], stt.targets["Board Wipe"]["target"][1])
        self.assertTrue(stt.targets["Board Wipe"]["why"])


@unittest.skipUnless(has_cards(), "needs cards.db with oracle_cards")
class CommanderProfileTests(unittest.TestCase):
    def test_caesar_plan(self):
        p = commander.profile([oracle("Caesar, Legion's Emperor")])
        keys = {m["key"] for m in p["mechanics"]}
        self.assertTrue({"attack", "sacrifice", "tokens", "attackers", "draw", "damage", "wide"} <= keys, keys)
        self.assertEqual(p["primary"], "Tokens")
        self.assertIn("Aristocrats", p["supporting"])
        self.assertIn("Combat", p["supporting"])
        chain = " -> ".join(p["chain"])
        self.assertIn("sacrifice", chain)
        self.assertIn("tokens", chain)
        needs = {n["key"] for n in p["needs"]}
        self.assertIn("sacrifice fodder", needs)
        self.assertGreaterEqual(p["understanding"], 0.75)

    def test_generic_commanders(self):
        want = {"Talrand, Sky Summoner": "Spellslinger", "Meren of Clan Nel Toth": "Graveyard",
                "Krenko, Mob Boss": "Tokens", "Rafiq of the Many": "Combat", "Tatyova, Benthic Druid": "Landfall",
                "Sythis, Harvest's Hand": "Enchantments", "Atraxa, Praetors' Voice": "Proliferate",
                "Storm, Force of Nature": "Spellslinger"}
        for name, plan in want.items():
            with self.subTest(name):
                self.assertEqual(commander.profile([oracle(name)])["primary"], plan)

    def test_keyword_lines_and_ability_words_are_not_counted_as_unknown(self):
        p = commander.profile([oracle("Storm, Force of Nature")])
        self.assertEqual(p["understood"], p["clauses"])


@unittest.skipUnless(has_cards(), "needs cards.db with oracle_cards")
class RoleTests(unittest.TestCase):
    def roles(self, name):
        return cardroles.classify(oracle(name))

    def test_new_roles(self):
        want = {"Isshin, Two Heavens as One": "Trigger Doubler", "Teysa Karlov": "Trigger Doubler",
                "Anointed Procession": "Token Doubler", "Exalted Sunborn": "Token Doubler",
                "Glorious Anthem": "Anthem", "Adeline, Resplendent Cathar": "Attack Triggers",
                "Bone-Cairn Butcher": "Token Generation", "Wasteland Raider": "Removal",
                "Voice of Victory": "Protection", "Deepglow Skate": "+1/+1 Counters"}
        for name, role in want.items():
            with self.subTest(name):
                self.assertIn(role, self.roles(name))

    def test_finishers_that_dont_say_win(self):
        for name, sub in (("Impact Tremors", "mass drain"), ("Neriv, Heart of the Storm", "damage multiplier"),
                          ("Craterhoof Behemoth", "overrun"), ("Iroas, God of Victory", "mass evasion"),
                          ("Aggravated Assault", "extra combat")):
            with self.subTest(name):
                info = oracle(name)
                roles = cardroles.classify(info)
                self.assertIn("Finisher", roles)
                self.assertIn(sub, synergy.functions(info, roles)["Finisher"])

    def test_tutors_and_land_search(self):
        for name, sub in (("Demonic Tutor", "unrestricted tutor"), ("Worldly Tutor", "creature tutor"),
                          ("Enlightened Tutor", "artifact tutor"), ("Vampiric Tutor", "top-deck tutor")):
            with self.subTest(name):
                info = oracle(name)
                roles = cardroles.classify(info)
                self.assertIn("Tutor", roles)
                self.assertIn(sub, synergy.functions(info, roles)["Tutor"])
        self.assertNotIn("Tutor", self.roles("Cultivate"))  # land search is ramp

    def test_wheel_and_subroles(self):
        info = oracle("Wheel of Fortune")
        self.assertIn("wheel", synergy.functions(info, cardroles.classify(info))["Card Draw"])
        info = oracle("Skullclamp")
        self.assertIn("death-trigger draw", synergy.functions(info, cardroles.classify(info))["Card Draw"])
        info = oracle("Ruinous Ultimatum")
        self.assertIn("asymmetric wipe", synergy.functions(info, cardroles.classify(info))["Board Wipe"])


@unittest.skipUnless(has_cards(), "needs cards.db with oracle_cards")
class GraphTests(unittest.TestCase):
    def graph(self, names, cmd):
        cards = {n: (oracle(n), cardroles.classify(oracle(n))) for n in names}
        return synergy.InteractionGraph(cards, [cmd])

    def test_strengths_and_weak_links_not_counted(self):
        g = self.graph(["Teysa Karlov", "Viscera Seer", "Blood Artist", "Bitterblossom", "Grizzly Bears"],
                       "Teysa Karlov")
        detail = {o: st for o, _, st in g.detailed("Blood Artist", include_weak=True)}
        self.assertEqual(detail["Viscera Seer"], "strong")  # outlet -> death payoff
        self.assertEqual(detail.get("Grizzly Bears"), "weak")  # "any creature could die"
        self.assertNotIn("Grizzly Bears", [o for o, _ in g.interactions("Blood Artist")])
        self.assertEqual(g.count("Blood Artist", strengths=("weak",)), 1)

    def test_engines_need_strong_links_providers_and_payoffs(self):
        g = self.graph(["Teysa Karlov", "Viscera Seer", "Blood Artist", "Bitterblossom", "Zulaport Cutthroat",
                        "Grizzly Bears"], "Teysa Karlov")
        eng = g.engines()
        self.assertIn("Sacrifice engine", eng)
        self.assertNotIn("Grizzly Bears", eng["Sacrifice engine"]["members"])
        self.assertIn("Blood Artist", eng["Sacrifice engine"]["payoffs"])


@unittest.skipUnless(has_cards(), "needs cards.db with oracle_cards")
class DeckLevelTests(DBCase):
    def caesarish(self):
        cards = ["Bitterblossom", "Impact Tremors", "Skullclamp", "Secure the Wastes", "Adeline, Resplendent Cathar",
                 "Viscera Seer", "Blood Artist", "Zulaport Cutthroat", "Martial Coup", "Wrath of God",
                 "Damnation", "Vault 75: Middle School", "Ruinous Ultimatum", "Swords to Plowshares",
                 "Path to Exile", "Teferi's Protection"] + FILLER
        return self.deck("Caesar-ish", "Caesar, Legion's Emperor", cards, basics=30)

    def test_deck_targets_have_reasons_and_go_wide_wants_fewer_wipes(self):
        d = self.caesarish()
        st, rv = self.review(d)
        t = st.targets["Board Wipe"]
        self.assertLess(t["target"][1], t["generic"][1])
        self.assertIn("wipes also hit your own wide board", t["why"])
        self.assertIn("the commander draws cards itself", st.targets["Card Draw"]["why"])
        # symmetrical wipes are the plausible cuts; the one-sided wipe is protected
        self.assertTrue(any(e[0].startswith("Wipes your own board") for e in rv["Wrath of God"]["evidence"]))
        self.assertTrue(any("One-sided wipe" in p[0] for p in rv["Ruinous Ultimatum"]["protection"]))
        for key in ("Bitterblossom", "Impact Tremors", "Skullclamp", "Viscera Seer", "Blood Artist", "Adeline, Resplendent Cathar"):
            self.assertFalse(rv[key]["eligible"], key)
        self.assertIn(st.confidence["overall"], ("HIGH", "MEDIUM"))

    def test_weakest_cards_and_no_ramp_padding(self):
        d = self.caesarish()
        st, _ = self.review(d)
        weak = analyser.weakest_cards(st)
        names = [w["name"] for w in weak]
        self.assertTrue(set(names) & {"Wrath of God", "Damnation", "Vault 75: Middle School"})
        self.assertNotIn("Sol Ring", names)  # ramp within target: never padding
        for w in weak:
            self.assertTrue(w["reasons_to_keep"] is not None and w["reasons_to_cut"] is not None)

    def test_find_a_card_to_replace(self):
        d = self.caesarish()
        found = analyser.find_cuts_for(self.db, self.st, d, SET, "Judith, the Scourge Diva")
        import cutfinder
        self.assertGreaterEqual(found["replacement_confidence"], cutfinder.OPTIONAL)  # a real improvement exists
        outs = [o["out"] for o in found["options"]]
        self.assertTrue(outs)
        self.assertLessEqual(len(outs), 3)
        for engine_piece in ("Bitterblossom", "Impact Tremors", "Viscera Seer", "Blood Artist", "Skullclamp"):
            self.assertNotIn(engine_piece, outs)
        for o in found["options"]:
            self.assertTrue(o["why_cut"])
            self.assertGreater(o["cut_confidence"], 0)

    def test_find_a_card_to_replace_can_find_nothing(self):
        d = self.caesarish()
        for r in self.st.deck_cards(d):
            self.db.set_card_flag(d, r["card_name"], "never_cut")
        found = analyser.find_cuts_for(self.db, self.st, d, SET, "Judith, the Scourge Diva")
        self.assertEqual(found["options"], [])

    def test_core_and_not_important_feedback(self):
        d = self.caesarish()
        _, before = self.review(d)
        self.db.set_card_flag(d, "Wrath of God", "core")
        self.db.set_card_flag(d, "Path to Exile", "not_important")
        st, after = self.review(d)
        self.assertFalse(after["Wrath of God"]["eligible"])
        self.assertGreater(after["Wrath of God"]["P"], before["Wrath of God"]["P"] + 2)
        self.assertLess(after["Path to Exile"]["P"], before["Path to Exile"]["P"])
        self.assertTrue(any("not important" in e[0] for e in after["Path to Exile"]["evidence"]))
        self.assertTrue(any("you marked" in r for rs in st.detected["reasons"].values() for r in rs))
        self.db.set_card_flag(d, "Wrath of God", "not_important")  # exclusive with core
        self.assertEqual(self.db.card_flags(d)["Wrath of God"], {"not_important"})
        names = [w["name"] for w in analyser.weakest_cards(analyser.prepare(self.db, self.st, d, SET))]
        self.db.set_card_flag(d, "Damnation", "core")
        names2 = [w["name"] for w in analyser.weakest_cards(analyser.prepare(self.db, self.st, d, SET))]
        self.assertNotIn("Damnation", names2)
        self.assertTrue(names)

    def test_pins_survive_the_flags_migration(self):
        d = self.caesarish()
        self.db.protect_card(d, "Wrath of God")
        self.assertEqual(self.db.protected_cards(d), {"Wrath of God"})
        self.assertEqual(self.db.card_flags(d)["Wrath of God"], {"never_cut"})

    def test_analysis_confidence_drops_when_cards_are_not_understood(self):
        odd = ["Shahrazad", "Mindslaver", "Reconnaissance", "Chaos Orb", "Fork", "Celestial Dawn", "Mirror Gallery",
               "Paradox Haze", "Pyramids", "Illusionary Mask"]
        d = self.deck("odd", "Ghave, Guru of Spores", odd)
        st, _ = self.review(d)
        self.assertIn(st.confidence["coverage"], ("LOW", "MEDIUM"))
        self.assertTrue(st.confidence["reasons"])

    def test_finisher_wording(self):
        d = self.deck("no finishers", "Ghave, Guru of Spores", FILLER + ["Swords to Plowshares", "Path to Exile"])
        res = analyser.analyse(self.db, self.st, d, SET, quick=True)
        texts = " ".join(w["text"] for w in res["weaknesses"])
        self.assertIn("No obvious dedicated finisher detected", texts)

    def test_price_and_print_count_are_not_quality(self):
        d = self.caesarish()
        st = analyser.prepare(self.db, self.st, d, SET)
        before = analyser.score(st.ctx, "Bitterblossom")["comp"]["quality"]
        st.cd.prices["Bitterblossom"] = 999.0
        st.cd.reprints["Bitterblossom"] = 1.0
        after = analyser.score(st.ctx, "Bitterblossom")["comp"]["quality"]
        self.assertEqual(before, after)


@unittest.skipUnless(has_cards(), "needs cards.db with oracle_cards")
class ScreenTests(DBCase):
    def screen(self, res, stale=False):
        from analyse_view import AnalyseScreen
        app = type("A", (), {})()
        app.settings, app.userdb, app.decks_screen, app.collection_screen, app.dialog = {"currency": "USD"}, \
            self.db, None, None, None
        app.money = lambda u: f"${u:.2f}"
        app.toast = lambda *a: None
        app.toast_text = lambda: None
        app.open_dialog = lambda d: setattr(app, "dialog", d)
        sc = AnalyseScreen(app, 1400, 610, store=self.st)
        sc.result, sc.stale, sc.deck_id = res, stale, None
        return sc

    def texts(self, sc):
        from ui import Painter
        p = Painter(1400, 610)
        out = []
        orig = p.text

        def spy(xy, text, *a, **k):
            out.append(str(text))
            return orig(xy, text, *a, **k)
        p.text = spy
        return p, out

    def test_zero_swaps_screen(self):
        d = self.deck("t", "Rhys the Redeemed", ARCHETYPES["Tokens"][1] + FILLER)
        res = analyser.analyse(self.db, self.st, d, SET, quick=True)
        res["swaps"] = []
        res["considered"] = [dict(name="Anointed Procession", confidence=0.9, why=["x"], ownership=dict(status="not_owned"),
                                  links=[], links_total=0)]
        sc = self.screen(res)
        sc.deck_id = d
        p, out = self.texts(sc)
        sc._swaps(p, 372, 1384, 52)
        joined = " ".join(out)
        self.assertIn("No change recommended.", joined)
        self.assertNotIn("Nothing is selected", joined)
        self.assertNotIn("tick the changes", joined)
        self.assertTrue(any(h[4] == ("tab", "consider") for h in p.hits))  # View cards to consider (1)
        self.assertFalse(any(h[4] == "apply" for h in p.hits))

    def test_stale_analysis_disables_apply_and_says_reanalyse(self):
        d = self.deck("t", "Rhys the Redeemed", ARCHETYPES["Tokens"][1] + FILLER)
        res = analyser.analyse(self.db, self.st, d, SET, quick=True)
        res["swaps"] = [dict(out="Raise the Alarm", into="Anointed Procession", ownership=dict(status="not_owned"),
                             confidence="Worth considering", cut_confidence=0.7, replacement_confidence=0.8,
                             why_out=["x"], why_in=["y"], in_links_total=0)]
        sc = self.screen(res, stale=True)
        sc.deck_id = d
        sc.selected = {0}
        p, out = self.texts(sc)
        sc._swaps(p, 372, 1384, 52)
        self.assertFalse(any(h[4] == "apply" for h in p.hits))
        self.assertIn("OUT OF DATE", " ".join(out))
        from ui import Painter
        p2 = Painter(1400, 610)
        labels = []
        orig = p2.button
        p2.button = lambda box, label, *a, **k: (labels.append(label), orig(box, label, *a, **k))
        sc._left(p2)
        self.assertIn("Re-analyse Deck", labels)

    def test_find_cut_dialog_with_no_options(self):
        from analyse_view import FindCutDialog
        sc = self.screen(dict(swaps=[]))
        dlg = FindCutDialog(sc, dict(into="Judith, the Scourge Diva", replacement_confidence=0.9, why_in=["x"],
                                     options=[]))
        dlg.draw(np.zeros((650, 1400, 3), np.uint8))
        self.assertFalse(any(h[4] and h[4][0] == "swap" for h in dlg.hits))


if __name__ == "__main__":
    unittest.main()
