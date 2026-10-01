"""The Collection tab: browse, search, filter, sort and edit your permanent collection.

Drawn with ui.Painter like the rest of the app. Only the rows on screen are drawn and
only their thumbnails are loaded, so 50,000 cards stay quick; the big card picture is
fetched from Scryfall in the background the first time you open a card."""
import os
import threading
from collections import OrderedDict

import cv2
import requests

from collection import (COLOURS, CONDITION_LABELS, RARITIES, SORTS, TYPES, CollectionStore, finish_label)
from currency import display_currency
from ui import (BG, BUTTON, BUTTON_HI, CARD_BG, GOLD, GREEN, MUTED, RED, ROW_ALT, ROW_SEL, TEXT, YELLOW, Dialog,
                Painter, open_folder)
from userdb import CONDITIONS, FINISHES, UserDBError

HERE = os.path.dirname(os.path.abspath(__file__))
IMG_DIR = os.path.join(HERE, "data", "img")
LARGE_DIR = os.path.join(HERE, "data", "img_large")
HIGH_VALUE = 5.0
COND_SHORT = {"mint": "M", "near_mint": "NM", "lightly_played": "LP", "moderately_played": "MP",
              "heavily_played": "HP", "damaged": "DMG"}
PAGE_KEYS = {0x210000: "pgup", 0x220000: "pgdn", 0x240000: "home", 0x230000: "end",
             0x260000: "up", 0x280000: "down"}

FAILED = object()  # what _guard returns when a change was refused

LIST_X0, LIST_X1 = 16, 930
PANE_X0 = 946
TOOLBAR_Y, LIST_Y0, ROW_H = 60, 108, 40


