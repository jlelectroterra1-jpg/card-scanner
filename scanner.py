"""Webcam MTG card scanner.

    python scanner.py            (use --camera 1 to pick another webcam)

Drag a box on the video where your cards will land, then flip cards through it.
Keys are listed at the bottom of the window.
"""
import argparse
import csv
import json
import os
import queue
import threading
import time
import unicodedata
from datetime import datetime

import cv2
import numpy as np

from carddb import CardDB
from printmatch import IMG_DIR
from recognizer import Recognizer
from visual import INDEX_PATH as VISUAL_INDEX

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(HERE, "data", "settings.json")
SESSION_PATH = os.path.join(HERE, "data", "session.json")
EXPORT_DIR = os.path.join(HERE, "exports")

VIEW_W, VIEW_H = 960, 540  # camera preview size on screen
PANEL_W = 420
WIN = "Card Scanner"
UP_KEY, DOWN_KEY = 0x260000, 0x280000  # arrow keys from cv2.waitKeyEx on Windows
MIN_ZONE_H = 200  # scan box smaller than this (camera pixels) = card too small to recognise

# Scan trigger tuning (on a 96-px-wide grey thumbnail of the scan zone).
PRESENT_FRAC = 0.20   # this share of the zone must differ from the empty desk
CHANGE_FRAC = 0.20    # ...or from the last scanned card, to count as "something new"
STILL_LEVEL = 4.0     # mean pixel change between frames below this = not moving
STILL_FRAMES = 6      # ~0.2 s at 30 fps

GREEN, YELLOW, RED, WHITE, GREY = (80, 200, 80), (0, 210, 255), (60, 60, 230), (240, 240, 240), (150, 150, 150)
KEYS_HELP = [
    "drag = scan zone   B = empty-desk photo   SPACE = scan now   S = add by name",
    "F = foil   G = foil by default   [ ] = printing   DEL = remove last   L = lock set",
    "E = export   C = camera   N = new list   Q = quit",
]


def ascii_text(s):
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".part", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(path + ".part", path)


def beep(ok=True):
    def run():
        try:
            import winsound
            if ok:
                winsound.Beep(1400, 80)
            else:
                winsound.Beep(600, 120)
                winsound.Beep(600, 120)
        except (ImportError, RuntimeError):
            pass
    threading.Thread(target=run, daemon=True).start()


def price_of(card, finish):
    key = {"foil": "usd_foil", "etched": "usd_etched"}.get(finish, "usd")
    try:
        return float(card.get(key) or card.get("usd") or 0)
    except ValueError:
        return 0.0


