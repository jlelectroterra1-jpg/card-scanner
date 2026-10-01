"""Build the picture-recognition index: a fingerprint of every distinct card artwork.

    python build_visual_index.py            (downloads ~50k small images the first time)

Downloads Scryfall's small image of one printing per artwork into data/img, then
stores a compact fingerprint of each in data/visual_index.npz, which scanner.py uses
to recognise cards by their picture when the name is too small to read."""
import gzip
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
IMG_DIR = os.path.join(DATA, "img")
LIST_PATH = os.path.join(DATA, "vis", "unique_artwork.jsonl.gz")
HEADERS = {"User-Agent": "HomeCardScanner/0.1", "Accept": "application/json"}
SKIP_LAYOUTS = {"art_series", "token", "double_faced_token", "emblem", "vanguard", "scheme", "planar"}
SKIP_SET_TYPES = {"token", "memorabilia", "minigame"}


def artwork_list(refresh=False):
    os.makedirs(os.path.dirname(LIST_PATH), exist_ok=True)
    if refresh or not os.path.exists(LIST_PATH):
        info = requests.get("https://api.scryfall.com/bulk-data/unique-artwork", headers=HEADERS, timeout=30).json()
        url = info.get("jsonl_download_uri") or info["download_uri"]
        print("Downloading artwork list...")
        with requests.get(url, headers=HEADERS, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(LIST_PATH + ".part", "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        os.replace(LIST_PATH + ".part", LIST_PATH)
    out = []
    with gzip.open(LIST_PATH, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip().rstrip(",")
            if not line.startswith("{"):
                continue
            c = json.loads(line)
            if c.get("digital") or c.get("layout") in SKIP_LAYOUTS or c.get("set_type") in SKIP_SET_TYPES:
                continue
            img = (c.get("image_uris") or (c.get("card_faces") or [{}])[0].get("image_uris") or {}).get("small")
            if img:
                out.append(dict(id=c["id"], name=c["name"], img=img))
    return out


def download_images(cards, workers=12):
    os.makedirs(IMG_DIR, exist_ok=True)
    todo = [c for c in cards if not os.path.exists(os.path.join(IMG_DIR, c["id"] + ".jpg"))]
    print(f"{len(cards):,} artworks, {len(todo):,} images to download")
    session = requests.Session()
    session.headers.update(HEADERS)
    done = [0]
    t0 = time.time()

    def fetch(c):
        path = os.path.join(IMG_DIR, c["id"] + ".jpg")
        for attempt in range(3):
            try:
                r = session.get(c["img"], timeout=30)
                if r.ok:
                    with open(path + ".part", "wb") as f:
                        f.write(r.content)
                    os.replace(path + ".part", path)
                    break
            except requests.RequestException:
                time.sleep(1 + attempt)
        done[0] += 1
        if done[0] % 500 == 0:
            rate = done[0] / (time.time() - t0)
            print(f"  {done[0]:,}/{len(todo):,}  ({rate:.0f}/s, ~{(len(todo) - done[0]) / rate / 60:.0f} min left)", flush=True)

    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(fetch, todo))


def all_printings():
    """Every English paper printing in cards.db, for telling reprints/frames apart."""
    import sqlite3
    db = sqlite3.connect(os.path.join(DATA, "cards.db"))
    rows = db.execute("SELECT id, name, image_small FROM cards WHERE image_small IS NOT NULL AND lang = 'en'")
    return [dict(id=r[0], name=r[1], img=r[2]) for r in rows]


if __name__ == "__main__":
    if "--all-printings" in sys.argv:
        download_images(all_printings())
        sys.exit()
    cards = artwork_list(refresh="--refresh" in sys.argv)
    download_images(cards)
    if "--download-only" not in sys.argv:
        from visual import build_index
        build_index(cards)
