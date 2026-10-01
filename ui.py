"""Small drawing toolkit for the app's screens (Pillow, same look as the side panel):
fonts and colours, buttons, chips, text boxes, dropdown lists and pop-up dialogs.
Everything is drawn into images; clickable areas are returned as
[(x0, y0, x1, y1, action)] lists and handled by the caller."""
import os

import cv2
import numpy as np
from PIL import Image, ImageDraw

from panel import (BG, BUTTON, BUTTON_HI, CARD_BG, GOLD, GREEN, MUTED, RARITY, RED, TEXT, YELLOW,  # noqa: F401
                   _font)

NAV_BG = (16, 17, 19)
INPUT_BG = (14, 15, 18)
ROW_ALT = (27, 29, 33)
ROW_SEL = (44, 58, 52)


class Fonts:
    _cache = None

    @classmethod
    def get(cls):
        if cls._cache is None:
            reg, semi, bold = ["segoeui.ttf", "arial.ttf"], ["seguisb.ttf", "arialbd.ttf"], ["segoeuib.ttf", "arialbd.ttf"]
            cls._cache = {
                "title": _font(bold, 22), "big": _font(bold, 27), "name": _font(semi, 17), "body": _font(reg, 15),
                "small": _font(reg, 13), "label": _font(semi, 12), "button": _font(semi, 14), "row": _font(reg, 14),
                "rowb": _font(semi, 14), "tab": _font(semi, 15),
            }
        return cls._cache


