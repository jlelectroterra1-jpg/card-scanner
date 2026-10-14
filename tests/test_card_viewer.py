"""Large card viewer: click a card picture -> the exact printing, readable.

* high-resolution URL for the exact printing (and the back face of double-faced cards)
* cached image used straight away; otherwise downloaded in the BACKGROUND (a slow download
  never blocks), with the small picture shown meanwhile
* Front / Back, zoom, ESC / X / click-outside close
* Collection, Deck and Analyse open it for the exact printing, and closing it leaves the
  selected card unchanged

    python -m unittest tests.test_card_viewer -v
"""
import os
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import card_viewer  # noqa: E402
import userdb  # noqa: E402
from card_viewer import CardViewer, large_url  # noqa: E402

CARDS_DB = os.path.join(ROOT, "data", "cards.db")
BG = np.full((650, 1400, 3), 80, np.uint8)
CARD = dict(id="abc-123", name="Test Card", set_code="mul", collector_number="45",
            image_normal="https://cards.scryfall.io/normal/front/a/b/abc-123.jpg?1",
            image_small="https://cards.scryfall.io/small/front/a/b/abc-123.jpg?1")
DFC = dict(CARD, id="dfc-1", name="Front Face // Back Face")


def fake_image(colour=(30, 120, 200)):
    img = np.zeros((936, 672, 3), np.uint8)
    img[:] = colour
    return img


class SlowFetcher:
    """Pretends to be a slow network: writes the image after `delay` seconds, in a thread."""

    def __init__(self, delay=0.6, ok=True, missing=()):
        self.delay, self.ok, self.missing, self.calls = delay, ok, set(missing), []

    def __call__(self, url, path, done):
        self.calls.append(url)

        def run():
            time.sleep(self.delay)
            good = self.ok and not any(m in url for m in self.missing)
            if good:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                cv2.imwrite(path, fake_image((40, 160, 90) if "/back/" in url else (30, 120, 200)))
            done(good)
        threading.Thread(target=run, daemon=True).start()


class ViewerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tmp_viewer_", dir=HERE)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for attr, sub in (("XL_DIR", "xl"), ("NORMAL_DIR", "normal"), ("SMALL_DIR", "small")):
            old = getattr(card_viewer, attr)
            setattr(card_viewer, attr, os.path.join(self.tmp, sub))
            self.addCleanup(setattr, card_viewer, attr, old)

    def wait(self, v, face="front", t=5):
        end = time.time() + t
        while v.state.get(face) == "loading" and time.time() < end:
            time.sleep(0.02)

    def test_high_resolution_url_for_the_exact_printing(self):
        self.assertEqual(large_url(CARD), "https://cards.scryfall.io/large/front/a/b/abc-123.jpg?1")
        self.assertEqual(large_url(CARD, "back"), "https://cards.scryfall.io/large/back/a/b/abc-123.jpg?1")

    def test_cached_image_is_used_without_downloading(self):
        os.makedirs(card_viewer.XL_DIR)
        cv2.imwrite(card_viewer.xl_path("abc-123"), fake_image())
        f = SlowFetcher()
        v = CardViewer(CARD, fetcher=f)
        self.assertEqual(v.state["front"], "ok")
        self.assertEqual(f.calls, [])
        img, full = v.current()
        self.assertTrue(full)
        self.assertEqual(img.shape[:2], (936, 672))

    def test_slow_download_does_not_block_and_small_picture_shows_meanwhile(self):
        os.makedirs(card_viewer.SMALL_DIR)
        cv2.imwrite(os.path.join(card_viewer.SMALL_DIR, "abc-123.jpg"), np.full((204, 146, 3), 200, np.uint8))
        f = SlowFetcher(delay=0.8)
        t = time.time()
        v = CardViewer(CARD, fetcher=f)
        out = v.draw(BG)
        self.assertLess(time.time() - t, 0.4)  # returned long before the "download" finished
        self.assertEqual(out.shape, BG.shape)
        self.assertEqual(v.state["front"], "loading")
        img, full = v.current()
        self.assertFalse(full)
        self.assertEqual(img.shape[:2], (204, 146))  # the small one meanwhile
        self.wait(v)
        self.assertEqual(v.state["front"], "ok")
        self.assertEqual(v.current()[0].shape[:2], (936, 672))
        self.assertTrue(os.path.exists(card_viewer.xl_path("abc-123")))  # cached for next time

    def test_image_is_large_enough_to_read(self):
        v = CardViewer(CARD, fetcher=SlowFetcher(delay=0))
        self.wait(v)
        x, y, w, h = v.card_box(1400, 650)
        self.assertGreaterEqual(h, 560)
        self.assertAlmostEqual(h / w, 88 / 63, delta=0.02)
        v.wheel(120)
        self.assertGreater(v.card_box(1400, 650)[3], h)
        v.key(ord("-"))
        self.assertEqual(v.card_box(1400, 650)[3], h)

    def test_double_faced_cards_flip(self):
        f = SlowFetcher(delay=0)
        v = CardViewer(DFC, fetcher=f)
        self.wait(v)
        self.assertEqual(v.faces, ["front", "back"])
        v.key(card_viewer.RIGHT)
        self.assertEqual(v.face, 1)
        self.wait(v, "back")
        self.assertEqual(v.state["back"], "ok")
        self.assertTrue(any("/large/back/" in u for u in f.calls))
        v.draw(BG)
        v.key(card_viewer.LEFT)
        self.assertEqual(v.face, 0)
        # clicking the card itself flips too (and doesn't close)
        x, y, w, h = v.card_box(1400, 650)
        v.draw(BG)
        v.click(x + w // 2, y + h // 2)
        self.assertEqual((v.face, v.done), (1, False))

    def test_split_card_without_back_image_says_so(self):
        v = CardViewer(DFC, fetcher=SlowFetcher(delay=0, missing=("/back/",)))
        self.wait(v)
        v.set_face(1)
        self.wait(v, "back")
        self.assertEqual(v.state["back"], "missing")
        v.draw(BG)  # no crash, shows the message

    def test_close_with_esc_x_and_outside_click(self):
        v = CardViewer(CARD, fetcher=SlowFetcher(delay=0))
        v.key(27)
        self.assertTrue(v.done)
        v = CardViewer(CARD, fetcher=SlowFetcher(delay=0))
        v.draw(BG)
        x0, y0, x1, y1, _ = next(h for h in v.hits if h[4] == ("close", None))
        v.click((x0 + x1) // 2, (y0 + y1) // 2)
        self.assertTrue(v.done)
        v = CardViewer(CARD, fetcher=SlowFetcher(delay=0))
        v.draw(BG)
        v.click(60, 400)  # outside the card
        self.assertTrue(v.done)
        v = CardViewer(CARD, fetcher=SlowFetcher(delay=0))
        x, y, w, h = v.card_box(1400, 650)
        v.draw(BG)
        v.click(x + w // 2, y + h // 2)  # on a single-faced card: stays open
        self.assertFalse(v.done)


class App:
    def __init__(self, db):
        self.userdb, self.settings, self.dialog = db, {"currency": "USD"}, None
        self.decks_screen = self.collection_screen = None

    def money(self, u):
        return f"${u or 0:.2f}"

    def toast(self, *a):
        pass

    def toast_text(self):
        return None

    def open_dialog(self, d):
        self.dialog = d

    def pending_deck_scan(self):
        return None


@unittest.skipUnless(os.path.exists(CARDS_DB), "no cards.db")
class ScreenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tmp_viewer2_", dir=HERE)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        old = userdb.BACKUP_DIR
        userdb.BACKUP_DIR = os.path.join(self.tmp, "b")
        self.addCleanup(setattr, userdb, "BACKUP_DIR", old)
        self.db = userdb.UserDB(os.path.join(self.tmp, "u.db"))
        self.addCleanup(self.db.close)
        from decks import DeckStore
        self.st = DeckStore(self.db)
        prints = [p for p in self.st.printings("Sol Ring") if p["lang"] == "en"]
        self.p1, self.p2 = prints[0], prints[1]
        # never touch the network in these tests
        old_fetch = card_viewer.fetch
        card_viewer.fetch = lambda url, path, done: done(False)
        self.addCleanup(setattr, card_viewer, "fetch", old_fetch)

    def click_hit(self, screen, action):
        img, hits = screen.render()
        x0, y0, x1, y1, a = next(h for h in hits if h[4] == action)
        screen.click((x0 + x1) // 2, (y0 + y1) // 2, hits)

    def test_collection_card_opens_large_exact_printing(self):
        from collection_view import CollectionScreen
        app = App(self.db)
        lot = self.db.add_card(self.p2["id"], "Sol Ring", self.p2["set_code"], self.p2["collector_number"])
        sc = CollectionScreen(app, 1400, 610)
        sc.selected = lot
        sc.bump()
        self.click_hit(sc, ("view", self.p2["id"]))
        self.assertIsInstance(app.dialog, CardViewer)
        self.assertEqual(app.dialog.card["id"], self.p2["id"])
        app.dialog.key(27)
        self.assertTrue(app.dialog.done)
        self.assertEqual(sc.selected, lot)  # still the same selected card
        # after Change printing, the viewer shows the NEW exact printing
        sc.store.change_printing(lot, self.p1["id"])
        sc.refresh()
        self.click_hit(sc, ("view", self.p1["id"]))
        self.assertEqual(app.dialog.card["id"], self.p1["id"])

    def test_deck_card_opens_large_exact_printing(self):
        from decks_view import DecksScreen
        app = App(self.db)
        deck = self.st.create_deck("Viewer deck")
        self.st.add_card(deck, self.st.card(self.p2["id"]))
        ds = DecksScreen(app, 1400, 610, store=self.st)
        ds.open_deck(deck)
        row = next(r for r in ds.rows if r["card_name"] == "Sol Ring")
        ds.selected = row["id"]
        ds.bump()
        self.click_hit(ds, ("view", self.p2["id"]))
        self.assertEqual(app.dialog.card["id"], self.p2["id"])
        app.dialog.key(27)
        self.assertEqual(ds.selected, row["id"])

    def test_analyse_images_open_the_deck_printing(self):
        from analyse_view import AnalyseScreen
        app = App(self.db)
        deck = self.st.create_deck("Viewer deck")
        self.st.add_card(deck, self.st.card(self.p2["id"]))
        sc = AnalyseScreen(app, 1400, 610, store=self.st)
        sc.select_deck(deck)
        sc.click(0, 0, [(0, 0, 10, 10, ("view", "Sol Ring"))])
        self.assertEqual(app.dialog.card["id"], self.p2["id"])  # the printing IN the deck

    def test_scanner_panel_last_card_is_clickable(self):
        from panel import Panel
        pnl = Panel(440, 610)
        view = dict(count=1, total=1.0, rate=0, lock=None, foil_default=False, camera=0, recent=[], review=None,
                    typing=None, bg={}, bg_menu=False, money=lambda u: f"${u:.2f}", deck_mode=None,
                    show_destination=False, destination=None, can_choose_destination=False, pending=0, auto_scan=False,
                    last=dict(name="Sol Ring", set_name="x", set_code="c21", number="1", rarity="uncommon",
                              finish="nonfoil", price=1.0, printing=1, printings=1, how="picture", image=None))
        _, buttons = pnl.render(view, 1)
        self.assertIn("view_last", [b[4] for b in buttons])


if __name__ == "__main__":
    unittest.main()
