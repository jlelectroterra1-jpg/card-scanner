"""Pick the exact printing by comparing the camera image with Scryfall's
picture of every printing of that card. Images are downloaded on first use and
cached in data/img, so each card is only fetched once."""
import os
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np
import requests

IMG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "img")
HEADERS = {"User-Agent": "HomeCardScanner/0.1"}
SIG_W, SIG_H = 40, 56

_session = requests.Session()
_session.headers.update(HEADERS)
_pool = ThreadPoolExecutor(8)


def signature(card_bgr):
    """Tiny, lighting-normalised colour thumbnail used for comparing cards."""
    small = cv2.resize(card_bgr, (SIG_W, SIG_H), interpolation=cv2.INTER_AREA)
    lab = cv2.cvtColor(small, cv2.COLOR_BGR2LAB).astype(np.float32)
    lab = cv2.GaussianBlur(lab, (3, 3), 0)
    mean, std = lab.reshape(-1, 3).mean(0), lab.reshape(-1, 3).std(0) + 1e-3
    return (lab - mean) / std


def _fetch(card):
    path = os.path.join(IMG_DIR, card["id"] + ".jpg")
    if not os.path.exists(path) and card.get("image_small"):
        try:
            data = _session.get(card["image_small"], timeout=20).content
            with open(path + ".part", "wb") as f:
                f.write(data)
            os.replace(path + ".part", path)
        except (requests.RequestException, OSError):
            return None
    img = cv2.imread(path) if os.path.exists(path) else None
    return None if img is None else signature(img)


def reference_signatures(printings):
    os.makedirs(IMG_DIR, exist_ok=True)
    return list(_pool.map(_fetch, printings))


def rank_printings(card_bgr, printings):
    """Returns printings sorted by visual similarity, each with a 'dist' key (lower = closer)."""
    sig = signature(card_bgr)
    out = []
    for p, ref in zip(printings, reference_signatures(printings)):
        dist = float(np.abs(sig - ref).mean()) if ref is not None else 9.0
        out.append(dict(p, dist=dist))
    return sorted(out, key=lambda p: p["dist"])
