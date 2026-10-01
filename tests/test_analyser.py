"""Phase 4 tests: the Commander deck analyser - card roles, strategy detection, deck
composition, cuts and replacements (identity, legality, ownership), My Collection / All
Cards modes and budgets, explanations, applying swaps (snapshot + allocation), undo,
cache invalidation, optional providers (no network) and the Analyse screen workflow.

    python -m unittest tests.test_analyser -v

Uses the real Scryfall cards.db (read-only) and temporary user databases. Each analysis
takes a few seconds, so analyses are shared per test class.
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import analyser  # noqa: E402
import cardroles  # noqa: E402
import strategy  # noqa: E402
import userdb  # noqa: E402
from decks import DeckStore  # noqa: E402
from providers import CommanderSpellbookProvider, edhrec_url  # noqa: E402
from userdb import UserDB  # noqa: E402

CARDS_DB = os.path.join(ROOT, "data", "cards.db")

DECK = ["Sol Ring", "Arcane Signet", "Burnished Hart", "Solemn Simulacrum", "Hardened Scales", "Doubling Season",
        "Evolution Sage", "Flux Channeler", "Karn's Bastion", "Swords to Plowshares", "Counterspell", "Wrath of God",
        "Inspiring Call", "Rishkar, Peema Renegade", "Grateful Apparition", "Contagion Clasp", "Thrummingbird",
        "Ezuri's Predation", "Gilded Lotus", "Darksteel Ingot", "Mind Stone", "Hedron Archive", "Thran Dynamo",
        "Worn Powerstone", "Commander's Sphere", "Gruul Signet", "Pelakka Wurm", "Ulamog's Crusher",
        "Colossal Dreadmaw", "Krosan Tusker", "Cultivate", "Kodama's Reach", "Farseek", "Demonic Tutor"]
OWNED = ["Rhystic Study", "Beast Whisperer", "Guardian Project", "Inexorable Tide", "Pir, Imaginative Rascal",
         "Toxic Deluge", "Path to Exile", "Heroic Intervention", "Fathom Mage", "Bred for the Hunt", "Three Visits",
         "Nature's Lore", "Lightning Bolt", "Smothering Tithe", "Kami of Whispered Hopes"]
IDENTITY = set("WUBG")
BANNED_WORDS = ("bad card", "must remove", "terrible", "useless")


def oracle(name):
    con = sqlite3.connect(CARDS_DB)
    con.row_factory = sqlite3.Row
    r = con.execute("SELECT * FROM oracle_cards WHERE name = ?", (name,)).fetchone()
    con.close()
    return dict(r)


def roles(name):
    return cardroles.classify(oracle(name))


def has_oracle_table():
    if not os.path.exists(CARDS_DB):
        return False
    con = sqlite3.connect(CARDS_DB)
    ok = con.execute("SELECT 1 FROM sqlite_master WHERE name = 'oracle_cards'").fetchone() is not None
    con.close()
    return ok


def needs_cards_db(cls):
    if not has_oracle_table():
        return unittest.skip("data/cards.db has no oracle_cards table - run Update Prices")(cls)
    return cls


def build(tmp):
    """Atraxa counters deck + a small collection; Rhystic Study is used by another deck."""
    db = UserDB(os.path.join(tmp, "user.db"))
    st = DeckStore(db)
    deck = st.create_deck("Atraxa Counters")
    lines = ["Commander", "1 Atraxa, Praetors' Voice", "Deck"] + [f"1 {n}" for n in DECK] + \
        ["8 Forest", "8 Plains", "8 Island", "4 Swamp", "1 Command Tower", "1 Exotic Orchard"]
    st.commit_import(deck, st.parse_decklist("\n".join(lines)))
    for n in OWNED:
        c = st.default_printing(n)
        db.add_card(c["id"], c["name"], c["set_code"], c["collector_number"])
    other = st.create_deck("Other Deck")
    st.add_card(other, st.default_printing("Rhystic Study"))
    return db, st, deck, other


def settings(mode="collection", goal="improve", tags=None, budget=None):
    return dict(mode=mode, goal=goal, tags=tags, budget_usd=None if mode == "collection" else budget)


class TempDB:
    @classmethod
    def make_tmp(cls):
        cls.tmp = tempfile.mkdtemp(prefix="tmp_analyse_", dir=HERE)
        cls._old_backup = userdb.BACKUP_DIR
        userdb.BACKUP_DIR = os.path.join(cls.tmp, "backups")

    @classmethod
    def drop_tmp(cls):
        cls.db.close()
        userdb.BACKUP_DIR = cls._old_backup
        shutil.rmtree(cls.tmp, ignore_errors=True)


# ---------------------------------------------------------------- roles and strategy (no database)

@needs_cards_db
class CardRoleTests(unittest.TestCase):
    def test_core_roles(self):
        expect = {"Sol Ring": "Ramp", "Swords to Plowshares": "Removal", "Wrath of God": "Board Wipe",
                  "Counterspell": "Counterspell", "Rhystic Study": "Card Draw", "Demonic Tutor": "Tutor",
                  "Cultivate": "Ramp", "Eternal Witness": "Recursion", "Heroic Intervention": "Protection",
                  "Craterhoof Behemoth": "Finisher", "Beast Within": "Removal"}
        for name, role in expect.items():
            with self.subTest(name):
                self.assertIn(role, roles(name))

    def test_multi_role_cards(self):
        self.assertTrue({"Token Generation", "+1/+1 Counters"} <= set(roles("Doubling Season")))
        self.assertTrue({"Protection", "Equipment"} <= set(roles("Lightning Greaves")))
        self.assertTrue({"Removal", "Board Wipe"} <= set(roles("Cyclonic Rift")))

    def test_lands_are_not_ramp_and_searches_are_not_tutors(self):
        self.assertEqual(set(roles("Command Tower")), {"Land"})
        self.assertNotIn("Tutor", roles("Cultivate"))

    def test_wipe_not_double_counted_as_removal(self):
        self.assertNotIn("Removal", roles("Wrath of God"))

    def test_confidence_and_reasons(self):
        r = roles("Sol Ring")["Ramp"]
        self.assertGreaterEqual(r.confidence, 0.8)
        self.assertTrue(r.reason)
        self.assertEqual(cardroles.roles_from_json(cardroles.roles_to_json(roles("Doubling Season"))).keys(),
                         roles("Doubling Season").keys())

    def test_reminder_text_and_card_name_are_ignored(self):
        t = cardroles.normalise_text("Flying (This creature can't be blocked except by flying.)\nWhen Mulldrifter enters, "
                                     "draw two cards.", "Mulldrifter")
        self.assertNotIn("can't be blocked", t)
        self.assertNotIn("mulldrifter", t.lower())


@needs_cards_db
class StrategyTests(unittest.TestCase):
    def test_atraxa_is_counters(self):
        det = strategy.detect([oracle("Atraxa, Praetors' Voice")], [oracle(n) for n in ("Hardened Scales",
                                                                                         "Evolution Sage")])
        self.assertTrue({"Counters", "Proliferate"} & set(det["primary"]))
        self.assertTrue(det["reasons"])

    def test_tokens_commander(self):
        det = strategy.detect([oracle("Rhys the Redeemed")])
        self.assertIn("Tokens", det["primary"] + det["secondary"])

    def test_tribal_commander(self):
        det = strategy.detect([oracle("Edgar Markov")])
        self.assertEqual(det["tribe"], "Vampire")


# ---------------------------------------------------------------- one analysed deck, read-only checks

@needs_cards_db
class AnalysisTests(TempDB, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.make_tmp()
        cls.db, cls.st, cls.deck, cls.other = build(cls.tmp)
        cls.coll = analyser.analyse(cls.db, cls.st, cls.deck, settings())
        cls.budget = 500 / 17.25
        cls.all = analyser.analyse(cls.db, cls.st, cls.deck, settings("all", budget=cls.budget))

    @classmethod
    def tearDownClass(cls):
        cls.drop_tmp()

    def ins(self, res):
        return [s["into"] for s in res["swaps"]]

    # composition
    def test_composition_counts_lands_curve_and_average(self):
        comp = self.coll["composition"]
        self.assertEqual(comp["lands"], 31)  # 30 lands from the list + Karn's Bastion
        self.assertEqual(comp["basics"], 28)
        self.assertEqual(comp["lands"] + comp["spells"], 1 + len(DECK) + 30)  # the commander counts too
        self.assertEqual(sum(comp["curve"].values()), comp["spells"])
        cd = analyser.card_data(self.db)
        mvs = [float(cd.info(n)["cmc"] or 0) for n in DECK + ["Atraxa, Praetors' Voice"]
               if "Land" not in cd.info(n)["type_line"]]
        self.assertAlmostEqual(comp["avg_mv"], round(sum(mvs) / len(mvs), 2), places=2)
        self.assertTrue(set(comp["pips"]) <= IDENTITY)

    def test_weaknesses_are_measured_against_guidelines(self):
        weak = {w["role"]: w for w in self.coll["weaknesses"]}
        self.assertIn("Card Draw", weak)  # the deck has hardly any draw
        for w in weak.values():
            lo, hi = w["range"]
            self.assertTrue(w["have"] < lo or w["have"] > hi)

    def test_strategy_detected(self):
        self.assertTrue({"Counters", "Proliferate"} & set(self.coll["detected"]["primary"]))

    # cuts
    def test_never_cuts_commander_or_lands(self):
        cd = analyser.card_data(self.db)
        for res in (self.coll, self.all):
            for s in res["swaps"]:
                self.assertNotEqual(s["out"], "Atraxa, Praetors' Voice")
                self.assertNotIn("Land", cd.info(s["out"])["type_line"])

    def test_staples_are_not_cut_when_improving(self):
        for res in (self.coll, self.all):
            self.assertNotIn("Demonic Tutor", [s["out"] for s in res["swaps"]])  # a Game Changer

    def test_does_not_cut_the_only_counterspell_for_something_unrelated(self):
        for res in (self.coll, self.all):
            for s in res["swaps"]:
                if s["out"] == "Counterspell":
                    self.assertIn("Counterspell", s["in_roles"])

    # replacements
    def test_replacements_are_legal_in_identity_and_new(self):
        cd = analyser.card_data(self.db)
        for res in (self.coll, self.all):
            for name in self.ins(res):
                info = cd.info(name)
                self.assertEqual(info["legal_commander"], "legal", name)
                self.assertTrue(set(info["color_identity"] or "") <= IDENTITY, name)
                self.assertNotIn(name, DECK)
        self.assertNotIn("Lightning Bolt", self.ins(self.coll) + self.ins(self.all))  # owned but red

    def test_collection_mode_only_free_owned_cards(self):
        self.assertTrue(self.coll["swaps"])
        for s in self.coll["swaps"]:
            self.assertIn(s["into"], OWNED)
            self.assertEqual(s["ownership"]["status"], "available")
            self.assertEqual(s["ownership"]["cost"], 0)
        self.assertEqual(self.coll["cost"], 0)

    def test_cards_used_by_other_decks_are_not_offered_as_free(self):
        self.assertNotIn("Rhystic Study", self.ins(self.coll))
        alt = {a["name"]: a for a in self.coll["alternatives"]}
        if "Rhystic Study" in alt:
            self.assertEqual(alt["Rhystic Study"]["used_in"], ["Other Deck"])

    def test_all_cards_mode_respects_total_budget(self):
        self.assertTrue(self.all["swaps"])
        self.assertLessEqual(self.all["cost"], self.budget + 1e-6)
        total = sum(s["ownership"]["cost"] or 0 for s in self.all["swaps"])
        self.assertAlmostEqual(total, self.all["cost"], places=2)
        self.assertTrue(any(s["ownership"]["status"] == "not_owned" for s in self.all["swaps"]))

    def test_all_cards_mode_with_zero_budget_uses_free_cards_only(self):
        res = analyser.analyse(self.db, self.st, self.deck, settings("all", budget=0.0))
        for s in res["swaps"]:
            self.assertEqual(s["ownership"]["cost"], 0)

    def test_ownership_status(self):
        own = analyser.ownership_map(self.db, self.deck)
        self.assertEqual(own["Rhystic Study"]["free"], 0)
        self.assertEqual(own["Rhystic Study"]["used_in"], ["Other Deck"])
        self.assertEqual(own["Smothering Tithe"]["free"], 1)
        combos = analyser.annotate_combos(self.db, self.deck, [dict(missing=["Rhystic Study", "Smothering Tithe",
                                                                              "Thassa's Oracle"])])
        status = {k: v["status"] for k, v in combos[0]["missing_ownership"].items()}
        self.assertEqual(status, {"Rhystic Study": "in_use", "Smothering Tithe": "available",
                                  "Thassa's Oracle": "not_owned"})

    def test_explanations(self):
        for res in (self.coll, self.all):
            for s in res["swaps"]:
                self.assertTrue(s["why_in"], s)
                self.assertIn(s["confidence"], ("Strong suggestion", "Worth considering", "Situational",
                                                "Depends on your intended strategy"))
                text = " ".join(s["why_in"] + s["why_out"]).lower()
                for word in BANNED_WORDS:
                    self.assertNotIn(word, text)

    def test_user_tags_override_detected_strategy(self):
        res = analyser.analyse(self.db, self.st, self.deck, settings(tags=["Lifegain"]))
        self.assertEqual(res["active_themes"], ["Lifegain"])
        self.assertTrue(res["user_tags"])

    def test_analysis_never_changes_the_deck(self):
        before = [tuple(r) for r in self.db.conn.execute("SELECT * FROM deck_cards ORDER BY id")]
        analyser.analyse(self.db, self.st, self.deck, settings(goal="high"))
        after = [tuple(r) for r in self.db.conn.execute("SELECT * FROM deck_cards ORDER BY id")]
        self.assertEqual(before, after)

    def test_analysis_works_offline(self):
        with mock.patch("requests.Session.request", side_effect=AssertionError("network used")), \
                mock.patch("requests.request", side_effect=AssertionError("network used")):
            res = analyser.analyse(self.db, self.st, self.deck, settings(goal="casual"))
        self.assertIn("swaps", res)


# ---------------------------------------------------------------- applying, undo, caching

@needs_cards_db
class ApplyUndoTests(TempDB, unittest.TestCase):
    def setUp(self):
        type(self).make_tmp()
        self.db, self.st, self.deck, self.other = build(self.tmp)
        type(self).db = self.db

    def tearDown(self):
        type(self).drop_tmp()

    def deck_state(self, deck_id):
        return sorted((r["card_name"], r["quantity"], r["collection_item_id"]) for r in self.db.conn.execute(
            "SELECT card_name, quantity, collection_item_id FROM deck_cards WHERE deck_id = ?", (deck_id,)))

    def names(self, deck_id):
        return {r[0] for r in self.db.conn.execute("SELECT card_name FROM deck_cards WHERE deck_id = ?", (deck_id,))}

    def count(self, deck_id):
        return self.db.conn.execute("""SELECT SUM(quantity) FROM deck_cards
                                       WHERE deck_id = ? AND role IN ('commander', 'partner', 'main')""",
                                    (deck_id,)).fetchone()[0]

    def test_apply_swaps_snapshot_allocation_and_undo(self):
        fp = analyser.current_fingerprint(self.db, self.deck, settings())
        res = analyser.analyse(self.db, self.st, self.deck, settings())
        aid = self.db.save_analysis(self.deck, fp, settings(), res)
        chosen = res["swaps"][:2]
        self.assertEqual(len(chosen), 2)
        before, other_before, n_before = self.deck_state(self.deck), self.deck_state(self.other), self.count(self.deck)

        analyser.apply_swaps(self.db, self.st, self.deck, chosen, aid)
        names = self.names(self.deck)
        for s in chosen:
            self.assertIn(s["into"], names)
            self.assertNotIn(s["out"], names)
        self.assertEqual(self.count(self.deck), n_before)  # same size deck
        linked = {r[0] for r in self.db.conn.execute(
            "SELECT card_name FROM deck_cards WHERE deck_id = ? AND collection_item_id IS NOT NULL", (self.deck,))}
        for s in chosen:
            self.assertIn(s["into"], linked)  # the owned free copy is now allocated to this deck
        own = analyser.ownership_map(self.db, self.deck)
        for s in chosen:
            self.assertEqual(own[s["into"]]["free"], 0)
        self.assertEqual(self.deck_state(self.other), other_before)  # other decks untouched
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM deck_snapshots WHERE deck_id = ?",
                                              (self.deck,)).fetchone()[0], 1)
        # the cached analysis is now out of date
        self.assertNotEqual(analyser.current_fingerprint(self.db, self.deck, settings()), fp)

        undone = analyser.undo_last(self.db, self.st, self.deck)
        self.assertEqual([(s["out"], s["into"]) for s in undone], [(s["out"], s["into"]) for s in chosen])
        self.assertEqual(self.deck_state(self.deck), before)
        self.assertEqual(self.deck_state(self.other), other_before)
        own = analyser.ownership_map(self.db, self.deck)
        for s in chosen:
            self.assertEqual(own[s["into"]]["free"], 1)  # copies released again
        self.assertIsNone(self.db.last_applied(self.deck))
        self.assertIsNone(analyser.undo_last(self.db, self.st, self.deck))

    def test_apply_unowned_card_goes_in_as_not_owned(self):
        swap = dict(out="Colossal Dreadmaw", into="Walking Ballista")
        analyser.apply_swaps(self.db, self.st, self.deck, [swap])
        r = self.db.conn.execute("""SELECT collection_item_id, source FROM deck_cards
                                    WHERE deck_id = ? AND card_name = 'Walking Ballista'""", (self.deck,)).fetchone()
        self.assertIsNone(r["collection_item_id"])
        self.assertEqual(r["source"], "analysis")

    def test_apply_never_takes_copies_from_other_decks(self):
        analyser.apply_swaps(self.db, self.st, self.deck, [dict(out="Colossal Dreadmaw", into="Rhystic Study")])
        r = self.db.conn.execute("""SELECT collection_item_id FROM deck_cards
                                    WHERE deck_id = ? AND card_name = 'Rhystic Study'""", (self.deck,)).fetchone()
        self.assertIsNone(r[0])
        self.assertEqual(analyser.ownership_map(self.db, self.deck)["Rhystic Study"]["used_in"], ["Other Deck"])

    def test_failed_apply_changes_nothing(self):
        before = self.deck_state(self.deck)
        with self.assertRaises(ValueError):
            analyser.apply_swaps(self.db, self.st, self.deck, [dict(out="Colossal Dreadmaw", into="Fathom Mage"),
                                                                dict(out="Not In This Deck", into="Path to Exile")])
        self.assertEqual(self.deck_state(self.deck), before)

    def test_cache_invalidation(self):
        s = settings()
        fp = analyser.current_fingerprint(self.db, self.deck, s)
        self.assertEqual(fp, analyser.current_fingerprint(self.db, self.deck, s))  # stable
        self.assertNotEqual(fp, analyser.current_fingerprint(self.db, self.deck, settings(goal="high")))
        self.assertNotEqual(fp, analyser.current_fingerprint(self.db, self.deck, settings("all", budget=10)))
        c = self.st.default_printing("Eternal Witness")
        self.db.add_card(c["id"], c["name"], c["set_code"], c["collector_number"])  # collection changed
        fp2 = analyser.current_fingerprint(self.db, self.deck, s)
        self.assertNotEqual(fp, fp2)
        self.st.add_card(self.deck, self.st.default_printing("Fathom Mage"))  # deck (and allocation) changed
        self.assertNotEqual(fp2, analyser.current_fingerprint(self.db, self.deck, s))
        now = analyser.current_fingerprint(self.db, self.deck, s)
        with mock.patch.object(analyser, "cards_stamp", return_value="new-cards-db"):  # card data changed
            self.assertNotEqual(analyser.current_fingerprint(self.db, self.deck, s), now)

    def test_profiles_and_history(self):
        p = self.db.analysis_profile(self.deck)
        self.assertEqual((p["mode"], p["goal"], p["tags"]), ("collection", "improve", None))
        self.db.save_analysis_profile(self.deck, mode="all", budget_usd=25.0, tags=["Counters"])
        p = self.db.analysis_profile(self.deck)
        self.assertEqual((p["mode"], p["budget_usd"], p["tags"]), ("all", 25.0, ["Counters"]))
        self.db.save_analysis_profile(self.deck, tags=None)
        self.assertIsNone(self.db.analysis_profile(self.deck)["tags"])
        self.assertEqual(self.db.analysis_profile(self.deck)["budget_usd"], 25.0)
        a1 = self.db.save_analysis(self.deck, "fp1", settings(), dict(swaps=[]))
        a2 = self.db.save_analysis(self.deck, "fp2", settings(), dict(swaps=[1]))
        self.assertGreater(a2, a1)
        self.assertEqual(self.db.latest_analysis(self.deck)["fingerprint"], "fp2")

    def test_deleting_a_deck_removes_its_analysis_data(self):
        self.db.save_analysis(self.deck, "fp", settings(), dict(swaps=[]))
        self.db.snapshot_deck(self.deck, "test")
        self.db.delete_deck(self.deck)
        for table in ("deck_analyses", "deck_snapshots"):
            self.assertEqual(self.db.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE deck_id = ?",
                                                  (self.deck,)).fetchone()[0], 0, table)


# ---------------------------------------------------------------- providers (no real network)

class ProviderTests(unittest.TestCase):
    def test_edhrec_is_only_a_link(self):
        self.assertEqual(edhrec_url(["Atraxa, Praetors' Voice"]), "https://edhrec.com/commanders/atraxa-praetors-voice")
        self.assertEqual(edhrec_url(["Tymna the Weaver", "Kraum, Ludevic's Opus"]),
                         "https://edhrec.com/commanders/kraum-ludevics-opus-tymna-the-weaver")
        self.assertEqual(edhrec_url([]), "https://edhrec.com/")

    def test_commander_spellbook_parsing_one_request_and_cache(self):
        payload = {"results": {
            "included": [{"id": "1-2", "uses": [{"card": {"name": "Sol Ring"}}, {"card": {"name": "Atraxa, Praetors' Voice"}}],
                          "produces": [{"feature": {"name": "Infinite mana"}}]}],
            "almostIncluded": [{"id": "3-4", "uses": [{"card": {"name": "Sol Ring"}}, {"card": {"name": "Thassa's Oracle"}}],
                                "produces": []}]}}
        session = mock.Mock()
        session.post.return_value = mock.Mock(status_code=200, json=lambda: payload, raise_for_status=lambda: None)
        p = CommanderSpellbookProvider(session=session)
        out = p.combos(["Atraxa, Praetors' Voice"], ["Sol Ring"])
        self.assertEqual([c["status"] for c in out], ["in_deck", "one_away"])
        self.assertEqual(out[1]["missing"], ["Thassa's Oracle"])
        self.assertEqual(out[0]["produces"], ["Infinite mana"])
        self.assertTrue(out[0]["url"].startswith("https://commanderspellbook.com/combo/"))
        p.combos(["Atraxa, Praetors' Voice"], ["Sol Ring"])
        self.assertEqual(session.post.call_count, 1)  # cached
        _, kwargs = session.post.call_args
        self.assertIn("User-Agent", kwargs["headers"])

    def test_rate_limit_is_reported(self):
        session = mock.Mock()
        session.post.return_value = mock.Mock(status_code=429)
        with self.assertRaises(RuntimeError):
            CommanderSpellbookProvider(session=session).combos(["Atraxa, Praetors' Voice"], [])


# ---------------------------------------------------------------- the manual scenario, automated

class FakeApp:
    def __init__(self, db):
        self.userdb, self.settings = db, {"currency": "ZAR", "usd_zar": 17.25}
        self.dialog, self.toasts = None, []
        self.decks_screen = self.collection_screen = None

    def toast(self, text, colour=None):
        self.toasts.append(text)

    def toast_text(self):
        return None

    def open_dialog(self, d):
        self.dialog = d


@needs_cards_db
class ScenarioTest(TempDB, unittest.TestCase):
    """Choose a deck, analyse it from My Collection, apply some suggestions, check the
    allocation, undo, then switch to All Cards with a R500 budget and analyse again."""

    @classmethod
    def setUpClass(cls):
        cls.make_tmp()
        cls.db, cls.st, cls.deck, cls.other = build(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        cls.drop_tmp()

    def wait(self, screen):
        t0 = time.time()
        while screen.running and time.time() - t0 < 120:
            time.sleep(0.1)
            screen.poll()
        self.assertIsNone(screen.running)

    def test_scenario(self):
        from analyse_view import AnalyseScreen, TagDialog
        app = FakeApp(self.db)
        sc = AnalyseScreen(app, 1400, 610, store=self.st)
        sc.select_deck(self.deck)
        self.assertIsNone(sc.result)
        sc.render()
        sc.run()
        self.wait(sc)
        self.assertTrue(sc.result["swaps"])
        self.assertFalse(sc.stale)
        self.assertEqual(sc.selected, set(range(len(sc.result["swaps"]))))
        for tab in ("overview", "swaps", "combos"):
            sc.tab = tab
            sc.bump()
            img, hits = sc.render()
            self.assertEqual(img.shape[:2], (610, 1400))
        # a re-opened screen shows the cached analysis without re-running
        sc2 = AnalyseScreen(app, 1400, 610, store=self.st)
        sc2.select_deck(self.deck)
        self.assertEqual(sc2.analysis_id, sc.analysis_id)
        self.assertFalse(sc2.stale)
        # deselect all but two, apply (with confirmation)
        sc.selected = {0, 1}
        chosen = [sc.result["swaps"][i] for i in (0, 1)]
        before = sorted(r[0] for r in self.db.conn.execute("SELECT card_name FROM deck_cards WHERE deck_id = ?",
                                                           (self.deck,)))
        sc.confirm_apply()
        self.assertIsNotNone(app.dialog)
        self.assertEqual(before, sorted(r[0] for r in self.db.conn.execute(
            "SELECT card_name FROM deck_cards WHERE deck_id = ?", (self.deck,))))  # nothing until confirmed
        app.dialog.finish(True)
        names = {r[0] for r in self.db.conn.execute("SELECT card_name FROM deck_cards WHERE deck_id = ?", (self.deck,))}
        for s in chosen:
            self.assertIn(s["into"], names)
            self.assertNotIn(s["out"], names)
        self.assertTrue(sc.stale)  # deck changed: the analysis is marked out of date
        # undo
        sc.confirm_undo()
        app.dialog.finish(True)
        self.assertEqual(before, sorted(r[0] for r in self.db.conn.execute(
            "SELECT card_name FROM deck_cards WHERE deck_id = ?", (self.deck,))))
        # strategy tags
        td = TagDialog(sc)
        td.tags = {"Counters", "Lifegain"}
        td.finish("save")
        self.assertEqual(self.db.analysis_profile(self.deck)["tags"], ["Counters", "Lifegain"])
        TagDialog(sc).finish("detected")
        self.assertIsNone(self.db.analysis_profile(self.deck)["tags"])
        # All Cards with a R500 budget
        sc.click(0, 0, [(0, 0, 10, 10, ("mode", "all"))])
        sc.click(0, 0, [(0, 0, 10, 10, ("budget", round(500 / 17.25, 4)))])
        self.assertTrue(sc.stale)
        sc.render()
        sc.run()
        self.wait(sc)
        self.assertEqual(sc.result["settings"]["mode"], "all")
        self.assertLessEqual(sc.result["cost"], 500 / 17.25 + 1e-6)
        sc.tab = "swaps"
        sc.bump()
        sc.render()


if __name__ == "__main__":
    unittest.main()
