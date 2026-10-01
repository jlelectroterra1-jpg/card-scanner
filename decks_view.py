"""The Decks tab: your Commander decks, one deck's cards grouped by type, card details,
and the dialogs for creating, searching, importing, exporting and managing decks.
Scanning a deck itself happens on the Scanner tab (Scan Deck mode, see deckscan.py)."""
import os
import threading
from datetime import datetime, timezone

import cv2
import requests

import deckrules
from collection import CONDITION_LABELS, finish_label
from decks import OWNERSHIP_LABELS, DeckStore
from ui import (BG, BUTTON, BUTTON_HI, CARD_BG, GOLD, GREEN, MUTED, RED, ROW_ALT, ROW_SEL, TEXT, YELLOW, Dialog,
                Painter, open_folder)
from userdb import FINISHES, UserDBError

HERE = os.path.dirname(os.path.abspath(__file__))
IMG_DIR = os.path.join(HERE, "data", "img")
LARGE_DIR = os.path.join(HERE, "data", "img_large")
FAILED = object()
HIGH_VALUE = 5.0
PIP = {"W": (240, 230, 200), "U": (110, 170, 240), "B": (150, 130, 150), "R": (235, 110, 90), "G": (110, 200, 120)}
OWN_COLOUR = {"exact": GREEN, "different_finish": GREEN, "different_printing": GREEN, "deck_only": YELLOW,
              "available": YELLOW, "in_use": RED, "missing": RED}
OWN_SHORT = {"exact": "Owned - exact", "different_finish": "Owned - other finish",
             "different_printing": "Owned - other printing", "deck_only": "Deck only",
             "available": "In Collection (link)", "in_use": "In another deck", "missing": "Not owned"}
LIST_X0, LIST_X1, PANE_X0 = 16, 930, 946
ROW_H, GROUP_H = 34, 26


def when(ts):
    """'2026-10-01T10:00:00Z' -> 'today' / 'yesterday' / '3 days ago' / '12 Sep'."""
    if not ts:
        return ""
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts[:10]
    days = (datetime.now(timezone.utc).date() - t.date()).days
    return "today" if days <= 0 else "yesterday" if days == 1 else f"{days} days ago" if days < 14 else t.strftime("%d %b")


def num(cn):
    return str(cn or "").replace("★", "*")


class Images:
    """Card pictures: small ones from data/img, big ones fetched once in the background."""

    def __init__(self, on_arrive):
        self.cache, self.fetching, self.on_arrive = {}, set(), on_arrive

    def small(self, scryfall_id, url=None, w=None, h=None):
        if not scryfall_id:
            return None
        key = (scryfall_id, w, h)
        if key in self.cache:
            return self.cache[key]
        path = os.path.join(IMG_DIR, scryfall_id + ".jpg")
        img = cv2.imread(path) if os.path.exists(path) else None
        if img is None:
            self.fetch(url, path)
            return None
        if w:
            img = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
        if len(self.cache) > 800:
            self.cache.clear()
        self.cache[key] = img
        return img

    def large(self, scryfall_id, url_large=None, url_small=None):
        if not scryfall_id:
            return None
        path = os.path.join(LARGE_DIR, scryfall_id + ".jpg")
        if os.path.exists(path):
            img = cv2.imread(path)
            if img is not None:
                return img
        self.fetch(url_large, path)
        return self.small(scryfall_id, url_small)

    def fetch(self, url, path):
        if not url or path in self.fetching or len(self.fetching) > 6:
            return
        self.fetching.add(path)

        def run():
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                data = requests.get(url, headers={"User-Agent": "HomeCardScanner/0.3"}, timeout=20).content
                with open(path + ".part", "wb") as f:
                    f.write(data)
                os.replace(path + ".part", path)
                self.cache = {k: v for k, v in self.cache.items() if k[0] != os.path.basename(path)[:-4]}
                self.on_arrive()
            except (requests.RequestException, OSError):
                pass
            finally:
                self.fetching.discard(path)
        threading.Thread(target=run, daemon=True).start()


