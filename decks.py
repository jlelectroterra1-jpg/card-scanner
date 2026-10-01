"""Commander deck data layer, on top of userdb.UserDB (deck tables) and Scryfall's
cards.db (names, types, colour identity, legality, prices, images).

Ownership of a deck card (see also the `deck_card_ownership` SQL view):
  linked to a collection copy  -> 'exact' / 'different_finish' / 'different_printing'
  not linked, physically in the deck (scanned) but not in the Collection -> 'deck_only'
  not linked, not physical -> 'missing' (possibly owned, but every copy is used elsewhere)
Copies are only ever linked if they are free (not used by another deck)."""
import csv
import io
import os
import re
from datetime import datetime

import deckrules
from userdb import CARDS_DB_PATH, FINISHES, UserDBError

MATCH_ORDER = ("exact", "different_finish", "different_printing")
OWNERSHIP_LABELS = {
    "exact": "Owned - exact printing",
    "different_finish": "Owned - different finish",
    "different_printing": "Owned - different printing",
    "deck_only": "In deck, not in Collection",
    "available": "In Collection - not linked yet",
    "in_use": "Owned, but used in another deck",
    "missing": "Not owned",
}


def card_price(card, finish):
    if not card:
        return 0.0
    v = {"foil": card.get("usd_foil"), "etched": card.get("usd_etched")}.get(finish or "nonfoil") or card.get("usd")
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


