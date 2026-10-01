"""Phase 2 tests: collection browsing/editing/export/import, USD/ZAR, Add to Collection,
migrations, and speed with 50,000 cards.

    python -m unittest tests.test_collection -v

Everything runs on temporary databases; data/user.db and your scan list are never touched.
"""
import csv
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import userdb  # noqa: E402
from collection import CollectionStore  # noqa: E402
from currency import display_currency, money, parse_rate  # noqa: E402
from userdb import UserDB, UserDBError  # noqa: E402

CARDS = [  # id, name, set, set_name, number, rarity, finishes, usd, usd_foil, usd_etched, colour id, type, released
    ("bolt-m11", "Lightning Bolt", "m11", "Magic 2011", "149", "common", "nonfoil,foil", "1.50", "9.00", None, "R",
     "Instant", "2010-07-16"),
    ("bolt-2xm", "Lightning Bolt", "2xm", "Double Masters", "117", "uncommon", "nonfoil,foil", "1.20", "3.00", None,
     "R", "Instant", "2020-08-07"),
    ("sol-c21", "Sol Ring", "c21", "Commander 2021", "263", "uncommon", "nonfoil", "2.00", None, None, "",
     "Artifact", "2021-04-23"),
    ("crypt", "Mana Crypt", "2xm", "Double Masters", "270", "mythic", "nonfoil,foil", "180.00", "250.00", None, "",
     "Artifact", "2020-08-07"),
    ("growth", "Rampant Growth", "m10", "Magic 2010", "200", "common", "nonfoil,foil", "0.30", "1.00", None, "G",
     "Sorcery", "2009-07-17"),
    ("atraxa", "Atraxa, Praetors' Voice", "c16", "Commander 2016", "28", "mythic", "nonfoil,foil,etched", "12.00",
     "40.00", "30.00", "WUBG", "Legendary Creature", "2016-11-11"),
    ("goblin", "Goblin Guide", "zen", "Zendikar", "126", "rare", "nonfoil,foil", "4.00", "20.00", None, "R",
     "Creature", "2009-10-02"),
]


