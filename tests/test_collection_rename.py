"""Renaming collections - only the display name changes.

    python -m unittest tests.test_collection_rename -v
"""
import csv
import os
import shutil
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import userdb  # noqa: E402
from userdb import UserDB, UserDBError  # noqa: E402

CARDS_DB = os.path.join(ROOT, "data", "cards.db")


class RenameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tmp_rename_", dir=HERE)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        old = userdb.BACKUP_DIR
        userdb.BACKUP_DIR = os.path.join(self.tmp, "b")
        self.addCleanup(setattr, userdb, "BACKUP_DIR", old)
        self.path = os.path.join(self.tmp, "u.db")
        self.db = UserDB(self.path)
        self.addCleanup(lambda: self.db.close())

    def add(self, cid=None, n=2, sid="sid-1", name="Sol Ring"):
        return self.db.add_card(sid, name, "c21", "263", quantity=n, collection_id=cid)

    def snapshot(self):
        return [tuple(r) for r in self.db.conn.execute(
            "SELECT id, collection_id, scryfall_id, quantity, finish, condition FROM collection_items ORDER BY id")]

    def test_rename_normal_collection_keeps_everything(self):
        rares = self.db.create_collection("rares")
        self.add(rares, 3)
        before = self.snapshot()
        self.assertEqual(self.db.rename_collection(rares, "Rare Cards"), "Rare Cards")
        self.assertEqual(self.snapshot(), before)  # same ids, collection ids, quantities
        names = {c["id"]: c["name"] for c in self.db.collections()}
        self.assertEqual(names[rares], "Rare Cards")
        self.assertEqual(len(names), 2)  # nothing created or removed
        self.assertEqual(self.db.collection_counts(rares), {"lots": 1, "copies": 3})

    def test_rename_main_collection_keeps_default_behaviour(self):
        main = self.db.default_collection_id()
        self.add(None, 1)
        self.db.rename_collection(main, "John-Luther's Collection")
        self.assertEqual(self.db.default_collection_id(), main)  # found by flag, not by name
        col = next(c for c in self.db.collections() if c["id"] == main)
        self.assertEqual((col["name"], col["is_default"]), ("John-Luther's Collection", 1))
        lot = self.add(None, 1, sid="sid-2", name="Arcane Signet")  # default destination still works
        self.assertEqual(self.db.item(lot)["collection_id"], main)
        added = self.db.import_session([dict(id="sid-3", finish="nonfoil")],
                                       lambda i: dict(name="Mind Stone", set_code="c21", collector_number="1",
                                                      lang="en", usd="1"))
        self.assertEqual(added, 1)
        self.assertEqual(self.db.item(max(r[0] for r in self.snapshot()))["collection_id"], main)
        with self.assertRaises(UserDBError):
            self.db.delete_collection(main)  # still protected

    def test_names_are_trimmed_and_blank_is_rejected(self):
        c = self.db.create_collection("Box 1")
        self.assertEqual(self.db.rename_collection(c, "   Trade    Binder  "), "Trade Binder")
        for bad in ("", "   ", "\t \n"):
            with self.assertRaises(UserDBError):
                self.db.rename_collection(c, bad)
        self.assertEqual(next(x["name"] for x in self.db.collections() if x["id"] == c), "Trade Binder")
        with self.assertRaises(UserDBError):
            self.db.create_collection("   ")

    def test_duplicate_names_rejected_with_a_clear_message(self):
        a = self.db.create_collection("Trade Binder")
        b = self.db.create_collection("Box 1")
        with self.assertRaises(UserDBError) as e:
            self.db.rename_collection(b, "trade binder")  # case doesn't matter
        self.assertIn('already a collection called "Trade Binder"', str(e.exception))
        with self.assertRaises(UserDBError):
            self.db.create_collection("TRADE BINDER")
        self.assertEqual(self.db.rename_collection(a, "Trade binder"), "Trade binder")  # its own name, new case: fine
        with self.assertRaises(UserDBError):
            self.db.rename_collection(9999, "Ghost")

    def test_deck_links_survive(self):
        binder = self.db.create_collection("Binder")
        lot = self.add(binder, 1)
        deck = self.db.create_deck("Deck")
        dc = self.db.add_deck_card(deck, "Sol Ring", scryfall_id="sid-1", collection_item_id=lot)
        before = self.db.conn.execute("SELECT * FROM deck_card_ownership WHERE deck_card_id = ?", (dc,)).fetchone()
        self.db.rename_collection(binder, "Commander Binder")
        after = self.db.conn.execute("SELECT * FROM deck_card_ownership WHERE deck_card_id = ?", (dc,)).fetchone()
        self.assertEqual(tuple(before), tuple(after))
        self.assertEqual(self.db.deck_card(dc)["collection_item_id"], lot)
        self.assertEqual(self.db.availability(lot)["allocated"], 1)

    def test_restart_keeps_the_new_name(self):
        c = self.db.create_collection("rares")
        self.db.rename_collection(c, "Rare Cards")
        self.db.rename_collection(self.db.default_collection_id(), "My Cards")
        self.db.close()
        self.db = UserDB(self.path)
        names = {c["name"] for c in self.db.collections()}
        self.assertEqual(names, {"Rare Cards", "My Cards"})
        self.assertEqual(next(c["name"] for c in self.db.collections() if c["is_default"]), "My Cards")

    def test_scanner_destination_shows_the_new_name_straight_away(self):
        import scanner
        fake = types.SimpleNamespace(userdb=self.db, settings={})
        fake.destination_id = lambda: scanner.Scanner.destination_id(fake)
        name = lambda: scanner.Scanner.destination_name(fake)  # noqa: E731
        self.assertEqual(name(), "Main Collection")
        self.db.rename_collection(self.db.default_collection_id(), "My Cards")
        self.assertEqual(name(), "My Cards")
        binder = self.db.create_collection("Binder")
        fake.settings["destination_collection_id"] = binder
        self.db.rename_collection(binder, "Trade Binder")
        self.assertEqual(name(), "Trade Binder")
        self.assertEqual(fake.destination_id(), binder)

    @unittest.skipUnless(os.path.exists(CARDS_DB), "no cards.db")
    def test_export_and_statistics_after_rename(self):
        from collection import CollectionStore
        st = CollectionStore(self.db)
        c = self.db.create_collection("rares")
        self.add(c, 2)
        self.db.rename_collection(c, "Rare Cards")
        self.assertEqual(st.summary(dict(collection_id=c))["cards"], 2)
        out = st.export(c, os.path.join(self.tmp, "exports"))
        files = out if isinstance(out, (list, tuple)) else [out]
        csv_file = next(f for f in files if str(f).endswith(".csv"))
        with open(csv_file, encoding="utf-8") as f:
            rows = list(csv.reader(f))
        self.assertGreaterEqual(len(rows), 2)


