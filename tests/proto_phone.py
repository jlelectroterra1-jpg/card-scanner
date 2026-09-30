"""Python mirror of the phone pipeline (raw onnxruntime + hand-written pre/post-processing,
exactly what app.js does) to measure accuracy before porting."""
import os, sys, time
import cv2, numpy as np, onnxruntime as ort
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from carddb import CardDB
from test_synthetic import get_img

sess = ort.InferenceSession("web/models/en_rec.onnx")
chars = ["<blank>"] + [c or " " for c in sess.get_modelmeta().custom_metadata_map["character"].split(chr(10))]  # last entry "" is the space
print("classes", len(chars), sess.get_outputs()[0].shape)

def rec(bgr):
    h, w = bgr.shape[:2]
    nw = min(640, max(16, int(np.ceil(48 * w / h))))
    x = cv2.resize(bgr, (nw, 48)).astype(np.float32) / 255.0
    x = ((x - 0.5) / 0.5).transpose(2, 0, 1)[None]
    out = sess.run(None, {sess.get_inputs()[0].name: x})[0][0]
    idx = out.argmax(1); prob = out.max(1)
    s, last, ps = [], -1, []
    for i, p in zip(idx, prob):
        if i != last and i != 0:
            s.append(chars[i] if i < len(chars) else ""); ps.append(p)
        last = i
    return "".join(s).strip(), float(np.mean(ps)) if ps else 0.0

def phone_shot(card, rng):
    """Card photographed through a card-shaped guide: roughly aligned, slightly off."""
    W, H = 720, 1280
    bg = np.full((H, W, 3), (70, 95, 120), np.uint8); bg = cv2.add(bg, rng.integers(0, 30, bg.shape, dtype=np.uint8))
    gw = W * 0.80; gh = gw * 88 / 63; gx, gy = (W - gw) / 2, (H - gh) / 2
    s = rng.uniform(0.9, 1.02); ang = np.radians(rng.uniform(-4, 4))
    cx, cy = W / 2 + rng.uniform(-0.03, 0.03) * gw, H / 2 + rng.uniform(-0.03, 0.03) * gh
    pts = np.array([[-gw/2, -gh/2], [gw/2, -gh/2], [gw/2, gh/2], [-gw/2, gh/2]]) * s
    R = np.array([[np.cos(ang), -np.sin(ang)], [np.sin(ang), np.cos(ang)]])
    dst = (pts @ R.T + [cx, cy]).astype(np.float32)
    h, w = card.shape[:2]
    M = cv2.getPerspectiveTransform(np.float32([[0,0],[w,0],[w,h],[0,h]]), dst)
    wc = cv2.warpPerspective(card, M, (W, H)); m = cv2.warpPerspective(np.full((h,w),255,np.uint8), M, (W,H))
    out = bg.copy(); out[m > 0] = wc[m > 0]
    out = cv2.GaussianBlur(out, (3, 3), 0.7)
    out = cv2.imdecode(cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 75])[1], 1)
    return out, (gx, gy, gw, gh)

def read_name(frame, guide, db):
    gx, gy, gw, gh = guide
    best = ("", [], 0)
    for dy in (0.0, -0.02, 0.02, -0.04, 0.04):
        y0 = gy + (0.025 + dy) * gh; y1 = gy + (0.105 + dy) * gh
        crop = frame[int(max(0, y0)):int(y1), int(gx + 0.04 * gw):int(gx + 0.80 * gw)]
        text, p = rec(crop)
        c = db.match_name(text)
        if c and c[0][1] > best[2]:
            best = (text, c, c[0][1])
        if best[2] >= 95: break
    return best

db = CardDB(); rng = np.random.default_rng(7)
ids = [r[0] for r in db.db.execute("SELECT id FROM cards WHERE lang='en' AND image_normal IS NOT NULL ORDER BY RANDOM() LIMIT ?", (int(sys.argv[1]) if len(sys.argv) > 1 else 60,))]
ok = conf = wrong_conf = 0; t0 = time.time()
for cid in ids:
    c = db.by_id(cid); frame, guide = phone_shot(get_img(c), rng)
    text, cands, top = read_name(frame, guide, db)
    second = cands[1][1] if len(cands) > 1 else 0
    confident = top >= 97 or (top >= 85 and top - second >= 8)
    hit = bool(cands) and cands[0][0] == c["name"]
    ok += hit; conf += confident; wrong_conf += confident and not hit
    if not hit or not confident:
        print(("OK " if hit else "XX ") + ("C" if confident else "?"), c["name"][:30], c["set_code"], "| read:", repr(text[:30]), "->", cands[0][0][:30] if cands else None)
print(f"name {ok}/{len(ids)}  confident {conf}  confident-but-wrong {wrong_conf}  {1000*(time.time()-t0)/len(ids):.0f} ms/card")

# ---- variant B: find the card edges inside the guide, straighten, then read ----
if len(sys.argv) > 2 and sys.argv[2] == "edges":
    from recognizer import Recognizer, NAME_BOX
    def read_name_edges(frame, guide, db):
        gx, gy, gw, gh = guide
        m = 0.08
        x0, y0 = int(max(0, gx - m * gw)), int(max(0, gy - m * gh))
        region = frame[y0:int(gy + gh * (1 + m)), x0:int(gx + gw * (1 + m))]
        card = Recognizer.find_card(region, None)
        if card is None:
            return read_name(frame, guide, db)
        best = ("", [], 0)
        for img in (card, cv2.rotate(card, cv2.ROTATE_180)):
            for dy in (0, -0.012, 0.012):
                a, b, c_, d = NAME_BOX
                h, w = img.shape[:2]
                text, _ = rec(img[int((b + dy) * h):int((d + dy) * h), int(a * w):int(c_ * w)])
                cands = db.match_name(text)
                if cands and cands[0][1] > best[2]:
                    best = (text, cands, cands[0][1])
                if best[2] >= 95: return best
        return best
    rng = np.random.default_rng(7)
    ok = conf = wrong_conf = 0; t0 = time.time()
    for cid in ids:
        c = db.by_id(cid); frame, guide = phone_shot(get_img(c), rng)
        text, cands, top = read_name_edges(frame, guide, db)
        second = cands[1][1] if len(cands) > 1 else 0
        confident = top >= 97 or (top >= 85 and top - second >= 8)
        hit = bool(cands) and cands[0][0] == c["name"]
        ok += hit; conf += confident; wrong_conf += confident and not hit
    print(f"EDGES: name {ok}/{len(ids)}  confident {conf}  confident-but-wrong {wrong_conf}  {1000*(time.time()-t0)/len(ids):.0f} ms/card")
