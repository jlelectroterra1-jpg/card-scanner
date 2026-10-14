"""Start / Stop Scan: automatic scanning starts OFF, Start Scan / P turns it on, Stop
Scan turns it off without touching the camera, background, scan list, deck session or
destination, and SPACE still scans one card while it is off.

    python -m unittest tests.test_scan_control -v

Uses a temporary folder (settings, backgrounds, session, user.db, deck session) and the
fake playmat frames from test_background - never your real data.
"""
import json
import os
import sys
import unittest

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import scanner  # noqa: E402
import test_background as tb  # noqa: E402
from test_background import lit, playmat, with_card  # noqa: E402

B = tb.BackgroundTests.__dict__


def read_json(path):
    with open(path) as f:
        return json.load(f)


class FakeCap:
    def __init__(self):
        self.released = False

    def read(self):
        return True, None

    def release(self):
        self.released = True


class ScanControlTests(unittest.TestCase):
    # same heavy-model sharing and temporary folders as the background tests
    setUpClass = B["setUpClass"]
    tearDownClass = B["tearDownClass"]
    new_scanner, drain, learn = B["new_scanner"], B["drain"], B["learn"]

    def setUp(self):
        B["setUp"](self)
        saved = scanner.DECK_SCAN_PATH
        scanner.DECK_SCAN_PATH = os.path.join(self.tmp, "deck_scan.json")
        self.addCleanup(setattr, scanner, "DECK_SCAN_PATH", saved)
        self.mat = playmat()
        db = tb._shared["db"]
        self.card_ids = {}
        for name in ("Lightning Bolt", "Counterspell"):
            self.card_ids[name] = next(r[0] for r in db.db.execute(
                "SELECT id FROM cards WHERE name = ? AND lang = 'en' ORDER BY released_at DESC", (name,))
                if os.path.exists(os.path.join(ROOT, "data", "img", r[0] + ".jpg")))

    def card_frames(self, name, n=12):
        img = cv2.imread(os.path.join(ROOT, "data", "img", self.card_ids[name] + ".jpg"))
        return [lit(self.mat, seed=i) for i in range(4)] + [lit(with_card(self.mat, img), seed=20 + i) for i in range(n)]

    def run_frames(self, s, frames):
        """What the main loop does with each camera frame (without forcing scanning on)."""
        started = []
        real = s.start_scan

        def spy(frame, manual=False):
            started.append(manual)
            real(frame, manual)
        s.start_scan = spy
        for f in frames:
            s.last_frame = f
            if s.background is not None:
                s.update_trigger(f)
            self.drain(s)
        self.drain(s, wait=True)
        s.start_scan = real
        return started

    def names(self, s):
        return [tb._shared["db"].by_id(e["id"])["name"] for e in s.entries]

    def ready(self):
        s = self.new_scanner()
        self.learn(s, lit(self.mat, seed=1))
        return s

    # ---- tests ---------------------------------------------------------------------

    def test_starts_off(self):
        s = self.ready()
        self.assertFalse(s.auto_scan)
        view, _ = s.panel_view()
        self.assertFalse(view["auto_scan"])
        s.draw_panel(np.zeros((scanner.VIEW_H + scanner.FOOTER_H, scanner.VIEW_W + scanner.PANEL_W, 3), np.uint8))
        self.assertIn("scan_toggle", [b[4] for b in s.panel_buttons])

    def test_no_auto_scans_while_stopped(self):
        s = self.ready()
        started = self.run_frames(s, self.card_frames("Lightning Bolt"))
        self.assertEqual(started, [])
        self.assertEqual(s.entries, [])

    def test_start_scan_enables_and_stop_disables(self):
        s = self.ready()
        s.set_auto_scan(True)
        self.assertEqual(self.run_frames(s, self.card_frames("Lightning Bolt")), [False])
        self.assertEqual(self.names(s), ["Lightning Bolt"])
        s.set_auto_scan(False)
        self.assertEqual(self.run_frames(s, self.card_frames("Counterspell")), [])
        self.assertEqual(self.names(s), ["Lightning Bolt"])  # list untouched, nothing added
        s.set_auto_scan(True)  # resume: the next card is scanned again
        self.run_frames(s, [lit(self.mat, seed=3)] * 3 + self.card_frames("Counterspell"))
        self.assertEqual(self.names(s), ["Lightning Bolt", "Counterspell"])

    def test_scan_already_running_when_stopped_is_not_added(self):
        s = self.ready()
        s.set_auto_scan(True)
        frame = self.card_frames("Lightning Bolt")[-1]
        s.last_frame = frame
        s.start_scan(frame)  # an automatic scan in progress...
        s.set_auto_scan(False)  # ...when Stop Scan is pressed
        self.drain(s, wait=True)
        self.assertEqual(s.entries, [])
        self.assertFalse(s.busy)

    def test_stop_keeps_camera_background_session_and_destination(self):
        s = self.ready()
        s.cap = FakeCap()
        cam, bg = s.cam_index, s.background.copy()
        s.set_auto_scan(True)
        self.run_frames(s, self.card_frames("Lightning Bolt"))
        entries = json.dumps(s.entries)
        dest = s.destination_id()
        settings_before = read_json(scanner.SETTINGS_PATH)
        s.on_key(ord("p"))  # Stop Scan
        self.assertFalse(s.auto_scan)
        self.assertFalse(s.cap.released)
        self.assertEqual(s.cam_index, cam)
        self.assertTrue(np.array_equal(s.background, bg))
        self.assertEqual(s.bg_state, "ok")
        self.assertEqual(json.dumps(s.entries), entries)
        with open(scanner.SESSION_PATH) as f:
            self.assertEqual(len(json.load(f)), 1)
        self.assertEqual(s.destination_id(), dest)
        self.assertEqual(read_json(scanner.SETTINGS_PATH), settings_before)
        # the live picture is still drawn while stopped
        frame = lit(self.mat, seed=5)
        canvas = s.draw_window(frame)
        view = cv2.resize(frame, (int(frame.shape[1] * s.scale), int(frame.shape[0] * s.scale)))
        region = canvas[scanner.NAV_H + 60:scanner.NAV_H + 120, 20:200].astype(int)
        self.assertLess(np.abs(region - view[60:120, 20:200].astype(int)).mean(), 2)

    def test_space_scans_one_card_while_stopped(self):
        s = self.ready()
        frames = self.card_frames("Lightning Bolt")
        self.run_frames(s, frames)
        self.assertEqual(s.entries, [])
        s.last_frame = frames[-1]
        s.on_key(32)  # SPACE
        self.drain(s, wait=True)
        self.assertEqual(self.names(s), ["Lightning Bolt"])
        self.assertFalse(s.auto_scan)  # still off afterwards
        self.assertEqual(self.run_frames(s, frames[-3:]), [])

    def test_p_key_and_panel_button_toggle(self):
        s = self.ready()
        s.on_key(ord("p"))
        self.assertTrue(s.auto_scan)
        s.on_key(ord("P"))
        self.assertFalse(s.auto_scan)
        s.draw_panel(np.zeros((scanner.VIEW_H + scanner.FOOTER_H, scanner.VIEW_W + scanner.PANEL_W, 3), np.uint8))
        x0, y0, x1, y1, _ = next(b for b in s.panel_buttons if b[4] == "scan_toggle")
        s.on_panel_click((x0 + x1) // 2, (y0 + y1) // 2)
        self.assertTrue(s.auto_scan)

    def test_deck_scan_session_survives_stop(self):
        s = self.ready()
        deck = s.deck_store.create_deck("Test Deck")
        s.start_deck_scan(deck)
        self.assertEqual(s.target.kind, "deck")
        self.assertFalse(s.auto_scan)  # entering Scan Deck doesn't start scanning
        s.set_auto_scan(True)
        self.run_frames(s, self.card_frames("Lightning Bolt"))
        self.assertEqual(len(s.target.entries), 1)
        s.set_auto_scan(False)
        self.assertEqual(s.target.kind, "deck")  # Stop Scan is not Finish
        self.assertEqual(len(s.target.entries), 1)
        with open(scanner.DECK_SCAN_PATH) as f:
            self.assertEqual(len(json.load(f)["entries"]), 1)
        self.assertEqual(self.run_frames(s, [lit(self.mat, seed=3)] * 3 + self.card_frames("Counterspell")), [])
        # restart: the unfinished deck session is offered again, scanning still OFF
        s.userdb.close()
        s2 = self.new_scanner()
        self.assertFalse(s2.auto_scan)
        self.assertEqual(s2.pending_deck_scan()["deck_id"], deck)

    def test_restart_returns_to_off(self):
        s = self.ready()
        s.set_auto_scan(True)
        s.save_session()
        scanner.save_json(scanner.SETTINGS_PATH, s.settings)
        self.assertNotIn("auto_scan", read_json(scanner.SETTINGS_PATH))
        s2 = self.new_scanner()
        self.assertFalse(s2.auto_scan)
        self.assertEqual(s2.bg_state, "ok")  # background still loaded after the restart


if __name__ == "__main__":
    unittest.main()
