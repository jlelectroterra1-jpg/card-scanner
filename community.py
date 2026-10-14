"""Local commander knowledge cache (data/commander_cache.db) - OPTIONAL community evidence.

The analyser never needs this. When present it adds a small, labelled bonus to cards that
already passed our own need / fit tests; it can never make a card recommended on its own
and never overrides the local deck understanding.

Sources (all documented, all explicit user actions, nothing crawled):

* "spellbook"   - combo pieces one card away, saved whenever you press Check combos
                  (Commander Spellbook's documented find-my-combos API).
* "recommander" - Recommander's documented public API (recommander.cards/api): one POST
                  with the commander + deck list, answered with card recommendations and
                  scores. Only when you press "Get community ideas"; cached, at most one
                  request per commander a day (unless the deck changed), 429 respected,
                  attribution shown. Their terms: personal / non-commercial use, attribute
                  Recommander, respect rate limits.
* EDHREC        - never. Its terms forbid automated queries; edhrec_url() only opens the
                  page in your own browser.

Table commander_cards: commander key (sorted commander oracle ids joined by '+'), candidate
oracle id + name, source, metric (0..1), metric kind, archetype tags, retrieved date.
"""
import hashlib
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(HERE, "data", "commander_cache.db")
USER_AGENT = "HomeCardScanner/0.7 (personal Commander deck analyser; offline-first)"
RECOMMANDER_URL = "https://api.recommander.cards/public-release/api/decks/recommend/top"
REFETCH_AFTER = 24 * 3600  # one request per commander per day (unless the deck changed)
MIN_GAP = 10.0             # never two Recommander requests within 10 s
ATTRIBUTION = {"recommander": "Recommander (recommander.cards)", "spellbook": "Commander Spellbook"}
_lock = threading.Lock()
_last_request = [0.0]

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS commander_cards (
        commander_key TEXT NOT NULL,
        card_oracle_id TEXT NOT NULL,
        card_name TEXT NOT NULL,
        source TEXT NOT NULL,
        metric REAL NOT NULL,
        metric_kind TEXT NOT NULL,
        archetype_tags TEXT,
        detail TEXT,
        retrieved_at TEXT NOT NULL,
        PRIMARY KEY (commander_key, card_oracle_id, source))""",
    """CREATE TABLE IF NOT EXISTS fetches (
        commander_key TEXT NOT NULL,
        source TEXT NOT NULL,
        deck_hash TEXT,
        status TEXT NOT NULL,
        retrieved_at TEXT NOT NULL,
        epoch REAL NOT NULL,
        PRIMARY KEY (commander_key, source))""",
]


def commander_key(oracle_ids):
    return "+".join(sorted(o for o in oracle_ids if o))


def deck_hash(cards):
    return hashlib.sha1(json.dumps(sorted(cards)).encode()).hexdigest()[:16]


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CommunityCache:
    def __init__(self, path=CACHE_PATH):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        with self.conn:
            for sql in SCHEMA:
                self.conn.execute(sql)

    def close(self):
        self.conn.close()

    def store(self, key, source, rows, kind, tags=None, dhash=None, status="ok"):
        """rows: [(card_oracle_id, card_name, metric, detail)] - replaces this source's rows."""
        now = _now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if kind != "combo piece":  # one-away combo pieces accumulate; everything else is replaced
                self.conn.execute("DELETE FROM commander_cards WHERE commander_key = ? AND source = ?", (key, source))
            self.conn.executemany(
                """INSERT OR REPLACE INTO commander_cards (commander_key, card_oracle_id, card_name, source, metric,
                   metric_kind, archetype_tags, detail, retrieved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(key, oid or name, name, source, float(m), kind, json.dumps(tags or []), detail, now)
                 for oid, name, m, detail in rows])
            self.conn.execute("""INSERT OR REPLACE INTO fetches (commander_key, source, deck_hash, status, retrieved_at,
                                 epoch) VALUES (?, ?, ?, ?, ?, ?)""", (key, source, dhash, status, now, time.time()))
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def lookup(self, key):
        """{card name: [dict(source, metric, kind, detail, retrieved)]}"""
        out = {}
        for r in self.conn.execute("SELECT * FROM commander_cards WHERE commander_key = ?", (key,)):
            out.setdefault(r["card_name"], []).append(dict(source=r["source"], metric=r["metric"],
                                                           kind=r["metric_kind"], detail=r["detail"],
                                                           retrieved=r["retrieved_at"][:10]))
        return out

    def last_fetch(self, key, source):
        r = self.conn.execute("SELECT * FROM fetches WHERE commander_key = ? AND source = ?", (key, source)).fetchone()
        return dict(r) if r else None


def load(key, path=None):
    """Cached evidence for a commander, or {} when there is no cache (never fails)."""
    path = path or CACHE_PATH
    if not os.path.exists(path):
        return {}
    try:
        c = CommunityCache(path)
        try:
            return c.lookup(key)
        finally:
            c.close()
    except sqlite3.Error:
        return {}


def save_combos(key, combos, path=None):
    """Remember combo pieces that are ONE card away (from Check combos)."""
    rows, in_deck = [], []
    for c in combos:
        if c.get("status") == "one_away" and len(c.get("missing") or []) == 1:
            others = [p for p in c["cards"] if p not in c["missing"]]
            rows.append((None, c["missing"][0], 1.0, "completes a combo with " + " + ".join(others[:3])))
        elif c.get("status") == "in_deck":
            for piece in c.get("cards") or []:
                in_deck.append((None, piece, 1.0, " + ".join(c["cards"][:4])))
    if not rows and not in_deck:
        return 0
    cache = CommunityCache(path or CACHE_PATH)
    try:
        if rows:
            cache.store(key, "spellbook", rows, "combo piece")
        # combos already in the deck: their pieces are protected from cuts (replaced each check)
        cache.store(key, "spellbook-deck", in_deck, "combo in deck")
    finally:
        cache.close()
    return len(rows)


class RecommanderClient:
    """Recommander's documented public API. One request per explicit user action."""

    def __init__(self, session=None, timeout=30, path=None):
        self.session = session or requests.Session()
        self.timeout, self.path = timeout, path or CACHE_PATH

    def fetch(self, key, commanders, cards, force=False):
        """Ask once and cache. Returns (n cached rows, 'fetched' | 'cached')."""
        dh = deck_hash(cards)
        cache = CommunityCache(self.path)
        try:
            last = cache.last_fetch(key, "recommander")
            if not force and last and last["status"] == "ok" and last["deck_hash"] == dh \
                    and time.time() - last["epoch"] < REFETCH_AFTER:
                return len(cache.lookup(key)), "cached"
            with _lock:
                wait = MIN_GAP - (time.time() - _last_request[0])
                if wait > 0:
                    raise RuntimeError(f"Please wait {int(wait) + 1} s before asking Recommander again")
                _last_request[0] = time.time()
            body = {"card_format": "name", "commander": commanders[0],
                    "partner": commanders[1] if len(commanders) > 1 else None,
                    "deck": list(cards)[:500], "candidate_step": {"source": "color_identity"}}
            r = self.session.post(RECOMMANDER_URL, json=body, timeout=self.timeout,
                                  headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            if r.status_code == 429:
                raise RuntimeError("Recommander is busy (rate limit) - try again later")
            r.raise_for_status()
            data = r.json()
            if data.get("result_code") not in (None, "success"):
                raise RuntimeError(str(data.get("error") or data.get("result_code")))
            recs = (data.get("data") or {}).get("recommendations") or []
            rows = [(x.get("oracle_id"), x["name"], max(0.0, min(1.0, float(x.get("score") or 0))), None)
                    for x in recs if x.get("name")]
            cache.store(key, "recommander", rows, "recommendation score", dhash=dh)
            return len(rows), "fetched"
        finally:
            cache.close()
