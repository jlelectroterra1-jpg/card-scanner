"""Consider recommendations: need-driven, tiered, grouped - never a fixed number.

* no top-N: the count comes from thresholds, so it differs per deck (and can exceed 8)
* every card passes the NEED test (what problem does it solve?) - a deck that already has
  plenty of a job gets no more of it (marginal value), with a "Why not?" reason
* tiers + need labels + specific plain-English reasons; each card once, grouped by purpose,
  varied inside a group
* your strategy tags change the recommendations; detected side themes are NICHE (hidden)
* GOOD IDEA / NOT FOR THIS DECK feedback (migration 7) - nothing is ever added to the deck
* My Collection only suggests free copies; All Cards labels OWNED / USED IN ANOTHER DECK /
  NOT OWNED; "No strong upgrades found in your available Collection" + Explore All Cards
* optional community cache (Recommander / Commander Spellbook) is a small signal only
* swaps: a replacement that doesn't take over the cut card's job must be a strong recommendation

    python -m unittest tests.test_recommend -v
"""
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import analyser  # noqa: E402
import community  # noqa: E402
import real_decks  # noqa: E402
import recommend  # noqa: E402
import userdb  # noqa: E402
from decks import DeckStore  # noqa: E402
from test_analyser_understanding import ARCHETYPES, FILLER, has_cards  # noqa: E402
from userdb import UserDB  # noqa: E402

ALL = dict(mode="all", goal="improve", tags=None, budget_usd=None)
COLL = dict(ALL, mode="collection")
LABEL = re.compile(r"^(FILLS|STRENGTHENS|ADDS|IMPROVES|UPGRADES): ")
TOKEN_MAKERS = ["Secure the Wastes", "Raise the Alarm", "Spectral Procession", "Lingering Souls", "Bitterblossom",
                "Hordeling Outburst", "Krenko's Command", "Dragon Fodder", "Goblin Instigator", "Beetleback Chief",
                "Mogg War Marshal", "Young Pyromancer", "Assemble the Legion", "Tempt with Vengeance",
                "Increasing Devotion", "Martial Coup"]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tmp_rec_", dir=HERE)
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

    def deck(self, name, cmd, cards, basics=30):
        d = self.st.create_deck(name)
        parsed = self.st.parse_decklist(f"Commander\n1 {cmd}\nDeck\n" + "\n".join("1 " + c for c in cards) +
                                        f"\n{basics} Wastes")
        self.assertEqual([r["name"] for r in parsed if not r["card"]], [], "unknown cards")
        self.st.commit_import(d, parsed)
        return d

    def recs(self, deck, settings=ALL, feedback=None):
        st = analyser.prepare(self.db, self.st, deck, settings)
        return st, recommend.recommend(st, settings, feedback if feedback is not None else
                                       self.db.suggestion_feedback(deck), {}, debug=True)

    def tokens_deck(self, extra=()):
        return self.deck("Tokens", "Rhys the Redeemed", TOKEN_MAKERS[:14] + ["Viscera Seer"] + list(extra) + FILLER)


