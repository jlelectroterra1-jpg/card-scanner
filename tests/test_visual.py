"""Picture-recognition accuracy on webcam-like shots taken from a distance:
card ~200-300 px tall in a 1920x1080 frame, blur, lighting shifts, glare and
sometimes a finger over a corner.

    python tests/test_visual.py [n_queries] [--index-from-downloaded N]
"""
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
import visual  # noqa: E402


def desk(rng, w=1920, h=1080):
    """Busy playmat-like background."""
    small = rng.integers(0, 255, (9, 16, 3), dtype=np.uint8)
    bg = cv2.resize(small, (w, h), interpolation=cv2.INTER_CUBIC)
    return cv2.add(cv2.GaussianBlur(bg, (0, 0), 25), rng.integers(0, 25, (h, w, 3), dtype=np.uint8))


def far_shot(card, bg, rng):
    h0, w0 = bg.shape[:2]
    ch = rng.uniform(200, 300)
    cw = ch * 63 / 88
    cx, cy = w0 / 2 + rng.uniform(-30, 30), h0 / 2 + rng.uniform(-30, 30)
    ang = np.radians(rng.uniform(-12, 12))
    pts = np.array([[-cw / 2, -ch / 2], [cw / 2, -ch / 2], [cw / 2, ch / 2], [-cw / 2, ch / 2]])
    R = np.array([[np.cos(ang), -np.sin(ang)], [np.sin(ang), np.cos(ang)]])
    dst = (pts @ R.T + [cx, cy]).astype(np.float32) + rng.uniform(-5, 5, (4, 2)).astype(np.float32)
    h, w = card.shape[:2]
    M = cv2.getPerspectiveTransform(np.float32([[0, 0], [w, 0], [w, h], [0, h]]), dst)
    warped = cv2.warpPerspective(card, M, (w0, h0))
    mask = cv2.warpPerspective(np.full((h, w), 255, np.uint8), M, (w0, h0))
    out = bg.copy()
    out[mask > 0] = warped[mask > 0]
    # Room lighting / camera colour: applies to the card *and* the empty-desk photo.
    gain, offset = rng.uniform(0.7, 1.25), rng.uniform(-25, 25)
    wb = np.array([rng.uniform(0.85, 1.1), 1.0, rng.uniform(0.85, 1.15)])
    light = lambda im: np.clip(im.astype(np.float32) * gain * wb + offset, 0, 255)
    empty = cv2.GaussianBlur(light(bg).astype(np.uint8), (0, 0), 1.2)
    out = light(out)
    if rng.random() < 0.5:  # glare spot
        gx, gy = cx + rng.uniform(-cw / 3, cw / 3), cy + rng.uniform(-ch / 3, ch / 3)
        yy, xx = np.mgrid[0:h0, 0:w0]
        out += 120 * np.exp(-(((xx - gx) / (cw * 0.25)) ** 2 + ((yy - gy) / (ch * 0.12)) ** 2))[..., None]
    out = np.clip(out, 0, 255).astype(np.uint8)
    if rng.random() < 0.4:  # finger over a bottom corner
        fx = int(cx + rng.choice([-1, 1]) * cw * 0.45)
        fy = int(cy + ch * rng.uniform(0.3, 0.5))
        cv2.ellipse(out, (fx, fy), (int(cw * 0.18), int(ch * 0.2)), rng.uniform(0, 180), 0, 360, (120, 150, 205), -1)
    out = cv2.GaussianBlur(out, (0, 0), rng.uniform(0.8, 1.8))
    out = cv2.add(out, rng.integers(0, 10, out.shape, dtype=np.uint8))
    out = cv2.imdecode(cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, int(rng.uniform(60, 85))])[1], 1)
    zone = (int(cx - ch * 0.75), int(cy - ch * 0.75), int(cx + ch * 0.75), int(cy + ch * 0.75))
    return out, zone, empty


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 100
    db = CardDB()
    vi = visual.VisualIndex()
    rng = np.random.default_rng(5)
    ids = [str(i) for i in rng.choice(vi.ids, n, replace=False)]
    top1 = top5 = 0
    t_total = 0.0
    rows = []  # (correct?, margin)
    for cid in ids:
        card = db.by_id(cid)
        if card is None or not card.get("image_normal"):
            continue
        shot, (x0, y0, x1, y1), empty = far_shot(get_img(card), desk(rng), rng)
        zone, zbg = shot[y0:y1, x0:x1], empty[y0:y1, x0:x1]
        t = time.time()
        straight = Recognizer.find_card(zone, zbg)
        if straight is None:
            straight = cv2.resize(zone, (630, 880))
        hits = vi.query(straight, k=5)
        t_total += time.time() - t
        names = [h[0] for h in hits]
        ok1 = names[0] == card["name"]
        top1 += ok1
        top5 += card["name"] in names
        margin = hits[0][2] - (hits[1][2] if len(hits) > 1 else hits[0][2] - 1)
        rows.append((ok1, margin))
        if not ok1:
            print(f"XX {card['name'][:28]:28} {card['set_code']:>5} -> {names[0][:28]:28} score={hits[0][2]:.2f} gap={margin:.2f}"
                  f"  rank={names.index(card['name']) + 1 if card['name'] in names else '-'}")
    print()
    print(f"index {len(vi.ids):,} artworks | top1 {top1}/{len(rows)}  top5 {top5}/{len(rows)}  {1000 * t_total / len(rows):.0f} ms/card")
    for gap in (0.05, 0.1, 0.15, 0.2, 0.3):
        sure = [ok for ok, m in rows if m >= gap]
        print(f"  if 'sure' means gap >= {gap:.2f}: sure on {len(sure)}/{len(rows)}, of which wrong {len(sure) - sum(sure)}")

if __name__ == "__main__":
    main()
