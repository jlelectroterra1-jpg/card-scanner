"""Download Scryfall's card list and build a small local database (cards.db).

Run this once, then again whenever you want fresh prices / new sets:
    python update_db.py
"""
import os
import sqlite3
import sys
import time

import gzip
import json

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
RAW_PATH = os.path.join(DATA_DIR, "default_cards.jsonl.gz")
DB_PATH = os.path.join(DATA_DIR, "cards.db")
HEADERS = {"User-Agent": "HomeCardScanner/0.1", "Accept": "application/json"}

# Things that aren't real cards you'd scan from a pile.
SKIP_LAYOUTS = {"art_series", "token", "double_faced_token", "emblem", "vanguard", "scheme", "planar"}
SKIP_SET_TYPES = {"token", "memorabilia", "minigame"}


def download():
    os.makedirs(DATA_DIR, exist_ok=True)
    info = requests.get("https://api.scryfall.com/bulk-data/default-cards", headers=HEADERS, timeout=30).json()
    # Scryfall serves gzipped JSON Lines (one card per line); older API versions used "download_uri".
    url = info.get("jsonl_download_uri") or info["download_uri"]
    total = info.get("compressed_size") or info.get("size") or 0
    print(f"Downloading Scryfall card data ({total / 1e6:.0f} MB, updated {info['updated_at'][:10]})...")
    tmp = RAW_PATH + ".part"
    done = 0
    with requests.get(url, headers=HEADERS, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if total:
                    sys.stdout.write(f"\r  {done / 1e6:6.0f} / {total / 1e6:.0f} MB")
                    sys.stdout.flush()
    os.replace(tmp, RAW_PATH)
    print()


def image_url(card, size):
    if "image_uris" in card:
        return card["image_uris"].get(size)
    faces = card.get("card_faces") or []
    if faces and "image_uris" in faces[0]:
        return faces[0]["image_uris"].get(size)
    return None


def build():
    print("Building local database...")
    tmp = DB_PATH + ".part"
    if os.path.exists(tmp):
        os.remove(tmp)
    db = sqlite3.connect(tmp)
    db.executescript("""
        CREATE TABLE cards (
            id TEXT PRIMARY KEY, name TEXT, set_code TEXT, set_name TEXT,
            collector_number TEXT, rarity TEXT, lang TEXT, released_at TEXT,
            finishes TEXT, promo INTEGER, usd TEXT, usd_foil TEXT, usd_etched TEXT, eur TEXT,
            image_small TEXT, image_normal TEXT
        );
        CREATE TABLE names (lookup TEXT, name TEXT);
    """)
    rows, names = [], set()
    opener = gzip.open if RAW_PATH.endswith(".gz") else open
    with opener(RAW_PATH, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip().rstrip(",")
            if not line.startswith("{"):
                continue
            c = json.loads(line)
            if c.get("digital") or c.get("layout") in SKIP_LAYOUTS or c.get("set_type") in SKIP_SET_TYPES:
                continue
            p = c.get("prices") or {}
            rows.append((
                c["id"], c["name"], c["set"], c.get("set_name"), c.get("collector_number"),
                c.get("rarity"), c.get("lang"), c.get("released_at"),
                ",".join(c.get("finishes") or []), int(bool(c.get("promo"))),
                p.get("usd"), p.get("usd_foil"), p.get("usd_etched"), p.get("eur"),
                image_url(c, "small"), image_url(c, "normal"),
            ))
            # The scanner reads the name printed at the top of the card, which for
            # split / double-faced cards is just one face, so index every face name.
            names.add((c["name"], c["name"]))
            for face in c.get("card_faces") or []:
                names.add((face["name"], c["name"]))
                if face.get("flavor_name"):
                    names.add((face["flavor_name"], c["name"]))
            # Secret Lair / Universes Within cards print a different name on the card.
            for alt in (c.get("printed_name"), c.get("flavor_name")):
                if alt:
                    names.add((alt, c["name"]))
    db.executemany("INSERT OR REPLACE INTO cards VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    db.executemany("INSERT INTO names VALUES (?,?)", sorted(names))
    db.executescript("CREATE INDEX idx_name ON cards(name); CREATE INDEX idx_set ON cards(set_code);")
    db.commit()
    db.close()
    os.replace(tmp, DB_PATH)
    print(f"  {len(rows):,} printings, {len({n for _, n in names}):,} unique cards -> {DB_PATH}")


if __name__ == "__main__":
    t = time.time()
    download()
    build()
    os.remove(RAW_PATH)  # the big raw file isn't needed once cards.db exists
    print(f"Done in {time.time() - t:.0f}s")
