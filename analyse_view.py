"""The Analyse tab: our own Commander deck analyser (analyser.py) with deck selection,
settings, an overview, suggested swaps (with reasons, ownership, cost and confidence),
optional combo checks, apply-with-snapshot and undo. Analysis runs in the background on
its own database connection, so the window never freezes."""
import os
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
               "Strategy-dependent": MUTED, "Low confidence": MUTED, "Depends on your intended strategy": MUTED}
STATUS_COLOUR = {"suggested cut": RED, "possible cut": YELLOW, "pinned": GREEN}
OWN_BADGE = {"available": ("Owned & available", GREEN), "in_use": ("Owned - used in another deck", YELLOW),
             "not_owned": ("Not owned", RED)}
OWN_LABEL = {"available": ("OWNED", GREEN), "in_use": ("OWNED BUT USED IN ANOTHER DECK", YELLOW),
             "not_owned": ("NOT OWNED", RED)}
SHOWN_TIERS = ("EXCELLENT", "STRONG", "WORTH")
TIER_COLOUR = {"EXCELLENT": GREEN, "STRONG": (140, 205, 120), "WORTH": YELLOW, "NICHE": MUTED}
PER_GROUP = 3  # cards shown per purpose before "Show more options" (variety, not a limit)
SWAP_GROUP = {"HIGH": ("HIGH CONFIDENCE", GREEN), "GOOD": ("GOOD UPGRADES", (140, 205, 120)),
              "OPTIONAL": ("OPTIONAL CHANGES", YELLOW), "USED": ("OWNED - USED IN ANOTHER DECK", MUTED)}
