"""The Print Deck dialog: options (commander, basic lands, reverse faces, cutting guides, A4),
the card and page count before exporting, where to save it, progress while card images are
fetched in the background, and the printing instructions afterwards. The deck itself and the
collection are never changed."""
import os
import threading

import printdeck
from decks_view import _wrap
from ui import BUTTON, BUTTON_HI, CARD_BG, GREEN, MUTED, RED, TEXT, YELLOW, Dialog, Painter, open_folder


class PrintDialog(Dialog):
    def __init__(self, app, items, deck_name, folder=None):
        super().__init__(f"Print {deck_name}")
        self.app, self.items, self.deck_name = app, items, deck_name
        self.opts = printdeck.Options()
        folder = folder or (app.export_dir() if hasattr(app, "export_dir") else os.getcwd())
        self.path = os.path.join(folder, printdeck.default_filename(deck_name))
        self.running = None   # dict(progress, text) while exporting
        self.result = None
        self.error = None
        self._lock = threading.Lock()

    # ---- numbers shown before exporting ---------------------------------------------------------
    def counts(self):
        p = printdeck.plan(self.items, self.opts)
        backs = len(p.backs)
        return p.cards, printdeck.pages_for(p.cards), backs

    # ---- drawing -----------------------------------------------------------------------------
    def draw(self, canvas_bgr):
        import cv2
        import numpy as np
        from PIL import Image, ImageDraw
        H, W = canvas_bgr.shape[:2]
        p = Painter(W, H)
        p.img = Image.fromarray(cv2.cvtColor((canvas_bgr * 0.35).astype(np.uint8), cv2.COLOR_BGR2RGB))
        p.d = ImageDraw.Draw(p.img)
        w, h = 640, 470
        x0, y0 = (W - w) // 2, max(10, (H - h) // 2)
        x1, y1 = x0 + w, y0 + h
        p.d.rounded_rectangle((x0, y0, x1, y1), radius=14, fill=CARD_BG, outline=BUTTON_HI)
        p.text((x0 + 24, y0 + 18), self.title, font="name", fill=YELLOW, width=w - 48)
        p.text((x0 + 24, y0 + 44), "A Word document with every card at real size (63 x 88 mm), nine per A4 page.",
               font="small", fill=MUTED, width=w - 48)
        y = y0 + 72
        if self.result is not None:
            return self._draw_done(p, x0, x1, y, y1)
        busy = self.running is not None
        for key, label in (("include_commander", "Include commander"), ("include_basics", "Include basic lands"),
                           ("include_backs", "Include reverse faces of double-faced cards (on extra pages)"),
                           ("guides", "Cutting guides (thin light-grey lines on the card edges)")):
            on = getattr(self.opts, key)
            p.button((x0 + 24, y, x0 + 50, y + 26), "", None if busy else ("toggle", key),
                     style="primary" if on else "normal", font="label")
            if on:  # a drawn tick (not every font has the symbol)
                p.d.line([(x0 + 30, y + 13), (x0 + 35, y + 19), (x0 + 44, y + 7)], fill=(10, 30, 16), width=3)
            p.text((x0 + 60, y + 13), label, font="body", anchor="lm", width=w - 90)
            p.hit((x0 + 24, y, x1 - 24, y + 26), None if busy else ("toggle", key))
            y += 34
        p.text((x0 + 24, y + 4), "Paper size: A4 (210 x 297 mm), portrait, 3 x 3 cards", font="body")
        y += 30
        cards, pages, backs = self.counts()
        line = f"{cards} cards on {pages} page{'s' if pages != 1 else ''}"
        if backs:
            line += f"  +  up to {backs} back face{'s' if backs != 1 else ''} on extra pages"
        p.text((x0 + 24, y + 4), line, font="rowb", fill=GREEN)
        y += 30
        p.text((x0 + 24, y + 4), "Save as:", font="label", fill=MUTED)
        p.text((x0 + 90, y + 4), self.path, font="small", width=w - 230)
        p.button((x1 - 124, y, x1 - 24, y + 26), "Change...", None if busy else ("path", None), font="label")
        y += 36
        fits, _, msg = printdeck.layout_check(self.opts.paper)
        if not fits:
            p.text((x0 + 24, y), msg, font="small", fill=RED, width=w - 48)
        note = ("For personal use - printing your own decks to play with. Card images come from Scryfall and are "
                "(c) Wizards of the Coast; please don't sell printed proxies.")
        for i, line in enumerate(_wrap(p, note, "small", w - 48)[:2]):
            p.text((x0 + 24, y1 - 92 + i * 16), line, font="small", fill=MUTED)
        if self.error:
            p.text((x0 + 24, y1 - 104), self.error, font="small", fill=RED, width=w - 48)
        if busy:
            r = self.running
            p.d.rounded_rectangle((x0 + 24, y1 - 54, x1 - 24, y1 - 22), radius=8, fill=BUTTON)
            p.d.rounded_rectangle((x0 + 24, y1 - 54, x0 + 24 + max(16, (w - 48) * r["progress"]), y1 - 22), radius=8,
                                  fill=(52, 120, 76))
            p.text(((x0 + x1) / 2, y1 - 38), r["text"], font="small", anchor="mm", width=w - 70)
        else:
            p.button((x1 - 250, y1 - 54, x1 - 140, y1 - 20), "Cancel", ("cancel", None))
            p.button((x1 - 132, y1 - 54, x1 - 24, y1 - 20), "Export", ("export", None) if fits and cards else None,
                     style="primary" if fits and cards else "disabled")
        self.hits = p.hits
        return p.to_bgr()

    def _draw_done(self, p, x0, x1, y, y1):
        r = self.result
        p.text((x0 + 24, y), "Your printable deck is ready.", font="name", fill=GREEN)
        y += 30
        for line in (f"{r['cards']} cards" + (f" + {r['back_faces']} back faces" if r["back_faces"] else "")
                     + f" on {r['pages']} pages - {r['path']}",
                     "Open the Word document and print on A4 paper at 100% / Actual Size.",
                     "Do not use Fit to Page."):
            p.text((x0 + 24, y), line, font="body", fill=TEXT, width=x1 - x0 - 48)
            y += 24
        for wline in r["warnings"]:
            p.text((x0 + 24, y + 6), wline, font="small", fill=YELLOW, width=x1 - x0 - 48)
            y += 34
        p.button((x0 + 24, y1 - 54, x0 + 184, y1 - 20), "Open document", ("open", None), style="primary")
        p.button((x0 + 192, y1 - 54, x0 + 330, y1 - 20), "Open folder", ("folder", None))
        p.button((x1 - 124, y1 - 54, x1 - 24, y1 - 20), "Close", ("cancel", None))
        self.hits = p.hits
        return p.to_bgr()

    # ---- input --------------------------------------------------------------------------------
    def click(self, x, y):
        for x0, y0, x1, y1, a in reversed(self.hits):
            if a and x0 <= x <= x1 and y0 <= y <= y1:
                kind, v = a
                if kind == "toggle":
                    setattr(self.opts, v, not getattr(self.opts, v))
                elif kind == "cancel":
                    if self.running is None:
                        self.done = True
                elif kind == "path":
                    self.change_path()
                elif kind == "export":
                    self.confirm_and_export()
                elif kind == "open":
                    os.startfile(self.result["path"])  # noqa: S606 - opens the user's own document
                elif kind == "folder":
                    open_folder(os.path.dirname(self.result["path"]))
                return

    def key(self, key):
        if key == 27 and self.running is None:
            self.done = True

    def change_path(self):
        ask = getattr(self.app, "ask_save_file", None)
        new = ask("Save the printable deck", self.path, [("Word document", "*.docx")]) if ask else None
        if new:
            self.path = new if new.lower().endswith(".docx") else new + ".docx"

    def confirm_and_export(self):
        if os.path.exists(self.path):
            def done(ok, _t):
                self.app.open_dialog(self)  # back to this dialog either way
                if ok:
                    self.start()
            self.app.open_dialog(Dialog("Replace the existing file?",
                                        [f"{os.path.basename(self.path)} already exists in that folder.",
                                         "Replace it with the new printable deck?"],
                                        buttons=[("Cancel", False, "normal"), ("Replace", True, "danger")],
                                        on_done=done))
            return
        self.start()

    def start(self):
        if self.running is not None:
            return
        self.running, self.error = dict(progress=0.0, text="Starting..."), None

        def progress(f, text):
            self.running = dict(progress=f, text=text)
            self._bump()

        def work():
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                self.result = printdeck.export(self.items, self.path, self.opts, progress)
            except PermissionError:
                self.error = "Couldn't save - is the document still open in Word? Close it and try again."
            except Exception as e:  # noqa: BLE001
                self.error = f"Export failed: {e}"
            finally:
                self.running = None
                self._bump()
        threading.Thread(target=work, daemon=True).start()

    def _bump(self):
        bump = getattr(self.app, "bump", None)
        if bump:
            bump()
