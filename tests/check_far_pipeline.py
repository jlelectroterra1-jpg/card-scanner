"""Regression check: the full desktop pipeline on far-away synthetic webcam shots.

    python tests/check_far_pipeline.py [n]

Goes through the scanner's card-evidence gate (find_card_checked) and identify()."""
import os
import sys
import time

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
from carddb import CardDB  # noqa: E402
from recognizer import Recognizer  # noqa: E402
from test_synthetic import get_img  # noqa: E402
from test_visual import desk, far_shot  # noqa: E402
from visual import load_index  # noqa: E402


def main(n=120):
    db = CardDB()
    vi = load_index()
    rec = Recognizer(db, vi)
    rng = np.random.default_rng(21)
    ok = wrong = asked = nocard = 0
    reasons, times = {}, []
    for cid in [str(i) for i in rng.choice(vi.ids, n, replace=False)]:
        c = db.by_id(cid)
        if not c or not c.get("image_normal"):
            continue
        shot, (x0, y0, x1, y1), empty = far_shot(get_img(c), desk(rng), rng)
        t = time.time()
        card, why = Recognizer.find_card_checked(shot[y0:y1, x0:x1], empty[y0:y1, x0:x1])
        if card is None:
            nocard += 1
            reasons[why] = reasons.get(why, 0) + 1
            continue
        r = rec.identify(card)
        times.append(time.time() - t)
        top = r["printings"][0]["name"] if r["printings"] else None
        if r["confident"]:
            ok += top == c["name"]
            wrong += top != c["name"]
            if top != c["name"]:
                print("WRONG", c["name"], "->", top, r["how"])
        else:
            asked += 1
    print(f"far shots: right {ok}, wrong {wrong}, asked {asked}, rejected as no card {nocard} {reasons}"
          f" | median {np.median(times) * 1000:.0f} ms")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 120)
