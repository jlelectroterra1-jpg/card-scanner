"""ONE PHYSICAL PRESENTATION = ONE SCAN.

After a card is scanned the scanner is locked on it until the box shows the empty
background again (card removed) or a clearly different card is lying there (next card
dropped on top). Stillness, small shifts, a hand passing over, lighting and glare
changes, Stop/Start, repeated SPACE, the review screen, skipping and deck-duplicate
rejection must never produce a second scan of the same physical card - while removing
the card and presenting it (or another copy) again, and stacking different cards, must.

    python -m unittest tests.test_scan_once -v

Real recognition on synthetic camera frames (test_background's playmat); temp folders only.
"""
import os
import sys
import time
import unittest

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import scanner  # noqa: E402
import test_background as tb  # noqa: E402
from test_background import ZONE, lit, playmat  # noqa: E402

B = tb.BackgroundTests.__dict__
IMG = os.path.join(ROOT, "data", "img")


def place(frame, card_img, dx=0.0, dy=0.0, angle=4.0):
    """Lay a card in the scan box, optionally shifted / rotated a little."""
    h, w = 230, int(230 * 63 / 88)
    cx, cy = (ZONE[0] + ZONE[2]) / 2 + dx, (ZONE[1] + ZONE[3]) / 2 + dy
    pts = np.array([[-w / 2, -h / 2], [w / 2, -h / 2], [w / 2, h / 2], [-w / 2, h / 2]])
    a = np.radians(angle)
    R = np.array([[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]])
    dst = (pts @ R.T + [cx, cy]).astype(np.float32)
    ch, cw = card_img.shape[:2]
    M = cv2.getPerspectiveTransform(np.float32([[0, 0], [cw, 0], [cw, ch], [0, ch]]), dst)
    warped = cv2.warpPerspective(card_img, M, (frame.shape[1], frame.shape[0]))
    mask = cv2.warpPerspective(np.full((ch, cw), 255, np.uint8), M, (frame.shape[1], frame.shape[0]))
    out = frame.copy()
    out[mask > 0] = warped[mask > 0]
    return cv2.GaussianBlur(out, (3, 3), 0.8)


