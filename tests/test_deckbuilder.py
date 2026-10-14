"""Build a Commander deck from the Collection.

Realistic collections are made from the card database (the most played Commander cards in
the commander's colours, plus each archetype's own pieces), so the builder is tested on the
kind of pool a real player owns.

    python -m unittest tests.test_deckbuilder -v
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import community  # noqa: E402
import deckbuilder  # noqa: E402
import deckrules  # noqa: E402
import userdb  # noqa: E402
from decks import DeckStore  # noqa: E402
from test_analyser_understanding import ARCHETYPES, has_cards  # noqa: E402
from userdb import UserDB  # noqa: E402

CARDS_DB = os.path.join(ROOT, "data", "cards.db")


def popular_in(identity, top=1800, extra=()):
    """The most played Commander cards inside a colour identity (lands included)."""
    con = sqlite3.connect(CARDS_DB)
    cols = {r[1] for r in con.execute("PRAGMA table_info(oracle_cards)")}
    order = "edhrec_rank IS NULL, edhrec_rank" if "edhrec_rank" in cols else "name"
    out = []
    for name, ci, legal in con.execute(f"SELECT name, color_identity, legal_commander FROM oracle_cards ORDER BY {order}"):
        if legal == "legal" and set(ci or "") <= set(identity):
            out.append(name)
            if len(out) >= top:
                break
    con.close()
    return list(dict.fromkeys(out + list(extra)))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tmp_build_", dir=HERE)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        old = userdb.BACKUP_DIR
        userdb.BACKUP_DIR = os.path.join(self.tmp, "b")
        self.addCleanup(setattr, userdb, "BACKUP_DIR", old)
        old_cache = community.CACHE_PATH
        community.CACHE_PATH = os.path.join(self.tmp, "c.db")
        self.addCleanup(setattr, community, "CACHE_PATH", old_cache)
        self.db = UserDB(os.path.join(self.tmp, "u.db"))
        self.addCleanup(self.db.close)
        self.st = DeckStore(self.db)

    def own(self, names):
        self.db.attach_cards_db()
        prints = {}
        for r in self.db.conn.execute("SELECT name, id, set_code, collector_number FROM scry.cards WHERE lang = 'en' "
                                      "ORDER BY released_at"):
            prints.setdefault(r[0], r)
        for n in names:
            p = prints.get(n)
            if p and not deckrules.is_basic(n):
                self.db.add_card(p[1], n, p[2], p[3])

    def build(self, cmd, **settings):
        return deckbuilder.build(self.db, self.st, cmd, settings)

    def check_legal(self, res, cmd):
        self.assertTrue(res["complete"], [i["text"] for i in res["issues"]])
        self.assertEqual(res["total"], 100)
        ident = set(res["identity"])
        counts = Counter()
        for c in res["cards"]:
            counts[c["name"]] += c["qty"]
        self.assertNotIn(cmd, counts)
        for n, k in counts.items():
            info = self.st.default_printing(n) or {}
            if not deckrules.is_basic(n):
                self.assertEqual(k, 1, n)  # singleton
            self.assertTrue(set(info.get("color_identity") or "") <= ident, n)
        lands = sum(c["qty"] for c in res["cards"] if c["section"] == "Lands")
        self.assertTrue(30 <= lands <= 40, lands)
        return counts


@unittest.skipUnless(has_cards(), "needs cards.db with oracle_cards")
class BuildTests(Base):
    CMD = "Caesar, Legion's Emperor"

    def setUp(self):
        super().setUp()
        self.own(popular_in("WBR", 1500, ["Metastatic Evangel", "Skullclamp", "Bitterblossom", "Mirkwood Bats"]))

    def test_legal_100_card_deck_from_owned_cards(self):
        res = self.build(self.CMD)
        counts = self.check_legal(res, self.CMD)
        for c in res["cards"]:
            self.assertIn(c["status"], ("available", "basic"), c["name"])  # only free copies you own
            self.assertTrue(c["why"], c["name"])  # every card says why it's here
        self.assertGreater(sum(k for n, k in counts.items() if deckrules.is_basic(n)), 1)  # basics repeat
        src = res["lands"]["sources"]
        self.assertTrue(all(src[c] >= 6 for c in "WBR"), src)
        for r in ("Ramp", "Card Draw", "Removal"):
            self.assertGreaterEqual(res["role_counts"][r], res["targets"][r]["target"][0] - 1, r)
        self.assertLess(res["avg_mv"], 3.6)
        self.assertLessEqual(sum(v for k, v in res["curve"].items() if int(k) >= 6), 8)

    def test_unavailable_cards_are_excluded_unless_allowed(self):
        other = self.st.create_deck("Other")
        lot = next(r for r in self.db.conn.execute("SELECT id, scryfall_id FROM collection_items WHERE card_name = ?",
                                                   ("Skullclamp",)))
        self.db.add_deck_card(other, "Skullclamp", scryfall_id=lot[1], collection_item_id=lot[0])
        res = self.build(self.CMD)
        self.assertNotIn("Skullclamp", [c["name"] for c in res["cards"]])
        res = self.build(self.CMD, scope="used")
        clamp = [c for c in res["cards"] if c["name"] == "Skullclamp"]
        if clamp:
            self.assertEqual(clamp[0]["status"], "in_use")
            self.assertIn("USED IN OTHER", clamp[0]["label"])

    def test_strategy_tags_change_the_build(self):
        tokens = {c["name"] for c in self.build(self.CMD, tags=["Tokens"])["cards"]}
        aristo = self.build(self.CMD, tags=["Aristocrats"])
        self.assertTrue(any(c["stage"].startswith("Engine: Sacrifice") for c in aristo["cards"]))
        self.assertGreater(len(tokens ^ {c["name"] for c in aristo["cards"]}), 6)
        self.assertEqual(aristo["plan"]["primary"], ["Aristocrats"])

    def test_casual_balanced_high_differ(self):
        casual = self.build(self.CMD, style="casual")
        high = self.build(self.CMD, style="high")
        self.assertLess(high["avg_mv"], casual["avg_mv"] + 0.05)
        self.assertGreaterEqual(high["role_counts"]["Removal"] + high["role_counts"]["Ramp"],
                                casual["role_counts"]["Removal"] + casual["role_counts"]["Ramp"])
        self.assertNotEqual({c["name"] for c in casual["cards"]}, {c["name"] for c in high["cards"]})

    def test_locked_cards_persist_and_regenerate_varies(self):
        first = self.build(self.CMD)
        keep = [c["name"] for c in first["cards"] if c["section"] != "Lands"][:20]
        again = self.build(self.CMD, locked=keep, seed=3, bias="aggressive")
        names = {c["name"] for c in again["cards"]}
        self.assertTrue(set(keep) <= names)
        self.assertTrue(all(c["locked"] for c in again["cards"] if c["name"] in keep))
        self.check_legal(again, self.CMD)
        self.assertNotEqual(names, {c["name"] for c in first["cards"]})

    def test_save_creates_the_deck_and_links_free_copies(self):
        res = self.build(self.CMD)
        before = self.db.conn.execute("SELECT COUNT(*), SUM(quantity) FROM collection_items").fetchone()
        deck = deckbuilder.save(self.db, self.st, res, "Caesar (built)")
        rows = self.st.deck_cards(deck)
        self.assertEqual(sum(r["quantity"] for r in rows if r["role"] in ("commander", "main")), 100)
        linked = [r for r in rows if r.get("collection_item_id")]
        self.assertGreater(len(linked), 50)
        self.assertEqual(tuple(self.db.conn.execute("SELECT COUNT(*), SUM(quantity) FROM collection_items").fetchone()),
                         tuple(before))  # the collection itself is unchanged
        res2 = self.build(self.CMD)  # those copies are no longer free
        self.assertFalse({r["card_name"] for r in linked if r["role"] == "main"} & {
            c["name"] for c in res2["cards"] if c["status"] == "available"} - {"Plains", "Swamp", "Mountain"})

    def test_unsupported_payoff_is_not_built_in(self):
        res = self.build(self.CMD, tags=["Tokens"])
        self.assertNotIn("Metastatic Evangel", [c["name"] for c in res["cards"]])  # proliferate, no counters

    def test_combo_signal_does_not_dominate(self):
        b = deckbuilder.Builder(self.db, self.st, self.CMD, {})
        b.counts, b.goal_counts = Counter(), {}
        b.understand()
        plain, _ = b.score("Gray Ogre" if "Gray Ogre" in b.pool else next(iter(b.pool)))
        name = "Gray Ogre" if "Gray Ogre" in b.pool else next(iter(b.pool))
        b.comm = {name: [dict(source="spellbook", kind="combo piece", metric=1.0, detail="x", retrieved="")]}
        boosted, _ = b.score(name)
        self.assertLessEqual(boosted - plain, 0.03)

    def test_commander_that_draws_lowers_the_draw_target(self):
        drawer = deckbuilder.Builder(self.db, self.st, "Tymna the Weaver", {})  # draws cards
        plain = deckbuilder.Builder(self.db, self.st, "Adeline, Resplendent Cathar", {})
        for b in (drawer, plain):
            b.counts = Counter()
            b.understand()
        self.assertLess(drawer.targets()["Card Draw"]["target"][0], plain.targets()["Card Draw"]["target"][0])
        self.assertIn("the commander draws cards itself", drawer.targets()["Card Draw"]["why"])


@unittest.skipUnless(has_cards(), "needs cards.db with oracle_cards")
class BuildScreenThumbnailTests(Base):
    """Small card pictures in the built deck list: exact printing, only visible rows load,
    click opens the large viewer, locks and rebuilds keep working."""
    CMD = "Caesar, Legion's Emperor"

    def setUp(self):
        super().setUp()
        self.own(popular_in("WBR", 1200))
        import card_viewer
        old = card_viewer.fetch
        card_viewer.fetch = lambda url, path, done: done(False)  # never touch the network in tests
        self.addCleanup(setattr, card_viewer, "fetch", old)
        from analyse_view import AnalyseScreen
        app = type("A", (), {})()
        app.settings, app.userdb, app.decks_screen, app.collection_screen, app.dialog = {"currency": "USD"},             self.db, None, None, None
        app.money = lambda u: f"${u or 0:.2f}"
        app.toast = lambda *a: None
        app.toast_text = lambda: None
        app.open_dialog = lambda d: setattr(app, "dialog", d)
        self.app = app
        self.sc = AnalyseScreen(app, 1400, 650, store=self.st)
        self.sc.mode = "build"
        self.b = self.sc.build
        self.b.set_commander(self.CMD)
        self.b.result = self.build(self.CMD)
        self.requested = []
        orig = self.sc.images.small
        self.sc.images.small = lambda sid, url=None, w=None, h=None: (self.requested.append((sid, w, h)),
                                                                      orig(sid, url, w, h))[1]

    def render(self):
        self.sc.bump()
        return self.sc.render()

    def test_thumbnails_use_the_exact_printing_and_only_visible_rows_load(self):
        from decks_view import THUMB_H, THUMB_W
        _, hits = self.render()
        cards = {c["name"]: c for c in self.b.result["cards"]}
        self.assertTrue(self.b.visible)
        thumbs = [r for r in self.requested if r[1] is not None]  # (the commander picture on the left is bigger)
        self.assertEqual(len(thumbs), len(self.b.visible))  # only what is on screen
        self.assertLess(len(thumbs), len(cards))
        self.assertTrue(all(w == THUMB_W and h == THUMB_H for _, w, h in thumbs))  # same size as the lists
        for name in self.b.visible:
            c = cards[name]
            self.assertIn((c["scryfall_id"], THUMB_W, THUMB_H), self.requested)
            lot = self.db.conn.execute("SELECT scryfall_id FROM collection_items WHERE card_name = ?", (name,)).fetchone()
            if lot:
                self.assertEqual(c["scryfall_id"], lot[0])  # your own copy's printing
        self.requested.clear()
        for _ in range(5):  # scrolling: still only the visible rows
            self.b.wheel(-1)
            self.render()
        self.assertLessEqual(len(self.requested), 5 * 14)

    def test_thumbnail_click_opens_the_large_viewer_and_rows_stay_aligned(self):
        from card_viewer import CardViewer
        _, hits = self.render()
        thumbs = [h for h in hits if h[4] and h[4][0] == "viewid"]
        locks = [h for h in hits if h[4] and h[4][0] == "b_lock"]
        names = [h for h in hits if h[4] and h[4][0] == "b_why"]
        self.assertTrue(thumbs and locks and names)
        self.assertEqual(len({h[0] for h in thumbs}), 1)  # same columns on every row
        self.assertEqual(len({h[0] for h in locks}), 1)
        self.assertEqual(len({h[0] for h in names}), 1)
        self.assertLess(thumbs[0][2], locks[0][0])  # [thumbnail] [Lock] [Qty] [Card Name]...
        x0, y0, x1, y1, action = thumbs[0]
        self.sc.click((x0 + x1) // 2, (y0 + y1) // 2, hits)
        self.assertIsInstance(self.app.dialog, CardViewer)
        self.assertEqual(self.app.dialog.card["id"], action[1])

    def test_lock_and_rebuild_keep_working_with_pictures(self):
        _, hits = self.render()
        lock = next(h for h in hits if h[4] and h[4][0] == "b_lock")
        self.sc.click((lock[0] + lock[2]) // 2, (lock[1] + lock[3]) // 2, hits)
        self.assertIn(lock[4][1], self.b.locked)
        again = self.build(self.CMD, locked=sorted(self.b.locked), seed=2)
        self.assertTrue(all(c["scryfall_id"] for c in again["cards"]))
        self.b.result = again
        self.render()
        self.assertTrue(self.b.visible)


@unittest.skipUnless(has_cards(), "needs cards.db with oracle_cards")
class SmallCollectionTests(Base):
    def test_insufficient_collection_is_handled_honestly(self):
        self.own(popular_in("WBR", 60))
        res = self.build("Caesar, Legion's Emperor")
        self.assertFalse(res["complete"])
        self.assertLess(res["total"], 100)  # no silent garbage
        self.assertTrue(res["shortfalls"])
        with self.assertRaises(deckbuilder.BuildError):
            deckbuilder.save(self.db, self.st, res, "x")
        padded = self.build("Caesar, Legion's Emperor", fill_weaker=True, pad_lands=True)
        self.assertEqual(padded["total"], 100)

    def test_not_a_commander(self):
        with self.assertRaises(deckbuilder.BuildError):
            self.build("Sol Ring")


@unittest.skipUnless(has_cards(), "needs cards.db with oracle_cards")
class ArchetypeBuildTests(Base):
    """Sample decks for ten archetypes, each from a realistic collection for that commander."""
    EXPECT = {"Tokens": "Tokens", "Aristocrats": "Aristocrats", "Voltron": "Voltron", "Spellslinger": "Spellslinger",
              "Reanimator": "Reanimator", "Landfall": "Landfall", "Tribal": "Tribal", "Artifacts": "Artifacts",
              "Enchantress": "Enchantments", "Counters": "Counters"}

    def test_archetypes(self):
        for arch, tag in self.EXPECT.items():
            cmd, plan, key, _, _ = ARCHETYPES[arch]
            with self.subTest(arch):
                ident = (self.st.default_printing(cmd) or {}).get("color_identity") or ""
                self.own(popular_in(ident, 900, plan + [key]))
                res = self.build(cmd, tags=[tag])
                self.check_legal(res, cmd)
                engine = [c for c in res["cards"] if c["stage"].startswith(("Engine", "Locked")) or "tribe" in c["stage"]]
                self.assertTrue(engine, f"{arch}: no engine cards")
                self.assertFalse([i for i in res["issues"] if i["level"] == "error"])


if __name__ == "__main__":
    unittest.main()