class DecksScreen:
    def __init__(self, app, width, height, store=None):
        self.app = app
        self.W, self.H = width, height
        self.store = store or DeckStore(app.userdb)
        self.view, self.deck_id, self.selected = "list", None, None
        self.grouping, self.sort = "type", "name"
        self.scroll = self.list_scroll = 0
        self.open_dd = None
        self.decks, self.rows, self.info = [], [], None
        self.version = 0
        self._key = self._cache = None
        self.images = Images(self.bump)
        self.refresh()

    # ---- data ---------------------------------------------------------------------

    def bump(self):
        self.version += 1

    def refresh(self):
        self.decks = self.store.deck_list()
        if self.view == "deck" and self.deck_id is not None:
            if self.app.userdb.deck(self.deck_id) is None:
                self.view, self.deck_id = "list", None
            else:
                self.rows = self.store.deck_cards(self.deck_id)
                self.info = self.store.summary(self.deck_id, self.rows)
                if self.selected is not None and self.selected not in {r["id"] for r in self.rows}:
                    self.selected = None
        self.bump()

    def open_deck(self, deck_id):
        self.view, self.deck_id, self.selected, self.scroll = "deck", deck_id, None, 0
        self.refresh()

    def money(self, usd):
        return self.app.money(usd)

    def display_rows(self):
        """The deck's cards as list lines: ('group', label, count) and ('card', row)."""
        rows = list(self.rows)
        key = (lambda r: (-r["price"], r["card_name"].lower())) if self.sort == "price" else \
            (lambda r: r["card_name"].lower())
        rows.sort(key=key)
        out = []
        if self.grouping == "type":
            for g in deckrules.GROUP_ORDER:
                part = [r for r in rows if r["group"] == g and r["role"] in ("commander", "partner", "main")]
                if part:
                    out.append(("group", deckrules.GROUP_LABELS[g], sum(r["quantity"] for r in part)))
                    out += [("card", r) for r in part]
        else:
            part = [r for r in rows if r["role"] in ("commander", "partner", "main")]
            out.append(("group", "All cards", sum(r["quantity"] for r in part)))
            out += [("card", r) for r in part]
        for role, label in (("companion", "Companion"), ("sideboard", "Sideboard"), ("maybe", "Maybeboard")):
            part = [r for r in rows if r["role"] == role]
            if part:
                out.append(("group", label, sum(r["quantity"] for r in part)))
                out += [("card", r) for r in part]
        return out

    # ---- drawing -----------------------------------------------------------------------

    def render(self):
        toast = self.app.toast_text()
        pending = self.app.pending_deck_scan()
        key = (self.version, self.view, self.deck_id, self.selected, self.scroll, self.list_scroll, self.open_dd,
               self.grouping, self.sort, toast, repr(self.app.settings.get("currency")),
               self.app.settings.get("usd_zar"), pending and (pending["deck_id"], len(pending["entries"])))
        if key == self._key and self._cache is not None:
            return self._cache
        p = Painter(self.W, self.H, BG)
        popup = None
        if self.view == "list":
            self._deck_list(p, pending)
        else:
            popup = self._deck_view(p, pending)
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

    def _pips(self, p, x, y, identity, size=18):
        if not identity:
            p.d.ellipse((x, y, x + size, y + size), fill=(170, 170, 175))
            p.text((x + size / 2, y + size / 2), "C", font="label", fill=(30, 30, 30), anchor="mm")
            return x + size + 4
        for ch in identity:
            p.d.ellipse((x, y, x + size, y + size), fill=PIP.get(ch, MUTED))
            p.text((x + size / 2, y + size / 2), ch, font="label", fill=(30, 30, 30), anchor="mm")
            x += size + 4
        return x

    def _deck_list(self, p, pending):
        p.text((LIST_X0, 28), "Decks", font="title", anchor="lm")
        p.text((LIST_X0 + 80, 30), f"{len(self.decks)} deck{'s' if len(self.decks) != 1 else ''}", font="body",
               fill=MUTED, anchor="lm")
        x = self.W - 16
        for label, action, style in reversed([("Import Deck", "import_new", "normal"), ("New Deck", "new", "primary")]):
            w = p.f["button"].getlength(label) + 30
            p.button((x - w, 12, x, 46), label, action, style=style)
            x -= w + 8
        y = 60
        if pending:
            p.d.rounded_rectangle((LIST_X0, y, self.W - 16, y + 44), radius=10, fill=(52, 46, 30))
            p.text((LIST_X0 + 14, y + 22), f"Unfinished scan: {pending['deck_name']} - {len(pending['entries'])} cards "
                                           "scanned so far", font="body", fill=YELLOW, anchor="lm")
            p.button((self.W - 16 - 230, y + 6, self.W - 16 - 126, y + 38), "Resume", "resume_pending", style="primary")
            p.button((self.W - 16 - 118, y + 6, self.W - 24, y + 38), "Discard", "discard_pending", style="danger")
            y += 54
        if not self.decks:
            p.text((self.W / 2, y + 140), "No decks yet", font="name", anchor="mm")
            p.text((self.W / 2, y + 170), "Click New Deck, choose the commander, then Scan Deck and feed the cards "
                                          "through the scanner.", font="body", fill=MUTED, anchor="mm")
            return
        tile_h = 96
        vis = (self.H - y - 8) // tile_h
        self.list_scroll = max(0, min(self.list_scroll, max(0, len(self.decks) - vis)))
        for i, d in enumerate(self.decks[self.list_scroll:self.list_scroll + vis]):
            ty = y + i * tile_h
            box = (LIST_X0, ty, self.W - 16, ty + tile_h - 8)
            p.d.rounded_rectangle(box, radius=12, fill=CARD_BG)
            p.paste(self.images.small(d.get("commander_scryfall_id"), d.get("commander_image")), LIST_X0 + 10, ty + 6,
                    56, 78, radius=5)
            p.text((LIST_X0 + 80, ty + 12), d["name"], font="name", width=520)
            cmd = d["commander"] or "No commander yet"
            if d.get("partner"):
                cmd += " + " + d["partner"]
            p.text((LIST_X0 + 80, ty + 38), cmd, font="body", fill=MUTED if d["commander"] else YELLOW, width=520)
            p.text((LIST_X0 + 80, ty + 62), f"{d['card_count']} cards", font="row", fill=TEXT)
            ox = LIST_X0 + 80 + p.f["row"].getlength(f"{d['card_count']} cards") + 16
            p.text((ox, ty + 62), f"{d['owned']} owned", font="row", fill=GREEN)
            ox += p.f["row"].getlength(f"{d['owned']} owned") + 10
            p.text((ox, ty + 62), f"/ {d['missing']} missing", font="row", fill=RED if d["missing"] else MUTED)
            p.text((self.W - 34, ty + 30), self.money(d["value"]), font="big", anchor="rm",
                   fill=GOLD if d["value"] >= 100 else TEXT)
            p.text((self.W - 34, ty + 64), f"{d['format'].title()} - updated {when(d['updated_at'])}", font="small",
                   fill=MUTED, anchor="rm")
            p.hit(box, ("open", d["deck_id"]))
        if len(self.decks) > vis:
            p.text((LIST_X0, self.H - 14), f"{self.list_scroll + 1}-{min(len(self.decks), self.list_scroll + vis)} of "
                                           f"{len(self.decks)} decks (scroll for more)", font="small", fill=MUTED,
                   anchor="lm")

    def _deck_view(self, p, pending):
        info = self.info
        if info is None:
            return None
        # top bar
        p.button((LIST_X0, 10, LIST_X0 + 90, 44), "< Decks", "back")
        p.text((LIST_X0 + 104, 27), info["name"], font="title", anchor="lm", width=430)
        tx = LIST_X0 + 104 + min(430, p.f["title"].getlength(info["name"])) + 14
        tx = p.chip(tx, 17, info["format"].title(), MUTED)
        self._pips(p, tx + 4, 18, info["identity"])
        x = self.W - 16
        scanning_this = pending and pending["deck_id"] == self.deck_id
        scan_label = f"Resume scan ({len(pending['entries'])})" if scanning_this else "Scan Deck"
        anchors = {}
        for label, action, style in reversed([(scan_label, "scan", "primary"), ("Add Card", "add", "normal"),
                                              ("Commander", "dd:commander", "dd"), ("More", "dd:more", "dd")]):
            w = p.f["button"].getlength(label) + (40 if style == "dd" else 28)
            if style == "dd":
                p.dropdown((x - w, 10, x, 44), label, action, active=self.open_dd == action[3:])
                anchors[action[3:]] = x - w
            else:
                p.button((x - w, 10, x, 44), label, action, style=style)
            x -= w + 8
        # summary line
        y = 56
        n = info["count"]
        if n == deckrules.DECK_SIZE:
            count_text, count_colour = f"Deck complete - {n} cards", GREEN
        elif n > deckrules.DECK_SIZE:
            count_text, count_colour = f"{n} / {deckrules.DECK_SIZE} cards - too many", YELLOW
        else:
            count_text, count_colour = f"{n} / {deckrules.DECK_SIZE} cards", TEXT
        p.text((LIST_X0, y + 14), count_text, font="name", fill=count_colour, anchor="lm")
        x = LIST_X0 + p.f["name"].getlength(count_text) + 20
        p.text((x, y + 14), self.money(info["value"]), font="name", fill=GOLD if info["value"] >= 100 else TEXT,
               anchor="lm")
        x += p.f["name"].getlength(self.money(info["value"])) + 20
        own = f"{info['owned']} owned"
        p.text((x, y + 14), own, font="body", fill=GREEN, anchor="lm")
        x += p.f["body"].getlength(own) + 10
        p.text((x, y + 14), f"{info['missing']} missing", font="body", fill=RED if info["missing"] else MUTED,
               anchor="lm")
        # grouping / sorting toggles
        gx = LIST_X1 - 12
        for label, action, on in reversed([("By type", ("group", "type"), self.grouping == "type"),
                                           ("All A-Z", ("group", "all"), self.grouping == "all"),
                                           ("Name", ("sort", "name"), self.sort == "name"),
                                           ("Price", ("sort", "price"), self.sort == "price")]):
            w = p.f["label"].getlength(label) + 20
            p.button((gx - w, y + 2, gx, y + 26), label, action, style="selected" if on else "ghost", font="label")
            gx -= w + (14 if label == "Name" else 4)
        self._card_list(p, y + 36)
        if self.selected is not None:
            self._card_detail(p)
        else:
            self._overview(p)
        if not self.open_dd:
            return None
        if self.open_dd == "commander":
            items = [("search_cmd", "Search for the commander"), ("scan_cmd", "Scan the commander"),
                     ("search_partner", "Search for a partner"), ("scan_partner", "Scan a partner")]
            if any(r["role"] == "partner" for r in self.rows):
                items.append(("remove_partner", "Remove the partner"))
            width = 250
        else:
            items = [("rename", "Rename deck"), ("duplicate", "Duplicate deck"), ("import_into", "Import cards into deck"),
                     ("export", "Export deck"), ("link_all", "Link owned copies now"), ("delete", "Delete deck")]
            width = 230
        x0 = min(anchors.get(self.open_dd, self.W - width - 16), self.W - width - 8)
        return lambda pp: pp.popup_list(x0, 48, width, items, "menu", None, max_rows=10)

    def _card_list(self, p, top):
        lines = self.display_rows()
        heights = [GROUP_H if l[0] == "group" else ROW_H for l in lines]
        avail = self.H - top - 8
        # scroll is in lines; clamp so the last lines are reachable
        total, maxs = 0, len(lines)
        for i in range(len(lines) - 1, -1, -1):
            total += heights[i]
            if total > avail:
                maxs = i + 1
                break
        else:
            maxs = 0
        self.scroll = max(0, min(self.scroll, maxs))
        y = top
        if not lines:
            p.text(((LIST_X0 + LIST_X1) / 2, top + 120), "No cards yet - press Scan Deck, Add Card or Import",
                   font="body", fill=MUTED, anchor="mm")
        problem = {w["card"] for w in (self.info or {}).get("warnings", []) if w.get("card")}
        for i in range(self.scroll, len(lines)):
            line = lines[i]
            h = heights[i]
            if y + h > self.H - 6:
                break
            if line[0] == "group":
                p.text((LIST_X0 + 4, y + 15), f"{line[1].upper()}  ({line[2]})", font="label", fill=YELLOW, anchor="lm")
                y += h
                continue
            r = line[1]
            bg = ROW_SEL if r["id"] == self.selected else (ROW_ALT if i % 2 else BG)
            p.d.rectangle((LIST_X0, y, LIST_X1 - 12, y + h - 2), fill=bg)
            p.paste(self.images.small(r["scryfall_id"], r.get("image_small"), 22, 31), LIST_X0 + 4, y + 1, 22, 31,
                    radius=3)
            p.text((LIST_X0 + 34, y + 16), f"{r['quantity']}", font="rowb", fill=MUTED, anchor="lm")
            name = r["card_name"] + ("  (commander)" if r["role"] == "commander" else
                                     "  (partner)" if r["role"] == "partner" else "")
            p.text((LIST_X0 + 54, y + 16), name, font="rowb", anchor="lm", width=300,
                   fill=YELLOW if r["role"] in ("commander", "partner") else TEXT)
            if r["card_name"] in problem:
                p.text((LIST_X0 + 362, y + 16), "!", font="rowb", fill=RED, anchor="lm")
            p.text((LIST_X0 + 380, y + 16), f"{(r.get('set_code') or '').upper()} #{num(r.get('collector_number'))}",
                   font="small", fill=MUTED, anchor="lm", width=96)
            p.text((LIST_X0 + 482, y + 16), finish_label(r["finish"] or "nonfoil"), font="small",
                   fill=YELLOW if (r["finish"] or "nonfoil") != "nonfoil" else MUTED, anchor="lm")
            p.text((LIST_X0 + 620, y + 16), self.money(r["price"]), font="row", anchor="rm",
                   fill=GOLD if r["price"] >= HIGH_VALUE else MUTED)
            p.text((LIST_X0 + 640, y + 16), OWN_SHORT[r["ownership"]], font="small", fill=OWN_COLOUR[r["ownership"]],
                   anchor="lm", width=250)
            p.hit((LIST_X0, y, LIST_X1 - 12, y + h - 2), ("card", r["id"]))
            y += h

    def _pane(self, p, title=""):
        x0, y0, x1, y1 = PANE_X0, 56, self.W - 16, self.H - 10
        p.d.rounded_rectangle((x0, y0, x1, y1), radius=12, fill=CARD_BG)
        if title:
            p.text((x0 + 16, y0 + 14), title, font="name", fill=YELLOW)
        return x0, y0, x1, y1

    def _overview(self, p):
        info = self.info
        x0, y0, x1, y1 = self._pane(p)
        cmds = info["commanders"]
        if cmds:
            c = cmds[0]
            p.paste(self.images.large(c["scryfall_id"], c.get("image_normal"), c.get("image_small")), x0 + 14, y0 + 14,
                    130, 181, radius=7)
            if len(cmds) > 1:
                c2 = cmds[1]
                p.paste(self.images.small(c2["scryfall_id"], c2.get("image_small")), x0 + 100, y0 + 120, 60, 84,
                        radius=5)
        else:
            p.d.rounded_rectangle((x0 + 14, y0 + 14, x0 + 144, y0 + 195), radius=7, fill=BUTTON)
            p.text((x0 + 79, y0 + 104), "No commander", font="small", fill=MUTED, anchor="mm")
            p.hit((x0 + 14, y0 + 14, x0 + 144, y0 + 195), "search_cmd")
        tx = x0 + 160
        y = y0 + 16
        names = " + ".join(c["card_name"] for c in cmds) or "Choose a commander (Commander menu)"
        for line in _wrap(p, names, "rowb", x1 - tx - 12)[:3]:
            p.text((tx, y), line, font="rowb", fill=YELLOW if cmds else MUTED)
            y += 20
        y += 4
        self._pips(p, tx, y, info["identity"])
        y += 26
        p.text((tx, y), deckrules.colour_words(info["identity"]), font="small", fill=MUTED, width=x1 - tx - 12)
        y += 26
        o = info["ownership"]
        for label, value, colour in (("Exact printing", o["exact"], GREEN),
                                     ("Other printing / finish", o["different_finish"] + o["different_printing"], GREEN),
                                     ("In deck, not in Collection", o["deck_only"], YELLOW),
                                     ("Not owned / used elsewhere", info["missing"], RED)):
            p.text((tx, y), label, font="small", fill=MUTED)
            p.text((x1 - 16, y), str(value), font="rowb", fill=colour if value else MUTED, anchor="ra")
            y += 19
        y = max(y, y0 + 205) + 8
        warns = info["warnings"]
        p.text((x0 + 16, y), f"WARNINGS ({len(warns)})" if warns else "NO WARNINGS", font="label",
               fill=RED if warns else GREEN)
        y += 20
        shown = 0
        for w in warns:
            if y > y1 - 150:
                break
            lines = _wrap(p, w["text"], "small", x1 - x0 - 44)[:2]
            p.text((x0 + 16, y), "!", font="rowb", fill=RED if w["kind"] in ("legality", "colour", "duplicate") else YELLOW)
            for line in lines:
                p.text((x0 + 30, y), line, font="small")
                y += 17
            y += 3
            shown += 1
        if shown < len(warns):
            p.text((x0 + 30, y), f"... and {len(warns) - shown} more", font="small", fill=MUTED)
            y += 20
        y = max(y + 6, y1 - 130)
        p.text((x0 + 16, y), "MOST VALUABLE", font="label", fill=MUTED)
        y += 20
        for r in info["top"]:
            if y > y1 - 20:
                break
            p.text((x0 + 16, y), r["card_name"], font="small", width=x1 - x0 - 120)
            p.text((x1 - 16, y), self.money(r["price"]), font="rowb", fill=GOLD if r["price"] >= HIGH_VALUE else TEXT,
                   anchor="ra")
            p.hit((x0 + 10, y - 2, x1 - 10, y + 18), ("card", r["id"]))
            y += 20

    def _card_detail(self, p):
        r = next((x for x in self.rows if x["id"] == self.selected), None)
        if r is None:
            return self._overview(p)
        x0, y0, x1, y1 = self._pane(p)
        p.button((x1 - 34, y0 + 8, x1 - 8, y0 + 34), "x", "close")
        iw, ih = 150, 209
        p.paste(self.images.large(r["scryfall_id"], r.get("image_normal"), r.get("image_small")), x0 + 14, y0 + 14,
                iw, ih, radius=7)
        tx, tw = x0 + 14 + iw + 14, x1 - (x0 + 14 + iw + 14) - 40
        y = y0 + 16
        for line in _wrap(p, r["card_name"], "name", tw + 30)[:2]:
            p.text((tx, y), line, font="name")
            y += 22
        p.text((tx, y + 2), r.get("set_name") or "", font="small", fill=MUTED, width=tw + 30)
        y += 20
        p.text((tx, y + 2), f"{(r.get('set_code') or '').upper()} #{num(r.get('collector_number'))} - "
                            f"{(r.get('rarity') or '').title()}", font="small", fill=MUTED, width=tw + 30)
        y += 20
        p.text((tx, y + 2), r.get("type_line") or "", font="small", fill=MUTED, width=tw + 30)
        y += 22
        self._pips(p, tx, y + 2, r.get("color_identity") or "", size=16)
        y += 26
        p.text((tx, y), self.money(r["price"]), font="big", fill=GOLD if r["price"] >= HIGH_VALUE else TEXT)
        y += 34
        p.text((tx, y), f"each - total {self.money(r['value'])}", font="small", fill=MUTED)
        y += 22
        p.text((tx, y), OWNERSHIP_LABELS[r["ownership"]], font="rowb", fill=OWN_COLOUR[r["ownership"]],
               width=tw + 30)
        # ownership details
        y = y0 + 14 + ih + 10
        copies = self.store.copies(r["card_name"])
        owned = sum(c["quantity"] for c in copies)
        free = sum(c["available"] for c in copies)
        p.text((x0 + 16, y), f"Owned: {owned}   Available: {free}", font="body", fill=TEXT if owned else MUTED)
        y += 22
        used = {}
        for c in copies:
            for u in c["used_in"]:
                used[u["name"]] = used.get(u["name"], 0) + u["copies"]
        if used:
            text = "Used in: " + ", ".join(f"{n} ({q})" for n, q in used.items())
            for line in _wrap(p, text, "small", x1 - x0 - 32)[:2]:
                p.text((x0 + 16, y), line, font="small", fill=MUTED)
                y += 18
        y += 4
        # editing
        lx, vx = x0 + 16, x0 + 110
        p.text((lx, y + 14), "Quantity", font="small", fill=MUTED, anchor="lm")
        p.button((vx, y, vx + 32, y + 28), "-", "qty-")
        p.text((vx + 52, y + 14), str(r["quantity"]), font="name", anchor="mm")
        p.button((vx + 72, y, vx + 104, y + 28), "+", "qty+")
        y += 34
        p.text((lx, y + 14), "Finish", font="small", fill=MUTED, anchor="lm")
        avail = [f for f in (r.get("available_finishes") or "nonfoil").split(",") if f in FINISHES]
        bx = vx
        for f in avail:
            w = p.f["label"].getlength(finish_label(f)) + 20
            p.button((bx, y, bx + w, y + 28), finish_label(f), ("finish", f),
                     style="selected" if f == (r["finish"] or "nonfoil") else "ghost", font="label")
            bx += w + 4
        y += 34
        half = (x1 - x0 - 32 - 6) / 2
        p.button((x0 + 16, y, x0 + 16 + half, y + 28), "Change printing", "printing", font="label")
        if r["collection_item_id"]:
            p.button((x0 + 22 + half, y, x1 - 16, y + 28), "Unlink copy", "unlink", font="label")
        elif r["ownership"] == "available":
            p.button((x0 + 22 + half, y, x1 - 16, y + 28), "Link owned copy", "link", style="primary", font="label")
        else:
            p.button((x0 + 22 + half, y, x1 - 16, y + 28), "Link owned copy", None, style="disabled", font="label")
        y += 34
        if r["role"] in ("commander", "partner"):
            p.button((x0 + 16, y, x0 + 16 + half, y + 28), "Make main-deck card", ("role", "main"), font="label")
        else:
            p.button((x0 + 16, y, x0 + 16 + half, y + 28), "Make commander", ("role", "commander"), font="label")
        if r["role"] != "partner":
            p.button((x0 + 22 + half, y, x1 - 16, y + 28), "Make partner", ("role", "partner"), font="label")
        y = y1 - 40
        p.button((x0 + 16, y, x1 - 16, y + 30), "Remove from deck", "remove", style="danger")

    # ---- input ---------------------------------------------------------------------------

    def click(self, x, y, hits):
        action = None
        for x0, y0, x1, y1, a in reversed(hits):
            if x0 <= x <= x1 and y0 <= y <= y1:
                action = a
                break
        was = self.open_dd
        self.open_dd = None
        if action is None:
            self.bump()
            return
        if isinstance(action, str) and action.startswith("dd:"):
            self.open_dd = None if was == action[3:] else action[3:]
        elif isinstance(action, tuple) and action[0] == "menu":
            self.menu(action[1])
        elif isinstance(action, tuple) and action[0] == "open":
            self.open_deck(action[1])
        elif isinstance(action, tuple) and action[0] == "card":
            self.selected = None if self.selected == action[1] else action[1]
        elif isinstance(action, tuple) and action[0] == "group":
            self.grouping, self.scroll = action[1], 0
        elif isinstance(action, tuple) and action[0] == "sort":
            self.sort, self.scroll = action[1], 0
        elif action == "back":
            self.view, self.selected = "list", None
            self.refresh()
        elif action == "close":
            self.selected = None
        else:
            self.action(action)
        self.bump()

    def wheel(self, delta):
        if self.view == "list":
            self.list_scroll = max(0, self.list_scroll + (-1 if delta > 0 else 1))
        else:
            self.scroll = max(0, self.scroll + (-3 if delta > 0 else 3))
        self.bump()

    def key(self, key):
        if key == 27:
            if self.open_dd:
                self.open_dd = None
            elif self.selected is not None:
                self.selected = None
            elif self.view == "deck":
                self.view = "list"
                self.refresh()
        elif key == 0x2E0000 and self.selected is not None:
            self.action("remove")
        elif key in (0x210000, 0x260000):  # PgUp / Up
            self.wheel(1)
        elif key in (0x220000, 0x280000):
            self.wheel(-1)
        else:
            return False
        self.bump()
        return True

    # ---- actions ---------------------------------------------------------------------------

    def _guard(self, fn, *args, **kw):
        try:
            return fn(*args, **kw)
        except (UserDBError, ValueError) as e:
            self.app.toast(str(e), RED)
            return FAILED

    def menu(self, item):
        {"search_cmd": lambda: self.pick_commander(False), "scan_cmd": lambda: self.app.start_commander_scan(
            self.deck_id, False), "search_partner": lambda: self.pick_commander(True),
         "scan_partner": lambda: self.app.start_commander_scan(self.deck_id, True),
         "remove_partner": self.remove_partner, "rename": self.rename, "duplicate": self.duplicate,
         "import_into": lambda: self.import_deck(into=self.deck_id), "export": self.export,
         "link_all": self.link_all, "delete": self.delete}.get(item, lambda: None)()

    def action(self, action):
        if action == "new":
            return self.new_deck()
        if action == "import_new":
            return self.import_deck()
        if action == "resume_pending":
            return self.app.resume_deck_scan()
        if action == "discard_pending":
            return self.app.confirm_discard_deck_scan()
        if action == "search_cmd":
            return self.pick_commander(False)
        if action == "scan":
            return self.app.start_deck_scan(self.deck_id)
        if action == "add":
            return self.app.open_dialog(CardPicker(self, "Add a card", self._add_picked))
        r = next((x for x in self.rows if x["id"] == self.selected), None)
        if r is None:
            return
        udb = self.app.userdb
        if action == "qty+":
            self._guard(udb.update_deck_card, r["id"], quantity=r["quantity"] + 1)
            limit = deckrules.copy_limit(r["card_name"], r.get("type_line"))
            if limit is not None and r["quantity"] + 1 > limit:
                self.app.toast(f"Note: Commander allows {limit} {r['card_name']}", YELLOW)
        elif action == "qty-":
            if r["quantity"] <= 1:
                return self.confirm_remove(r)
            self._guard(udb.update_deck_card, r["id"], quantity=r["quantity"] - 1)
        elif isinstance(action, tuple) and action[0] == "finish":
            self._guard(self.store.change_finish, r["id"], action[1])
        elif action == "printing":
            return self.app.open_dialog(CardPicker(
                self, f"Printing of {r['card_name']}", lambda card, finish: self._after(
                    self._guard(self.store.change_printing, r["id"], card, finish)), name=r["card_name"]))
        elif action == "unlink":
            self._guard(self.store.unlink, r["id"])
        elif action == "link":
            return self.choose_copy(r)
        elif isinstance(action, tuple) and action[0] == "role":
            self._guard(udb.set_role, r["id"], action[1])
        elif action == "remove":
            return self.confirm_remove(r)
        self.refresh()

    def _after(self, result, message=None):
        if result is not FAILED and message:
            self.app.toast(message, GREEN)
        self.refresh()

    def _add_picked(self, card, finish):
        n = self._guard(self.store.add_card, self.deck_id, card, finish)
        if n is not FAILED:
            name = card["name"]
            have = sum(r["quantity"] for r in self.rows if r["card_name"] == name and r["role"] in ("main", "commander",
                                                                                                  "partner"))
            limit = deckrules.copy_limit(name, card.get("type_line"))
            extra = f" - note: already {have} in the deck" if limit is not None and have >= limit else ""
            self.app.toast(f"Added {name}{extra}", YELLOW if extra else GREEN)
        self.refresh()

    def confirm_remove(self, r):
        self.app.open_dialog(Dialog(
            f"Remove {r['card_name']}?", ["It comes out of this deck." + (" The collection copy becomes free again."
                                                                          if r["collection_item_id"] else "")],
            buttons=[("Cancel", None, "normal"), ("Remove", True, "danger")],
            on_done=lambda v, _t: v and self._after(self._guard(self.app.userdb.remove_deck_card, r["id"]),
                                                    f"Removed {r['card_name']}")))

    def choose_copy(self, r):
        free = [c for c in self.store.copies(r["card_name"]) if c["available"] > 0]
        if not free:
            return self.app.toast("No free copy - every copy is used in a deck", RED)
        opts = []
        for c in free:
            card = self.store.card(c["scryfall_id"]) or {}
            opts.append((c["collection_item_id"], f"{(card.get('set_code') or '').upper()} "
                                                  f"#{num(card.get('collector_number'))} {finish_label(c['finish'])}, "
                                                  f"{CONDITION_LABELS.get(c['condition'], c['condition'])} - "
                                                  f"{c['collection_name']} ({c['available']} free)", "normal"))
        self.app.open_dialog(Dialog(f"Link a copy of {r['card_name']}", kind="choice", options=opts[:8],
                                    buttons=[("Cancel", None, "normal")],
                                    on_done=lambda v, _t: v and self._after(self._guard(self.store.link, r["id"], v),
                                                                            "Linked to your copy")))

    def link_all(self):
        n = self._guard(self.store.allocate, self.deck_id)
        if n is not FAILED:
            self.app.toast(f"Linked {n} owned cop{'y' if n == 1 else 'ies'}" if n else "No free copies to link",
                           GREEN if n else MUTED)
        self.refresh()

    def pick_commander(self, partner):
        def picked(card, finish):
            self._after(self._guard(self.store.add_card, self.deck_id, card, finish,
                                    role="partner" if partner else "commander"),
                        f"{card['name']} is now the {'partner ' if partner else ''}commander")
        self.app.open_dialog(CardPicker(self, "Choose the partner" if partner else "Choose the commander", picked,
                                        legendary_first=True))

    def remove_partner(self):
        r = next((x for x in self.rows if x["role"] == "partner"), None)
        if r:
            self.confirm_remove(r)

    def new_deck(self):
        def named(ok, text):
            if not ok or not text.strip():
                return

            def fmt(v, _t):
                if v is None:
                    return
                deck = self._guard(self.store.create_deck, text.strip(), v)
                if deck is FAILED:
                    return
                self.open_deck(deck)
                self.app.open_dialog(Dialog(
                    "Choose the commander", f"How do you want to pick the commander for {text.strip()}?",
                    kind="choice", options=[("search", "Search by name", "primary"),
                                            ("scan", "Scan it with the camera", "normal")],
                    buttons=[("Later", None, "normal")],
                    on_done=lambda how, _t2: how == "search" and self.pick_commander(False)
                    or how == "scan" and self.app.start_commander_scan(deck, False)))
            self.app.open_dialog(Dialog("Format", f"Format for {text.strip()}", kind="choice",
                                        options=[("commander", "Commander", "primary"), ("brawl", "Brawl", "normal"),
                                                 ("casual", "Casual / other", "normal")],
                                        buttons=[("Cancel", None, "normal")], on_done=fmt))
        self.app.open_dialog(Dialog("New deck", "Name the deck, e.g. My Atraxa Deck", kind="input",
                                    buttons=[("Cancel", None, "normal"), ("Next", True, "primary")], on_done=named))

    def rename(self):
        name = self.info["name"]
        self.app.open_dialog(Dialog(f"Rename {name}", kind="input", text=name,
                                    buttons=[("Cancel", None, "normal"), ("Rename", True, "primary")],
                                    on_done=lambda v, t: v and t.strip() and self._after(
                                        self._guard(self.app.userdb.rename_deck, self.deck_id, t.strip()),
                                        f"Renamed to {t.strip()}")))

    def duplicate(self):
        name = self.info["name"]

        def done(v, t):
            if v and t.strip():
                new = self._guard(self.store.duplicate, self.deck_id, t.strip())
                if new is not FAILED:
                    self.open_deck(new)
                    self.app.toast(f"Created {t.strip()} - it only uses free copies from your Collection", GREEN)
        self.app.open_dialog(Dialog("Duplicate deck", ["Name for the copy. The copy has the same card list but",
                                                       "doesn't take the physical cards this deck uses."],
                                    kind="input", text=f"{name} (copy)",
                                    buttons=[("Cancel", None, "normal"), ("Duplicate", True, "primary")],
                                    on_done=done))

    def delete(self):
        info = self.info
        deck = self.deck_id

        def done(v, _t):
            if v and self._guard(self.app.userdb.delete_deck, deck) is not FAILED:
                if (self.app.pending_deck_scan() or {}).get("deck_id") == deck:
                    self.app.discard_deck_scan()
                self.view, self.deck_id, self.selected = "list", None, None
                self.app.toast(f"Deleted {info['name']} - your Collection is unchanged", GREEN)
            self.refresh()
        self.app.open_dialog(Dialog(
            f"Delete {info['name']}?",
            [f"The deck and its list of {info['count']} cards are deleted. Cards in your Collection stay there - "
             "copies this deck was using simply become free again."],
            buttons=[("Cancel", None, "normal"), ("Delete deck", True, "danger")], on_done=done))

    def export(self):
        txt, csv_path = self.store.export(self.deck_id, self.app.export_dir())
        self.app.toast("Exported the deck (decklist + CSV) to exports\\", GREEN)
        open_folder(os.path.dirname(txt))

    def import_deck(self, into=None):
        def source(v, _t):
            if v is None:
                return
            text, name = None, None
            if v == "file":
                path = self.app.ask_open_file("Import a decklist", [("Decklists", "*.txt *.csv *.dek"),
                                                                    ("All files", "*.*")])
                if not path:
                    return
                try:
                    with open(path, encoding="utf-8-sig", errors="replace") as f:
                        text = f.read()
                except OSError as e:
                    return self.app.toast(f"Couldn't read the file: {e}", RED)
                name = os.path.splitext(os.path.basename(path))[0]
            else:
                text = self.app.clipboard_text()
                name = "Imported deck"
            if not text or not text.strip():
                return self.app.toast("That was empty - copy a decklist first", RED)
            parsed = self.store.parse_decklist(text)
            if not parsed:
                return self.app.toast("No cards found in that list", RED)
            self.app.open_dialog(DeckImportPreview(self, parsed, into, name))
        self.app.open_dialog(Dialog("Import a decklist", ["Plain lists ('1 Sol Ring'), Moxfield / Archidekt / ManaBox "
                                                          "exports or a ManaBox CSV."], kind="choice",
                                    options=[("file", "From a file...", "normal"),
                                             ("clip", "From the clipboard (copy the list first)", "normal")],
                                    buttons=[("Cancel", None, "normal")], on_done=source))