class DeckStore:
    def __init__(self, userdb, cards_db_path=CARDS_DB_PATH):
        self.udb = userdb
        self.conn = userdb.conn
        userdb.attach_cards_db(cards_db_path)

    # ---- card data -------------------------------------------------------------

    def card(self, scryfall_id):
        if not scryfall_id:
            return None
        r = self.conn.execute("SELECT * FROM scry.cards WHERE id = ?", (scryfall_id,)).fetchone()
        return dict(r) if r else None

    def printings(self, name):
        """All paper printings of a card, regular English ones first, newest first."""
        return [dict(r) for r in self.conn.execute(
            """SELECT * FROM scry.cards WHERE name = ? COLLATE NOCASE
               ORDER BY lang = 'en' DESC, promo ASC, set_code = 'plst' ASC, INSTR(finishes, 'nonfoil') > 0 DESC,
                        usd IS NOT NULL DESC, released_at DESC""", (name,))]

    def default_printing(self, name):
        p = self.printings(name)
        if not p and " // " not in name:
            p = [dict(r) for r in self.conn.execute(
                """SELECT * FROM scry.cards WHERE name LIKE ? ORDER BY lang = 'en' DESC, promo ASC,
                   INSTR(finishes, 'nonfoil') > 0 DESC, usd IS NOT NULL DESC, released_at DESC LIMIT 1""",
                (name + " // %",))]
        return p[0] if p else None

    # ---- decks -------------------------------------------------------------------

    def create_deck(self, name, format="commander"):
        name = (name or "").strip()
        if not name:
            raise ValueError("a deck needs a name")
        return self.udb.create_deck(name, format)

    def deck_list(self):
        """Every deck with commander, counts, value and ownership - for the Decks screen."""
        decks = self.udb.decks()
        own = {}
        for r in self.conn.execute("""SELECT deck_id, ownership, SUM(quantity) FROM deck_card_ownership
                                      WHERE role IN ('commander', 'partner', 'main') GROUP BY deck_id, ownership"""):
            own.setdefault(r[0], {})[r[1]] = r[2]
        values = {r[0]: r[1] for r in self.conn.execute(
            """SELECT dc.deck_id, SUM(dc.quantity * CAST(COALESCE(
                    CASE dc.finish WHEN 'foil' THEN s.usd_foil WHEN 'etched' THEN s.usd_etched END, s.usd, 0) AS REAL))
               FROM deck_cards dc LEFT JOIN scry.cards s ON s.id = dc.scryfall_id
               WHERE dc.role IN ('commander', 'partner', 'main') GROUP BY dc.deck_id""")}
        for d in decks:
            o = own.get(d["deck_id"], {})
            d["owned"] = sum(o.get(k, 0) for k in MATCH_ORDER) + o.get("deck_only", 0)
            d["missing"] = o.get("missing", 0)
            d["value"] = values.get(d["deck_id"], 0.0) or 0.0
            c = self.card(d.get("commander_scryfall_id"))
            d["commander_image"] = c and c.get("image_small")
        return decks

    def deck_cards(self, deck_id):
        """Deck cards joined with card data and ownership. Each row gets: card (dict),
        price, value, group, ownership ('exact'...'missing'), owned_info."""
        rows = [dict(r) for r in self.conn.execute(
            """SELECT dc.*, o.ownership, s.type_line, s.color_identity, s.legal_commander, s.set_code, s.set_name,
                      s.collector_number, s.rarity, s.image_small, s.image_normal, s.finishes AS available_finishes,
                      s.usd, s.usd_foil, s.usd_etched
               FROM deck_cards dc JOIN deck_card_ownership o ON o.deck_card_id = dc.id
               LEFT JOIN scry.cards s ON s.id = dc.scryfall_id
               WHERE dc.deck_id = ? ORDER BY dc.card_name COLLATE NOCASE""", (deck_id,))]
        missing = {r["card_name"] for r in rows if r["ownership"] == "missing"}
        lots = self._lots_by_name(missing)
        elsewhere = self._used_by_other_decks(missing, deck_id)
        for r in rows:
            r["price"] = card_price(r, r["finish"])
            r["value"] = r["price"] * r["quantity"]
            r["group"] = deckrules.group_of(r["type_line"], r["role"])
            if r["ownership"] == "missing":
                mine = lots.get(r["card_name"].lower(), [])
                if any(l["available"] > 0 for l in mine):
                    r["ownership"] = "available"
                elif r["card_name"].lower() in elsewhere:
                    r["ownership"] = "in_use"  # owned, but the copies are in other decks
                # else: every copy is already used by this deck -> still 'missing' for this row
        return rows

    def _lots_by_name(self, names):
        out = {}
        if not names:
            return out
        q = ",".join("?" * len(names))
        for r in self.conn.execute(
                f"""SELECT a.*, ci.condition, ci.language, c.name AS collection_name
                    FROM collection_item_availability a JOIN collection_items ci ON ci.id = a.collection_item_id
                    JOIN collections c ON c.id = ci.collection_id
                    WHERE a.card_name COLLATE NOCASE IN ({q}) AND a.quantity > 0""", list(names)):
            out.setdefault(r["card_name"].lower(), []).append(dict(r))
        return out

    def _used_by_other_decks(self, names, deck_id):
        if not names:
            return set()
        q = ",".join("?" * len(names))
        return {r[0].lower() for r in self.conn.execute(
            f"""SELECT DISTINCT ci.card_name FROM deck_cards dc JOIN collection_items ci ON ci.id = dc.collection_item_id
                WHERE dc.deck_id != ? AND ci.card_name COLLATE NOCASE IN ({q})""", [deck_id, *names])}

    def copies(self, card_name):
        """Every collection copy of a card: owned, free, and which decks use them."""
        lots = self._lots_by_name({card_name}).get(card_name.lower(), [])
        for l in lots:
            l["used_in"] = [dict(r) for r in self.conn.execute(
                """SELECT d.id AS deck_id, d.name, SUM(dc.quantity) AS copies FROM deck_cards dc
                   JOIN decks d ON d.id = dc.deck_id WHERE dc.collection_item_id = ? GROUP BY d.id""",
                (l["collection_item_id"],))]
        return lots

    def summary(self, deck_id, rows=None):
        rows = rows if rows is not None else self.deck_cards(deck_id)
        main = [r for r in rows if r["role"] in ("commander", "partner", "main")]
        info = self.udb.deck(deck_id) or {}
        commanders = [r for r in main if r["role"] in ("commander", "partner")]
        identity = deckrules.colour_identity(*(r["color_identity"] for r in commanders))
        counts = {k: 0 for k in OWNERSHIP_LABELS}
        for r in main:
            counts[r["ownership"]] += r["quantity"]
        owned = sum(counts[k] for k in MATCH_ORDER) + counts["deck_only"]
        top = sorted(main, key=lambda r: -r["price"])[:5]
        return dict(
            name=info.get("name"), format=info.get("format"), updated_at=info.get("updated_at"),
            count=sum(r["quantity"] for r in main), unique=len({r["card_name"] for r in main}),
            value=sum(r["value"] for r in main), identity=identity, commanders=commanders,
            ownership=counts, owned=owned, missing=sum(r["quantity"] for r in main) - owned,
            exact=counts["exact"], different=counts["different_finish"] + counts["different_printing"],
            top=top, warnings=deckrules.warnings(main, identity, has_commander=bool(commanders)))

    # ---- adding / editing ------------------------------------------------------------

    def add_card(self, deck_id, card, finish="nonfoil", quantity=1, role="main", physical=False, source="manual",
                 allocate=True):
        """Add a printing to a deck (merging with an identical row), then link free
        collection copies if there are any. Returns the deck-card id."""
        if finish not in FINISHES:
            finish = "nonfoil"
        if role in ("commander", "partner"):
            dc = self.udb.set_commander(deck_id, card["name"], card["id"], partner=role == "partner", finish=finish,
                                        oracle_id=card.get("oracle_id"), physical=physical, source=source)
        else:
            same = self.conn.execute(
                """SELECT id, quantity FROM deck_cards WHERE deck_id = ? AND scryfall_id = ? AND finish IS ?
                   AND role = ? AND physical = ? AND collection_item_id IS NULL""",
                (deck_id, card["id"], finish, role, 1 if physical else 0)).fetchone()
            if same:
                dc = same["id"]
                self.udb.update_deck_card(dc, quantity=same["quantity"] + quantity)
            else:
                dc = self.udb.add_deck_card(deck_id, card["name"], quantity=quantity, role=role, scryfall_id=card["id"],
                                            finish=finish, oracle_id=card.get("oracle_id"), physical=physical,
                                            source=source)
        if allocate:
            self.allocate(deck_id, [dc])
        return dc

    def allocate(self, deck_id, deck_card_ids=None):
        """Link not-yet-linked deck cards to FREE collection copies of the same card:
        same printing + finish first, then same printing, then any printing. Never takes
        copies used by other decks. Splits a row when only some copies are free.
        Returns the number of copies linked."""
        linked = 0
        with self.udb.transaction() as c:
            sql = "SELECT * FROM deck_cards WHERE deck_id = ? AND collection_item_id IS NULL"
            args = [deck_id]
            if deck_card_ids is not None:
                if not deck_card_ids:
                    return 0
                sql += f" AND id IN ({','.join('?' * len(deck_card_ids))})"
                args += list(deck_card_ids)
            queue = [dict(r) for r in c.execute(sql + " ORDER BY id", args)]
            while queue:
                dc = queue.pop(0)
                lot = self._best_free_lot(c, dc)
                if lot is None:
                    continue
                take = min(dc["quantity"], lot["available"])
                if take < dc["quantity"]:  # split: this row gets the free copies, a new row waits for more
                    c.execute("UPDATE deck_cards SET quantity = ? WHERE id = ?", (take, dc["id"]))
                    rest = c.execute(
                        """INSERT INTO deck_cards (deck_id, card_name, oracle_id, scryfall_id, quantity, finish, role,
                                                   physical, source)
                           SELECT deck_id, card_name, oracle_id, scryfall_id, ?, finish, 'main', physical, source
                           FROM deck_cards WHERE id = ?""", (dc["quantity"] - take, dc["id"])).lastrowid
                    queue.insert(0, dict(dc, id=rest, quantity=dc["quantity"] - take, role="main"))
                c.execute("UPDATE deck_cards SET collection_item_id = ? WHERE id = ?", (lot["id"], dc["id"]))
                linked += take
        return linked

    @staticmethod
    def _best_free_lot(c, dc):
        rows = c.execute(
            """SELECT ci.id, ci.scryfall_id, ci.finish, c.is_default,
                      ci.quantity - COALESCE((SELECT SUM(quantity) FROM deck_cards WHERE collection_item_id = ci.id), 0)
                      AS available
               FROM collection_items ci JOIN collections c ON c.id = ci.collection_id
               WHERE ci.card_name = ? COLLATE NOCASE AND ci.quantity > 0""", (dc["card_name"],)).fetchall()
        free = [dict(r) for r in rows if r["available"] > 0]
        if not free:
            return None

        def rank(l):
            same_print = l["scryfall_id"] == dc["scryfall_id"]
            same_finish = dc["finish"] is None or l["finish"] == dc["finish"]
            return (0 if same_print and same_finish else 1 if same_print else 2, -l["is_default"], l["id"])
        return min(free, key=rank)

    def link(self, deck_card_id, collection_item_id):
        self.udb.allocate(deck_card_id, collection_item_id)

    def unlink(self, deck_card_id):
        self.udb.update_deck_card(deck_card_id, unlink=True)

    def change_printing(self, deck_card_id, card, finish=None):
        dc = self.udb.deck_card(deck_card_id)
        f = finish or dc["finish"] or "nonfoil"
        if f not in (card.get("finishes") or "nonfoil").split(","):
            f = (card.get("finishes") or "nonfoil").split(",")[0]
        self.udb.update_deck_card(deck_card_id, scryfall_id=card["id"], card_name=card["name"], finish=f,
                                  oracle_id=card.get("oracle_id"))
        self.allocate(dc["deck_id"], [deck_card_id])

    def change_finish(self, deck_card_id, finish):
        dc = self.udb.deck_card(deck_card_id)
        self.udb.update_deck_card(deck_card_id, finish=finish)
        self.allocate(dc["deck_id"], [deck_card_id])

    def duplicate(self, deck_id, name):
        """Copy the list; ownership is recalculated from FREE copies only."""
        new = self.udb.duplicate_deck(deck_id, name)
        self.allocate(new)
        return new

    # ---- finishing a deck scan ---------------------------------------------------------

    def finish_scan(self, deck_id, entries):
        """Put scanned cards ({id, finish}) into the deck as physical cards and link
        free collection copies. The scanned commander card fills the commander slot
        instead of being added twice. Returns dict(added, linked, not_in_collection=[deck
        card ids with no copy in the Collection], in_use=[ids owned but used elsewhere])."""
        groups = {}
        for e in entries:
            key = (e["id"], e.get("finish") or "nonfoil")
            groups[key] = groups.get(key, 0) + 1
        touched, added = [], 0
        commanders = {r["card_name"].lower(): dict(r) for r in self.conn.execute(
            "SELECT * FROM deck_cards WHERE deck_id = ? AND role IN ('commander', 'partner')", (deck_id,))}
        for (sid, finish), n in groups.items():
            card = self.card(sid)
            if card is None:
                continue
            cmd = commanders.get(card["name"].lower())
            if cmd is not None and not cmd["physical"]:
                self.udb.update_deck_card(cmd["id"], scryfall_id=sid, finish=finish, physical=True,
                                          oracle_id=card.get("oracle_id"))
                cmd["physical"] = 1
                touched.append(cmd["id"])
                n -= 1
                added += 1
            if n <= 0:
                continue
            touched.append(self.add_card(deck_id, card, finish, quantity=n, physical=True, source="scan",
                                         allocate=False))
            added += n
        linked = self.allocate(deck_id)
        rows = [dict(r) for r in self.conn.execute(
            "SELECT * FROM deck_cards WHERE deck_id = ? AND physical = 1 AND collection_item_id IS NULL", (deck_id,))]
        owned_names = self._lots_by_name({r["card_name"] for r in rows})
        not_in, in_use = [], []
        for r in rows:
            (in_use if owned_names.get(r["card_name"].lower()) else not_in).append(r["id"])
        return dict(added=added, linked=linked, not_in_collection=not_in, in_use=in_use)

    def add_to_collection(self, deck_card_ids, collection_id=None):
        """Add these (physical, unlinked) deck cards to the Collection and link them to
        the new copies - in one transaction. Returns copies added."""
        n = 0
        with self.udb.transaction() as c:
            for dc_id in deck_card_ids:
                dc = c.execute("SELECT * FROM deck_cards WHERE id = ? AND collection_item_id IS NULL",
                               (dc_id,)).fetchone()
                if dc is None:
                    continue
                card = self.card(dc["scryfall_id"]) or {}
                lot = self.udb.add_card(dc["scryfall_id"], dc["card_name"], card.get("set_code") or "?",
                                        card.get("collector_number") or "?", finish=dc["finish"] or "nonfoil",
                                        quantity=dc["quantity"], collection_id=collection_id,
                                        language=card.get("lang") or "en", oracle_id=card.get("oracle_id"),
                                        market_price=card_price(card, dc["finish"]) or None, _conn=c)
                c.execute("UPDATE deck_cards SET collection_item_id = ? WHERE id = ?", (lot, dc_id))
                n += dc["quantity"]
            if n:
                self.udb.log_import("deck_scan", collection_id or self.udb.default_collection_id(), n, _conn=c)
        return n

    # ---- import / export -------------------------------------------------------------------

    LINE = re.compile(r"^\s*(?:(\d+)\s*x?\s+)?(.+?)(?:\s+\(([A-Za-z0-9]{2,6})\)(?:\s+([0-9A-Za-z★\-]+))?)?"
                      r"(?:\s+\*(F|E|CMDR)\*)*(?:\s+\[[^\]]*\])?(?:\s+\^[^^]*\^)?\s*$")
    SECTIONS = {"commander": "commander", "commanders": "commander", "deck": "main", "main": "main",
                "mainboard": "main", "main deck": "main", "sideboard": "sideboard", "maybeboard": "maybe",
                "maybe": "maybe", "companion": "companion", "considering": "maybe"}

    def parse_decklist(self, text):
        """Read a decklist: plain ('1 Sol Ring'), Moxfield/Archidekt/ManaBox text
        ('1x Sol Ring (C21) 263 *F*', section headers like 'Commander' or '// Sideboard',
        '*CMDR*' markers) or a CSV export with Name/Quantity/Set/Collector number columns.
        Nothing is written. Each row: dict(name, quantity, role, finish, status, card, note)
        with status 'set' (set + number matched), 'id', 'name' (newest printing used) or 'none'."""
        text = text.lstrip("﻿")
        first = text.splitlines()[0] if text.strip() else ""
        if "," in first and "name" in first.lower():
            return self._parse_csv(text)
        out, role = [], "main"
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            head = line.strip("/ :").lower()
            if head in self.SECTIONS:
                role = self.SECTIONS[head]
                continue
            if line.startswith("//"):
                continue
            m = self.LINE.match(line)
            if not m:
                continue
            qty, name, set_code, number, mark = m.group(1), m.group(2).strip(), m.group(3), m.group(4), m.group(5)
            r = dict(name=name, quantity=int(qty or 1), role=role, finish="nonfoil", set_code=set_code,
                     number=number, scryfall_id=None)
            if "*F*" in line:
                r["finish"] = "foil"
            if "*E*" in line:
                r["finish"] = "etched"
            if "*CMDR*" in line or "^Commander^" in line or "[Commander" in line:
                r["role"] = "commander"
            out.append(self._match(r))
        return self._assign_partner(out)

    def _parse_csv(self, text):
        out = []
        for row in csv.DictReader(io.StringIO(text)):
            r = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            name = r.get("name") or r.get("card name") or r.get("card")
            if not name:
                continue
            try:
                qty = int(float(r.get("quantity") or r.get("count") or r.get("qty") or 1))
            except ValueError:
                qty = 1
            foil = (r.get("foil") or r.get("finish") or "").lower()
            out.append(self._match(dict(
                name=name, quantity=max(1, qty), role="commander" if "commander" in (r.get("board") or r.get("category")
                                                                                 or "").lower() else "main",
                finish="foil" if foil in ("foil", "true", "yes", "1") else ("etched" if foil == "etched" else "nonfoil"),
                set_code=r.get("set code") or r.get("edition") or r.get("set"),
                number=r.get("collector number") or r.get("collector_number") or r.get("number"),
                scryfall_id=r.get("scryfall id") or r.get("scryfall_id"))))
        return self._assign_partner(out)

    def _match(self, r):
        card = None
        if r.get("scryfall_id"):
            card = self.card(r["scryfall_id"])
            r["status"] = "id" if card else None
        if card is None and r.get("set_code") and r.get("number"):
            row = self.conn.execute("SELECT * FROM scry.cards WHERE set_code = ? AND collector_number = ? LIMIT 1",
                                    (r["set_code"].lower(), r["number"])).fetchone()
            card = dict(row) if row else None
            r["status"] = "set" if card else None
        if card is None:
            card = self.default_printing(r["name"])
            r["status"] = "name" if card else "none"
        r["card"] = card
        r["note"] = "printing guessed (newest)" if r["status"] == "name" and r.get("set_code") else ""
        if card and r["finish"] not in (card.get("finishes") or "nonfoil").split(","):
            r["finish"] = (card.get("finishes") or "nonfoil").split(",")[0]
        return r

    @staticmethod
    def _assign_partner(rows):
        n = 0
        for r in rows:
            if r["role"] == "commander":
                n += 1
                if n == 2:
                    r["role"] = "partner"
                elif n > 2:
                    r["role"] = "main"
        return rows

    def commit_import(self, deck_id, parsed):
        """Add the matched rows of a parsed decklist to a deck, then link free collection
        copies. Rows that didn't match are skipped. Returns cards added."""
        n = 0
        for r in parsed:
            if r["card"] is None or r["role"] not in ("commander", "partner", "companion", "main", "sideboard",
                                                       "maybe"):
                continue
            self.add_card(deck_id, r["card"], r["finish"], quantity=1 if r["role"] in ("commander", "partner")
                          else r["quantity"], role=r["role"], source="import", allocate=False)
            n += r["quantity"]
        self.allocate(deck_id)
        return n

    def export(self, deck_id, folder):
        """Write the deck as a decklist (commander first) and a ManaBox-style CSV."""
        rows = self.deck_cards(deck_id)
        info = self.udb.deck(deck_id)
        os.makedirs(folder, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", info["name"]).strip("_") or "deck"
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        txt, csv_path = os.path.join(folder, f"{safe}_{stamp}.txt"), os.path.join(folder, f"{safe}_{stamp}.csv")
        order = {"commander": 0, "partner": 1, "companion": 2, "main": 3, "sideboard": 4, "maybe": 5}
        rows.sort(key=lambda r: (order.get(r["role"], 9), r["card_name"].lower()))

        def line(r):
            tag = {"foil": " *F*", "etched": " *E*"}.get(r["finish"] or "", "")
            where = f" ({r['set_code'].upper()}) {r['collector_number']}" if r.get("set_code") else ""
            return f"{r['quantity']} {r['card_name']}{where}{tag}"
        with open(txt, "w", encoding="utf-8") as f:
            sections = [("Commander", ("commander", "partner")), ("Companion", ("companion",)), ("Deck", ("main",)),
                        ("Sideboard", ("sideboard",)), ("Maybeboard", ("maybe",))]
            first = True
            for title, roles in sections:
                part = [r for r in rows if r["role"] in roles]
                if not part:
                    continue
                if not first:
                    f.write("\n")
                f.write(f"{title}\n")
                f.writelines(line(r) + "\n" for r in part)
                first = False
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Name", "Set code", "Set name", "Collector number", "Foil", "Rarity", "Quantity",
                        "Scryfall ID", "Board"])
            for r in rows:
                w.writerow([r["card_name"], (r.get("set_code") or "").upper(), r.get("set_name") or "",
                            r.get("collector_number") or "", "normal" if (r["finish"] or "nonfoil") == "nonfoil"
                            else r["finish"], r.get("rarity") or "", r["quantity"], r.get("scryfall_id") or "",
                            r["role"]])
        return txt, csv_path

    # ---- questions Phase 4 (analysis) will ask ------------------------------------------------

    def commander_identity(self, deck_id):
        rows = self.conn.execute(
            """SELECT s.color_identity FROM deck_cards dc LEFT JOIN scry.cards s ON s.id = dc.scryfall_id
               WHERE dc.deck_id = ? AND dc.role IN ('commander', 'partner')""", (deck_id,)).fetchall()
        return deckrules.colour_identity(*(r[0] for r in rows))

    def composition(self, deck_id):
        """{group: copies} - Creatures, Lands, Artifacts..."""
        out = {}
        for r in self.deck_cards(deck_id):
            if r["role"] in ("commander", "partner", "main"):
                out[r["group"]] = out.get(r["group"], 0) + r["quantity"]
        return out

    def missing_cards(self, deck_id):
        return [r for r in self.deck_cards(deck_id) if r["ownership"] in ("missing", "available", "in_use")
                and r["role"] in ("commander", "partner", "main")]

    def free_owned_cards(self, identity, legal_only=True):
        """Owned cards with at least one copy not used by any deck, inside a colour identity
        (and Commander-legal) - the pool a swap could come from."""
        allowed = set(identity or "")
        rows = self.conn.execute(
            """SELECT a.card_name, SUM(a.available) AS free, MAX(s.color_identity) AS color_identity,
                      MAX(s.type_line) AS type_line, MAX(s.legal_commander) AS legal
               FROM collection_item_availability a JOIN scry.cards s ON s.id = a.scryfall_id
               WHERE a.available > 0 GROUP BY a.card_name ORDER BY a.card_name""").fetchall()
        return [dict(r) for r in rows if set(r["color_identity"] or "") <= allowed
                and (not legal_only or r["legal"] == "legal")]


def ownership_label(row):
    return OWNERSHIP_LABELS.get(row.get("ownership"), row.get("ownership") or "")


__all__ = ["DeckStore", "card_price", "ownership_label", "OWNERSHIP_LABELS", "UserDBError"]
