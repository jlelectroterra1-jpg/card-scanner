"""The scanner's side panel ("dashboard"), drawn with Pillow for proper fonts.

Panel.render(view) returns the panel as a BGR image plus clickable areas
[(x0, y0, x1, y1, action)]. It only redraws when what it shows has changed."""
import os

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")

BG = (22, 23, 26)
CARD_BG = (32, 34, 39)
BUTTON = (44, 47, 54)
BUTTON_HI = (64, 68, 78)
TEXT = (238, 238, 242)
MUTED = (150, 153, 162)
GREEN = (88, 200, 120)
YELLOW = (255, 196, 70)
RED = (240, 96, 96)
GOLD = (255, 186, 60)
RARITY = {"common": (160, 164, 172), "uncommon": (170, 200, 222), "rare": (228, 196, 100),
          "mythic": (240, 130, 60), "special": (180, 140, 240), "bonus": (180, 140, 240)}
HIGH_VALUE = 5.0


def _font(names, size):
    for n in names:
        try:
            return ImageFont.truetype(os.path.join(FONT_DIR, n), size)
        except OSError:
            continue
    return ImageFont.load_default(size)


class Panel:
    def __init__(self, width, height):
        self.W, self.H = width, height
        reg, semi, bold = ["segoeui.ttf", "arial.ttf"], ["seguisb.ttf", "arialbd.ttf"], ["segoeuib.ttf", "arialbd.ttf"]
        self.f = {
            "big": _font(bold, 27), "price": _font(bold, 30), "name": _font(semi, 19), "body": _font(reg, 15),
            "small": _font(reg, 13), "label": _font(semi, 12), "button": _font(semi, 14), "row": _font(reg, 14),
        }
        self._key = None
        self._cache = None

    # ---- helpers ------------------------------------------------------------

    def _fit(self, text, font, width):
        """Shorten text with an ellipsis so it fits in `width` pixels."""
        if font.getlength(text) <= width:
            return text
        while text and font.getlength(text + "…") > width:
            text = text[:-1]
        return text + "…"

    def _wrap2(self, text, font, width):
        words, lines, cur = text.split(), [], ""
        for w in words:
            trial = (cur + " " + w).strip()
            if font.getlength(trial) <= width or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
        if len(lines) > 2:
            lines = [lines[0], self._fit(" ".join(lines[1:]), font, width)]
        return lines

    def _button(self, d, box, label, action, buttons, primary=False, danger=False):
        x0, y0, x1, y1 = box
        d.rounded_rectangle(box, radius=8, fill=GREEN if primary else BUTTON)
        colour = (10, 30, 16) if primary else (RED if danger else TEXT)
        tw = self.f["button"].getlength(label)
        d.text(((x0 + x1 - tw) / 2, (y0 + y1) / 2), label, font=self.f["button"], fill=colour, anchor="lm")
        buttons.append((x0, y0, x1, y1, action))

    def _chip(self, d, x, y, text, colour):
        w = self.f["label"].getlength(text) + 14
        d.rounded_rectangle((x, y, x + w, y + 20), radius=10, fill=BUTTON)
        d.text((x + 7, y + 10), text, font=self.f["label"], fill=colour, anchor="lm")
        return x + w + 6

    @staticmethod
    def _paste(img, bgr, x, y, w, h, radius=6):
        if bgr is None:
            ImageDraw.Draw(img).rounded_rectangle((x, y, x + w, y + h), radius=radius, fill=BUTTON)
            return
        tile = Image.fromarray(cv2.cvtColor(cv2.resize(bgr, (w, h), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB))
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
        img.paste(tile, (x, y), mask)

    # ---- sections -----------------------------------------------------------

    def _header(self, d, v):
        p = 16
        d.text((p, 14), f"{v['count']} card{'s' if v['count'] != 1 else ''}", font=self.f["big"], fill=TEXT)
        total = f"${v['total']:,.2f}"
        d.text((self.W - p, 14), total, font=self.f["big"], fill=GOLD if v["total"] >= 100 else TEXT, anchor="ra")
        x = p
        sub = f"{v['rate']:.0f} cards/min" if v["rate"] else "ready to scan"
        d.text((x, 56), sub, font=self.f["small"], fill=MUTED, anchor="lm")
        x += self.f["small"].getlength(sub) + 10
        if v.get("lock"):
            x = self._chip(d, x, 46, f"SET {v['lock'].upper()}", YELLOW)
        if v.get("foil_default"):
            x = self._chip(d, x, 46, "FOIL", YELLOW)
        cam = f"camera {v['camera']}"
        d.text((self.W - p, 56), cam, font=self.f["small"], fill=MUTED, anchor="rm")
        d.line((p, 76, self.W - p, 76), fill=BUTTON, width=1)

    def _last_card(self, img, d, v, buttons, top):
        p = 16
        last = v["last"]
        if last is None:
            d.rounded_rectangle((p, top, self.W - p, top + 250), radius=12, fill=CARD_BG)
            d.text((self.W / 2, top + 95), "Put a card in the box", font=self.f["name"], fill=TEXT, anchor="mm")
            for i, line in enumerate(["It scans by itself when the card holds still.",
                                      "Take it away (or drop the next one on top)",
                                      "and keep going."]):
                d.text((self.W / 2, top + 130 + i * 20), line, font=self.f["small"], fill=MUTED, anchor="mm")
            return top + 262
        d.rounded_rectangle((p, top, self.W - p, top + 250), radius=12, fill=CARD_BG)
        iw, ih = 148, 206
        self._paste(img, last["image"], p + 10, top + 10, iw, ih)
        x = p + 10 + iw + 14
        width = self.W - p - 10 - x
        y = top + 12
        for line in self._wrap2(last["name"], self.f["name"], width):
            d.text((x, y), line, font=self.f["name"], fill=TEXT)
            y += 25
        d.text((x, y + 2), self._fit(last["set_name"], self.f["small"], width), font=self.f["small"], fill=MUTED)
        y += 22
        rc = RARITY.get(last["rarity"], MUTED)
        d.ellipse((x, y + 5, x + 9, y + 14), fill=rc)
        d.text((x + 15, y), f"{last['set_code'].upper()} #{last['number']} · {last['rarity']}", font=self.f["small"], fill=MUTED)
        y += 26
        foil = last["finish"] != "nonfoil"
        self._chip(d, x, y, last["finish"].upper() if foil else "NON-FOIL", YELLOW if foil else MUTED)
        y += 32
        price = last["price"]
        d.text((x, y), f"${price:,.2f}", font=self.f["price"], fill=GOLD if price >= HIGH_VALUE else TEXT)
        y += 42
        if last["printings"] > 1:
            d.text((x, y), f"printing {last['printing']} of {last['printings']}", font=self.f["small"], fill=MUTED)
        if last.get("how"):
            d.text((x, y + 18), f"recognised by {last['how']}", font=self.f["small"], fill=MUTED)
        # Buttons along the bottom of the card block, under the picture.
        labels = [("‹ Print", "prev", False), ("Print ›", "next", False), ("Foil", "foil", False), ("Remove", "remove", True)]
        bx, by = p + 10, top + 224
        bw = (self.W - 2 * p - 20 - 3 * 6) / 4
        for i, (label, action, danger) in enumerate(labels):
            x0 = bx + i * (bw + 6)
            self._button(d, (int(x0), by, int(x0 + bw), by + 22), label, action, buttons, danger=danger)
        return top + 262

    def _recent(self, img, d, v, top, bottom):
        p = 16
        d.text((p, top), "RECENT", font=self.f["label"], fill=MUTED)
        y = top + 20
        row_h = 30
        for r in v["recent"]:
            if y + row_h > bottom:
                break
            self._paste(img, r["image"], p, y, 20, 28, radius=3)
            price = r["price"]
            ptxt = f"${price:,.2f}"
            pw = self.f["row"].getlength(ptxt)
            name = r["name"] + (" ★" if r["finish"] != "nonfoil" else "")
            d.text((p + 30, y + 14), self._fit(name, self.f["row"], self.W - 2 * p - 30 - pw - 60), font=self.f["row"],
                   fill=TEXT, anchor="lm")
            d.text((self.W - p - pw - 10, y + 14), r["set_code"].upper(), font=self.f["small"], fill=MUTED, anchor="rm")
            d.text((self.W - p, y + 14), ptxt, font=self.f["row"], fill=GOLD if price >= HIGH_VALUE else MUTED, anchor="rm")
            y += row_h
        if not v["recent"]:
            d.text((p, y + 4), "Nothing yet", font=self.f["small"], fill=MUTED)

    def _footer_buttons(self, d, buttons):
        p = 16
        y = self.H - 46
        bw = (self.W - 2 * p - 3 * 6) / 4
        for i, (label, action, primary) in enumerate([("Export", "export", True), ("Lock set", "lock", False),
                                                     ("New list", "new", False), ("Camera", "camera", False)]):
            x0 = p + i * (bw + 6)
            self._button(d, (int(x0), y, int(x0 + bw), y + 32), label, action, buttons, primary=primary)

    def _review(self, img, d, v, buttons, top):
        p = 16
        r = v["review"]
        d.text((p, top), "Which card is it?", font=self.f["name"], fill=YELLOW)
        y = top + 34
        self._paste(img, r["image"], p, y, 110, 153)
        x = p + 122
        for i, c in enumerate(r["choices"][:5]):
            box = (x, y + i * 40, self.W - p, y + i * 40 + 34)
            d.rounded_rectangle(box, radius=8, fill=BUTTON)
            self._paste(img, c["image"], x + 6, box[1] + 3, 20, 28, radius=3)
            d.text((x + 34, box[1] + 17), f"{i + 1}  " + self._fit(c["name"], self.f["row"], self.W - p - x - 90),
                   font=self.f["row"], fill=TEXT, anchor="lm")
            d.text((self.W - p - 10, box[1] + 17), f"{c['score']:.0f}%", font=self.f["small"], fill=MUTED, anchor="rm")
            buttons.append((*box, f"pick:{i}"))
        y += 5 * 40 + 6
        half = (self.W - 2 * p - 6) / 2
        self._button(d, (p, y, int(p + half), y + 32), "Type the name (S)", "search", buttons)
        self._button(d, (int(p + half + 6), y, self.W - p, y + 32), "Skip (X)", "skip", buttons)

    def _typing(self, img, d, v, buttons, top):
        p = 16
        t = v["typing"]
        title = "Type the card name" if t["kind"] == "search" else "Lock to a set (code, empty = any)"
        d.text((p, top), title, font=self.f["name"], fill=YELLOW)
        y = top + 34
        d.rounded_rectangle((p, y, self.W - p, y + 38), radius=8, fill=(14, 15, 18), outline=BUTTON_HI)
        d.text((p + 12, y + 19), self._fit(t["text"], self.f["body"], self.W - 2 * p - 30) + "|", font=self.f["body"],
               fill=TEXT, anchor="lm")
        y += 50
        for i, name in enumerate(t["matches"][:5]):
            sel = i == t["sel"]
            box = (p, y, self.W - p, y + 34)
            d.rounded_rectangle(box, radius=8, fill=BUTTON_HI if sel else BUTTON)
            d.text((p + 12, y + 17), self._fit(name, self.f["row"], self.W - 2 * p - 24), font=self.f["row"],
                   fill=GREEN if sel else TEXT, anchor="lm")
            buttons.append((*box, f"sugg:{i}"))
            y += 40
        d.text((p, y + 6), "ENTER = ok    ESC = cancel" + ("    Up / Down = choose" if t["kind"] == "search" else ""),
               font=self.f["small"], fill=MUTED)

    # ---- main ---------------------------------------------------------------

    def render(self, view, key):
        """view: dict built by the scanner; key: anything that changes when view does."""
        if key == self._key and self._cache is not None:
            return self._cache
        img = Image.new("RGB", (self.W, self.H), BG)
        d = ImageDraw.Draw(img)
        buttons = []
        self._header(d, view)
        top = 90
        if view.get("typing"):
            self._typing(img, d, view, buttons, top)
        elif view.get("review"):
            self._review(img, d, view, buttons, top)
        else:
            y = self._last_card(img, d, view, buttons, top)
            self._recent(img, d, view, y + 4, self.H - 56)
        self._footer_buttons(d, buttons)
        out = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)
        self._key, self._cache = key, (out, buttons)
        return self._cache
