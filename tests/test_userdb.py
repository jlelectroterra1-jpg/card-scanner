"""Tests for the permanent collection/deck database (userdb.py).

    python -m unittest tests.test_userdb -v

Everything runs on temporary databases, never data/user.db.
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import userdb  # noqa: E402
from userdb import UserDB, UserDBError  # noqa: E402

BOLT = dict(scryfall_id="bolt-m11", card_name="Lightning Bolt", set_code="M11", collector_number="149")
BOLT_2 = dict(scryfall_id="bolt-2xm", card_name="Lightning Bolt", set_code="2xm", collector_number="117")
SOL = dict(scryfall_id="sol-c21", card_name="Sol Ring", set_code="c21", collector_number="263")


def make_cards_db(path, with_new_columns=True):
    """A tiny Scryfall-style cards.db for join tests."""
    db = sqlite3.connect(path)
    cols = ("id TEXT PRIMARY KEY, name TEXT, set_code TEXT, collector_number TEXT, lang TEXT, "
            "usd TEXT, usd_foil TEXT, usd_etched TEXT, oracle_id TEXT")
    if with_new_columns:
        cols += ", color_identity TEXT, legal_commander TEXT"
    db.execute(f"CREATE TABLE cards ({cols})")
    rows = [("bolt-m11", "Lightning Bolt", "m11", "149", "en", "1.50", "9.00", None, "o-bolt", "R", "legal"),
            ("bolt-2xm", "Lightning Bolt", "2xm", "117", "en", "1.20", "3.00", None, "o-bolt", "R", "legal"),
            ("sol-c21", "Sol Ring", "c21", "263", "en", "2.00", None, None, "o-sol", "", "legal"),
            ("growth", "Rampant Growth", "m10", "200", "en", "0.30", None, None, "o-rg", "G", "legal"),
            ("ancestral", "Ancestral Recall", "lea", "48", "en", "9999", None, None, "o-anc", "U", "banned")]
    if not with_new_columns:
        rows = [r[:9] for r in rows]
    db.executemany(f"INSERT INTO cards VALUES ({','.join('?' * len(rows[0]))})", rows)
    db.commit()
    db.close()


class UserDBTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tmp_userdb_", dir=HERE)
        self.addCleanup(shutil.rmtree, self.tmp, True)  # registered first = runs last, after DBs close
        self.path = os.path.join(self.tmp, "user.db")
        self._backup_dir = userdb.BACKUP_DIR
        userdb.BACKUP_DIR = os.path.join(self.tmp, "backups")

    def tearDown(self):
        userdb.BACKUP_DIR = self._backup_dir

    def open(self, **kw):
        db = UserDB(self.path, **kw)
        self.addCleanup(db.close)
        return db

    # ---- creation ---------------------------------------------------------------

    def test_creates_on_first_run_with_main_collection(self):
        db = self.open()
        self.assertEqual(db.version, userdb.LATEST_VERSION)
        cols = db.collections()
        self.assertEqual([c["name"] for c in cols], ["Main Collection"])
        self.assertEqual(cols[0]["is_default"], 1)
        self.assertEqual(db.default_collection_id(), cols[0]["id"])
        logged = db.conn.execute("SELECT version FROM schema_migrations").fetchall()
        self.assertEqual([r[0] for r in logged], [m[0] for m in userdb.MIGRATIONS])
        self.assertEqual(db.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(db.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_reopening_keeps_data_and_does_not_duplicate_default(self):
        db = self.open()
        db.add_card(**BOLT)
        db.close()
        db2 = self.open()
        self.assertEqual(db2.applied, [])
        self.assertEqual(len(db2.collections()), 1)
        self.assertEqual(db2.copies_owned("Lightning Bolt"), 1)

    # ---- cards ----------------------------------------------------------------------

    def test_insert_and_increment_quantity(self):
        db = self.open()
        a = db.add_card(**BOLT, market_price=1.5)
        b = db.add_card(**BOLT)
        c = db.add_card(**BOLT, quantity=2)
        self.assertEqual(a, b)
        self.assertEqual(a, c)
        item = db.item(a)
        self.assertEqual(item["quantity"], 4)
        self.assertEqual(item["condition"], "near_mint")
        self.assertEqual(item["language"], "en")
        self.assertEqual(item["set_code"], "m11")  # stored lower-case like Scryfall
        self.assertEqual(item["market_price"], 1.5)  # a later add without a price keeps it
        self.assertIsNotNone(item["added_at"])

    def test_foil_and_nonfoil_stay_separate(self):
        db = self.open()
        n = db.add_card(**BOLT, finish="nonfoil")
        f = db.add_card(**BOLT, finish="foil")
        e = db.add_card(**BOLT, finish="etched")
        self.assertEqual(len({n, f, e}), 3)
        self.assertEqual(db.copies_owned("Lightning Bolt"), 3)
        self.assertEqual({p["finish"] for p in db.printings_owned("lightning bolt")}, {"nonfoil", "foil", "etched"})
        with self.assertRaises(ValueError):
            db.add_card(**BOLT, finish="shiny")

    def test_conditions_and_printings_are_separate_lots(self):
        db = self.open()
        a = db.add_card(**BOLT)
        b = db.add_card(**BOLT, condition="lightly_played", purchase_price=0.8, notes="from a trade")
        c = db.add_card(**BOLT_2)
        self.assertEqual(len({a, b, c}), 3)
        self.assertEqual(db.item(b)["purchase_price"], 0.8)
        self.assertEqual(db.item(b)["notes"], "from a trade")
        self.assertEqual(db.conn.execute("SELECT printings FROM owned_cards").fetchone()[0], 2)
        with self.assertRaises(ValueError):
            db.add_card(**BOLT, condition="ruined")

    def test_quantity_changes(self):
        db = self.open()
        a = db.add_card(**BOLT, quantity=3)
        db.remove_copies(a, 1)
        self.assertEqual(db.item(a)["quantity"], 2)
        db.remove_copies(a, 5)
        self.assertEqual(db.item(a)["quantity"], 0)
        self.assertEqual(db.copies_owned("Lightning Bolt"), 0)
        db.set_quantity(a, 4)
        self.assertEqual(db.copies_owned("Lightning Bolt"), 4)
        with self.assertRaises(ValueError):
            db.set_quantity(a, -1)

    # ---- collections ------------------------------------------------------------------

    def test_multiple_collections(self):
        db = self.open()
        trade = db.create_collection("Trade Binder", kind="binder")
        box = db.create_collection("Box 1", kind="box")
        db.add_card(**BOLT, market_price=1.5)
        db.add_card(**BOLT, collection_id=trade, market_price=1.5, quantity=2)
        db.add_card(**SOL, collection_id=box, market_price=2.0)
        self.assertEqual(len(db.items(trade)), 1)
        self.assertEqual(db.items(trade)[0]["quantity"], 2)
        self.assertEqual(db.copies_owned("Lightning Bolt"), 3)
        self.assertAlmostEqual(db.collection_value(), 1.5 * 3 + 2.0)
        self.assertAlmostEqual(db.collection_value(trade), 3.0)
        self.assertEqual(db.collection_id("trade binder"), trade)
        with self.assertRaises(sqlite3.IntegrityError):
            db.create_collection("Trade Binder")
        # a collection that still holds cards can't be deleted by accident
        with self.assertRaises(sqlite3.IntegrityError):
            db.conn.execute("DELETE FROM collections WHERE id = ?", (trade,))
        self.assertEqual(set(r["collection"] for r in db.printings_owned("Lightning Bolt")),
                         {"Main Collection", "Trade Binder"})

    # ---- decks ----------------------------------------------------------------------------

    def test_decks_commander_partner_and_cards(self):
        db = self.open()
        deck = db.create_deck("Thrasios + Tymna", notes="cEDH-ish")
        db.set_commander(deck, "Thrasios, Triton Hero", scryfall_id="thrasios")
        db.set_commander(deck, "Tymna the Weaver", partner=True)
        db.add_deck_card(deck, "Sol Ring")
        db.add_deck_card(deck, "Island", quantity=8)
        info = db.deck(deck)
        self.assertEqual(info["commander"], "Thrasios, Triton Hero")
        self.assertEqual(info["partner"], "Tymna the Weaver")
        self.assertEqual(info["format"], "commander")
        self.assertEqual(info["card_count"], 11)
        # replacing the commander keeps exactly one
        db.set_commander(deck, "Kraum, Ludevic's Opus")
        self.assertEqual(db.deck(deck)["commander"], "Kraum, Ludevic's Opus")
        self.assertEqual(sum(1 for c in db.deck_cards(deck) if c["role"] == "commander"), 1)
        with self.assertRaises(sqlite3.IntegrityError):
            db.add_deck_card(deck, "Another Commander", role="commander")
        with self.assertRaises(ValueError):
            db.add_deck_card(deck, "Sol Ring", role="nonsense")

    def test_deck_updated_at_changes(self):
        db = self.open()
        deck = db.create_deck("Test")
        db.conn.execute("UPDATE decks SET updated_at = '2000-01-01T00:00:00Z' WHERE id = ?", (deck,))
        db.add_deck_card(deck, "Sol Ring")
        self.assertNotEqual(db.deck(deck)["updated_at"], "2000-01-01T00:00:00Z")

    def test_deck_cards_linked_to_owned_copies(self):
        db = self.open()
        sol = db.add_card(**SOL, quantity=2)
        d1, d2, d3 = db.create_deck("Deck 1"), db.create_deck("Deck 2"), db.create_deck("Deck 3")
        db.add_deck_card(d1, "Sol Ring", scryfall_id=SOL["scryfall_id"], collection_item_id=sol)
        db.add_deck_card(d2, "Sol Ring", collection_item_id=sol)
        # both owned copies are in use now
        avail = db.availability(sol)
        self.assertEqual((avail["quantity"], avail["allocated"], avail["available"]), (2, 2, 0))
        with self.assertRaises(UserDBError):
            db.add_deck_card(d3, "Sol Ring", collection_item_id=sol)
        dc = db.add_deck_card(d3, "Sol Ring")  # in the deck, but not (yet) an owned copy
        with self.assertRaises(UserDBError):
            db.allocate(dc, sol)
        db.add_card(**SOL)  # buy a third copy
        db.allocate(dc, sol)
        self.assertEqual(db.availability(sol)["available"], 0)
        using = db.decks_using("sol ring")
        self.assertEqual(sorted(u["deck"] for u in using), ["Deck 1", "Deck 2", "Deck 3"])
        self.assertTrue(all(u["collection_item_id"] == sol for u in using))
        # deleting a deck frees its copies
        db.conn.execute("DELETE FROM decks WHERE id = ?", (d1,))
        self.assertEqual(db.availability(sol)["available"], 1)
        # removing a lot keeps deck cards but unlinks them
        db.conn.execute("DELETE FROM collection_items WHERE id = ?", (sol,))
        self.assertTrue(all(u["collection_item_id"] is None for u in db.decks_using("Sol Ring")))

    def test_which_recommended_cards_are_owned(self):
        db = self.open()
        db.add_card(**BOLT, quantity=2)
        db.add_card(**SOL)
        self.assertEqual(db.which_owned(["Sol Ring", "Lightning Bolt", "Mana Crypt"]),
                         {"Sol Ring": 1, "Lightning Bolt": 2})

    # ---- Scryfall joins -----------------------------------------------------------------------

    def test_owned_cards_legal_for_a_commander(self):
        cards = os.path.join(self.tmp, "cards.db")
        make_cards_db(cards)
        db = self.open()
        db.add_card(**BOLT)
        db.add_card(**SOL)
        db.add_card("growth", "Rampant Growth", "m10", "200")
        db.add_card("ancestral", "Ancestral Recall", "lea", "48")
        names = [r["card_name"] for r in db.owned_cards_for_commander("UR", cards)]
        self.assertEqual(names, ["Lightning Bolt", "Sol Ring"])  # no green card, no banned card

    def test_legality_query_needs_new_cards_db(self):
        cards = os.path.join(self.tmp, "cards_old.db")
        make_cards_db(cards, with_new_columns=False)
        db = self.open()
        with self.assertRaises(UserDBError):
            db.owned_cards_for_commander("R", cards)

    def test_refresh_prices_and_import_session(self):
        cards = os.path.join(self.tmp, "cards.db")
        make_cards_db(cards)
        con = sqlite3.connect(cards)
        con.row_factory = sqlite3.Row
        lookup = lambda cid: dict(con.execute("SELECT * FROM cards WHERE id = ?", (cid,)).fetchone() or {}) or None
        db = self.open()
        entries = [dict(id="bolt-m11", finish="nonfoil"), dict(id="bolt-m11", finish="nonfoil"),
                   dict(id="bolt-m11", finish="foil"), dict(id="sol-c21", finish="nonfoil"),
                   dict(id="unknown-card", finish="nonfoil")]
        self.assertEqual(db.import_session(entries, lookup), 4)
        lots = {(i["scryfall_id"], i["finish"]): i for i in db.items()}
        self.assertEqual(lots[("bolt-m11", "nonfoil")]["quantity"], 2)
        self.assertEqual(lots[("bolt-m11", "foil")]["market_price"], 9.0)
        self.assertEqual(lots[("bolt-m11", "nonfoil")]["oracle_id"], "o-bolt")
        con.execute("UPDATE cards SET usd = '5.00' WHERE id = 'bolt-m11'")
        con.commit()
        self.assertEqual(db.refresh_prices(cards), 3)
        self.assertEqual(db.item(lots[("bolt-m11", "nonfoil")]["id"])["market_price"], 5.0)
        con.close()

    # ---- migrations / safety -----------------------------------------------------------------

    def test_migration_upgrades_without_losing_data_and_backs_up(self):
        db = self.open()
        db.add_card(**BOLT, quantity=3)
        deck = db.create_deck("Kept")
        db.close()
        v2 = userdb.MIGRATIONS + [(2, "test: add a binder colour", [
            "ALTER TABLE collections ADD COLUMN colour TEXT",
            "CREATE TABLE wishlist (id INTEGER PRIMARY KEY, card_name TEXT NOT NULL)",
        ])]
        db2 = self.open(migrations=v2)
        self.assertEqual(db2.applied, [2])
        self.assertEqual(db2.version, 2)
        self.assertEqual(db2.copies_owned("Lightning Bolt"), 3)
        self.assertEqual(db2.deck(deck)["name"], "Kept")
        db2.conn.execute("UPDATE collections SET colour = 'red'")
        backups = os.listdir(userdb.BACKUP_DIR)
        self.assertEqual(len(backups), 1)
        self.assertIn("before-v2", backups[0])
        b = sqlite3.connect(os.path.join(userdb.BACKUP_DIR, backups[0]))
        self.assertEqual(b.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(b.execute("SELECT SUM(quantity) FROM collection_items").fetchone()[0], 3)
        b.close()

    def test_failed_migration_changes_nothing(self):
        db = self.open()
        db.add_card(**BOLT, quantity=2)
        db.close()
        broken = userdb.MIGRATIONS + [(2, "broken", [
            "ALTER TABLE collections ADD COLUMN colour TEXT",
            "THIS IS NOT SQL",
        ])]
        with self.assertRaises(UserDBError):
            UserDB(self.path, migrations=broken)
        db2 = self.open()
        self.assertEqual(db2.version, 1)
        self.assertEqual(db2.copies_owned("Lightning Bolt"), 2)
        cols = [r[1] for r in db2.conn.execute("PRAGMA table_info(collections)")]
        self.assertNotIn("colour", cols)  # the half-done step was rolled back

    def test_database_from_newer_app_is_not_touched(self):
        db = self.open()
        db.add_card(**BOLT)
        db.conn.execute("PRAGMA user_version = 99")
        db.close()
        with self.assertRaises(UserDBError):
            UserDB(self.path)
        con = sqlite3.connect(self.path)
        self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 99)
        self.assertEqual(con.execute("SELECT quantity FROM collection_items").fetchone()[0], 1)
        con.close()

    def test_failed_write_rolls_back(self):
        db = self.open()
        db.add_card(**BOLT)
        with self.assertRaises(RuntimeError):
            with db.transaction() as c:
                c.execute("UPDATE collection_items SET quantity = 50")
                raise RuntimeError("crash halfway")
        self.assertEqual(db.copies_owned("Lightning Bolt"), 1)


class UpdateDbTests(unittest.TestCase):
    def test_new_scryfall_columns_are_stored(self):
        """update_db.build() on a tiny bulk file: the new collection/deck fields are filled."""
        import gzip
        import update_db
        tmp = tempfile.mkdtemp(prefix="tmp_updatedb_", dir=HERE)
        self.addCleanup(shutil.rmtree, tmp, True)
        raw, out = os.path.join(tmp, "cards.jsonl.gz"), os.path.join(tmp, "cards.db")
        card = {"id": "abc", "oracle_id": "o-abc", "name": "Sol Ring", "set": "c21", "set_name": "Commander 2021",
                "collector_number": "263", "rarity": "uncommon", "lang": "en", "released_at": "2021-04-23",
                "finishes": ["nonfoil"], "promo": False, "layout": "normal", "type_line": "Artifact",
                "color_identity": [], "legalities": {"commander": "legal"},
                "prices": {"usd": "2.00"}, "image_uris": {"small": "s", "normal": "n"}}
        with gzip.open(raw, "wt", encoding="utf-8") as f:
            f.write(json.dumps(card) + "\n")
        old = (update_db.RAW_PATH, update_db.DB_PATH)
        update_db.RAW_PATH, update_db.DB_PATH = raw, out
        try:
            update_db.build()
        finally:
            update_db.RAW_PATH, update_db.DB_PATH = old
        con = sqlite3.connect(out)
        row = con.execute("SELECT name, usd, oracle_id, type_line, color_identity, legal_commander FROM cards").fetchone()
        con.close()
        self.assertEqual(row, ("Sol Ring", "2.00", "o-abc", "Artifact", "", "legal"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