@unittest.skipUnless(os.path.exists(CARDS_DB), "no cards.db")
class RenameScreenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tmp_rename2_", dir=HERE)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        old = userdb.BACKUP_DIR
        userdb.BACKUP_DIR = os.path.join(self.tmp, "b")
        self.addCleanup(setattr, userdb, "BACKUP_DIR", old)
        self.db = UserDB(os.path.join(self.tmp, "u.db"))
        self.addCleanup(self.db.close)
        from collection_view import CollectionScreen
        self.toasts = []
        app = types.SimpleNamespace(userdb=self.db, settings={"currency": "USD"}, dialog=None)
        app.money = lambda u: f"${u or 0:.2f}"
        app.toast = lambda t, *a: self.toasts.append(t)
        app.toast_text = lambda: None
        app.open_dialog = lambda d: setattr(app, "dialog", d)
        self.app = app
        self.sc = CollectionScreen(app, 1400, 610)

    def type_and_save(self, text):
        d = self.app.dialog
        d.text = text
        d.finish(True)

    def test_manage_rename_updates_the_screen(self):
        self.sc.collection_id = self.db.default_collection_id()
        self.sc.manage()
        self.app.dialog.finish("rename")
        self.assertIn("Rename Main Collection", self.app.dialog.title)
        self.type_and_save("  John-Luther's   Collection ")
        self.assertEqual(self.sc._collection_name(), "John-Luther's Collection")
        self.assertIn('Renamed "Main Collection" to "John-Luther\'s Collection"', self.toasts[-1])
        self.sc.render()  # header / dropdown redraw with the new name

    def test_duplicate_reopens_with_the_reason_and_the_typed_text(self):
        self.db.create_collection("Trade Binder")
        self.sc.collection_id = self.db.default_collection_id()
        self.sc.rename()
        self.type_and_save("trade binder")
        d = self.app.dialog
        self.assertFalse(d.done)  # a new dialog asking again
        self.assertIn("already a collection", " ".join(d.message))
        self.assertEqual(d.text, "trade binder")
        self.assertEqual(self.sc._collection_name(), "Main Collection")

    def test_rename_from_all_collections_view(self):
        rares = self.db.create_collection("rares")
        self.sc.collection_id = None  # "All collections"
        self.sc.manage()
        self.assertIn("rename_pick", [o[0] for o in self.app.dialog.options])
        self.app.dialog.finish("rename_pick")
        self.app.dialog.finish(rares)
        self.type_and_save("Rare Cards")
        self.assertEqual(next(c["name"] for c in self.db.collections() if c["id"] == rares), "Rare Cards")


if __name__ == "__main__":
    unittest.main()
