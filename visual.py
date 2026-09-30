"""Recognise a card by its picture (see build_visual_index.py), in two steps:

1. Shortlist: a MobileNetV2 fingerprint (a small general-purpose image network,
   taken just before its classifier) of the whole card and of the art box, compared
   with the fingerprints of every distinct artwork -> the 100 closest.
2. Close look: compare a small colour thumbnail of the card with each shortlisted
   card's image, ignoring the worst-matching quarter of the pixels (glare, a finger).

On webcam-like test shots of small, blurry cards this finds the right card about
nine times in ten; the name reader covers many of the rest."""
from functools import lru_cache
import os

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_SRC = os.path.join(HERE, "data", "vis", "mobilenetv2.onnx")
MODEL_PATH = os.path.join(HERE, "data", "vis", "mobilenetv2_feat.onnx")
MODEL_URL = "https://github.com/onnx/models/raw/main/validated/vision/classification/mobilenet/model/mobilenetv2-12.onnx"
INDEX_PATH = os.path.join(HERE, "data", "visual_index.npz")
IMG_DIR = os.path.join(HERE, "data", "img")

# Regions of a straightened card (x0, y0, x1, y1), as fractions.
WHOLE = (0.03, 0.02, 0.97, 0.98)   # a little inside the edge, so desk doesn't leak in
ART = (0.07, 0.10, 0.93, 0.56)
SIZE = 224
MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)

_session = None


def _ensure_model():
    if os.path.exists(MODEL_PATH):
        return
    import onnx
    import requests
    from onnx import helper
    os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
    if not os.path.exists(MODEL_SRC):
        print("Downloading image model (14 MB)...")
        with open(MODEL_SRC, "wb") as f:
            f.write(requests.get(MODEL_URL, timeout=120).content)
    m = onnx.load(MODEL_SRC)
    pool = [n for n in m.graph.node if n.op_type in ("GlobalAveragePool", "ReduceMean")][-1]
    m.graph.output.insert(0, helper.make_tensor_value_info(pool.output[0], onnx.TensorProto.FLOAT, None))
    onnx.save(m, MODEL_PATH)


def session():
    global _session
    if _session is None:
        import onnxruntime as ort
        _ensure_model()
        _session = ort.InferenceSession(MODEL_PATH, providers=["CPUExecutionProvider"])
    return _session


def _crop(img, box):
    h, w = img.shape[:2]
    x0, y0, x1, y1 = box
    return img[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)]


def _prep(img_bgr):
    rgb = cv2.cvtColor(cv2.resize(img_bgr, (SIZE, SIZE), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
    return (((rgb.astype(np.float32) / 255.0) - MEAN) / STD).transpose(2, 0, 1)


def fingerprints(cards_bgr, batch=64):
    """cards_bgr: list of card images (any size, portrait). Returns (N, 2560) float32,
    each half L2-normalised: [whole card | art box]."""
    s = session()
    out_name = s.get_outputs()[0].name
    in_name = s.get_inputs()[0].name
    feats = []
    for i in range(0, len(cards_bgr), batch):
        chunk = cards_bgr[i:i + batch]
        x = np.stack([_prep(_crop(c, WHOLE)) for c in chunk] + [_prep(_crop(c, ART)) for c in chunk])
        f = s.run([out_name], {in_name: x})[0].reshape(len(x), -1)
        f /= np.linalg.norm(f, axis=1, keepdims=True) + 1e-6
        n = len(chunk)
        feats.append(np.hstack([f[:n], f[n:]]))
    return np.vstack(feats).astype(np.float32)


# ---- index ------------------------------------------------------------------

def build_index(cards, dims=512):
    """cards: [{id, name}] with images in data/img. Saves compressed fingerprints."""
    import time
    have = [c for c in cards if os.path.exists(os.path.join(IMG_DIR, c["id"] + ".jpg"))]
    print(f"Fingerprinting {len(have):,} artworks...")
    feats, ids, names = [], [], []
    t0 = time.time()
    for i in range(0, len(have), 256):
        chunk = have[i:i + 256]
        imgs, ok = [], []
        for c in chunk:
            img = cv2.imread(os.path.join(IMG_DIR, c["id"] + ".jpg"))
            if img is not None:
                imgs.append(img)
                ok.append(c)
        if imgs:
            feats.append(fingerprints(imgs))
            ids += [c["id"] for c in ok]
            names += [c["name"] for c in ok]
        if (i // 256) % 20 == 0:
            done = i + len(chunk)
            print(f"  {done:,}/{len(have):,}  ~{(len(have) - done) / (done / (time.time() - t0)) / 60:.0f} min left", flush=True)
    feats = np.vstack(feats)
    # Compress 2560 -> `dims` numbers (projection onto the main directions of variation,
    # without centring, which kept search accuracy in tests): 5x smaller and faster.
    _, _, vt = np.linalg.svd(feats[:: max(1, len(feats) // 20000)], full_matrices=False)
    proj = vt[:dims].T.astype(np.float32)
    comp = feats @ proj
    comp /= np.linalg.norm(comp, axis=1, keepdims=True) + 1e-6
    np.savez_compressed(INDEX_PATH, feats=comp.astype(np.float16), proj=proj, ids=np.array(ids), names=np.array(names))
    print(f"Saved {len(ids):,} fingerprints -> {INDEX_PATH}")


def colour_thumb(img_bgr, w=24, h=33):
    """Tiny Lab-colour thumbnail, normalised per channel (evens out lighting)."""
    x = cv2.cvtColor(cv2.resize(img_bgr, (w, h), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2LAB)
    x = x.astype(np.float32).reshape(-1, 3)
    return (x - x.mean(0)) / (x.std(0) + 1e-3)


def trimmed_distance(a, b, keep=0.75):
    """Mean pixel difference over the best-matching `keep` share of pixels, so glare
    or a finger over part of the card doesn't count against the right match."""
    d = np.abs(a - b).sum(1)
    k = int(len(d) * keep)
    return float(np.partition(d, k)[:k].mean())


@lru_cache(maxsize=20000)
def _ref_thumb(card_id):
    img = cv2.imread(os.path.join(IMG_DIR, card_id + ".jpg"))
    return None if img is None else colour_thumb(img)


class VisualIndex:
    SHORTLIST = 100
    FINGERPRINT_WEIGHT = 3.0

    def __init__(self, path=INDEX_PATH):
        d = np.load(path)
        self.feats = d["feats"].astype(np.float32)
        self.proj = d["proj"]
        self.ids, self.names = d["ids"], d["names"]

    def query(self, card_bgr, k=10):
        """Returns [(card_name, printing_id, score)], best first, one entry per name.
        Scores are roughly 0..1+; the gap between the first two says how sure it is."""
        f = fingerprints([card_bgr])[0] @ self.proj
        f /= np.linalg.norm(f) + 1e-6
        sims = self.feats @ f
        n = min(self.SHORTLIST, len(sims) - 1)
        short = np.argpartition(-sims, n)[:n]
        mine = colour_thumb(card_bgr)
        scored = []
        for i in short:
            ref = _ref_thumb(str(self.ids[i]))
            dist = trimmed_distance(mine, ref) if ref is not None else 9.0
            # Higher is better: fingerprint similarity plus (negative) colour distance.
            scored.append((self.FINGERPRINT_WEIGHT * float(sims[i]) - dist, i))
        scored.sort(reverse=True)
        out, seen = [], set()
        for score, i in scored:
            name = str(self.names[i])
            if name not in seen:
                seen.add(name)
                out.append((name, str(self.ids[i]), score))
            if len(out) == k:
                break
        return out
