"""The Analyse tab: our own Commander deck analyser (analyser.py) with deck selection,
settings, an overview, suggested swaps (with reasons, ownership, cost and confidence),
optional combo checks, apply-with-snapshot and undo. Analysis runs in the background on
its own database connection, so the window never freezes."""
import threading
import webbrowser

import analyser
import strategy
from currency import display_currency, money
from decks import DeckStore
from decks_view import Images, _wrap
from providers import CommanderSpellbookProvider, edhrec_url
from ui import (BG, BUTTON, BUTTON_HI, CARD_BG, GOLD, GREEN, MUTED, RED, TEXT, YELLOW, Dialog, Painter)
from userdb import UserDB, UserDBError

LEFT_W = 340
RX = 16 + LEFT_W + 16
CONF_COLOUR = {"Strong suggestion": GREEN, "Worth considering": YELLOW, "Situational": MUTED,
               "Depends on your intended strategy": MUTED}
OWN_BADGE = {"available": ("Owned & available", GREEN), "in_use": ("Owned - used in another deck", YELLOW),
             "not_owned": ("Not owned", RED)}
BUDGETS_ZAR = [0, 250, 500, 1000]
BUDGETS_USD = [0, 15, 30, 60]


class AnalyseScreen:
    def __init__(self, app, width, height, store=None):
        self.app = app
        self.W, self.H = width, height
        self.store = store or DeckStore(app.userdb)
        self.images = Images(self.bump)
        self.deck_id = None
        self.tab = "overview"
        self.result = self.analysis_id = None
        self.stale = False
        self.selected = set()
        self.scroll = 0
        self.open_dd = None
        self.running = None  # dict(progress, text) while analysing
        self.combo_state = None  # None / "checking" / error text
        self.version = 0
        self._key = self._cache = None
        self._print = {}
        self.decks = []
        self.refresh()

    # ---- state ------------------------------------------------------------------

    def bump(self):
        self.version += 1

    def refresh(self):
        self.decks = self.store.deck_list()
        if self.deck_id is None or self.deck_id not in {d["deck_id"] for d in self.decks}:
            self.select_deck(self.decks[0]["deck_id"] if self.decks else None)
        else:
            self.check_stale()
        self.bump()

    def select_deck(self, deck_id):
        self.deck_id, self.result, self.analysis_id = deck_id, None, None
        self.scroll, self.tab, self.combo_state = 0, "overview", None
        if deck_id is not None:
            saved = self.app.userdb.latest_analysis(deck_id)
            if saved:
                self.result, self.analysis_id = saved["result"], saved["id"]
                self.selected = set(range(len(self.result.get("swaps", []))))
            self.check_stale()
        self.bump()

    def profile(self):
        return self.app.userdb.analysis_profile(self.deck_id) if self.deck_id else None

    def settings(self):
        p = self.profile()
        return dict(mode=p["mode"], goal=p["goal"], tags=p["tags"],
                    budget_usd=None if p["mode"] == "collection" else p["budget_usd"])

    def check_stale(self):
        """The cached analysis is only shown as current while the deck, collection, settings
        and card data are unchanged."""
        if not self.result or self.deck_id is None:
            self.stale = False
            return
        saved = self.app.userdb.latest_analysis(self.deck_id)
        self.stale = not saved or saved["fingerprint"] != analyser.current_fingerprint(
            self.app.userdb, self.deck_id, self.settings())

    def deck(self):
        return next((d for d in self.decks if d["deck_id"] == self.deck_id), None)

    def money(self, usd):
        return money(usd, self.app.settings)

    def printing(self, name):
        if name not in self._print:
            c = self.store.default_printing(name)
            self._print[name] = (c["id"], c.get("image_small")) if c else (None, None)
        return self._print[name]

    # ---- running the analysis -------------------------------------------------------

    def run(self):
        if self.running or self.deck_id is None:
            return
        deck_id, settings, path = self.deck_id, self.settings(), self.app.userdb.path
        self.running = dict(progress=0.0, text="Starting...")

        def progress(p, text):
            self.running = dict(progress=p, text=text)
            self.bump()

        def work():
            db = None
            try:
                db = UserDB(path)  # own connection: the window keeps working meanwhile
                st = DeckStore(db)
                fp = analyser.current_fingerprint(db, deck_id, settings)
                res = analyser.analyse(db, st, deck_id, settings, progress=progress)
                aid = db.save_analysis(deck_id, fp, settings, res)
                self._done = (deck_id, res, aid, None)
            except Exception as e:  # noqa: BLE001
                self._done = (deck_id, None, None, str(e))
            finally:
                if db is not None:
                    db.close()
                self.bump()
        self._done = None
        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        done = getattr(self, "_done", None)
        if done is None:
            return
        self._done = None
        self.running = None
        deck_id, res, aid, err = done
        if err:
            self.app.toast(f"Analysis failed: {err}", RED)
        elif deck_id == self.deck_id:
            self.result, self.analysis_id, self.stale = res, aid, False
            self.selected = set(range(len(res["swaps"])))
            self.tab = "swaps" if res["swaps"] else "overview"
            self.scroll = 0
            self.app.toast(f"Analysed in {res['seconds']:.1f} s - {len(res['swaps'])} possible swaps", GREEN)
        self.bump()

    # ---- drawing ------------------------------------------------------------------------

    def render(self):
        self.poll()
        toast = self.app.toast_text()
        key = (self.version, self.tab, self.scroll, self.open_dd, toast, tuple(sorted(self.selected)),
               display_currency(self.app.settings), self.running and round(self.running["progress"], 2))
        if key == self._key and self._cache is not None:
            return self._cache
        p = Painter(self.W, self.H, BG)
        popup = self._left(p)
        self._right(p)
        if popup:
            popup(p)
        if toast:
            text, colour = toast
            w = p.f["body"].getlength(text) + 30
            x0 = RX + (self.W - RX - w) / 2
            p.d.rounded_rectangle((x0, self.H - 44, x0 + w, self.H - 12), radius=16, fill=(40, 42, 48))
            p.text((x0 + 15, self.H - 28), text, fill=colour, anchor="lm")
        self._key, self._cache = key, (p.to_bgr(), p.hits)
        return self._cache

    def _seg(self, p, x, y, w, options, current, action):
        bw = (w - 4 * (len(options) - 1)) / len(options)
        for i, (value, label) in enumerate(options):
            p.button((x + i * (bw + 4), y, x + i * (bw + 4) + bw, y + 30), label, (action, value),
                     style="selected" if value == current else "ghost", font="label")

    def _left(self, p):
        x0, x1 = 16, 16 + LEFT_W
        p.text((x0, 26), "Analyse Commander Deck", font="name", anchor="lm", fill=YELLOW)
        d = self.deck()
        if not self.decks:
            p.text((x0, 70), "Make a deck on the Decks tab first.", font="body", fill=MUTED)
            return None
        p.dropdown((x0, 46, x1, 80), d["name"] if d else "Select deck", "dd:deck", active=self.open_dd == "deck")
        y = 90
        p.paste(self.images.small(d.get("commander_scryfall_id"), d.get("commander_image")), x0, y, 80, 112, radius=6)
        tx = x0 + 92
        cmd = d["commander"] or "No commander yet"
        if d.get("partner"):
            cmd += " + " + d["partner"]
        for line in _wrap(p, cmd, "rowb", x1 - tx)[:2]:
            p.text((tx, y), line, font="rowb", fill=TEXT if d["commander"] else YELLOW)
            y += 19
        ident = (self.result or {}).get("identity") if self.result else None
        ident = ident if ident is not None else self.store.commander_identity(self.deck_id)
        self._pips(p, tx, y + 4, ident)
        p.text((tx, y + 34), f"{d['card_count']} cards - {self.money(d['value'])}", font="small", fill=MUTED)
        p.text((tx, y + 52), f"{d['owned']} owned, {d['missing']} missing", font="small", fill=MUTED)
        prof = self.profile()
        y = 214
        p.text((x0, y), "SUGGEST CARDS FROM", font="label", fill=MUTED)
        self._seg(p, x0, y + 16, LEFT_W, [("collection", "My Collection"), ("all", "All Cards")], prof["mode"], "mode")
        y += 56
        p.text((x0, y), "DECK GOAL", font="label", fill=MUTED)
        self._seg(p, x0, y + 16, LEFT_W, [("casual", "Casual"), ("improve", "Improve It"), ("high", "High Power")],
                  prof["goal"], "goal")
        y += 56
        p.text((x0, y), "STRATEGY" + ("  (your choice)" if prof["tags"] else "  (detected)"), font="label", fill=MUTED)
        tags = prof["tags"] or ((self.result or {}).get("detected") or {}).get("primary", []) + \
            ((self.result or {}).get("detected") or {}).get("secondary", [])[:2]
        cx = x0
        cy = y + 18
        for t in (tags or ["analyse to detect"])[:5]:
            w = p.f["label"].getlength(t) + 14
            if cx + w > x1 - 60:
                break
            cx = p.chip(cx, cy, t, YELLOW if prof["tags"] else TEXT)
        p.button((x1 - 52, cy - 2, x1, cy + 22), "Edit", "tags", font="label")
        y += 50
        if prof["mode"] == "all":
            cur, _ = display_currency(self.app.settings)
            p.text((x0, y), "UPGRADE BUDGET (TOTAL)", font="label", fill=MUTED)
            b = prof["budget_usd"]
            rate = self.app.settings.get("usd_zar") or 0
            opts = []
            presets = BUDGETS_ZAR if cur == "ZAR" else BUDGETS_USD
            for amt in presets:
                usd = amt / rate if cur == "ZAR" else amt
                opts.append((round(usd, 4), ("R" if cur == "ZAR" else "$") + f"{amt:,}"))
            opts.append((None, "No limit"))
            current = None if b is None else round(b, 4)
            custom = b is not None and all(abs((o[0] or -1) - b) > 0.01 for o in opts if o[0] is not None)
            bw = (LEFT_W - 4 * 5) / 6
            for i, (v, label) in enumerate(opts + [("custom", "Other")]):
                on = (v == "custom" and custom) or (v != "custom" and not custom and v == current)
                p.button((x0 + i * (bw + 4), y + 16, x0 + i * (bw + 4) + bw, y + 44),
                         self.money(b) if (v == "custom" and custom) else label, ("budget", v),
                         style="selected" if on else "ghost", font="label")
            y += 54
        else:
            p.text((x0, y + 4), "Only free cards you own - cost R0 / $0", font="small", fill=MUTED)
            y += 30
        # the big button
        by = max(y + 6, self.H - 120)
        if self.running:
            r = self.running
            p.d.rounded_rectangle((x0, by, x1, by + 44), radius=10, fill=BUTTON)
            p.d.rounded_rectangle((x0, by, x0 + max(20, LEFT_W * r["progress"]), by + 44), radius=10, fill=(52, 120, 76))
            p.text(((x0 + x1) / 2, by + 22), r["text"], font="button", anchor="mm")
        else:
            p.button((x0, by, x1, by + 44), "Analyse Deck", "run", style="primary", font="name")
        by += 52
        third = (LEFT_W - 8) / 3
        p.button((x0, by, x0 + third, by + 30), "EDHREC (browser)", "edhrec", font="label")
        p.button((x0 + third + 4, by, x0 + 2 * third + 4, by + 30), "Check combos", "combos", font="label")
        can_undo = self.app.userdb.last_applied(self.deck_id) is not None
        p.button((x0 + 2 * third + 8, by, x1, by + 30), "Undo last change", "undo" if can_undo else None,
                 style="normal" if can_undo else "disabled", font="label")
        if self.open_dd == "deck":
            items = [(dd["deck_id"], dd["name"]) for dd in self.decks]
            return lambda pp: pp.popup_list(x0, 82, LEFT_W, items, "pick_deck", self.deck_id, max_rows=12)
        return None

    def _pips(self, p, x, y, identity):
        from decks_view import PIP
        if not identity:
            p.d.ellipse((x, y, x + 18, y + 18), fill=(170, 170, 175))
            p.text((x + 9, y + 9), "C", font="label", fill=(30, 30, 30), anchor="mm")
            return
        for ch in identity:
            p.d.ellipse((x, y, x + 18, y + 18), fill=PIP.get(ch, MUTED))
            p.text((x + 9, y + 9), ch, font="label", fill=(30, 30, 30), anchor="mm")
            x += 22

    def _right(self, p):
        x0, x1 = RX, self.W - 16
        res = self.result
        n = len(res["swaps"]) if res else 0
        tabs = [("overview", "Overview"), ("swaps", f"Swaps ({n})" if res else "Swaps"), ("combos", "Combos")]
        tx = x0
        for key, label in tabs:
            w = p.f["tab"].getlength(label) + 30
            if key == self.tab:
                p.d.rounded_rectangle((tx, 8, tx + w, 42), radius=8, fill=BUTTON_HI)
            p.text((tx + w / 2, 25), label, font="tab", anchor="mm", fill=TEXT if key == self.tab else MUTED)
            p.hit((tx, 8, tx + w, 42), ("tab", key))
            tx += w + 4
        if res:
            p.text((x1, 25), f"analysed {res['created']} ({'My Collection' if res['settings']['mode'] == 'collection' else 'All Cards'}, "
                             f"{analyser.GOALS[res['settings']['goal']]})", font="small", fill=MUTED, anchor="rm")
        top = 52
        if res and self.stale:
            p.d.rounded_rectangle((x0, top, x1, top + 32), radius=8, fill=(52, 46, 30))
            p.text((x0 + 12, top + 16), "The deck, your collection or the settings changed since this analysis - "
                                        "press Analyse Deck for fresh suggestions", font="small", fill=YELLOW, anchor="lm")
            top += 40
        if not res:
            p.text(((x0 + x1) / 2, self.H / 2 - 20), "Choose the settings on the left and press Analyse Deck",
                   font="name", anchor="mm")
            p.text(((x0 + x1) / 2, self.H / 2 + 12), "Nothing in your deck changes unless you apply a suggestion - "
                                                     "and you can always undo it.", font="body", fill=MUTED, anchor="mm")
            return
        if self.tab == "overview":
            self._overview(p, x0, x1, top)
        elif self.tab == "swaps":
            self._swaps(p, x0, x1, top)
        else:
            self._combos(p, x0, x1, top)

    def _overview(self, p, x0, x1, top):
        res = self.result
        comp = res["composition"]
        goal = res["settings"]["goal"]
        mid = x0 + (x1 - x0) * 0.48
        y = top
        p.text((x0, y), "DECK COMPOSITION", font="label", fill=MUTED)
        y += 20
        rows = [("Lands", comp["lands"], analyser.GUIDELINES["Lands"][goal],
                 f"{comp['basics']} basic, {comp['nonbasics']} other")]
        for role, have in comp["core"].items():
            if role == "Lands":
                continue
            rng = analyser.GUIDELINES[role][goal]
            rows.append((role, have, rng, ""))
        for role, have, rng, note in rows:
            colour = GREEN if rng[0] <= have <= rng[1] else YELLOW
            p.text((x0, y), role, font="row")
            p.text((x0 + 150, y), str(have), font="rowb", fill=colour)
            bar_x, bar_w = x0 + 180, mid - x0 - 300
            p.d.rounded_rectangle((bar_x, y + 5, bar_x + bar_w, y + 13), radius=4, fill=BUTTON)
            full = max(rng[1] * 1.3, have, 1)
            lo, hi = bar_x + bar_w * rng[0] / full, bar_x + bar_w * rng[1] / full
            p.d.rectangle((lo, y + 5, hi, y + 13), fill=(60, 80, 66))
            p.d.rounded_rectangle((bar_x, y + 5, bar_x + max(3, bar_w * min(have, full) / full), y + 13), radius=4,
                                  fill=colour)
            p.text((mid - 110, y), f"typical {rng[0]}-{rng[1]}", font="small", fill=MUTED)
            y += 21
        others = [(r, n) for r, n in sorted(comp["roles"].items(), key=lambda kv: -kv[1])
                  if r not in comp["core"] and r not in ("Lands",)][:8]
        if others:
            y += 4
            p.text((x0, y), "Also: " + ", ".join(f"{r} {n}" for r, n in others), font="small", fill=MUTED,
                   width=mid - x0 - 16)
            y += 18
        p.text((x0, y + 4), "(a card can count for several jobs)", font="small", fill=MUTED)
        # right column: curve, strategy, weaknesses
        rx = mid + 10
        y = top
        p.text((rx, y), f"MANA CURVE  -  average {comp['avg_mv']:.2f} (non-land)", font="label", fill=MUTED)
        y += 20
        peak = max(comp["curve"].values()) or 1
        cw = (x1 - rx) / 7
        for mv in range(7):
            n = comp["curve"].get(str(mv), comp["curve"].get(mv, 0))
            h = 60 * n / peak
            bx = rx + mv * cw
            p.d.rounded_rectangle((bx + 6, y + 78 - h, bx + cw - 6, y + 78), radius=3, fill=(88, 130, 200))
            p.text((bx + cw / 2, y + 70 - h), str(n), font="label", anchor="md")
            p.text((bx + cw / 2, y + 92), f"{mv}{'+' if mv == 6 else ''}", font="small", fill=MUTED, anchor="mm")
        y += 104
        if comp["pips"]:
            p.text((rx, y), "Colour symbols: " + "  ".join(f"{c} {v:g}" for c, v in comp["pips"].items()), font="small",
                   fill=MUTED)
            y += 22
        det = res["detected"]
        p.text((rx, y), "STRATEGY" + (" (your choice)" if res["user_tags"] else " (detected - edit on the left)"),
               font="label", fill=MUTED)
        y += 18
        prim = res["active_themes"][:2] if res["user_tags"] else det["primary"]
        sec = [] if res["user_tags"] else det["secondary"]
        p.text((rx, y), "Primary: " + (", ".join(prim) or "nothing clear"), font="rowb", fill=YELLOW)
        y += 19
        if sec:
            p.text((rx, y), "Secondary: " + ", ".join(sec), font="row")
            y += 19
        if det.get("tribe"):
            p.text((rx, y), f"Creature type: {det['tribe']}", font="row")
            y += 19
        why = (det["reasons"].get(prim[0]) if prim else None) or []
        for w in why[:2]:
            p.text((rx, y), "- " + w, font="small", fill=MUTED, width=x1 - rx)
            y += 17
        y += 8
        p.text((rx, y), "POTENTIAL WEAKNESSES", font="label", fill=MUTED)
        y += 18
        if not res["weaknesses"]:
            p.text((rx, y), "Nothing stands out against the usual guidelines", font="small", fill=GREEN)
        for w in res["weaknesses"]:
            if y > self.H - 30:
                break
            for line in _wrap(p, w["text"], "small", x1 - rx - 14)[:2]:
                p.text((rx + 12, y), line, font="small")
                y += 16
            p.text((rx, y - 16 * min(2, len(_wrap(p, w["text"], "small", x1 - rx - 14)))), "!", font="rowb",
                   fill=YELLOW)
            y += 4

    def _swaps(self, p, x0, x1, top):
        res = self.result
        swaps = res["swaps"]
        cost = sum((s["ownership"].get("cost") or 0) for i, s in enumerate(swaps) if i in self.selected)
        head = f"Recommended changes: {len(swaps)}   Selected: {len(self.selected)}   Cost: {self.money(cost)}"
        if res["settings"]["mode"] == "all" and res.get("budget") is not None:
            head += f" (budget {self.money(res['budget'])})"
        p.text((x0, top + 8), head, font="rowb", anchor="lm")
        if res["improvements"]:
            p.text((x0, top + 28), "Expected: " + ", ".join(res["improvements"]), font="small", fill=GREEN,
                   width=x1 - x0 - 250, anchor="lm")
        n_sel = len(self.selected)
        p.button((x1 - 236, top - 2, x1, top + 30), f"Apply selected changes ({n_sel})", "apply" if n_sel else None,
                 style="primary" if n_sel else "disabled")
        y0 = top + 42
        if not swaps:
            msg = ("No swaps found - your free owned cards don't beat what's in the deck. Try All Cards, or add cards "
                   "to your Collection." if res["settings"]["mode"] == "collection" else
                   "No swaps found within these settings - try a bigger budget or another deck goal.")
            p.text((x0, y0 + 20), msg, font="body", fill=MUTED, width=x1 - x0)
        card_h = 128
        vis = max(1, (self.H - y0 - 34) // card_h)
        self.scroll = max(0, min(self.scroll, max(0, len(swaps) - vis)))
        y = y0
        for i in range(self.scroll, min(len(swaps), self.scroll + vis)):
            s = swaps[i]
            on = i in self.selected
            p.d.rounded_rectangle((x0, y, x1, y + card_h - 8), radius=10, fill=CARD_BG,
                                  outline=GREEN if on else None)
            p.button((x0 + 10, y + 10, x0 + 34, y + 34), "✓" if on else "", ("toggle", i),
                     style="selected" if on else "ghost", font="label")
            oid, oimg = self.printing(s["out"])
            iid, iimg = self.printing(s["into"])
            p.paste(self.images.small(oid, oimg), x0 + 44, y + 10, 66, 92, radius=4)
            p.text((x0 + 120, y + 12), "OUT", font="label", fill=RED)
            for k, line in enumerate(_wrap(p, s["out"], "rowb", 150)[:2]):
                p.text((x0 + 120, y + 28 + k * 18), line, font="rowb")
            p.text((x0 + 120, y + 66), ", ".join(s["out_roles"][:2]) or "no clear job found", font="small",
                   fill=MUTED, width=150)
            p.text((x0 + 284, y + 50), "->", font="title", fill=MUTED, anchor="mm")
            p.paste(self.images.small(iid, iimg), x0 + 306, y + 10, 66, 92, radius=4)
            p.text((x0 + 382, y + 12), "IN", font="label", fill=GREEN)
            for k, line in enumerate(_wrap(p, s["into"], "rowb", 170)[:2]):
                p.text((x0 + 382, y + 28 + k * 18), line, font="rowb")
            badge, colour = OWN_BADGE[s["ownership"]["status"]]
            p.text((x0 + 382, y + 66), badge if s["ownership"]["status"] != "in_use" else s["ownership"]["label"],
                   font="small", fill=colour, width=180)
            price = s["ownership"].get("cost")
            p.text((x0 + 382, y + 84), "Cost: " + (self.money(0) if s["ownership"]["status"] == "available" else
                                                   (self.money(price) if price is not None else "price unknown")),
                   font="small", fill=MUTED)
            rx = x0 + 572
            p.chip(rx, y + 8, s["confidence"], CONF_COLOUR.get(s["confidence"], MUTED))
            ly = y + 34
            for line in (["+ " + t for t in s["why_in"][:3]] + ["- " + t for t in s["why_out"][:1]]):
                for wl in _wrap(p, line, "small", x1 - rx - 14)[:1]:
                    p.text((rx, ly), wl, font="small", fill=TEXT if line.startswith("+") else MUTED)
                    ly += 17
            p.hit((x0, y, x0 + 40, y + card_h - 8), ("toggle", i))
            y += card_h
        bottom = self.H - 22
        if len(swaps) > vis:
            p.text((x0, bottom), f"{self.scroll + 1}-{min(len(swaps), self.scroll + vis)} of {len(swaps)} "
                                 "(scroll for more)", font="small", fill=MUTED, anchor="lm")
        if res.get("alternatives"):
            alt = res["alternatives"][0]
            p.text((x1, bottom), f"Also worth a look: {alt['name']} - owned, but used in {', '.join(alt['used_in'][:2])}",
                   font="small", fill=YELLOW, anchor="rm")

    def _combos(self, p, x0, x1, top):
        combos = (self.result or {}).get("combos") or []
        y = top + 4
        p.text((x0, y), "COMBOS  -  optional, from Commander Spellbook (commanderspellbook.com)", font="label",
               fill=MUTED)
        p.hit((x0, y, x1, y + 16), "spellbook_site")
        y += 24
        if self.combo_state == "checking":
            p.text((x0, y), "Asking Commander Spellbook...", font="body", fill=YELLOW)
            return
        if isinstance(self.combo_state, str):
            p.text((x0, y), self.combo_state, font="body", fill=RED, width=x1 - x0)
            y += 26
        if not combos:
            p.text((x0, y), "Press Check combos on the left. It sends this deck's card list to Commander Spellbook "
                            "once (nothing else) and shows combos in the deck or one card away.", font="small",
                   fill=MUTED, width=x1 - x0)
            return
        for c in combos:
            if y > self.H - 40:
                p.text((x0, y), f"... {len(combos)} combos in total", font="small", fill=MUTED)
                break
            if c.get("error"):
                p.text((x0, y), c["error"], font="small", fill=RED)
                y += 20
                continue
            head = ("In your deck" if c["status"] == "in_deck" else "One card away")
            p.text((x0, y), head, font="label", fill=GREEN if c["status"] == "in_deck" else YELLOW)
            p.text((x0 + 110, y - 2), c["name"], font="rowb", width=x1 - x0 - 120)
            y += 18
            extra = "; ".join(c.get("produces") or [])
            if c.get("missing"):
                m = c["missing"][0]
                own = c.get("missing_ownership", {}).get(m, {})
                extra = f"Missing: {m} - {own.get('label', '?')}" + (f"  ({extra})" if extra else "")
            p.text((x0 + 110, y), extra, font="small", fill=MUTED, width=x1 - x0 - 120)
            p.hit((x0, y - 18, x1, y + 16), ("combo", c["url"]))
            y += 24

    # ---- input --------------------------------------------------------------------------

    def click(self, x, y, hits):
        action = None
        for x0, y0, x1, y1, a in reversed(hits):
            if x0 <= x <= x1 and y0 <= y <= y1:
                action = a
                break
        was = self.open_dd
        self.open_dd = None
        if action is None:
            self.bump()
            return
        udb = self.app.userdb
        if action == "dd:deck":
            self.open_dd = None if was == "deck" else "deck"
        elif isinstance(action, tuple):
            kind, v = action
            if kind == "pick_deck":
                self.select_deck(v)
            elif kind == "tab":
                self.tab, self.scroll = v, 0
            elif kind == "mode":
                udb.save_analysis_profile(self.deck_id, mode=v)
                self.check_stale()
            elif kind == "goal":
                udb.save_analysis_profile(self.deck_id, goal=v)
                self.check_stale()
            elif kind == "budget":
                if v == "custom":
                    return self.custom_budget()
                udb.save_analysis_profile(self.deck_id, budget_usd=v)
                self.check_stale()
            elif kind == "toggle":
                self.selected ^= {v}
            elif kind == "combo":
                webbrowser.open(v)
        elif action == "run":
            self.run()
        elif action == "tags":
            self.app.open_dialog(TagDialog(self))
        elif action == "edhrec":
            d = self.deck()
            names = [n for n in (d["commander"], d.get("partner")) if n]
            webbrowser.open(edhrec_url(names))
            self.app.toast("Opened EDHREC in your browser (for reference only)", GREEN)
        elif action == "spellbook_site":
            webbrowser.open("https://commanderspellbook.com")
        elif action == "combos":
            self.check_combos()
        elif action == "apply":
            self.confirm_apply()
        elif action == "undo":
            self.confirm_undo()
        self.bump()

    def wheel(self, delta):
        if self.tab == "swaps":
            self.scroll = max(0, self.scroll + (-1 if delta > 0 else 1))
            self.bump()

    def key(self, key):
        if key == 27 and self.open_dd:
            self.open_dd = None
        elif key in (0x210000, 0x260000):
            self.wheel(1)
        elif key in (0x220000, 0x280000):
            self.wheel(-1)
        else:
            return False
        self.bump()
        return True

    # ---- actions --------------------------------------------------------------------------

    def custom_budget(self):
        cur, rate = display_currency(self.app.settings)

        def done(ok, text):
            if not ok:
                return
            try:
                amount = float(text.replace(",", ".").strip().lstrip("R$"))
            except ValueError:
                return self.app.toast("That isn't an amount", RED)
            self.app.userdb.save_analysis_profile(self.deck_id, budget_usd=amount / rate if cur == "ZAR" else amount)
            self.check_stale()
            self.bump()
        self.app.open_dialog(Dialog("Upgrade budget", [f"Total you'd spend on the whole package, in {cur}."],
                                    kind="input", numeric=True, buttons=[("Cancel", None, "normal"),
                                                                         ("Save", True, "primary")], on_done=done))

    def check_combos(self):
        if self.result is None:
            return self.app.toast("Analyse the deck first", YELLOW)
        if self.combo_state == "checking":
            return
        self.combo_state, self.tab = "checking", "combos"
        rows = self.store.deck_cards(self.deck_id)
        commanders = [r["card_name"] for r in rows if r["role"] in ("commander", "partner")]
        cards = [r["card_name"] for r in rows if r["role"] == "main"]
        path, deck_id = self.app.userdb.path, self.deck_id

        def work():
            db = None
            try:
                combos = CommanderSpellbookProvider().combos(commanders, cards)
                db = UserDB(path)
                analyser.annotate_combos(db, deck_id, combos)
                self.result["combos"] = combos
                self.combo_state = None
            except Exception as e:  # noqa: BLE001
                self.combo_state = f"Couldn't reach Commander Spellbook: {e}"
            finally:
                if db is not None:
                    db.close()
                self.bump()
        threading.Thread(target=work, daemon=True).start()

    def confirm_apply(self):
        chosen = [s for i, s in enumerate(self.result["swaps"]) if i in self.selected]
        if not chosen:
            return
        lines = ["OUT: " + ", ".join(s["out"] for s in chosen), "IN: " + ", ".join(s["into"] for s in chosen),
                 "A snapshot of the deck is saved first - Undo last change puts it back."]
        buy = [s["into"] for s in chosen if s["ownership"]["status"] != "available"]
        if buy:
            lines.append(f"{len(buy)} of these you don't have a free copy of - they go in the deck as 'not owned'.")

        def done(ok, _t):
            if not ok:
                return
            try:
                analyser.apply_swaps(self.app.userdb, self.store, self.deck_id, chosen, self.analysis_id)
            except (UserDBError, ValueError) as e:
                return self.app.toast(f"Nothing changed: {e}", RED)
            self.app.toast(f"Applied {len(chosen)} changes to {self.deck()['name']}", GREEN)
            self.after_change()
        self.app.open_dialog(Dialog(f"Apply {len(chosen)} changes?", lines,
                                    buttons=[("Cancel", None, "normal"), ("Apply", True, "primary")], on_done=done))

    def confirm_undo(self):
        last = self.app.userdb.last_applied(self.deck_id)
        if not last:
            return

        def done(ok, _t):
            if ok:
                swaps = analyser.undo_last(self.app.userdb, self.store, self.deck_id)
                self.app.toast(f"Undid {len(swaps or [])} changes - the deck is back as it was", GREEN)
                self.after_change()
        self.app.open_dialog(Dialog("Undo the last applied changes?",
                                    ["Restores the deck exactly as it was before them: " +
                                     ", ".join(f"{s['into']} -> {s['out']}" for s in last["swaps"][:6]) +
                                     (" ..." if len(last["swaps"]) > 6 else ""),
                                     "Edits made to the deck since then are undone too."],
                                    buttons=[("Cancel", None, "normal"), ("Undo", True, "primary")], on_done=done))

    def after_change(self):
        self.decks = self.store.deck_list()
        self.check_stale()
        if getattr(self.app, "decks_screen", None) is not None:
            self.app.decks_screen.refresh()
        if getattr(self.app, "collection_screen", None) is not None:
            self.app.collection_screen.refresh()
        self.bump()


class TagDialog(Dialog):
    """Pick the deck's goals (strategy tags) - or go back to the detected ones."""

    def __init__(self, screen):
        super().__init__("What is this deck trying to do?")
        self.screen = screen
        prof = screen.profile()
        det = ((screen.result or {}).get("detected") or {})
        self.tags = set(prof["tags"] or det.get("primary", []))
        self.detected = det.get("primary", []) + det.get("secondary", [])

    def draw(self, canvas_bgr):
        import cv2
        import numpy as np
        from PIL import Image, ImageDraw
        H, W = canvas_bgr.shape[:2]
        p = Painter(W, H)
        p.img = Image.fromarray(cv2.cvtColor((canvas_bgr * 0.35).astype(np.uint8), cv2.COLOR_BGR2RGB))
        p.d = ImageDraw.Draw(p.img)
        bw, bh = 640, 400
        x0, y0 = (W - bw) // 2, (H - bh) // 2
        p.d.rounded_rectangle((x0, y0, x0 + bw, y0 + bh), radius=14, fill=CARD_BG, outline=BUTTON_HI)
        p.text((x0 + 24, y0 + 22), self.title, font="name", fill=YELLOW)
        p.text((x0 + 24, y0 + 50), "Detected: " + (", ".join(self.detected) or "nothing clear"), font="small",
               fill=MUTED, width=bw - 48)
        p.text((x0 + 24, y0 + 68), "Pick what you're going for - it steers the suggestions.", font="small", fill=MUTED)
        x, y = x0 + 24, y0 + 96
        for t in strategy.THEME_NAMES:
            w = p.f["label"].getlength(t) + 26
            if x + w > x0 + bw - 24:
                x, y = x0 + 24, y + 36
            p.button((x, y, x + w, y + 28), t, ("tag", t), style="primary" if t in self.tags else "ghost", font="label")
            x += w + 6
        by = y0 + bh - 52
        p.button((x0 + 24, by, x0 + 220, by + 34), "Use detected strategy", ("btn", "detected"))
        p.button((x0 + bw - 24 - 110, by, x0 + bw - 24, by + 34), "Save", ("btn", "save"), style="primary")
        p.button((x0 + bw - 24 - 220, by, x0 + bw - 24 - 120, by + 34), "Cancel", ("btn", None))
        self.hits = p.hits
        return p.to_bgr()

    def click(self, x, y):
        for x0, y0, x1, y1, a in self.hits:
            if x0 <= x <= x1 and y0 <= y <= y1 and a:
                if a[0] == "tag":
                    self.tags ^= {a[1]}
                elif a[0] == "btn":
                    self.finish(a[1])
                return

    def key(self, key):
        if key == 27:
            self.finish(None)
        elif key == 13:
            self.finish("save")

    def finish(self, value):
        self.done = True
        sc = self.screen
        if value == "save":
            sc.app.userdb.save_analysis_profile(sc.deck_id, tags=sorted(self.tags) or None)
        elif value == "detected":
            sc.app.userdb.save_analysis_profile(sc.deck_id, tags=None)
        if value:
            sc.check_stale()
            sc.bump()
