"""Learned-background tests: the empty playmat must never be scanned, a real card must
still be, and the background must survive restarts / be invalidated when the setup changes.

    python -m unittest tests.test_background -v

Uses a temporary folder for settings/backgrounds/session, never your real data.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import scanner  # noqa: E402
from recognizer import Recognizer  # noqa: E402

ZONE = [560, 300, 760, 560]  # 200 x 260 scan box in a 1280 x 720 frame
_shared = {}


def playmat(seed=0):
    """Busy colourful playmat with a dark, card-shaped rectangle painted in it (like the
    owner's real mat, whose artwork used to be mistaken for a card)."""
    rng = np.random.default_rng(seed)
    small = rng.integers(0, 255, (12, 20, 3), dtype=np.uint8)
    img = cv2.GaussianBlur(cv2.resize(small, (1280, 720), interpolation=cv2.INTER_CUBIC), (0, 0), 6)
    for _ in range(40):  # brush strokes / texture
        p = tuple(int(v) for v in rng.integers(0, [1280, 720]))
        q = tuple(int(v) for v in rng.integers(0, [1280, 720]))
        cv2.line(img, p, q, [int(c) for c in rng.integers(0, 255, 3)], int(rng.integers(2, 9)), cv2.LINE_AA)
    # the painted "card": a dark card-aspect rectangle with a lighter inside, inside the scan box
    x0, y0 = ZONE[0] + 40, ZONE[1] + 40
    cv2.rectangle(img, (x0, y0), (x0 + 110, y0 + 154), (25, 20, 30), -1)
    cv2.rectangle(img, (x0 + 8, y0 + 8), (x0 + 102, y0 + 70), (140, 120, 160), -1)
    return img


def lit(img, gain=1.0, offset=0.0, noise=3.0, seed=0):
    rng = np.random.default_rng(seed)
    out = img.astype(np.float32) * gain + offset + rng.normal(0, noise, img.shape)
    return np.clip(out, 0, 255).astype(np.uint8)


def with_card(frame, card_img, angle=4.0):
    """Lay a real card image in the scan box."""
    h, w = 230, int(230 * 63 / 88)
    cx, cy = (ZONE[0] + ZONE[2]) / 2, (ZONE[1] + ZONE[3]) / 2
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


def fake_open_camera(self, index):
    self.cap = None
    self.settings["camera"] = index
    self.cam_index = index
    self.background = None
    if hasattr(self, "zone"):
        self.load_background()


class BackgroundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Load the heavy parts (card database, OCR, picture model) once and reuse them.
        from carddb import CardDB
        from visual import load_index
        _shared["db"] = CardDB()
        _shared["rec"] = Recognizer(_shared["db"], load_index())
        cls._orig = (scanner.CardDB, scanner.Recognizer, scanner.Scanner.open_camera)
        scanner.CardDB = lambda: _shared["db"]
        scanner.Recognizer = lambda db, vis: _shared["rec"]
        scanner.Scanner.open_camera = fake_open_camera
        import visual
        cls._orig_load_index = visual.load_index
        visual.load_index = lambda: _shared["rec"].visual  # reuse the already-loaded model

    @classmethod
    def tearDownClass(cls):
        scanner.CardDB, scanner.Recognizer, scanner.Scanner.open_camera = cls._orig
        import visual
        visual.load_index = cls._orig_load_index

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tmp_bg_", dir=HERE)
        self.addCleanup(shutil.rmtree, self.tmp, True)  # registered first = runs last, after DBs close
        scanner.SETTINGS_PATH = os.path.join(self.tmp, "settings.json")
        scanner.SESSION_PATH = os.path.join(self.tmp, "session.json")
        scanner.BACKGROUND_DIR = os.path.join(self.tmp, "backgrounds")
        scanner.DEBUG_DIR = os.path.join(self.tmp, "debug")
        scanner.EXPORT_DIR = os.path.join(self.tmp, "exports")
        scanner.USER_DB_PATH = os.path.join(self.tmp, "user.db")
        with open(scanner.SETTINGS_PATH, "w") as f:
            json.dump({"camera": 2, "zone": ZONE}, f)

    def new_scanner(self):
        s = scanner.Scanner(type("Args", (), {"camera": None})())
        self.addCleanup(lambda: s.userdb and s.userdb.close())
        return s

    def feed(self, s, frames, manual_scans=None):
        """Run frames through the scanner's trigger logic; return how many scans started."""
        started = []
        real_start = s.start_scan

        def spy(frame, manual=False):
            started.append(manual)
            real_start(frame, manual)
        s.start_scan = spy
        for f in frames:
            s.last_frame = f
            if s.background is not None:
                if not s.bg_verified:
                    s.verify_background(f)
                if s.background is not None:
                    s.update_trigger(f)
            self.drain(s)
        self.drain(s, wait=True)
        return started

    @staticmethod
    def drain(s, wait=False):
        deadline = time.time() + (20 if wait else 0)
        while True:
            while not s.results.empty():
                s.handle_result(s.results.get())
            if not (wait and s.busy and time.time() < deadline):
                return
            time.sleep(0.02)

    def learn(self, s, frame):
        s.last_frame = frame
        s.learn_background()

    # ---- tests ------------------------------------------------------------------

    def test_learned_background_does_not_trigger(self):
        s = self.new_scanner()
        mat = playmat()
        self.learn(s, lit(mat, seed=1))
        # the same empty playmat, under changing light, held perfectly still
        frames = [lit(mat, gain=g, offset=o, seed=i) for i, (g, o) in
                  enumerate([(1.0, 0), (0.8, -10), (1.25, 12), (0.7, 0), (1.35, 5)] * 8)]
        started = self.feed(s, frames)
        self.assertEqual(s.review, None)
        self.assertEqual(len(s.entries), 0)
        # even if a scan were forced through the auto path, it would find no card
        for g in (0.75, 1.0, 1.3):
            card, why = Recognizer.find_card_checked(s.zone_crop(lit(mat, gain=g)), s.background)
            self.assertIsNone(card, f"playmat at {g}x light was taken for a card")
        self.assertEqual(started, [], "the empty background started a scan")

    def test_card_over_background_still_triggers(self):
        db = _shared["db"]
        s = self.new_scanner()
        mat = playmat()
        self.learn(s, lit(mat, seed=1))
        name = "Lightning Bolt"
        card_id = next(r[0] for r in db.db.execute(
            "SELECT id FROM cards WHERE name = ? AND lang = 'en' ORDER BY released_at DESC", (name,))
            if os.path.exists(os.path.join(ROOT, "data", "img", r[0] + ".jpg")))
        card_img = cv2.imread(os.path.join(ROOT, "data", "img", card_id + ".jpg"))
        frames = [lit(mat, seed=i) for i in range(5)] + [lit(with_card(mat, card_img), seed=10 + i) for i in range(12)]
        started = self.feed(s, frames)
        self.assertEqual(len(started), 1, "a card in the box should start exactly one scan")
        self.assertEqual([db.by_id(e["id"])["name"] for e in s.entries], [name])

    def test_background_persists_after_restart(self):
        s = self.new_scanner()
        mat = playmat()
        self.learn(s, lit(mat, seed=1))
        saved = s.background.copy()
        self.assertTrue(os.path.exists(os.path.join(scanner.BACKGROUND_DIR, "camera2.png")))
        s2 = self.new_scanner()  # "restart"
        self.assertEqual(s2.bg_state, "ok")
        self.assertIsNotNone(s2.background)
        self.assertTrue(np.array_equal(s2.background, saved))
        self.assertEqual(self.feed(s2, [lit(mat, gain=1.1, seed=i) for i in range(20)]), [])
        self.assertEqual(s2.bg_state, "ok")

    def test_relearn_after_changing_playmat(self):
        s = self.new_scanner()
        self.learn(s, lit(playmat(0), seed=1))
        first = s.background.copy()
        new_mat = playmat(7)
        self.learn(s, lit(new_mat, seed=2))  # Background > Relearn
        self.assertFalse(np.array_equal(first, s.background))
        self.assertEqual(self.feed(s, [lit(new_mat, gain=0.9, seed=i) for i in range(20)]), [])
        s2 = self.new_scanner()
        self.assertTrue(np.array_equal(s2.background, s.background), "relearned background wasn't saved")

    def test_moving_the_box_invalidates_saved_background(self):
        s = self.new_scanner()
        mat = playmat()
        self.learn(s, lit(mat, seed=1))
        # box changed outside the scanner (e.g. edited settings): don't silently use the old photo
        with open(scanner.SETTINGS_PATH) as f:
            settings = json.load(f)
        settings["zone"] = [600, 300, 800, 560]
        with open(scanner.SETTINGS_PATH, "w") as f:
            json.dump(settings, f)
        s2 = self.new_scanner()
        self.assertEqual(s2.bg_state, "stale")
        self.assertIsNone(s2.background)
        # drawing a new box on the empty desk learns (and saves) a background for it
        s2.last_frame = lit(mat, seed=3)
        s2.scale = 0.75
        for ev, x, y in ((scanner.cv2.EVENT_LBUTTONDOWN, 450, 240), (scanner.cv2.EVENT_MOUSEMOVE, 600, 435),
                         (scanner.cv2.EVENT_LBUTTONUP, 600, 435)):
            s2.on_mouse(ev, x, y, 0, None)
        self.assertEqual(s2.bg_state, "ok")
        self.assertEqual(s2.background.shape[:2], (s2.zone[3] - s2.zone[1], s2.zone[2] - s2.zone[0]))
        s3 = self.new_scanner()
        self.assertEqual(s3.bg_state, "ok")
        self.assertEqual(s3.zone, s2.zone)

    def test_camera_switch_uses_that_cameras_background(self):
        s = self.new_scanner()
        self.learn(s, lit(playmat(), seed=1))
        s.open_camera(0)
        self.assertIsNone(s.background)
        self.assertIsNone(s.bg_state)
        s.open_camera(2)
        self.assertEqual(s.bg_state, "ok")

    def test_clear_background(self):
        s = self.new_scanner()
        self.learn(s, lit(playmat(), seed=1))
        s.clear_background()
        self.assertIsNone(s.background)
        self.assertFalse(os.path.exists(os.path.join(scanner.BACKGROUND_DIR, "camera2.png")))
        self.assertIsNone(self.new_scanner().background)

    def test_changed_view_after_restart_is_flagged(self):
        s = self.new_scanner()
        self.learn(s, lit(playmat(0), seed=1))
        s2 = self.new_scanner()
        s2.verify_background(lit(playmat(3), seed=2))  # camera moved / different mat
        self.assertEqual(s2.bg_state, "check")
        s3 = self.new_scanner()
        s3.verify_background(lit(playmat(0), gain=1.25, seed=4))  # same mat, brighter room
        self.assertEqual(s3.bg_state, "ok")

    def test_real_scans_if_available(self):
        """On the owner's real scan captures (local only): empty playmat never passes the
        card check; real cards always do."""
        d = os.path.join(ROOT, "data", "debug")
        if not os.path.isdir(d):
            self.skipTest("no real scan captures on this machine")
        empty, cards = [], []
        for f in sorted(os.listdir(d)):
            if not f.endswith(".json"):
                continue
            with open(os.path.join(d, f)) as fh:
                j = json.load(fh)
            z = cv2.imread(os.path.join(d, f[:-5] + "_zone.jpg"))
            if z is None or z.shape[:2] != (187, 146):
                continue
            names = {c[0] for c in j["candidates"][:2]}
            if not j["confident"] and names & {"Altar of Shadows", "Rakdos Joins Up", "Open the Vaults"}:
                empty.append(z)
            elif j["confident"]:
                cards.append(z)
        from recognizer import changed_fraction
        if len(empty) < 3 or not cards:
            self.skipTest("not enough real captures")
        ref = empty[0]
        empty = [z for z in empty[1:] if changed_fraction(z, ref) < 0.3]  # drop mislabelled ones with a card in
        for z in empty:
            for g in (0.75, 1.0, 1.3):
                card, _ = Recognizer.find_card_checked(lit(z, gain=g, noise=0), ref)
                self.assertIsNone(card)
        passed = sum(Recognizer.find_card_checked(z, ref)[0] is not None for z in cards)
        self.assertEqual(passed, len(cards))


if __name__ == "__main__":
    unittest.main(verbosity=2)