@unittest.skipUnless(has_cards(), "needs cards.db with oracle_cards")
class NoFixedCountTests(Base):
    def test_no_top_n_cap_and_counts_differ_per_deck(self):
        with open(os.path.join(ROOT, "analyser.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("len(considered) >= 8", src)
        self.assertFalse(hasattr(analyser, "CONSIDER_MIN"))
        # a half-built deck has many needs -> many ideas; a complete real deck has few
        thin = self.deck("thin", "Rhys the Redeemed", ARCHETYPES["Tokens"][1] + FILLER)
        _, r_thin = self.recs(thin)
        real, _ = real_decks.build(self.db, self.st, "Meren Aristocrats")
        _, r_real = self.recs(real)
        self.assertGreater(r_thin["shown"], 8)  # no cap
        self.assertLess(r_real["shown"], r_thin["shown"])
        self.assertNotEqual(r_real["shown"], 8)

    def test_items_are_tiered_labelled_grouped_once(self):
        d = self.deck("thin", "Teysa Karlov", ARCHETYPES["Aristocrats"][1] + FILLER)
        _, r = self.recs(d, dict(ALL, tags=["Aristocrats"]))
        names = [it["name"] for it in r["items"]]
        self.assertEqual(len(names), len(set(names)))  # each card once
        grouped = [n for g in r["groups"] for n in g["names"]]
        self.assertEqual(sorted(grouped), sorted(names))  # in exactly one (its strongest) group
        for it in r["items"]:
            self.assertIn(it["tier"], recommend.TIERS)
            self.assertRegex(it["need_label"], LABEL)
            self.assertTrue(it["category"])
            self.assertFalse(any("Fits the" in w and "strategy" in w for w in it["why"]), it["why"])
            self.assertTrue(it["why"][0].startswith(("Adds", "The deck has", "Another", "Stronger")), it["why"][0])
            self.assertNotIn("score", " ".join(it["why"]))  # the number stays internal

    def test_diversity_inside_a_group(self):
        d = self.deck("thin", "Rhys the Redeemed", ARCHETYPES["Tokens"][1] + FILLER)
        _, r = self.recs(d)
        for g in r["groups"]:
            items = [next(i for i in r["items"] if i["name"] == n) for n in g["names"]]
            funcs = [i["main_function"] for i in items]
            if len(set(funcs)) > 1 and len(items) > 3:
                first3 = funcs[:3]
                self.assertLess(max(first3.count(f) for f in first3), 3, (g["category"], first3))


@unittest.skipUnless(has_cards(), "needs cards.db with oracle_cards")
class NeedTests(Base):
    def test_redundant_job_gets_no_more_cards_but_a_missing_one_does(self):
        d = self.tokens_deck()
        st, r = self.recs(d, dict(ALL, tags=["Tokens", "Aristocrats"]))
        needs = recommend.deck_needs(st)
        self.assertEqual(needs["piece:token_makers"]["severity"], 0.0)  # 14+ token makers: enough
        self.assertGreater(needs["piece:sac_outlets"]["severity"], 0.3)  # 1 sacrifice outlet: a real need
        # the redundant job: no idea is justified by "another token maker"
        self.assertFalse([it for it in r["items"] if it["need_key"] == "piece:token_makers"])
        # the missing job: sacrifice outlets are suggested, labelled as such
        outlets = [it for it in r["items"] if it["need_key"] == "piece:sac_outlets"]
        self.assertTrue(outlets)
        self.assertEqual(outlets[0]["need_label"], "STRENGTHENS: Sacrifice Outlets")
        self.assertIn("sacrifice outlet", outlets[0]["why"][0])
        why = r["recommender"].why_not("Raise the Alarm") if False else r["recommender"].why_not("Captain's Call")
        self.assertFalse(why["recommended"])
        self.assertIn("already has", why["reason"])

    def test_every_card_answers_what_problem_it_solves(self):
        d = self.tokens_deck()
        _, r = self.recs(d)
        rec = r["recommender"]
        for it in r["items"]:
            n = rec.needs[it["need_key"]]
            self.assertTrue(n["severity"] >= recommend.MIN_NEED or it["need_key"].startswith("upgrade:"), it)

    def test_strategy_tags_change_the_recommendations(self):
        d = self.tokens_deck()
        _, a = self.recs(d, dict(ALL, tags=["Aristocrats"]))
        _, b = self.recs(d, dict(ALL, tags=["Counters"]))
        cats_a = {g["category"] for g in a["groups"]}
        cats_b = {g["category"] for g in b["groups"]}
        self.assertIn("SACRIFICE OUTLETS", cats_a)
        self.assertNotIn("SACRIFICE OUTLETS", cats_b)
        self.assertTrue(cats_b & {"COUNTERS ENGINE", "COUNTERS PAYOFFS"})

    def test_detected_side_themes_are_niche_and_hidden(self):
        d, _ = real_decks.build(self.db, self.st, "Atraxa Superfriends")
        _, r = self.recs(d)
        life = [it for it in r["items"] if "LIFE GAIN" in it["category"]]
        self.assertTrue(all(it["tier"] == "NICHE" for it in life))
        self.assertTrue(all(any("Only matters if you build toward" in w for w in it["why"]) for it in life))


@unittest.skipUnless(has_cards(), "needs cards.db with oracle_cards")
class ArchetypeTests(Base):
    """Real archetype decks missing a key piece: the recommender finds what's missing for
    the plan you chose, with that plan's label."""
    EXPECT = {"Tokens": ("Tokens", {"TOKEN ENGINE", "TOKEN PAYOFFS", "GO-WIDE PAYOFFS"}),
              "Aristocrats": ("Aristocrats", {"SACRIFICE OUTLETS", "SACRIFICE / DEATH PAYOFFS", "SACRIFICE FODDER"}),
              "Voltron": ("Voltron", {"EQUIPMENT & AURAS", "PROTECTION", "REMOVAL"}),
              "Spellslinger": ("Spellslinger", {"SPELLS", "SPELL PAYOFFS"}),
              "Reanimator": ("Reanimator", {"REANIMATION", "GRAVEYARD ENABLERS", "GRAVEYARD PAYOFFS", "RECURSION"}),
              "Tribal": ("Tribal", {"VAMPIRES"}),
              "Landfall": ("Landfall", {"LANDFALL ENABLERS", "LANDFALL PAYOFFS", "CORE STRATEGY UPGRADES"}),
              "Counters": ("Counters", {"COUNTERS ENGINE", "COUNTERS PAYOFFS", "CORE STRATEGY UPGRADES"}),
              "Artifacts": ("Artifacts", {"ARTIFACTS", "ARTIFACT PAYOFFS", "CORE STRATEGY UPGRADES", "TOKEN PAYOFFS"}),
              "Enchantress": ("Enchantments", {"ENCHANTMENTS", "ENCHANTRESS PAYOFFS", "CORE STRATEGY UPGRADES"})}
    # when the plan's own pieces are already covered, the ideas for other needs must still fit the plan
    PLAN_WORDS = {"Artifacts": "artifact", "Enchantress": "enchant"}

    def test_archetypes(self):
        for arch, (tag, cats) in self.EXPECT.items():
            cmd, plan, key, _, _ = ARCHETYPES[arch]
            d = self.deck(arch, cmd, plan + FILLER)
            _, r = self.recs(d, dict(ALL, tags=[tag]))
            got = {g["category"] for g in r["groups"]}
            word = self.PLAN_WORDS.get(arch)
            fits_plan = word and any(word in " ".join(it["why"][1:3]).lower() for it in r["items"]
                                     if it["tier"] in recommend.SHOWN)
            self.assertTrue(got & cats or fits_plan, f"{arch}: {sorted(got)}")
            for it in r["items"]:
                self.assertRegex(it["need_label"], LABEL)

    def test_control_deck_asks_for_interaction(self):
        d = self.deck("Control", "Talrand, Sky Summoner", ["Ponder", "Preordain", "Opt", "Brainstorm",
                                                           "Archmage Emeritus"] + FILLER)
        _, r = self.recs(d, dict(ALL, tags=["Control"]))
        cats = {g["category"] for g in r["groups"]}
        self.assertTrue(cats & {"REMOVAL", "COUNTERSPELLS", "BOARD WIPES"}, cats)

    def test_doublers_once_there_is_an_engine(self):
        d = self.tokens_deck()
        st, _ = self.recs(d, dict(ALL, tags=["Tokens"]))
        self.assertGreater(recommend.deck_needs(st)["piece:token_doublers"]["severity"], 0.3)
        small = self.deck("small", "Rhys the Redeemed", TOKEN_MAKERS[:3] + FILLER)
        st2, _ = self.recs(small, dict(ALL, tags=["Tokens"]))
        self.assertEqual(recommend.deck_needs(st2)["piece:token_doublers"]["severity"], 0.0)


@unittest.skipUnless(has_cards(), "needs cards.db with oracle_cards")
class FeedbackTests(Base):
    def test_migration_7_and_feedback_storage(self):
        self.assertGreaterEqual(self.db.version, 7)
        d = self.tokens_deck()
        self.db.set_suggestion_feedback(d, "Viscera Seer", "good")
        self.db.set_suggestion_feedback(d, "Carrion Feeder", "not_for_deck")
        self.assertEqual(self.db.suggestion_feedback(d), {"Viscera Seer": "good", "Carrion Feeder": "not_for_deck"})
        self.db.set_suggestion_feedback(d, "Viscera Seer", None)
        self.assertNotIn("Viscera Seer", self.db.suggestion_feedback(d))
        with self.assertRaises(ValueError):
            self.db.set_suggestion_feedback(d, "X", "maybe")

    def test_not_for_this_deck_is_never_suggested_again_and_good_adds_nothing(self):
        d = self.tokens_deck()
        tags = dict(ALL, tags=["Tokens", "Aristocrats"])
        _, r = self.recs(d, tags)
        first = r["items"][0]["name"]
        before = [(x["card_name"], x["quantity"]) for x in self.st.deck_cards(d)]
        self.db.set_suggestion_feedback(d, first, "not_for_deck")
        _, r2 = self.recs(d, tags)
        self.assertNotIn(first, [it["name"] for it in r2["items"]])
        self.assertEqual(r2["recommender"].why_not(first)["reason"], "you marked it NOT FOR THIS DECK")
        other = r2["items"][0]
        self.db.set_suggestion_feedback(d, other["name"], "good")
        _, r3 = self.recs(d, tags)
        again = next(it for it in r3["items"] if it["name"] == other["name"])
        self.assertGreaterEqual(again["confidence"], other["confidence"])  # a small nudge at most
        self.assertLessEqual(again["confidence"] - other["confidence"], 0.07)
        self.assertEqual([(x["card_name"], x["quantity"]) for x in self.st.deck_cards(d)], before)  # never added
        # other decks are unaffected
        d2 = self.tokens_deck()
        _, r4 = self.recs(d2, tags)
        self.assertIn(first, [it["name"] for it in r4["items"]])


@unittest.skipUnless(has_cards(), "needs cards.db with oracle_cards")
class OwnershipTests(Base):
    def own(self, name, n=1):
        card = self.st.default_printing(name)
        return self.db.add_card(card["id"], name, card["set_code"], card["collector_number"], quantity=n)

    def test_collection_mode_only_free_owned_cards(self):
        d = self.tokens_deck()
        tags = dict(COLL, tags=["Tokens", "Aristocrats"])
        _, r = self.recs(d, tags)
        self.assertEqual(r["items"], [])  # owns nothing
        # a free copy of a sacrifice outlet -> suggested; the same card used in another deck -> not
        lot = self.own("Ashnod's Altar")
        _, r = self.recs(d, tags)
        self.assertIn("Ashnod's Altar", [it["name"] for it in r["items"]])
        other = self.st.create_deck("Other")
        self.db.add_deck_card(other, "Ashnod's Altar", scryfall_id=self.db.item(lot)["scryfall_id"],
                              collection_item_id=lot)
        _, r = self.recs(d, tags)
        self.assertNotIn("Ashnod's Altar", [it["name"] for it in r["items"]])
        self.assertIn("used in another deck", r["recommender"].why_not("Ashnod's Altar")["reason"].replace(
            "every copy is used in another deck", "used in another deck"))

    def test_all_cards_ownership_labels(self):
        d = self.tokens_deck()
        lot = self.own("Ashnod's Altar")
        other = self.st.create_deck("Other")
        self.db.add_deck_card(other, "Ashnod's Altar", scryfall_id=self.db.item(lot)["scryfall_id"],
                              collection_item_id=lot)
        self.own("Phyrexian Altar")
        _, r = self.recs(d, dict(ALL, tags=["Tokens", "Aristocrats"]))
        status = {it["name"]: it["ownership"]["status"] for it in r["items"]}
        self.assertEqual(status.get("Ashnod's Altar"), "in_use")
        self.assertEqual(status.get("Phyrexian Altar"), "available")
        self.assertIn("not_owned", status.values())

    def test_no_collection_upgrades_message_and_explore_all(self):
        d = self.tokens_deck()
        res = analyser.analyse(self.db, self.st, d, dict(COLL, tags=["Tokens"]))
        self.assertTrue(res["recommendations"]["no_collection_upgrades"])
        sc = screen(self.db, self.st, res, d)
        p, out = texts()
        sc._consider(p, 372, 1384, 52)
        self.assertIn("No strong upgrades found in your available Collection.", out)
        self.assertTrue(any(h[4] == ("explore_all", None) for h in p.hits))


@unittest.skipUnless(has_cards(), "needs cards.db with oracle_cards")
class CommunityTests(Base):
    def test_cache_and_recommander_client(self):
        calls = []

        class Resp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"result_code": "success", "data": {"recommendations": [
                    {"oracle_id": "abc", "name": "Viscera Seer", "score": 0.97},
                    {"oracle_id": "def", "name": "Carrion Feeder", "score": 0.4}]}, "error": None}

        class Session:
            def post(self, url, json=None, timeout=None, headers=None):
                calls.append((url, json, headers))
                return Resp()
        old = community._last_request[0]
        community._last_request[0] = 0
        self.addCleanup(lambda: community._last_request.__setitem__(0, old))
        client = community.RecommanderClient(session=Session(), path=community.CACHE_PATH)
        n, how = client.fetch("cmd-1", ["Teysa Karlov"], ["Blood Artist"])
        self.assertEqual((n, how), (2, "fetched"))
        url, body, headers = calls[0]
        self.assertEqual(url, community.RECOMMANDER_URL)
        self.assertEqual((body["commander"], body["deck"], body["card_format"]), ("Teysa Karlov", ["Blood Artist"], "name"))
        self.assertIn("HomeCardScanner", headers["User-Agent"])
        self.assertEqual(client.fetch("cmd-1", ["Teysa Karlov"], ["Blood Artist"])[1], "cached")  # no 2nd request
        self.assertEqual(len(calls), 1)
        rows = community.load("cmd-1")
        self.assertEqual(rows["Viscera Seer"][0]["source"], "recommander")

    def test_429_is_respected(self):
        class Session:
            def post(self, *a, **k):
                return type("R", (), {"status_code": 429})()
        community._last_request[0] = 0
        with self.assertRaises(RuntimeError):
            community.RecommanderClient(session=Session()).fetch("cmd-2", ["Teysa Karlov"], ["X"])

    def test_community_is_a_small_signal_never_a_reason(self):
        d = self.tokens_deck()
        tags = dict(ALL, tags=["Tokens", "Aristocrats"])
        st = analyser.prepare(self.db, self.st, d, tags)
        plain = recommend.recommend(st, tags, {}, {})
        # community data for a card the deck doesn't need, and for one it does
        outlet = next(it for it in plain["items"] if it["need_key"] == "piece:sac_outlets")
        comm = {"Captain's Call": [dict(source="recommander", metric=1.0, kind="x", detail=None, retrieved="2026")],
                outlet["name"]: [dict(source="recommander", metric=1.0, kind="x", detail=None, retrieved="2026")]}
        boosted = recommend.recommend(st, tags, {}, comm)
        self.assertNotIn("Captain's Call", [it["name"] for it in boosted["items"]])  # no need -> still no
        b = next(it for it in boosted["items"] if it["name"] == outlet["name"])
        self.assertLessEqual(b["confidence"] - outlet["confidence"], 0.061)
        self.assertTrue(any("Recommander" in w for w in b["why"]))

    def test_spellbook_combo_pieces_are_cached(self):
        n = community.save_combos("cmd-3", [dict(status="one_away", cards=["A", "B", "C"], missing=["C"]),
                                            dict(status="in_deck", cards=["A", "B"], missing=[])])
        self.assertEqual(n, 1)
        self.assertIn("completes a combo with A + B", community.load("cmd-3")["C"][0]["detail"])


@unittest.skipUnless(has_cards(), "needs cards.db with oracle_cards")
class SwapAndSafetyTests(Base):
    def test_swaps_use_strong_recommendations_or_like_for_like(self):
        for name in ("Krenko Goblins", "Meren Aristocrats"):
            d, _ = real_decks.build(self.db, self.st, name)
            res = analyser.analyse(self.db, self.st, d, ALL)
            tiers = {c["name"]: c.get("tier") for c in res["considered"]}
            for s in res["swaps"]:
                shared = set(s["in_roles"]) & set(s["out_roles"])
                self.assertTrue(shared or s["into"] not in tiers, s)  # swap INs leave Consider
                self.assertNotIn(s["out"], real_decks.DECKS[name]["must_keep"])
            self.assertFalse({c["name"] for c in res["considered"]} & {s["into"] for s in res["swaps"]})

    def test_why_not_debug(self):
        d = self.tokens_deck()
        w = recommend.why_not(self.db, self.st, d, ALL, "Lightning Bolt")
        self.assertFalse(w["recommended"])
        self.assertIn("colour identity", w["reason"])
        w = recommend.why_not(self.db, self.st, d, ALL, "Secure the Wastes")
        self.assertIn("already in the deck", w["reason"])


def screen(db, st, res, deck):
    from analyse_view import AnalyseScreen
    app = type("A", (), {})()
    app.settings, app.userdb, app.decks_screen, app.collection_screen, app.dialog = {"currency": "USD"}, db, None, \
        None, None
    app.money = lambda u: f"${u:.2f}"
    app.toasts = []
    app.toast = lambda t, *a: app.toasts.append(t)
    app.toast_text = lambda: None
    app.open_dialog = lambda d: setattr(app, "dialog", d)
    sc = AnalyseScreen(app, 1400, 650, store=st)
    sc.deck_id, sc.result, sc.stale, sc.tab = deck, res, False, "consider"
    return sc


def texts():
    from ui import Painter
    p = Painter(1400, 650)
    out = []
    orig = p.text

    def spy(xy, text, *a, **k):
        out.append(str(text))
        return orig(xy, text, *a, **k)
    p.text = spy
    return p, out


@unittest.skipUnless(has_cards(), "needs cards.db with oracle_cards")
class ScreenTests(Base):
    def test_grouped_consider_screen_and_feedback_buttons(self):
        d = self.deck("thin", "Teysa Karlov", ARCHETYPES["Aristocrats"][1] + FILLER)
        res = analyser.analyse(self.db, self.st, d, dict(ALL, tags=["Aristocrats"]))
        sc = screen(self.db, self.st, res, d)
        rows = sc.consider_rows()
        self.assertEqual(rows[0][0], "group")
        cards = [r for r in rows if r[0] == "card"]
        self.assertTrue(cards)
        groups = [r for r in rows if r[0] == "group"]
        self.assertTrue(all(len([1 for r in rows[rows.index(g) + 1:] if r[0] == "card"]) for g in groups))
        p, out = texts()
        sc._consider(p, 372, 1384, 52)
        joined = " ".join(out)
        self.assertIn("Cards worth considering", joined)
        self.assertNotIn("confidence", joined.lower().replace("lower-confidence", ""))  # no numeric score shown
        idx = cards[0][1]
        name = res["considered"][idx]["name"]
        fb = next(h for h in p.hits if h[4] == ("fb", (name, "not_for_deck")))
        sc.click((fb[0] + fb[2]) // 2, (fb[1] + fb[3]) // 2, p.hits)
        self.assertEqual(self.db.suggestion_feedback(d)[name], "not_for_deck")
        self.assertNotIn(name, [c["name"] for _, c in sc.consider_items()])  # gone straight away
        more = [r for r in sc.consider_rows() if r[0] == "more"]
        if more:
            sc.click(0, 0, [(0, 0, 10, 10, ("more", more[0][1]))])
            self.assertIn(more[0][1], sc.expanded)

    def test_lower_confidence_ideas_are_hidden_until_asked(self):
        d, _ = real_decks.build(self.db, self.st, "Atraxa Superfriends")
        res = analyser.analyse(self.db, self.st, d, ALL)
        sc = screen(self.db, self.st, res, d)
        niche = [c for c in res["considered"] if c["tier"] == "NICHE"]
        self.assertTrue(niche)
        shown = {c["name"] for _, c in sc.consider_items()}
        self.assertFalse(shown & {c["name"] for c in niche})
        sc.click(0, 0, [(0, 0, 10, 10, ("lowconf", None))])
        self.assertTrue({c["name"] for _, c in sc.consider_items()} & {c["name"] for c in niche})

    def test_old_saved_analysis_without_tiers_still_renders(self):
        d = self.tokens_deck()
        res = analyser.analyse(self.db, self.st, d, ALL, quick=True)
        res["considered"] = [dict(name="Viscera Seer", confidence=0.7, why=["x"], ownership=dict(status="not_owned"),
                                  links=[], links_total=0)]
        res.pop("recommendations", None)
        sc = screen(self.db, self.st, res, d)
        p, out = texts()
        sc._consider(p, 372, 1384, 52)
        self.assertIn("Viscera Seer", out)


if __name__ == "__main__":
    unittest.main()
