"""Large card viewer: click a card picture anywhere -> the EXACT printing, as big as fits.

Uses Scryfall's 'large' image (672 x 936) of that printing, downloaded once in the
background and cached in data/img_xl (front and, for double-faced cards, back). While it
loads, the best picture already on disk is shown. ESC / the X / a click outside closes it;
mouse wheel or + / - zoom; Front / Back (or Left / Right, or clicking the card) flips
double-faced cards. Nothing about the selected card in the screen behind it changes."""
import os
import threading

import cv2
import numpy as np
import requests

from ui import BUTTON, BUTTON_HI, MUTED, TEXT, YELLOW, Dialog, Painter

HERE = os.path.dirname(os.path.abspath(__file__))
XL_DIR = os.path.join(HERE, "data", "img_xl")
NORMAL_DIR = os.path.join(HERE, "data", "img_large")
SMALL_DIR = os.path.join(HERE, "data", "img")
HEADERS = {"User-Agent": "HomeCardScanner/0.6"}
CARD_RATIO = 88 / 63
LEFT, RIGHT = 0x250000, 0x270000
_lock = threading.Lock()
_in_flight = set()


def large_url(card, face="front"):
    """Scryfall URL of the large image for this exact printing (and face)."""
    url = card.get("image_normal") or card.get("image_small") or ""
    if not url:
        return None
    url = url.replace("/normal/", "/large/").replace("/small/", "/large/")
    if face == "back":
        url = url.replace("/front/", "/back/")
    return url


def xl_path(card_id, face="front"):
    return os.path.join(XL_DIR, f"{card_id}_{face}.jpg")


def fetch(url, path, on_done, session=None, timeout=20):
    """Download in the background; on_done(ok). Never blocks the caller."""
    with _lock:
        if path in _in_flight:
            return
        _in_flight.add(path)

    def run():
        ok = False
        try:
            r = (session or requests).get(url, headers=HEADERS, timeout=timeout)
            if r.status_code == 200 and r.content[:2] == b"\xff\xd8":  # a JPEG
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path + ".part", "wb") as f:
                    f.write(r.content)
                os.replace(path + ".part", path)
                ok = True
        except (requests.RequestException, OSError):
            ok = False
        finally:
            with _lock:
                _in_flight.discard(path)
            on_done(ok)
    threading.Thread(target=run, daemon=True).start()


