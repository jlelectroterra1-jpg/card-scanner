"""Compare fingerprint methods for picture recognition (experiment, not part of the app)."""
import os, sys, time, gzip, json
import cv2, numpy as np, onnxruntime as ort
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT); sys.path.insert(0, HERE)
from build_visual_index import artwork_list, IMG_DIR
from carddb import CardDB
from recognizer import Recognizer
from test_synthetic import get_img
from test_visual import desk, far_shot
import visual

N_INDEX, N_Q = int(sys.argv[1]), int(sys.argv[2])
rng = np.random.default_rng(1)
arts = [a for a in artwork_list() if os.path.exists(os.path.join(IMG_DIR, a["id"] + ".jpg"))]
arts = [arts[i] for i in rng.choice(len(arts), N_INDEX, replace=False)]
imgs = [cv2.imread(os.path.join(IMG_DIR, a["id"] + ".jpg")) for a in arts]
names = np.array([a["name"] for a in arts])

db = CardDB(); qrng = np.random.default_rng(9)
qidx = qrng.choice(N_INDEX, N_Q, replace=False); queries = []
for i in qidx:
    c = db.by_id(arts[i]["id"])
    shot, (x0, y0, x1, y1), empty = far_shot(get_img(c), desk(qrng), qrng)
    st = Recognizer.find_card(shot[y0:y1, x0:x1], empty[y0:y1, x0:x1])
    queries.append(st if st is not None else cv2.resize(shot[y0:y1, x0:x1], (630, 880)))

dino = ort.InferenceSession(os.path.join(ROOT, "data/vis/dinov2_small.onnx"), providers=["CPUExecutionProvider"])
def dino_feats(cards, box, size=224, mode="cls"):
    out = []
    for i in range(0, len(cards), 32):
        x = np.stack([visual._prep(cv2.resize(visual._crop(c, box), (size, size), interpolation=cv2.INTER_AREA)) for c in cards[i:i+32]])
        h = dino.run(None, {"pixel_values": x})[0]
        f = h[:, 0] if mode == "cls" else np.concatenate([h[:, 0], h[:, 1:].mean(1)], 1)
        out.append(f)
    f = np.vstack(out); return f / np.linalg.norm(f, axis=1, keepdims=True)

def mb_feats(cards):
    return visual.fingerprints(cards)

def evaluate(name, fn):
    t = time.time(); I = fn(imgs); Q = fn(queries); secs = (time.time() - t) / (len(imgs) + len(queries))
    sims = Q @ I.T
    order = np.argsort(-sims, 1)
    top1 = sum(names[order[k, 0]] == names[qidx[k]] for k in range(N_Q))
    top5 = sum(names[qidx[k]] in names[order[k, :5]] for k in range(N_Q))
    print(f"{name:28} top1 {top1}/{N_Q}  top5 {top5}/{N_Q}   {secs*1000:.0f} ms/img", flush=True)

evaluate("mobilenet whole+art", mb_feats)
evaluate("mobilenet art only", lambda c: mb_feats(c)[:, 1280:])
evaluate("dino cls whole", lambda c: dino_feats(c, visual.WHOLE))
evaluate("dino cls art", lambda c: dino_feats(c, visual.ART))
def both(c):
    f = np.hstack([dino_feats(c, visual.WHOLE), dino_feats(c, visual.ART)]); return f / np.linalg.norm(f, axis=1, keepdims=True)
evaluate("dino cls whole+art", both)
evaluate("dino cls+mean whole", lambda c: (lambda f: f / np.linalg.norm(f, axis=1, keepdims=True))(dino_feats(c, visual.WHOLE, mode="both")))
