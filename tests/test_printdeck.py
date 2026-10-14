"""Print Deck: a Word document of real-size cards (63 x 88 mm), nine per A4 page.

    python -m unittest tests.test_printdeck -v
"""
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import printdeck  # noqa: E402
import userdb  # noqa: E402
from decks import DeckStore  # noqa: E402
from test_analyser_understanding import has_cards  # noqa: E402
from userdb import UserDB  # noqa: E402

# Talrand + 98 different cards + 1 double-faced card... and 30 Islands (a quantity > 1)
SPELLS = ["Sol Ring", "Arcane Signet", "Mind Stone", "Thought Vessel", "Counterspell", "Brainstorm", "Ponder",
          "Preordain", "Opt", "Consider", "Arcane Denial", "Mana Drain", "Swan Song", "Negate", "Cyclonic Rift",
          "Rhystic Study", "Mystic Remora", "Reality Shift", "Pongify", "Rapid Hybridization", "Snap", "Frantic Search",
          "Fact or Fiction", "Mystic Confluence", "Pull from Tomorrow", "Blue Sun's Zenith", "Dig Through Time",
          "Treasure Cruise", "Mizzix's Mastery", "Ghostly Flicker", "Narset's Reversal", "Delay", "Mana Leak",
          "Into the Roil", "Capsize", "Aetherflux Reservoir", "Sapphire Medallion", "Baral, Chief of Compliance",
          "Archmage Emeritus", "Murmuring Mystic", "Giant Octopus", "Air Elemental", "Coral Eel", "Spell Pierce",
          "Dispel", "Flusterstorm", "Force of Will", "Pact of Negation", "Fierce Guardianship", "Gitaxian Probe",
          "Impulse", "Anticipate", "Think Twice", "Deep Analysis", "Lat-Nam's Legacy", "Chart a Course",
          "Distant Melody", "Echo of Eons", "Timetwister", "Windfall", "Jace's Erasure", "Psychosis Crawler",
          "Paradox Haze", "Talrand's Invocation", "Metallurgic Summonings",
          "Thing in the Ice // Awoken Horror",  # double-faced
          "Commit // Memory",  # split card: one printed face
          "High Tide", "Cryptic Command"]


def fake_jpeg(path, colour=(60, 120, 200)):
    img = np.zeros((936, 672, 3), np.uint8)
    img[:] = colour
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cv2.imwrite(path, img)
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tmp_print_", dir=HERE)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        old = userdb.BACKUP_DIR
        userdb.BACKUP_DIR = os.path.join(self.tmp, "b")
        self.addCleanup(setattr, userdb, "BACKUP_DIR", old)
        self.db = UserDB(os.path.join(self.tmp, "u.db"))
        self.addCleanup(self.db.close)
        self.st = DeckStore(self.db)
        spells = list(dict.fromkeys(SPELLS))
        n_spells = 99 - 30
        self.deck = self.st.create_deck("Talrand Spells")
        text = "Commander\n1 Talrand, Sky Summoner\nDeck\n" + "\n".join("1 " + c for c in spells[:n_spells]) + \
            "\n30 Island"
        parsed = self.st.parse_decklist(text)
        self.assertEqual([r["name"] for r in parsed if not r["card"]], [])
        self.st.commit_import(self.deck, parsed)
        self.requested = []

    def fetch(self, it, face):
        """Offline stand-in for Scryfall: one picture per exact printing + face."""
        self.requested.append((it.scryfall_id, face))
        return fake_jpeg(os.path.join(self.tmp, "img", f"{it.scryfall_id}_{face}.jpg"))

    def export(self, options=None, fetch=None):
        items = printdeck.items_from_deck(self.st, self.deck)
        out = os.path.join(self.tmp, printdeck.default_filename("Talrand Spells"))
        return items, printdeck.export(items, out, options or printdeck.Options(), fetch=fetch or self.fetch)


