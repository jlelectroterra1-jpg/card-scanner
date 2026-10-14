"""'Build a deck from my Collection' - the Build side of the Analyse tab.

Left: commander (search or pick one you own), style (Casual / Balanced / High Power),
strategy (detected - Edit to choose your own), what may be used (free cards you own by
default; optionally cards used in other decks, or all cards), Build / Regenerate / Save.
Right: the built deck grouped by card type with lock toggles, curve, roles, mana, issues and
- when the collection can't make a sensible deck - the honest options. Click a card name for
'Why this card is here'. Nothing changes in your collection or decks until Save Deck."""
import threading

import deckbuilder
from decks import DeckStore
from decks_view import THUMB_H, THUMB_W, _wrap
from ui import BUTTON_HI, CARD_BG, GOLD, GREEN, MUTED, RED, TEXT, YELLOW, Dialog
from userdb import UserDB

STYLES = [("casual", "Casual"), ("balanced", "Balanced"), ("high", "High Power")]
STATUS = {"available": ("OWNED", GREEN), "in_use": ("OWNED - USED", YELLOW), "not_owned": ("NOT OWNED", RED),
          "basic": ("BASIC", MUTED)}
THEMES = ["Tokens", "Aristocrats", "Counters", "Proliferate", "Spellslinger", "Artifacts", "Enchantments", "Landfall",
          "Graveyard", "Reanimator", "Lifegain", "Voltron", "Equipment", "Auras", "Combat", "Blink", "Planeswalkers",
          "Creatures", "Tribal", "Control", "Big Mana"]