class Scanner:
    def __init__(self, args):
        print("Loading card database...")
        self.db = CardDB()
        print("Loading text reader...")
        vis = None
        if os.path.exists(VISUAL_INDEX):
            print("Loading picture recognition...")
            from visual import VisualIndex
            vis = VisualIndex()
        else:
            print("No picture index yet (run build_visual_index.py) - reading names only.")
        self.rec = Recognizer(self.db, vis)
        self.settings = load_json(SETTINGS_PATH, {})
        if args.camera is not None:
            self.settings["camera"] = args.camera
        self.entries = load_json(SESSION_PATH, [])  # one dict per physical card scanned
        self.cap = None
        self.open_camera(self.settings.get("camera", 0))

        self.zone = self.settings.get("zone")  # [x0, y0, x1, y1] in camera pixels
        self.background = None
        self.drag = None
        self.prev_small = None
        self.still = 0
        self.armed = True
        self.last_scanned_small = None
        self.busy = False
        self.review = None  # recognition result waiting for the user to choose
        self.typing = None  # {"kind": "search" | "lock", "text", "matches", "sel"} while typing in the window
        self.status, self.status_color = "", WHITE
        self.results = queue.Queue()
        self.last_frame = None
        self.new_list_pressed = 0.0
        self.thumb_cache = {}

    # ---- camera ---------------------------------------------------------

    def open_camera(self, index):
        if self.cap is not None:
            self.cap.release()
        cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
        if not cap.isOpened():
            cap.release()
            cap = cv2.VideoCapture(index)
        self.cap = cap
        self.settings["camera"] = index
        self.cam_index = index
        self.background = None
        ok, frame = cap.read()
        if ok:
            print(f"Camera {index}: {frame.shape[1]}x{frame.shape[0]}")
        else:
            print(f"Camera {index} gives no picture - press C to try the next one.")

    # ---- zone / trigger -------------------------------------------------

    def zone_crop(self, frame):
        x0, y0, x1, y1 = self.zone
        return frame[y0:y1, x0:x1]

    @staticmethod
    def small(zone_img):
        g = cv2.cvtColor(zone_img, cv2.COLOR_BGR2GRAY)
        w = 96
        return cv2.GaussianBlur(cv2.resize(g, (w, max(8, int(w * g.shape[0] / g.shape[1])))), (5, 5), 0).astype(np.int16)

    @staticmethod
    def frac_diff(a, b, level=25):
        return float((np.abs(a - b) > level).mean())

    def capture_background(self, frame):
        self.background = self.zone_crop(frame).copy()
        self.armed = True
        self.last_scanned_small = None
        self.set_status("Empty desk saved - put a card in the box", GREEN)

    def update_trigger(self, frame):
        """Watch the zone and start a scan when a new card has settled in it."""
        z = self.zone_crop(frame)
        s = self.small(z)
        if self.prev_small is not None and self.prev_small.shape == s.shape:
            moving = np.abs(s - self.prev_small).mean() > STILL_LEVEL
            self.still = 0 if moving else self.still + 1
        self.prev_small = s
        bg_small = self.small(self.background)
        present = self.frac_diff(s, bg_small) > PRESENT_FRAC
        if not self.armed:
            # Wait until the last card is taken away or covered by a new one.
            if not present or (self.last_scanned_small is not None
                               and self.frac_diff(s, self.last_scanned_small) > CHANGE_FRAC):
                self.armed = True
            return
        if present and self.still >= STILL_FRAMES and not self.busy and self.review is None:
            self.start_scan(frame)

    def start_scan(self, frame):
        self.busy = True
        self.armed = False
        z = self.zone_crop(frame).copy()
        self.last_scanned_small = self.small(z)
        bg = self.background.copy() if self.background is not None else None
        locked = self.settings.get("locked_set")
        self.set_status("Reading card...", YELLOW)

        def work():
            t = time.time()
            card = self.rec.find_card(z, bg)
            if card is None:
                card = self.rec.find_card(z, None)
            if card is None:  # assume the zone is drawn snugly around the card
                card = cv2.resize(z, (630, 880))
            try:
                res = self.rec.identify(card, locked)
            except Exception as e:  # never let one bad frame kill the scanner
                res = dict(candidates=[], printings=[], confident=False, name_text=f"error: {e}", card=card)
            res["secs"] = time.time() - t
            self.results.put(res)

        threading.Thread(target=work, daemon=True).start()

    # ---- results --------------------------------------------------------

    def handle_result(self, res):
        self.busy = False
        if res["confident"] and res["printings"]:
            self.add_card(res["printings"], res["card"])
            beep(True)
        elif res["candidates"]:
            self.review = res
            beep(False)
            self.set_status("Not sure - pick 1-5, S to type the name, X to skip", YELLOW)
        else:
            beep(False)
            if self.zone and self.zone[3] - self.zone[1] < MIN_ZONE_H:
                self.set_status("Couldn't read it - the card is too small in the picture. Move the camera closer.", RED)
            else:
                self.set_status("Couldn't read the name - SPACE to retry, S to type it", RED)

    def choose_candidate(self, i):
        res = self.review
        if res is None or i >= len(res["candidates"]):
            return
        name = res["candidates"][i][0]
        self.set_status("Finding printing...", YELLOW)
        prints = self.db.ranked_printings(name, res.get("footer_text", ""), self.settings.get("locked_set"), res["card"])
        self.review = None
        if prints:
            self.add_card(prints, res["card"])
            beep(True)

    def add_card(self, printings, card_img):
        top = printings[0]
        finish = self.settings.get("default_finish", "nonfoil")
        if finish not in (top["finishes"] or "nonfoil").split(","):
            finish = (top["finishes"] or "nonfoil").split(",")[0]
        self.entries.append(dict(
            id=top["id"], finish=finish, alts=[p["id"] for p in printings[:40]], alt_idx=0,
            time=datetime.now().isoformat(timespec="seconds"),
        ))
        self.save_session()
        self.set_status(f"+ {ascii_text(top['name'])}", GREEN)

    def save_session(self):
        save_json(SESSION_PATH, self.entries)

    def cycle_printing(self, step):
        if not self.entries:
            return
        e = self.entries[-1]
        e["alt_idx"] = (e["alt_idx"] + step) % len(e["alts"])
        e["id"] = e["alts"][e["alt_idx"]]
        card = self.db.by_id(e["id"])
        finishes = (card["finishes"] or "nonfoil").split(",")
        if e["finish"] not in finishes:
            e["finish"] = finishes[0]
        self.save_session()
        self.set_status(f"Printing {e['alt_idx'] + 1}/{len(e['alts'])}: {card['set_name']} #{card['collector_number']}", WHITE)

    def cycle_finish(self):
        if not self.entries:
            return
        e = self.entries[-1]
        card = self.db.by_id(e["id"])
        finishes = (card["finishes"] or "nonfoil").split(",")
        e["finish"] = finishes[(finishes.index(e["finish"]) + 1) % len(finishes)] if e["finish"] in finishes else finishes[0]
        self.save_session()
        self.set_status(f"Finish: {e['finish']}", WHITE)

    # ---- export ---------------------------------------------------------

    def export(self):
        if not self.entries:
            self.set_status("Nothing to export yet", RED)
            return
        os.makedirs(EXPORT_DIR, exist_ok=True)
        counts = {}
        for e in self.entries:
            counts[(e["id"], e["finish"])] = counts.get((e["id"], e["finish"]), 0) + 1
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        manabox = os.path.join(EXPORT_DIR, f"scan_{stamp}_manabox.csv")
        plain = os.path.join(EXPORT_DIR, f"scan_{stamp}_decklist.txt")
        with open(manabox, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Name", "Set code", "Set name", "Collector number", "Foil", "Rarity", "Quantity",
                        "Scryfall ID", "Purchase price", "Condition", "Language", "Purchase price currency"])
            for (cid, finish), qty in counts.items():
                c = self.db.by_id(cid)
                foil = {"foil": "foil", "etched": "etched"}.get(finish, "normal")
                w.writerow([c["name"], c["set_code"].upper(), c["set_name"], c["collector_number"], foil, c["rarity"],
                            qty, cid, f"{price_of(c, finish):.2f}", "near_mint", c["lang"], "USD"])
        with open(plain, "w", encoding="utf-8") as f:
            for (cid, finish), qty in counts.items():
                c = self.db.by_id(cid)
                tag = {"foil": " *F*", "etched": " *E*"}.get(finish, "")
                f.write(f"{qty} {c['name']} ({c['set_code'].upper()}) {c['collector_number']}{tag}\n")
        self.set_status(f"Exported {len(self.entries)} cards to exports\\", GREEN)
        print(f"Exported:\n  {manabox}\n  {plain}")
        if os.name == "nt":
            os.startfile(EXPORT_DIR)

    # ---- drawing --------------------------------------------------------

    def set_status(self, text, color=WHITE):
        self.status, self.status_color = text, color

    def thumb(self, card_id, w):
        key = (card_id, w)
        if key not in self.thumb_cache:
            img = cv2.imread(os.path.join(IMG_DIR, card_id + ".jpg"))
            if img is None:
                c = self.db.by_id(card_id)
                if c:
                    from printmatch import _fetch
                    _fetch(c)
                    img = cv2.imread(os.path.join(IMG_DIR, card_id + ".jpg"))
            self.thumb_cache[key] = None if img is None else cv2.resize(img, (w, int(w * img.shape[0] / img.shape[1])))
        return self.thumb_cache[key]

    def draw(self, frame):
        fh, fw = frame.shape[:2]
        self.scale = min(VIEW_W / fw, VIEW_H / fh)
        view = cv2.resize(frame, (int(fw * self.scale), int(fh * self.scale)))
        canvas = np.full((VIEW_H + 70, VIEW_W + PANEL_W, 3), 28, np.uint8)
        canvas[:view.shape[0], :view.shape[1]] = view

        box = self.drag or self.zone
        if box:
            x0, y0, x1, y1 = [int(v * self.scale) for v in box]
            color = YELLOW if self.busy else (GREEN if self.armed else GREY)
            cv2.rectangle(canvas, (x0, y0), (x1, y1), color, 2)
            if self.background is None and not self.drag:
                put(canvas, "Clear the box, then press B", (x0 + 6, y0 + 22), YELLOW, 0.55)
        else:
            put(canvas, "Drag a box where your cards will land", (20, 40), YELLOW, 0.8)

        put(canvas, self.status, (12, VIEW_H + 24), self.status_color, 0.6)
        for i, line in enumerate(KEYS_HELP[:2]):
            put(canvas, line, (12, VIEW_H + 46 + i * 18), GREY, 0.42)
        put(canvas, KEYS_HELP[2], (VIEW_W - 330, VIEW_H + 64), GREY, 0.42)
        self.draw_panel(canvas)
        return canvas

    def draw_panel(self, canvas):
        x = VIEW_W + 14
        total = sum(price_of(self.db.by_id(e["id"]) or {}, e["finish"]) for e in self.entries)
        put(canvas, f"{len(self.entries)} cards   ${total:,.2f}", (x, 30), WHITE, 0.7)
        lock = self.settings.get("locked_set")
        put(canvas, f"camera {self.cam_index}" + (f"   set locked: {lock.upper()}" if lock else "")
            + ("   default: FOIL" if self.settings.get("default_finish") == "foil" else ""), (x, 52), GREY, 0.45)

        if self.typing is not None:
            t = self.typing
            put(canvas, "Type the card name:" if t["kind"] == "search" else "Set code to lock (empty = any set):",
                (x, 90), YELLOW, 0.6)
            put(canvas, t["text"] + "_", (x, 124), WHITE, 0.7)
            for i, name in enumerate(t["matches"]):
                sel = i == t["sel"]
                put(canvas, ("> " if sel else "  ") + ascii_text(name)[:34], (x, 160 + i * 26), GREEN if sel else WHITE, 0.55)
            put(canvas, "ENTER = ok   ESC = cancel" + ("   up/down = choose" if t["kind"] == "search" else ""),
                (x, 300), GREY, 0.45)
            if self.review is not None:
                canvas[320:530, x:x + 150] = cv2.resize(self.review["card"], (150, 210))
            return

        if self.review is not None:
            put(canvas, "Which card is it?", (x, 90), YELLOW, 0.65)
            for i, (name, score) in enumerate(self.review["candidates"][:5]):
                put(canvas, f"{i + 1}. {ascii_text(name)[:32]}", (x, 120 + i * 26), WHITE, 0.55)
                put(canvas, f"{score:.0f}%", (x + 360, 120 + i * 26), GREY, 0.45)
            put(canvas, "X = skip    S = search by name", (x, 262), GREY, 0.5)
            card = cv2.resize(self.review["card"], (150, 210))
            canvas[290:500, x:x + 150] = card
            return

        if self.entries:
            e = self.entries[-1]
            c = self.db.by_id(e["id"])
            t = self.thumb(e["id"], 150)
            if t is not None:
                h = min(t.shape[0], 212)
                canvas[70:70 + h, x:x + 150] = t[:h]
            tx = x + 162
            put(canvas, ascii_text(c["name"])[:22], (tx, 90), WHITE, 0.55)
            put(canvas, ascii_text(c["set_name"])[:26], (tx, 114), GREY, 0.45)
            put(canvas, f"{c['set_code'].upper()} #{c['collector_number']}  {c['rarity']}", (tx, 136), GREY, 0.45)
            put(canvas, e["finish"].upper() if e["finish"] != "nonfoil" else "non-foil", (tx, 158),
                YELLOW if e["finish"] != "nonfoil" else GREY, 0.45)
            p = price_of(c, e["finish"])
            put(canvas, f"${p:,.2f}", (tx, 188), (0, 180, 255) if p >= 5 else WHITE, 0.7)
            put(canvas, f"printing {e['alt_idx'] + 1}/{len(e['alts'])}  [ ]", (tx, 212), GREY, 0.42)

        y = 310
        for e in reversed(self.entries[-10:-1] if len(self.entries) > 1 else []):
            c = self.db.by_id(e["id"])
            p = price_of(c, e["finish"])
            f = "*" if e["finish"] != "nonfoil" else ""
            put(canvas, f"{ascii_text(c['name'])[:26]}{f}", (x, y), WHITE, 0.45)
            put(canvas, f"{c['set_code'].upper()}  ${p:,.2f}", (x + 290, y), (0, 180, 255) if p >= 5 else GREY, 0.42)
            y += 24

    # ---- input ----------------------------------------------------------

    def on_mouse(self, event, x, y, flags, _):
        if x >= VIEW_W or not hasattr(self, "scale"):
            return
        cx, cy = int(x / self.scale), int(y / self.scale)
        if event == cv2.EVENT_LBUTTONDOWN:
            self.drag = [cx, cy, cx, cy]
        elif event == cv2.EVENT_MOUSEMOVE and self.drag:
            self.drag[2], self.drag[3] = cx, cy
        elif event == cv2.EVENT_LBUTTONUP and self.drag:
            x0, x1 = sorted((self.drag[0], self.drag[2]))
            y0, y1 = sorted((self.drag[1], self.drag[3]))
            self.drag = None
            if x1 - x0 > 40 and y1 - y0 > 40:
                self.zone = [x0, y0, x1, y1]
                self.settings["zone"] = self.zone
                save_json(SETTINGS_PATH, self.settings)
                self.prev_small = None
                if self.last_frame is not None:
                    self.capture_background(self.last_frame)
                    if y1 - y0 < MIN_ZONE_H:
                        self.set_status("Box is small: move the camera closer so the card looks bigger", YELLOW)
                    else:
                        self.set_status("Box set & empty desk saved (press B again if a card was in it)", GREEN)

    def on_key(self, key):
        if self.typing is not None:
            self.on_typing_key(key)
            return True
        if key in (ord("q"), 27):
            return False
        ch = chr(key).lower() if 0 <= key < 256 else ""
        if self.review is not None:
            if ch in "12345" and ch:
                self.choose_candidate(int(ch) - 1)
            elif ch == "x":
                self.review = None
                self.set_status("Skipped", GREY)
            elif ch == "s":
                self.start_typing("search")
            elif key == 13 and self.review["candidates"]:
                self.choose_candidate(0)
            return True
        if ch == "b" and self.zone and self.last_frame is not None:
            self.capture_background(self.last_frame)
        elif key == 32 and self.zone and self.last_frame is not None and not self.busy:
            self.start_scan(self.last_frame)
        elif ch == "f":
            self.cycle_finish()
        elif ch == "]":
            self.cycle_printing(1)
        elif ch == "[":
            self.cycle_printing(-1)
        elif key in (8, 0x2E0000, 0x7F) or ch == "u":
            if self.entries:
                gone = self.entries.pop()
                self.save_session()
                self.set_status(f"Removed {ascii_text(self.db.by_id(gone['id'])['name'])}", GREY)
        elif ch == "e":
            self.export()
        elif ch == "c":
            self.open_camera((self.cam_index + 1) % 5)
            save_json(SETTINGS_PATH, self.settings)
        elif ch == "l":
            self.start_typing("lock", self.settings.get("locked_set") or "")
        elif ch == "s":
            self.start_typing("search")
        elif ch == "g":
            foil = self.settings.get("default_finish") == "foil"
            self.settings["default_finish"] = "nonfoil" if foil else "foil"
            save_json(SETTINGS_PATH, self.settings)
            self.set_status(f"New cards default to {'non-foil' if foil else 'FOIL'}", WHITE)
        elif ch == "n":
            if time.time() - self.new_list_pressed < 3:
                if self.entries:
                    self.export()
                self.entries = []
                self.save_session()
                self.set_status("Started a new list (old one was exported)", GREEN)
            else:
                self.new_list_pressed = time.time()
                self.set_status("Press N again to export and start a new list", YELLOW)
        return True

    # Typing happens in the scanner window itself (never the terminal, which would
    # freeze the window while it waits).
    def start_typing(self, kind, text=""):
        self.typing = dict(kind=kind, text=text, matches=[], sel=0)
        self.set_status("Type the card name, then ENTER" if kind == "search" else "Type a set code, then ENTER", YELLOW)

    def on_typing_key(self, key):
        t = self.typing
        if key == 27:
            self.typing = None
            self.set_status("Cancelled", GREY)
            return
        if key == 13:
            self.finish_typing()
            return
        if key == UP_KEY:
            t["sel"] = max(0, t["sel"] - 1)
        elif key == DOWN_KEY:
            t["sel"] = min(max(0, len(t["matches"]) - 1), t["sel"] + 1)
        elif key == 8:
            t["text"] = t["text"][:-1]
        elif 32 <= key < 127:
            t["text"] += chr(key)
        else:
            return
        if t["kind"] == "search" and key not in (UP_KEY, DOWN_KEY):
            t["matches"] = self.db.search(t["text"], 5) if len(t["text"].strip()) >= 2 else []
            t["sel"] = 0

    def finish_typing(self):
        t, self.typing = self.typing, None
        if t["kind"] == "lock":
            code = t["text"].strip().lower()
            if code and code not in self.db.set_codes:
                self.set_status(f"Unknown set code '{code}' - press L to try again", RED)
                return
            self.settings["locked_set"] = code or None
            save_json(SETTINGS_PATH, self.settings)
            self.set_status(f"Locked to {code.upper()}" if code else "Set unlocked", GREEN)
            return
        if not t["matches"]:
            self.set_status("No card by that name - press S to try again", RED)
            return
        name = t["matches"][t["sel"]]
        card_img = self.review["card"] if self.review is not None else None
        prints = self.db.ranked_printings(name, "", self.settings.get("locked_set"), card_img)
        self.review = None
        if prints:
            self.add_card(prints, card_img)
            beep(True)

    def refresh(self):
        if self.last_frame is not None:
            cv2.imshow(WIN, self.draw(self.last_frame))
            cv2.waitKey(1)

    # ---- main loop ------------------------------------------------------

    def run(self):
        cv2.namedWindow(WIN, cv2.WINDOW_AUTOSIZE)
        cv2.setMouseCallback(WIN, self.on_mouse)
        self.set_status("Ready" if self.zone else "Drag a box where cards will land", WHITE)
        fails = 0
        while True:
            ok, frame = self.cap.read()
            if not ok:
                fails += 1
                frame = self.last_frame if self.last_frame is not None else np.zeros((720, 1280, 3), np.uint8)
                if fails == 30:
                    self.set_status(f"Camera {self.cam_index} not giving a picture - press C", RED)
            else:
                fails = 0
                self.last_frame = frame
                if self.zone:
                    h, w = frame.shape[:2]
                    x0, y0, x1, y1 = self.zone
                    if x1 > w or y1 > h:  # camera resolution changed
                        self.zone = None
                    elif self.background is not None:
                        self.update_trigger(frame)
            while not self.results.empty():
                self.handle_result(self.results.get())
            cv2.imshow(WIN, self.draw(frame))
            key = cv2.waitKeyEx(1)
            if key != -1 and not self.on_key(key):
                break
            if cv2.getWindowProperty(WIN, cv2.WND_PROP_VISIBLE) < 1:
                break
        self.save_session()
        save_json(SETTINGS_PATH, self.settings)
        self.cap.release()
        cv2.destroyAllWindows()
        print(f"Saved {len(self.entries)} cards. Press E next time to export, or they'll still be here.")


def put(img, text, org, color, scale):
    """Text on a dark box, so it stays readable over the camera picture."""
    (w, h), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
    x, y = org
    cv2.rectangle(img, (x - 3, y - h - 4), (x + w + 3, y + base + 2), (28, 28, 28), -1)
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, color, 1, cv2.LINE_AA)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Webcam MTG card scanner")
    ap.add_argument("--camera", type=int, help="webcam number (0, 1, 2...)")
    Scanner(ap.parse_args()).run()
