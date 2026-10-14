"""Correcting a collection lot's printing / finish (Collection card detail panel):
finishes offered, exact prices, what is kept, deck links, merging identical lots, and the
picker / panel workflow.

    python -m unittest tests.test_printing -v

Uses the real Scryfall cards.db (read-only) and a temporary user database.
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import userdb  # noqa: E402
from collection import CollectionStore  # noqa: E402
from currency import money  # noqa: E402
from decks import DeckStore  # noqa: E402
from userdb import UserDB  # noqa: E402

CARDS_DB = os.path.join(ROOT, "data", "cards.db")


def find(where):
    con = sqlite3.connect(CARDS_DB)
    con.row_factory = sqlite3.Row
    r = con.execute(f"SELECT * FROM cards WHERE name = 'Sol Ring' AND {where} ORDER BY id LIMIT 1").fetchone()
    con.close()
    return dict(r) if r else None


@unittest.skipUnless(os.path.exists(CARDS_DB), "no data/cards.db")
class PrintingBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        en = "lang = 'en' AND "
        cls.both = find(en + "finishes = 'nonfoil,foil' AND usd IS NOT NULL AND usd_foil IS NOT NULL "
                             "AND CAST(usd AS REAL) != CAST(usd_foil AS REAL)")
        cls.both2 = find(en + f"finishes = 'nonfoil,foil' AND usd IS NOT NULL AND id != '{cls.both['id']}'")
        cls.nonfoil_only = find(en + "finishes = 'nonfoil' AND usd IS NOT NULL")
        cls.etched = find("finishes LIKE '%etched%'")
        cls.other_lang = find("lang != 'en'")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tmp_printing_", dir=HERE)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        old = userdb.BACKUP_DIR
        userdb.BACKUP_DIR = os.path.join(self.tmp, "backups")
        self.addCleanup(setattr, userdb, "BACKUP_DIR", old)
        self.db = UserDB(os.path.join(self.tmp, "user.db"))
        self.addCleanup(self.db.close)
        self.store = CollectionStore(self.db)
        self.decks = DeckStore(self.db)

    def add(self, card, finish="nonfoil", quantity=1, **kw):
        price = self.store.price_of(card, finish)
        return self.db.add_card(card["id"], card["name"], card["set_code"], card["collector_number"], finish=finish,
                                quantity=quantity, market_price=price, oracle_id=card["oracle_id"], **kw)

    def item(self, lot):
        return self.db.item(lot)

    def link_deck(self, lot, name="Deck A", quantity=1):
        it = self.item(lot)
        deck = self.db.create_deck(name)
        dc = self.db.add_deck_card(deck, it["card_name"], quantity=quantity, scryfall_id=it["scryfall_id"],
                                   finish=it["finish"], collection_item_id=lot)
        return deck, dc


class FinishTests(PrintingBase):
    def test_foil_to_nonfoil_and_back(self):
        lot = self.add(self.both, "foil", quantity=2, condition="lightly_played", purchase_price=3.5,
                       purchase_currency="USD", notes="from a trade")
        before = self.item(lot)
        same = self.store.change_finish(lot, "nonfoil")
        self.assertEqual(same, lot)  # same physical lot, not delete + add
        it = self.item(lot)
        self.assertEqual(it["finish"], "nonfoil")
        self.assertAlmostEqual(it["market_price"], float(self.both["usd"]))
        self.store.change_finish(lot, "foil")
        it = self.item(lot)
        self.assertAlmostEqual(it["market_price"], float(self.both["usd_foil"]))
        for k in ("quantity", "condition", "purchase_price", "purchase_currency", "notes", "collection_id",
                  "added_at", "scryfall_id", "language"):
            self.assertEqual(it[k], before[k], k)
        self.assertIsNotNone(it["price_updated_at"])

    def test_only_valid_finishes(self):
        self.assertEqual(self.store.finishes_of(self.both), ["nonfoil", "foil"])
        self.assertEqual(self.store.finishes_of(self.nonfoil_only), ["nonfoil"])
        self.assertIn("etched", self.store.finishes_of(self.etched))
        lot = self.add(self.nonfoil_only)
        for bad in ("foil", "etched"):
            with self.assertRaises(ValueError):
                self.store.change_finish(lot, bad)
        self.assertEqual(self.item(lot)["finish"], "nonfoil")
        with self.assertRaises(ValueError):
            self.store.change_printing(lot, self.nonfoil_only["id"], "foil")

    def test_price_with_no_price_for_that_finish_is_empty_not_another_finish(self):
        card = self.etched
        lot = self.add(card, self.store.finishes_of(card)[0])
        for f in self.store.finishes_of(card):
            self.store.change_finish(lot, f) if self.item(lot)["finish"] != f else None
            self.assertEqual(self.item(lot)["market_price"], self.store.price_of(card, f))

    def test_collection_total_and_zar_display_follow(self):
        lot = self.add(self.both, "nonfoil", quantity=3)
        total = lambda: self.store.summary(dict(collection_id=None))["value"]
        self.assertAlmostEqual(total(), 3 * float(self.both["usd"]), places=2)
        self.store.change_finish(lot, "foil")
        self.assertAlmostEqual(total(), 3 * float(self.both["usd_foil"]), places=2)
        settings = {"currency": "ZAR", "usd_zar": 18.0}
        self.assertEqual(money(self.item(lot)["market_price"], settings), f"R{float(self.both['usd_foil']) * 18:,.2f}")
        self.assertEqual(money(self.item(lot)["market_price"], {"currency": "USD"}),
                         f"${float(self.both['usd_foil']):,.2f}")


class PrintingTests(PrintingBase):
    def test_printings_found_by_oracle_id_not_name(self):
        lot = self.add(self.both)
        self.db.conn.execute("UPDATE collection_items SET card_name = 'Something Else' WHERE id = ?", (lot,))
        prints = self.store.printings_for(self.item(lot))
        self.assertGreater(len(prints), 20)
        self.assertEqual({p["oracle_id"] for p in prints}, {self.both["oracle_id"]})
        # no oracle id on the lot: taken from its current printing
        self.db.conn.execute("UPDATE collection_items SET oracle_id = NULL WHERE id = ?", (lot,))
        self.assertEqual(len(self.store.printings_for(self.item(lot))), len(prints))
        self.assertEqual(prints[0]["lang"], "en")

    def test_change_to_another_printing_keeps_the_physical_lot(self):
        lot = self.add(self.both, "foil", quantity=2, condition="moderately_played", purchase_price=40,
                       purchase_currency="ZAR", notes="binder 3")
        before = self.item(lot)
        new = self.store.change_printing(lot, self.both2["id"])
        self.assertEqual(new, lot)
        it = self.item(lot)
        self.assertEqual(it["scryfall_id"], self.both2["id"])
        self.assertEqual(it["set_code"], self.both2["set_code"].lower())
        self.assertEqual(it["collector_number"], self.both2["collector_number"])
        self.assertEqual(it["oracle_id"], self.both2["oracle_id"])
        self.assertEqual(it["card_name"], "Sol Ring")
        self.assertEqual(it["finish"], "foil")  # kept: the new printing has foil
        self.assertEqual(it["market_price"], self.store.price_of(self.both2, "foil"))
        for k in ("quantity", "condition", "purchase_price", "purchase_currency", "notes", "collection_id", "added_at"):
            self.assertEqual(it[k], before[k], k)
        row = self.store.rows([lot])[0]  # what the panel shows: rarity, set name, image, finishes
        self.assertEqual(row["set_name"], self.both2["set_name"])
        self.assertEqual(row["image_normal"], self.both2["image_normal"])
        self.assertEqual(row["available_finishes"], self.both2["finishes"])

    def test_finish_falls_back_when_new_printing_lacks_it(self):
        lot = self.add(self.both, "foil")
        self.store.change_printing(lot, self.nonfoil_only["id"])
        it = self.item(lot)
        self.assertEqual(it["finish"], "nonfoil")
        self.assertAlmostEqual(it["market_price"], float(self.nonfoil_only["usd"]))

    def test_language_follows_the_printing(self):
        lot = self.add(self.both)
        fin = self.store.finishes_of(self.other_lang)[0]
        self.store.change_printing(lot, self.other_lang["id"], fin)
        self.assertEqual(self.item(lot)["language"], self.other_lang["lang"])


class DeckLinkTests(PrintingBase):
    def ownership(self, dc):
        return self.db.conn.execute("SELECT ownership FROM deck_card_ownership WHERE deck_card_id = ?", (dc,)).fetchone()[0]

    def test_finish_change_keeps_deck_link(self):
        lot = self.add(self.both, "foil")
        deck, dc = self.link_deck(lot)
        self.assertEqual(self.ownership(dc), "exact")
        self.store.change_finish(lot, "nonfoil")
        d = self.db.deck_card(dc)
        self.assertEqual(d["collection_item_id"], lot)
        self.assertEqual(d["finish"], "nonfoil")
        self.assertEqual(self.ownership(dc), "exact")
        self.assertEqual(self.db.availability(lot)["allocated"], 1)

    def test_printing_change_keeps_deck_link(self):
        lot = self.add(self.both, "nonfoil")
        deck, dc = self.link_deck(lot)
        self.store.change_printing(lot, self.both2["id"])
        d = self.db.deck_card(dc)
        self.assertEqual((d["collection_item_id"], d["scryfall_id"]), (lot, self.both2["id"]))
        self.assertEqual(self.ownership(dc), "exact")

    def test_merge_keeps_deck_links(self):
        plain = self.add(self.both, "nonfoil")
        foil = self.add(self.both, "foil")
        deck, dc = self.link_deck(foil)
        check = self.store.merge_check(foil, finish="nonfoil")
        self.assertEqual(check["target"]["id"], plain)
        self.assertEqual(check["conflicts"], [])
        merged = self.store.change_finish(foil, "nonfoil")
        self.assertEqual(merged, plain)
        self.assertIsNone(self.item(foil))
        self.assertEqual(self.item(plain)["quantity"], 2)
        self.assertEqual(self.db.deck_card(dc)["collection_item_id"], plain)
        self.assertEqual(self.db.availability(plain)["allocated"], 1)
        self.assertEqual(self.ownership(dc), "exact")

    def test_other_decks_links_untouched(self):
        a = self.add(self.both, "foil")
        b = self.add(self.both2, "nonfoil")
        _, dca = self.link_deck(a, "Deck A")
        _, dcb = self.link_deck(b, "Deck B")
        self.store.change_finish(a, "nonfoil")
        self.assertEqual(self.db.deck_card(dcb)["collection_item_id"], b)
        self.assertEqual(self.db.deck_card(dcb)["scryfall_id"], self.both2["id"])


class MergeTests(PrintingBase):
    def test_identical_lots_merge(self):
        a = self.add(self.both, "nonfoil")
        b = self.add(self.both2, "nonfoil")
        merged = self.store.change_printing(b, self.both["id"], "nonfoil")
        self.assertEqual(merged, a)
        self.assertEqual(self.item(a)["quantity"], 2)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM collection_items").fetchone()[0], 1)

    def test_different_condition_or_collection_does_not_merge(self):
        a = self.add(self.both, "nonfoil")
        b = self.add(self.both2, "nonfoil", condition="lightly_played")
        binder = self.db.create_collection("Binder")
        c = self.add(self.both2, "nonfoil", collection_id=binder)
        self.assertIsNone(self.store.merge_check(b, self.both["id"], "nonfoil"))
        self.assertIsNone(self.store.merge_check(c, self.both["id"], "nonfoil"))
        self.assertEqual(self.store.change_printing(b, self.both["id"], "nonfoil"), b)
        self.assertEqual(self.store.change_printing(c, self.both["id"], "nonfoil"), c)
        self.assertEqual(self.item(a)["quantity"], 1)

    def test_merge_preserves_purchase_prices_and_notes(self):
        a = self.add(self.both, "nonfoil", purchase_price=2.0, purchase_currency="USD", notes="signed")
        b = self.add(self.both, "foil", purchase_price=4.0, purchase_currency="USD", notes="from Sam")
        check = self.store.merge_check(b, finish="nonfoil")
        self.assertEqual(check["conflicts"], ["purchase price", "notes"])
        self.store.change_finish(b, "nonfoil")
        it = self.item(a)
        self.assertEqual(it["quantity"], 2)
        self.assertAlmostEqual(it["purchase_price"], 3.0)
        self.assertIn("signed", it["notes"])
        self.assertIn("from Sam", it["notes"])
        self.assertIn("2.00", it["notes"])
        self.assertIn("4.00", it["notes"])

    def test_merge_fills_missing_purchase_price(self):
        a = self.add(self.both, "nonfoil")
        b = self.add(self.both, "foil", purchase_price=4.0, purchase_currency="ZAR")
        self.assertEqual(self.store.merge_check(b, finish="nonfoil")["conflicts"], [])
        self.store.change_finish(b, "nonfoil")
        self.assertEqual((self.item(a)["purchase_price"], self.item(a)["purchase_currency"]), (4.0, "ZAR"))


class FakeApp:
    def __init__(self, db):
        self.userdb, self.settings, self.dialog, self.toasts = db, {"currency": "USD"}, None, []

    def money(self, usd):
        return money(usd, self.settings)

    def toast(self, text, colour=None):
        self.toasts.append(text)

    def toast_text(self):
        return None

    def open_dialog(self, d):
        self.dialog = d


class PanelWorkflowTests(PrintingBase):
    def screen(self):
        from collection_view import CollectionScreen
        app = FakeApp(self.db)
        return app, CollectionScreen(app, 1400, 610, store=self.store)

    def test_change_printing_via_picker(self):
        from collection_view import PrintingPicker
        lot = self.add(self.both, "foil", quantity=2)
        app, sc = self.screen()
        sc.selected = lot
        sc.render()
        sc.action("printing")
        picker = app.dialog
        self.assertIsInstance(picker, PrintingPicker)
        self.assertEqual(picker.sel, self.both["id"])  # starts on the current printing
        picker.draw(__import__("numpy").zeros((650, 1400, 3), "uint8"))
        for ch in f"{self.both2['set_code']} {self.both2['collector_number']}":
            picker.key(ord(ch))
        self.assertTrue(any(c["id"] == self.both2["id"] for c in picker.shown))
        self.assertLess(len(picker.shown), len(picker.all))
        picker.select(self.both2["id"])
        picker.finish = "nonfoil"
        picker.use()
        it = self.item(lot)
        self.assertEqual((it["scryfall_id"], it["finish"], it["quantity"]), (self.both2["id"], "nonfoil", 2))
        self.assertEqual(sc.selected, lot)
        sc.render()

    def test_filter_with_no_match_changes_nothing(self):
        lot = self.add(self.both, "foil")
        app, sc = self.screen()
        sc.selected = lot
        sc.action("printing")
        picker = app.dialog
        for ch in "zzzz999":
            picker.key(ord(ch))
        self.assertEqual(picker.shown, [])
        picker.key(13)
        self.assertEqual(self.item(lot)["scryfall_id"], self.both["id"])

    def test_finish_button_and_merge_question(self):
        a = self.add(self.both, "nonfoil", purchase_price=1.0, purchase_currency="USD")
        b = self.add(self.both, "foil", purchase_price=9.0, purchase_currency="USD")
        app, sc = self.screen()
        sc.selected = b
        sc.action(("finish", "nonfoil"))
        self.assertIsNotNone(app.dialog)  # prices differ: asks first
        self.assertIsNotNone(self.item(b))
        app.dialog.finish(True)
        self.assertIsNone(self.item(b))
        self.assertEqual(self.item(a)["quantity"], 2)
        self.assertEqual(sc.selected, a)
        self.assertIn("Merged", app.toasts[-1])

    def test_finish_switch_is_immediate(self):
        lot = self.add(self.both, "nonfoil")
        app, sc = self.screen()
        sc.selected = lot
        sc.action(("finish", "foil"))
        self.assertIsNone(app.dialog)
        self.assertEqual(self.item(lot)["finish"], "foil")


if __name__ == "__main__":
    unittest.main()