class BuildPanel:
    def __init__(self, screen):
        self.s = screen
        self.commander = None
        self.style = "balanced"
        self.tags = None          # None = the detected strategy
        self.scope = "free"
        self.locked = set()
        self.bias = None
        self.seed = 0
        self.fill_weaker = False
        self.pad_lands = False
        self.plan = None
        self.result = None
        self.running = None
        self._done = None
        self.scroll = 0
        self.visible = []

    @property
    def app(self):
        return self.s.app

    def settings(self):
        return dict(style=self.style, tags=self.tags, scope=self.scope, locked=sorted(self.locked), bias=self.bias,
                    seed=self.seed, fill_weaker=self.fill_weaker, pad_lands=self.pad_lands)

    # ---- background build ------------------------------------------------------------------
    def run(self):
        if self.running or not self.commander:
            return
        path, cmd, settings = self.app.userdb.path, self.commander, self.settings()
        self.running = dict(progress=0.0, text="Starting...")

        def progress(p, text):
            self.running = dict(progress=p, text=text)
            self.s.bump()

        def work():
            db = None
            try:
                db = UserDB(path)
                self._done = (deckbuilder.build(db, DeckStore(db), cmd, settings, progress), None)
            except Exception as e:  # noqa: BLE001
                self._done = (None, str(e))
            finally:
                if db is not None:
                    db.close()
                self.s.bump()
        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        done, self._done = self._done, None
        if done is None:
            return
        self.running = None
        res, err = done
        if err:
            self.app.toast(f"Couldn't build: {err}", RED)
        else:
            self.result, self.scroll = res, 0
            self.plan = res["plan"]
            self.app.toast(f"Built {res['total']} cards for {res['commander']}" if res["complete"] else
                           f"Only {res['total']} cards - see the options on the right", GREEN if res["complete"]
                           else YELLOW)
        self.s.bump()

    # ---- left panel ------------------------------------------------------------------------
    def left(self, p, x0, x1, y):
        p.text((x0, y), "BUILD A DECK FROM YOUR COLLECTION", font="label", fill=MUTED)
        y += 18
        if self.commander:
            cid, cimg = self.s.printing(self.commander)
            p.paste(self.s.images.small(cid, cimg), x0, y, 64, 89, radius=5)
            p.hit((x0, y, x0 + 64, y + 89), ("view", self.commander))
            for i, line in enumerate(_wrap(p, self.commander, "rowb", x1 - x0 - 76)[:2]):
                p.text((x0 + 76, y + i * 18), line, font="rowb")
        else:
            p.d.rounded_rectangle((x0, y, x0 + 64, y + 89), radius=5, fill=CARD_BG)
            p.text((x0 + 76, y + 4), "Choose a commander", font="rowb", fill=YELLOW)
        p.button((x0 + 76, y + 42, x0 + 196, y + 66), "Search card", ("b_cmd", None), font="label")
        p.button((x0 + 202, y + 42, x1, y + 66), "From my Collection", ("b_cmd_owned", None), font="label")
        y += 100
        p.text((x0, y), "DECK POWER / STYLE", font="label", fill=MUTED)
        self.s._seg(p, x0, y + 16, x1 - x0, STYLES, self.style, "b_style")
        y += 54
        p.text((x0, y), "STRATEGY" + ("  (your choice)" if self.tags else "  (detected)"), font="label", fill=MUTED)
        tags = self.tags or ((self.plan or {}).get("primary", []) + (self.plan or {}).get("secondary", [])[:2])
        cx = x0
        for t in (tags or ["choose a commander"])[:4]:
            if cx + p.f["label"].getlength(t) + 14 > x1 - 60:
                break
            cx = p.chip(cx, y + 18, t, YELLOW if self.tags else TEXT)
        p.button((x1 - 52, y + 16, x1, y + 40), "Edit", ("b_tags", None), font="label")
        y += 50
        p.text((x0, y), "USE", font="label", fill=MUTED)
        self.s._seg(p, x0, y + 16, x1 - x0, [("free", "Free cards I own"), ("used", "+ used in decks"),
                                             ("all", "All cards")], self.scope, "b_scope")
        y += 52
        if self.scope == "used":
            p.text((x0, y), "Cards used in other decks are marked OWNED - USED IN ... and never taken from them.",
                   font="small", fill=MUTED, width=x1 - x0)
        elif self.scope == "all":
            p.text((x0, y), "Any card - marked OWNED / OWNED BUT USED / NOT OWNED (a wishlist).", font="small",
                   fill=MUTED, width=x1 - x0)
        by = self.s.H - 120
        if self.running:
            r = self.running
            p.d.rounded_rectangle((x0, by, x1, by + 44), radius=10, fill=BUTTON_HI)
            p.d.rounded_rectangle((x0, by, x0 + max(20, (x1 - x0) * r["progress"]), by + 44), radius=10,
                                  fill=(52, 120, 76))
            p.text(((x0 + x1) / 2, by + 22), r["text"], font="button", anchor="mm")
        else:
            p.button((x0, by, x1, by + 44), "Rebuild Deck" if self.result else "Build Deck",
                     ("b_build", None) if self.commander else None, style="primary" if self.commander else "disabled",
                     font="name")
        half = (x1 - x0 - 6) / 2
        has = self.result is not None and not self.running
        p.button((x0, by + 52, x0 + half, by + 82), "Regenerate...", ("b_regen", None) if has else None,
                 style="normal" if has else "disabled", font="label")
        can_save = has and self.result["complete"]
        p.button((x0 + half + 6, by + 52, x1, by + 82), "Save Deck", ("b_save", None) if can_save else None,
                 style="primary" if can_save else "disabled", font="label")
        p.button((x0, by + 88, x1, by + 114), "Print Deck (Word)", ("b_print", None) if has else None,
                 style="normal" if has else "disabled", font="label")

    # ---- right panel -------------------------------------------------------------------------
    def rows(self):
        res = self.result
        rows, last = [], None
        for c in res["cards"]:
            if c["section"] != last:
                n = sum(x["qty"] for x in res["cards"] if x["section"] == c["section"])
                rows.append(("section", c["section"], n))
                last = c["section"]
            rows.append(("card", c))
        return rows

    def right(self, p, x0, x1, top):
        res = self.result
        if self.running:
            p.text(((x0 + x1) / 2, self.s.H / 2), self.running["text"], font="name", anchor="mm")
            return
        if not res:
            msg = ["Choose a commander, a style and (optionally) your own strategy, then press Build Deck.",
                   "The builder works in stages - engine pieces, then ramp / draw / interaction / protection, then the",
                   "best synergy for the remaining slots, then a mana base for the colours the cards actually need -",
                   "using only cards you own and haven't put in other decks. Nothing is saved until you press Save Deck."]
            for i, line in enumerate(msg):
                p.text((x0, top + 20 + i * 22), line, font="body", fill=TEXT if i == 0 else MUTED)
            return
        plan = res["plan"]
        head = f"{res['commander']}  -  {res['total']} cards  -  " + \
            "Primary: " + ", ".join(plan["primary"] or ["?"]) + \
            ("  -  Secondary: " + ", ".join(plan["secondary"][:3]) if plan["secondary"] else "") + \
            (f"  -  {plan['tribe']} tribe" if plan.get("tribe") else "")
        p.text((x0, top + 6), head, font="rowb", width=x1 - x0)
        rc = res["role_counts"]
        p.text((x0, top + 26), f"Average mana value {res['avg_mv']:.2f}  -  lands {res['lands']['lands']}  -  "
                               f"ramp {rc['Ramp']}  draw {rc['Card Draw']}  removal {rc['Removal']}  wipes "
                               f"{rc['Board Wipe']}  protection {rc['Protection']}  finishers {rc['Finisher']}  -  "
                               f"value {self.s.money(res['value'])}  -  "
                               + ", ".join(f"{STATUS[k][0].lower()} {v}" for k, v in res["owned"].items() if k in STATUS),
               font="small", fill=MUTED, width=x1 - x0)
        curve = res["curve"]
        p.text((x0, top + 42), "Curve: " + "  ".join(f"{k if k < 6 else '6+'}:{curve.get(str(k), curve.get(k, 0))}"
                                                     for k in range(7)), font="small", fill=MUTED)
        y = top + 60
        if not res["complete"]:
            p.d.rounded_rectangle((x0, y, x1, y + 62), radius=8, fill=(60, 44, 26))
            p.text((x0 + 10, y + 6), f"I could build {res['strong_cards']} strong cards from your available "
                                     f"Collection - the deck has {res['total']} of 100.", font="rowb", fill=YELLOW)
            bx = x0 + 10
            for label, action in (("Fill with weaker owned cards", "b_weaker"), ("Add basic lands", "b_basics"),
                                  ("Allow cards used in decks", "b_used"), ("Switch to All Cards", "b_all")):
                w = p.f["label"].getlength(label) + 24
                p.button((bx, y + 30, bx + w, y + 54), label, (action, None), font="label")
                bx += w + 6
            y += 68
        issues = [i for i in res["issues"]]
        for i in issues[:2]:
            p.text((x0, y), ("! " if i["level"] == "error" else "- ") + i["text"], font="small",
                   fill=RED if i["level"] == "error" else YELLOW, width=x1 - x0)
            y += 16
        if res["shortfalls"]:
            p.text((x0, y), "Your collection was short of: " + ", ".join(
                f"{s['stage'].lower()} ({s['missing']})" for s in res["shortfalls"][:5]), font="small", fill=MUTED,
                   width=x1 - x0)
            y += 16
        y += 4
        rows = self.rows()
        heights = {"section": 22, "card": THUMB_H + 6}
        self.scroll = max(0, min(self.scroll, max(0, len(rows) - 1)))
        col_w = x1 - x0
        self.visible = []  # card names drawn this frame (only these load a picture)
        i = self.scroll
        while i < len(rows) and y + heights[rows[i][0]] <= self.s.H - 8:
            r = rows[i]
            h = heights[r[0]]
            if r[0] == "section":
                p.text((x0 + 2, y + 11), f"{r[1].upper()} ({r[2]})", font="label", fill=GOLD, anchor="lm")
            else:
                c = r[1]
                self.visible.append(c["name"])
                mid = y + h // 2
                # [thumbnail] [Lock] [Qty] [Card Name] [MV] [Role] [Ownership]
                img = self.s.images.small(c.get("scryfall_id"), c.get("image_small"), THUMB_W, THUMB_H)
                p.paste(img, x0, y + (h - THUMB_H) // 2, THUMB_W, THUMB_H, radius=3)
                if c.get("scryfall_id"):
                    p.hit((x0, y + (h - THUMB_H) // 2, x0 + THUMB_W, y + (h + THUMB_H) // 2), ("viewid", c["scryfall_id"]))
                tx = x0 + THUMB_W + 8
                locked = c["name"] in self.locked or c["locked"]
                if c["section"] != "Lands" or c["qty"] == 1:
                    p.button((tx, mid - 9, tx + 44, mid + 9), "Lock" if not locked else "\u2713 Lock",
                             ("b_lock", c["name"]), font="label", style="selected" if locked else "ghost")
                p.text((tx + 52, mid), f"{c['qty']}", font="small", fill=MUTED, anchor="lm")
                p.text((tx + 68, mid), c["name"], font="row", anchor="lm", width=300)
                p.hit((tx + 68, mid - 10, tx + 368, mid + 10), ("b_why", c["name"]))
                p.text((tx + 376, mid), f"{int(c['cmc'])}" if c["section"] != "Lands" else "", font="small",
                       fill=MUTED, anchor="lm")
                p.text((tx + 400, mid), c["stage"], font="small", fill=MUTED, anchor="lm", width=200)
                label, colour = STATUS.get(c["status"], ("", MUTED))
                p.text((tx + 610, mid), c["label"] if c["status"] == "in_use" else label, font="small",
                       fill=colour, anchor="lm", width=max(10, col_w - (tx - x0) - 620))
            y += h
            i += 1
        vis = i - self.scroll
        if len(rows) > vis:
            p.text((x1, self.s.H - 4), f"{self.scroll + 1}-{min(len(rows), self.scroll + vis)} of {len(rows)} "
                                       "(scroll)", font="small", fill=MUTED, anchor="rs")

    # ---- input ---------------------------------------------------------------------------
    def click(self, kind, v):
        if kind == "b_cmd":
            self.choose_commander()
        elif kind == "b_cmd_owned":
            self.choose_owned_commander()
        elif kind == "b_style":
            self.style = v
        elif kind == "b_scope":
            self.scope = v
        elif kind == "b_tags":
            self.edit_tags()
        elif kind == "b_build":
            self.run()
        elif kind == "b_regen":
            self.regenerate()
        elif kind == "b_save":
            self.save()
        elif kind == "b_print":
            self.print_deck()
        elif kind == "b_lock":
            self.locked ^= {v}
        elif kind == "b_why":
            self.why(v)
        elif kind == "b_weaker":
            self.fill_weaker = True
            self.run()
        elif kind == "b_basics":
            self.pad_lands = True
            self.run()
        elif kind == "b_used":
            self.scope = "used"
            self.run()
        elif kind == "b_all":
            self.scope = "all"
            self.run()
        self.s.bump()

    def wheel(self, delta):
        self.scroll = max(0, self.scroll + (-3 if delta > 0 else 3))

    def set_commander(self, name):
        if not name:
            return
        self.commander, self.result, self.locked, self.seed, self.bias = name, None, set(), 0, None
        self.fill_weaker = self.pad_lands = False
        try:
            self.plan = deckbuilder.strategy_for(self.app.userdb, self.s.store, name, self.settings())
        except deckbuilder.BuildError as e:
            self.commander, self.plan = None, None
            self.app.toast(str(e), RED)
        self.s.bump()

    def choose_commander(self, text="", problem=None):
        def done(ok, t):
            name = (t or "").strip()
            if not ok or not name:
                return
            matches = self.s.search_names(name)
            exact = [m for m in matches if m.lower() == name.lower()]
            if exact or len(matches) == 1:
                return self.set_commander((exact or matches)[0])
            if not matches:
                return self.choose_commander(name, f'No card called "{name}" found.')
            self.app.open_dialog(Dialog("Which commander?", kind="choice", options=[(m, m, "normal") for m in matches],
                                        buttons=[("Cancel", None, "normal")],
                                        on_done=lambda v, _t: v is not None and self.set_commander(v)))
        self.app.open_dialog(Dialog("Choose a commander", ([problem] if problem else []) + ["Type its name."],
                                    kind="input", text=text, placeholder="Commander name",
                                    buttons=[("Cancel", None, "normal"), ("Choose", True, "primary")], on_done=done))

    def choose_owned_commander(self):
        owned = deckbuilder.owned_commanders(self.app.userdb, self.s.store)

        def done(ok, t):
            if not ok:
                return
            q = (t or "").strip().lower()
            names = [n for n in owned if q in n.lower()][:14]
            if not names:
                return self.app.toast(f'None of your legendary creatures match "{t}"', YELLOW)
            self.app.open_dialog(Dialog("Your commanders", kind="choice", options=[(n, n, "normal") for n in names],
                                        buttons=[("Cancel", None, "normal")],
                                        on_done=lambda v, _t: v is not None and self.set_commander(v)))
        self.app.open_dialog(Dialog("Choose from my Collection", [f"You own {len(owned)} cards that can be a "
                                                                  "commander. Search (leave empty for A-Z)."],
                                    kind="input", text="", placeholder="Name (optional)",
                                    buttons=[("Cancel", None, "normal"), ("Search", True, "primary")], on_done=done))

    def edit_tags(self):
        cur = ", ".join(self.tags or ((self.plan or {}).get("primary", []) + (self.plan or {}).get("secondary", [])))

        def done(ok, t):
            if not ok:
                return
            chosen = [x.strip().title() for x in (t or "").split(",") if x.strip()]
            chosen = [next((th for th in THEMES if th.lower() == c.lower()), c) for c in chosen]
            self.tags = [c for c in chosen if c in THEMES] or None
            self.s.bump()
        self.app.open_dialog(Dialog("Strategy", ["Themes in priority order, separated by commas (first = primary). "
                                                 "Leave empty to use the detected strategy.",
                                                 "Choose from: " + ", ".join(THEMES)],
                                    kind="input", text=cur, buttons=[("Cancel", None, "normal"), ("Use", True, "primary")],
                                    on_done=done))

    def regenerate(self):
        opts = [("same", "Same strategy", "normal")] + [(k, v, "normal") for k, v in deckbuilder.BIASES.items()]

        def done(v, _t):
            if v is None:
                return
            self.bias = None if v == "same" else v
            self.seed += 1
            self.run()
        self.app.open_dialog(Dialog("Regenerate (locked cards stay)", kind="choice", options=opts,
                                    buttons=[("Cancel", None, "normal")], on_done=done))

    def why(self, name):
        c = next((x for x in self.result["cards"] if x["name"] == name), None)
        if c is None:
            return
        lines = [f"Chosen for: {c['stage']}"] + [("- " + w) for w in c["why"][:5]]
        if c["roles"]:
            lines.append("Jobs: " + ", ".join(c["roles"][:4]))
        lines.append(c["label"])
        self.app.open_dialog(Dialog(f"Why {name} is here", lines, buttons=[("OK", True, "primary")]))

    def print_deck(self):
        """Print the BUILT deck (the commander + the printings the builder chose), not a saved one."""
        import printdeck
        from print_view import PrintDialog
        items = printdeck.items_from_build(self.s.store, self.result)
        self.app.open_dialog(PrintDialog(self.app, items, self.result["commander"].split(",")[0] + " (built)"))

    def save(self):
        res = self.result

        def done(ok, t):
            name = (t or "").strip()
            if not ok or not name:
                return
            try:
                deck = deckbuilder.save(self.app.userdb, self.s.store, res, name)
            except Exception as e:  # noqa: BLE001
                return self.app.toast(f"Not saved: {e}", RED)
            self.app.toast(f"Saved {name} - free collection copies were linked", GREEN)
            self.s.refresh()
            self.s.select_deck(deck)
        self.app.open_dialog(Dialog("Save as a new deck", ["Creates a new deck and links FREE copies from your "
                                                           "collection (never copies other decks use). Your "
                                                           "collection itself doesn't change."],
                                    kind="input", text=f"{res['commander'].split(',')[0]} (built)",
                                    buttons=[("Cancel", None, "normal"), ("Save", True, "primary")], on_done=done))
