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

# "Is there really a card?" checks, used before auto-scans when an empty-background photo
# is known. Similarity is lighting-independent (zero-mean normalised correlation), so a
# brighter/darker view of the same playmat still counts as "the background".
# On real scans: empty playmat vs its learned background 0.99-1.00, cards vs background <= 0.39.
EMPTY_SIMILARITY = 0.80   # whole box this similar to the background...
EMPTY_CHANGED_FRAC = 0.12  # ...and less than this share changed beyond a brightness shift -> nothing there
REGION_SIMILARITY = 0.60  # the "card" found is this similar to the same spot of the background -> playmat art
MIN_CARD_FRAC = 0.08      # a card must cover at least this share of the scan box
FILLS_BOX_CHANGED_FRAC = 0.30  # no outline found, but this much of the box is new -> the card fills the box (real: empty <0.01, cards >=0.44)
CARD_ASPECT = (0.50, 0.95)  # short side / long side (a Magic card is 0.716; a finger over an edge squares it up)


def background_similarity(img_a, img_b):
    """How alike two views of the same area are, ignoring brightness/contrast changes:
    1.0 = same picture, ~0 = unrelated. Small shifts and noise are smoothed away."""
    def norm(img):
        g = cv2.cvtColor(cv2.resize(img, (48, 64), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
        g = cv2.GaussianBlur(g.astype(np.float32), (3, 3), 0)
        return (g - g.mean()) / (g.std() + 1e-3)
    return float((norm(img_a) * norm(img_b)).mean())


def changed_fraction(img, background, level=28):
    """Share of the area that differs from the background after allowing for an
    overall brightness change (the room getting lighter/darker)."""
    a = cv2.GaussianBlur(cv2.cvtColor(cv2.resize(img, (64, 85), interpolation=cv2.INTER_AREA),
                                      cv2.COLOR_BGR2GRAY).astype(np.float32), (5, 5), 0)
    b = cv2.GaussianBlur(cv2.cvtColor(cv2.resize(background, (64, 85), interpolation=cv2.INTER_AREA),
                                      cv2.COLOR_BGR2GRAY).astype(np.float32), (5, 5), 0)
    gain = float(np.clip(np.median((a + 8) / (b + 8)), 0.5, 2.0))
    return float((np.abs(a - gain * b) > level).mean())


def looks_empty(zone, background):
    """True when the scan box shows just the learned background: same pattern
    (lighting-independent) and nothing new beyond an overall brightness change."""
    return (background_similarity(zone, background) >= EMPTY_SIMILARITY
            and changed_fraction(zone, background) < EMPTY_CHANGED_FRAC)


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
        box = Recognizer.card_quad(zone_bgr, background_bgr)
        return None if box is None else warp_card(zone_bgr, box)

    @staticmethod
    def card_quad(zone_bgr, background_bgr=None):
        """The card's four corners (tl, tr, br, bl, portrait) in the zone, or None."""
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
        if background_bgr is None:
            # Edges alone: take the biggest outline that is actually card-shaped, so a
            # card held far away (small in the picture) is still found, but a random
            # blob isn't mistaken for one.
            c = None
            for cand in sorted(contours, key=cv2.contourArea, reverse=True):
                area = cv2.contourArea(cand)
                if area < 0.025 * w * h:
                    break
                (_, _), (rw, rh), _ = cv2.minAreaRect(cand)
                if rw * rh <= 0:
                    continue
                aspect = min(rw, rh) / max(rw, rh)
                if 0.6 <= aspect <= 0.85 and area / (rw * rh) > 0.75:
                    c = cand
                    break
            if c is None:
                return None
        else:
            c = max(contours, key=cv2.contourArea)
            if cv2.contourArea(c) < 0.05 * w * h:
                return None
        if background_bgr is not None and cv2.contourArea(c) > 0.85 * w * h:
            # Nearly the whole box "changed": the light changed, not just a card
            # arriving. Find the card by its edges instead.
            return Recognizer.card_quad(zone_bgr, None)
        box = order_corners(cv2.boxPoints(cv2.minAreaRect(c)))
        tl, tr, br, bl = box
        if np.linalg.norm(tr - tl) > np.linalg.norm(bl - tl):  # lying sideways -> make it portrait
            box = np.array([tr, br, bl, tl], dtype=np.float32)
        return box

    @staticmethod
    def find_card_checked(zone_bgr, background_bgr):
        """Like find_card, but only returns a card when there is convincing evidence a
        real one is there, so an empty playmat (with card-like shapes in its artwork)
        never reaches recognition. Returns (card or None, reason)."""
        if background_bgr is None or background_bgr.shape != zone_bgr.shape:
            card = Recognizer.find_card(zone_bgr, None)
            return card, ("edges" if card is not None else "no card shape")
        if looks_empty(zone_bgr, background_bgr):
            return None, "matches background"
        h, w = zone_bgr.shape[:2]
        for box in (Recognizer.card_quad(zone_bgr, background_bgr), Recognizer.card_quad(zone_bgr, None)):
            if box is None:
                continue
            tl, tr, br, bl = box
            side_w, side_h = np.linalg.norm(tr - tl), np.linalg.norm(bl - tl)
            if side_w * side_h < MIN_CARD_FRAC * w * h:
                continue
            if not CARD_ASPECT[0] <= min(side_w, side_h) / max(side_w, side_h, 1e-3) <= CARD_ASPECT[1]:
                continue
            card = warp_card(zone_bgr, box)
            under = warp_card(background_bgr, box)
            if (background_similarity(card, under) >= REGION_SIMILARITY
                    and changed_fraction(card, under) < EMPTY_CHANGED_FRAC):
                continue  # that "card" is part of the background picture (e.g. playmat art)
            return card, "ok"
        # A card (often on top of a stack) that fills the whole box has no outline inside
        # it to find. Accept the box itself, but only when most of it is clearly new
        # compared with the learned background - an empty playmat can never pass this.
        if (changed_fraction(zone_bgr, background_bgr) >= FILLS_BOX_CHANGED_FRAC
                and background_similarity(zone_bgr, background_bgr) < REGION_SIMILARITY):
            return cv2.resize(zone_bgr, (CARD_W, CARD_H), interpolation=cv2.INTER_AREA), "fills box"
        return None, "no card shape"

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

    def picture_first(self, card_bgr, locked_set=None):
        """Fast path: if the picture model clearly knows the card, skip text reading
        entirely and take the printing from the matched artwork (no downloads)."""
        sure_gap = getattr(self.visual, "SURE_GAP", VIS_GAP)
        for img in (card_bgr, cv2.rotate(card_bgr, cv2.ROTATE_180)):
            vis = self.visual.query(img, k=5)
            if len(vis) > 1 and vis[0][2] - vis[1][2] >= sure_gap:
                name, art_id = vis[0][0], vis[0][1]
                # Reprints share artwork; on a close card the set code / number in the
                # bottom-left corner tells them apart (~20 ms; junk from far away is ignored).
                footer = " ".join(self.read_line(img, b, height=40) for b in FOOTER_LINES)
                return dict(name_text="", footer_text=footer, candidates=[(n, 100 * max(0.0, sc)) for n, _, sc in vis],
                            printings=self.db.printings_for_art(name, art_id, locked_set, footer, img),
                            confident=True, card=img, how="picture")
        return None

    def identify(self, card_bgr, locked_set=None):
        """Returns dict(name_text, footer_text, candidates=[(name, score)],
        printings=[most likely first], confident, card=straightened image, how)."""
        if self.visual is not None:
            fast = self.picture_first(card_bgr, locked_set)
            if fast and fast["printings"]:
                return fast
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
        vis_sure = bool(vis) and vis_gap >= getattr(self.visual, "SURE_GAP", VIS_GAP)

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
            elif vis_sure and best["top"] < 97:
                # A clear picture match beats a shaky name read (e.g. "Natural Spring" read as "Slay").
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


def warp_card(img, box):
    dst = np.array([[0, 0], [CARD_W - 1, 0], [CARD_W - 1, CARD_H - 1], [0, CARD_H - 1]], dtype=np.float32)
    return cv2.warpPerspective(img, cv2.getPerspectiveTransform(np.asarray(box, np.float32), dst), (CARD_W, CARD_H))


def order_corners(pts):
    pts = np.asarray(pts, dtype=np.float32)
    s, d = pts.sum(axis=1), np.diff(pts, axis=1).ravel()
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]], dtype=np.float32)
