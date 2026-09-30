"""Find a card in a camera image and read its name + set/collector info."""
import cv2
import numpy as np
from rapidocr import RapidOCR

CARD_W, CARD_H = 630, 880  # 63 x 88 mm, the size of a Magic card

# Regions of a straightened card, as fractions of (x0, y0, x1, y1).
NAME_BOX = (0.04, 0.025, 0.80, 0.105)
# Modern frames print "0123 R" above "SET • EN  ARTIST" in the bottom-left corner.
FOOTER_LINES = [(0.04, 0.925, 0.45, 0.958), (0.04, 0.944, 0.45, 0.977)]


# Picture match counts as sure when the best card's score leads the next card by this much
# (calibrated with tests/test_visual.py).
VIS_GAP = 0.20


class Recognizer:
    def __init__(self, db, visual_index=None):
        self.db = db
        self.ocr = RapidOCR(params={"Global.log_level": "error"})
        self.visual = visual_index

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
        if background_bgr is not None and cv2.contourArea(c) > 0.85 * w * h:
            # Nearly the whole box "changed": the light changed, not just a card
            # arriving. Find the card by its edges instead.
            return Recognizer.find_card(zone_bgr, None)
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
        printings=[most likely first], confident, card=straightened image, how)."""
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
        name_sure = best["top"] >= 97 or (best["top"] >= 85 and best["top"] - second_score(best["candidates"]) >= 8)

        # Recognise the picture: works when the card is too small or blurry to read.
        vis = []
        if self.visual is not None and not (name_sure and best["top"] >= 97):
            vis = self.visual.query(best["img"], k=5)
            if best["top"] < 90:  # name unreadable, so we don't know which way up it is
                flipped = self.visual.query(cv2.rotate(best["img"], cv2.ROTATE_180), k=5)
                if flipped and flipped[0][2] > vis[0][2]:
                    vis = flipped
                    best["img"] = cv2.rotate(best["img"], cv2.ROTATE_180)
        vis_gap = (vis[0][2] - vis[1][2]) if len(vis) > 1 else 0
        vis_sure = bool(vis) and vis_gap >= VIS_GAP

        if best["top"] < 80 and not vis_sure:
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
            name_sure = best["top"] >= 97 or (best["top"] >= 85 and best["top"] - second_score(best["candidates"]) >= 8)

        candidates, confident, how = best["candidates"], name_sure, "name"
        if vis:
            v_name = vis[0][0]
            v_names = [v[0] for v in vis]
            n_top = candidates[0][0] if candidates else None
            if n_top and n_top == v_name and best["top"] >= 60:
                confident, how = True, "name+picture"  # both agree
            elif not name_sure and vis_sure:
                candidates, confident, how = [(v_name, 100.0)] + [c for c in candidates if c[0] != v_name], True, "picture"
            elif not name_sure:
                # Not sure either way: offer the best guesses from both, agreeing ones first.
                merged = {}
                for n, sc in candidates:
                    merged[n] = sc + (20 if n in v_names else 0)
                for rank, (n, _, _) in enumerate(vis):
                    merged[n] = max(merged.get(n, 0), 90 - 8 * rank)
                candidates = sorted(merged.items(), key=lambda t: -t[1])[:5]

        if not candidates:
            return dict(name_text=best["name_text"], footer_text="", candidates=[], printings=[], confident=False,
                        card=best["img"], how="none")
        name = candidates[0][0]
        footer = " ".join(self.read_line(best["img"], b, height=40) for b in FOOTER_LINES)
        return dict(
            name_text=best["name_text"], footer_text=footer, candidates=candidates,
            printings=self.db.ranked_printings(name, footer, locked_set, best["img"]),
            confident=confident, card=best["img"], how=how,
        )


def second_score(cands):
    return cands[1][1] if len(cands) > 1 else 0


def order_corners(pts):
    pts = np.asarray(pts, dtype=np.float32)
    s, d = pts.sum(axis=1), np.diff(pts, axis=1).ravel()
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]], dtype=np.float32)