@unittest.skipUnless(has_cards(), "needs cards.db with oracle_cards")
class PrintTests(Base):

    def test_full_deck_100_fronts_exact_size_nine_per_page(self):
        from docx import Document
        from docx.enum.table import WD_ROW_HEIGHT_RULE
        items, res = self.export()
        self.assertEqual(res["cards"], 100)
        self.assertEqual(res["pages"], 12)
        self.assertEqual(res["missing"], [])
        d = Document(res["path"])  # a real .docx that opens again
        sec = d.sections[0]
        self.assertEqual((round(sec.page_width.mm), round(sec.page_height.mm)), (210, 297))
        pics = d.inline_shapes
        self.assertEqual(len(pics), 100)
        self.assertEqual({(round(p.width.mm, 2), round(p.height.mm, 2)) for p in pics}, {(63.0, 88.0)})  # not stretched
        per_page = [sum(1 for row in t.rows for c in row.cells if c._tc.xpath(".//pic:pic")) for t in d.tables]
        self.assertEqual(per_page, [9] * 11 + [1])
        for t in d.tables:
            self.assertIn('w:type="fixed"', t._tbl.tblPr.xml)
            for row in t.rows:
                self.assertEqual(round(row.height.mm, 2), 88.0)
                self.assertEqual(row.height_rule, WD_ROW_HEIGHT_RULE.EXACTLY)
        # no extra blank page: one page break between pages, none after the last
        breaks = d.element.body.xml.count('w:type="page"')
        self.assertEqual(breaks, len(d.tables) - 1)
        # page margins stay printable and the 3 x 3 block fits without scaling
        self.assertGreaterEqual(sec.left_margin.mm, printdeck.MIN_PRINTER_MARGIN_MM)
        self.assertGreaterEqual(sec.top_margin.mm, printdeck.MIN_PRINTER_MARGIN_MM)
        self.assertLessEqual(3 * 63 + sec.left_margin.mm + sec.right_margin.mm, 210.01)
        self.assertLessEqual(3 * 88 + sec.top_margin.mm + sec.bottom_margin.mm, 297.01)

    def test_quantities_and_exact_printings(self):
        items, res = self.export()
        island = next(i for i in items if i.name == "Island")
        self.assertEqual(island.qty, 30)
        rows = {r["card_name"]: r["scryfall_id"] for r in self.st.deck_cards(self.deck)}
        for it in items:
            self.assertEqual(it.scryfall_id, rows[it.name])  # the deck's own printing
        # one download per printing, even for 30 copies; 30 copies printed
        self.assertEqual(sum(1 for s, f in self.requested if s == island.scryfall_id), 1)
        self.assertEqual(len([1 for it, _ in printdeck.plan(items).fronts if it.name == "Island"]), 30)

    def test_options_commander_basics_and_counts(self):
        items = printdeck.items_from_deck(self.st, self.deck)
        self.assertEqual(printdeck.plan(items, printdeck.Options(include_commander=False)).cards, 99)
        self.assertEqual(printdeck.plan(items, printdeck.Options(include_basics=False)).cards, 70)
        self.assertEqual(printdeck.pages_for(100), 12)
        self.assertEqual(printdeck.pages_for(9), 1)

    def test_double_faced_backs_only_when_asked_and_only_real_ones(self):
        items = printdeck.items_from_deck(self.st, self.deck)
        dfc = [i.name for i in items if i.double_faced]
        self.assertIn("Thing in the Ice // Awoken Horror", dfc)
        self.assertEqual(printdeck.plan(items).backs, [])  # front only by default
        p = printdeck.plan(items, printdeck.Options(include_backs=True))
        self.assertTrue(p.backs)
        self.assertEqual(p.cards, 100)  # backs are extra pages, not extra cards

        def fetch(it, face):  # split cards (Commit // Memory) have no back image
            if face == "back" and it.name == "Commit // Memory":
                return None
            return self.fetch(it, face)
        _, res = self.export(printdeck.Options(include_backs=True), fetch)
        self.assertGreater(res["back_faces"], 0)
        self.assertEqual(res["pages"], 12 + printdeck.pages_for(res["back_faces"]))

    def test_missing_image_is_listed_never_replaced(self):
        def fetch(it, face):
            if it.name == "Sol Ring":
                return None
            return self.fetch(it, face)
        from docx import Document
        _, res = self.export(fetch=fetch)
        self.assertEqual(res["missing"], ["Sol Ring"])
        self.assertTrue(res["warnings"])
        d = Document(res["path"])
        self.assertEqual(len(d.inline_shapes), 99)  # no other printing put in its place
        self.assertIn("IMAGE MISSING", d.element.body.xml)
        self.assertIn("Sol Ring", d.element.body.xml)

    def test_deck_and_collection_are_not_changed(self):
        before = [tuple(r) for r in self.db.conn.execute("SELECT * FROM deck_cards ORDER BY id")]
        coll = [tuple(r) for r in self.db.conn.execute("SELECT * FROM collection_items ORDER BY id")]
        self.export()
        self.assertEqual([tuple(r) for r in self.db.conn.execute("SELECT * FROM deck_cards ORDER BY id")], before)
        self.assertEqual([tuple(r) for r in self.db.conn.execute("SELECT * FROM collection_items ORDER BY id")], coll)

    def test_layout_check_and_filename(self):
        fits, (side, top), msg = printdeck.layout_check("A4")
        self.assertTrue(fits, msg)
        self.assertAlmostEqual(side, 10.5)
        self.assertEqual(printdeck.default_filename("My Deck"), "My_Deck_Printable.docx")
        old = printdeck.PAPER.copy()
        printdeck.PAPER["Tiny"] = (190, 265)
        try:
            self.assertFalse(printdeck.layout_check("Tiny")[0])  # warns, never scales
            with self.assertRaises(ValueError):
                printdeck.export([], os.path.join(self.tmp, "x.docx"), printdeck.Options(paper="Tiny"))
        finally:
            printdeck.PAPER.clear()
            printdeck.PAPER.update(old)