def hand(frame, t):
    """A hand-coloured blob sweeping across the box (t from 0 to 1)."""
    out = frame.copy()
    x = int(ZONE[0] - 60 + t * (ZONE[2] - ZONE[0] + 120))
    cv2.ellipse(out, (x, (ZONE[1] + ZONE[3]) // 2), (70, 150), 15, 0, 360, (120, 150, 205), -1)
    return out


def glare(frame, seed):
    rng = np.random.default_rng(seed)
    out = frame.astype(np.float32)
    m = np.zeros(frame.shape[:2], np.float32)
    c = (int(rng.uniform(ZONE[0] + 40, ZONE[2] - 40)), int(rng.uniform(ZONE[1] + 40, ZONE[3] - 40)))
    cv2.ellipse(m, c, (45, 25), rng.uniform(0, 180), 0, 360, 1, -1)
    m = cv2.GaussianBlur(m, (0, 0), 9)
    out = out * (1 - m[..., None]) + 250 * m[..., None]
    return np.clip(out, 0, 255).astype(np.uint8)


class ScanOnceTests(unittest.TestCase):
    setUpClass, tearDownClass = B["setUpClass"], B["tearDownClass"]
    new_scanner, drain, learn = B["new_scanner"], B["drain"], B["learn"]

    def setUp(self):
        B["setUp"](self)
        saved = scanner.DECK_SCAN_PATH
        scanner.DECK_SCAN_PATH = os.path.join(self.tmp, "deck_scan.json")
        self.addCleanup(setattr, scanner, "DECK_SCAN_PATH", saved)
        self.mat = playmat()
        self.cards = {}
        db = tb._shared["db"]
        for name in ("Sol Ring", "Counterspell", "Lightning Bolt", "Llanowar Elves", "Swords to Plowshares",
                     "Cultivate"):
            cid = next(r[0] for r in db.db.execute(
                "SELECT id FROM cards WHERE name = ? AND lang = 'en' ORDER BY released_at DESC", (name,))
                if os.path.exists(os.path.join(IMG, r[0] + ".jpg")))
            self.cards[name] = cv2.imread(os.path.join(IMG, cid + ".jpg"))
        self.s = self.new_scanner()
        self.learn(self.s, lit(self.mat, seed=1))
        self.s.set_auto_scan(True)
        self.started = []
        real = self.s.start_scan

        def spy(frame, manual=False):
            if real(frame, manual) is not False:
                self.started.append(manual)
        self.s.start_scan = spy

    # ---- helpers
    def feed(self, frames):
        for f in frames:
            self.s.last_frame = f
            self.s.update_trigger(f)
            self.drain(self.s)
            if self.s.busy:
                self.drain(self.s, wait=True)

    def still(self, frame, n=12):
        self.feed([frame] * n)

    def card(self, name, seed=0, **kw):
        return lit(place(self.mat, self.cards[name], **kw), seed=seed)

    def empty(self, n=4):
        self.feed([lit(self.mat, seed=50 + i) for i in range(n)])

    def names(self):
        return [tb._shared["db"].by_id(e["id"])["name"] for e in self.s.entries]

    def scan_first(self, name="Sol Ring"):
        self.empty()
        self.still(self.card(name), 12)
        self.assertEqual(self.names(), [name])
        self.assertEqual(len(self.started), 1)

    # ---- the same physical card: never a second scan
    def test_card_stays_still(self):
        self.scan_first()
        self.still(self.card("Sol Ring"), 60)
        self.assertEqual(len(self.started), 1)
        self.assertEqual(self.names(), ["Sol Ring"])
        self.assertTrue(self.s.locked)  # still locked on it

    def test_card_shifts_slightly(self):
        self.scan_first()
        for i, (dx, dy, a) in enumerate([(6, -4, 6), (-8, 5, 1), (10, 8, 8), (-5, -9, 2)]):
            self.feed([self.card("Sol Ring", seed=i, dx=dx / 2, dy=dy / 2, angle=a)] * 2)  # the nudge (moving)
            self.still(self.card("Sol Ring", seed=10 + i, dx=dx, dy=dy, angle=a), 10)
        self.assertEqual(len(self.started), 1)
        self.assertIn("waiting for next card", self.s.status)
        self.assertEqual(self.names(), ["Sol Ring"])

    def test_hand_passes_over(self):
        self.scan_first()
        base = self.card("Sol Ring")
        for rep in range(3):
            self.feed([hand(base, t) for t in np.linspace(0, 1, 8)])
            self.still(self.card("Sol Ring", seed=20 + rep), 10)
        self.assertEqual(len(self.started), 1)

    def test_lighting_changes(self):
        self.scan_first()
        f = place(self.mat, self.cards["Sol Ring"])
        for i, (g, o) in enumerate([(0.7, -10), (1.3, 15), (0.8, 0), (1.2, -5)]):
            self.feed([lit(f, gain=(1 + g) / 2, seed=30 + i)] * 2)
            self.still(lit(f, gain=g, offset=o, seed=40 + i), 10)
        self.assertEqual(len(self.started), 1)

    def test_glare_changes(self):
        self.scan_first()
        f = self.card("Sol Ring")
        for i in range(5):
            self.feed([glare(f, 100 + i)] * 2)
            self.still(glare(f, 200 + i), 10)
        self.assertEqual(len(self.started), 1)

    def test_stop_then_start_with_the_same_card(self):
        self.scan_first()
        self.s.set_auto_scan(False)
        self.still(self.card("Sol Ring", seed=3), 10)
        self.s.set_auto_scan(True)
        self.still(self.card("Sol Ring", seed=4), 30)
        self.assertEqual(len(self.started), 1)

    def test_repeated_space_on_the_same_card(self):
        self.scan_first()
        for _ in range(5):
            self.s.on_key(32)
            self.drain(self.s, wait=True)
        self.assertEqual(self.names(), ["Sol Ring"])
        self.assertIn("Already scanned", self.s.status)

    def test_space_on_a_new_card_still_works_when_stopped(self):
        self.s.set_auto_scan(False)
        self.empty()
        f = self.card("Counterspell")
        self.still(f, 6)
        self.s.on_key(32)
        self.drain(self.s, wait=True)
        self.s.on_key(32)  # pressed again by accident
        self.drain(self.s, wait=True)
        self.assertEqual(self.names(), ["Counterspell"])

    def test_review_screen_and_skip_do_not_retrigger(self):
        rec = self.s.rec
        real = rec.identify
        rec.identify = lambda card, locked=None: dict(candidates=[("Sol Ring", 60.0), ("Sol Talisman", 55.0)],
                                                      printings=[], confident=False, name_text="?", card=card)
        self.addCleanup(setattr, rec, "identify", real)
        self.empty()
        self.still(self.card("Sol Ring"), 40)
        self.assertEqual(len(self.started), 1)
        self.assertIsNotNone(self.s.review)
        self.s.on_key(ord("x"))  # skip
        self.assertIsNone(self.s.review)
        self.still(self.card("Sol Ring", seed=7), 30)
        self.assertEqual(len(self.started), 1)
        self.assertEqual(self.s.entries, [])

    def test_failed_read_can_be_retried_with_space(self):
        rec = self.s.rec
        real = rec.identify
        rec.identify = lambda card, locked=None: dict(candidates=[], printings=[], confident=False, name_text="",
                                                      card=card)
        self.empty()
        self.still(self.card("Sol Ring"), 30)
        self.assertEqual(len(self.started), 1)  # not retried automatically
        rec.identify = real
        self.addCleanup(setattr, rec, "identify", real)
        self.s.on_key(32)
        self.drain(self.s, wait=True)
        self.assertEqual(self.names(), ["Sol Ring"])

    def test_deck_duplicate_rejection_does_not_retrigger(self):
        st = self.s.deck_store
        deck = st.create_deck("Dup")
        st.add_card(deck, st.default_printing("Sol Ring"))
        self.s.start_deck_scan(deck)
        self.s.set_auto_scan(True)
        self.empty()
        self.still(self.card("Sol Ring"), 50)
        self.assertEqual(len(self.started), 1)
        self.assertEqual(self.s.target.entries, [])
        self.assertIn("Duplicate ignored", self.s.status)

    # ---- a new physical presentation: must scan
    def test_removed_then_presented_again_scans_again(self):
        """A second physical copy (or the same card shown again) after removal is a new scan."""
        self.scan_first()
        self.empty()
        self.still(self.card("Sol Ring", seed=5, dx=4), 12)
        self.assertEqual(self.names(), ["Sol Ring", "Sol Ring"])

    def test_stacking_different_cards_without_showing_the_mat(self):
        self.scan_first("Sol Ring")
        for name in ("Counterspell", "Lightning Bolt"):  # B on A, then C on B
            self.feed([hand(self.card(name), t) for t in (0.4, 0.5, 0.6)])  # put down by hand
            self.still(self.card(name), 12)
        self.assertEqual(self.names(), ["Sol Ring", "Counterspell", "Lightning Bolt"])

    def test_fast_stack_scanning(self):
        self.empty()
        order = ["Sol Ring", "Counterspell", "Lightning Bolt", "Llanowar Elves", "Swords to Plowshares", "Cultivate"]
        for i, name in enumerate(order):
            self.feed([self.card(name, seed=i, angle=3 + i)] * 2)
            self.still(self.card(name, seed=60 + i, angle=3 + i), scanner.STILL_FRAMES + 3)
        self.assertEqual(self.names(), order)

    def test_identical_copy_on_top_needs_R(self):
        self.scan_first()
        self.feed([hand(self.card("Sol Ring"), t) for t in (0.4, 0.5, 0.6)])
        self.still(self.card("Sol Ring", seed=9), 20)
        self.assertEqual(self.names(), ["Sol Ring"])  # looks identical: not counted on its own
        self.s.on_key(ord("r"))  # "this is another copy"
        self.drain(self.s, wait=True)
        self.assertEqual(self.names(), ["Sol Ring", "Sol Ring"])
        self.still(self.card("Sol Ring", seed=11), 20)
        self.assertEqual(self.names(), ["Sol Ring", "Sol Ring"])  # and no extra after that


class RealCaptureTests(unittest.TestCase):
    """Real webcam frames from the owner's scans (tests/fixtures/zone_*.jpg): pairs where the
    old scanner scanned the SAME physical card twice (hand / shift in between), and pairs where
    a different card really was stacked on top."""

    @classmethod
    def setUpClass(cls):
        from visual import load_index
        cls.fake = type("S", (), {})()
        cls.fake.rec = type("R", (), {"visual": load_index()})()

    def dist(self, tag):
        a, b = (cv2.imread(os.path.join(HERE, "fixtures", f"zone_{tag}_{x}.jpg")) for x in "ab")
        if a is None or b is None:
            self.skipTest(f"no fixture {tag}")
        fp = scanner.Scanner.presentation_fp
        return scanner.Scanner.fp_distance(fp(self.fake, a), fp(self.fake, b))

    def test_real_accidental_repeats_count_as_the_same_card(self):
        for tag in ("same_thought", "same_brainstorm", "same_burningtree"):
            with self.subTest(tag):
                self.assertLessEqual(self.dist(tag), scanner.NEW_CARD_DIST)

    def test_real_stacked_cards_count_as_new(self):
        for tag in ("diff_island_swamp", "diff_swiftfoot_brotherhood"):
            with self.subTest(tag):
                self.assertGreater(self.dist(tag), scanner.NEW_CARD_DIST)


if __name__ == "__main__":
    unittest.main()