class CardPicker(Dialog):
    """Find a card by name, then pick the printing. on_pick(card_row, finish)."""

    def __init__(self, screen, title, on_pick, name=None, legendary_first=False):
        super().__init__(title)
        self.screen, self.on_pick, self.legendary_first = screen, on_pick, legendary_first
        self.text, self.matches, self.sel = "", [], 0
        self.step, self.prints, self.pscroll = "name", [], 0
        self.finish = "nonfoil"
        if name:
            self.choose_name(name)

    def search(self):
        names = self.screen.app.db.search(self.text, 12) if len(self.text.strip()) >= 2 else []
        if self.legendary_first and names:
            store = self.screen.store

            def legendary(n):
                c = store.default_printing(n)
                return 0 if c and "Legendary" in (c.get("type_line") or "") else 1
            names.sort(key=legendary)
        self.matches, self.sel = names[:8], 0

    def choose_name(self, name):
        self.prints = self.screen.store.printings(name)
        self.step, self.pscroll = "printing", 0
        if len(self.prints) == 1:
            self.pick(self.prints[0])

    def pick(self, card):
        finishes = (card.get("finishes") or "nonfoil").split(",")
        finish = self.finish if self.finish in finishes else finishes[0]
        self.done = True
        self.on_pick(card, finish)

    def draw(self, canvas_bgr):
        import numpy as np
        from PIL import Image, ImageDraw
        H, W = canvas_bgr.shape[:2]
        p = Painter(W, H)
        p.img = Image.fromarray(cv2.cvtColor((canvas_bgr * 0.35).astype(np.uint8), cv2.COLOR_BGR2RGB))
        p.d = ImageDraw.Draw(p.img)
        bw, bh = 620, 470
        x0, y0 = (W - bw) // 2, (H - bh) // 2
        x1, y1 = x0 + bw, y0 + bh
        p.d.rounded_rectangle((x0, y0, x1, y1), radius=14, fill=CARD_BG, outline=BUTTON_HI)
        p.text((x0 + 24, y0 + 22), self.title, font="name", fill=YELLOW, width=bw - 48)
        y = y0 + 58
        if self.step == "name":
            p.input_box((x0 + 24, y, x1 - 24, y + 40), self.text, "Type the card name...", True, None)
            y += 52
            if not self.matches and len(self.text.strip()) >= 2:
                p.text((x0 + 24, y), "No card by that name", font="body", fill=MUTED)
            for i, n in enumerate(self.matches):
                p.button((x0 + 24, y, x1 - 24, y + 32), n, ("name", n), style="selected" if i == self.sel else "normal")
                y += 36
        else:
            name = self.prints[0]["name"] if self.prints else ""
            p.text((x0 + 24, y), f"{name}: {len(self.prints)} printing{'s' if len(self.prints) != 1 else ''} - "
                                 "pick yours (the first is a sensible default)", font="small", fill=MUTED, width=bw - 48)
            y += 24
            fx = x0 + 24
            for f in FINISHES:
                w = p.f["label"].getlength(finish_label(f)) + 20
                p.button((fx, y, fx + w, y + 26), finish_label(f), ("finish", f),
                         style="selected" if f == self.finish else "ghost", font="label")
                fx += w + 4
            y += 34
            vis = 8
            self.pscroll = max(0, min(self.pscroll, max(0, len(self.prints) - vis)))
            for c in self.prints[self.pscroll:self.pscroll + vis]:
                p.d.rounded_rectangle((x0 + 24, y, x1 - 24, y + 32), radius=8, fill=BUTTON)
                p.paste(self.screen.images.small(c["id"], c.get("image_small"), 20, 28), x0 + 30, y + 2, 20, 28, radius=2)
                p.text((x0 + 60, y + 16), f"{c.get('set_name') or ''}", font="row", anchor="lm", width=300)
                p.text((x0 + 370, y + 16), f"{c['set_code'].upper()} #{num(c['collector_number'])}", font="small",
                       fill=MUTED, anchor="lm")
                p.text((x0 + 470, y + 16), (c.get("released_at") or "")[:4], font="small", fill=MUTED, anchor="lm")
                p.text((x1 - 34, y + 16), self.screen.money(float(c.get("usd") or 0)) if c.get("usd") else "-",
                       font="small", fill=MUTED, anchor="rm")
                p.hit((x0 + 24, y, x1 - 24, y + 32), ("print", c["id"]))
                y += 36
            if len(self.prints) > vis:
                p.text((x0 + 24, y1 - 70), f"{self.pscroll + 1}-{min(len(self.prints), self.pscroll + vis)} of "
                                           f"{len(self.prints)} (mouse wheel to scroll)", font="small", fill=MUTED)
        by = y1 - 50
        if self.step == "printing":
            p.button((x0 + 24, by, x0 + 124, by + 34), "< Back", ("back", None))
        p.button((x1 - 124, by, x1 - 24, by + 34), "Cancel", ("btn", None))
        self.hits = p.hits
        return p.to_bgr()

    def click(self, x, y):
        for x0, y0, x1, y1, a in self.hits:
            if x0 <= x <= x1 and y0 <= y <= y1 and a:
                kind, v = a
                if kind == "name":
                    self.choose_name(v)
                elif kind == "print":
                    self.pick(next(c for c in self.prints if c["id"] == v))
                elif kind == "finish":
                    self.finish = v
                elif kind == "back":
                    self.step = "name"
                elif kind == "btn":
                    self.done = True
                return

    def wheel(self, delta):
        self.pscroll += -3 if delta > 0 else 3

    def key(self, key):
        if key == 27:
            self.done = True
        elif self.step == "name":
            if key == 13 and self.matches:
                self.choose_name(self.matches[self.sel])
            elif key == 0x260000:
                self.sel = max(0, self.sel - 1)
            elif key == 0x280000:
                self.sel = min(len(self.matches) - 1, self.sel + 1)
            elif key == 8:
                self.text = self.text[:-1]
                self.search()
            elif 32 <= key < 127:
                self.text += chr(key)
                self.search()
        elif key == 13 and self.prints:
            self.pick(self.prints[self.pscroll])
        elif key == 8:
            self.step = "name"