class CardViewer(Dialog):
    """card: dict(id = exact Scryfall printing id, name, image_normal, image_small,
    set_code, collector_number) - e.g. a collection / deck row or a cards.db row."""
    ZOOMS = (1.0, 1.25, 1.5, 1.8, 2.2)

    def __init__(self, card, fetcher=None):
        super().__init__(card.get("name") or "Card")
        self.card = card
        self.fetcher = fetcher or (lambda url, path, done: fetch(url, path, done))
        self.faces = ["front", "back"] if " // " in (card.get("name") or "") else ["front"]
        self.face = 0
        self.zoom = 0
        self.state = {}       # face -> "loading" | "ok" | "missing"
        self.images = {}      # face -> full-size BGR image
        self._sized = {}      # (face, w, h) -> resized image
        self.version = 0
        self.hits = []
        self._load("front")

    # ---- images ------------------------------------------------------------------------

    def _load(self, face):
        cid = self.card.get("id")
        path = xl_path(cid, face)
        if os.path.exists(path):
            img = cv2.imread(path)
            if img is not None:
                self.images[face], self.state[face] = img, "ok"
                return
        url = large_url(self.card, face)
        if not cid or not url:
            self.state[face] = "missing"
            return
        self.state[face] = "loading"

        def done(ok):
            img = cv2.imread(path) if ok else None
            if img is not None:
                self.images[face], self.state[face] = img, "ok"
            else:
                self.state[face] = "missing"
            self._sized.clear()
            self.version += 1
        self.fetcher(url, path, done)

    def _fallback(self, face):
        """Best picture already on disk while the large one loads (front face only)."""
        if face != "front":
            return None
        cid = self.card.get("id") or ""
        for d in (NORMAL_DIR, SMALL_DIR):
            p = os.path.join(d, cid + ".jpg")
            if os.path.exists(p):
                img = cv2.imread(p)
                if img is not None:
                    return img
        return None

    def current(self):
        face = self.faces[self.face]
        if face in self.images:
            return self.images[face], True
        return self._fallback(face), False

    # ---- drawing -------------------------------------------------------------------------

    def card_box(self, W, H):
        z = self.ZOOMS[self.zoom]
        ch = int((H - 70) * z)
        cw = int(ch / CARD_RATIO)
        return (W - cw) // 2, (H - ch) // 2 + 6, cw, ch

    def draw(self, canvas_bgr):
        from PIL import Image, ImageDraw
        H, W = canvas_bgr.shape[:2]
        dim = (canvas_bgr.astype(np.float32) * 0.18).astype(np.uint8)
        x, y, cw, ch = self.card_box(W, H)
        img, full = self.current()
        if img is not None:
            key = (self.faces[self.face], cw, ch, full)
            sized = self._sized.get(key)
            if sized is None:
                big = cv2.resize(img, (cw, ch), interpolation=cv2.INTER_AREA if img.shape[0] > ch else cv2.INTER_CUBIC)
                mask = np.zeros((ch, cw), np.uint8)  # rounded card corners (Scryfall's JPGs have white ones)
                r = max(4, int(cw * 0.045))
                cv2.rectangle(mask, (r, 0), (cw - r, ch), 255, -1)
                cv2.rectangle(mask, (0, r), (cw, ch - r), 255, -1)
                for cx, cy in ((r, r), (cw - r - 1, r), (r, ch - r - 1), (cw - r - 1, ch - r - 1)):
                    cv2.circle(mask, (cx, cy), r, 255, -1)
                sized = (big, mask > 0)
                self._sized[key] = sized
            big, mask = sized
            # paste, clipped to the window when zoomed in
            sx0, sy0 = max(0, -x), max(0, -y)
            dx0, dy0 = max(0, x), max(0, y)
            w = min(cw - sx0, W - dx0)
            h = min(ch - sy0, H - dy0)
            if w > 0 and h > 0:
                region = dim[dy0:dy0 + h, dx0:dx0 + w]
                m = mask[sy0:sy0 + h, sx0:sx0 + w]
                region[m] = big[sy0:sy0 + h, sx0:sx0 + w][m]
        p = Painter(W, H)
        p.img = Image.fromarray(cv2.cvtColor(dim, cv2.COLOR_BGR2RGB))
        p.d = ImageDraw.Draw(p.img)
        if img is None:
            p.d.rounded_rectangle((x, y, x + cw, y + ch), radius=14, fill=BUTTON)
        state = self.state.get(self.faces[self.face])
        msg = None
        if state == "loading":
            msg = "Loading the full-size picture..."
        elif state == "missing" and self.faces[self.face] == "back":
            msg = "No separate back image for this card (split / adventure cards show both halves on the front)"
        elif state == "missing":
            msg = "Couldn't download the full-size picture - showing the one on disk"
        title = self.card.get("name") or ""
        sub = " ".join(x for x in ((self.card.get("set_code") or "").upper(),
                                   f"#{self.card['collector_number']}" if self.card.get("collector_number") else "") if x)
        p.text((16, 14), title, font="name", fill=YELLOW, width=W // 2 - 140)
        if sub:
            p.text((16, 38), sub, font="small", fill=MUTED)
        if msg:
            p.text((W // 2, H - 14), msg, font="small", fill=YELLOW, anchor="mm")
        # card area click = flip (double-faced); clicks elsewhere close
        p.hit((max(0, x), max(0, y), min(W, x + cw), min(H, y + ch)), ("card", None))
        bx = W - 16
        p.button((bx - 40, 10, bx, 44), "X", ("close", None), font="name")
        bx -= 48
        p.button((bx - 36, 10, bx, 44), "+", ("zoom", 1), font="name",
                 style="normal" if self.zoom < len(self.ZOOMS) - 1 else "disabled")
        bx -= 40
        p.button((bx - 36, 10, bx, 44), "-", ("zoom", -1), font="name", style="normal" if self.zoom else "disabled")
        bx -= 44
        if len(self.faces) > 1:
            for i, face in reversed(list(enumerate(self.faces))):
                label = face.title()
                p.button((bx - 72, 10, bx, 44), label, ("face", i), style="selected" if i == self.face else "normal",
                         font="label")
                bx -= 78
        p.text((16, H - 14), "Esc / X closes  -  wheel or + / - zooms" + ("  -  Left / Right flips"
                                                                          if len(self.faces) > 1 else ""),
               font="small", fill=MUTED, anchor="lm")
        self.hits = p.hits
        return p.to_bgr()

    # ---- input ---------------------------------------------------------------------------

    def set_face(self, i):
        i = max(0, min(len(self.faces) - 1, i))
        if i != self.face:
            self.face = i
            if self.faces[i] not in self.state:
                self._load(self.faces[i])

    def set_zoom(self, step):
        self.zoom = max(0, min(len(self.ZOOMS) - 1, self.zoom + step))

    def click(self, x, y):
        for x0, y0, x1, y1, a in reversed(self.hits):
            if x0 <= x <= x1 and y0 <= y <= y1 and a:
                kind, v = a
                if kind == "close":
                    self.done = True
                elif kind == "zoom":
                    self.set_zoom(v)
                elif kind == "face":
                    self.set_face(v)
                elif kind == "card" and len(self.faces) > 1:
                    self.set_face(1 - self.face)
                return
        self.done = True  # clicked outside the card

    def wheel(self, delta):
        self.set_zoom(1 if delta > 0 else -1)

    def key(self, key):
        if key in (27, 13, ord("q")):
            self.done = True
        elif key in (ord("+"), ord("=")):
            self.set_zoom(1)
        elif key == ord("-"):
            self.set_zoom(-1)
        elif key in (LEFT, RIGHT, ord("f")) and len(self.faces) > 1:
            self.set_face(1 - self.face if key == ord("f") else (0 if key == LEFT else 1))


def card_for(store_or_db, scryfall_id):
    """cards.db row for an exact printing (DeckStore.card / CardDB.by_id / CollectionStore.card)."""
    for attr in ("card", "by_id"):
        fn = getattr(store_or_db, attr, None)
        if fn:
            c = fn(scryfall_id)
            if c:
                return c
    return None


def open_viewer(app, card):
    """Open the viewer for a card dict (needs at least id + image URL); returns the dialog."""
    if not card or not card.get("id"):
        return None
    dlg = CardViewer(card)
    app.open_dialog(dlg)
    return dlg
