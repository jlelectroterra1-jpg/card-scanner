"""Simulate webcam shots of real cards (Scryfall images on a fake desk) and check recognition."""
import os, sys, time, random
import cv2, numpy as np, requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from carddb import CardDB
from recognizer import Recognizer

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "img_cache")
os.makedirs(CACHE, exist_ok=True)
H = {"User-Agent": "HomeCardScanner/0.1"}

def get_img(card):
    p = os.path.join(CACHE, card["id"] + ".jpg")
    if not os.path.exists(p):
        open(p, "wb").write(requests.get(card["image_normal"], headers=H, timeout=30).content)
        time.sleep(0.1)
    return cv2.imread(p)

def fake_photo(card_img, rng, upside_down=False):
    bg = np.full((720, 1280, 3), (60, 90, 120), np.uint8)
    bg = cv2.add(bg, rng.integers(0, 40, bg.shape, dtype=np.uint8))
    bg = cv2.GaussianBlur(bg, (9, 1), 0)  # streaky "wood"
    card = cv2.rotate(card_img, cv2.ROTATE_180) if upside_down else card_img
    ch = rng.uniform(330, 420)  # card height in pixels, typical for a C270 over a desk
    cw = ch * 63 / 88
    cx, cy, ang = 640 + rng.uniform(-40, 40), 360 + rng.uniform(-20, 20), np.radians(rng.uniform(-8, 8))
    pts = np.array([[-cw/2, -ch/2], [cw/2, -ch/2], [cw/2, ch/2], [-cw/2, ch/2]])
    R = np.array([[np.cos(ang), -np.sin(ang)], [np.sin(ang), np.cos(ang)]])
    dst = (pts @ R.T + [cx, cy]).astype(np.float32)
    dst += rng.uniform(-6, 6, dst.shape).astype(np.float32)  # slight perspective
    h, w = card.shape[:2]
    M = cv2.getPerspectiveTransform(np.float32([[0, 0], [w, 0], [w, h], [0, h]]), dst)
    warped = cv2.warpPerspective(card, M, (1280, 720))
    mask = cv2.warpPerspective(np.full((h, w), 255, np.uint8), M, (1280, 720))
    out = bg.copy(); out[mask > 0] = warped[mask > 0]
    out = cv2.GaussianBlur(out, (3, 3), 0.8)
    out = cv2.add(out, rng.integers(0, 12, out.shape, dtype=np.uint8))
    return bg, cv2.imdecode(cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 70])[1], 1)

def main(n=25, seed=1):
    db = CardDB(); rec = Recognizer(db); rng = np.random.default_rng(seed); random.seed(seed)
    ids = [r[0] for r in db.db.execute(
        "SELECT id FROM cards WHERE lang='en' AND image_normal IS NOT NULL AND released_at > '1995' ORDER BY RANDOM() LIMIT ?", (n,))]
    ok_name = ok_print = conf = 0; t_total = 0
    for i, cid in enumerate(ids):
        card = db.by_id(cid)
        bg, photo = fake_photo(get_img(card), rng, upside_down=(i % 5 == 4))
        t = time.time()
        zone = (slice(100, 620), slice(380, 900))  # the scan zone the user would draw
        straight = rec.find_card(photo[zone], bg[zone])
        r = rec.identify(straight) if straight is not None else None
        t_total += time.time() - t
        got = r["printings"][0] if r and r["printings"] else None
        name_ok = got is not None and got["name"] == card["name"]
        print_ok = got is not None and got["id"] == card["id"]
        ok_name += name_ok; ok_print += print_ok; conf += bool(r and r["confident"])
        flag = "OK " if name_ok else "XX "
        print(f"{flag}{'P' if print_ok else ' '} {'C' if r and r['confident'] else '?'}  {card['name'][:32]:32} {card['set_code']:>5} #{card['collector_number']:<5}"
              f" -> {(got['name'][:28] + ' ' + got['set_code'] + ' #' + got['collector_number']) if got else None} | ocr={r['name_text'][:30] if r else ''!r} foot={r['footer_text'][:30] if r else ''!r}")
    print(f"\nname correct {ok_name}/{n}, exact printing {ok_print}/{n}, confident {conf}/{n}, avg {t_total/n*1000:.0f} ms/card")

if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 25)