SWAP_GROUP_NOTE = {"HIGH": "strong evidence the deck gets better", "GOOD": "better for the current plan - the card "
                   "leaving is still reasonable", "OPTIONAL": "both cards are fine - it depends how you want the deck "
                   "to play", "USED": "not part of the free plan"}
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
        self.finding = None  # card being compared by "Find a card to replace"
        self._found = None
        self.combo_state = None  # None / "checking" / error text
        self.community_state = None  # None / "fetching"
        self.mode = "analyse"  # or "build" (build a deck from the collection)
        from build_view import BuildPanel
        self.build = BuildPanel(self)
        self.show_low = False  # NICHE ideas hidden until "Show lower-confidence ideas"
        self.expanded = set()  # Consider categories showing all their options
        self._fb, self._fb_key = {}, None
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
        self.expanded, self.show_low = set(), False
        if deck_id is not None:
            saved = self.app.userdb.latest_analysis(deck_id)
            if saved:
                self.result, self.analysis_id = saved["result"], saved["id"]
                self.selected = set()  # suggestions start unticked: you choose each change
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

    def deck_printing(self, name):
        """The exact printing of a card IN this deck (else the default printing)."""
        for r in self.store.deck_cards(self.deck_id) if self.deck_id is not None else []:
            if r["card_name"] == name and r.get("scryfall_id"):
                return r["scryfall_id"]
        return self.printing(name)[0]

    def view_card(self, scryfall_id):
        from card_viewer import open_viewer
        open_viewer(self.app, self.store.card(scryfall_id))
        self.bump()

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
        found, self._found = self._found, None
        if found is not None:
            self.finding = None
            if isinstance(found, str):
                self.app.toast(f"Couldn't compare: {found}", RED)
            else:
                self.app.open_dialog(FindCutDialog(self, found))
            self.bump()
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
            self.selected = set()  # suggestions start unticked: you choose each change
            self.tab = "swaps" if res["swaps"] else "overview"
            self.scroll = 0
            self.app.toast(f"Analysed in {res['seconds']:.1f} s - {len(res['swaps'])} possible swaps", GREEN)
        self.bump()

    # ---- drawing ------------------------------------------------------------------------

    def render(self):
        self.poll()
        self.build.poll()
        toast = self.app.toast_text()
        key = (self.version, self.tab, self.scroll, self.open_dd, toast, tuple(sorted(self.selected)),
               display_currency(self.app.settings), self.running and round(self.running["progress"], 2),
               self.mode, self.build.running and round(self.build.running["progress"], 2))
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
        p.text((x0, 26), "Analyse" if self.mode == "analyse" else "Build Deck", font="name", anchor="lm", fill=YELLOW)
        for i, (m, label) in enumerate((("analyse", "Analyse deck"), ("build", "Build deck"))):
            bx = x1 - 236 + i * 120
            p.button((bx, 12, bx + 116, 40), label, ("screen_mode", m), font="label",
                     style="selected" if self.mode == m else "ghost")
        if self.mode == "build":
            self.build.left(p, x0, x1, 52)
            return None
        d = self.deck()
        if not self.decks:
            p.text((x0, 70), "Make a deck on the Decks tab first.", font="body", fill=MUTED)
            return None
        p.dropdown((x0, 46, x1, 80), d["name"] if d else "Select deck", "dd:deck", active=self.open_dd == "deck")
        y = 90
        p.paste(self.images.small(d.get("commander_scryfall_id"), d.get("commander_image")), x0, y, 80, 112, radius=6)
        if d.get("commander_scryfall_id"):
            p.hit((x0, y, x0 + 80, y + 112), ("viewid", d["commander_scryfall_id"]))
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
            label = "Re-analyse Deck" if (self.result is not None and self.stale) else "Analyse Deck"
            p.button((x0, by, x1, by + 44), label, "run", style="primary", font="name")
            half_w = (LEFT_W - 6) / 2
            p.button((x0, by - 76, x0 + half_w, by - 46), "I want to add a card", ("addcard", None), font="label",
                     style="normal" if self.deck_id is not None else "disabled")
            p.button((x0 + half_w + 6, by - 76, x1, by - 46), "Choose from my Collection", ("pickowned", None),
                     font="label", style="normal" if self.deck_id is not None else "disabled")
            if self.result is not None:
                p.button((x0, by - 38, x1, by - 8), "Find my weakest slots", ("tab", "weakest"), font="label")
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
        if self.mode == "build":
            return self.build.right(p, x0, x1, 12)
        res = self.result
        n = len(res["swaps"]) if res else 0
        nc = len(self.consider_items(low=False)) if res else 0
        tabs = [("overview", "Overview"), ("swaps", f"Swaps ({n})" if res else "Swaps"),
                ("consider", f"Consider ({nc})" if res else "Consider"), ("weakest", "Weakest"),
                ("review", "Card review"), ("combos", "Combos")]
        tx = x0
        for key, label in tabs:
            w = p.f["tab"].getlength(label) + 26
            if key == self.tab:
                p.d.rounded_rectangle((tx, 8, tx + w, 42), radius=8, fill=BUTTON_HI)
            p.text((tx + w / 2, 25), label, font="tab", anchor="mm", fill=TEXT if key == self.tab else MUTED)
            p.hit((tx, 8, tx + w, 42), ("tab", key))
            tx += w + 3
        if res:
            when = res["created"][5:] if len(res["created"]) > 10 else res["created"]
            p.text((x1, 25), ("OUT OF DATE - " if self.stale else "") + f"analysed {when}",
                   font="small", fill=YELLOW if self.stale else MUTED, anchor="rm")
        top = 52
        if res and self.stale:
            p.d.rounded_rectangle((x0, top, x1, top + 32), radius=8, fill=(60, 44, 26))
            p.text((x0 + 12, top + 16), "OUT OF DATE - the deck, your collection, card feedback or the settings changed. "
                                        "These results are kept for reference; Apply is off until you Re-analyse.",
                   font="small", fill=YELLOW, anchor="lm")
            top += 40
        if res and res.get("strategy_uncertain") and self.tab in ("swaps", "consider"):
            p.d.rounded_rectangle((x0, top, x1, top + 32), radius=8, fill=(52, 46, 30))
            p.text((x0 + 12, top + 16), f"Strategy uncertain ({res.get('strategy_note')}) - review the detected strategy "
                                        "(Edit on the left) before acting on these", font="small", fill=YELLOW, anchor="lm")
            p.hit((x0, top, x1, top + 32), "tags")
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
        elif self.tab == "consider":
            self._consider(p, x0, x1, top)
        elif self.tab == "weakest":
            self._weakest(p, x0, x1, top)
        elif self.tab == "review":
            self._review(p, x0, x1, top)
        else:
            self._combos(p, x0, x1, top)

    def _overview(self, p, x0, x1, top):
        res = self.result
        comp = res["composition"]
        mid = x0 + (x1 - x0) * 0.47
        # ---- left: what the commander and deck are trying to do
        y = top
        cp = res.get("commander_profile") or {}
        p.text((x0, y), "COMMANDER PLAN" + (" (your strategy tags win)" if res["user_tags"] else ""), font="label",
               fill=MUTED)
        y += 18
        p.text((x0, y), "Primary: " + (cp.get("primary") or "unclear"), font="rowb", fill=YELLOW, width=mid - x0 - 12)
        y += 19
        if cp.get("supporting"):
            p.text((x0, y), "Supporting: " + ", ".join(cp["supporting"][:3]), font="small", width=mid - x0 - 12)
            y += 17
        if cp.get("chain"):
            for line in _wrap(p, "Engine: " + " -> ".join(cp["chain"]), "small", mid - x0 - 12)[:3]:
                p.text((x0, y), line, font="small")
                y += 16
        if cp.get("mechanics"):
            for line in _wrap(p, "Commander does: " + ", ".join(m["label"] for m in cp["mechanics"]), "small",
                              mid - x0 - 12)[:2]:
                p.text((x0, y), line, font="small", fill=MUTED)
                y += 16
        y += 4
        p.text((x0, y), "WHAT THE DECK NEEDS FOR THIS PLAN", font="label", fill=MUTED)
        y += 17
        for need in [n for n in cp.get("needs", []) if n.get("have") is not None][:6]:
            colour = GREEN if need["have"] >= 6 else YELLOW if need["have"] >= 2 else RED
            p.text((x0, y), need["label"], font="small", width=mid - x0 - 90)
            p.text((mid - 16, y), f"{need['have']} cards", font="small", fill=colour, anchor="ra")
            y += 16
        if res.get("engines"):
            y += 4
            eng = ", ".join(f"{e.replace(' engine', '')} ({v['size']})" for e, v in list(res["engines"].items())[:5])
            for line in _wrap(p, "Engines found: " + eng, "small", mid - x0 - 12)[:2]:
                p.text((x0, y), line, font="small", fill=TEXT)
                y += 16
        y += 6
        conf = res.get("confidence") or {}
        if conf:
            colour = {"HIGH": GREEN, "MEDIUM": YELLOW, "LOW": RED}.get(conf["overall"], MUTED)
            p.text((x0, y), f"ANALYSIS CONFIDENCE: {conf['overall']}", font="rowb", fill=colour)
            y += 19
            p.text((x0, y), f"Commander understood {conf['commander']}  -  strategy {conf['strategy']}  -  "
                            f"{conf['understood']}/{conf['total']} cards understood", font="small", fill=MUTED,
                   width=mid - x0 - 12)
            y += 16
            for r in conf.get("reasons", [])[:2]:
                p.text((x0, y), "- " + r, font="small", fill=YELLOW, width=mid - x0 - 12)
                y += 16
        if res.get("strategy_uncertain"):
            p.text((x0, y + 2), f"Strategy uncertain - {res.get('strategy_note')}. Review it (Edit) before acting.",
                   font="small", fill=YELLOW, width=mid - x0 - 12)
        # ---- right: composition against THIS deck's targets
        rx = mid + 10
        y = top
        p.text((rx, y), "DECK COMPOSITION  -  target for this deck (generic)", font="label", fill=MUTED)
        y += 18
        targets = res.get("targets") or {}
        for role, t in targets.items():
            have = comp["lands"] if role == "Lands" else comp["core"].get(role, 0)
            lo, hi = t["target"]
            colour = GREEN if lo <= have <= hi else YELLOW
            label = "Finishers" if role == "Finisher" else role
            p.text((rx, y), label, font="row")
            p.text((rx + 108, y), str(have), font="rowb", fill=colour)
            g = t["generic"]
            changed = [lo, hi] != list(g)
            p.text((rx + 140, y), f"{lo}-{hi}" + (f" ({g[0]}-{g[1]})" if changed else ""), font="small",
                   fill=TEXT if changed else MUTED)
            if t.get("why"):
                p.text((rx + 230, y + 1), "; ".join(t["why"]), font="small", fill=MUTED, width=x1 - rx - 232)
            y += 19
        if comp["core"].get("Finisher", 0) == 0:
            p.text((rx, y), "No obvious dedicated finisher detected", font="small", fill=YELLOW)
            y += 17
        y += 4
        curve = "  ".join(f"{mv}{'+' if mv == 6 else ''}:{comp['curve'].get(str(mv), comp['curve'].get(mv, 0))}"
                          for mv in range(7))
        p.text((rx, y), f"Curve  {curve}   -   average {comp['avg_mv']:.2f}", font="small", fill=MUTED, width=x1 - rx)
        y += 17
        if comp["pips"]:
            p.text((rx, y), "Colour symbols: " + "  ".join(f"{c} {v:g}" for c, v in comp["pips"].items()),
                   font="small", fill=MUTED)
            y += 17
        if res.get("protected"):
            p.text((rx, y), f"Protected from cuts: {', '.join(res['protected'])}", font="small", fill=GREEN,
                   width=x1 - rx)
            y += 17
        y += 6
        p.text((rx, y), "POTENTIAL WEAKNESSES", font="label", fill=MUTED)
        y += 17
        if not res["weaknesses"]:
            p.text((rx, y), "Nothing stands out against this deck's targets", font="small", fill=GREEN)
        for w in res["weaknesses"]:
            if y > self.H - 24:
                break
            for line in _wrap(p, "! " + w["text"], "small", x1 - rx - 4)[:2]:
                p.text((rx, y), line, font="small")
                y += 16
            y += 2

    def swap_rows(self):
        """('group', tier, n) / ('swap', i) / ('used', j) - swaps grouped HIGH / GOOD / OPTIONAL."""
        res = self.result or {}
        swaps = res.get("swaps") or []
        rows = []
        for tier in ("HIGH", "GOOD", "OPTIONAL"):
            idx = [i for i, sw in enumerate(swaps) if sw.get("tier", "GOOD") == tier]
            if idx:
                rows.append(("group", tier, len(idx)))
                rows += [("swap", i) for i in idx]
        used = res.get("used_elsewhere") or []
        if used:
            rows.append(("group", "USED", len(used)))
            rows += [("used", j) for j in range(len(used))]
        return rows

    def _swaps(self, p, x0, x1, top):
        res = self.result
        swaps = res["swaps"]
        cost = sum((s["ownership"].get("cost") or 0) for i, s in enumerate(swaps) if i in self.selected)
        n_sel = len(self.selected)
        can_apply = n_sel and not self.stale
        y0 = top + 42
        if not swaps:
            nc = len(self.consider_items(low=False))
            p.text((x0, top + 10), "No change recommended.", font="name")
            p.button((x1 - 236, top - 2, x1, top + 30), "Apply selected changes", None, style="disabled")
            msg = ("For every idea on the Consider list, the analyser compared the deck before and after swapping it "
                   "in for each card that could leave - none made the deck clearly better without hurting something "
                   "it needs. You can still pick a card and press Find a cut." if nc else
                   "Nothing available improves the deck enough to replace any of its cards.")
            ly = top + 36
            for line in _wrap(p, msg, "body", x1 - x0)[:3]:
                p.text((x0, ly), line, font="body", fill=MUTED)
                ly += 20
            yb = ly + 10
            if nc:
                p.button((x0, yb, x0 + 260, yb + 34), f"View cards to consider ({nc})", ("tab", "consider"),
                         style="primary")
            p.button((x0 + (270 if nc else 0), yb, x0 + (270 if nc else 0) + 220, yb + 34), "Find my weakest slots",
                     ("tab", "weakest"))
            p.button((x0 + (500 if nc else 230), yb, x0 + (500 if nc else 230) + 200, yb + 34), "I want to add a card",
                     ("addcard", None))
            return
        n = {t: sum(1 for sw in swaps if sw.get("tier", "GOOD") == t) for t in ("HIGH", "GOOD", "OPTIONAL")}
        head = "   ".join(f"{n[t]} {label}" for t, label in (("HIGH", "high-confidence"), ("GOOD", "good upgrades"),
                                                              ("OPTIONAL", "optional")) if n[t])
        head += f"   -   Selected: {n_sel}   Cost: {self.money(cost)}"
        if res["settings"]["mode"] == "all" and res.get("budget") is not None:
            head += f" (budget {self.money(res['budget'])})"
        p.text((x0, top + 8), head, font="rowb", anchor="lm", width=x1 - x0 - 250)
        note = ("OUT OF DATE - press Re-analyse Deck before applying anything." if self.stale else
                "A compatible plan: each swap was judged on the deck with the ones above it already made. Nothing is "
                "ticked - choose the ones you want." if not n_sel else
                "Only the ticked changes are applied (you confirm first).")
        p.text((x0, top + 28), note, font="small", fill=YELLOW if self.stale else MUTED, width=x1 - x0 - 250,
               anchor="lm")
        p.button((x1 - 236, top - 2, x1, top + 30), f"Apply selected changes ({n_sel})", "apply" if can_apply else None,
                 style="primary" if can_apply else "disabled")
        rows = self.swap_rows()
        heights = {"group": 28, "swap": 172, "used": 38}
        self.scroll = max(0, min(self.scroll, len(rows) - 1))
        y, i = y0, self.scroll
        while i < len(rows) and y + heights[rows[i][0]] <= self.H - 12:
            r = rows[i]
            if r[0] == "group":
                label, colour = SWAP_GROUP[r[1]]
                p.text((x0 + 2, y + 13), label, font="label", fill=colour, anchor="lm")
                p.text((x0 + 14 + p.f["label"].getlength(label), y + 13), SWAP_GROUP_NOTE[r[1]], font="small",
                       fill=MUTED, anchor="lm", width=x1 - x0 - 300)
            elif r[0] == "swap":
                self._swap_card(p, x0, x1, y, heights["swap"], swaps[r[1]], r[1])
            else:
                u = res["used_elsewhere"][r[1]]
                p.d.rounded_rectangle((x0, y, x1, y + 32), radius=8, fill=CARD_BG)
                p.text((x0 + 12, y + 16), f"OWNED - USED IN {', '.join(u['used_in'][:2]).upper()}:  {u['name']}  "
                                          f"could replace {u['out']} ({u['tier_label'].lower()}) - not part of the free "
                                          "plan; move it from that deck yourself if you want it here",
                       font="small", fill=YELLOW, anchor="lm", width=x1 - x0 - 24)
            y += heights[r[0]]
            i += 1
        if i < len(rows) or self.scroll:
            p.text((x1, self.H - 4), "scroll for more", font="small", fill=MUTED, anchor="rs")

    def _swap_card(self, p, x0, x1, y, card_h, s, i):
        on = i in self.selected
        p.d.rounded_rectangle((x0, y, x1, y + card_h - 8), radius=10, fill=CARD_BG, outline=GREEN if on else None)
        p.button((x0 + 10, y + 10, x0 + 34, y + 34), "\u2713" if on else "", ("toggle", i),
                 style="primary" if on else "normal", font="label")
        p.hit((x0, y, x0 + 40, y + card_h - 8), ("toggle", i))
        half = x0 + 44 + (x1 - x0 - 44) // 2
        oid, oimg = self.printing(s["out"])
        p.paste(self.images.small(oid, oimg), x0 + 44, y + 8, 50, 70, radius=4)
        p.hit((x0 + 44, y + 8, x0 + 94, y + 78), ("view", s["out"]))
        iid, iimg = self.printing(s["into"])
        p.paste(self.images.small(iid, iimg), half, y + 8, 50, 70, radius=4)
        p.hit((half, y + 8, half + 50, y + 78), ("view", s["into"]))
        tx, ix = x0 + 104, half + 60
        p.text((tx, y + 8), "OUT", font="label", fill=RED)
        p.text((tx + 34, y + 6), s["out"], font="rowb", width=half - tx - 50)
        p.text((ix, y + 8), "IN", font="label", fill=GREEN)
        p.text((ix + 22, y + 6), s["into"], font="rowb", width=x1 - ix - 220)
        label = s.get("tier_label") or s.get("confidence") or ""
        p.chip(x1 - 14 - p.f["label"].getlength(label) - 14, y + 7, label,
               SWAP_GROUP.get(s.get("tier"), ("", MUTED))[1])
        own = s["ownership"]
        badge, colour = OWN_LABEL[own["status"]]
        price = own.get("cost")
        cost_txt = "" if own["status"] == "available" else \
            (f" - {self.money(price)}" if price is not None else " - price unknown")
        p.text((ix, y + 26), f"{badge}{cost_txt}" + (f"  -  {s['kind']}" if s.get("kind") else ""), font="small",
               fill=colour, width=x1 - ix - 10)
        p.button((half - 110, y + 24, half - 12, y + 44), "Never cut", ("pin", s["out"]), font="label")

        def block(x, yy, w, title, lines, colour, n=2):
            p.text((x, yy), title, font="label", fill=colour)
            yy += 15
            for line in lines[:n]:
                p.text((x, yy), line if line[:1] in "+-" else "- " + line, font="small", width=w)
                yy += 15
            return yy
        cw_l, cw_r = half - tx - 12, x1 - ix - 12
        block(tx, y + 48, cw_l, "WHY OUT", s.get("why_out") or [], MUTED)
        block(tx, y + 96, cw_l, "WHAT YOU LOSE", s.get("losses") or ["nothing measurable"], RED)
        block(ix, y + 48, cw_r, "WHY IN", s.get("why_in") or [], MUTED)
        block(ix, y + 96, cw_r, "WHAT IMPROVES", s.get("gains") or [], GREEN)
        net = s.get("net") or ""
        p.text((x0 + 44, y + card_h - 24), "NET: " + net, font="small", fill=TEXT, width=x1 - x0 - 380)
        alts = s.get("other_ins") or []
        n_links = s.get("in_links_total", 0)
        p.button((x1 - 330, y + card_h - 30, x1 - 160, y + card_h - 12), f"Show interactions ({n_links})",
                 ("links", i) if n_links else None, style="normal" if n_links else "disabled", font="label")
        p.button((x1 - 152, y + card_h - 30, x1 - 12, y + card_h - 12), "Other cuts for IN", ("findcut_name", s["into"]),
                 font="label", style="disabled" if self.stale or self.finding else "normal")
        if alts:
            p.text((x0 + 44, y + card_h - 40), "Also wanted this slot: " + ", ".join(alts[:3]), font="small",
                   fill=MUTED, width=half - x0 - 60)

    # ---- Consider: need-driven ideas, grouped by purpose ----------------------------------

    def feedback(self):
        """{card: 'good' | 'not_for_deck'} for this deck (read fresh - it changes from this screen)."""
        if self.deck_id is None:
            return {}
        key = (self.deck_id, self.version)
        if self._fb_key != key:
            self._fb, self._fb_key = self.app.userdb.suggestion_feedback(self.deck_id), key
        return self._fb

    def consider_items(self, low=None):
        """Considered cards still worth showing: never a card you marked NOT FOR THIS DECK; the
        NICHE tier only with "Show lower-confidence ideas"."""
        res = self.result or {}
        low = self.show_low if low is None else low
        fb = self.feedback()
        out = []
        for i, c in enumerate(res.get("considered") or []):
            if fb.get(c["name"]) == "not_for_deck":
                continue
            if c.get("tier", "WORTH") not in SHOWN_TIERS and not low:
                continue
            out.append((i, c))
        return out

    def consider_rows(self):
        """Flat list of rows: ('group', title, need, n_more) / ('card', index) / ('more', category, n)."""
        items = self.consider_items()
        by_cat = {}
        for i, c in items:
            by_cat.setdefault(c.get("category") or "OTHER IDEAS", []).append(i)
        rec = (self.result or {}).get("recommendations") or {}
        order = [g["category"] for g in rec.get("groups") or []]
        cats = sorted(by_cat, key=lambda k: order.index(k) if k in order else 999)
        need_of = {g["category"]: g.get("need", "") for g in rec.get("groups") or []}
        rows = []
        for cat in cats:
            idx = by_cat[cat]
            rows.append(("group", cat, need_of.get(cat, ""), len(idx)))
            shown = idx if cat in self.expanded else idx[:PER_GROUP]
            rows += [("card", i) for i in shown]
            if len(idx) > len(shown):
                rows.append(("more", cat, len(idx) - len(shown)))
        return rows

    def _consider(self, p, x0, x1, top):
        res = self.result
        rec = res.get("recommendations") or {}
        rows = self.consider_rows()
        all_items = res.get("considered") or []
        fb = self.feedback()
        n_low = sum(1 for c in all_items if c.get("tier", "WORTH") not in SHOWN_TIERS and fb.get(c["name"]) != "not_for_deck")
        p.text((x0, top + 8), "Cards worth considering", font="rowb", anchor="lm")
        needs = rec.get("needs") or []
        sub = ("What the deck needs: " + "; ".join(f"{n['label']} ({n['have']} now)" for n in needs[:3])) if needs \
            else "No missing pieces found - only upgrades over the deck's weakest cards for a job (if any)."
        p.text((x0, top + 28), sub, font="small", fill=MUTED, anchor="lm", width=x1 - x0 - 330)
        bx = x1
        if n_low:
            label = "Hide lower-confidence ideas" if self.show_low else f"Show lower-confidence ideas ({n_low})"
            p.button((bx - 220, top - 2, bx, top + 24), label, ("lowconf", None), font="label",
                     style="selected" if self.show_low else "normal")
            bx -= 228
        if os.environ.get("CARDSCAN_DEBUG_ANALYSER"):
            p.button((bx - 110, top - 2, bx, top + 24), "Why not?", ("whynot", None), font="label")
            bx -= 118
        busy = self.community_state == "fetching"
        p.button((bx - 150, top - 2, bx, top + 24), "Fetching..." if busy else "Community ideas",
                 None if busy else ("community", None), font="label", style="disabled" if busy else "normal")
        y0 = top + 46
        if not rows:
            if (rec.get("mode") or res["settings"].get("mode")) == "collection":
                p.text((x0, y0 + 10), "No strong upgrades found in your available Collection.", font="name")
                p.text((x0, y0 + 38), "Only free copies you own are suggested here - cards used in other decks stay "
                                      "where they are. Explore All Cards to see what else would help (nothing is "
                                      "bought or changed).", font="small", fill=MUTED, width=x1 - x0)
                p.button((x0, y0 + 64, x0 + 200, y0 + 98), "Explore All Cards", ("explore_all", None), style="primary")
            else:
                p.text((x0, y0 + 10), "No card crossed the bar for a recommendation with these settings.", font="body",
                       fill=MUTED)
                p.text((x0, y0 + 34), "That's a valid result: nothing missing stood out, and nothing was clearly "
                                      "better than what the deck already runs.", font="small", fill=MUTED,
                       width=x1 - x0)
            return
        heights = {"group": 30, "card": 104, "more": 34}
        self.scroll = max(0, min(self.scroll, len(rows) - 1))
        y = y0
        i = self.scroll
        while i < len(rows) and y + heights[rows[i][0]] <= self.H - 16:
            r = rows[i]
            if r[0] == "group":
                p.text((x0 + 2, y + 14), r[1], font="label", fill=GOLD, anchor="lm")
                w = p.f["label"].getlength(r[1])
                p.text((x0 + 14 + w, y + 14), f"{r[2]}  -  {r[3]} idea{'s' if r[3] != 1 else ''}", font="small",
                       fill=MUTED, anchor="lm", width=x1 - x0 - w - 20)
            elif r[0] == "more":
                p.button((x0 + 80, y + 2, x0 + 320, y + 28), f"Show more options ({r[2]})", ("more", r[1]),
                         font="label")
            else:
                self._consider_card(p, x0, x1, y, r[1], all_items[r[1]], fb)
            y += heights[r[0]]
            i += 1
        if i < len(rows) or self.scroll:
            p.text((x1, self.H - 8), "scroll for more", font="small", fill=MUTED, anchor="rs")
        if self.finding:
            p.text((x0, self.H - 8), f"Comparing {self.finding} against the deck's possible cuts...", font="small",
                   fill=YELLOW, anchor="ls")

    def _consider_card(self, p, x0, x1, y, i, c, fb):
        row_h = 98
        p.d.rounded_rectangle((x0, y, x1, y + row_h), radius=10, fill=CARD_BG)
        cid, cimg = self.printing(c["name"])
        p.paste(self.images.small(cid, cimg), x0 + 10, y + 8, 58, 81, radius=4)
        p.hit((x0 + 10, y + 8, x0 + 68, y + 89), ("view", c["name"]))
        tx = x0 + 80
        tier = c.get("tier", "WORTH")
        tl = c.get("tier_label") or "WORTH CONSIDERING"
        p.text((tx, y + 6), c["name"], font="rowb", width=330)
        cx = tx + min(330, p.f["rowb"].getlength(c["name"])) + 10
        cx = p.chip(cx, y + 6, tl, TIER_COLOUR.get(tier, MUTED))
        for label in (c.get("labels") or [])[:2]:
            if cx > x1 - 470:
                break
            cx = p.chip(cx, y + 6, label, TEXT)
        own = c["ownership"]
        badge, colour = OWN_LABEL[own["status"]]
        if own["status"] == "in_use" and own.get("used_in"):
            badge += f" ({', '.join(own['used_in'][:2])})"
        price = own.get("cost")
        cost_txt = "" if own["status"] == "available" else \
            (f"  -  {self.money(price)}" if price is not None else "  -  price unknown")
        p.text((tx, y + 30), badge + cost_txt, font="label", fill=colour, width=x1 - tx - 230)
        ly = y + 47
        for line in c["why"][:3]:
            p.text((tx, ly), "- " + line, font="small", width=x1 - tx - 230)
            ly += 16
        bx = x1 - 214
        p.button((bx, y + 6, x1 - 10, y + 28), "Find a cut",
                 None if self.stale or self.finding else ("findcut", i), font="label",
                 style="disabled" if self.stale or self.finding else "primary")
        p.button((bx, y + 32, x1 - 10, y + 54), f"Show interactions ({c.get('links_total', 0)})",
                 ("clinks", i) if c.get("links_total") else None, font="label",
                 style="normal" if c.get("links_total") else "disabled")
        good = fb.get(c["name"]) == "good"
        p.button((bx, y + 58, bx + 100, y + 80), "Good idea" if not good else "Good idea \u2713",
                 ("fb", (c["name"], None if good else "good")), font="label", style="selected" if good else "normal")
        p.button((bx + 104, y + 58, x1 - 10, y + 80), "Not for this deck", ("fb", (c["name"], "not_for_deck")),
                 font="label")

    def _flag_buttons(self, p, x, y, name, flags):
        never = "never_cut" in flags or name in (self.result.get("protected") or [])
        p.button((x, y, x + 96, y + 24), "Unprotect" if never else "Never cut", ("unpin" if never else "pin", name),
                 font="label", style="selected" if never else "normal")
        core = "core" in flags
        p.button((x + 100, y, x + 156, y + 24), "Core", ("flag", (name, "core", not core)), font="label",
                 style="primary" if core else "normal")
        ni = "not_important" in flags
        p.button((x + 160, y, x + 262, y + 24), "Not important", ("flag", (name, "not_important", not ni)),
                 font="label", style="selected" if ni else "normal")

    def _weakest(self, p, x0, x1, top):
        res = self.result
        rows = res.get("weakest") or []
        p.text((x0, top + 8), "Find my weakest slots - if you need room, look here first", font="rowb", anchor="lm")
        p.text((x0, top + 28), "Not 'bad cards': the slots the deck could free most cheaply, with what each card "
                               "contributes and what you'd lose. Core, Never cut and essential cards are left out.",
               font="small", fill=MUTED, anchor="lm", width=x1 - x0)
        y0 = top + 46
        if not rows:
            p.text((x0, y0 + 10), "Every card is essential to this deck or protected.", font="body", fill=GREEN)
            return
        row_h = 92
        vis = max(1, (self.H - y0 - 16) // row_h)
        self.scroll = max(0, min(self.scroll, max(0, len(rows) - vis)))
        y = y0
        flags = res.get("flags") or {}
        for i in range(self.scroll, min(len(rows), self.scroll + vis)):
            w = rows[i]
            p.d.rounded_rectangle((x0, y, x1, y + row_h - 6), radius=10, fill=CARD_BG)
            p.text((x0 + 10, y + 6), w["name"], font="rowb", width=330)
            p.hit((x0 + 10, y + 4, x0 + 10 + min(330, p.f["rowb"].getlength(w["name"])), y + 24), ("view", w["name"]))
            level = w.get("flexibility") or ("HIGH" if w.get("expendability", 0) >= 0.7 else "MEDIUM")
            p.text((x0 + 350, y + 7), f"Flexible slot: {level}", font="small",
                   fill=GREEN if level == "HIGH" else YELLOW if level == "MEDIUM" else MUTED)
            p.text((x0 + 520, y + 7), f"{w['strong']} strong / {w['links']} interactions", font="small", fill=MUTED,
                   width=x1 - x0 - 820)
            self._flag_buttons(p, x1 - 272, y + 4, w["name"], set(flags.get(w["name"], [])))
            jobs = ", ".join(w["functions"][:3] or w["roles"][:3]) or "no clear job found"
            p.text((x0 + 10, y + 27), f"Contributes: {jobs}" + (f"  -  engines: {', '.join(w['engines'][:2])}"
                                                               if w["engines"] else ""), font="small", fill=MUTED,
                   width=x1 - x0 - 20)
            p.text((x0 + 10, y + 44), "Why flexible: " + ("; ".join(w["reasons_to_cut"][:2]) or "nothing specific - "
                                                          "it is simply among the least connected cards"),
                   font="small", fill=YELLOW if w["reasons_to_cut"] else MUTED, width=x1 - x0 - 20)
            lose = "; ".join((w.get("losses") or [])[:2] + w["reasons_to_keep"][:1]) or "nothing special detected"
            p.text((x0 + 10, y + 61), "You'd lose: " + lose, font="small",
                   fill=RED if w.get("losses") else MUTED, width=x1 - x0 - 20)
            y += row_h

    def _review(self, p, x0, x1, top):
        res = self.result
        rows = res.get("cut_review") or []
        order = {"suggested cut": 0, "possible cut": 1, "pinned": 3}
        rows = sorted(rows, key=lambda v: (order.get(v["status"].split(" -")[0], 2), -v["cut_confidence"], v["name"]))
        p.text((x0, top + 8), "Card review - what the analyser thinks each card does here", font="rowb", anchor="lm")
        p.text((x0, top + 28), "A cut needs positive evidence that outweighs everything that makes the card important. "
                               "Core = this card is what the deck is about; Not important = slightly less protection.",
               font="small", fill=MUTED, anchor="lm", width=x1 - x0)
        y0 = top + 46
        row_h = 80
        vis = max(1, (self.H - y0 - 18) // row_h)
        self.scroll = max(0, min(self.scroll, max(0, len(rows) - vis)))
        y = y0
        for i in range(self.scroll, min(len(rows), self.scroll + vis)):
            v = rows[i]
            p.d.rounded_rectangle((x0, y, x1, y + row_h - 4), radius=8, fill=CARD_BG)
            verdict = v.get("verdict") or v["status"]
            colour = RED if verdict.startswith("POSSIBLE") else GREEN if "core" in verdict or "protected" in verdict \
                else TEXT
            p.text((x0 + 10, y + 5), v["name"], font="rowb", width=290)
            p.hit((x0 + 10, y + 3, x0 + 10 + min(290, p.f["rowb"].getlength(v["name"])), y + 23), ("view", v["name"]))
            p.text((x0 + 310, y + 6), verdict, font="small", fill=colour, width=330)
            p.text((x0 + 650, y + 6), f"cut {v['cut_confidence']:.0%}  -  evidence {v['E']:g} / protection {v['P']:g}",
                   font="small", fill=MUTED, width=x1 - x0 - 930)
            self._flag_buttons(p, x1 - 272, y + 3, v["name"], set(v.get("flags") or []))
            subs = ", ".join(v.get("functions", [])[:3]) or "-"
            p.text((x0 + 10, y + 25), f"Roles: {', '.join(v.get('roles', [])[:3]) or 'none found'}  -  Subroles: {subs}"
                                      f"  -  Commander synergy: {v.get('commander_synergy', 'none')}  -  "
                                      f"Interactions: {v.get('strong', 0)} strong / {v.get('moderate', 0)} moderate"
                                      + (f"  -  Engines: {', '.join(v['engines'][:2])}" if v.get("engines") else ""),
                   font="small", fill=MUTED, width=x1 - x0 - 20)
            ev = "; ".join(t for t, _ in v["evidence"][:2]) or "none"
            pr = "; ".join(t for t, w in v["protection"][:2] if w > 0) or "nothing special detected"
            p.text((x0 + 10, y + 42), "Cut evidence: " + ev, font="small", fill=YELLOW if v["evidence"] else MUTED,
                   width=x1 - x0 - 20)
            p.text((x0 + 10, y + 58), "Protection: " + pr, font="small", fill=GREEN if v["protection"] else MUTED,
                   width=x1 - x0 - 20)
            y += row_h
        if len(rows) > vis:
            p.text((x0, self.H - 8), f"{self.scroll + 1}-{min(len(rows), self.scroll + vis)} of {len(rows)} cards "
                                     "(scroll for more)", font="small", fill=MUTED, anchor="lm")

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
            elif kind in ("view", "viewid"):
                return self.view_card(v if kind == "viewid" else self.deck_printing(v))
            elif kind == "toggle":
                self.selected ^= {v}
            elif kind in ("pin", "unpin"):
                self.protect(v, kind == "pin")
            elif kind == "flag":
                self.set_flag(*v)
            elif kind == "findcut":
                self.find_cut(self.result["considered"][v]["name"])
            elif kind == "findcut_name":
                self.find_cut(v)
            elif kind == "addcard":
                self.add_card_dialog()
            elif kind == "pickowned":
                self.pick_owned_dialog()
            elif kind == "links":
                sw = self.result["swaps"][v]
                self.app.open_dialog(InteractionsDialog(sw["into"], sw.get("in_links") or [], sw.get("in_links_total", 0),
                                                        sw["out"], sw.get("out_links") or []))
            elif kind == "clinks":
                c = self.result["considered"][v]
                self.app.open_dialog(InteractionsDialog(c["name"], c.get("links") or [], c.get("links_total", 0)))
            elif kind == "combo":
                webbrowser.open(v)
            elif kind == "screen_mode":
                self.mode, self.scroll = v, 0
            elif kind.startswith("b_"):
                self.build.click(kind, v)
            elif kind == "fb":
                self.suggestion_feedback(*v)
            elif kind == "more":
                self.expanded.add(v)
            elif kind == "lowconf":
                self.show_low = not self.show_low
                self.scroll = 0
            elif kind == "explore_all":
                udb.save_analysis_profile(self.deck_id, mode="all")
                self.app.toast("Exploring All Cards - nothing is bought or changed", GREEN)
                self.run()
            elif kind == "community":
                self.fetch_community()
            elif kind == "whynot":
                self.why_not()
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
        if self.mode == "build":
            self.build.wheel(delta)
            return self.bump()
        if self.tab in ("swaps", "consider", "review", "weakest"):
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

    def set_flag(self, name, flag, on):
        """Core card / Not important feedback for this deck (used by the next analysis)."""
        self.app.userdb.set_card_flag(self.deck_id, name, flag, on)
        res = self.result
        if res:
            flags = res.setdefault("flags", {})
            cur = set(flags.get(name, []))
            (cur.add if on else cur.discard)(flag)
            if on and flag in ("core", "not_important"):
                cur.discard("not_important" if flag == "core" else "core")
            flags[name] = sorted(cur)
            for v in res.get("cut_review") or []:
                if v["name"] == name:
                    v["flags"] = sorted(cur)
        label = {"core": "a core card", "not_important": "not important"}[flag]
        self.app.toast(f"{name} marked as {label} - Re-analyse to use it" if on else f"{name}: mark removed", GREEN)
        self.check_stale()
        self.bump()

    def search_names(self, text, limit=12):
        """Card names for the search box (the scanner's search when available)."""
        db = getattr(self.app, "db", None)
        if db is not None and hasattr(db, "search"):
            return db.search(text, limit)
        self.app.userdb.attach_cards_db()
        q = text.strip()
        rows = self.app.userdb.conn.execute(
            """SELECT name FROM scry.oracle_cards WHERE name LIKE ? ORDER BY name NOT LIKE ?, LENGTH(name), name
               LIMIT ?""", (f"%{q}%", f"{q}%", limit)).fetchall()
        return [r[0] for r in rows]

    def add_card_dialog(self, text="", problem=None):
        """'I want to add a card': search any card by name, then Find a cut for it."""
        if self.deck_id is None:
            return
        msg = ([problem] if problem else []) + ["Type the card's name (part of it is enough). The analyser then "
                                                "compares it against the deck and suggests what to cut."]

        def done(ok, t):
            name = (t or "").strip()
            if not ok or not name:
                return
            matches = self.search_names(name)
            exact = [m for m in matches if m.lower() == name.lower()]
            if exact or len(matches) == 1:
                return self.find_cut((exact or matches)[0])
            if not matches:
                return self.add_card_dialog(name, f'No card called "{name}" found.')
            self.app.open_dialog(Dialog("Which card?", kind="choice", options=[(m, m, "normal") for m in matches],
                                        buttons=[("Cancel", None, "normal")],
                                        on_done=lambda v, _t: v is not None and self.find_cut(v)))
        self.app.open_dialog(Dialog("I want to add a card", msg, kind="input", text=text, placeholder="Card name",
                                    buttons=[("Cancel", None, "normal"), ("Find a cut", True, "primary")],
                                    on_done=done))

    def pick_owned_dialog(self, text=""):
        """'Choose from my Collection': free copies you own (and, marked, copies used in other decks)."""
        if self.deck_id is None:
            return
        own = analyser.ownership_map(self.app.userdb, self.deck_id)
        in_deck = {r["card_name"] for r in self.store.deck_cards(self.deck_id)}

        def done(ok, t):
            if not ok:
                return
            q = (t or "").strip().lower()
            names = sorted(n for n, o in own.items() if n not in in_deck and q in n.lower())
            names.sort(key=lambda n: (own[n]["free"] <= 0, n))
            if not names:
                return self.app.toast(f'Nothing in your collection matches "{t}"', YELLOW)
            opts = [(n, n + ("" if own[n]["free"] > 0 else f"   (used in {', '.join(own[n]['used_in'][:1])})"),
                     "normal") for n in names[:14]]
            self.app.open_dialog(Dialog(f"Your cards matching \"{t}\"" if q else "Your cards", kind="choice",
                                        options=opts, buttons=[("Cancel", None, "normal")],
                                        on_done=lambda v, _t: v is not None and self.find_cut(v)))
        self.app.open_dialog(Dialog("Choose from my Collection", ["Search the cards you own (leave empty to list "
                                                                  "them A-Z). Cards free in your collection come first."],
                                    kind="input", text=text, placeholder="Card name (optional)",
                                    buttons=[("Cancel", None, "normal"), ("Search", True, "primary")], on_done=done))

    def suggestion_feedback(self, name, verdict):
        """GOOD IDEA: a small nudge for similar cards next time (never adds anything).
        NOT FOR THIS DECK: hidden now and never suggested for this deck again."""
        if self.deck_id is None:
            return
        self.app.userdb.set_suggestion_feedback(self.deck_id, name, verdict)
        self._fb_key = None
        if verdict == "not_for_deck":
            self.app.toast(f"{name} won't be suggested for this deck again", GREEN)
        elif verdict == "good":
            self.app.toast(f"Noted - similar cards get a small boost next time (nothing was added)", GREEN)
        self.bump()

    def why_not(self):
        """Debug (CARDSCAN_DEBUG_ANALYSER=1): why is a card (not) suggested for this deck?"""
        deck_id, settings, path = self.deck_id, self.settings(), self.app.userdb.path

        def ask(ok, text):
            name = (text or "").strip()
            if not ok or not name:
                return

            def work():
                import recommend
                db = UserDB(path)
                try:
                    w = recommend.why_not(db, DeckStore(db), deck_id, settings, name)
                    lines = ([f"Recommended: {w['tier']}"] + w["why"][:4]) if w["recommended"] else                         [f"Not suggested: {w['reason']}"]
                    if w.get("signals"):
                        lines.append(", ".join(f"{k} {v}" for k, v in w["signals"].items()))
                except Exception as e:  # noqa: BLE001
                    lines = [f"Couldn't check: {e}"]
                finally:
                    db.close()
                self.app.open_dialog(Dialog(f"Why (not) {name}?", lines, buttons=[("OK", True, "primary")]))
                self.bump()
            threading.Thread(target=work, daemon=True).start()
        self.app.open_dialog(Dialog("Why not? (debug)", "Card name", kind="input", text="",
                                    buttons=[("Cancel", None, "normal"), ("Check", True, "primary")], on_done=ask))

    def fetch_community(self):
        """Optional: ask Recommander (documented public API) once for this commander + deck and
        cache it; the next analysis uses it as a small, labelled signal. Explicit action only."""
        if self.deck_id is None or self.community_state == "fetching":
            return
        rows = self.store.deck_cards(self.deck_id)
        commanders = [r["card_name"] for r in rows if r["role"] in ("commander", "partner")]
        cards = [r["card_name"] for r in rows if r["role"] == "main"]
        if not commanders:
            return self.app.toast("This deck has no commander yet", YELLOW)

        def done(ok, _t):
            if not ok:
                return
            self.community_state = "fetching"
            oids = [r.get("oracle_id") for r in rows if r["role"] in ("commander", "partner")]

            def work():
                import community
                try:
                    n, how = community.RecommanderClient().fetch(community.commander_key(oids), commanders, cards)
                    self.app.toast(f"Community ideas: {n} cards ({'already cached' if how == 'cached' else 'saved'}) "
                                   "- Re-analyse to use them", GREEN)
                except Exception as e:  # noqa: BLE001
                    self.app.toast(f"Couldn't get community ideas: {e}", RED)
                finally:
                    self.community_state = None
                    self.bump()
            threading.Thread(target=work, daemon=True).start()
        self.app.open_dialog(Dialog(
            "Get community ideas?",
            ["Sends this deck's commander and card list ONCE to Recommander (recommander.cards), a free public "
             "recommendation service, and keeps the answer on this computer.",
             "It is only a small extra signal: a card still has to fill a real need in YOUR deck to be suggested. "
             "Data: Recommander - personal use."],
            buttons=[("Cancel", False, "normal"), ("Ask once", True, "primary")], on_done=done))

    def find_cut(self, name):
        """'If this card goes in, what is the safest card to take out?' - in the background."""
        if self.finding or self.deck_id is None:
            return
        self.finding = name
        deck_id, settings, path = self.deck_id, self.settings(), self.app.userdb.path

        def work():
            db = None
            try:
                db = UserDB(path)
                self._found = analyser.find_cuts_for(db, DeckStore(db), deck_id, settings, name)
            except Exception as e:  # noqa: BLE001
                self._found = str(e)
            finally:
                if db is not None:
                    db.close()
                self.bump()
        threading.Thread(target=work, daemon=True).start()
        self.bump()

    def protect(self, name, on=True):
        """'Never cut' / 'Unprotect' a card in this deck."""
        self.app.userdb.protect_card(self.deck_id, name, on)
        res = self.result
        if res:
            if on:
                keep = {res["swaps"][i]["out"] for i in self.selected}
                res["swaps"] = [s for s in res["swaps"] if s["out"] != name]
                self.selected = {i for i, s in enumerate(res["swaps"]) if s["out"] in keep}
            prot = set(res.get("protected") or [])
            (prot.add if on else prot.discard)(name)
            res["protected"] = sorted(prot)
            for v in res.get("cut_review") or []:
                if v["name"] == name:
                    v["status"] = "pinned" if on else "unprotected - analyse again to re-check it"
                    v["verdict"] = "KEEP - you protected it" if on else "unprotected - Re-analyse to re-check it"
            flags = res.setdefault("flags", {})
            cur = set(flags.get(name, []))
            (cur.add if on else cur.discard)("never_cut")
            flags[name] = sorted(cur)
        self.check_stale()
        self.app.toast(f"{name} will never be suggested as a cut" if on else f"{name} is no longer protected", GREEN)
        if getattr(self.app, "decks_screen", None) is not None:
            self.app.decks_screen.bump()
        self.bump()

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
        oids = [r.get("oracle_id") for r in rows if r["role"] in ("commander", "partner")]
        cards = [r["card_name"] for r in rows if r["role"] == "main"]
        path, deck_id = self.app.userdb.path, self.deck_id

        def work():
            db = None
            try:
                combos = CommanderSpellbookProvider().combos(commanders, cards)
                db = UserDB(path)
                analyser.annotate_combos(db, deck_id, combos)
                try:  # remember one-card-away combo pieces as (small) evidence for Consider
                    import community
                    community.save_combos(community.commander_key(oids), combos)
                except Exception:  # noqa: BLE001 - the cache is optional
                    pass
                self.result["combos"] = combos
                self.combo_state = None
            except Exception as e:  # noqa: BLE001
                self.combo_state = f"Couldn't reach Commander Spellbook: {e}"
            finally:
                if db is not None:
                    db.close()
                self.bump()
        threading.Thread(target=work, daemon=True).start()

    def confirm_single_swap(self, out, into):
        """'Swap this' from Find a card to replace - confirmed first, snapshotted, undoable."""
        def done(ok, _t):
            if not ok:
                return
            try:
                analyser.apply_swaps(self.app.userdb, self.store, self.deck_id, [dict(out=out, into=into)])
            except (UserDBError, ValueError) as e:
                return self.app.toast(f"Nothing changed: {e}", RED)
            self.app.toast(f"Swapped {out} -> {into} (Undo last change puts it back)", GREEN)
            self.after_change()
        self.app.open_dialog(Dialog(f"Swap {out} for {into}?",
                                    [f"OUT: {out}", f"IN: {into}",
                                     "A snapshot of the deck is saved first - Undo last change puts it back."],
                                    buttons=[("Cancel", None, "normal"), ("Swap", True, "primary")], on_done=done))

    def confirm_apply(self):
        if self.stale:
            return self.app.toast("This analysis is out of date - press Re-analyse Deck first", YELLOW)
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


class InteractionsDialog(Dialog):
    """The evidence behind a "works with N cards" claim: each linked card and why."""

    def __init__(self, name, links, total, other=None, other_links=()):
        super().__init__(f"How {name} works with this deck")
        self.name, self.links, self.total = name, list(links), total
        self.other, self.other_links = other, list(other_links)
        self.scroll = 0

    def draw(self, canvas_bgr):
        import cv2
        import numpy as np
        from PIL import Image, ImageDraw
        H, W = canvas_bgr.shape[:2]
        p = Painter(W, H)
        p.img = Image.fromarray(cv2.cvtColor((canvas_bgr * 0.35).astype(np.uint8), cv2.COLOR_BGR2RGB))
        p.d = ImageDraw.Draw(p.img)
        x0, y0, x1, y1 = 160, 30, W - 160, H - 30
        p.d.rounded_rectangle((x0, y0, x1, y1), radius=14, fill=CARD_BG, outline=BUTTON_HI)
        p.text((x0 + 24, y0 + 18), self.title, font="name", fill=YELLOW, width=x1 - x0 - 48)
        y = y0 + 52
        p.text((x0 + 24, y), f"Works with {self.total} card{'s' if self.total != 1 else ''} in the deck (strong and "
                             "moderate links; weak, incidental ones aren't counted)"
                             + (f" - first {len(self.links)} shown" if self.total > len(self.links) else "") + ":",
               font="small", fill=MUTED, width=x1 - x0 - 48)
        y += 22
        if not self.links:
            p.text((x0 + 24, y), "No interactions found - so the analyser makes no synergy claim.", font="small",
                   fill=MUTED)
            y += 20
        for link in self.links:
            if y > y1 - 120:
                break
            st = link.get("strength", "")
            if st:
                p.text((x0 + 24, y), st.upper(), font="label", fill=GREEN if st == "strong" else YELLOW)
            p.text((x0 + 104, y), link["why"], font="small", width=x1 - x0 - 128)
            y += 18
        if self.other is not None:
            y += 8
            p.text((x0 + 24, y), f"{self.other} (the card it would replace) works with:", font="small", fill=MUTED)
            y += 20
            if not self.other_links:
                p.text((x0 + 24, y), "no interactions found", font="small", fill=MUTED)
            for link in self.other_links:
                if y > y1 - 60:
                    break
                st = link.get("strength", "")
                if st:
                    p.text((x0 + 24, y), st.upper(), font="label", fill=GREEN if st == "strong" else YELLOW)
                p.text((x0 + 104, y), link["why"], font="small", width=x1 - x0 - 128)
                y += 18
        p.button((x1 - 124, y1 - 50, x1 - 24, y1 - 16), "Close", ("btn", None))
        self.hits = p.hits
        return p.to_bgr()

    def click(self, x, y):
        for x0, y0, x1, y1, a in self.hits:
            if x0 <= x <= x1 and y0 <= y <= y1 and a and a[0] == "btn":
                self.done = True

    def key(self, key):
        if key in (27, 13):
            self.done = True


class FindCutDialog(Dialog):
    """'Find a cut for this card': the best cuts for one new card (deck before vs after each
    swap), each with why, what improves, what you lose and the net effect - or an honest
    'No safe cut found' with the reasons."""

    def __init__(self, screen, found):
        super().__init__(f"Find a cut for {found['into']}")
        self.screen, self.found = screen, found
        self.scroll = 0

    def draw(self, canvas_bgr):
        import cv2
        import numpy as np
        from PIL import Image, ImageDraw
        H, W = canvas_bgr.shape[:2]
        p = Painter(W, H)
        p.img = Image.fromarray(cv2.cvtColor((canvas_bgr * 0.35).astype(np.uint8), cv2.COLOR_BGR2RGB))
        p.d = ImageDraw.Draw(p.img)
        x0, y0, x1, y1 = 80, 16, W - 80, H - 16
        p.d.rounded_rectangle((x0, y0, x1, y1), radius=14, fill=CARD_BG, outline=BUTTON_HI)
        f = self.found
        sc = self.screen
        cid, cimg = sc.printing(f["into"])
        p.paste(sc.images.small(cid, cimg), x0 + 20, y0 + 14, 64, 89, radius=5)
        p.hit((x0 + 20, y0 + 14, x0 + 84, y0 + 103), ("view", f["into"]))
        p.text((x0 + 98, y0 + 14), "FIND A CUT FOR", font="label", fill=MUTED)
        p.text((x0 + 98, y0 + 30), f["into"], font="name", fill=YELLOW, width=x1 - x0 - 260)
        own = (f.get("ownership") or {})
        if own:
            badge, colour = OWN_LABEL.get(own.get("status"), ("", MUTED))
            p.text((x0 + 98, y0 + 54), own.get("label") or badge, font="small", fill=colour, width=x1 - x0 - 260)
        ny = y0 + 72
        for line in (f.get("notes") or [])[:2]:
            p.text((x0 + 98, ny), line, font="small", fill=YELLOW, width=x1 - x0 - 140)
            ny += 16
        y = y0 + 112
        opts = f.get("options") or []
        if not opts:
            p.text((x0 + 24, y + 6), f.get("verdict") or "No safe cut found.", font="name")
            ly = y + 34
            for line in (f.get("reasons") or [])[:4]:
                for wl in _wrap(p, "- " + line, "small", x1 - x0 - 48)[:2]:
                    p.text((x0 + 24, ly), wl, font="small", fill=MUTED)
                    ly += 16
            p.text((x0 + 24, ly + 8), "That's a valid answer: every card that could leave is either protected, needed "
                                      "for a job the deck is short of, or does more for this deck than the new card "
                                      "would. You can still swap one yourself.", font="small", fill=MUTED,
                   width=x1 - x0 - 48)
        box_h = 196
        vis = max(1, (y1 - 60 - y) // box_h)
        self.scroll = max(0, min(self.scroll, max(0, len(opts) - vis)))
        for k in range(self.scroll, min(len(opts), self.scroll + vis)):
            o = opts[k]
            p.d.rounded_rectangle((x0 + 20, y, x1 - 20, y + box_h - 8), radius=10, fill=(40, 42, 48))
            oid, oimg = sc.printing(o["out"])
            p.paste(sc.images.small(oid, oimg), x0 + 30, y + 8, 56, 78, radius=4)
            p.hit((x0 + 30, y + 8, x0 + 86, y + 86), ("view", o["out"]))
            tx = x0 + 98
            rank = o.get("rank") or ("BEST CUT" if k == 0 else "ALTERNATIVE")
            p.text((tx, y + 8), f"{k + 1}. {rank}", font="label", fill=GREEN if k == 0 else MUTED)
            p.text((tx + 120, y + 6), f"OUT: {o['out']}   ->   IN: {f['into']}", font="rowb", width=x1 - tx - 420)
            if o.get("tier_label"):
                p.chip(x1 - 290 - p.f["label"].getlength(o["tier_label"]), y + 6, o["tier_label"],
                       SWAP_GROUP.get(o.get("tier"), ("", MUTED))[1])
            info = "  -  ".join(x for x in (o.get("kind"), "cut confidence " + o["cut_word"] if o.get("cut_word")
                                            else None, "replacement confidence " + o["replacement_word"]
                                            if o.get("replacement_word") else None) if x)
            p.text((tx, y + 26), info, font="small", fill=MUTED, width=x1 - tx - 290)
            half = tx + (x1 - 280 - tx) // 2

            def block(x, yy, w, title, lines, colour):
                p.text((x, yy), title, font="label", fill=colour)
                yy += 15
                for line in lines[:2]:
                    p.text((x, yy), line if line[:1] in "+-" else "- " + line, font="small", width=w)
                    yy += 15
            block(tx, y + 44, half - tx - 10, "WHY CUT THIS?", o.get("why_out") or o.get("why_cut") or [], MUTED)
            block(half, y + 44, x1 - 290 - half, "WHY ADD THAT?", o.get("why_in") or f.get("why_in") or [], MUTED)
            block(tx, y + 92, half - tx - 10, "WHAT YOU LOSE", o.get("losses") or ["nothing measurable"], RED)
            block(half, y + 92, x1 - 290 - half, "WHAT YOU GAIN", o.get("gains") or [], GREEN)
            ch = o.get("changes") or {}
            if ch:
                p.text((tx, y + 142), "Roles: " + "; ".join(ch["roles"][:3]) + "   |   Curve: " + ch["curve"][0],
                       font="small", fill=TEXT, width=x1 - tx - 290)
                p.text((tx, y + 157), "Synergy: " + "; ".join(ch["synergy"][:2]) + "   |   Interactions: " +
                       ch["interactions"][0], font="small", fill=TEXT, width=x1 - tx - 290)
            p.text((tx, y + 173), "NET: " + (o.get("net") or ""), font="small", fill=YELLOW, width=x1 - tx - 290)
            p.button((x1 - 262, y + 34, x1 - 142, y + 62), "Swap this", None if sc.stale else ("swap", k),
                     style="disabled" if sc.stale else "primary", font="label")
            p.button((x1 - 134, y + 34, x1 - 34, y + 62), "Never cut", ("pin", k), font="label")
            y += box_h
        if len(opts) > vis:
            p.text((x0 + 24, y1 - 30), f"{self.scroll + 1}-{min(len(opts), self.scroll + vis)} of {len(opts)} options "
                                       "(scroll for more)", font="small", fill=MUTED, anchor="lm")
        p.button((x1 - 124, y1 - 46, x1 - 24, y1 - 14), "Close", ("btn", None))
        self.hits = p.hits
        return p.to_bgr()

    def wheel(self, delta):
        self.scroll = max(0, self.scroll + (-1 if delta > 0 else 1))

    def click(self, x, y):
        for x0, y0, x1, y1, a in self.hits:
            if not (x0 <= x <= x1 and y0 <= y <= y1 and a):
                continue
            kind, v = a
            sc = self.screen
            if kind == "btn":
                self.done = True
            elif kind == "view":
                self.done = True
                sc.view_card(sc.deck_printing(v))
            elif kind == "pin":
                sc.protect(self.found["options"][v]["out"], True)
                self.found["options"].pop(v)
            elif kind == "swap":
                o = self.found["options"][v]
                self.done = True
                sc.confirm_single_swap(o["out"], self.found["into"])
            return

    def key(self, key):
        if key == 27:
            self.done = True
