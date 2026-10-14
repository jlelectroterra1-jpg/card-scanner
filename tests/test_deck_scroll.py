"""Deck view card list scrolling: mouse wheel, scrollbar (click + drag), Page Up / Page
Down, Home / End, fixed header, grouped and A-Z views, and the window's mouse routing.

    python -m unittest tests.test_deck_scroll -v

Uses a temporary user database and the real cards.db (read-only).
"""
import os
import shutil
import sys
import tempfile
import types
import unittest

import cv2

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import userdb  # noqa: E402
from currency import money  # noqa: E402
from decks import DeckStore  # noqa: E402
from decks_view import LIST_X1, WHEEL_LINES, DecksScreen  # noqa: E402
from userdb import UserDB  # noqa: E402

CARDS_DB = os.path.join(ROOT, "data", "cards.db")
PGUP, PGDN, HOME, END = 0x210000, 0x220000, 0x240000, 0x230000


class App:
    def __init__(self, db):
        self.userdb, self.settings = db, {"currency": "USD"}

    def money(self, usd):
        return money(usd, self.settings)

    def toast(self, *a):
        pass

    def toast_text(self):
        return None

    def pending_deck_scan(self):
        return None

    def open_dialog(self, d):
        pass


@unittest.skipUnless(os.path.exists(CARDS_DB), "no data/cards.db")
class DeckScrollTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tmp_scroll_", dir=HERE)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        old = userdb.BACKUP_DIR
        userdb.BACKUP_DIR = os.path.join(self.tmp, "backups")
        self.addCleanup(setattr, userdb, "BACKUP_DIR", old)
        self.db = UserDB(os.path.join(self.tmp, "user.db"))
        self.addCleanup(self.db.close)
        self.st = DeckStore(self.db)
        names = [r[0] for r in self.db.conn.execute(
            """SELECT name FROM scry.oracle_cards WHERE legal_commander = 'legal' AND type_line NOT LIKE '%Land%'
               AND color_identity IN ('', 'G') ORDER BY name LIMIT 70""")]
        text = "Commander\n1 Azusa, Lost but Seeking\nDeck\n" + "\n".join("1 " + n for n in names) + "\n29 Forest"
        self.deck = self.st.create_deck("Big Deck")
        self.st.commit_import(self.deck, self.st.parse_decklist(text))
        self.small = self.st.create_deck("Small Deck")
        self.st.commit_import(self.small, self.st.parse_decklist("Commander\n1 Azusa, Lost but Seeking\nDeck\n1 Sol Ring"))
        self.sc = DecksScreen(App(self.db), 1400, 610, store=self.st)
        self.sc.open_deck(self.deck)

    # helpers
    def hits(self):
        return self.sc.render()[1]

    def visible_cards(self):
        return [h[4][1] for h in self.hits() if isinstance(h[4], tuple) and h[4][0] == "card" and h[0] < LIST_X1]

    def all_cards(self):
        return [l[1]["id"] for l in self.sc.display_rows() if l[0] == "card"]

    def hit(self, action):
        return next(h for h in self.hits() if h[4] == action)

    # tests
    def test_whole_deck_reachable_with_the_wheel(self):
        self.assertNotIn(self.all_cards()[-1], self.visible_cards())  # the deck is longer than the window
        for _ in range(100):
            self.sc.wheel(-120)
        self.assertEqual(self.visible_cards()[-1], self.all_cards()[-1])
        self.assertEqual(self.sc.scroll, self.sc.max_scroll())
        for _ in range(100):
            self.sc.wheel(120)
        self.assertEqual(self.sc.scroll, 0)
        self.assertEqual(self.visible_cards()[0], self.all_cards()[0])

    def test_wheel_step(self):
        self.sc.render()
        self.sc.wheel(-120)
        self.assertEqual(self.sc.scroll, WHEEL_LINES)
        self.sc.wheel(120)
        self.assertEqual(self.sc.scroll, 0)

    def test_page_keys_home_end(self):
        self.sc.render()
        page = self.sc.page()
        self.assertGreater(page, 5)
        self.sc.key(PGDN)
        self.assertEqual(self.sc.scroll, page)
        self.sc.key(PGUP)
        self.assertEqual(self.sc.scroll, 0)
        self.sc.key(END)
        self.assertEqual(self.sc.scroll, self.sc.max_scroll())
        self.assertEqual(self.visible_cards()[-1], self.all_cards()[-1])
        self.sc.key(PGUP)  # straight back from the end (no "dead" scrolling past it)
        self.assertEqual(self.sc.scroll, self.sc.max_scroll() - page)
        self.sc.key(HOME)
        self.assertEqual(self.sc.scroll, 0)

    def test_paging_shows_every_card_in_both_views(self):
        for grouping in ("type", "all"):
            self.sc.grouping, self.sc.scroll = grouping, 0
            self.sc.bump()
            seen = set(self.visible_cards())
            for _ in range(30):
                self.sc.key(PGDN)
                seen |= set(self.visible_cards())
            self.assertEqual(seen, set(self.all_cards()), grouping)

    def test_scrollbar_thumb_size_and_position(self):
        hits = self.hits()
        self.assertTrue(any(h[4] == ("sb", "track") for h in hits))
        sb = self.sc._sb
        t0, t1 = sb["thumb"]
        track = sb["bottom"] - sb["top"]
        self.assertAlmostEqual(t1 - t0, max(28, track * sb["visible"] / sb["total"]), delta=1)
        self.assertAlmostEqual(t0, sb["top"], delta=1)
        self.sc.key(END)
        self.hits()
        self.assertAlmostEqual(self.sc._sb["thumb"][1], sb["bottom"], delta=1)

    def test_click_above_and_below_the_thumb_pages(self):
        x0, y0, x1, y1, _ = self.hit(("sb", "track"))
        page = self.sc.page()
        self.sc.click((x0 + x1) // 2, y1 - 5, self.hits())  # below the thumb
        self.assertEqual(self.sc.scroll, page)
        self.sc.click((x0 + x1) // 2, y0 + 2, self.hits())  # above the thumb
        self.assertEqual(self.sc.scroll, 0)

    def test_drag_the_thumb(self):
        x0, y0, x1, y1, _ = self.hit(("sb", "thumb"))
        cx = (x0 + x1) // 2
        self.sc.click(cx, y0 + 5, self.hits())
        self.sc.drag(cx, 10_000)  # all the way down
        self.assertEqual(self.sc.scroll, self.sc.max_scroll())
        self.sc.drag(cx, (self.sc._sb["top"] + self.sc._sb["bottom"]) // 2)
        self.assertTrue(0 < self.sc.scroll < self.sc.max_scroll())
        self.sc.drag(cx, -10_000)
        self.assertEqual(self.sc.scroll, 0)
        self.sc.release()
        self.sc.drag(cx, 10_000)  # no longer dragging: nothing moves
        self.assertEqual(self.sc.scroll, 0)

    def test_header_stays_fixed(self):
        def header(hits):
            return sorted((h[1], h[3], repr(h[4])) for h in hits
                          if h[4] in ("back", "scan", "add", "dd:commander", "dd:more")
                          or (isinstance(h[4], tuple) and h[4][0] in ("group", "sort")))
        top = header(self.hits())
        self.sc.key(END)
        hits = self.hits()
        self.assertEqual(header(hits), top)
        list_top = self.sc._sb["top"]
        self.assertTrue(all(h[1] >= list_top for h in hits if isinstance(h[4], tuple) and h[4][0] == "card"
                            and h[0] < LIST_X1))

    def test_short_deck_has_no_scrolling(self):
        self.sc.open_deck(self.small)
        hits = self.hits()
        self.assertFalse(any(h[4] == ("sb", "thumb") for h in hits))
        self.sc.wheel(-120)
        self.sc.key(PGDN)
        self.sc.key(END)
        self.assertEqual(self.sc.scroll, 0)

    def test_window_routes_drag_and_release_to_the_deck_list(self):
        import scanner
        calls = []
        screen = types.SimpleNamespace(click=lambda *a: calls.append("click"), wheel=lambda d: calls.append("wheel"),
                                       drag=lambda x, y: calls.append(("drag", y)), release=lambda: calls.append("up"))
        fake = types.SimpleNamespace(dialog=None, tab="decks", decks_screen=screen, _screen_hits=[], _nav_hits=[])
        on_mouse = scanner.Scanner.on_mouse
        on_mouse(fake, cv2.EVENT_MOUSEMOVE, 900, 300, cv2.EVENT_FLAG_LBUTTON, None)
        on_mouse(fake, cv2.EVENT_MOUSEMOVE, 900, 320, 0, None)  # button not held: ignored
        on_mouse(fake, cv2.EVENT_LBUTTONUP, 900, 320, 0, None)
        on_mouse(fake, cv2.EVENT_MOUSEWHEEL, 500, 300, -120 << 16, None)
        self.assertEqual(calls, [("drag", 300 - scanner.NAV_H), "up", "wheel"])

    def test_wheel_reaches_every_screen(self):
        """The wheel used to crash inside the mouse handler (cv2.getMouseWheelDelta is gone
        in OpenCV 5), so it never scrolled anything."""
        import scanner
        self.assertEqual(scanner.wheel_delta(120 << 16), 120)
        self.assertEqual(scanner.wheel_delta(-120 << 16), -120)
        self.assertEqual(scanner.wheel_delta((-120 << 16) & 0xFFFFFFFF), -120)  # unsigned form
        self.assertEqual(scanner.wheel_delta((240 << 16) | 8), 240)  # with a key flag set
        got = []
        screen = types.SimpleNamespace(wheel=got.append)
        dialog = types.SimpleNamespace(wheel=got.append, done=False)
        for tab in ("collection", "decks", "analyse"):
            fake = types.SimpleNamespace(dialog=None, tab=tab, collection_screen=screen, decks_screen=screen,
                                         analyse_screen=screen, _screen_hits=[], _nav_hits=[])
            scanner.Scanner.on_mouse(fake, cv2.EVENT_MOUSEWHEEL, 500, 300, -120 << 16, None)
        fake = types.SimpleNamespace(dialog=dialog, tab="decks")
        scanner.Scanner.on_mouse(fake, cv2.EVENT_MOUSEWHEEL, 500, 300, 120 << 16, None)
        self.assertEqual(got, [-120, -120, -120, 120])
        # end to end on the real deck screen
        fake = types.SimpleNamespace(dialog=None, tab="decks", decks_screen=self.sc, _screen_hits=[], _nav_hits=[])
        self.sc.render()
        scanner.Scanner.on_mouse(fake, cv2.EVENT_MOUSEWHEEL, 500, 300, -120 << 16, None)
        self.assertEqual(self.sc.scroll, WHEEL_LINES)


if __name__ == "__main__":
    unittest.main()
