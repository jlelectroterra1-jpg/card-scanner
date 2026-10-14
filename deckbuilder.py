"""Build a Commander deck from your Collection.

COMMANDER -> PLAN -> ENGINES -> ROLES -> FLEX -> MANA BASE -> CURVE -> VALIDATE.
Never "sort every legal card by score and take 99": every stage knows what the earlier ones
added, and each card is chosen FOR a job (an engine piece, ramp, draw, removal...).

Reuses the analyser's understanding: commander.profile (what the commander does / needs),
strategy.detect (or your own tags - they always win), analyser.deck_targets (ranges adjusted
to the commander, curve and plan), recommend (engine pieces, card quality, commander fit),
enablement (a payoff needs its enablers), synergy (interaction QUALITY) and community
(optional cached signals - never decisive).

build(udb, store, commander_name, settings) -> result (JSON-able):
  settings: style 'casual' | 'balanced' | 'high'; tags [themes] or None (detected);
            scope 'free' (default: free copies you own) | 'used' (+ copies used in other decks,
            marked) | 'all' (any card - marked OWNED / OWNED BUT USED / NOT OWNED);
            locked [card names kept as they are]; bias None | 'aggressive' | 'control' |
            'low_curve' | 'tribal' | 'combo' | 'casual'; seed (regenerate variety);
            fill_weaker (bool) - fill missing slots with weaker owned cards instead of stopping.
Nothing in your collection or decks changes until save() is called."""
import math
import random
import re
from collections import Counter

import analyser
import cardroles
import commander as cmdr
import community
import deckrules
import enablement
import recommend
import strategy
import synergy

STYLE_GOAL = {"casual": "casual", "balanced": "improve", "high": "high"}
STYLE = {
    # assumed average mana value (for the land / ramp targets), score weights, curve limits
    "casual": dict(mv=3.5, w_quality=0.25, w_synergy=0.40, staple=-0.06, big_mv=6, big_cap=16, cheap_min=10),
    "balanced": dict(mv=3.15, w_quality=0.33, w_synergy=0.33, staple=0.0, big_mv=6, big_cap=12, cheap_min=14),
    "high": dict(mv=2.8, w_quality=0.42, w_synergy=0.28, staple=0.04, big_mv=5, big_cap=8, cheap_min=20),
}
BIASES = {"aggressive": "More aggressive", "control": "More control", "low_curve": "Lower mana curve",
          "tribal": "More tribal", "combo": "More combo", "casual": "More casual"}
ROLE_STAGES = ["Ramp", "Card Draw", "Removal", "Board Wipe", "Counterspell", "Protection", "Recursion", "Tutor",
               "Finisher"]
STAGE_LABEL = {"Ramp": "Ramp", "Card Draw": "Card draw", "Removal": "Targeted interaction",
               "Board Wipe": "Board wipe", "Counterspell": "Counterspell", "Protection": "Protection",
               "Recursion": "Recursion", "Tutor": "Tutor", "Finisher": "Finisher"}
MIN_SCORE = 0.30      # below this a card is filler - the builder stops and says so (unless fill_weaker)
SECTIONS = ["Creatures", "Planeswalkers", "Artifacts", "Enchantments", "Instants", "Sorceries", "Battles", "Lands"]
BASIC_FOR = {"W": "Plains", "U": "Island", "B": "Swamp", "R": "Mountain", "G": "Forest"}
LAND_TYPE_COLOUR = {"plains": "W", "island": "U", "swamp": "B", "mountain": "R", "forest": "G"}


class BuildError(Exception):
    pass


def land_colours(info, identity):
    """Colours (within the identity) a land can produce."""
    text = synergy._text(info)
    tl = (info.get("type_line") or "").lower()
    out = set()
    for sym in re.findall(r"\{([wubrg])\}", text):
        out.add(sym.upper())
    for t, c in LAND_TYPE_COLOUR.items():
        if t in tl or re.search(rf"search your library for [^.]*\b{t}\b", text):
            out.add(c)
    if re.search(r"(?:mana )?of any (?:one )?colou?r|any colou?r of mana|mana of any type|chosen colou?r", text):
        out |= set(identity)
    if re.search(r"search your library for a basic land", text):
        out |= set(identity)
    return out & set(identity)


def enters_tapped(info):
    t = synergy._text(info)
    return bool(re.search(r"enters tapped|enters the battlefield tapped", t)) and "unless" not in t


def section_of(info):
    tl = (info.get("type_line") or "").split(" // ")[0]
    for word, sec in (("Land", "Lands"), ("Creature", "Creatures"), ("Planeswalker", "Planeswalkers"),
                      ("Battle", "Battles"), ("Instant", "Instants"), ("Sorcery", "Sorceries"),
                      ("Artifact", "Artifacts"), ("Enchantment", "Enchantments")):
        if word in tl:
            return sec
    return "Other"


