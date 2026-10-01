"""Collection data layer: browsing (search / filter / sort), totals, statistics, export
and ManaBox CSV import - on top of userdb.UserDB, with card details (rarity, set name,
images, colour, type) looked up in Scryfall's cards.db by Scryfall ID.

Built for 10,000-50,000 cards: browsing returns just the ordered lot ids (one fast
query), and full rows are fetched only for the ones on screen.
"""
import csv
import os
import re
import sqlite3
from datetime import datetime

from userdb import CARDS_DB_PATH, CONDITIONS, FINISHES

SORTS = {
    "value": ("Highest value first", "ci.quantity * COALESCE(ci.market_price, 0) DESC"),
    "price": ("Price (each)", "COALESCE(ci.market_price, -1) DESC"),
    "name": ("Name", "ci.card_name COLLATE NOCASE ASC"),
    "quantity": ("Quantity", "ci.quantity DESC"),
    "set": ("Set", "ci.set_code ASC, CAST(ci.collector_number AS INTEGER) ASC, ci.collector_number ASC"),
    "added": ("Recently added", "ci.added_at DESC, ci.id DESC"),
}
RARITIES = ("common", "uncommon", "rare", "mythic", "special", "bonus")
COLOURS = {"W": "White", "U": "Blue", "B": "Black", "R": "Red", "G": "Green", "C": "Colourless", "M": "Multicolour"}
TYPES = ("Creature", "Instant", "Sorcery", "Artifact", "Enchantment", "Planeswalker", "Land", "Battle")
CONDITION_LABELS = {"mint": "Mint", "near_mint": "Near Mint", "lightly_played": "Lightly Played",
                    "moderately_played": "Moderately Played", "heavily_played": "Heavily Played", "damaged": "Damaged"}

MANABOX_HEADER = ["Name", "Set code", "Set name", "Collector number", "Foil", "Rarity", "Quantity",
                  "Scryfall ID", "Purchase price", "Condition", "Language", "Purchase price currency"]


