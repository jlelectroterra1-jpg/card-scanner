"""Render the app's screens to PNGs for a visual check (no camera, temporary data).

    python tests/render_ui.py OUT_DIR

Uses a copy of data/session.json in a temporary folder; your real collection and
scan list are never touched."""
import json
import os
import shutil
import sys
import tempfile

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import scanner  # noqa: E402
from test_background import ZONE, fake_open_camera, lit, playmat  # noqa: E402


def main(out):
    os.makedirs(out, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="tmp_ui_", dir=HERE)
    try:
        real_session = os.path.join(os.path.dirname(HERE), "data", "session.json")
        if os.path.exists(real_session):
            shutil.copy(real_session, os.path.join(tmp, "session.json"))
        scanner.SETTINGS_PATH = os.path.join(tmp, "settings.json")
        scanner.SESSION_PATH = os.path.join(tmp, "session.json")
        scanner.BACKGROUND_DIR = os.path.join(tmp, "bg")
        scanner.USER_DB_PATH = os.path.join(tmp, "user.db")
        scanner.EXPORT_DIR = os.path.join(tmp, "exports")
        with open(scanner.SETTINGS_PATH, "w") as f:
            json.dump({"camera": 2, "zone": ZONE, "usd_zar": 17.25}, f)
        scanner.Scanner.open_camera = fake_open_camera
        s = scanner.Scanner(type("A", (), {"camera": None})())
        frame = lit(playmat())
        s.last_frame = frame
        s.learn_background()
        shot = lambda name: cv2.imwrite(os.path.join(out, name), s.draw_window(frame))
        shot("1_scanner.png")
        s.add_to_collection()
        shot("2_added_dialog.png")
        s.dialog.finish(False)  # keep list
        s.dialog = None
        s.switch_tab("collection")
        c = s.collection_screen
        shot("3_collection.png")
        c.selected = c.ids[0]
        c.bump()
        shot("4_detail.png")
        s.set_currency("ZAR")
        shot("5_detail_zar.png")
        c.search = "go"
        c.focus = "search"
        c.refresh()
        c.open_dd = "sort"
        c.bump()
        shot("6_search_sort_dropdown.png")
        c.open_dd = None
        c.search, c.focus = "", None
        c.filters = {"finish": "foil"}
        c.selected = None
        c.refresh()
        shot("7_filter_foil_stats.png")
        c.filters = {}
        c.refresh()
        c.manage()
        shot("8_manage_dialog.png")
        s.dialog = None
        s.switch_tab("decks")
        shot("9_decks_placeholder.png")
        s.userdb.close()
        print("rendered to", out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "ui_shots"))
