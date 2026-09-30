"""Find a card in a camera image and read its name + set/collector info."""
import cv2
import numpy as np
from rapidocr import RapidOCR

CARD_W, CARD_H = 630, 880  # 63 x 88 mm, the size of a Magic card

# Regions of a straightened card, as fractions of (x0, y0, x1, y1).
NAME_BOX = (0.04, 0.025, 0.80, 0.105)
# Modern frames print "0123 R" above "SET • EN  ARTIST" in the bottom-left corner.
FOOTER_LINES = [(0.04, 0.925, 0.45, 0.958), (0.04, 0.944, 0.45, 0.977)]


class Recognizer:
    def __init__(self, db):
        self.db = db
        self.ocr = RapidOCR(params={"Global.log_level": "error"})

    # ---- geometry -------------------------------------------------------

    @staticmethod
    def find_card(zone_bgr, background_bgr=None):
        """Return the card straightened to CARD_W x CARD_H, or None.

        With an empty-desk background we look at what changed; that copes with
        wood grain, playmats etc. Without one we fall back to edge detection."""
        h, w = zone_bgr.shape[:2]
        if background_bgr is not None:
            diff = cv2.absdiff(cv2.GaussianBlur(zone_bgr, (5, 5), 0), cv2.GaussianBlur(background_bgr, (5, 5), 0))
            mask = (diff.max(axis=2) > 30).astype(np.uint8) * 255
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
        else:
            gray = cv2.cvtColor(zone_bgr, cv2.COLOR_BGR2GRAY)
            mask = cv2.dilate(cv2.Canny(cv2.GaussianBlur(gray, (5, 5), 0), 40, 120), np.ones((5, 5), np.uint8))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None
        c = max(contours, key=cv2.contourArea)
        if cv2.contourArea(c) < 0.12 * w * h:
            return None
        box = order_corners(cv2.boxPoints(cv2.minAreaRect(c)))
        tl, tr, br, bl = box
        if np.linalg.norm(tr - tl) > np.linalg.norm(bl - tl):  # lying sideways -> make it portrait
            box = np.array([tr, br, bl, tl], dtype=np.float32)
        dst = np.array([[0, 0], [CARD_W - 1, 0], [CARD_W - 1, CARD_H - 1], [0, CARD_H - 1]], dtype=np.float32)
        return cv2.warpPerspective(zone_bgr, cv2.getPerspectiveTransform(box, dst), (CARD_W, CARD_H))

    # ---- reading --------------------------------------------------------

    def read_line(self, img, box, height=48):
        """OCR one line of text. We skip RapidOCR's text *detector* (it struggles
        on thin strips) and run its recogniser directly on the crop."""
        x0, y0, x1, y1 = box
        h, w = img.shape[:2]
        crop = img[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)]
        crop = cv2.resize(crop, None, fx=height / crop.shape[0], fy=height / crop.shape[0], interpolation=cv2.INTER_CUBIC)
        res = self.ocr(crop, use_det=False, use_cls=False, use_rec=True)
        if res is None or not res.txts:
            return ""
        return res.txts[0].strip()

    def identify(self, card_bgr, locked_set=None):
        """Returns dict(name_text, footer_text, candidates=[(name, score)],
        printings=[most likely first], confident, card=straightened image)."""
        best = None
        # Cards can land upside down; try both ways and keep whichever reads better.
        for img in (card_bgr, cv2.rotate(card_bgr, cv2.ROTATE_180)):
            text = self.read_line(img, NAME_BOX)
            cands = self.db.match_name(text)
            top = cands[0][1] if cands else 0
            if best is None or top > best["top"]:
                best = dict(img=img, name_text=text, candidates=cands, top=top)
            if top >= 90:
                break
        if best["top"] < 80:
            # Showcase / borderless / split frames put the name somewhere else,
            # so read everything on the card and see if any line is a card name.
            for img in (best["img"], cv2.rotate(best["img"], cv2.ROTATE_180)):
                res = self.ocr(img)
                for line in (res.txts or []) if res is not None else []:
                    cands = self.db.match_name(line)
                    if len(line) >= 4 and cands and cands[0][1] > best["top"]:
                        best = dict(img=img, name_text=line, candidates=cands, top=cands[0][1])
                if best["top"] >= 90:
                    break
        if not best["candidates"]:
            return dict(name_text=best["name_text"], footer_text="", candidates=[], printings=[], confident=False, card=best["img"])
        footer = " ".join(self.read_line(best["img"], b, height=40) for b in FOOTER_LINES)
        name = best["candidates"][0][0]
        second = best["candidates"][1][1] if len(best["candidates"]) > 1 else 0
        # An exact read is trusted even when a similar name exists (Lightning Bolt / Lightning Colt).
        confident = best["top"] >= 97 or (best["top"] >= 85 and best["top"] - second >= 8)
        return dict(
            name_text=best["name_text"], footer_text=footer, candidates=best["candidates"],
            printings=self.db.ranked_printings(name, footer, locked_set, best["img"]),
            confident=confident, card=best["img"],
        )


def order_corners(pts):
    pts = np.asarray(pts, dtype=np.float32)
    s, d = pts.sum(axis=1), np.diff(pts, axis=1).ravel()
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]], dtype=np.float32)