class CollectionScreen:
    def __init__(self, app, width, height, store=None):
        self.app = app
        self.W, self.H = width, height
        self.store = store or CollectionStore(app.userdb)
        self.collection_id = app.userdb.default_collection_id()
        self.search, self.focus = "", None
        self.filters = {}  # set_code / finish / condition / rarity / colour / type
        self.sort = "value"
        self.scroll, self.selected, self.open_dd = 0, None, None
        self.ids, self.summary_data, self.stats_data = [], {}, None
        self._stats_key = None
        self.data_version = 0  # bumped whenever cards are added/edited/removed
        self.version = 0
        self._key = self._cache = None
        self._rows_key, self._rows = None, []
        self.thumbs = OrderedDict()
        self._fetching = set()
        self.refresh()

    # ---- data -----------------------------------------------------------------

    @property
    def visible_rows(self):
        return (self.H - LIST_Y0 - 30) // ROW_H

    def query_filter(self):
        return dict(collection_id=self.collection_id, search=self.search, **self.filters)

    def refresh(self, data_changed=True):
        """Re-run the query. data_changed=False (search/filter/sort changes) keeps the
        statistics, which describe the whole collection, instead of recomputing them."""
        if data_changed:
            self.data_version += 1
        self.ids = self.store.query_ids(self.query_filter(), self.sort)
        self.summary_data = self.store.summary(self.query_filter())
        self.scroll = max(0, min(self.scroll, max(0, len(self.ids) - self.visible_rows)))
        if self.selected is not None and self.selected not in set(self.ids):
            self.selected = None
        self.bump()

    def bump(self):
        self.version += 1

    def visible(self):
        key = (self.version, self.scroll)
        if key != self._rows_key:
            self._rows = self.store.rows(self.ids[self.scroll:self.scroll + self.visible_rows])
            self._rows_key = key
        return self._rows

    def selected_row(self):
        if self.selected is None:
            return None
        rows = self.store.rows([self.selected])
        return rows[0] if rows else None

    def money(self, usd):
        return self.app.money(usd)

    # ---- images (lazy) -----------------------------------------------------------

    def thumb(self, row):
        sid = row["scryfall_id"]
        if sid in self.thumbs:
            self.thumbs.move_to_end(sid)
            return self.thumbs[sid]
        path = os.path.join(IMG_DIR, sid + ".jpg")
        img = cv2.imread(path) if os.path.exists(path) else None
        if img is None:
            self._fetch(row.get("image_small"), path)
            return None
        img = cv2.resize(img, (26, 36), interpolation=cv2.INTER_AREA)
        self.thumbs[sid] = img
        if len(self.thumbs) > 600:
            self.thumbs.popitem(last=False)
        return img

    def large(self, row):
        sid = row["scryfall_id"]
        path = os.path.join(LARGE_DIR, sid + ".jpg")
        if os.path.exists(path):
            img = cv2.imread(path)
            if img is not None:
                return img
        self._fetch(row.get("image_normal"), path)
        small = os.path.join(IMG_DIR, sid + ".jpg")
        return cv2.imread(small) if os.path.exists(small) else None  # small one until the big one arrives

    def _fetch(self, url, path):
        """Download an image in the background; redraw when it arrives."""
        if not url or path in self._fetching or len(self._fetching) > 6:
            return
        self._fetching.add(path)

        def run():
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                data = requests.get(url, headers={"User-Agent": "HomeCardScanner/0.2"}, timeout=20).content
                with open(path + ".part", "wb") as f:
                    f.write(data)
                os.replace(path + ".part", path)
                self.thumbs.pop(os.path.basename(path)[:-4], None)
                self.bump()
            except (requests.RequestException, OSError):
                pass
            finally:
                self._fetching.discard(path)
        threading.Thread(target=run, daemon=True).start()

    # ---- drawing -----------------------------------------------------------------

    def render(self):
        toast = self.app.toast_text()
        key = (self.version, self.scroll, self.selected, self.open_dd, self.focus, self.search, toast,
               display_currency(self.app.settings))
        if key == self._key and self._cache is not None:
            return self._cache
        p = Painter(self.W, self.H, BG)
        popup = self._header(p)
        self._list(p)
        if self.selected is not None:
            self._detail(p)
        else:
            self._stats(p)
        if popup:
            popup(p)
        if toast:
            text, colour = toast
            w = p.f["body"].getlength(text) + 30
            x0 = (LIST_X0 + LIST_X1 - w) / 2
            p.d.rounded_rectangle((x0, self.H - 44, x0 + w, self.H - 12), radius=16, fill=(40, 42, 48))
            p.text((x0 + 15, self.H - 28), text, fill=colour, anchor="lm")
        self._key, self._cache = key, (p.to_bgr(), p.hits)
        return self._cache

    def _collections(self):
        return self.app.userdb.collections()

    def _collection_name(self):
        if self.collection_id is None:
            return "All collections"
        for c in self._collections():
            if c["id"] == self.collection_id:
                return c["name"]
        return "?"

    def _header(self, p):
        cols = self._collections()
        name = self._collection_name()
        p.dropdown((LIST_X0, 10, 300, 48), name, "dd:collection", active=self.open_dd == "collection")
        s = self.summary_data
        filtered = bool(self.search or self.filters)
        line = f"{s.get('cards', 0):,} cards  |  {s.get('unique', 0):,} unique  |  {self.money(s.get('value', 0))}"
        p.text((316, 29), line + ("   (filtered)" if filtered else ""), font="name", anchor="lm",
               fill=TEXT if not filtered else YELLOW)
        x = self.W - 16
        for label, action, style in reversed([("Manage", "manage", "normal"), ("Refresh prices", "refresh", "normal"),
                                              ("Export", "export", "normal"), ("Import CSV", "import", "normal")]):
            w = p.f["button"].getlength(label) + 28
            p.button((x - w, 12, x, 46), label, action, style=style)
            x -= w + 8

        # toolbar: search, filters, sort
        y0, y1 = TOOLBAR_Y, TOOLBAR_Y + 36
        p.input_box((LIST_X0, y0, 300, y1), self.search, "Search name, set or number", self.focus == "search", "search")
        x = 310
        specs = [("set", "Set", self.filters.get("set_code", "").upper() or "All"),
                 ("finish", "Finish", finish_label(self.filters["finish"]) if "finish" in self.filters else "All"),
                 ("condition", "Cond.", COND_SHORT.get(self.filters.get("condition"), "All")),
                 ("rarity", "Rarity", (self.filters.get("rarity") or "All").title()),
                 ("colour", "Colour", COLOURS.get(self.filters.get("colour"), "All")),
                 ("type", "Type", self.filters.get("type") or "All")]
        anchors = {}
        for dd, label, value in specs:
            text = f"{label}: {value}"
            w = max(92, p.f["button"].getlength(text) + 34)
            active = (dd == "set" and "set_code" in self.filters) or (dd != "set" and dd in self.filters)
            p.dropdown((x, y0, x + w, y1), text, f"dd:{dd}", active=active or self.open_dd == dd)
            anchors[dd] = x
            x += w + 6
        if self.filters or self.search:
            p.button((x, y0, x + 64, y1), "Clear", "clear")
        sort_label = "Sort: " + SORTS[self.sort][0]
        sw = p.f["button"].getlength(sort_label) + 40
        p.dropdown((self.W - 16 - sw, y0, self.W - 16, y1), sort_label, "dd:sort", active=self.open_dd == "sort")
        anchors["sort"] = self.W - 16 - max(sw, 230)
        anchors["collection"] = LIST_X0

        if not self.open_dd:
            return None
        dd = self.open_dd
        if dd == "collection":
            items = [(None, "All collections")] + [(c["id"], c["name"]) for c in cols] + [("new", "+ New collection...")]
            sel, top, width = self.collection_id, 50, 284
        elif dd == "set":
            items = [(None, "All sets")] + [(c, c.upper()) for c in self.store.set_codes(self.collection_id)[:13]]
            sel, top, width = self.filters.get("set_code"), y1 + 4, 160
        elif dd == "finish":
            items = [(None, "All finishes")] + [(f, finish_label(f)) for f in FINISHES]
            sel, top, width = self.filters.get("finish"), y1 + 4, 170
        elif dd == "condition":
            items = [(None, "All conditions")] + [(c, CONDITION_LABELS[c]) for c in CONDITIONS]
            sel, top, width = self.filters.get("condition"), y1 + 4, 200
        elif dd == "rarity":
            items = [(None, "All rarities")] + [(r, r.title()) for r in RARITIES]
            sel, top, width = self.filters.get("rarity"), y1 + 4, 170
        elif dd == "colour":
            items = [(None, "All colours")] + list(COLOURS.items())
            sel, top, width = self.filters.get("colour"), y1 + 4, 170
        elif dd == "type":
            items = [(None, "All types")] + [(t, t) for t in TYPES]
            sel, top, width = self.filters.get("type"), y1 + 4, 170
        else:
            items = [(k, v[0]) for k, v in SORTS.items()]
            sel, top, width = self.sort, y1 + 4, 230
        x = anchors.get(dd, LIST_X0)
        return lambda pp: pp.popup_list(min(x, self.W - width - 8), top, width, items, f"pick:{dd}", sel, max_rows=14)

    def _list(self, p):
        x0, x1 = LIST_X0, LIST_X1
        y = LIST_Y0
        cols = [("Card", 36, "name"), ("Set", 330, "set"), ("Rarity", 430, None), ("Finish", 532, None),
                ("Cond.", 606, None), ("Qty", 690, "quantity"), ("Each", 760, "price"), ("Total", 850, "value")]
        for label, cx, sort in cols:
            colour = YELLOW if sort == self.sort else MUTED
            p.text((x0 + cx, y + 8), label, font="label", fill=colour)
            if sort:
                p.hit((x0 + cx - 4, y, x0 + cx + 70, y + 22), ("sortcol", sort))
        y += 22
        rows = self.visible()
        if not rows:
            msg = ("No cards match - try clearing the search or filters" if (self.search or self.filters)
                   else "No cards here yet - scan some and press Add to Collection")
            p.text(((x0 + x1) / 2, y + 120), msg, font="body", fill=MUTED, anchor="mm")
        for i, r in enumerate(rows):
            ry = y + i * ROW_H
            bg = ROW_SEL if r["id"] == self.selected else (ROW_ALT if (self.scroll + i) % 2 else BG)
            p.d.rectangle((x0, ry, x1 - 12, ry + ROW_H - 2), fill=bg)
            p.paste(self.thumb(r), x0 + 4, ry + 2, 26, 36, radius=3)
            p.text((x0 + 36, ry + 19), r["card_name"], font="rowb", anchor="lm", width=286)
            p.text((x0 + 330, ry + 19), f"{r['set_code'].upper()} #{num(r['collector_number'])}", font="row", fill=MUTED,
                   anchor="lm", width=94)
            if r.get("rarity"):
                p.rarity_dot(x0 + 430, ry + 15, r["rarity"])
                p.text((x0 + 444, ry + 19), r["rarity"].title(), font="small", fill=MUTED, anchor="lm", width=84)
            p.text((x0 + 532, ry + 19), finish_label(r["finish"]), font="row",
                   fill=YELLOW if r["finish"] != "nonfoil" else MUTED, anchor="lm")
            p.text((x0 + 606, ry + 19), COND_SHORT.get(r["condition"], r["condition"]), font="row", fill=MUTED, anchor="lm")
            p.text((x0 + 720, ry + 19), str(r["quantity"]), font="rowb", anchor="rm")
            price = r["market_price"]
            p.text((x0 + 830, ry + 19), self.money(price) if price is not None else "-", font="row",
                   fill=GOLD if (price or 0) >= HIGH_VALUE else MUTED, anchor="rm")
            p.text((x1 - 16, ry + 19), self.money(r["value"]), font="rowb",
                   fill=GOLD if r["value"] >= HIGH_VALUE else TEXT, anchor="rm")
            p.hit((x0, ry, x1 - 12, ry + ROW_H - 2), ("row", r["id"]))
        # scrollbar
        n, vis = len(self.ids), self.visible_rows
        track = (x1 - 8, y, x1 - 2, y + vis * ROW_H)
        p.d.rounded_rectangle(track, radius=3, fill=CARD_BG)
        if n > vis:
            h = max(24, (track[3] - track[1]) * vis / n)
            top = track[1] + (track[3] - track[1] - h) * self.scroll / max(1, n - vis)
            p.d.rounded_rectangle((track[0], top, track[2], top + h), radius=3, fill=BUTTON_HI)
            p.hit((track[0] - 6, track[1], track[2] + 2, track[3]), "scrollbar")
            first, last = self.scroll + 1, min(n, self.scroll + vis)
            p.text((x0, self.H - 14), f"{first:,}-{last:,} of {n:,} entries", font="small", fill=MUTED, anchor="lm")

    def _pane_box(self, p, title):
        x0, y0, x1, y1 = PANE_X0, TOOLBAR_Y + 46, self.W - 16, self.H - 10
        p.d.rounded_rectangle((x0, y0, x1, y1), radius=12, fill=CARD_BG)
        p.text((x0 + 16, y0 + 14), title, font="name", fill=YELLOW)
        return x0, y0, x1, y1

    def _detail(self, p):
        r = self.selected_row()
        if r is None:
            return self._stats(p)
        x0, y0, x1, y1 = self._pane_box(p, "")
        p.button((x1 - 34, y0 + 8, x1 - 8, y0 + 34), "x", "close")
        iw, ih = 150, 209
        p.paste(self.large(r), x0 + 14, y0 + 14, iw, ih, radius=8)
        tx, tw = x0 + 14 + iw + 14, x1 - (x0 + 14 + iw + 14) - 40
        y = y0 + 16
        for line in _wrap2(p, r["card_name"], "name", tw + 30):
            p.text((tx, y), line, font="name")
            y += 22
        p.text((tx, y + 2), r.get("set_name") or r["set_code"].upper(), font="small", fill=MUTED, width=tw + 30)
        y += 22
        if r.get("rarity"):
            p.rarity_dot(tx, y + 4, r["rarity"])
        p.text((tx + 14, y), f"{r['set_code'].upper()} #{num(r['collector_number'])} · {(r.get('rarity') or '').title()}",
               font="small", fill=MUTED)
        y += 22
        p.text((tx, y), f"In: {r['collection_name']}", font="small", fill=MUTED, width=tw + 30)
        y += 30
        price = r["market_price"]
        p.text((tx, y), self.money(price) if price is not None else "no price", font="big",
               fill=GOLD if (price or 0) >= HIGH_VALUE else TEXT)
        y += 36
        p.text((tx, y), "each", font="small", fill=MUTED)
        y += 24
        p.text((tx, y), f"Total {self.money(r['value'])}", font="rowb")
        y += 24
        if r.get("price_updated_at"):
            p.text((tx, y), f"price from {r['price_updated_at'][:10]}", font="small", fill=MUTED)

        # editable fields under the picture
        y = y0 + 14 + ih + 12
        lx, vx = x0 + 16, x0 + 130
        alloc = self.app.userdb.availability(r["id"]) or {}
        p.text((lx, y + 15), "Quantity", font="small", fill=MUTED, anchor="lm")
        p.button((vx, y, vx + 34, y + 28), "-", "qty-")
        p.text((vx + 56, y + 15), str(r["quantity"]), font="name", anchor="mm")
        p.button((vx + 78, y, vx + 112, y + 28), "+", "qty+")
        if alloc.get("allocated"):
            p.text((vx + 124, y + 15), f"{alloc['allocated']} in decks", font="small", fill=YELLOW, anchor="lm")
        y += 34
        p.text((lx, y + 15), "Finish", font="small", fill=MUTED, anchor="lm")
        avail = [f for f in (r.get("available_finishes") or "nonfoil,foil,etched").split(",") if f in FINISHES]
        if r["finish"] not in avail:
            avail.append(r["finish"])
        bx = vx
        for f in FINISHES:
            if f not in avail:
                continue
            w = p.f["button"].getlength(finish_label(f)) + 22
            p.button((bx, y, bx + w, y + 28), finish_label(f), ("finish", f),
                     style="selected" if f == r["finish"] else "ghost")
            bx += w + 6
        y += 34
        p.text((lx, y + 15), "Condition", font="small", fill=MUTED, anchor="lm")
        p.dropdown((vx, y, vx + 200, y + 28), CONDITION_LABELS.get(r["condition"], r["condition"]), "condition")
        y += 34
        p.text((lx, y + 15), "Purchase price", font="small", fill=MUTED, anchor="lm")
        pp = (f"{'R' if r['purchase_currency'] == 'ZAR' else '$'}{r['purchase_price']:,.2f}"
              if r["purchase_price"] is not None else "add")
        p.button((vx, y, vx + 140, y + 28), pp, "purchase", style="ghost")
        y += 34
        p.text((lx, y + 15), "Added", font="small", fill=MUTED, anchor="lm")
        p.text((vx, y + 15), (r["added_at"] or "").replace("T", " ")[:16], font="row", anchor="lm")
        y += 28
        p.text((lx, y + 15), "Notes", font="small", fill=MUTED, anchor="lm")
        p.button((vx, y, x1 - 16, y + 28), r["notes"] or "add a note", "notes", style="ghost")
        y = y1 - 42
        bw = (x1 - x0 - 32 - 8) / 2
        p.button((x0 + 16, y, x0 + 16 + bw, y + 30), "Move to...", "move")
        p.button((x0 + 24 + bw, y, x1 - 16, y + 30), "Remove", "remove", style="danger")

    def _stats(self, p):
        x0, y0, x1, y1 = self._pane_box(p, "Statistics")
        key = (self.collection_id, self.data_version)
        if self.stats_data is None or self._stats_key != key:
            self.stats_data = self.store.stats(self.collection_id)
            self._stats_key = key
        s = self.stats_data
        y = y0 + 48
        for label, value in (("Total cards", f"{s['cards']:,}"), ("Unique cards", f"{s['unique']:,}"),
                             ("Total value", self.money(s["value"])), ("Foils", f"{s['foils']:,}")):
            p.text((x0 + 16, y), label, font="small", fill=MUTED)
            p.text((x1 - 16, y), value, font="rowb", anchor="ra")
            y += 22
        if s["by_rarity"]:
            parts = [f"{k.title()} {v:,}" for k, v in sorted(s["by_rarity"].items(),
                                                            key=lambda kv: RARITIES.index(kv[0]) if kv[0] in RARITIES else 9)]
            p.text((x0 + 16, y + 4), "  ·  ".join(parts), font="small", fill=MUTED, width=x1 - x0 - 32)
            y += 26
        y += 6
        p.text((x0 + 16, y), "MOST VALUABLE", font="label", fill=MUTED)
        y += 20
        if not s["top"]:
            p.text((x0 + 16, y), "Nothing yet", font="small", fill=MUTED)
        for i, r in enumerate(s["top"]):
            p.text((x0 + 16, y), f"{i + 1}.", font="row", fill=MUTED)
            p.text((x0 + 40, y), r["card_name"], font="row", width=x1 - x0 - 150)
            p.text((x1 - 16, y), self.money(r["market_price"]), font="rowb",
                   fill=GOLD if (r["market_price"] or 0) >= HIGH_VALUE else TEXT, anchor="ra")
            p.hit((x0 + 10, y - 2, x1 - 10, y + 20), ("row", r["id"]))
            y += 22
        if s["by_set"] and y < y1 - 80:
            y += 10
            p.text((x0 + 16, y), "VALUE BY SET", font="label", fill=MUTED)
            y += 20
            for r in s["by_set"]:
                if y > y1 - 26:
                    break
                p.text((x0 + 16, y), f"{(r['set_name'] or r['set_code'].upper())}", font="small", width=x1 - x0 - 150)
                p.text((x1 - 16, y), f"{r['cards']:,} · {self.money(r['value'])}", font="small", fill=MUTED, anchor="ra")
                y += 20

    # ---- input -------------------------------------------------------------------

    def click(self, x, y, hits):
        action = None
        for x0, y0, x1, y1, a in reversed(hits):  # popups are drawn last, so they win
            if x0 <= x <= x1 and y0 <= y <= y1:
                action = a
                break
        was_open = self.open_dd
        self.open_dd = None
        if action is None:
            self.focus = None
            self.bump()
            return
        if isinstance(action, tuple) and action[0].startswith("pick:"):
            return self.pick(action[0][5:], action[1])
        if isinstance(action, str) and action.startswith("dd:"):
            dd = action[3:]
            if dd == "colour" and not self.store.has_colour or dd == "type" and not self.store.has_type:
                self.app.toast("Run Update Prices once to filter by colour and card type", YELLOW)
            else:
                self.open_dd = None if was_open == dd else dd
            self.bump()
            return
        if action == "search":
            self.focus = "search"
        elif action == "clear":
            self.search, self.filters = "", {}
            self.refresh(False)
        elif isinstance(action, tuple) and action[0] == "row":
            self.selected = None if self.selected == action[1] else action[1]
        elif isinstance(action, tuple) and action[0] == "sortcol":
            self.set_sort(action[1])
        elif action == "scrollbar":
            frac = (y - (LIST_Y0 + 22)) / max(1, self.visible_rows * ROW_H)
            self.scroll_to(int(frac * len(self.ids)) - self.visible_rows // 2)
        elif action == "close":
            self.selected = None
        else:
            self.action(action)
        self.bump()

    def pick(self, dd, value):
        if dd == "collection":
            if value == "new":
                return self.new_collection()
            self.collection_id = value
            self.filters.pop("set_code", None)
        elif dd == "sort":
            return self.set_sort(value)
        else:
            key = "set_code" if dd == "set" else dd
            if value is None:
                self.filters.pop(key, None)
            else:
                self.filters[key] = value
        self.scroll = 0
        self.refresh(False)

    def set_sort(self, sort):
        self.sort = sort
        self.scroll = 0
        self.refresh(False)

    def scroll_to(self, row):
        self.scroll = max(0, min(row, max(0, len(self.ids) - self.visible_rows)))
        self.bump()

    def wheel(self, delta):
        if self.open_dd:
            return
        self.scroll_to(self.scroll + (-3 if delta > 0 else 3))

    def key(self, key):
        """Keyboard on the Collection tab. Typing goes straight into the search box."""
        nav = PAGE_KEYS.get(key)
        if self.focus == "search" or (nav is None and 32 < key < 127 and chr(key).isalnum()):
            if key == 27:
                if self.search:
                    self.search = ""
                    self.refresh(False)
                self.focus = None
            elif key in (13, 9):
                self.focus = None
            elif key == 8:
                self.search = self.search[:-1]
                self.refresh(False)
            elif nav in ("up", "down"):
                self.focus = None
                return self.key(key)
            elif 32 <= key < 127:
                self.focus = "search"
                self.search += chr(key)
                self.scroll = 0
                self.refresh(False)
            self.bump()
            return True
        if key == 27:
            self.selected, self.open_dd = None, None
        elif nav in ("up", "down") and self.ids:
            i = self.ids.index(self.selected) if self.selected in self.ids else (-1 if nav == "down" else len(self.ids))
            i = max(0, min(len(self.ids) - 1, i + (1 if nav == "down" else -1)))
            self.selected = self.ids[i]
            if i < self.scroll:
                self.scroll = i
            elif i >= self.scroll + self.visible_rows:
                self.scroll = i - self.visible_rows + 1
        elif nav == "pgdn":
            self.scroll_to(self.scroll + self.visible_rows)
        elif nav == "pgup":
            self.scroll_to(self.scroll - self.visible_rows)
        elif nav == "home":
            self.scroll_to(0)
        elif nav == "end":
            self.scroll_to(len(self.ids))
        elif key == 0x2E0000 and self.selected is not None:  # Delete
            self.action("remove")
        elif chr(key) in "+=" if 0 <= key < 256 else False:
            self.action("qty+")
        elif chr(key) == "-" if 0 <= key < 256 else False:
            self.action("qty-")
        else:
            return False
        self.bump()
        return True

    # ---- actions -------------------------------------------------------------------

    def _guard(self, fn, *args, **kw):
        """Run a database change; show refusals (e.g. cards used in decks) as a message."""
        try:
            return fn(*args, **kw)
        except (UserDBError, ValueError) as e:
            self.app.toast(str(e), RED)
            return FAILED

    def action(self, action):
        udb = self.app.userdb
        r = self.selected_row() if self.selected is not None else None
        if action == "manage":
            return self.manage()
        if action == "refresh":
            return self.refresh_prices()
        if action == "export":
            return self.export()
        if action == "import":
            return self.import_csv()
        if r is None:
            return
        if action == "qty+":
            self._guard(udb.set_quantity, r["id"], r["quantity"] + 1)
        elif action == "qty-":
            if r["quantity"] <= 1:
                return self.confirm_remove(r)
            self._guard(udb.set_quantity, r["id"], r["quantity"] - 1)
        elif isinstance(action, tuple) and action[0] == "finish" and action[1] != r["finish"]:
            f = action[1]
            price = {"foil": r.get("usd_foil"), "etched": r.get("usd_etched")}.get(f) or r.get("usd")
            new_id = self._guard(udb.update_item, r["id"], finish=f, market_price=float(price) if price else None)
            if isinstance(new_id, int):
                self.selected = new_id
        elif action == "condition":
            self.app.open_dialog(Dialog(
                "Condition", f"{r['card_name']} ({r['set_code'].upper()})", kind="choice",
                options=[(c, CONDITION_LABELS[c], "selected" if c == r["condition"] else "normal") for c in CONDITIONS],
                buttons=[("Cancel", None, "normal")],
                on_done=lambda v, _t: v and self._after(self._guard(udb.update_item, r["id"], condition=v))))
            return
        elif action == "purchase":
            cur, _ = display_currency(self.app.settings)
            self.app.open_dialog(Dialog(
                "Purchase price", [f"What you paid for each {r['card_name']}, in {cur}.", "Leave empty to clear it."],
                kind="input", numeric=True,
                text="" if r["purchase_price"] is None else f"{r['purchase_price']:.2f}",
                buttons=[("Cancel", None, "normal"), ("Save", True, "primary")],
                on_done=lambda v, t: v and self._save_purchase(r, t, cur)))
            return
        elif action == "notes":
            self.app.open_dialog(Dialog(
                "Notes", r["card_name"], kind="input", text=r["notes"] or "", placeholder="e.g. signed, from a trade",
                buttons=[("Cancel", None, "normal"), ("Save", True, "primary")],
                on_done=lambda v, t: v and self._after(self._guard(udb.update_item, r["id"], notes=t.strip() or None))))
            return
        elif action == "move":
            others = [c for c in self._collections() if c["id"] != r["collection_id"]]
            if not others:
                self.app.toast("Make another collection first (Manage > New collection)", YELLOW)
                return
            self.app.open_dialog(Dialog(
                "Move to collection", f"{r['quantity']} x {r['card_name']}", kind="choice",
                options=[(c["id"], c["name"], "normal") for c in others], buttons=[("Cancel", None, "normal")],
                on_done=lambda v, _t: v and self._after(self._guard(udb.move_items, [r["id"]], v),
                                                        f"Moved to {next(c['name'] for c in others if c['id'] == v)}")))
            return
        elif action == "remove":
            return self.confirm_remove(r)
        self.refresh()

    def _after(self, result, message=None):
        if result is not FAILED and message:
            self.app.toast(message, GREEN)
        if isinstance(result, int) and self.selected is not None:
            self.selected = result
        self.refresh()

    def _save_purchase(self, r, text, cur):
        if not text.strip():
            return self._after(self._guard(self.app.userdb.update_item, r["id"], purchase_price=None,
                                           purchase_currency=None))
        try:
            value = float(text.strip().replace(",", "."))
        except ValueError:
            return self.app.toast("That isn't a number", RED)
        self._after(self._guard(self.app.userdb.update_item, r["id"], purchase_price=value, purchase_currency=cur))

    def confirm_remove(self, r):
        def done(v, _t):
            if v and self._guard(self.app.userdb.delete_item, r["id"]) is not FAILED:
                self.selected = None
                self.app.toast(f"Removed {r['card_name']}", GREEN)
            self.refresh()
        self.app.open_dialog(Dialog(
            f"Remove {r['card_name']}?",
            [f"This takes {'the card' if r['quantity'] == 1 else 'all ' + str(r['quantity']) + ' copies'} "
             f"({r['set_code'].upper()} #{r['collector_number']}, {finish_label(r['finish']).lower()}) "
             f"out of {r['collection_name']}."],
            buttons=[("Cancel", None, "normal"), ("Remove", True, "danger")], on_done=done))

    # ---- collections ---------------------------------------------------------------

    def new_collection(self):
        def done(v, t):
            if v and t.strip():
                cid = self._guard(self.app.userdb.create_collection, t.strip())
                if isinstance(cid, int):
                    self.collection_id = cid
                    self.app.toast(f"Created {t.strip()}", GREEN)
            self.refresh()
        self.app.open_dialog(Dialog("New collection", "e.g. Trade Binder, Box 1, Expensive Cards", kind="input",
                                    buttons=[("Cancel", None, "normal"), ("Create", True, "primary")], on_done=done))

    def manage(self):
        cid = self.collection_id
        name = self._collection_name()
        options = [("new", "New collection", "normal")]
        is_main = cid is not None and any(c["id"] == cid and c["is_default"] for c in self._collections())
        if cid is not None:
            options.append(("rename", f"Rename {name}", "normal"))
            if not is_main:
                options.append(("delete", f"Delete {name}", "danger"))
        note = (f"{name} is your main collection - it can be renamed but not deleted." if is_main
                else "Pick a collection at the top left to manage it.")
        self.app.open_dialog(Dialog("Collections", note, kind="choice",
                                    options=options, buttons=[("Close", None, "normal")],
                                    on_done=lambda v, _t: {"new": self.new_collection, "rename": self.rename,
                                                           "delete": self.delete}.get(v, lambda: None)()))

    def rename(self):
        cid, name = self.collection_id, self._collection_name()

        def done(v, t):
            if v and t.strip() and t.strip() != name:
                if self._guard(self.app.userdb.rename_collection, cid, t.strip()) is not FAILED:
                    self.app.toast(f"Renamed to {t.strip()}", GREEN)
            self.refresh()
        self.app.open_dialog(Dialog(f"Rename {name}", kind="input", text=name,
                                    buttons=[("Cancel", None, "normal"), ("Rename", True, "primary")], on_done=done))

    def delete(self):
        udb = self.app.userdb
        cid, name = self.collection_id, self._collection_name()
        col = next((c for c in self._collections() if c["id"] == cid), None)
        if col is None:
            return
        if col["is_default"]:
            self.app.open_dialog(Dialog(f"{name} is protected", "Your main collection can't be deleted.",
                                        buttons=[("OK", None, "primary")]))
            return
        counts = udb.collection_counts(cid)

        def finish(result_msg):
            self.collection_id = udb.default_collection_id()
            self.selected = None
            self.app.toast(result_msg, GREEN)
            self.refresh()

        if not counts["copies"]:
            self.app.open_dialog(Dialog(f"Delete {name}?", "It's empty.", buttons=[
                ("Cancel", None, "normal"), ("Delete", True, "danger")],
                on_done=lambda v, _t: v and self._guard(udb.delete_collection, cid) is not FAILED and finish(f"Deleted {name}")))
            return
        others = [c for c in self._collections() if c["id"] != cid]
        n = counts["copies"]

        def chosen(v, _t):
            if v is None:
                return
            if v == "delete":
                self.app.open_dialog(Dialog(
                    f"Permanently delete {n} cards?",
                    [f"{name} and the {n} cards in it will be removed from your collection. This can't be undone "
                     "(a backup is only made before app upgrades)."],
                    buttons=[("Cancel", None, "normal"), (f"Delete {n} cards", True, "danger")],
                    on_done=lambda ok, _t2: ok and self._guard(udb.delete_collection, cid, "delete") is not FAILED
                    and finish(f"Deleted {name} and its {n} cards")))
            else:
                target = next(c for c in others if c["id"] == v)
                if self._guard(udb.delete_collection, cid, "move", v) is not FAILED:
                    finish(f"Moved {n} cards to {target['name']} and deleted {name}")
        self.app.open_dialog(Dialog(
            f"Delete {name}?", f"It has {n} card{'s' if n != 1 else ''} in it. What should happen to them?",
            kind="choice", options=[(c["id"], f"Move them to {c['name']}", "normal") for c in others]
            + [("delete", f"Delete the collection and its {n} cards", "danger")],
            buttons=[("Cancel", None, "normal")], on_done=chosen))

    # ---- prices / export / import ------------------------------------------------------

    def refresh_prices(self):
        n = self._guard(self.app.userdb.refresh_prices)
        if n is not FAILED:
            self.app.toast(f"Prices updated for {n:,} entries from your local card data", GREEN)
        self.refresh()

    def export(self):
        name = self._collection_name() if self.collection_id is not None else "all_collections"
        csv_path, txt_path = self.store.export(self.collection_id, self.app.export_dir(), name)
        self.app.toast(f"Exported {name} (ManaBox CSV + list) to exports\\", GREEN)
        open_folder(os.path.dirname(csv_path))

    def import_csv(self):
        path = self.app.ask_open_file("Import a ManaBox CSV", [("CSV files", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        try:
            parsed = self.store.parse_manabox(path)
        except (OSError, UnicodeDecodeError, ValueError) as e:
            return self.app.toast(f"Couldn't read that file: {e}", RED)
        target = self.collection_id or self.app.userdb.default_collection_id()
        target_name = next(c["name"] for c in self._collections() if c["id"] == target)
        self.app.open_dialog(ImportPreview(self, parsed, target, target_name, os.path.basename(path)))


class ImportPreview(Dialog):
    """Shows what a ManaBox CSV would add before anything is written."""

    def __init__(self, screen, parsed, collection_id, collection_name, filename):
        super().__init__("Import preview")
        self.screen, self.parsed = screen, parsed
        self.collection_id, self.collection_name, self.filename = collection_id, collection_name, filename
        self.include_names = False
        self.scroll = 0

    def counts(self):
        c = {"id": 0, "set": 0, "name": 0, "none": 0}
        q = {"id": 0, "set": 0, "name": 0, "none": 0}
        for p in self.parsed:
            c[p["status"]] += 1
            q[p["status"]] += p["quantity"]
        return c, q

    def draw(self, canvas_bgr):
        import numpy as np
        from PIL import Image, ImageDraw
        H, W = canvas_bgr.shape[:2]
        p = Painter(W, H)
        p.img = Image.fromarray(cv2.cvtColor((canvas_bgr * 0.35).astype(np.uint8), cv2.COLOR_BGR2RGB))
        p.d = ImageDraw.Draw(p.img)
        x0, y0, x1, y1 = 80, 30, W - 80, H - 30
        p.d.rounded_rectangle((x0, y0, x1, y1), radius=14, fill=CARD_BG, outline=BUTTON_HI)
        p.text((x0 + 24, y0 + 20), f"Import {self.filename} into {self.collection_name}", font="name", fill=YELLOW,
               width=x1 - x0 - 48)
        c, q = self.counts()
        y = y0 + 54
        p.text((x0 + 24, y), f"Matched exactly: {c['id'] + c['set']} rows ({q['id'] + q['set']} cards)", fill=GREEN)
        p.text((x0 + 380, y), f"Name only (printing guessed): {c['name']} rows ({q['name']} cards)", fill=YELLOW)
        p.text((x1 - 24, y), f"Not found: {c['none']}", fill=RED if c["none"] else MUTED, anchor="ra")
        y += 30
        rows_y, row_h = y + 22, 26
        for label, cx in (("Status", 0), ("Qty", 120), ("Card", 170), ("Set / #", 520), ("Finish", 640),
                          ("Cond.", 720), ("Note", 790)):
            p.text((x0 + 24 + cx, y), label, font="label", fill=MUTED)
        vis = (y1 - 80 - rows_y) // row_h
        order = {"none": 0, "name": 1, "set": 2, "id": 3}  # problems first
        rows = sorted(self.parsed, key=lambda r: order[r["status"]])
        self.scroll = max(0, min(self.scroll, max(0, len(rows) - vis)))
        for i, r in enumerate(rows[self.scroll:self.scroll + vis]):
            ry = rows_y + i * row_h
            st = {"id": ("Scryfall ID", GREEN), "set": ("Set + number", GREEN), "name": ("Name only", YELLOW),
                  "none": ("Not found", RED)}[r["status"]]
            p.text((x0 + 24, ry), st[0], font="small", fill=st[1])
            p.text((x0 + 144, ry), str(r["quantity"]), font="small")
            card = r["card"] or {}
            p.text((x0 + 194, ry), card.get("name") or r["row"].get("name", "?"), font="small", width=340)
            p.text((x0 + 544, ry), f"{(card.get('set_code') or r['row'].get('set code', '')).upper()} "
                                   f"#{card.get('collector_number') or r['row'].get('collector number', '')}",
                   font="small", fill=MUTED, width=110)
            p.text((x0 + 664, ry), finish_label(r["finish"]), font="small", fill=MUTED)
            p.text((x0 + 744, ry), COND_SHORT.get(r["condition"], r["condition"]), font="small", fill=MUTED)
            p.text((x0 + 814, ry), r["note"], font="small", fill=MUTED, width=x1 - x0 - 840)
        if len(rows) > vis:
            p.text((x0 + 24, y1 - 66), f"Showing {self.scroll + 1}-{min(len(rows), self.scroll + vis)} of {len(rows)} "
                                       "rows (scroll with the mouse wheel)", font="small", fill=MUTED)
        by = y1 - 50
        if c["name"]:
            box = "[x]" if self.include_names else "[  ]"
            p.button((x0 + 24, by, x0 + 420, by + 34), f"{box} Also import {q['name']} name-only cards",
                     ("toggle", None), style="selected" if self.include_names else "normal")
        n_cards = q["id"] + q["set"] + (q["name"] if self.include_names else 0)
        p.button((x1 - 24 - 220, by, x1 - 24, by + 34), f"Import {n_cards} cards", ("btn", True),
                 style="primary" if n_cards else "disabled")
        p.button((x1 - 24 - 330, by, x1 - 24 - 230, by + 34), "Cancel", ("btn", None))
        self.hits = p.hits
        return p.to_bgr()

    def click(self, x, y):
        for x0, y0, x1, y1, action in self.hits:
            if x0 <= x <= x1 and y0 <= y <= y1:
                if action[0] == "toggle":
                    self.include_names = not self.include_names
                elif action[0] == "btn":
                    self.finish(action[1])
                return

    def wheel(self, delta):
        self.scroll += -5 if delta > 0 else 5

    def key(self, key):
        if key == 27:
            self.finish(None)
        elif key == 13:
            self.finish(True)

    def finish(self, value):
        self.done = True
        if value:
            result = self.screen._guard(self.screen.store.commit_import, self.parsed, self.collection_id,
                                        self.include_names, self.filename)
            copies = 0 if result is FAILED else result[0]
            if copies:
                self.screen.app.toast(f"Imported {copies:,} cards into {self.collection_name}", GREEN)
            self.screen.refresh()


def num(cn):
    return str(cn).replace("\u2605", "*")


def _wrap2(p, text, font, width):
    f = p.f[font]
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if f.getlength(trial) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    if len(lines) > 2:
        lines = [lines[0], p.fit(" ".join(lines[1:]), f, width)]
    return lines
