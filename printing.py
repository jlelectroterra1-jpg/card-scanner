"""Stage 2 of recognition: WHICH PRINTING is this card? (Stage 1 - which card - is
recognizer.py.) Once the card is known, choosing the printing is a small, constrained
artwork match:

1. Take every printing of that card (English ones, unless there are none).
2. Score each printing's clean Scryfall picture against the photo with the trained card
   model (visual.CardModelIndex) - the same model that recognised the card, which was
   trained on webcam-like shots, so it copes with distance, blur, sleeves and glare.
3. Group printings that look the same from a webcam (same art + same frame: their clean
   pictures are near-identical to the model). The best-matching group is the artwork.
4. Inside that group the art is the same, so the printing is decided by secondary
   evidence: the set code / collector number read from the bottom-left corner, a locked
   set, or a clearly closer frame/border/colour match (printmatch.signature). Without
   such evidence the printing is reported as uncertain - and the cheapest regular
   printing is used, so an uncertain scan never inflates your value.

Release date, price, promo status etc. only order printings that look identical; they
can never beat a clearly better-matching artwork.

choose() returns the printings best-first plus a `PrintingChoice` explaining the result:
art confidence (is it this artwork?), set confidence (which of the identical-looking
printings?), overall printing confidence, a plain-English reason and the top candidates
with their visual similarity (written to the scan debug log)."""
import os
import re
import threading

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
IMG_DIR = os.path.join(HERE, "data", "img")

SAME_LOOK = 0.80        # clean-vs-clean similarity: these printings look the same (same art + frame)
ART_SURE_MARGIN = 0.10  # lead of the best artwork over the next different one for full art confidence
MIN_ART_SIM = 0.20      # below this the photo doesn't really resemble even the best artwork
PRINTING_SURE = 0.70    # printing confidence needed to present the printing as certain
FRAME_RATIO = 1.05      # inside a same-art group: the closest frame/colour match must beat the next by this
                        # factor to count as evidence (close-up tests: right 55/56; far shots at 1.1: 17/18)
MODEL_LEAD = 0.015      # ...or the model's similarity must lead the next same-art printing by this (55/55)
SPECIAL_SETS = {"plst", "sld", "slz", "sld2", "sldc"}  # The List / Secret Lair: never the default guess


class PrintingChoice(dict):
    """dict(confidence, art_confidence, set_confidence, uncertain, reason, candidates=[...])."""


def _footer_tokens(footer_text):
    tokens = set(re.findall(r"[a-z0-9]+", (footer_text or "").lower()))
    return tokens, {t.lstrip("0") for t in tokens if t.isdigit()}


