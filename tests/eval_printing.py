"""Printing (artwork) accuracy on far-away webcam-like shots of a KNOWN printing.

    python tests/eval_printing.py [n] [--old]

Picks cards that have several different-looking printings, renders one printing as a
far shot (like a webcam ~60 cm up), recognises it with the full pipeline, then checks
the chosen printing:
  exact      = the very printing photographed
  same look  = a printing with the same art + frame (indistinguishable by picture)
  conf-wrong = presented as certain but a DIFFERENT artwork (the bad case)
With --old it also scores the previous method (colour thumbnail + newest-first)."""
import os
import sys
import time

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
from carddb import CardDB  # noqa: E402
from printing import SAME_LOOK, PrintingMatcher  # noqa: E402
from recognizer import Recognizer  # noqa: E402
from test_visual import desk, far_shot  # noqa: E402
from visual import load_index  # noqa: E402


def sample(db, matcher, n, rng):
    """Random English printings of cards with >= 2 different looks (and a local picture)."""
    names = [r[0] for r in db.db.execute(
        """SELECT name FROM cards WHERE lang = 'en' GROUP BY name HAVING COUNT(*) >= 3 ORDER BY RANDOM() LIMIT ?""",
        (n * 4,))]
    out = []
    for name in names:
        prints = [p for p in matcher.candidates(name) if os.path.exists(os.path.join(ROOT, "data", "img", p["id"] + ".jpg"))]
        if len(prints) < 2:
            continue
        embs = [matcher.clean_embedding(p["id"]) for p in prints]
        looks = 0
        for i, e in enumerate(embs):
            if all(float(e @ embs[j]) < SAME_LOOK for j in range(i)):
                looks += 1
        if looks >= 2:
            out.append(prints[int(rng.integers(len(prints)))])
        if len(out) == n:
            break
    return out


def main(n=150, old=False):
    db = CardDB()
    vi = load_index()
    rec = Recognizer(db, vi)
    m = PrintingMatcher(db, vi)
    rng = np.random.default_rng(7)
    db.db.execute("SELECT 1")
    stats = dict(new=dict(exact=0, look=0, conf_wrong=0, uncertain=0, uncertain_right=0), old=dict(exact=0, look=0, conf_wrong=0))
    total = name_ok = 0
    times = []
    for truth in sample(db, m, n, rng):
        img = cv2.imread(os.path.join(ROOT, "data", "img", truth["id"] + ".jpg"))
        big = cv2.resize(img, (488, 680), interpolation=cv2.INTER_CUBIC)
        shot, (x0, y0, x1, y1), empty = far_shot(big, desk(rng), rng)
        card, _ = Recognizer.find_card_checked(shot[y0:y1, x0:x1], empty[y0:y1, x0:x1])
        if card is None:
            continue
        total += 1
        t = time.time()
        r = rec.identify(card)
        times.append(time.time() - t)
        if not r["printings"] or r["printings"][0]["name"] != truth["name"]:
            continue
        name_ok += 1
        te = m.clean_embedding(truth["id"])

        def same_look(pid):
            e = m.clean_embedding(pid)
            return pid == truth["id"] or (e is not None and float(e @ te) >= SAME_LOOK)

        ch = r.get("printing") or {}
        sel = r["printings"][0]["id"]
        s = stats["new"]
        s["exact"] += sel == truth["id"]
        s["look"] += same_look(sel)
        if ch.get("uncertain"):
            s["uncertain"] += 1
            s["uncertain_right"] += same_look(sel)
        elif not same_look(sel):
            s["conf_wrong"] += 1
            print(f"  CONFIDENT BUT WRONG ART: {truth['name']} {truth['set_code']} #{truth['collector_number']} -> "
                  f"{r['printings'][0]['set_code']} #{r['printings'][0]['collector_number']}  ({ch.get('reason')})")
        if old:
            o = db.ranked_printings(truth["name"], r.get("footer_text", ""), None, r["card"], download=False)[0]["id"]
            so = stats["old"]
            so["exact"] += o == truth["id"]
            so["look"] += same_look(o)
            so["conf_wrong"] += not same_look(o)
    print(f"{total} far shots, card name right {name_ok} | median {np.median(times) * 1000:.0f} ms per card")
    s = stats["new"]
    print(f"NEW: exact printing {s['exact']}/{name_ok}, right artwork {s['look']}/{name_ok}, "
          f"confidently wrong artwork {s['conf_wrong']}, marked uncertain {s['uncertain']} "
          f"(of which the guess had the right artwork: {s['uncertain_right']})")
    if old:
        so = stats["old"]
        print(f"OLD: exact printing {so['exact']}/{name_ok}, right artwork {so['look']}/{name_ok}, "
              f"wrong artwork presented as certain {so['conf_wrong']}")
    return stats, name_ok


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    main(int(args[0]) if args else 150, "--old" in sys.argv)
