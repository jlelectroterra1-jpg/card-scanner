"""End-to-end Phase 3 workflow with the real scanner (simulated camera frames of real
card pictures, real recognition), following the requested manual test:

 1 create 'Test Commander Deck'   2 choose the commander (by scanning it)
 3 Scan Deck mode                 4 scan cards        5 stop halfway
 6 close / restart the scanner    7 resume the deck   8 finish the deck
 9 review ownership matches      10 add missing scanned cards to the Collection
11 (saved as you go)             12 reopen it        13 allocations still correct
14 export the deck               15 delete the deck  16 Collection intact, copies released

    python -m unittest tests.test_deck_workflow -v

Everything happens in a temporary folder; your real data is never touched."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

import cv2

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import scanner  # noqa: E402
import userdb  # noqa: E402
from recognizer import Recognizer  # noqa: E402
from test_background import ZONE, fake_open_camera, lit, playmat, with_card  # noqa: E402

IMG_DIR = os.path.join(ROOT, "data", "img")
_shared = {}


class DeckWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.exists(os.path.join(ROOT, "data", "cards.db")):
            raise unittest.SkipTest("no cards.db")
        from carddb import CardDB
        from visual import load_index
        _shared["db"] = CardDB()
        _shared["rec"] = Recognizer(_shared["db"], load_index())
        cls.saved = (scanner.CardDB, scanner.Recognizer, scanner.Scanner.open_camera)
        scanner.CardDB = lambda: _shared["db"]
        scanner.Recognizer = lambda db, vis: _shared["rec"]
        scanner.Scanner.open_camera = fake_open_camera
        import visual
        cls.saved_li = visual.load_index
        visual.load_index = lambda: _shared["rec"].visual
        import decks_view
        cls.saved_of = decks_view.open_folder
        decks_view.open_folder = lambda p: None

    @classmethod
    def tearDownClass(cls):
        scanner.CardDB, scanner.Recognizer, scanner.Scanner.open_camera = cls.saved
        import visual
        visual.load_index = cls.saved_li
        import decks_view
        decks_view.open_folder = cls.saved_of

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tmp_wf_", dir=HERE)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        saved = {k: getattr(scanner, k) for k in ("SETTINGS_PATH", "SESSION_PATH", "BACKGROUND_DIR", "USER_DB_PATH",
                                                  "EXPORT_DIR", "DEBUG_DIR", "DECK_SCAN_PATH")}
        self.addCleanup(lambda: [setattr(scanner, k, v) for k, v in saved.items()])
        for k, name in (("SETTINGS_PATH", "settings.json"), ("SESSION_PATH", "session.json"), ("BACKGROUND_DIR", "bg"),
                        ("USER_DB_PATH", "user.db"), ("EXPORT_DIR", "exports"), ("DEBUG_DIR", "debug"),
                        ("DECK_SCAN_PATH", "deck_scan.json")):
            setattr(scanner, k, os.path.join(self.tmp, name))
        old = userdb.BACKUP_DIR
        userdb.BACKUP_DIR = os.path.join(self.tmp, "backups")
        self.addCleanup(setattr, userdb, "BACKUP_DIR", old)
        with open(scanner.SETTINGS_PATH, "w") as f:
            json.dump({"camera": 2, "zone": ZONE, "usd_zar": 18.0, "currency": "ZAR"}, f)
        self.mat = playmat()
        self.scanners = []

    def tearDown(self):
        for s in self.scanners:
            if s.userdb is not None:
                try:
                    s.userdb.close()
                except Exception:  # noqa: BLE001
                    pass

    # ---- helpers -----------------------------------------------------------------

    def start(self):
        s = scanner.Scanner(type("A", (), {"camera": None})())
        self.scanners.append(s)
        s.last_frame = lit(self.mat)
        if s.background is None:
            s.learn_background()
        return s

    def printing_with_image(self, s, name):
        for p in s.deck_store.printings(name):
            if os.path.exists(os.path.join(IMG_DIR, p["id"] + ".jpg")) and p["lang"] == "en":
                return p
        self.skipTest(f"no local picture of {name}")

    def scan(self, s, card):
        """Put a card in the box until the scanner has read it, then take it away."""
        img = cv2.imread(os.path.join(IMG_DIR, card["id"] + ".jpg"))
        frames = [lit(self.mat, seed=1)] * 3 + [lit(with_card(self.mat, img), seed=i) for i in range(8)]
        for f in frames:
            s.last_frame = f
            if s.dialog is None and s.background is not None:
                s.update_trigger(f)
            self.drain(s)
        self.drain(s, wait=True)
        for f in [lit(self.mat, seed=2)] * 3:  # card taken away
            s.last_frame = f
            if s.dialog is None:
                s.update_trigger(f)
            self.drain(s)

    @staticmethod
    def drain(s, wait=False):
        deadline = time.time() + (30 if wait else 0)
        while True:
            while not s.results.empty():
                s.handle_result(s.results.get())
            if not (wait and s.busy and time.time() < deadline):
                return
            time.sleep(0.02)

    @staticmethod
    def answer(s, value, text=None):
        d = s.dialog
        assert d is not None, "expected a question"
        if text is not None:
            d.text = text
        d.finish(value)
        if s.dialog is d:
            s.dialog = None
        return d

    # ---- the workflow ------------------------------------------------------------------

    def test_full_deck_workflow(self):
        s = self.start()
        names = ["Sol Ring", "Arcane Signet", "Command Tower", "Rhystic Study", "Swords to Plowshares", "Island"]
        cards = {n: self.printing_with_image(s, n) for n in names + ["Atraxa, Praetors' Voice"]}
        # The Collection before: Sol Ring (this printing), Arcane Signet (another printing), and the only
        # Rhystic Study is already in another deck.
        udb, st = s.userdb, s.deck_store
        sol_lot = udb.add_card(cards["Sol Ring"]["id"], "Sol Ring", cards["Sol Ring"]["set_code"],
                               cards["Sol Ring"]["collector_number"])
        other_signet = next(p for p in st.printings("Arcane Signet") if p["id"] != cards["Arcane Signet"]["id"])
        udb.add_card(other_signet["id"], "Arcane Signet", other_signet["set_code"], other_signet["collector_number"])
        rhystic_lot = udb.add_card(cards["Rhystic Study"]["id"], "Rhystic Study", cards["Rhystic Study"]["set_code"],
                                   cards["Rhystic Study"]["collector_number"])
        other_deck = st.create_deck("Other Deck")
        udb.add_deck_card(other_deck, "Rhystic Study", scryfall_id=cards["Rhystic Study"]["id"],
                          collection_item_id=rhystic_lot)
        before = {n: udb.copies_owned(n) for n in names}

        # 1. create the deck through the Decks tab
        s.switch_tab("decks")
        s.decks_screen.new_deck()
        self.answer(s, True, "Test Commander Deck")
        self.answer(s, "commander")
        deck = s.decks_screen.deck_id
        self.assertEqual(udb.deck(deck)["name"], "Test Commander Deck")
        self.assertEqual(udb.deck(deck)["format"], "commander")
        self.answer(s, "scan")  # 2. choose the commander by scanning it
        self.assertEqual(s.target.kind, "commander")
        self.scan(s, cards["Atraxa, Praetors' Voice"])
        self.assertIn("Atraxa", s.dialog.title)  # "Set Atraxa, Praetors' Voice as commander?"
        self.answer(s, True)
        self.assertEqual(udb.deck(deck)["commander"], "Atraxa, Praetors' Voice")
        self.assertEqual(s.tab, "decks")

        # 3-4. Scan Deck mode, scan the first half
        s.start_deck_scan(deck)
        self.assertEqual((s.tab, s.target.kind), ("scanner", "deck"))
        view = s.panel_view()[0]
        self.assertEqual(view["deck_mode"]["name"], "Test Commander Deck")
        for n in names[:3]:
            self.scan(s, cards[n])
        self.assertEqual([e["name"] for e in s.target.entries], names[:3])
        self.assertEqual(s.panel_view()[0]["deck_mode"]["count"], 4)  # commander + 3
        self.assertEqual(s.normal_target.entries, [], "deck scans must not go into the normal scan list")

        # 5-6. stop halfway: the app closes
        udb.close()
        s.userdb = None
        self.assertTrue(os.path.exists(scanner.DECK_SCAN_PATH))

        # 7. restart: offered to resume
        s = self.start()
        udb, st = s.userdb, s.deck_store
        self.assertIn("Resume scanning Test Commander Deck", s.dialog.title)
        self.answer(s, "resume")
        self.assertEqual(s.target.kind, "deck")
        self.assertEqual(len(s.target.entries), 3)
        for n in names[3:]:
            self.scan(s, cards[n])
        self.scan(s, cards["Island"])  # basic lands: no warning
        self.assertIsNone(s.dialog)
        self.scan(s, cards["Sol Ring"])  # a duplicate: asked, not dropped
        self.assertEqual(s.dialog.title, "Duplicate detected")
        self.assertEqual(sum(1 for e in s.target.entries if e["name"] == "Sol Ring"), 2)
        self.answer(s, "remove")
        self.assertEqual(sum(1 for e in s.target.entries if e["name"] == "Sol Ring"), 1)
        self.assertEqual(s.panel_view()[0]["deck_mode"]["count"], 8)

        # 8. finish -> 10. add the scanned cards that aren't in the Collection
        s.finish_deck_scan()
        self.assertFalse(os.path.exists(scanner.DECK_SCAN_PATH))
        self.assertIn("not in your Collection", s.dialog.title)
        self.answer(s, True)

        # 9. ownership review
        rows = {r["card_name"]: r for r in st.deck_cards(deck)}
        self.assertEqual(rows["Sol Ring"]["ownership"], "exact")
        self.assertEqual(rows["Sol Ring"]["collection_item_id"], sol_lot)
        self.assertEqual(rows["Arcane Signet"]["ownership"], "different_printing")
        self.assertEqual(rows["Rhystic Study"]["ownership"], "deck_only")  # its copy belongs to Other Deck
        for n in ("Command Tower", "Swords to Plowshares", "Atraxa, Praetors' Voice"):
            self.assertEqual(rows[n]["ownership"], "exact", n)
        self.assertEqual(rows["Island"]["quantity"], 2)
        info = st.summary(deck)
        self.assertEqual(info["count"], 8)
        self.assertEqual((info["owned"], info["missing"]), (8, 0))
        self.assertEqual(s.money(info["value"]), f"R{info['value'] * 18:,.2f}")

        # 12-13. reopen (restart again): everything is still linked the same way
        snapshot = {r["card_name"]: (r["collection_item_id"], r["ownership"], r["quantity"]) for r in st.deck_cards(deck)}
        udb.close()
        s.userdb = None
        s = self.start()
        udb, st = s.userdb, s.deck_store
        self.assertIsNone(s.dialog, "nothing left to resume")
        s.switch_tab("decks")
        s.decks_screen.open_deck(deck)
        s.decks_screen.render()
        again = {r["card_name"]: (r["collection_item_id"], r["ownership"], r["quantity"]) for r in st.deck_cards(deck)}
        self.assertEqual(again, snapshot)
        self.assertEqual(udb.availability(rhystic_lot)["available"], 0)  # still Other Deck's

        # 14. export
        s.decks_screen.export()
        exported = os.listdir(scanner.EXPORT_DIR)
        self.assertEqual(len(exported), 2)
        txt = next(f for f in exported if f.endswith(".txt"))
        with open(os.path.join(scanner.EXPORT_DIR, txt), encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(text.startswith("Commander\n1 Atraxa, Praetors' Voice"))
        self.assertIn("2 Island", text)

        # 15-16. delete the deck: Collection cards stay, its copies become free
        owned_before_delete = {n: udb.copies_owned(n) for n in names}
        s.decks_screen.delete()
        self.answer(s, True)
        self.assertIsNone(udb.deck(deck))
        self.assertEqual({n: udb.copies_owned(n) for n in names}, owned_before_delete)
        self.assertEqual(udb.availability(sol_lot)["available"], 1)
        self.assertEqual(udb.availability(rhystic_lot)["available"], 0)  # Other Deck untouched
        used = udb.conn.execute("SELECT COUNT(*) FROM deck_cards WHERE collection_item_id IS NOT NULL").fetchone()[0]
        self.assertEqual(used, 1)  # only Other Deck's Rhystic Study is still allocated
        # the scanned cards that were added to the Collection are still there
        for n in ("Command Tower", "Swords to Plowshares"):
            self.assertEqual(owned_before_delete[n], before[n] + 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
