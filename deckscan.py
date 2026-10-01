"""Where a recognised card goes. The scanner recognises cards exactly the same way in
every mode; it then hands each card to the active *scan target*:

  SessionTarget        normal scanning -> data/session.json (Add to Collection later)
  DeckScanTarget       Scan Deck mode  -> data/deck_scan.json, its own session for one deck
  CommanderPickTarget  Scan Commander  -> asks "Set X as Commander?"

Each target owns its list of scanned entries and saves it after every change, so an
unfinished deck scan survives the app closing."""
import json
import os
from datetime import datetime

import deckrules
from decks import card_price


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".part", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(path + ".part", path)


class SessionTarget:
    """The normal scan list."""
    kind = "session"

    def __init__(self, path):
        self.path = path
        self.entries = _load(path, [])

    def save(self):
        _save(self.path, self.entries)

    def add(self, app, entry, card):
        self.entries.append(entry)
        self.save()
        return None  # nothing more to ask


class DeckScanTarget:
    """Scanning a physical deck into one deck. Duplicates of non-basic cards are flagged
    (never silently dropped); the scanned commander card isn't counted twice."""
    kind = "deck"

    def __init__(self, path, deck_id, deck_name, store=None, data=None):
        self.path, self.deck_id, self.deck_name = path, deck_id, deck_name
        data = data or {}
        self.entries = data.get("entries", [])
        self.started_at = data.get("started_at") or datetime.now().isoformat(timespec="seconds")
        self.base = dict(count=0, value=0.0, names={}, commanders=[], identity="")
        if store is not None:
            self.refresh_base(store)

    @classmethod
    def load(cls, path):
        """The unfinished deck scan saved on disk, or None."""
        data = _load(path, None)
        if not isinstance(data, dict) or not data.get("deck_id"):
            return None
        return data

    def refresh_base(self, store):
        """What the deck already holds (read once, so scanning stays fast)."""
        rows = [r for r in store.deck_cards(self.deck_id) if r["role"] in ("commander", "partner", "main")]
        names = {}
        for r in rows:
            names[r["card_name"]] = names.get(r["card_name"], 0) + r["quantity"]
        commanders = [r for r in rows if r["role"] in ("commander", "partner")]
        self.base = dict(count=sum(r["quantity"] for r in rows), value=sum(r["value"] for r in rows), names=names,
                         commanders=[dict(name=r["card_name"], physical=r["physical"], scryfall_id=r["scryfall_id"])
                                     for r in commanders],
                         identity=deckrules.colour_identity(*(r["color_identity"] for r in commanders)))

    def save(self):
        _save(self.path, dict(version=1, deck_id=self.deck_id, deck_name=self.deck_name, started_at=self.started_at,
                              entries=self.entries))

    def discard(self):
        try:
            os.remove(self.path)
        except OSError:
            pass

    def commander_names(self):
        return {c["name"].lower() for c in self.base["commanders"]}

    def add(self, app, entry, card):
        """Returns a duplicate warning dict(name, count) or None."""
        name = card.get("name", "")
        # The physical commander card is part of the pile: it fills the commander slot.
        cmd_unscanned = [c for c in self.base["commanders"] if c["name"].lower() == name.lower() and not c["physical"]]
        if cmd_unscanned and not any(e.get("commander_card") and e.get("name") == name for e in self.entries):
            entry["commander_card"] = True
        entry["name"] = name
        self.entries.append(entry)
        self.save()
        if entry.get("commander_card"):
            return None
        limit = deckrules.copy_limit(name, card.get("type_line"))
        have = self.copies_of(name)
        if limit is not None and have > limit:
            return dict(name=name, count=have, limit=limit)
        return None

    def copies_of(self, name):
        n = self.base["names"].get(name, 0)
        return n + sum(1 for e in self.entries if e.get("name") == name and not e.get("commander_card"))

    def stats(self, card_lookup):
        """Live numbers for the scanner panel (cheap: no deck analysis)."""
        added = [e for e in self.entries if not e.get("commander_card")]
        value = self.base["value"]
        names = set(self.base["names"])
        for e in self.entries:
            c = card_lookup(e["id"]) or {}
            value += card_price(c, e.get("finish"))
            names.add(e.get("name") or c.get("name"))
        return dict(count=self.base["count"] + len(added), unique=len(names), value=value,
                    commander=" + ".join(c["name"] for c in self.base["commanders"]) or "not chosen",
                    scanned=len(self.entries))


class CommanderPickTarget:
    """Scan Commander: the next recognised card is offered as the (partner) commander."""
    kind = "commander"

    def __init__(self, deck_id, deck_name, partner=False):
        self.deck_id, self.deck_name, self.partner = deck_id, deck_name, partner
        self.entries = []

    def save(self):
        pass

    def add(self, app, entry, card):
        return dict(commander=card, entry=entry)
