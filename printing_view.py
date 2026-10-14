"""'Choose printing' for the last scanned card: its likely printings, most visually
similar first (from printing.py), with picture, set, number, finishes, price and how
closely each printing's picture matches the photo. Click one to use it."""
import cv2

from ui import BUTTON, BUTTON_HI, CARD_BG, GOLD, GREEN, MUTED, TEXT, YELLOW, Dialog, Painter

FINISH_LABEL = {"nonfoil": "Non-foil", "foil": "Foil", "etched": "Etched"}


def finish_price(card, finish):
    v = (card or {}).get({"nonfoil": "usd", "foil": "usd_foil", "etched": "usd_etched"}.get(finish, "usd"))
    return float(v) if v not in (None, "") else None


class ChoosePrintingDialog(Dialog):
    VIS = 10
    ROW = 46

    def __init__(self, app, entry):
        super().__init__("Choose the printing")
        self.app, self.entry = app, entry
        self.cards = [c for c in (app.db.by_id(i) for i in entry["alts"]) if c]
        self.sims = entry.get("alt_vis") or {}
        self.scroll = 0
        self.sel = entry["id"]

    def draw(self, canvas_bgr):
        import numpy as np
        from PIL import Image, ImageDraw
        H, W = canvas_bgr.shape[:2]
        p = Painter(W, H)
        p.img = Image.fromarray(cv2.cvtColor((canvas_bgr * 0.35).astype(np.uint8), cv2.COLOR_BGR2RGB))
        p.d = ImageDraw.Draw(p.img)
        x0, y0, x1, y1 = 120, 20, W - 120, H - 20
        p.d.rounded_rectangle((x0, y0, x1, y1), radius=14, fill=CARD_BG, outline=BUTTON_HI)
        name = self.cards[0]["name"] if self.cards else ""
        p.text((x0 + 24, y0 + 18), f"Choose the printing - {name}", font="name", fill=YELLOW, width=x1 - x0 - 48)
        e = self.entry
        sub = "Most visually similar first."
        if e.get("printing_uncertain"):
            sub = "The scanner wasn't sure which printing this is. " + sub
        p.text((x0 + 24, y0 + 46), sub, font="small", fill=MUTED, width=x1 - x0 - 48)
        if e.get("printing_reason"):
            p.text((x0 + 24, y0 + 64), "Why: " + e["printing_reason"], font="small", fill=MUTED, width=x1 - x0 - 48)
        y = y0 + 90
        self.scroll = max(0, min(self.scroll, max(0, len(self.cards) - self.VIS)))
        for c in self.cards[self.scroll:self.scroll + self.VIS]:
            on = c["id"] == e["id"]
            p.d.rounded_rectangle((x0 + 24, y, x1 - 24, y + self.ROW - 4), radius=8, fill=BUTTON_HI if on else BUTTON,
                                  outline=YELLOW if on else None)
            p.paste(self.app.thumb(c["id"], 30), x0 + 30, y + 1, 30, 40, radius=3)
            p.text((x0 + 70, y + 4), c.get("set_name") or c["set_code"].upper(), font="rowb", width=330)
            p.text((x0 + 70, y + 23), f"{c['set_code'].upper()} #{c['collector_number']} · {(c.get('rarity') or '').title()}"
                                      f" · {(c.get('released_at') or '')[:4]}", font="small", fill=MUTED, width=330)
            fins = [f for f in ("nonfoil", "foil", "etched") if f in (c.get("finishes") or "nonfoil").split(",")]
            p.text((x0 + 420, y + 4), " / ".join(FINISH_LABEL[f] for f in fins), font="small", width=170)
            price = finish_price(c, e["finish"] if e["finish"] in fins else fins[0])
            p.text((x0 + 420, y + 23), self.app.money(price) if price is not None else "no price", font="small",
                   fill=GOLD if (price or 0) >= 5 else MUTED)
            sim = self.sims.get(c["id"])
            if sim is not None:
                label = f"looks {max(0, min(100, round(sim * 100)))}% like the photo"
                p.text((x1 - 34, y + 4), label, font="small", fill=GREEN if sim >= 0.4 else MUTED, anchor="ra")
            if on:
                p.text((x1 - 34, y + 23), "current", font="label", fill=YELLOW, anchor="ra")
            p.hit((x0 + 24, y, x1 - 24, y + self.ROW - 4), ("print", c["id"]))
            y += self.ROW
        if len(self.cards) > self.VIS:
            p.text((x0 + 24, y1 - 34), f"{self.scroll + 1}-{min(len(self.cards), self.scroll + self.VIS)} of "
                                       f"{len(self.cards)} printings (mouse wheel)", font="small", fill=MUTED)
        p.button((x1 - 144, y1 - 48, x1 - 24, y1 - 14), "Cancel", ("btn", None))
        self.hits = p.hits
        return p.to_bgr()

    def click(self, x, y):
        for x0, y0, x1, y1, a in self.hits:
            if x0 <= x <= x1 and y0 <= y <= y1 and a:
                if a[0] == "print":
                    self.done = True
                    self.app.set_printing(self.entry, a[1])
                elif a[0] == "btn":
                    self.done = True
                return

    def wheel(self, delta):
        self.scroll += -3 if delta > 0 else 3

    def key(self, key):
        if key == 27:
            self.done = True
        elif key in (0x260000, 0x210000):
            self.scroll -= 1 if key == 0x260000 else self.VIS
        elif key in (0x280000, 0x220000):
            self.scroll += 1 if key == 0x280000 else self.VIS