class CollectionStore:
    def __init__(self, userdb, cards_db_path=CARDS_DB_PATH):
        self.udb = userdb
        self.conn = userdb.conn
        self.has_cards_db = os.path.exists(cards_db_path)
        if self.has_cards_db:
            userdb.attach_cards_db(cards_db_path)
            cols = {r[1] for r in self.conn.execute("PRAGMA scry.table_info(cards)")}
        else:
            cols = set()
        # Colour / type filters need the fields added to cards.db in Phase 1 (Update Prices).
        self.has_colour = "color_identity" in cols
        self.has_type = "type_line" in cols

    # ---- browsing -------------------------------------------------------------

    def _where(self, f):
        """SQL WHERE clause + args for a filter dict:
        collection_id, search, set_code, finish, condition, rarity, colour, type."""
        where, args = ["ci.quantity > 0"], []
        if f.get("collection_id"):
            where.append("ci.collection_id = ?")
            args.append(f["collection_id"])
        for tok in (f.get("search") or "").lower().split():
            # every word must match: card name contains it, or it IS the set code / number
            where.append("(ci.card_name LIKE ? ESCAPE '\\' OR ci.set_code = ? OR ci.collector_number = ?)")
            args += ["%" + tok.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%", tok, tok]
        for key, col in (("set_code", "ci.set_code"), ("finish", "ci.finish"), ("condition", "ci.condition")):
            if f.get(key):
                where.append(f"{col} = ?")
                args.append(f[key].lower() if key == "set_code" else f[key])
        self._needs_join = bool(f.get("rarity") or f.get("colour") or f.get("type"))
        if f.get("rarity") and self.has_cards_db:
            where.append("s.rarity = ?")
            args.append(f["rarity"])
        if f.get("colour") and self.has_colour:
            c = f["colour"]
            if c == "C":
                where.append("COALESCE(s.color_identity, '') = ''")
            elif c == "M":
                where.append("LENGTH(COALESCE(s.color_identity, '')) > 1")
            else:
                where.append("INSTR(COALESCE(s.color_identity, ''), ?) > 0")
                args.append(c)
        if f.get("type") and self.has_type:
            where.append("s.type_line LIKE ?")
            args.append(f"%{f['type']}%")
        return " AND ".join(where), args

    def _from(self, join=True):
        return ("FROM collection_items ci LEFT JOIN scry.cards s ON s.id = ci.scryfall_id"
                if self.has_cards_db and join else "FROM collection_items ci")

    def query_ids(self, f, sort="value"):
        """Ids of matching lots, in display order."""
        where, args = self._where(f)
        order = SORTS.get(sort, SORTS["value"])[1]
        return [r[0] for r in self.conn.execute(
            f"SELECT ci.id {self._from(self._needs_join)} WHERE {where} "
            f"ORDER BY {order}, ci.card_name COLLATE NOCASE, ci.id", args)]

    def rows(self, ids):
        """Full rows for these lot ids (same order)."""
        if not ids:
            return []
        extra = (", s.rarity, s.set_name, s.image_small, s.image_normal, s.finishes AS available_finishes, "
                 "s.usd, s.usd_foil, s.usd_etched" if self.has_cards_db else "")
        extra += ", s.color_identity, s.type_line" if self.has_colour and self.has_type else ""
        q = ",".join("?" * len(ids))
        found = {r["id"]: dict(r) for r in self.conn.execute(
            f"SELECT ci.*, c.name AS collection_name {extra} {self._from()} "
            f"JOIN collections c ON c.id = ci.collection_id WHERE ci.id IN ({q})", ids)}
        out = []
        for i in ids:
            r = found.get(i)
            if r:
                r["value"] = (r["market_price"] or 0) * r["quantity"]
                out.append(r)
        return out

    def summary(self, f):
        """Totals for what the filter shows: copies, unique card names, value (USD)."""
        where, args = self._where(f)
        r = self.conn.execute(
            f"""SELECT COALESCE(SUM(ci.quantity), 0), COUNT(DISTINCT ci.card_name COLLATE NOCASE),
                       COALESCE(SUM(ci.quantity * COALESCE(ci.market_price, 0)), 0), COUNT(*)
                {self._from(self._needs_join)} WHERE {where}""", args).fetchone()
        return dict(cards=r[0], unique=r[1], value=r[2], lots=r[3])

    def set_codes(self, collection_id=None):
        """Set codes present (for the Set filter), most cards first."""
        where, args = self._where({"collection_id": collection_id})
        return [r[0] for r in self.conn.execute(
            f"SELECT ci.set_code {self._from(False)} WHERE {where} GROUP BY ci.set_code "
            f"ORDER BY SUM(ci.quantity) DESC, ci.set_code", args)]

    # ---- statistics ---------------------------------------------------------------

    def stats(self, collection_id=None):
        f = {"collection_id": collection_id}
        where, args = self._where(f)
        s = self.summary(f)
        s["foils"] = self.conn.execute(
            f"SELECT COALESCE(SUM(ci.quantity), 0) {self._from(False)} WHERE {where} AND ci.finish != 'nonfoil'",
            args).fetchone()[0]
        s["top"] = self.rows(self.query_ids(f, "price")[:10])
        s["by_set"] = [dict(set_code=r[0], set_name=r[1], cards=r[2], value=r[3]) for r in self.conn.execute(
            f"""SELECT ci.set_code, {'MAX(s.set_name)' if self.has_cards_db else 'NULL'}, SUM(ci.quantity),
                       SUM(ci.quantity * COALESCE(ci.market_price, 0)) AS v
                {self._from()} WHERE {where} GROUP BY ci.set_code ORDER BY v DESC LIMIT 8""", args)]
        s["by_rarity"] = {}
        if self.has_cards_db:
            for r in self.conn.execute(f"SELECT s.rarity, SUM(ci.quantity) {self._from()} WHERE {where} "
                                       "GROUP BY s.rarity", args):
                s["by_rarity"][r[0] or "unknown"] = r[1]
        return s

    # ---- export ---------------------------------------------------------------------

    def export(self, collection_id, folder, name=None):
        """Write the collection as a ManaBox CSV and a plain decklist. Returns both paths."""
        os.makedirs(folder, exist_ok=True)
        rows = self.rows(self.query_ids({"collection_id": collection_id}, "name"))
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", name or "collection").strip("_") or "collection"
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        csv_path = os.path.join(folder, f"{safe}_{stamp}_manabox.csv")
        txt_path = os.path.join(folder, f"{safe}_{stamp}_list.txt")
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(MANABOX_HEADER)
            for r in rows:
                w.writerow([r["card_name"], r["set_code"].upper(), r.get("set_name") or "", r["collector_number"],
                            "normal" if r["finish"] == "nonfoil" else r["finish"], r.get("rarity") or "",
                            r["quantity"], r["scryfall_id"],
                            "" if r["purchase_price"] is None else f"{r['purchase_price']:.2f}",
                            r["condition"], r["language"], r["purchase_currency"] or ""])
        with open(txt_path, "w", encoding="utf-8") as fh:
            for r in rows:
                tag = {"foil": " *F*", "etched": " *E*"}.get(r["finish"], "")
                fh.write(f"{r['quantity']} {r['card_name']} ({r['set_code'].upper()}) {r['collector_number']}{tag}\n")
        return csv_path, txt_path

    # ---- ManaBox CSV import ----------------------------------------------------------

    def parse_manabox(self, path):
        """Read a ManaBox CSV and match every row to a Scryfall printing. Nothing is
        written. Each result: dict(row=..., status=..., card=..., quantity, finish,
        condition, language, purchase_price, note) where status is
          'id'    - matched by Scryfall ID (certain)
          'set'   - matched by set code + collector number (certain)
          'name'  - only the card name matched (uncertain printing: newest one picked)
          'none'  - no match"""
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
        out = []
        for row in rows:
            r = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            res = dict(row=r, status="none", card=None, note="")
            try:
                res["quantity"] = max(1, int(float(r.get("quantity") or 1)))
            except ValueError:
                res["quantity"], res["note"] = 1, "quantity unreadable, using 1"
            foil = (r.get("foil") or "").lower()
            res["finish"] = "foil" if foil in ("foil", "true", "yes", "1") else ("etched" if foil == "etched" else "nonfoil")
            cond = (r.get("condition") or "near_mint").lower().replace(" ", "_")
            res["condition"] = cond if cond in CONDITIONS else "near_mint"
            res["language"] = (r.get("language") or "en").lower()[:5] or "en"
            try:
                res["purchase_price"] = float(r["purchase price"]) if r.get("purchase price") else None
            except ValueError:
                res["purchase_price"] = None
            res["purchase_currency"] = r.get("purchase price currency") or None
            card = None
            sid = r.get("scryfall id")
            if sid:
                card = self._card("id = ?", sid)
                if card:
                    res["status"] = "id"
            if card is None and r.get("set code") and r.get("collector number"):
                card = self._card("set_code = ? AND collector_number = ?", r["set code"].lower(), r["collector number"])
                if card:
                    res["status"] = "set"
            if card is None and r.get("name"):
                card = self._card("name = ? COLLATE NOCASE ORDER BY lang = 'en' DESC, released_at DESC", r["name"])
                if card is None and " // " not in r["name"]:
                    card = self._card("name LIKE ? ORDER BY lang = 'en' DESC, released_at DESC", r["name"] + " // %")
                if card:
                    res["status"] = "name"
                    res["note"] = "only the name matched - printing is a guess"
            if card is not None and res["finish"] not in (card.get("finishes") or "nonfoil").split(","):
                res["note"] = (res["note"] + "; " if res["note"] else "") + f"{res['finish']} not made for this printing"
            res["card"] = card
            out.append(res)
        return out

    def _card(self, where, *args):
        if not self.has_cards_db:
            return None
        r = self.conn.execute(f"SELECT * FROM scry.cards WHERE {where} LIMIT 1", args).fetchone()
        return dict(r) if r else None

    def commit_import(self, parsed, collection_id, include_name_matches=False, source_name=""):
        """Add matched rows in one transaction. Uncertain (name-only) rows are only added
        when include_name_matches is True. Returns (copies added, rows added)."""
        ok = {"id", "set"} | ({"name"} if include_name_matches else set())
        copies = lots = 0
        with self.udb.transaction() as c:
            for p in parsed:
                if p["status"] not in ok:
                    continue
                card = p["card"]
                price = {"foil": card.get("usd_foil"), "etched": card.get("usd_etched")}.get(p["finish"]) or card.get("usd")
                self.udb.add_card(card["id"], card["name"], card["set_code"], card["collector_number"],
                                  finish=p["finish"], quantity=p["quantity"], collection_id=collection_id,
                                  condition=p["condition"], language=p["language"],
                                  market_price=float(price) if price else None,
                                  purchase_price=p["purchase_price"], purchase_currency=p["purchase_currency"],
                                  oracle_id=card.get("oracle_id"), _conn=c)
                copies += p["quantity"]
                lots += 1
            if copies:
                self.udb.log_import("manabox_csv", collection_id, copies, source_name, _conn=c)
        return copies, lots


def finish_label(f):
    return {"nonfoil": "Non-foil", "foil": "Foil", "etched": "Etched"}.get(f, f)