class App:
    def __init__(self, db, folder):
        self.userdb, self.settings, self.dialog, self.folder = db, {"currency": "USD"}, None, folder
        self.decks_screen = self.collection_screen = None
        self.toasts = []

    def export_dir(self):
        return self.folder

    def money(self, u):
        return f"${u or 0:.2f}"

    def toast(self, t, *a):
        self.toasts.append(t)

    def toast_text(self):
        return None

    def open_dialog(self, d):
        self.dialog = d

    def pending_deck_scan(self):
        return None


@unittest.skipUnless(has_cards(), "needs cards.db with oracle_cards")
class PrintScreenTests(Base):
    def test_deck_details_print_button_opens_the_dialog_for_this_deck(self):
        from decks_view import DecksScreen
        from print_view import PrintDialog
        app = App(self.db, os.path.join(self.tmp, "exports"))
        other = self.st.create_deck("Other deck")
        ds = DecksScreen(app, 1400, 610, store=self.st)
        ds.open_deck(self.deck)
        img, hits = ds.render()
        hit = next(h for h in hits if h[4] == "print")
        ds.click((hit[0] + hit[2]) // 2, (hit[1] + hit[3]) // 2, hits)
        self.assertIsInstance(app.dialog, PrintDialog)
        self.assertEqual(app.dialog.deck_name, "Talrand Spells")  # the selected deck, not another one
        self.assertEqual(sum(i.qty for i in app.dialog.items), 100)
        self.assertTrue(app.dialog.path.endswith("Talrand_Spells_Printable.docx"))
        self.assertNotEqual(other, self.deck)

    def test_dialog_counts_toggles_overwrite_confirmation_and_export(self):
        from print_view import PrintDialog
        app = App(self.db, os.path.join(self.tmp, "exports"))
        items = printdeck.items_from_deck(self.st, self.deck)
        dlg = PrintDialog(app, items, "Talrand Spells")
        app.dialog = dlg
        self.assertEqual(dlg.counts()[:2], (100, 12))
        dlg.draw(np.zeros((650, 1400, 3), np.uint8))
        self.assertTrue(any(h[4] == ("toggle", "include_basics") for h in dlg.hits))
        dlg.opts.include_basics = False
        self.assertEqual(dlg.counts()[:2], (70, 8))
        dlg.opts.include_basics = True
        # an existing file is never overwritten without asking
        os.makedirs(os.path.dirname(dlg.path), exist_ok=True)
        open(dlg.path, "w").close()
        orig = printdeck.export
        printdeck.export = lambda it, path, o, progress=None: orig(it, path, o, progress, fetch=self.fetch)
        self.addCleanup(setattr, printdeck, "export", orig)
        dlg.confirm_and_export()
        self.assertIsNot(app.dialog, dlg)
        self.assertIn("Replace", app.dialog.title)
        app.dialog.finish(False)  # Cancel: nothing written, back to the print dialog
        self.assertIs(app.dialog, dlg)
        self.assertEqual(os.path.getsize(dlg.path), 0)
        dlg.confirm_and_export()
        app.dialog.finish(True)
        end = time.time() + 60
        while dlg.running is not None or (dlg.result is None and dlg.error is None):
            self.assertLess(time.time(), end)
            time.sleep(0.05)  # the export runs in the background
        self.assertIsNone(dlg.error)
        self.assertEqual(dlg.result["cards"], 100)
        self.assertGreater(os.path.getsize(dlg.path), 1000)
        dlg.draw(np.zeros((650, 1400, 3), np.uint8))
        self.assertTrue(any(h[4] == ("open", None) for h in dlg.hits))

    def test_build_deck_prints_the_built_deck(self):
        res = dict(commander="Talrand, Sky Summoner",
                   cards=[dict(name=r["card_name"], scryfall_id=r["scryfall_id"], qty=r["quantity"])
                          for r in self.st.deck_cards(self.deck) if r["role"] == "main"])
        items = printdeck.items_from_build(self.st, res)
        self.assertEqual(sum(i.qty for i in items), 100)
        self.assertEqual(items[0].role, "commander")
        self.assertEqual({i.scryfall_id for i in items[1:]}, {c["scryfall_id"] for c in res["cards"]})


if __name__ == "__main__":
    unittest.main()