class DeckImportPreview(Dialog):
    """Preview of a decklist import - nothing is written until Import is pressed."""

    def __init__(self, screen, parsed, into, name):
        super().__init__("Import preview")
        self.screen, self.parsed, self.into, self.name, self.scroll = screen, parsed, into, name, 0

    def draw(self, canvas_bgr):
        import numpy as np
        from PIL import Image, ImageDraw
        H, W = canvas_bgr.shape[:2]
        p = Painter(W, H)
        p.img = Image.fromarray(cv2.cvtColor((canvas_bgr * 0.35).astype(np.uint8), cv2.COLOR_BGR2RGB))
        p.d = ImageDraw.Draw(p.img)
        x0, y0, x1, y1 = 120, 30, W - 120, H - 30
        p.d.rounded_rectangle((x0, y0, x1, y1), radius=14, fill=CARD_BG, outline=BUTTON_HI)
        target = (self.screen.app.userdb.deck(self.into) or {}).get("name") if self.into else f"a new deck '{self.name}'"
        p.text((x0 + 24, y0 + 20), f"Import into {target}", font="name", fill=YELLOW, width=x1 - x0 - 48)
        ok = [r for r in self.parsed if r["card"]]
        bad = [r for r in self.parsed if not r["card"]]
        guessed = [r for r in ok if r["status"] == "name"]
        y = y0 + 54
        p.text((x0 + 24, y), f"{sum(r['quantity'] for r in ok)} cards found", fill=GREEN)
        p.text((x0 + 220, y), f"{len(guessed)} with the printing guessed (change later)", fill=YELLOW if guessed else MUTED)
        p.text((x1 - 24, y), f"{len(bad)} not recognised", fill=RED if bad else MUTED, anchor="ra")
        y += 30
        rows = sorted(self.parsed, key=lambda r: (r["card"] is not None, r["role"] not in ("commander", "partner")))
        vis = (y1 - 80 - y) // 26
        self.scroll = max(0, min(self.scroll, max(0, len(rows) - vis)))
        for r in rows[self.scroll:self.scroll + vis]:
            card = r["card"] or {}
            status = {"id": ("Exact", GREEN), "set": ("Exact", GREEN), "name": ("Name", YELLOW),
                      "none": ("Not found", RED)}[r["status"]]
            p.text((x0 + 24, y), status[0], font="small", fill=status[1])
            p.text((x0 + 104, y), f"{r['quantity']}", font="small")
            p.text((x0 + 134, y), card.get("name") or r["name"], font="small", width=330,
                   fill=YELLOW if r["role"] in ("commander", "partner") else TEXT)
            p.text((x0 + 474, y), r["role"] if r["role"] != "main" else "", font="small", fill=MUTED)
            if card:
                p.text((x0 + 574, y), f"{card['set_code'].upper()} #{num(card['collector_number'])} "
                                      f"{finish_label(r['finish'])}", font="small", fill=MUTED, width=x1 - x0 - 600)
            y += 26
        if len(rows) > vis:
            p.text((x0 + 24, y1 - 66), f"{self.scroll + 1}-{min(len(rows), self.scroll + vis)} of {len(rows)} lines "
                                       "(mouse wheel to scroll)", font="small", fill=MUTED)
        n = sum(r["quantity"] for r in ok)
        p.button((x1 - 24 - 200, y1 - 50, x1 - 24, y1 - 16), f"Import {n} cards", ("btn", True),
                 style="primary" if n else "disabled")
        p.button((x1 - 24 - 310, y1 - 50, x1 - 24 - 210, y1 - 16), "Cancel", ("btn", None))
        self.hits = p.hits
        return p.to_bgr()

    def click(self, x, y):
        for x0, y0, x1, y1, a in self.hits:
            if x0 <= x <= x1 and y0 <= y <= y1 and a and a[0] == "btn":
                self.finish(a[1])
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
        if not value:
            return
        sc = self.screen
        deck = self.into
        if deck is None:
            deck = sc._guard(sc.store.create_deck, self.name or "Imported deck")
            if deck is FAILED:
                return
        n = sc._guard(sc.store.commit_import, deck, self.parsed)
        if n is not FAILED:
            sc.app.toast(f"Imported {n} cards", GREEN)
        sc.open_deck(deck)


def _wrap(p, text, font, width):
    f = p.f[font]
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if f.getlength(trial) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]