class PrintingMatcher:
    def __init__(self, db, model=None):
        self.db, self.model = db, model
        self.can_embed = model is not None and hasattr(model, "embed") and hasattr(model, "feats")
        self._row = {str(i): k for k, i in enumerate(model.ids)} if self.can_embed else {}
        self._cache = {}
        self._lock = threading.Lock()

    # ---- embeddings ----------------------------------------------------------------

    def clean_embedding(self, card_id):
        """The model's fingerprint of a printing's clean Scryfall picture (None if we have
        no picture). Artworks in the recognition index are looked up; others computed once."""
        if card_id in self._cache:
            return self._cache[card_id]
        k = self._row.get(card_id)
        if k is not None:
            e = self.model.feats[k].astype(np.float32)
        else:
            img = cv2.imread(os.path.join(IMG_DIR, card_id + ".jpg"))
            e = None if img is None else self.model.embed(img).astype(np.float32)
        with self._lock:
            self._cache[card_id] = e
        return e

    # ---- the choice ---------------------------------------------------------------------

    def candidates(self, name, locked_set=None):
        prints = self.db.printings(name)
        if locked_set:
            in_set = [p for p in prints if p["set_code"] == locked_set]
            prints = in_set or prints
        english = [p for p in prints if p["lang"] == "en"]
        return english or prints

    @staticmethod
    def _default_order(p, footer_score):
        """Order inside a group of identical-looking printings: footer evidence, then a
        regular printing (not a promo / The List / Secret Lair), then the cheapest (an
        uncertain guess shouldn't inflate value), then newest."""
        price = float(p["usd"]) if p.get("usd") not in (None, "") else 1e9
        regular = not p.get("promo") and p["set_code"] not in SPECIAL_SETS
        return (-footer_score(p), not regular, price, -int((p.get("released_at") or "0").replace("-", "") or 0))

    def choose(self, name, card_img=None, footer_text="", locked_set=None, capture_emb=None):
        """Returns (printings best-first, PrintingChoice)."""
        prints = self.candidates(name, locked_set)
        if not prints:
            return [], PrintingChoice(confidence=0.0, art_confidence=0.0, set_confidence=0.0, uncertain=True,
                                      reason="no printings found", candidates=[])
        tokens, numbers = _footer_tokens(footer_text)

        def footer_score(p):
            s = 0
            if p["set_code"] in tokens:
                s += 4
            if str(p["collector_number"]).lower().lstrip("0") in numbers:
                s += 3
            return s

        if len(prints) == 1:
            return prints, PrintingChoice(confidence=1.0, art_confidence=1.0, set_confidence=1.0, uncertain=False,
                                          reason="only one printing", candidates=self._debug(prints, {}, {}))
        if not self.can_embed or card_img is None:
            # No picture model (or no photo, e.g. a typed name): order by the text read from
            # the card, then - if there is a photo - the rough colour/frame match; never certain
            # unless the set code and number were read.
            ordered = sorted(prints, key=lambda p: self._default_order(p, footer_score))
            if card_img is not None:
                from printmatch import rank_printings
                dist = {p["id"]: p["dist"] for p in rank_printings(card_img, prints, download=False)}
                ordered.sort(key=lambda p: (-footer_score(p), dist.get(p["id"], 9.0)))
            known = footer_score(ordered[0]) >= 7 and footer_score(ordered[1]) < 7
            conf = 0.9 if known else 0.3
            return ordered, PrintingChoice(confidence=conf, art_confidence=0.0, set_confidence=conf,
                                           uncertain=conf < PRINTING_SURE, candidates=self._debug(ordered, {}, {}),
                                           reason="set/number read from the card" if known else "no picture match "
                                                  "available - printing guessed")
        cap = capture_emb if capture_emb is not None else self.model.embed(card_img).astype(np.float32)
        emb = {p["id"]: self.clean_embedding(p["id"]) for p in prints}
        sim = {pid: (float(cap @ e) if e is not None else None) for pid, e in emb.items()}
        # Group identical-looking printings, best-matching first.
        order = sorted(prints, key=lambda p: -(sim[p["id"]] if sim[p["id"]] is not None else -9))
        groups = []
        for p in order:
            e = emb[p["id"]]
            if e is None:
                groups.append([p])  # no picture: its own group, ranked last
                continue
            for g in groups:
                lead = emb[g[0]["id"]]
                if lead is not None and float(lead @ e) >= SAME_LOOK:
                    g.append(p)
                    break
            else:
                groups.append([p])
        gscore = [max((sim[p["id"]] for p in g if sim[p["id"]] is not None), default=-9) for g in groups]
        group_of = {p["id"]: gi for gi, g in enumerate(groups) for p in g}
        best = groups[0]
        s1 = gscore[0]
        s2 = gscore[1] if len(groups) > 1 else None
        margin = (s1 - s2) if s2 is not None and s2 > -9 else 1.0
        art_conf = max(0.0, min(1.0, margin / ART_SURE_MARGIN)) * max(0.0, min(1.0, s1 / MIN_ART_SIM))
        # Which of the identical-looking printings? Secondary evidence only.
        best.sort(key=lambda p: self._default_order(p, footer_score))
        f1 = footer_score(best[0])
        f2 = footer_score(best[1]) if len(best) > 1 else -1
        frame = self._frame_match(card_img, best) if len(best) > 1 and not (f1 >= 3 and f1 > f2) else None
        frame_pick = frame[0] if frame and frame[1] >= FRAME_RATIO else None
        model_pick = None
        if len(best) > 1:
            ranked = sorted((p for p in best if sim[p["id"]] is not None), key=lambda p: -sim[p["id"]])
            if len(ranked) > 1 and sim[ranked[0]["id"]] - sim[ranked[1]["id"]] >= MODEL_LEAD:
                model_pick = ranked[0]
        if len(best) == 1:
            set_conf, set_why = 1.0, "the only printing with this artwork"
        elif f1 >= 7 and f2 < 7:
            set_conf, set_why = 1.0, "set code and collector number read from the card"
        elif f1 > f2 and f1 >= 3:
            set_conf, set_why = 0.8, "set code or collector number read from the card"
        elif locked_set and all(p["set_code"] == locked_set for p in best):
            set_conf, set_why = 0.9, f"set locked to {locked_set.upper()}"
        elif (frame_pick or model_pick) and not (frame_pick and model_pick and frame_pick["id"] != model_pick["id"]):
            # secondary picture evidence for one of the same-art printings (frame, border, colours)
            pick = frame_pick or model_pick
            both = frame_pick is not None and model_pick is not None
            best.remove(pick)
            best.insert(0, pick)
            set_conf = 0.9 if both else (0.8 if frame_pick else 0.75)
            how = ("frame/border/colours and the picture model both point to"
                   if both else "frame/border/colours match best:" if frame_pick else "the picture model prefers")
            set_why = (f"{len(best)} printings share this artwork; {how} "
                       f"{pick['set_code'].upper()} #{pick['collector_number']}")
        else:
            set_conf = 0.5
            set_why = (f"{len(best)} printings share this artwork and frame - set/number unreadable, "
                       "picked the cheapest regular one")
        conf = art_conf * set_conf
        if art_conf >= 0.999:
            art_why = f"artwork clearly matches (similarity {s1:.2f}, next different artwork {s2:.2f})" \
                if s2 is not None and s2 > -9 else "every printing has this artwork"
        else:
            art_why = (f"artwork match not decisive (similarity {s1:.2f} vs {s2:.2f} for a different artwork)"
                       if s2 is not None else f"weak artwork match ({s1:.2f})")
        ordered = list(best)
        for g in groups[1:]:
            g.sort(key=lambda p: self._default_order(p, footer_score))
            ordered += g
        return ordered, PrintingChoice(
            confidence=round(conf, 3), art_confidence=round(art_conf, 3), set_confidence=set_conf,
            uncertain=conf < PRINTING_SURE, reason=f"{art_why}; {set_why}",
            candidates=self._debug(ordered[:10], sim, group_of))

    @staticmethod
    def _frame_match(card_img, group):
        """Among same-art printings: (closest printing by frame/colours, how many times closer
        it is than the next), or None. Uses only locally cached pictures."""
        if card_img is None:
            return None
        from printmatch import _fetch, signature
        sig = signature(card_img)
        dists = []
        for p in group:
            ref = _fetch(p, download=False)
            if ref is not None:
                dists.append((float(np.abs(sig - ref).mean()), p))
        if len(dists) < 2:
            return None
        dists.sort(key=lambda t: t[0])
        return dists[0][1], dists[1][0] / max(dists[0][0], 1e-6)

    @staticmethod
    def _debug(prints, sim, group_of):
        return [dict(id=p["id"], set=p["set_code"], number=str(p["collector_number"]),
                     visual=None if sim.get(p["id"]) is None else round(sim[p["id"]], 3),
                     group=group_of.get(p["id"]), usd=p.get("usd")) for p in prints]


def describe(name, choice, printings):
    """Readable debug lines for the console / log."""
    lines = [f"Card: {name}", "Printing candidates:"]
    for i, c in enumerate(choice.get("candidates", [])[:8], 1):
        vis = "-" if c["visual"] is None else f"{c['visual']:.2f}"
        lines.append(f"  {i}. {c['set'].upper()} #{c['number']} - visual {vis} (look-group {c['group']}) ${c['usd'] or '?'}")
    sel = printings[0] if printings else None
    lines.append(f"Selected: {sel['set_code'].upper()} #{sel['collector_number']}" if sel else "Selected: none")
    lines.append(f"Printing confidence {choice['confidence']:.0%} (art {choice['art_confidence']:.0%}, "
                 f"set {choice['set_confidence']:.0%}){' - UNCERTAIN' if choice['uncertain'] else ''}")
    lines.append(f"Reason: {choice['reason']}")
    return "\n".join(lines)