def make_cards_db(path):
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE cards (id TEXT PRIMARY KEY, name TEXT, set_code TEXT, set_name TEXT,
                  collector_number TEXT, rarity TEXT, lang TEXT, released_at TEXT, finishes TEXT, promo INTEGER,
                  usd TEXT, usd_foil TEXT, usd_etched TEXT, eur TEXT, image_small TEXT, image_normal TEXT,
                  oracle_id TEXT, type_line TEXT, color_identity TEXT, legal_commander TEXT)""")
    for (cid, name, st, sn, num, rar, fin, usd, foil, etched, ci, tl, rel) in CARDS:
        db.execute("INSERT INTO cards VALUES (?,?,?,?,?,?,'en',?,?,0,?,?,?,NULL,NULL,NULL,?,?,?,'legal')",
                   (cid, name, st, sn, num, rar, rel, fin, usd, foil, etched, "o-" + name, tl, ci))
    db.commit()
    db.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tmp_coll_", dir=HERE)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._backup_dir = userdb.BACKUP_DIR
        userdb.BACKUP_DIR = os.path.join(self.tmp, "backups")
        self.addCleanup(setattr, userdb, "BACKUP_DIR", self._backup_dir)
        self.cards_path = os.path.join(self.tmp, "cards.db")
        make_cards_db(self.cards_path)
        self.db = UserDB(os.path.join(self.tmp, "user.db"))
        self.addCleanup(self.db.close)
        self.store = CollectionStore(self.db, self.cards_path)
        con = sqlite3.connect(self.cards_path)
        con.row_factory = sqlite3.Row
        self.addCleanup(con.close)
        self.lookup = lambda cid: (dict(r) if (r := con.execute("SELECT * FROM cards WHERE id = ?", (cid,)).fetchone())
                                   else None)

    def add(self, cid, finish="nonfoil", quantity=1, collection_id=None, condition="near_mint"):
        c = self.lookup(cid)
        price = {"foil": c["usd_foil"], "etched": c["usd_etched"]}.get(finish) or c["usd"]
        return self.db.add_card(cid, c["name"], c["set_code"], c["collector_number"], finish=finish, quantity=quantity,
                                collection_id=collection_id, condition=condition, market_price=float(price))

    def names(self, f=None, sort="value"):
        return [r["card_name"] for r in self.store.rows(self.store.query_ids(f or {}, sort))]


class ImportAndMergeTests(Base):
    def test_session_import_merges_duplicates_and_keeps_finishes_apart(self):
        entries = [dict(id="bolt-m11", finish="nonfoil"), dict(id="bolt-m11", finish="nonfoil"),
                   dict(id="bolt-m11", finish="foil"), dict(id="sol-c21", finish="nonfoil"),
                   dict(id="bolt-2xm", finish="nonfoil")]
        n = self.db.import_session(entries, self.lookup, mark=True)
        self.assertEqual(n, 5)
        self.assertTrue(all(e.get("in_collection") for e in entries))
        lots = {(i["scryfall_id"], i["finish"]): i["quantity"] for i in self.db.items()}
        self.assertEqual(lots, {("bolt-m11", "nonfoil"): 2, ("bolt-m11", "foil"): 1, ("sol-c21", "nonfoil"): 1,
                                ("bolt-2xm", "nonfoil"): 1})
        # importing the same printing again increases the quantity, no new row
        self.db.import_session([dict(id="bolt-m11", finish="nonfoil")], self.lookup)
        self.assertEqual(len(self.db.items()), 4)
        self.assertEqual(self.db.copies_owned("Lightning Bolt"), 5)
        log = self.db.conn.execute("SELECT source, cards FROM import_log ORDER BY id").fetchall()
        self.assertEqual([tuple(r) for r in log], [("scanner", 5), ("scanner", 1)])

    def test_failed_session_import_adds_nothing(self):
        entries = [dict(id="bolt-m11", finish="nonfoil"), dict(id="sol-c21", finish="shiny")]
        with self.assertRaises(ValueError):
            self.db.import_session(entries, self.lookup, mark=True)
        self.assertEqual(self.db.items(), [])
        self.assertFalse(any(e.get("in_collection") for e in entries))


class BrowseTests(Base):
    def setUp(self):
        super().setUp()
        self.trade = self.db.create_collection("Trade Binder", kind="binder")
        self.add("bolt-m11", quantity=4)
        self.add("bolt-m11", "foil")
        self.add("sol-c21", quantity=2)
        self.add("crypt")
        self.add("growth", quantity=10, condition="lightly_played")
        self.add("atraxa", "etched", collection_id=self.trade)
        self.add("goblin", collection_id=self.trade)

    def test_summary(self):
        s = self.store.summary({})
        self.assertEqual(s["cards"], 20)
        self.assertEqual(s["unique"], 6)  # 7 lots, Lightning Bolt twice
        self.assertAlmostEqual(s["value"], 4 * 1.5 + 9 + 2 * 2 + 180 + 10 * 0.3 + 30 + 4)
        main = self.store.summary({"collection_id": self.db.default_collection_id()})
        self.assertEqual(main["cards"], 18)

    def test_search(self):
        self.assertEqual(set(self.names({"search": "bolt"})), {"Lightning Bolt"})
        self.assertEqual(self.names({"search": "BOLT", "finish": "foil"}), ["Lightning Bolt"])
        self.assertEqual(self.names({"search": "c21"}), ["Sol Ring"])              # set code
        self.assertEqual(self.names({"search": "270"}), ["Mana Crypt"])            # collector number
        self.assertEqual(self.names({"search": "lightning m11"}), ["Lightning Bolt", "Lightning Bolt"])
        self.assertEqual(self.names({"search": "praetors'"}), ["Atraxa, Praetors' Voice"])
        self.assertEqual(self.names({"search": "nothing like this"}), [])
        self.assertEqual(self.names({"search": "100%"}), [])                       # LIKE wildcards are escaped

    def test_filters(self):
        main = self.db.default_collection_id()
        self.assertEqual(set(self.names({"collection_id": self.trade})), {"Atraxa, Praetors' Voice", "Goblin Guide"})
        self.assertEqual(len(self.names({"collection_id": main})), 5)
        self.assertEqual(self.names({"set_code": "C21"}), ["Sol Ring"])
        self.assertEqual(self.names({"finish": "etched"}), ["Atraxa, Praetors' Voice"])
        self.assertEqual(self.names({"condition": "lightly_played"}), ["Rampant Growth"])
        self.assertEqual(set(self.names({"rarity": "mythic"})), {"Mana Crypt", "Atraxa, Praetors' Voice"})
        self.assertEqual(set(self.names({"colour": "R"})), {"Lightning Bolt", "Goblin Guide"})
        self.assertEqual(set(self.names({"colour": "C"})), {"Sol Ring", "Mana Crypt"})
        self.assertEqual(self.names({"colour": "M"}), ["Atraxa, Praetors' Voice"])
        self.assertEqual(set(self.names({"type": "Creature"})), {"Atraxa, Praetors' Voice", "Goblin Guide"})
        self.assertEqual(self.names({"type": "Artifact", "rarity": "uncommon"}), ["Sol Ring"])
        self.assertEqual(self.store.set_codes(main)[0], "m10")  # most cards first

    def test_sorting(self):
        self.assertEqual(self.names(sort="value")[0], "Mana Crypt")            # 180
        self.assertEqual(self.names(sort="value")[1], "Atraxa, Praetors' Voice")  # 30
        self.assertEqual(self.names(sort="price")[:2], ["Mana Crypt", "Atraxa, Praetors' Voice"])
        self.assertEqual(self.names(sort="name")[0], "Atraxa, Praetors' Voice")
        self.assertEqual(self.names(sort="quantity")[0], "Rampant Growth")
        self.assertEqual([r["set_code"] for r in self.store.rows(self.store.query_ids({}, "set"))][:2], ["2xm", "c16"])
        newest = self.add("goblin", "foil")
        self.assertEqual(self.store.query_ids({}, "added")[0], newest)

    def test_stats(self):
        s = self.store.stats()
        self.assertEqual(s["foils"], 2)
        self.assertEqual(s["top"][0]["card_name"], "Mana Crypt")
        self.assertEqual(s["by_rarity"]["common"], 15)
        self.assertEqual(s["by_set"][0]["set_code"], "2xm")

    def test_refresh_prices(self):
        con = sqlite3.connect(self.cards_path)
        con.execute("UPDATE cards SET usd = '200.00', usd_foil = '260.00' WHERE id = 'crypt'")
        con.execute("UPDATE cards SET usd_etched = '33.00' WHERE id = 'atraxa'")
        con.commit()
        con.close()
        n = self.db.refresh_prices(self.cards_path)
        self.assertEqual(n, 7)
        prices = {(i["scryfall_id"], i["finish"]): i["market_price"] for i in self.db.items()}
        self.assertEqual(prices[("crypt", "nonfoil")], 200.0)
        self.assertEqual(prices[("atraxa", "etched")], 33.0)
        self.assertEqual(prices[("bolt-m11", "foil")], 9.0)

    def test_export(self):
        csv_path, txt_path = self.store.export(self.trade, os.path.join(self.tmp, "exports"), "Trade Binder")
        with open(csv_path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 2)
        atraxa = next(r for r in rows if r["Name"].startswith("Atraxa"))
        self.assertEqual((atraxa["Set code"], atraxa["Foil"], atraxa["Quantity"], atraxa["Scryfall ID"],
                          atraxa["Condition"]), ("C16", "etched", "1", "atraxa", "near_mint"))
        with open(txt_path, encoding="utf-8") as f:
            self.assertIn("1 Atraxa, Praetors' Voice (C16) 28 *E*", f.read())
        self.assertTrue(os.path.basename(csv_path).startswith("Trade_Binder_"))


class EditTests(Base):
    def test_quantity_condition_notes_purchase(self):
        a = self.add("bolt-m11", quantity=2)
        self.db.update_item(a, quantity=5)
        self.assertEqual(self.db.item(a)["quantity"], 5)
        self.db.update_item(a, notes="signed", purchase_price=12.5, purchase_currency="ZAR")
        item = self.db.item(a)
        self.assertEqual((item["notes"], item["purchase_price"], item["purchase_currency"]), ("signed", 12.5, "ZAR"))
        self.db.update_item(a, notes=None, purchase_price=None)
        self.assertIsNone(self.db.item(a)["notes"])
        self.assertIsNone(self.db.item(a)["purchase_price"])
        same = self.db.update_item(a, condition="lightly_played")
        self.assertEqual(same, a)
        self.assertEqual(self.db.item(a)["condition"], "lightly_played")
        with self.assertRaises(ValueError):
            self.db.update_item(a, condition="mangled")
        with self.assertRaises(ValueError):
            self.db.update_item(a, quantity=-1)

    def test_changing_condition_or_finish_merges_with_an_existing_lot(self):
        nm = self.add("bolt-m11", quantity=3)
        lp = self.add("bolt-m11", quantity=2, condition="lightly_played")
        self.db.update_item(lp, notes="from a trade")
        kept = self.db.update_item(lp, condition="near_mint")
        self.assertEqual(kept, nm)
        self.assertIsNone(self.db.item(lp))
        self.assertEqual(self.db.item(nm)["quantity"], 5)
        self.assertEqual(self.db.item(nm)["notes"], "from a trade")
        foil = self.add("bolt-m11", "foil")
        merged = self.db.update_item(foil, finish="nonfoil")
        self.assertEqual(merged, nm)
        self.assertEqual(self.db.item(nm)["quantity"], 6)

    def test_cannot_go_below_cards_used_in_decks(self):
        sol = self.add("sol-c21", quantity=3)
        deck = self.db.create_deck("Atraxa")
        self.db.add_deck_card(deck, "Sol Ring", quantity=2, collection_item_id=sol)
        self.db.set_quantity(sol, 2)
        for attempt in (lambda: self.db.set_quantity(sol, 1), lambda: self.db.remove_copies(sol, 1),
                        lambda: self.db.update_item(sol, quantity=0), lambda: self.db.delete_item(sol)):
            with self.assertRaises(UserDBError):
                attempt()
        self.assertEqual(self.db.item(sol)["quantity"], 2)
        # merging keeps the deck link pointing at the cards
        merged = self.db.update_item(sol, condition="lightly_played")
        self.assertEqual(self.db.availability(merged)["allocated"], 2)

    def test_delete_item(self):
        a = self.add("bolt-m11")
        self.db.delete_item(a)
        self.assertIsNone(self.db.item(a))


class CollectionManagementTests(Base):
    def test_create_rename_and_protect_main(self):
        box = self.db.create_collection("Box 1", kind="box")
        self.db.rename_collection(box, "Box One")
        self.assertEqual(self.db.collection_id("box one"), box)
        main = self.db.default_collection_id()
        self.db.rename_collection(main, "Everything")  # renaming is fine
        with self.assertRaises(UserDBError):
            self.db.delete_collection(main)
        with self.assertRaises(UserDBError):
            self.db.delete_collection(main, "delete")
        with self.assertRaises(ValueError):
            self.db.rename_collection(box, "   ")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.rename_collection(box, "everything")  # names are unique

    def test_delete_collection_with_cards_requires_a_choice(self):
        main = self.db.default_collection_id()
        trade = self.db.create_collection("Trade Binder")
        self.add("bolt-m11", quantity=2, collection_id=trade)
        self.add("bolt-m11", quantity=1)  # same lot exists in Main
        self.add("crypt", collection_id=trade)
        with self.assertRaises(UserDBError):
            self.db.delete_collection(trade)  # not empty, no choice made
        self.db.delete_collection(trade, "move", main)
        self.assertIsNone(self.db.collection_id("Trade Binder"))
        lots = {(i["scryfall_id"], i["finish"]): i["quantity"] for i in self.db.items(main)}
        self.assertEqual(lots, {("bolt-m11", "nonfoil"): 3, ("crypt", "nonfoil"): 1})

    def test_delete_collection_and_contents(self):
        box = self.db.create_collection("Box 1")
        self.add("growth", quantity=5, collection_id=box)
        self.db.delete_collection(box, "delete")
        self.assertEqual(self.db.copies_owned("Rampant Growth"), 0)
        empty = self.db.create_collection("Empty")
        self.db.delete_collection(empty)

    def test_cards_in_decks_block_deleting_their_collection(self):
        box = self.db.create_collection("Box 1")
        sol = self.add("sol-c21", collection_id=box)
        deck = self.db.create_deck("Deck")
        self.db.add_deck_card(deck, "Sol Ring", collection_item_id=sol)
        with self.assertRaises(UserDBError):
            self.db.delete_collection(box, "delete")
        self.assertEqual(self.db.copies_owned("Sol Ring"), 1)
        self.db.delete_collection(box, "move", self.db.default_collection_id())  # moving is fine
        self.assertEqual(self.db.decks_using("Sol Ring")[0]["collection_item_id"], sol)


class CsvImportTests(Base):
    def write_csv(self, rows):
        path = os.path.join(self.tmp, "import.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Name", "Set code", "Collector number", "Foil", "Quantity", "Scryfall ID", "Condition",
                        "Language", "Purchase price", "Purchase price currency"])
            w.writerows(rows)
        return path

    def test_preview_matching_and_commit(self):
        path = self.write_csv([
            ["Lightning Bolt", "M11", "149", "normal", "2", "bolt-m11", "near_mint", "en", "1.00", "USD"],  # by id
            ["Mana Crypt", "2XM", "270", "foil", "1", "", "lightly_played", "en", "", ""],               # set + number
            ["Sol Ring", "XXX", "999", "normal", "3", "", "", "", "", ""],                                 # name only
            ["Not A Real Card", "", "", "normal", "1", "", "", "", "", ""],                                # no match
            ["Lightning Bolt", "", "", "foil", "1", "not-a-real-id", "", "", "", ""],                       # bad id -> name
        ])
        parsed = self.store.parse_manabox(path)
        self.assertEqual([p["status"] for p in parsed], ["id", "set", "name", "none", "name"])
        self.assertEqual(parsed[1]["finish"], "foil")
        self.assertEqual(parsed[1]["condition"], "lightly_played")
        self.assertEqual(self.db.items(), [], "parsing must not write anything")
        copies, lots = self.store.commit_import(parsed, self.db.default_collection_id())
        self.assertEqual((copies, lots), (3, 2))  # uncertain name-only rows are left out
        self.assertEqual(self.db.copies_owned("Sol Ring"), 0)
        bolt = next(i for i in self.db.items() if i["scryfall_id"] == "bolt-m11")
        self.assertEqual((bolt["quantity"], bolt["purchase_price"]), (2, 1.0))
        copies, _ = self.store.commit_import(parsed, self.db.default_collection_id(), include_name_matches=True)
        self.assertEqual(copies, 2 + 1 + 3 + 1)
        self.assertEqual(self.db.copies_owned("Sol Ring"), 3)


class CurrencyTests(unittest.TestCase):
    def test_usd_and_zar_display(self):
        self.assertEqual(money(72.2, {}), "$72.20")
        self.assertEqual(money(72.2, {"currency": "USD", "usd_zar": 17.25}), "$72.20")
        self.assertEqual(money(72.2, {"currency": "ZAR", "usd_zar": 17.25}), "R1,245.45")
        self.assertEqual(money(1234567.891, {"currency": "ZAR", "usd_zar": 1.0}), "R1,234,567.89")
        self.assertEqual(money(None, {}), "-")
        self.assertEqual(display_currency({"currency": "ZAR"}), ("USD", 1.0))  # no rate yet -> stay in dollars
        self.assertEqual(parse_rate("17.25"), 17.25)
        self.assertEqual(parse_rate("R17,25"), 17.25)
        for bad in ("", "abc", "0", "5000"):
            with self.assertRaises(ValueError):
                parse_rate(bad)


class MigrationTests(unittest.TestCase):
    def test_phase1_database_upgrades_to_v2_without_losing_data(self):
        tmp = tempfile.mkdtemp(prefix="tmp_coll_mig_", dir=HERE)
        self.addCleanup(shutil.rmtree, tmp, True)
        old_backup = userdb.BACKUP_DIR
        userdb.BACKUP_DIR = os.path.join(tmp, "backups")
        self.addCleanup(setattr, userdb, "BACKUP_DIR", old_backup)
        path = os.path.join(tmp, "user.db")
        v1 = UserDB(path, migrations=userdb.MIGRATIONS[:1])  # a database made by the Phase 1 app
        v1.add_card("bolt-m11", "Lightning Bolt", "m11", "149", quantity=3)
        deck = v1.create_deck("Kept")
        self.assertEqual(v1.version, 1)
        v1.close()
        db = UserDB(path)
        self.addCleanup(db.close)
        self.assertEqual(db.version, userdb.LATEST_VERSION)
        self.assertEqual(db.applied, [m[0] for m in userdb.MIGRATIONS[1:]])
        self.assertEqual(db.copies_owned("Lightning Bolt"), 3)
        self.assertEqual(db.deck(deck)["name"], "Kept")
        self.assertTrue(db.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'import_log'").fetchone())
        backups = os.listdir(userdb.BACKUP_DIR)
        self.assertEqual(len(backups), 1)
        b = sqlite3.connect(os.path.join(userdb.BACKUP_DIR, backups[0]))
        self.assertEqual(b.execute("PRAGMA user_version").fetchone()[0], 1)
        b.close()


class FakeApp:
    """The bits of the scanner app the Collection screen uses."""

    def __init__(self, db):
        self.userdb, self.settings, self.dialog, self.toasts = db, {}, None, []

    def money(self, usd):
        return money(usd, self.settings)

    def toast(self, text, colour=None):
        self.toasts.append(text)

    def toast_text(self):
        return None

    def open_dialog(self, d):
        self.dialog = d

    def export_dir(self):
        return os.path.join(HERE, "tmp_never_used")

    def ask_open_file(self, *a):
        return None


class ScreenTests(Base):
    def screen(self):
        from collection_view import CollectionScreen
        app = FakeApp(self.db)
        return app, CollectionScreen(app, 1400, 610, store=self.store)

    def test_screen_actions(self):
        a = self.add("bolt-m11", quantity=1)
        self.add("crypt")
        app, sc = self.screen()
        img, hits = sc.render()
        self.assertEqual(img.shape[:2], (610, 1400))
        self.assertEqual(sc.summary_data["cards"], 2)
        # typing goes into the search box
        for ch in "bolt":
            sc.key(ord(ch))
        self.assertEqual(sc.search, "bolt")
        self.assertEqual(len(sc.ids), 1)
        sc.key(27)
        self.assertEqual(sc.search, "")
        # select a card and change its quantity with the buttons
        sc.selected = a
        sc.bump()
        sc.action("qty+")
        self.assertEqual(self.db.item(a)["quantity"], 2)
        sc.action("qty-")
        self.assertEqual(self.db.item(a)["quantity"], 1)
        sc.action("qty-")  # 1 -> 0 asks first, never removes silently
        self.assertIsNotNone(app.dialog)
        self.assertIsNotNone(self.db.item(a))
        app.dialog.finish(None)  # Cancel
        self.assertIsNotNone(self.db.item(a))
        sc.action("qty-")
        app.dialog.finish(True)  # Remove
        self.assertIsNone(self.db.item(a))
        # sorting via column header
        sc.set_sort("name")
        self.assertEqual(sc.sort, "name")
        sc.render()

    def test_main_collection_delete_is_refused_in_the_ui(self):
        app, sc = self.screen()
        sc.manage()
        self.assertNotIn("delete", [o[0] for o in app.dialog.options])
        sc.delete()
        self.assertIn("protected", app.dialog.title)


class ScannerAddToCollectionTests(unittest.TestCase):
    """The scanner's Add to Collection button, with the real card database."""

    def test_add_to_collection_then_choose_whether_to_clear(self):
        import cv2  # noqa: F401
        import scanner
        from test_background import ZONE, fake_open_camera
        tmp = tempfile.mkdtemp(prefix="tmp_coll_scan_", dir=HERE)
        self.addCleanup(shutil.rmtree, tmp, True)
        saved = {k: getattr(scanner, k) for k in ("SETTINGS_PATH", "SESSION_PATH", "BACKGROUND_DIR", "USER_DB_PATH",
                                                  "EXPORT_DIR", "DEBUG_DIR")}
        self.addCleanup(lambda: [setattr(scanner, k, v) for k, v in saved.items()])
        orig_open = scanner.Scanner.open_camera
        scanner.Scanner.open_camera = fake_open_camera
        self.addCleanup(setattr, scanner.Scanner, "open_camera", orig_open)
        for k, name in (("SETTINGS_PATH", "settings.json"), ("SESSION_PATH", "session.json"),
                        ("BACKGROUND_DIR", "bg"), ("USER_DB_PATH", "user.db"), ("EXPORT_DIR", "exports"),
                        ("DEBUG_DIR", "debug")):
            setattr(scanner, k, os.path.join(tmp, name))
        with open(scanner.SETTINGS_PATH, "w") as f:
            json.dump({"camera": 2, "zone": ZONE}, f)
        from carddb import CardDB
        cdb = CardDB()
        ids = [r[0] for r in cdb.db.execute("SELECT id FROM cards WHERE lang = 'en' AND usd IS NOT NULL LIMIT 3")]
        with open(scanner.SESSION_PATH, "w") as f:
            json.dump([dict(id=ids[0], finish="nonfoil"), dict(id=ids[0], finish="nonfoil"),
                       dict(id=ids[1], finish="nonfoil"), dict(id=ids[2], finish="nonfoil")], f)
        s = scanner.Scanner(type("A", (), {"camera": None})())
        self.addCleanup(lambda: s.userdb and s.userdb.close())
        self.assertEqual(len(s.pending_entries()), 4)
        s.add_to_collection()
        self.assertIn("4 cards added to Main Collection", s.status)
        self.assertIsNotNone(s.dialog)
        self.assertEqual(s.userdb.copies_owned(cdb.by_id(ids[0])["name"]), 2)
        s.dialog.finish(False)  # Keep list
        self.assertEqual(len(s.entries), 4)
        self.assertEqual(s.pending_entries(), [])
        s.dialog = None
        s.add_to_collection()  # nothing new: no duplicates, no dialog
        self.assertIsNone(s.dialog)
        self.assertEqual(sum(i["quantity"] for i in s.userdb.items()), 4)
        with open(scanner.SESSION_PATH) as f:
            self.assertTrue(all(e.get("in_collection") for e in json.load(f)), "the 'already added' marks must be saved")
        s.entries.append(dict(id=ids[1], finish="nonfoil", alts=[ids[1]], alt_idx=0))
        s.add_to_collection()
        self.assertIn("1 card added", s.status)
        s.dialog.finish(True)  # Clear list
        self.assertEqual(s.entries, [])
        self.assertTrue(os.listdir(scanner.EXPORT_DIR), "a copy of the list is exported before clearing")
        self.assertEqual(sum(i["quantity"] for i in s.userdb.items()), 5)
        s.switch_tab("collection")
        self.assertEqual(s.collection_screen.summary_data["cards"], 5)
        s.switch_tab("scanner")


