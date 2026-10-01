"""Local card lookups: fuzzy name matching and picking the right printing."""
import os
import re
import sqlite3

import unicodedata

from rapidfuzz import fuzz, process

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cards.db")


class CardDB:
    def __init__(self, path=DB_PATH):
        if not os.path.exists(path):
            raise SystemExit("No card database yet - run:  python update_db.py")
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        # printed name (normalised) -> the card names it can belong to
        self.by_lookup = {}
        for lookup, name in self.db.execute("SELECT lookup, name FROM names"):
            self.by_lookup.setdefault(normalise(lookup), []).append(name)
        self.lookups = list(self.by_lookup)
        self.set_codes = {r[0] for r in self.db.execute("SELECT DISTINCT set_code FROM cards")}

    def match_name(self, text, limit=5):
        """Fuzzy-match OCR text to card names. Returns [(card_name, score)] best first."""
        text = normalise(text)
        if len(text) < 3:
            return []
        hits = process.extract(text, self.lookups, scorer=fuzz.ratio, limit=limit)
        out, seen = [], set()
        for lookup, score, _ in hits:
            names = self.by_lookup[lookup]
            # "Swamp" is both the card Swamp and a face of "Swamp // Swamp";
            # the plain card is the one people mean.
            exact = [n for n in names if normalise(n) == lookup]
            name = (exact or names)[0]
            if name not in seen:
                seen.add(name)
                out.append((name, score))
        return out[:limit]

    def printings(self, name):
        rows = self.db.execute(
            "SELECT * FROM cards WHERE name = ? ORDER BY lang = 'en' DESC, released_at DESC", (name,)
        ).fetchall()
        return [dict(r) for r in rows]

    def by_id(self, card_id):
        r = self.db.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
        return dict(r) if r else None

    def search(self, text, limit=8):
        """Search-as-you-type: names starting with the text first (shortest first),
        then names containing it, then fuzzy matches for typos."""
        q = normalise(text)
        if len(q) < 2:
            return []
        out = []

        def add(lookup):
            names = self.by_lookup[lookup]
            exact = [n for n in names if normalise(n) == lookup]
            name = (exact or names)[0]
            # Skip "Sol Ring // Sol Ring"-style reprints when the plain card is already listed.
            if name not in out and not any(part in out for part in name.split(" // ")):
                out.append(name)

        for lk in sorted((l for l in self.lookups if l.startswith(q)), key=len):
            add(lk)
            if len(out) >= limit:
                return out
        for lk in sorted((l for l in self.lookups if q in l and not l.startswith(q)), key=len):
            add(lk)
            if len(out) >= limit:
                return out
        for n, _ in self.match_name(text, limit):
            if n not in out:
                out.append(n)
        return out[:limit]

    def pick_printing(self, name, footer_text="", locked_set=None, card_img=None):
        ranked = self.ranked_printings(name, footer_text, locked_set, card_img)
        return ranked[0] if ranked else None

    def printings_for_art(self, name, art_id, locked_set=None, footer_text="", card_img=None):
        """Printings of `name`, most likely first, without downloading anything:
        the same frame/colours as the photo (vs locally cached images), then the set
        code / number read from the corner, then the recognised artwork, then
        regular printings over promos, then newest."""
        return self.ranked_printings(name, footer_text, locked_set, card_img, prefer_id=art_id, download=False)

    def ranked_printings(self, name, footer_text="", locked_set=None, card_img=None, prefer_id=None, download=True):
        """All printings of `name`, most likely first: by how it looks (compared
        with Scryfall's images), then the set code / collector number read from
        the bottom-left corner, then the regular printing over promos, then newest."""
        prints = self.printings(name)
        if not prints:
            return []
        if locked_set:
            in_set = [p for p in prints if p["set_code"] == locked_set]
            if in_set:
                prints = in_set
        # Non-English printings are only kept when there's no English one.
        english = [p for p in prints if p["lang"] == "en"]
        prints = english or prints
        tokens = set(re.findall(r"[a-z0-9]+", footer_text.lower()))
        numbers = {t.lstrip("0") for t in tokens if t.isdigit()}

        def footer_score(p):
            s = 0
            if p["set_code"] in tokens:
                s += 4
            if p["collector_number"].lower().lstrip("0") in numbers:
                s += 3
            if not p["promo"] and p["set_code"] != "plst":
                s += 0.5
            if p["id"] == prefer_id:
                s += 0.75
            return s

        if card_img is None or len(prints) == 1:
            # sorted() is stable, so ties keep the newest-first order from printings().
            return sorted(prints, key=footer_score, reverse=True)
        from printmatch import rank_printings
        ranked = rank_printings(card_img, prints, download=download)
        # Reprints with the same art and frame look identical to a webcam,
        # so treat everything close to the best match as a tie.
        cutoff = ranked[0]["dist"] * 1.10 + 0.02
        close = [p for p in ranked if p["dist"] <= cutoff]
        close.sort(key=lambda p: (-footer_score(p), -int(p["released_at"].replace("-", "") or 0)))
        return close + [p for p in ranked if p["dist"] > cutoff]

def normalise(text):
    """Lower-case, strip accents and odd characters so 'ALTAÏR' matches 'Altaïr'."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = text.replace("`", "'").lower()
    return re.sub(r"[^a-z0-9',\-/ ]", "", text).strip()