class Builder:
    def __init__(self, udb, store, commander_name, settings):
        self.udb, self.store = udb, store
        self.settings = dict(style="balanced", tags=None, scope="free", locked=[], bias=None, seed=0,
                             fill_weaker=False)
        self.settings.update(settings or {})
        self.style = STYLE[self.settings["style"]]
        self.cd = cd = analyser.card_data(udb)
        info = cd.info(commander_name)
        if info is None:
            raise BuildError(f"Unknown card: {commander_name}")
        if not re.search(r"legendary (?:[a-z]+ )*creature|can be your commander", (info.get("type_line") or "").lower()
                         + " " + synergy._text(info)):
            raise BuildError(f"{info['name']} can't be a commander (not a legendary creature)")
        self.cmd = info
        self.identity = deckrules.colour_identity(info.get("color_identity"))
        self.rng = random.Random(self.settings.get("seed") or 0)
        self.ownership = analyser.ownership_map(udb, deck_id=-1)
        self.comm = community.load(community.commander_key([info.get("oracle_id")]))
        self.pool = self._pool()
        self.chosen = {}       # name -> dict(stage, why, score, locked)
        self.notes = []

    # ---- what can go in ---------------------------------------------------------------------
    def _pool(self):
        scope = self.settings["scope"]
        ident = set(self.identity)
        out = {}
        names = self.cd.cards if scope == "all" else self.ownership
        for name in names:
            info = self.cd.info(name)
            if info is None or info["name"] == self.cmd["name"] or info.get("legal_commander") != "legal":
                continue
            if not set(info.get("color_identity") or "") <= ident:
                continue
            if deckrules.is_basic(info["name"], info.get("type_line")):
                continue  # basics are added by the mana base
            own = self.ownership.get(info["name"])
            status = "available" if own and own["free"] > 0 else "in_use" if own else "not_owned"
            if scope == "free" and status != "available":
                continue
            if scope == "used" and status == "not_owned":
                continue
            if scope == "all" and status == "not_owned" and analyser._obscure(self.cd, info["name"]):
                continue
            out[info["name"]] = status
        return out

    def ownership_label(self, name):
        own = self.ownership.get(name)
        status = self.pool.get(name) or ("basic" if deckrules.is_basic(name) else "not_owned")
        if status == "available":
            return status, f"OWNED - {own.get('free_in') or own['collection']}"
        if status == "in_use":
            return status, "OWNED - USED IN " + (", ".join(own["used_in"][:2]) or "another deck").upper()
        if status == "basic":
            return status, "Basic land"
        return status, "NOT OWNED"

    # ---- the deck so far, understood like any other deck ---------------------------------------
    def rows(self):
        rows = [dict(card_name=self.cmd["name"], role="commander", quantity=1, type_line=self.cmd.get("type_line"),
                     color_identity=self.cmd.get("color_identity"))]
        for n in self.chosen:
            i = self.cd.info(n) or {}
            rows.append(dict(card_name=n, role="main", quantity=self.chosen[n].get("qty", 1),
                             type_line=i.get("type_line"), color_identity=i.get("color_identity")))
        return rows

    def understand(self):
        goal = STYLE_GOAL[self.settings["style"]]
        if self.settings.get("bias") == "casual":
            goal = "casual"
        self.st_settings = dict(mode="all", goal=goal, tags=self.settings.get("tags"), budget_usd=None)
        self.st = analyser.prepare_rows(self.cd, self.rows(), self.st_settings, review=False,
                                        tribe=getattr(self, "tribe", None))
        self.rec = recommend.Recommender(self.st, self.st_settings, {}, self.comm)
        self.graph = self.st.graph

    # ---- strategy and targets ---------------------------------------------------------------
    def plan(self):
        detected = strategy.detect([self.cmd])
        roles = cardroles.classify(self.cmd)
        prof = cmdr.profile([self.cmd], None, detected.get("tribe"))
        supplies = [r for r in ("Card Draw", "Ramp", "Removal", "Token Generation", "Sacrifice Outlet", "Finisher",
                                "Recursion", "Protection") if r in roles and roles[r].confidence >= 0.5]
        tags = self.settings.get("tags")
        if not detected.get("tribe") and ((tags and "Tribal" in tags) or self.settings.get("bias") == "tribal"):
            detected["tribe"] = self._best_tribe()
        primary = tags[:1] if tags else detected["primary"][:1] or ([prof["primary"]] if prof.get("primary") else [])
        secondary = tags[1:] if tags else [t for t in detected["primary"][1:] + detected["secondary"] if t][:3]
        return dict(primary=primary, secondary=secondary, tribe=detected.get("tribe"), user_tags=bool(tags),
                    mechanics=[m["label"] for m in prof["mechanics"]], chain=prof["chain"], supplies=supplies,
                    plan_name=prof["primary_name"], identity=self.identity,
                    reasons={k: v[:2] for k, v in detected["reasons"].items()}, scores=dict(detected["scores"]))

    def _best_tribe(self):
        """For Tribal without a tribe in the commander's text: the commander's own creature type
        your collection has the most of."""
        types = cardroles.creature_types(self.cmd.get("type_line"))
        counts = Counter()
        for n in self.pool:
            for t in cardroles.creature_types((self.cd.info(n) or {}).get("type_line")) & types:
                counts[t] += 1
        return counts.most_common(1)[0][0] if counts else None

    def targets(self, avg_mv=None):
        """Deck-specific ranges (with reasons), from the analyser's deck_targets."""
        ctx = self.st.ctx
        comp = dict(self.st.comp, avg_mv=avg_mv or self.style["mv"] - (0.3 if self.settings.get("bias") == "low_curve"
                                                                       else 0.0))
        t = analyser.deck_targets(ctx, comp, self.st.profile, self.graph)
        bias = self.settings.get("bias")
        if bias == "control":
            for r in ("Removal", "Board Wipe", "Counterspell"):
                if r in t:
                    t[r]["target"] = [t[r]["target"][0] + 1, t[r]["target"][1] + 2]
                    t[r]["why"].append("more control (your choice)")
        if bias == "aggressive" and "Board Wipe" in t:
            t["Board Wipe"]["target"] = [0, max(1, t["Board Wipe"]["target"][1] - 1)]
            t["Board Wipe"]["why"].append("more aggressive (your choice)")
        if bias == "combo" and "Tutor" in t:
            t["Tutor"]["target"] = [t["Tutor"]["target"][0] + 2, t["Tutor"]["target"][1] + 2]
            t["Tutor"]["why"].append("more combo (your choice)")
        return t

    # ---- scoring one candidate for one job ----------------------------------------------------
    def score(self, name, job=None):
        cd, rec = self.cd, self.rec
        info = cd.info(name)
        roles = cd.roles.get(name, {})
        res = recommend.card_resources(cd, name, info, roles)
        links = self.graph.candidate_links(info, roles)
        syn = 1 - math.exp(-synergy.link_value(links, self.rec.cmd_names) / 6)
        fit = rec.commander_fit(res)
        tribe = self.st.ctx.tribe
        is_tribe = bool(tribe and tribe in cardroles.creature_types(info.get("type_line")))
        if is_tribe:
            fit = max(fit, 0.8 if rec.cmd_tribal else 0.4)
        q = rec.card_quality(name)
        ev = rec.support.evaluate(info, roles)
        share = enablement.payoff_share(info, roles, ev)
        unsupported = share * (1 - ev["enabled"]) if ev["enabled"] is not None else 0.0
        cmc = float(info.get("cmc") or 0)
        s = self.style["w_quality"] * q + self.style["w_synergy"] * syn + 0.15 * fit
        # what the deck still needs: a card doing a missing job (besides this one) is worth more
        open_roles = [r for r in ROLE_STAGES if r in roles and roles[r].confidence >= 0.6 and r != job
                      and self.counts.get(r, 0) < self.goal_counts.get(r, 0)]
        s += 0.05 * min(2, len(open_roles))
        # mana value: style and bias
        big = self.style["big_mv"] - (1 if self.settings.get("bias") == "low_curve" else 0)
        if cmc >= big:
            s -= 0.05 * (cmc - big + 1)
        if self.settings.get("bias") == "low_curve" and cmc <= 2:
            s += 0.03
        rank = cd.popularity.get(name)
        if (rank and rank <= 100) or info.get("game_changer"):
            s += self.style["staple"] + (-0.06 if self.settings.get("bias") == "casual" else 0)
        bias = self.settings.get("bias")
        if bias == "tribal" and is_tribe:
            s += 0.08
        if bias == "aggressive" and ("attacking" in res[1] or "attacking" in res[0] or "Anthem" in roles):
            s += 0.05
        if bias == "control" and set(roles) & {"Removal", "Board Wipe", "Counterspell"}:
            s += 0.04
        for e in self.comm.get(name, []):  # optional community evidence: small, never decisive
            if e["kind"] == "combo piece":
                s += 0.08 if bias == "combo" else 0.02
            elif e["source"] == "recommander":
                s += 0.03 * e["metric"]
        s -= 0.35 * unsupported
        if self.settings.get("seed"):
            s += self.rng.uniform(-0.03, 0.03)  # regenerate: a different, equally sensible deck
        return s, dict(links=links, fit=fit, quality=q, enablement=ev, unsupported=unsupported, cmc=cmc,
                       is_tribe=is_tribe, res=res, roles=roles, info=info)

    # ---- choosing ----------------------------------------------------------------------------
    def add(self, name, stage, why, score, locked=False):
        self.chosen[name] = dict(stage=stage, why=why, score=round(score, 3), locked=locked)
        for r, v in self.cd.roles.get(name, {}).items():
            if v.confidence >= 0.5:
                self.counts[r] += 1
        if self.st.ctx.tribe and self.st.ctx.tribe in cardroles.creature_types((self.cd.info(name) or {}).get(
                "type_line")):
            self.counts["tribe"] += 1

    def fill(self, stage, want, matches, why_fn, limit_pool=None):
        """Add up to `want` cards for one job (each re-scored against the deck so far),
        stopping when the best remaining card isn't worth a slot."""
        added = 0
        cands = [n for n in (limit_pool or self.pool) if n not in self.chosen
                 and section_of(self.cd.info(n)) != "Lands" and matches(n)]
        scored = []
        for n in cands:
            s, f = self.score(n, stage)
            scored.append((s, n, f))
        scored.sort(key=lambda x: -x[0])
        seen_funcs = Counter()
        floor = MIN_SCORE if not self.settings.get("fill_weaker") else -1
        for s, n, f in scored:
            if added >= want or self.nonland_room() <= 0:
                break
            if s < floor:
                self.shortfalls.append((stage, want - added))
                break
            funcs = sorted(x for _, x in synergy.flat_functions(f["info"], f["roles"]))[:1]
            key = funcs[0] if funcs else None
            if key and seen_funcs[key] >= 3 and len(scored) > want * 2:
                continue  # variety: not four copies of the same effect for one job
            seen_funcs[key] += 1
            self.add(n, stage, why_fn(n, f), s)
            added += 1
        return added

    def nonland_room(self):
        nonland = sum(1 for n in self.chosen if section_of(self.cd.info(n) or {}) != "Lands")
        return self.nonland_target - nonland

    # ---- the stages ---------------------------------------------------------------------------
    def build(self, progress=None):
        say = progress or (lambda p, t: None)
        self.counts = Counter()
        self.shortfalls = []
        say(0.05, "Understanding the commander...")
        self.understand()
        plan = self.plan()
        self.tribe = plan["tribe"]
        self.understand()
        tg = self.targets()
        lands_lo, lands_hi = tg["Lands"]["target"]
        floor = {0: 30, 1: 31, 2: 33, 3: 34}.get(len(self.identity), 35) + (1 if self.settings["style"] == "casual" else 0)
        self.lands_target = max((lands_lo + lands_hi) // 2, floor)  # more colours need more lands
        self.nonland_target = 99 - self.lands_target
        pick = {"casual": 0.35, "balanced": 0.5, "high": 0.6}[self.settings["style"]]
        self.goal_counts = {r: int(round(v["target"][0] + pick * (v["target"][1] - v["target"][0])))
                            for r, v in tg.items() if r in ROLE_STAGES}
        if self.settings["style"] != "high" and self.settings.get("bias") != "combo":
            self.goal_counts["Tutor"] = min(self.goal_counts.get("Tutor", 0), 1)
        # A. locked cards first - they stay whatever happens
        for n in self.settings.get("locked") or []:
            info = self.cd.info(n)
            if info and info["name"] != self.cmd["name"] and set(info.get("color_identity") or "") <= set(
                    self.identity) and not deckrules.is_basic(info["name"]):
                self.add(info["name"], "Locked", ["You locked this card"], 1.0, locked=True)
        # B. engines: the plan's providers first, then the payoffs they feed
        say(0.2, "Building the engine...")
        self.understand()
        pieces = self._engine_pieces(plan)
        for p, want, label in pieces:
            have = sum(1 for n in self.chosen if recommend._has_piece(
                recommend.card_resources(self.cd, n, self.cd.info(n), self.cd.roles.get(n, {})), p,
                self.cd.info(n), self.cd.roles.get(n, {})))
            if have >= want:
                continue
            self.fill(f"Engine: {label}", want - have, lambda n, p=p: recommend._has_piece(
                recommend.card_resources(self.cd, n, self.cd.info(n), self.cd.roles.get(n, {})), p, self.cd.info(n),
                self.cd.roles.get(n, {})), lambda n, f, label=label: self._why(n, f, f"{label}"))
            self.understand()  # payoffs are judged against the providers just added
        if plan["tribe"] and (self.rec.cmd_tribal or "Tribal" in (self.settings.get("tags") or [])
                              or self.settings.get("bias") == "tribal"):
            want = recommend.TRIBE_ENOUGH + (5 if self.settings.get("bias") == "tribal" else 0)
            t = plan["tribe"]
            self.fill(f"{t} tribe", want - self.counts["tribe"], lambda n: t in cardroles.creature_types(
                (self.cd.info(n) or {}).get("type_line")), lambda n, f: self._why(n, f, f"Another {t}"))
            self.understand()
        # C. roles: ramp, draw, interaction, wipes, protection, recursion, tutors, finishers
        say(0.5, "Filling the deck's jobs...")
        for role in ROLE_STAGES:
            want = self.goal_counts.get(role, 0) - self.counts[role]
            if want <= 0:
                continue
            self.fill(STAGE_LABEL[role], want, lambda n, role=role: (self.cd.roles.get(n, {}).get(role) is not None
                                                                      and self.cd.roles[n][role].confidence >= 0.6
                                                                      and section_of(self.cd.info(n)) != "Lands"),
                      lambda n, f, role=role: self._why(n, f, STAGE_LABEL[role]))
        self.understand()
        # D. flexible synergy slots
        say(0.65, "Adding the best synergy for the remaining slots...")
        room = self.nonland_room()
        if room > 0:
            self.fill("Synergy", room, lambda n: section_of(self.cd.info(n)) != "Lands",
                      lambda n, f: self._why(n, f, "Synergy with the deck"), limit_pool=self._flex_pool())
        self.understand()
        # E. curve, then F. unsupported / dead cards out
        say(0.75, "Balancing the curve...")
        self._balance_curve()
        self._repair()
        # G. mana base for the spells actually chosen
        say(0.85, "Building the mana base...")
        lands = self._mana_base()
        say(0.95, "Checking the deck...")
        return self._result(plan, tg, lands)

    def _engine_pieces(self, plan):
        themes = (self.settings.get("tags") or plan["primary"] + plan["secondary"][:1])
        bias = self.settings.get("bias")
        out, seen = [], set()
        for t in themes:
            for p in recommend.PLAN_PIECES.get(t, []):
                if p in seen:
                    continue
                seen.add(p)
                res, side, enough, sing, plural, cat, partner = recommend.PIECES[p]
                want = enough if t in themes[:1] else max(2, enough // 2)
                if p in recommend.AMPLIFIERS:
                    want = 2
                if side == "rew":
                    want = min(want, 6)
                if bias == "aggressive" and p in ("attack_payoffs", "attackers", "wide_payoffs"):
                    want += 2
                out.append((p, want, cat.title()))
        # the commander's own inputs (what it consumes) are always part of the plan
        for n in self.st.profile.get("needs", []):
            key = n["key"]
            for p, spec in recommend.PIECES.items():
                if p not in seen and spec[0] == key and spec[1] in ("prov", "any"):
                    seen.add(p)
                    out.append((p, min(spec[2], 8), spec[5].title()))
        # providers before payoffs, so payoffs are judged on real support
        out.sort(key=lambda x: recommend.PIECES[x[0]][1] == "rew")
        return out

    def _flex_pool(self):
        """A cheap first cut for the flexible slots (quality + commander fit), so the full
        scoring only runs on plausible cards."""
        pre = []
        for n in self.pool:
            if n in self.chosen:
                continue
            info = self.cd.info(n)
            if section_of(info) == "Lands":
                continue
            res = recommend.card_resources(self.cd, n, info, self.cd.roles.get(n, {}))
            pre.append((self.rec.card_quality(n) + 0.4 * self.rec.commander_fit(res), n))
        pre.sort(reverse=True)
        return [n for _, n in pre[:400]]

    def _why(self, name, f, job):
        """Plain-English: why this card is here."""
        out = [f"{job}" + (f": {self._job_detail(name, f)}" if self._job_detail(name, f) else "")]
        cmd_links = [l for l in f["links"] if l[0] in self.rec.cmd_names]
        if cmd_links:
            out.append(self.rec._commander_line(f["res"], cmd_links[0]))
        strong = [o for o, _, s in f["links"] if s == "strong" and o not in self.rec.cmd_names]
        if strong:
            out.append(f"Works directly with {len(strong)} cards already chosen (e.g. {', '.join(strong[:3])})")
        if f["enablement"]["text"] and not f["enablement"]["unsupported"]:
            out.append(f["enablement"]["text"])
        rank = self.cd.popularity.get(name)
        if rank and rank <= 500:
            out.append(f"Widely played in Commander (rank #{rank})")
        return out

    def _job_detail(self, name, f):
        funcs = synergy.functions(f["info"], f["roles"])
        subs = [s for subs in funcs.values() for s in subs][:2]
        return ", ".join(subs)

    def _balance_curve(self):
        """Too many expensive cards / too few early plays: swap the weakest offenders for the
        best cheaper cards that do a job the deck needs."""
        st = self.style
        for _ in range(12):
            nonland = [n for n in self.chosen if section_of(self.cd.info(n)) != "Lands"]
            big = [n for n in nonland if float(self.cd.info(n).get("cmc") or 0) >= st["big_mv"] + 0
                   and not self.chosen[n]["locked"]]
            cheap = [n for n in nonland if float(self.cd.info(n).get("cmc") or 0) <= 2]
            if len(big) <= st["big_cap"] and len(cheap) >= st["cheap_min"]:
                return
            pool = [n for n in self.pool if n not in self.chosen and section_of(self.cd.info(n)) != "Lands"
                    and float(self.cd.info(n).get("cmc") or 0) <= 3]
            if not pool:
                return
            victims = sorted(big, key=lambda n: self.chosen[n]["score"]) if len(big) > st["big_cap"] else sorted(
                [n for n in nonland if float(self.cd.info(n).get("cmc") or 0) >= 4 and not self.chosen[n]["locked"]],
                key=lambda n: self.chosen[n]["score"])
            if not victims:
                return
            out = victims[0]
            out_roles = {r for r, v in self.cd.roles.get(out, {}).items() if v.confidence >= 0.6 and r in ROLE_STAGES}
            best = None
            for n in pool:
                roles = {r for r, v in self.cd.roles.get(n, {}).items() if v.confidence >= 0.6}
                if out_roles and not (roles & out_roles):
                    continue
                s, f = self.score(n, None)
                if best is None or s > best[0]:
                    best = (s, n, f)
            if best is None or best[0] < self.chosen[out]["score"] - 0.12:
                return
            self._replace(out, best[1], best[0], best[2], "Curve: a cheaper card for the same job")

    def _replace(self, out, into, score, f, why):
        old = self.chosen.pop(out)
        for r, v in self.cd.roles.get(out, {}).items():
            if v.confidence >= 0.5:
                self.counts[r] -= 1
        self.add(into, old["stage"], self._why(into, f, why), score)
        self.notes.append(f"{why}: {out} -> {into}")

    def _repair(self):
        """Final pass: payoffs the finished deck doesn't support, and cards with no job and no
        links, are replaced (locked cards are never touched)."""
        self.understand()
        support = enablement.DeckSupport(self.graph.cards, self.rec.cmd_names, self.st.ctx.tribe)
        for n in list(self.chosen):
            if self.chosen[n]["locked"]:
                continue
            info, roles = self.cd.info(n), self.cd.roles.get(n, {})
            ev = support.evaluate(info, roles)
            share = enablement.payoff_share(info, roles, ev)
            weak_payoff = ev["enabled"] is not None and share >= 1.0 and ev["enabled"] < enablement.UNSUPPORTED
            dead = not any(v.confidence >= 0.5 for r, v in roles.items() if r != "Land") and \
                synergy.link_value(self.graph.detailed(n), self.rec.cmd_names) < 0.5
            if not (weak_payoff or dead):
                continue
            best = None
            for c in self._flex_pool()[:150]:
                s, f = self.score(c, None)
                if f["unsupported"] < 0.3 and (best is None or s > best[0]):
                    best = (s, c, f)
            if best:
                reason = "unsupported payoff" if weak_payoff else "no job and no links"
                self._replace(n, best[1], best[0], best[2], f"Replaced ({reason})")

    # ---- mana base -------------------------------------------------------------------------
    def _mana_base(self):
        ident = self.identity or ""
        pips = Counter()
        for n in self.chosen:
            cost = (self.cd.info(n) or {}).get("mana_cost") or ""
            for sym in re.findall(r"\{([^}]+)\}", cost):
                for ch in sym:
                    if ch in "WUBRG":
                        pips[ch] += 1 if len(sym) == 1 else 0.5
        for sym in re.findall(r"\{([^}]+)\}", self.cmd.get("mana_cost") or ""):
            for ch in sym:
                if ch in "WUBRG":
                    pips[ch] += 3  # the commander must be castable on time
        nonland = sum(1 for n in self.chosen if section_of(self.cd.info(n)) != "Lands")
        L = 99 - nonland  # lands fill exactly the rest of the 99...
        if L > self.lands_target + 1 and not self.settings.get("pad_lands"):
            L = self.lands_target  # ...unless that would mean padding a short deck with basics (you decide)
        # owned nonbasic lands, judged on fixing, speed and usefulness
        cands = []
        for n, status in self.pool.items():
            info = self.cd.info(n)
            if section_of(info) != "Lands" or n in self.chosen:
                continue
            cols = land_colours(info, ident)
            roles = self.cd.roles.get(n, {})
            utility = any(r != "Land" and v.confidence >= 0.6 for r, v in roles.items())
            if len(ident) >= 2 and len(cols) < 2 and not utility:
                continue  # a mono-colour or colourless land in a multicolour deck: basics do it better
            if len(ident) <= 1 and not utility:
                continue
            s = 0.3 * len(cols) + (0.25 if utility else 0) - (0.25 if enters_tapped(info) else 0) \
                - (0.15 if enters_tapped(info) and self.settings["style"] == "high" else 0) \
                + 0.2 * self.rec.card_quality(n)
            if not cols and len(ident) >= 2:
                s -= 0.3
            cands.append((s, n, cols, utility))
        cands.sort(key=lambda x: -x[0])
        # keep some basics: land-search ramp ("search for a basic land") and basic-land payoffs need them
        fetchers = sum(1 for n in self.chosen if re.search(r"search your library for [^.]*basic land",
                                                           synergy._text(self.cd.info(n) or {})))
        min_basics = len(ident) * (2 if fetchers else 1)
        max_nonbasic = min(L - min_basics, L if len(ident) >= 3 else int(L * 0.6) if len(ident) == 2 else 8)
        chosen_lands = []
        for s, n, cols, utility in cands:
            if len(chosen_lands) >= max_nonbasic or s < 0.15:
                break
            chosen_lands.append((n, cols, utility))
        # basics in proportion to the colours the spells need
        basics_n = L - len(chosen_lands)
        total = sum(pips.values()) or 1
        basics = Counter()
        if ident:
            shares = {c: pips[c] / total for c in ident} if sum(pips[c] for c in ident) else \
                {c: 1 / len(ident) for c in ident}
            for c in ident:
                basics[c] = int(basics_n * shares[c])
            while sum(basics.values()) < basics_n:
                c = max(ident, key=lambda c: shares[c] * basics_n - basics[c])
                basics[c] += 1
        else:
            basics["C"] = basics_n
        # enough sources of each colour (basics + lands that make it)
        sources = {c: basics[c] + sum(1 for _, cols, _ in chosen_lands if c in cols) for c in ident}
        warnings = []
        for c in ident:
            share = pips[c] / total if total else 0
            need = 13 if share >= 0.35 else 10 if share >= 0.15 else 7
            if sources[c] < need:
                warnings.append(f"only {sources[c]} {BASIC_FOR[c]} sources for {share:.0%} of the coloured mana "
                                f"(about {need} recommended)")
        for n, cols, utility in chosen_lands:
            info = self.cd.info(n)
            why = ["Mana: " + ("produces " + "/".join(sorted(cols)) if cols else "utility land")]
            if utility:
                why.append("also does a job: " + ", ".join(r for r, v in self.cd.roles.get(n, {}).items()
                                                            if r != "Land" and v.confidence >= 0.6))
            if not enters_tapped(info):
                why.append("enters untapped")
            self.chosen[n] = dict(stage="Mana base", why=why, score=0.0, locked=False)
        for c, k in basics.items():
            if k > 0:
                name = BASIC_FOR.get(c, "Wastes")
                self.chosen[name] = dict(stage="Mana base", why=[f"Basic land for {c} mana ({k})"], score=0.0,
                                         locked=False, qty=k)
        self.mana_warnings = warnings
        return dict(lands=L, nonbasic=[n for n, _, _ in chosen_lands], basics=dict(basics), pips=dict(pips),
                    sources=sources, warnings=warnings)

    # ---- result + validation -----------------------------------------------------------------
    def _result(self, plan, tg, lands):
        self.understand()
        cards = []
        value = 0.0
        for n, c in self.chosen.items():
            info = self.cd.info(n) or {}
            status, label = self.ownership_label(n)
            price = self.cd.prices.get(n)
            q = c.get("qty", 1)
            value += (price or 0) * q
            pr = printing_for(self.udb, self.store, n, status) or {}
            cards.append(dict(name=n, qty=q, section=section_of(info), cmc=float(info.get("cmc") or 0),
                              scryfall_id=pr.get("id"), image_small=pr.get("image_small"),
                              stage=c["stage"], why=c["why"], locked=c["locked"], status=status, label=label,
                              price=price, roles=sorted(r for r, v in self.cd.roles.get(n, {}).items()
                                                        if r != "Land" and v.confidence >= 0.5)))
        cards.sort(key=lambda c: (SECTIONS.index(c["section"]) if c["section"] in SECTIONS else 99, c["cmc"],
                                  c["name"]))
        total = 1 + sum(c["qty"] for c in cards)
        nonland = [c for c in cards if c["section"] != "Lands"]
        curve = Counter(min(6, int(c["cmc"])) for c in nonland for _ in range(c["qty"]))
        avg = sum(c["cmc"] * c["qty"] for c in nonland) / max(1, sum(c["qty"] for c in nonland))
        final_t = self.targets(avg_mv=avg)
        role_counts = {r: self.counts.get(r, 0) for r in ROLE_STAGES}
        issues = self.validate(cards, total, final_t, role_counts, lands)
        strong = sum(1 for c in nonland if (self.chosen[c["name"]]["score"] >= MIN_SCORE or c["locked"]))
        short = 99 - (total - 1)
        return dict(
            commander=self.cmd["name"], identity=self.identity, settings=self.settings, plan=plan,
            targets={r: dict(target=v["target"], why=v["why"]) for r, v in final_t.items()},
            cards=cards, total=total, complete=total == 100 and not [i for i in issues if i["level"] == "error"],
            missing=max(0, short), strong_cards=strong, curve={k: curve.get(k, 0) for k in range(7)},
            avg_mv=round(avg, 2), role_counts=role_counts, lands=lands, value=round(value, 2),
            owned=dict(Counter(c["status"] for c in cards)), issues=issues, notes=self.notes,
            shortfalls=self._final_shortfalls())

    def _final_shortfalls(self):
        """Jobs the finished deck is still short of (your collection had nothing good enough)."""
        out = [dict(stage=STAGE_LABEL[r], missing=self.goal_counts[r] - self.counts.get(r, 0))
               for r in ROLE_STAGES if self.counts.get(r, 0) < self.goal_counts.get(r, 0)]
        seen = set()
        for stage, m in self.shortfalls:
            if stage.startswith("Engine") or stage.endswith("tribe"):
                if stage not in seen:
                    seen.add(stage)
                    out.append(dict(stage=stage, missing=m))
        return out

    def validate(self, cards, total, tg, role_counts, lands):
        issues = []
        names = Counter()
        for c in cards:
            names[c["name"]] += c["qty"]
        for n, k in names.items():
            info = self.cd.info(n) or {}
            if k > 1 and not deckrules.is_basic(n, info.get("type_line")) and n not in deckrules.ANY_NUMBER:
                issues.append(dict(level="error", text=f"{n} is in the deck {k} times (singleton)"))
            if not set(info.get("color_identity") or "") <= set(self.identity):
                issues.append(dict(level="error", text=f"{n} is outside the commander's colour identity"))
            if info and info.get("legal_commander") not in ("legal", None) and not deckrules.is_basic(n):
                issues.append(dict(level="error", text=f"{n} isn't legal in Commander"))
        if total != 100:
            issues.append(dict(level="error", text=f"The deck has {total} cards, not 100"))
        for r in ("Ramp", "Card Draw", "Removal"):
            lo = tg.get(r, {}).get("target", [0])[0]
            if role_counts.get(r, 0) < lo:
                issues.append(dict(level="warning", text=f"Only {role_counts.get(r, 0)} {r.lower()} cards (this "
                                                         f"deck wants {lo}+)"))
        mech = {m for m in self.plan()["mechanics"]}
        if role_counts.get("Finisher", 0) == 0 and not mech & {"direct damage / life loss", "rewards going wide"}:
            issues.append(dict(level="warning", text="No clear way to win found - add a finisher"))
        for w in lands.get("warnings", []):
            issues.append(dict(level="warning", text="Mana: " + w))
        return issues


def build(udb, store, commander_name, settings=None, progress=None):
    return Builder(udb, store, commander_name, settings).build(progress)


def strategy_for(udb, store, commander_name, settings=None):
    """The proposed plan, for the confirmation step (before building)."""
    b = Builder(udb, store, commander_name, settings)
    b.counts = Counter()
    b.understand()
    return b.plan()


def save(udb, store, result, name):
    """Create a new deck from a build result and link FREE collection copies (existing rules:
    never takes copies other decks use; the collection itself isn't changed). Returns deck id."""
    if not result.get("complete"):
        raise BuildError("The deck isn't a complete, legal 100-card deck yet")
    deck = store.create_deck(name)
    cmd = store.default_printing(result["commander"])
    store.add_card(deck, cmd, role="commander", allocate=False)
    for c in result["cards"]:
        printing = (store.card(c["scryfall_id"]) if c.get("scryfall_id") else None) or \
            _owned_printing(udb, c["name"]) or store.default_printing(c["name"])
        if printing is None:
            continue
        store.add_card(deck, printing, quantity=c["qty"], role="main", source="builder", allocate=False)
    store.allocate(deck)
    return deck


def printing_for(udb, store, name, status=None):
    """The exact printing a built deck uses for a card: your free copy's printing, else the
    printing of a copy you own (used elsewhere), else the default printing."""
    pr = _owned_printing(udb, name)
    if pr is None and status in ("in_use", None):
        r = udb.conn.execute("SELECT scryfall_id FROM collection_items WHERE card_name = ? AND quantity > 0 "
                             "ORDER BY id LIMIT 1", (name,)).fetchone()
        pr = store.card(r[0]) if r else None
    return pr or store.default_printing(name)


def _owned_printing(udb, name):
    r = udb.conn.execute("""SELECT ci.scryfall_id FROM collection_item_availability a
                            JOIN collection_items ci ON ci.id = a.collection_item_id
                            WHERE a.card_name = ? AND a.available > 0 ORDER BY ci.id LIMIT 1""", (name,)).fetchone()
    if r is None:
        return None
    udb.attach_cards_db()
    row = udb.conn.execute("SELECT * FROM scry.cards WHERE id = ?", (r[0],)).fetchone()
    return dict(row) if row else None


def owned_commanders(udb, store):
    """Legendary creatures you own (for 'choose a commander from my Collection')."""
    udb.attach_cards_db()
    rows = udb.conn.execute("""SELECT DISTINCT ci.card_name FROM collection_items ci
                               JOIN scry.oracle_cards o ON o.name = ci.card_name
                               WHERE ci.quantity > 0 AND o.legal_commander = 'legal'
                                 AND ((o.type_line LIKE 'Legendary%Creature%')
                                      OR o.oracle_text LIKE '%can be your commander%')
                               ORDER BY ci.card_name""").fetchall()
    return [r[0] for r in rows]