class PerformanceTests(unittest.TestCase):
    """50,000 cards: queries and drawing must stay quick."""

    def test_fifty_thousand_cards(self):
        tmp = tempfile.mkdtemp(prefix="tmp_coll_perf_", dir=HERE)
        self.addCleanup(shutil.rmtree, tmp, True)
        real_cards = os.path.join(ROOT, "data", "cards.db")
        if not os.path.exists(real_cards):
            self.skipTest("no cards.db")
        db = UserDB(os.path.join(tmp, "user.db"))
        self.addCleanup(db.close)
        src = sqlite3.connect(real_cards)
        printings = src.execute("SELECT id, name, set_code, collector_number, usd FROM cards "
                                "WHERE lang = 'en' ORDER BY RANDOM() LIMIT 25000").fetchall()
        src.close()
        box = db.create_collection("Box 1")
        with db.transaction() as c:
            for i, (cid, name, st, num, usd) in enumerate(printings):
                for finish in ("nonfoil", "foil"):
                    db.add_card(cid, name, st, num, finish=finish, quantity=1 + i % 3,
                                collection_id=box if i % 4 == 0 else None,
                                market_price=float(usd) if usd else None, _conn=c)
        store = CollectionStore(db, real_cards)
        self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM collection_items").fetchone()[0], 50000)
        timings = {}

        def timed(label, fn):
            t = time.perf_counter()
            out = fn()
            timings[label] = (time.perf_counter() - t) * 1000
            return out

        ids = timed("list by value", lambda: store.query_ids({}, "value"))
        self.assertEqual(len(ids), 50000)
        timed("search 'dragon'", lambda: store.query_ids({"search": "dragon"}, "value"))
        timed("filter mythic + foil, by name", lambda: store.query_ids({"rarity": "mythic", "finish": "foil"}, "name"))
        timed("summary", lambda: store.summary({}))
        timed("one screen of rows", lambda: store.rows(ids[25000:25013]))
        from collection_view import CollectionScreen
        app = FakeApp(db)
        sc = timed("open Collection tab", lambda: CollectionScreen(app, 1400, 610, store=store))
        sc.collection_id = None
        sc.refresh()
        timed("draw screen", sc.render)
        sc.scroll_to(30000)
        timed("scroll + draw", sc.render)
        timed("type a letter (search + draw)", lambda: (sc.key(ord("a")), sc.render()))
        timed("refresh prices", lambda: db.refresh_prices(real_cards))
        for k, v in timings.items():
            print(f"    {k:32} {v:7.0f} ms")
        for k in ("search 'dragon'", "one screen of rows", "scroll + draw", "type a letter (search + draw)"):
            self.assertLess(timings[k], 400, f"{k} is too slow")


if __name__ == "__main__":
    unittest.main(verbosity=2)