class Painter:
    """A PIL image plus helpers; collects clickable areas in self.hits."""

    def __init__(self, w, h, bg=BG):
        self.w, self.h = w, h
        self.img = Image.new("RGB", (w, h), bg)
        self.d = ImageDraw.Draw(self.img)
        self.f = Fonts.get()
        self.hits = []

    def to_bgr(self):
        return cv2.cvtColor(np.asarray(self.img), cv2.COLOR_RGB2BGR)

    def fit(self, text, font, width):
        text = str(text)
        if font.getlength(text) <= width:
            return text
        while text and font.getlength(text + "…") > width:
            text = text[:-1]
        return text + "…"

    def text(self, xy, text, font="body", fill=TEXT, anchor="la", width=None):
        f = self.f[font]
        if width is not None:
            text = self.fit(text, f, width)
        self.d.text(xy, str(text), font=f, fill=fill, anchor=anchor)

    def hit(self, box, action):
        self.hits.append((int(box[0]), int(box[1]), int(box[2]), int(box[3]), action))

    def button(self, box, label, action, style="normal", font="button", radius=8):
        x0, y0, x1, y1 = box
        fill = {"primary": GREEN, "selected": BUTTON_HI, "ghost": CARD_BG}.get(style, BUTTON)
        self.d.rounded_rectangle(box, radius=radius, fill=fill)
        colour = (10, 30, 16) if style == "primary" else (RED if style == "danger" else
                                                          (MUTED if style == "disabled" else TEXT))
        self.d.text(((x0 + x1) / 2, (y0 + y1) / 2), self.fit(label, self.f[font], x1 - x0 - 10), font=self.f[font],
                    fill=colour, anchor="mm")
        if style != "disabled" and action is not None:
            self.hit(box, action)

    def dropdown(self, box, label, action, active=False):
        """A button that opens a list: label + small down-arrow."""
        x0, y0, x1, y1 = box
        self.d.rounded_rectangle(box, radius=8, fill=BUTTON_HI if active else BUTTON)
        self.d.text((x0 + 10, (y0 + y1) / 2), self.fit(label, self.f["button"], x1 - x0 - 30), font=self.f["button"],
                    fill=YELLOW if active else TEXT, anchor="lm")
        cx, cy = x1 - 13, (y0 + y1) / 2
        self.d.polygon([(cx - 4, cy - 2), (cx + 4, cy - 2), (cx, cy + 3)], fill=MUTED)
        self.hit(box, action)

    def chip(self, x, y, text, colour, fill=BUTTON):
        w = self.f["label"].getlength(text) + 14
        self.d.rounded_rectangle((x, y, x + w, y + 20), radius=10, fill=fill)
        self.d.text((x + 7, y + 10), text, font=self.f["label"], fill=colour, anchor="lm")
        return x + w + 6

    def input_box(self, box, text, placeholder, focused, action):
        x0, y0, x1, y1 = box
        self.d.rounded_rectangle(box, radius=8, fill=INPUT_BG, outline=YELLOW if focused else BUTTON_HI)
        shown = text if text else placeholder
        colour = TEXT if text else MUTED
        t = self.fit(shown, self.f["body"], x1 - x0 - 26)
        self.d.text((x0 + 10, (y0 + y1) / 2), t + ("|" if focused and text else ""), font=self.f["body"],
                    fill=colour, anchor="lm")
        if focused and not text:
            self.d.text((x0 + 9, (y0 + y1) / 2), "|", font=self.f["body"], fill=TEXT, anchor="lm")
        self.hit(box, action)

    def paste(self, bgr, x, y, w, h, radius=4):
        if bgr is None:
            self.d.rounded_rectangle((x, y, x + w, y + h), radius=radius, fill=BUTTON)
            return
        tile = Image.fromarray(cv2.cvtColor(cv2.resize(bgr, (w, h), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB))
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
        self.img.paste(tile, (int(x), int(y)), mask)

    def rarity_dot(self, x, y, rarity):
        self.d.ellipse((x, y, x + 9, y + 9), fill=RARITY.get(rarity or "", MUTED))

    def popup_list(self, x, y, width, items, action_prefix, selected=None, max_rows=12):
        """A dropdown's open list: items = [(value, label)]. Draws over everything."""
        items = items[:max_rows]
        h = 32 * len(items) + 8
        y = min(y, self.h - h - 4)
        self.d.rounded_rectangle((x, y, x + width, y + h), radius=10, fill=(36, 38, 44), outline=BUTTON_HI)
        for i, (value, label) in enumerate(items):
            ry = y + 4 + i * 32
            if value == selected:
                self.d.rounded_rectangle((x + 4, ry, x + width - 4, ry + 30), radius=6, fill=BUTTON_HI)
            self.d.text((x + 14, ry + 15), self.fit(label, self.f["row"], width - 28), font=self.f["row"],
                        fill=YELLOW if value == selected else TEXT, anchor="lm")
            self.hit((x + 4, ry, x + width - 4, ry + 30), (action_prefix, value))
        return (x, y, x + width, y + h)


# ---------------------------------------------------------------- dialogs

class Dialog:
    """A pop-up over the whole window. kind: 'confirm' (buttons), 'input' (a text field
    + buttons) or 'choice' (a list of options). on_done(value, text) is called with the
    chosen button/option value (None = cancelled)."""

    def __init__(self, title, message="", buttons=None, kind="confirm", text="", placeholder="",
                 options=None, on_done=None, numeric=False):
        self.title, self.kind = title, kind
        self.message = message if isinstance(message, (list, tuple)) else [m for m in str(message).split("\n") if m]
        self.buttons = buttons or [("OK", True, "primary")]
        self.text, self.placeholder, self.numeric = text, placeholder, numeric
        self.options = options or []  # [(value, label, style)]
        self.on_done = on_done
        self.hits = []
        self.done = False

    def finish(self, value):
        self.done = True
        if self.on_done:
            self.on_done(value, self.text)

    def draw(self, canvas_bgr):
        """Draw the dialog over a BGR canvas (dimming everything else)."""
        H, W = canvas_bgr.shape[:2]
        dim = (canvas_bgr.astype(np.float32) * 0.35).astype(np.uint8)
        p = Painter(W, H)
        p.img = Image.fromarray(cv2.cvtColor(dim, cv2.COLOR_BGR2RGB))
        p.d = ImageDraw.Draw(p.img)
        bw = 520
        lines = []
        for m in self.message:
            lines += _wrap(m, p.f["body"], bw - 48)
        bh = 70 + 24 * len(lines) + (56 if self.kind == "input" else 0) + 40 * len(self.options) + 64
        x0, y0 = (W - bw) // 2, max(20, (H - bh) // 2)
        p.d.rounded_rectangle((x0, y0, x0 + bw, y0 + bh), radius=14, fill=CARD_BG, outline=BUTTON_HI)
        p.text((x0 + 24, y0 + 22), self.title, font="name", fill=YELLOW, width=bw - 48)
        y = y0 + 58
        for line in lines:
            p.text((x0 + 24, y), line, font="body")
            y += 24
        if self.kind == "input":
            y += 6
            p.input_box((x0 + 24, y, x0 + bw - 24, y + 40), self.text, self.placeholder, True, None)
            y += 50
        for value, label, style in self.options:
            p.button((x0 + 24, y, x0 + bw - 24, y + 34), label, ("opt", value), style=style or "normal")
            y += 40
        y = y0 + bh - 52
        bx = x0 + bw - 24
        for label, value, style in reversed(self.buttons):
            w = p.f["button"].getlength(label) + 36
            p.button((bx - w, y, bx, y + 36), label, ("btn", value), style=style)
            bx -= w + 10
        self.hits = p.hits
        return p.to_bgr()

    def click(self, x, y):
        for x0, y0, x1, y1, action in self.hits:
            if x0 <= x <= x1 and y0 <= y <= y1:
                if action and action[0] in ("btn", "opt"):
                    self.finish(action[1])
                return

    def key(self, key):
        if key == 27:
            self.finish(None)
        elif key == 13:
            default = next((v for _, v, s in self.buttons if s == "primary"), self.buttons[-1][1])
            self.finish(default)
        elif self.kind == "input":
            if key == 8:
                self.text = self.text[:-1]
            elif 32 <= key < 127:
                ch = chr(key)
                if not self.numeric or ch in "0123456789.,":
                    self.text += ch


def _wrap(text, font, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if font.getlength(trial) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def open_folder(path):
    if os.name == "nt":
        try:
            os.startfile(path)
        except OSError:
            pass
