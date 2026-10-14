"""Cards worth ADDING - only when there is a real reason. No fixed number of suggestions.

Pipeline (uses the deck understanding from analyser.prepare):

  commander + your strategy -> engines -> what the deck HAS (roles, subroles, engine pieces)
  -> NEEDS (missing roles, unbalanced engines: e.g. 14 token makers but 1 sacrifice outlet)
  -> candidates that address a need -> separate signals per candidate:
       NEED (what problem does it solve, and how badly is it needed - after counting the
             cards that already do it = marginal value), RELEVANCE (strength-weighted
             interactions), COMMANDER FIT, ENGINE FIT, QUALITY (what it does - never price),
             OPPORTUNITY COST (mana, narrowness), COMMUNITY EVIDENCE (optional, cached, small)
  -> tier (EXCELLENT FIT / STRONG FIT / WORTH CONSIDERING / NICHE - STRATEGY DEPENDENT) or
     rejected with a reason ("Why not?") -> grouped by purpose, each card once, varied.

A card that solves no current problem is not recommended, however popular it is.
The numeric score is internal; people see the tier, the need it fills and plain reasons."""
import math
import re
from collections import Counter

import analyser
import cardroles
import community
import synergy

TIERS = ("EXCELLENT", "STRONG", "WORTH", "NICHE")
TIER_LABEL = {"EXCELLENT": "EXCELLENT FIT", "STRONG": "STRONG FIT", "WORTH": "WORTH CONSIDERING",
              "NICHE": "NICHE - STRATEGY DEPENDENT"}
SHOWN = ("EXCELLENT", "STRONG", "WORTH")  # NICHE only behind "Show lower-confidence ideas"
MIN_NEED = 0.15        # the NEED test: below this the card solves no real problem here
EXCELLENT_MIN, STRONG_MIN, WORTH_MIN = 0.72, 0.62, 0.52
UPGRADE_GAP = 0.15     # an upgrade must be clearly better than the deck's weakest card doing that job
CLEARLY_BETTER = 0.12  # an option this far below the best option for the same need isn't worth your slot
# How much a shortfall in each core job hurts (missing ramp / draw / removal hurts most).
ROLE_WEIGHT = {"Ramp": 1.0, "Card Draw": 1.0, "Removal": 1.0, "Board Wipe": 0.7, "Protection": 0.7,
               "Finisher": 0.7, "Counterspell": 0.5, "Recursion": 0.5, "Tutor": 0.4}

ROLE_CATEGORY = {"Ramp": "RAMP", "Card Draw": "CARD DRAW", "Removal": "REMOVAL", "Board Wipe": "BOARD WIPES",
                 "Counterspell": "COUNTERSPELLS", "Protection": "PROTECTION", "Tutor": "TUTORS",
                 "Recursion": "RECURSION", "Finisher": "FINISHERS"}
ROLE_NOUN = {"Ramp": ("ramp card", "ramp cards"), "Card Draw": ("card draw source", "card draw sources"),
             "Removal": ("removal spell", "removal spells"), "Board Wipe": ("board wipe", "board wipes"),
             "Counterspell": ("counterspell", "counterspells"), "Protection": ("protection card", "protection cards"),
             "Tutor": ("tutor", "tutors"), "Recursion": ("recursion card", "recursion cards"),
             "Finisher": ("finisher", "finishers")}

# Engine pieces: key -> (resource, side, enough, singular, plural, category, counterpart key)
# side: prov = makes the resource on purpose, rew = pays off on it, any = is / makes it at all
PIECES = {
    "token_makers": ("tokens", "prov", 8, "token maker", "token makers", "TOKEN ENGINE", "token_payoffs"),
    "token_payoffs": ("tokens", "rew", 3, "token payoff", "token payoffs", "TOKEN PAYOFFS", "token_makers"),
    "wide_payoffs": ("going wide", "rew", 3, "go-wide payoff (anthem / overrun)", "go-wide payoffs",
                     "GO-WIDE PAYOFFS", "token_makers"),
    "sac_outlets": ("creature deaths", "prov", 3, "sacrifice outlet", "sacrifice outlets", "SACRIFICE OUTLETS",
                    "fodder"),
    "death_payoffs": ("creature deaths", "rew", 4, "death payoff", "death payoffs", "SACRIFICE / DEATH PAYOFFS",
                      "fodder"),
    "fodder": ("sacrifice fodder", "prov", 8, "card that makes bodies to sacrifice", "cards that make bodies to "
               "sacrifice", "SACRIFICE FODDER", "sac_outlets"),
    "counter_sources": ("+1/+1 counters", "prov", 8, "+1/+1 counter source", "+1/+1 counter sources",
                        "COUNTERS ENGINE", "counter_payoffs"),
    "counter_payoffs": ("+1/+1 counters", "rew", 4, "+1/+1 counter payoff", "+1/+1 counter payoffs",
                        "COUNTERS PAYOFFS", "counter_sources"),
    "spells": ("instants and sorceries", "any", 20, "instant or sorcery", "instants and sorceries", "SPELLS",
               "spell_payoffs"),
    "spell_payoffs": ("instants and sorceries", "rew", 4, "spell payoff", "spell payoffs", "SPELL PAYOFFS", "spells"),
    "artifacts": ("artifacts", "any", 15, "artifact", "artifacts", "ARTIFACTS", "artifact_payoffs"),
    "artifact_payoffs": ("artifacts", "rew", 4, "artifact payoff", "artifact payoffs", "ARTIFACT PAYOFFS",
                         "artifacts"),
    "enchantments": ("enchantments", "any", 15, "enchantment", "enchantments", "ENCHANTMENTS",
                     "enchantment_payoffs"),
    "enchantment_payoffs": ("enchantments", "rew", 4, "enchantress payoff", "enchantress payoffs",
                            "ENCHANTRESS PAYOFFS", "enchantments"),
    "land_ramp": ("lands entering", "prov", 8, "extra-land effect", "extra-land effects", "LANDFALL ENABLERS",
                  "landfall_payoffs"),
    "landfall_payoffs": ("lands entering", "rew", 5, "landfall payoff", "landfall payoffs", "LANDFALL PAYOFFS",
                         "land_ramp"),
    "yard_fillers": ("cards in the graveyard", "prov", 6, "graveyard filler", "graveyard fillers",
                     "GRAVEYARD ENABLERS", "yard_payoffs"),
    "yard_payoffs": ("cards in the graveyard", "rew", 5, "graveyard payoff", "graveyard payoffs",
                     "GRAVEYARD PAYOFFS", "yard_fillers"),
    "lifegain": ("life gain", "prov", 8, "life gain source", "life gain sources", "LIFE GAIN", "lifegain_payoffs"),
    "lifegain_payoffs": ("life gain", "rew", 4, "life gain payoff", "life gain payoffs", "LIFE GAIN PAYOFFS",
                         "lifegain"),
    "gear": ("equipment", "prov", 8, "equipment / aura", "equipment and auras", "EQUIPMENT & AURAS", None),
    "attack_payoffs": ("attacking", "rew", 4, "attack payoff", "attack payoffs", "ATTACK PAYOFFS", "attackers"),
    "attackers": ("attacking", "prov", 5, "card that adds attackers", "cards that add attackers", "EXTRA ATTACKERS",
                  "attack_payoffs"),
    "blinkers": ("blink targets", "prov", 4, "blink effect", "blink effects", "BLINK", "etb"),
    "etb": ("blink targets", "rew", 8, "enter-effect creature", "enter-effect creatures", "ENTER EFFECTS",
            "blinkers"),
    "walkers": ("planeswalkers", "any", 8, "planeswalker", "planeswalkers", "PLANESWALKERS", "walker_payoffs"),
    "walker_payoffs": ("planeswalkers", "rew", 3, "planeswalker payoff", "planeswalker payoffs",
                       "SUPERFRIENDS PAYOFFS", "walkers"),
    "etb_payoffs": ("creatures entering", "rew", 3, "creature-enters payoff", "creature-enters payoffs",
                    "CREATURE PAYOFFS", None),
    # amplifiers: only a need once there is an engine to amplify (the counterpart has >= AMPLIFY members)
    "token_doublers": ("Token Doubler", "role", 2, "token doubler", "token doublers", "TOKEN ENGINE", "token_makers"),
    "counter_doublers": ("counter doubler", "rx", 2, "counter doubler", "counter doublers", "COUNTERS ENGINE",
                         "counter_sources"),
    "reanimation": ("Reanimation", "role", 4, "reanimation spell", "reanimation spells", "REANIMATION",
                    "yard_fillers"),
}
AMPLIFIERS = {"token_doublers", "counter_doublers"}
AMPLIFY = 6
CREATURE_TOKEN = re.compile(r"\d+/\d+[^.]*\btokens?\b|creature tokens?\b|\bmobilize\b|\bsquad\b|\bamass\b")
FLOOR_MAX_PROTECTION = 2.0  # a card the deck clearly relies on is never "the bar to beat"
COUNTER_DOUBLER = re.compile(r"(?:twice that many|that many plus (?:one|two))[^.]*counters|"
                             r"counters[^.]*(?:twice that many|that many plus (?:one|two))")
PLAN_PIECES = {
    "Tokens": ["token_makers", "token_payoffs", "wide_payoffs", "token_doublers"],
    "Aristocrats": ["sac_outlets", "death_payoffs", "fodder"],
    "Counters": ["counter_sources", "counter_payoffs", "counter_doublers"],
    "Proliferate": ["counter_sources", "counter_payoffs", "counter_doublers"],
    "Spellslinger": ["spells", "spell_payoffs"],
    "Artifacts": ["artifacts", "artifact_payoffs"],
    "Enchantments": ["enchantments", "enchantment_payoffs"],
    "Landfall": ["land_ramp", "landfall_payoffs"],
    "Graveyard": ["yard_fillers", "yard_payoffs"], "Reanimator": ["yard_fillers", "yard_payoffs", "reanimation"],
    "Lifegain": ["lifegain", "lifegain_payoffs"],
    "Voltron": ["gear"], "Equipment": ["gear"], "Auras": ["gear"],
    "Combat": ["attack_payoffs", "attackers"],
    "Blink": ["blinkers", "etb"],
    "Planeswalkers": ["walkers", "walker_payoffs"],
    "Creatures": ["etb_payoffs"],
}
TRIBE_ENOUGH = 20
BUILT_AROUND = 6  # a detected theme counts as the deck's plan once this many cards serve it


def plan_name(theme):
    import commander
    return commander.PLAN_NAME.get(theme, theme)


_RES = {}


def card_resources(cd, name, info, roles):
    """resources() is regex-heavy: cache it per card (card text doesn't change within a run)."""
    key = (cd.stamp, name)
    r = _RES.get(key)
    if r is None:
        r = _RES[key] = synergy.resources(info, roles)
    return r


def _has_piece(res, piece, info=None, roles=None):
    resource, side = PIECES[piece][0], PIECES[piece][1]
    if side == "role":
        v = (roles or {}).get(resource)
        return bool(v and v.confidence >= 0.6)
    if side == "rx":
        return bool(info and COUNTER_DOUBLER.search(synergy._text(info)))
    prov, rew, gen = res
    if piece == "fodder" and info is not None and not CREATURE_TOKEN.search(synergy._text(info)):
        return False  # Food / Treasure / Clues aren't bodies to sacrifice
    if side == "prov":
        return resource in prov
    if side == "rew":
        return resource in rew
    return resource in prov or resource in gen


# ---------------------------------------------------------------- deck needs

def deck_needs(st):
    """Every problem the deck has, each with how badly (severity 0..1), why, and how many
    cards already address it. Saturated jobs (marginal value ~0) are kept too, with
    severity 0, so "Why not?" can say "the deck already has 14 of those"."""
    ctx, graph, prof = st.ctx, st.graph, st.profile
    needs = {}
    # 1. core roles against THIS deck's targets
    for role, cat in ROLE_CATEGORY.items():
        g = ctx.guideline(role)
        if not g:
            continue
        lo, hi = g
        have = st.counts.get(role, 0)
        if have < lo:
            sev = ROLE_WEIGHT.get(role, 0.6) * (0.35 + 0.65 * (lo - have) / max(1, lo))
        elif have < (lo + hi) / 2:
            sev = 0.12  # in range, room for one more - never enough on its own
        else:
            sev = 0.0
        kind = "ADDS" if role == "Finisher" else "FILLS"
        names = [n for n, (i, r) in graph.cards.items() if role in r and n not in graph.commanders]
        needs["role:" + role] = dict(key="role:" + role, kind=kind, label=role, category=cat, severity=round(sev, 3),
                                     have=have, enough=lo, target=[lo, hi], examples=names[:4], role=role,
                                     source="deck targets")
    # 2. engine pieces for the plans that matter (your tags first, then the commander)
    weights = {}
    for t, w in ctx.themes.items():
        weights[t] = max(weights.get(t, 0), w)
    if ctx.user_tags:
        if prof.get("primary"):
            weights[prof["primary"]] = max(weights.get(prof["primary"], 0), 0.6)
    else:
        if prof.get("primary"):
            weights[prof["primary"]] = max(weights.get(prof["primary"], 0), 1.0)
        for t in (prof.get("supporting") or [])[:2]:
            weights[t] = max(weights.get(t, 0), 0.55)
    piece_w, piece_plan, piece_src = {}, {}, {}
    for theme, w in weights.items():
        for p in PLAN_PIECES.get(theme, []):
            if w > piece_w.get(p, 0):
                piece_w[p], piece_plan[p] = w, theme
                piece_src[p] = "your strategy" if ctx.user_tags and theme in ctx.themes else \
                    "the commander" if theme == prof.get("primary") else "the detected strategy"
    # what the commander itself consumes / produces is always relevant
    cmd_w = 0.7 if ctx.user_tags else 1.0
    for n in prof.get("needs", []):
        key = n["key"]
        res, side = (key[:-7], "rew") if key.endswith(":payoff") else (key, None)
        w = cmd_w * (0.6 if side else 1.0)  # what it CONSUMES matters more than payoffs for what it makes
        for p, spec in PIECES.items():
            if spec[0] == res and (spec[1] == side if side else spec[1] in ("prov", "any")):
                if w > piece_w.get(p, 0):
                    piece_w[p], piece_plan[p], piece_src[p] = w, prof.get("primary") or "Commander", "the commander"
    counts = {}
    members = {}
    for p in PIECES:
        names = [n for n, r in graph.res.items() if n not in graph.commanders and _has_piece(r, p, *graph.cards[n])]
        cmd = [n for n, r in graph.res.items() if n in graph.commanders and _has_piece(r, p, *graph.cards[n])]
        counts[p] = len(names) + 2 * len(cmd)  # the commander is always there: counts double
        members[p] = (names, cmd)
    # any cheap creature is sacrifice fodder too, not just token makers
    cheap = sum(1 for n, (i, r) in graph.cards.items() if n not in graph.commanders
                and "Creature" in (i.get("type_line") or "").split(" // ")[0] and float(i.get("cmc") or 0) <= 3)
    counts["fodder"] += cheap // 2
    for p, w in piece_w.items():
        resource, side, enough, sing, plural, cat, partner = PIECES[p]
        if side == "rew" and partner:  # many providers want more payoffs
            enough = max(enough, round(counts.get(partner, 0) / 5))
        have = counts[p]
        sev = w * max(0.0, (enough - have) / enough)
        if p in AMPLIFIERS and counts.get(partner, 0) < AMPLIFY:
            sev = 0.0  # nothing big enough to amplify yet
        if sev > 0:
            sev = max(sev, 0.2 * w)
        plan = piece_plan[p]
        names, cmd = members[p]
        needs["piece:" + p] = dict(key="piece:" + p, kind="STRENGTHENS", label=cat.title().replace("/ ", "/ "),
                                   plan_label=plan_name(plan), plan=plan, weight=w, category=cat, severity=round(sev, 3),
                                   have=have, enough=enough, examples=names[:4], commander_does=bool(cmd), piece=p,
                                   singular=sing, plural=plural, partner=partner,
                                   partner_have=counts.get(partner) if partner else None,
                                   partner_plural=PIECES[partner][4] if partner else None, source=piece_src[p],
                                   niche=piece_src[p] == "the detected strategy" and (
                                       st.uncertain or w < 1.0 or ctx.theme_counts.get(plan, 0) < BUILT_AROUND))
    # 3. tribe
    # only when the commander cares about the tribe or you chose Tribal - a deck that merely
    # contains many Humans isn't a Human tribal deck
    if ctx.tribe and ((ctx.user_tags and "Tribal" in ctx.themes) or (prof.get("themes") or {}).get("Tribal")):
        have = ctx.tribe_count
        sev = max(0.0, (TRIBE_ENOUGH - have) / TRIBE_ENOUGH)
        needs["tribe"] = dict(key="tribe", kind="STRENGTHENS", label=f"{ctx.tribe} Tribal", category=f"{ctx.tribe.upper()}S",
                              severity=round(sev and max(sev, 0.25), 3), have=have, enough=TRIBE_ENOUGH, examples=[],
                              source="the commander", niche=False, tribe=ctx.tribe)
    return needs


def curve_need(st):
    target = analyser.AVG_MV_TARGET[st.ctx.goal]
    avg = st.comp["avg_mv"]
    return avg > target + 0.25, avg, target


# ---------------------------------------------------------------- one candidate

class Recommender:
    def __init__(self, st, settings, feedback=None, community_rows=None):
        self.st, self.settings = st, settings
        self.ctx, self.cd, self.graph = st.ctx, st.cd, st.graph
        self.mode = settings.get("mode", "collection")
        self.budget = None if self.mode == "collection" else settings.get("budget_usd")
        self.needs = deck_needs(st)
        self.high_curve, self.avg_mv, self.mv_target = curve_need(st)
        self.feedback = feedback or {}
        self.community = community_rows or {}
        self.cmd_names = {c["card_name"] for c in self.ctx.commanders}
        self.deck_names = {r["card_name"] for r in st.deck_rows}
        self.cmd_res = {n: self.graph.res[n] for n in self.cmd_names if n in self.graph.res}
        self.engines = self.graph.engines()
        self.liked = self._liked()
        import enablement
        self.support = enablement.DeckSupport(self.graph.cards, self.cmd_names, self.ctx.tribe)
        cmd_text = " ".join(synergy._text(i) for n, (i, _) in self.graph.cards.items() if n in self.cmd_names)
        self.cmd_tribal = bool(self.ctx.tribe and self.ctx.tribe.lower() in cmd_text)
        self.floors = self._upgrade_floors()

    def _upgrade_floors(self):
        """For the deck's main engine pieces (your plan / the commander's - not merely detected
        side themes): the weakest card currently doing that job. A clearly better card for the
        SAME job is a core strategy upgrade even when the job is already covered."""
        flags = self.st.flags
        review = {v["name"]: v for v in self.st.review}
        out = {}
        for key, n in self.needs.items():
            if not key.startswith("piece:") or n.get("niche") or n.get("weight", 0) < 0.6 or n["severity"] >= MIN_NEED:
                continue
            if PIECES[n["piece"]][1] == "any":
                continue  # "any instant" / "any artifact" isn't a job one card can do better than another
            members = [m for m in self.graph.res if m not in self.cmd_names
                       and _has_piece(self.graph.res[m], n["piece"], *self.graph.cards[m])]
            scored = []
            for m in members:
                if flags.get(m, set()) & {"core", "never_cut"}:
                    continue  # you said it stays - it isn't the bar to beat
                v = review.get(m)
                if v is None or v["E"] <= 0 or v["P"] >= FLOOR_MAX_PROTECTION:
                    continue  # only a card the cut review already doubts is a bar to beat
                info = self.cd.info(m)
                if info:
                    roles = self.cd.roles.get(info["name"], {})
                    scored.append((self.quality(m, info, roles, [(1.0, key)], float(info.get("cmc") or 0))[0], m))
            # upgrading one of very many (e.g. the 31st token maker) is worth little
            if scored and len(members) >= 2 and len(members) <= 2 * n["enough"]:
                q, weakest = min(scored)
                out[key] = (q, weakest, len(members))
        return out

    def _liked(self):
        """Needs that cards you marked GOOD SUGGESTION addressed - a small nudge for similar cards."""
        out = Counter()
        for name, verdict in self.feedback.items():
            if verdict != "good":
                continue
            info = self.cd.info(name)
            if not info:
                continue
            roles = self.cd.roles.get(info["name"], {})
            for k in self._addressed(info, roles, card_resources(self.cd, info["name"], info, roles)):
                out[k] += 1
        return out

    def _addressed(self, info, roles, res):
        """Need keys this card would address (any severity)."""
        out = []
        for key, n in self.needs.items():
            if key.startswith("role:"):
                v = roles.get(n["role"])
                if v and v.confidence >= 0.6:
                    out.append(key)
            elif key.startswith("piece:"):
                if _has_piece(res, n["piece"], info, roles):
                    out.append(key)
            elif key == "tribe":
                t = n["tribe"]
                if t in cardroles.creature_types(info.get("type_line")) or t.lower() in (info.get("oracle_text") or "").lower():
                    out.append(key)
        return out

    # ---- availability (stage 1: legal, identity, ownership mode) ----
    def availability(self, name, info):
        if info is None:
            return "unknown card"
        if name in self.deck_names:
            return "already in the deck"
        if info["legal_commander"] != "legal":
            return "not legal in Commander"
        if not set(info["color_identity"] or "") <= set(self.ctx.identity):
            return "outside your commander's colour identity"
        if "Land" in (info["type_line"] or "").split(" // ")[0]:
            return "lands are not suggested here"
        own = self.ctx.ownership.get(name)
        if self.mode == "collection" and not (own and own["free"] > 0):
            return ("owned, but every copy is used in another deck (" + ", ".join(own["used_in"][:2]) + ")"
                    if own else "not in your collection") + " - My Collection mode only suggests free cards you own"
        if self.mode == "all" and not own and analyser._obscure(self.cd, name):
            return "rarely played and not owned - too obscure to suggest getting"
        return None

    def evaluate(self, name):
        """-> (item, None) when recommended (any tier) or (None, reason) when not."""
        cd, ctx = self.cd, self.ctx
        info = cd.info(name)
        if self.feedback.get(name) == "not_for_deck" or (info and self.feedback.get(info["name"]) == "not_for_deck"):
            return None, dict(name=name, reason="you marked it NOT FOR THIS DECK")
        why_out = self.availability(name, info)
        if why_out:
            return None, dict(name=name, reason=why_out)
        name = info["name"]
        roles = cd.roles.get(name, {})
        res = card_resources(cd, name, info, roles)
        addressed = self._addressed(info, roles, res)
        # ---- NEED (with marginal value: severity already counts the cards doing it)
        need_parts = []
        for key in addressed:
            n = self.needs[key]
            sev = n["severity"]
            if key.startswith("role:"):
                sev *= roles[n["role"]].confidence
            need_parts.append((sev, key))
        need_parts.sort(reverse=True)
        need = need_parts[0][0] if need_parts else 0.0
        upgrade = None
        if need < MIN_NEED:
            upgrade = self._upgrade(name, info, roles, addressed)
            if upgrade is None:
                return None, dict(name=name, reason=self._no_need_reason(addressed, roles), need=round(need, 2))
            need, key = upgrade[0], upgrade[1]
            need_parts = [(need, key)] + [p for p in need_parts if p[1] != key]
        # ---- signals
        links = self.graph.candidate_links(info, roles)
        cmd_links = [l for l in links if l[0] in self.cmd_names]
        deck_links = [l for l in links if l[0] not in self.cmd_names]
        strong = sum(1 for l in deck_links if l[2] == "strong")
        enabled = self.support.evaluate(info, roles)
        share = __import__("enablement").payoff_share(info, roles, enabled)
        unsupported = share * (1 - enabled["enabled"]) if enabled["enabled"] is not None else 0.0
        # links that exist only through an unsupported payoff aren't real synergy
        weighted = synergy.link_value(links, self.cmd_names) * (1 - 0.7 * unsupported)
        relevance = 1 - math.exp(-weighted / 6)
        commander_fit = self.commander_fit(res)
        if "tribe" in addressed:  # thematic unless the commander itself cares about the tribe
            commander_fit = max(commander_fit, 0.8 if self.cmd_tribal else 0.4)
        engine, engine_links = None, 0
        strong_to = {n for n, _, s in deck_links if s == "strong"}
        for e, v in self.engines.items():
            k = len(strong_to & set(v["members"]))
            if k > engine_links:
                engine, engine_links = e, k
        engine_fit = min(1.0, engine_links / 6)
        sc = analyser.score(ctx, name, self.st.counts)
        cmc = sc["cmc"]
        quality, rank = self.quality(name, info, roles, need_parts, cmc)
        # ---- opportunity cost
        cost = 0.0
        cost_notes = []
        if cmc >= 6:
            cost += 0.04 + (0.04 if self.high_curve else 0)
            if self.high_curve:
                cost_notes.append(f"Costs {int(cmc)} mana and the deck's curve is already high "
                                  f"(average {self.avg_mv:.2f})")
        narrow = sc["main_role"] in analyser.NARROW or (not sc["core"] and set(roles) <= analyser.NARROW)
        if narrow:
            cost += 0.05
        # ---- community evidence (optional, small, never decisive)
        comm, comm_lines = 0.0, []
        for ev in self.community.get(name, []):
            if ev["source"] == "recommander" and ev["metric"] >= 0.5:
                comm = max(comm, 0.06 * ev["metric"])
                comm_lines.append(f"Recommander also suggests it for this commander (community data, "
                                  f"{ev['retrieved']})")
            elif ev["source"] == "spellbook":
                comm = max(comm, 0.05)
                comm_lines.append(f"Commander Spellbook: {ev['detail']}")
        # ---- your feedback on earlier suggestions
        fb = min(0.06, 0.03 * sum(self.liked.get(k, 0) for _, k in need_parts[:2]))
        if self.feedback.get(name) == "good":
            fb = max(fb, 0.05)
        curve_bonus = 0.03 if self.high_curve and cmc <= 2 else 0.0
        secondary = sum(s for s, _ in need_parts[1:3]) * 0.25
        top = self.needs[need_parts[0][1]]
        if top["key"].startswith("role:"):
            # a deck job (removal, draw, ramp, wipes...): judged on the need and how well it does
            # the job - interaction cards have no synergy links by nature, and that's fine
            score = (0.35 * min(1.0, need + secondary) + 0.62 * quality + 0.05 * relevance + 0.05 * commander_fit
                     + 0.03 * engine_fit - cost + comm + fb + curve_bonus - 0.3 * unsupported)
        else:  # an engine / plan piece: synergy is the point
            score = (0.25 * min(1.0, need + secondary) + 0.15 * relevance + 0.15 * commander_fit + 0.10 * engine_fit
                     + 0.35 * quality - cost + comm + fb + curve_bonus - 0.3 * unsupported)
        niche = bool(top.get("niche")) or narrow
        obscure = analyser._obscure(cd, name)
        signals = dict(need=round(need, 2), relevance=round(relevance, 2), commander_fit=commander_fit,
                       engine_fit=round(engine_fit, 2), quality=round(quality, 2), opportunity_cost=round(cost, 2),
                       community=round(comm, 3), feedback=round(fb, 3), score=round(score, 3))
        # ---- tier
        if score >= EXCELLENT_MIN and need >= 0.45 and not upgrade and quality >= 0.62 and (commander_fit >= 1 or engine_fit >= 0.5) \
                and not niche and not obscure:
            tier = "EXCELLENT"
        elif score >= STRONG_MIN and need >= 0.3 and quality >= 0.5 and not niche and not obscure:
            tier = "STRONG"
        elif score >= WORTH_MIN and quality >= 0.38 and not niche:
            tier = "WORTH"
        elif score >= WORTH_MIN - 0.03 and niche:
            tier = "NICHE"
        else:
            weakest = min(("need", need), ("relevance", relevance), ("quality", quality), key=lambda kv: kv[1])
            return None, dict(name=name, reason=f"below the recommendation threshold (weakest signal: "
                                                f"{weakest[0]} {weakest[1]:.2f})", need=round(need, 2),
                              signals=signals)
        own = analyser.ownership_of(ctx, name)
        if self.budget is not None and (own["cost"] is None or own["cost"] > self.budget + 1e-9):
            return None, dict(name=name, reason="over your upgrade budget", signals=signals)
        # ---- plain-English reasons
        why = [self._need_line(top, roles, info) if not upgrade else upgrade[2]]
        if cmd_links:
            why.append(self._commander_line(res, cmd_links[0]))
        if engine and engine_links >= 2:
            ex = [n for n in self.engines[engine]["members"] if n in strong_to][:3]
            why.append(f"Joins your {engine.lower()}: works directly with {', '.join(ex)}")
        if len(links) >= 2:  # the same cards "Show interactions" lists - never a vague count
            examples = [n for n, _, _ in deck_links[:3]] or [n for n, _, _ in links[:3]]
            why.append(f"Works with {len(links)} cards already in the deck ({strong} strong; e.g. {', '.join(examples)})")
        for _, k in need_parts[1:2]:
            n2 = self.needs[k]
            if n2["severity"] >= MIN_NEED and n2["category"] != top["category"]:
                why.append("Also " + self._need_short(n2))
        if curve_bonus:
            why.append(f"Cheap ({int(cmc)} mana) - helps a curve that averages {self.avg_mv:.2f}")
        why += cost_notes
        if enabled["text"] and enabled["unsupported"]:
            why.append(enabled["text"])
        if niche:
            why.append("Only matters if you build toward " + (top.get("plan_label") or top.get("label") or "this plan") +
                       (" - set the strategy if that's your plan" if not self.ctx.user_tags else ""))
        why += comm_lines[:1]
        if rank and rank <= 1500 and not comm_lines:
            why.append(f"Widely played in Commander (popularity rank #{rank}, Scryfall/EDHREC data)")
        labels = [self._label(top)]
        for _, k in need_parts[1:3]:
            n2 = self.needs[k]
            if n2["severity"] >= MIN_NEED and self._label(n2) not in labels:
                labels.append(self._label(n2))
        if curve_bonus:
            labels.append("IMPROVES: Mana Curve")
        funcs = sorted(f for subs in synergy.functions(info, roles).values() for f in subs)
        return dict(name=name, tier=tier, tier_label=TIER_LABEL[tier], category=top["category"], need_key=top["key"],
                    need_label=labels[0], labels=labels, why=why, ownership=own, confidence=round(score, 2),
                    signals=signals, links=[dict(card=n, why=t, strength=s) for n, t, s in links[:20]],
                    links_total=len(links), strong=strong, cmc=cmc,
                    roles=sorted(sc["core"] or [r for r in roles if r != "Land"])[:3], functions=funcs[:3],
                    main_function=funcs[0] if funcs else None, feedback=self.feedback.get(name)), None

    def _upgrade(self, name, info, roles, addressed):
        """-> (need, need key, reason line) when this card clearly out-does the deck's weakest
        card in one of its core engine pieces, else None."""
        best = None
        for key in addressed:
            floor = self.floors.get(key)
            if not floor:
                continue
            q_min, weakest, have = floor
            q = self.quality(name, info, roles, [(1.0, key)], float(info.get("cmc") or 0))[0]
            gap = q - q_min
            if gap >= UPGRADE_GAP and q >= 0.5 and (best is None or gap > best[0]):
                best = (gap, key, weakest, have)
        if best is None:
            return None
        gap, key, weakest, have = best
        n = self.needs[key]
        ukey = "upgrade:" + key
        if ukey not in self.needs:
            self.needs[ukey] = dict(n, key=ukey, kind="UPGRADES", category="CORE STRATEGY UPGRADES", severity=0.0,
                                    niche=False)
        line = f"Stronger {n['singular']} than {weakest}, one of your {have} {n['plural']} the analyser already doubts"
        return min(0.6, 0.3 + gap), ukey, line

    def commander_fit(self, res):
        """1.0 = feeds what the commander specifically rewards; 0.7 = pays off what the
        commander specifically makes; 0.25 = only links through the commander being a creature
        (or the card being one); 0 = nothing."""
        prov, rew, gen = res
        best = 0.0
        for cprov, crew, cgen in self.cmd_res.values():
            if crew & prov:
                best = max(best, 1.0)
            elif crew & gen and crew & gen & {"instants and sorceries", "artifacts", "enchantments", "equipment",
                                              "planeswalkers"}:
                best = max(best, 0.8)  # e.g. any instant for a spellslinger commander
            if rew & cprov:
                best = max(best, 0.7)
            if (crew & gen) or (rew & cgen):
                best = max(best, 0.25)
        return best

    def card_quality(self, name):
        """General quality of a card at its main job (for comparing swap replacements)."""
        info = self.cd.info(name)
        if not info:
            return 0.0
        roles = self.cd.roles.get(info["name"], {})
        fake = [(1.0, "role:" + r) for r in roles if "role:" + r in self.needs][:1] or [(1.0, next(iter(self.needs)))]
        return self.quality(info["name"], info, roles, fake, float(info.get("cmc") or 0))[0]

    def quality(self, name, info, roles, need_parts, cmc):
        """How good the card is AT THE JOB it would do here: how surely it does it, at what mana
        cost, repeatably or once - plus (when Update Prices has stored it) how widely the
        Commander community plays it (Scryfall's documented edhrec_rank). Never price."""
        top = self.needs[need_parts[0][1]]
        job = top.get("role")
        conf = roles[job].confidence if job and job in roles else max([v.confidence for v in roles.values()] + [0.5])
        ideal = analyser.IDEAL_MV.get(job, 3.0)
        eff = max(0.0, min(1.0, 1 - max(0.0, cmc - ideal) / 4))
        engine = 1.0 if synergy.is_engine(info, roles) else 0.0
        multi = min(1.0, (len(need_parts) - 1) / 2)
        func = 0.45 * conf + 0.25 * eff + 0.15 * engine + 0.15 * multi
        if info.get("game_changer"):
            func = min(1.0, func + 0.1)
        rank = self.cd.popularity.get(name)
        if rank:
            pop = max(0.0, 1 - math.log10(rank) / 4.5)
            return 0.45 * func + 0.55 * pop, rank
        if self.cd.popularity:  # data present, card unranked: hardly played
            return 0.45 * func, None
        return 0.85 * func, None  # no popularity data at all: function only, a little less sure

    # ---- wording
    def _label(self, n):
        return f"{n['kind']}: {n['label']}"

    def _need_short(self, n):
        if n["key"].startswith("role:"):
            return f"adds {ROLE_NOUN[n['role']][0]} (the deck has {n['have']}, about {n['enough']}+ wanted)"
        if n["key"] == "tribe":
            return f"adds a {n['tribe']} ({n['have']} in the deck)"
        return f"adds a {n['singular']} (the deck has {n['have']})"

    def _need_line(self, n, roles, info):
        if n["key"].startswith("role:"):
            role = n["role"]
            sing, plural = ROLE_NOUN[role]
            sub = synergy.functions(info, roles).get(role, [None])[0]
            what = sub if sub and sub != role.lower() else sing
            lo, hi = n["target"]
            if n["have"] == 0:
                return f"Adds {what}: the deck has no {plural} (this deck wants {lo}-{hi})"
            return f"Adds {what}: the deck has only {n['have']} {plural if n['have'] != 1 else sing} " \
                   f"(this deck wants {lo}-{hi})"
        if n["key"] == "tribe":
            return f"Another {n['tribe']} for your {n['tribe']} commander: the deck has {n['have']} " \
                   f"(about {n['enough']}+ makes tribal payoffs reliable)"
        have = n["have"] - (2 if n.get("commander_does") else 0)
        base = f"The deck has {'no' if have <= 0 else 'only ' + str(have)} {n['plural'] if have != 1 else n['singular']}"
        if n.get("commander_does"):
            base += " besides your commander"
        if n.get("partner_have"):
            base += f", while {n['partner_have']} cards are {n['partner_plural']}"
        src = {"your strategy": f"for the {n['plan_label'].lower()} plan you chose",
               "the commander": "for your commander's plan",
               "the detected strategy": f"for the {n['plan_label'].lower()} plan"}.get(n["source"], "")
        return f"{base} - this adds a {n['singular']} {src}".strip()

    def _commander_line(self, res, link):
        cmd, text, strength = link
        short = cmd.split(",")[0]
        prov, rew, gen = res
        cprov, crew, cgen = self.cmd_res.get(cmd, (set(), set(), set()))
        # the most specific reason first: what it makes on purpose, then what it pays off,
        # and only then "it's a creature"
        for r in sorted(crew & prov):
            pl, rl = synergy.LABELS.get(r, (r, r))
            return f"Works with {short}: {short} {rl}, and this card {pl}"
        for r in sorted(rew & cprov):
            pl, rl = synergy.LABELS.get(r, (r, r))
            return f"Works with {short}: {short} {pl}, and this card {rl}"
        for r in sorted(crew & gen):
            pl, rl = synergy.LABELS.get(r, (r, r))
            return f"Works with {short}: {short} {rl}, and this card {synergy._generic_label(r, pl)}"
        for r in sorted(rew & cgen):
            pl, rl = synergy.LABELS.get(r, (r, r))
            return f"Works with {short}: {short} {synergy._generic_label(r, pl)}, and this card {rl}"
        return f"Works with {short} ({strength}): {text.split(' - ', 1)[-1]}"

    def _no_need_reason(self, addressed, roles):
        sat = [self.needs[k] for k in addressed if self.needs[k]["severity"] < MIN_NEED]
        if sat:
            n = max(sat, key=lambda n: n["have"])
            what = ROLE_NOUN[n["role"]][1] if n["key"].startswith("role:") else n.get("plural") or n["label"]
            return f"doesn't address a current need - the deck already has {n['have']} {what} " \
                   f"(about {n['enough']} is enough)"
        jobs = ", ".join(sorted(r for r in roles if r != "Land")[:3]) or "no recognised job"
        return f"doesn't address any need this deck has ({jobs})"

    # ---- the whole search
    def run(self, names=None, debug=False):
        cd = self.cd
        items, rejected = [], []
        relevant = names
        if relevant is None:
            relevant = [n for n, info in cd.cards.items() if self.availability(n, info) is None]
        for n in relevant:
            item, why_not = self.evaluate(n)
            if item:
                items.append(item)
            elif debug and why_not and why_not.get("signals"):
                rejected.append(why_not)
        items, worse = self.clearly_better(items)
        if debug:
            rejected += worse
        return self.group(items), rejected

    @staticmethod
    def clearly_better(items):
        """Opportunity cost against the OTHER options: for each need, a card well below the best
        option for that same need would waste the slot - it's dropped (with the reason)."""
        best = {}
        for it in items:
            if it["tier"] in SHOWN:
                best[it["need_key"]] = max(best.get(it["need_key"], 0), it["confidence"])
        keep, out = [], []
        for it in items:
            b = best.get(it["need_key"])
            if b is not None and it["confidence"] < b - CLEARLY_BETTER:
                tops = sorted((x for x in items if x["need_key"] == it["need_key"]), key=lambda x: -x["confidence"])
                out.append(dict(name=it["name"], reason="clearly better options for the same need: " +
                                ", ".join(x["name"] for x in tops[:3]), signals=it["signals"]))
            else:
                keep.append(it)
        return keep, out

    def group(self, items):
        """Each card once, in its strongest category; categories ordered by how badly the deck
        needs them; inside a category the best first, but not five copies of the same effect."""
        order = {t: i for i, t in enumerate(TIERS)}
        by_cat = {}
        for it in items:
            by_cat.setdefault(it["category"], []).append(it)
        sev = {n["category"]: max(n["severity"], 0) for n in self.needs.values()}
        for n in self.needs.values():
            sev[n["category"]] = max(sev.get(n["category"], 0), n["severity"])
        groups = []
        for cat, lst in by_cat.items():
            lst.sort(key=lambda it: (order[it["tier"]], -it["confidence"], it["name"]))
            seen, first, later = Counter(), [], []
            for it in lst:  # diversity: a third card with the same main effect waits until the end
                f = it.get("main_function")
                (later if f and seen[f] >= 2 else first).append(it)
                seen[f] += 1
            lst = first + later
            need = self.needs[lst[0]["need_key"]]
            text = "better versions of cards your plan already relies on" if cat == "CORE STRATEGY UPGRADES" \
                else need["kind"] + ": " + need["label"]
            groups.append(dict(category=cat, need=text,
                               severity=sev.get(cat, 0), items=lst,
                               shown=sum(1 for it in lst if it["tier"] in SHOWN)))
        groups.sort(key=lambda g: (-max((1 if it["tier"] in SHOWN else 0) for it in g["items"]), -g["severity"],
                                   g["category"]))
        return groups

    def why_not(self, name):
        item, reason = self.evaluate(name)
        if item:
            return dict(name=item["name"], recommended=True, tier=item["tier_label"], why=item["why"],
                        signals=item["signals"])
        return dict(reason, recommended=False)


def needs_summary(needs):
    """Needs worth showing (most severe first)."""
    out = [dict(key=n["key"], label=(n["kind"] + ": " + n["label"]), category=n["category"], have=n["have"],
                enough=n["enough"], severity=n["severity"]) for n in needs.values() if n["severity"] >= MIN_NEED]
    return sorted(out, key=lambda n: -n["severity"])


def recommend(st, settings, feedback=None, community_rows=None, debug=False):
    """-> dict(items (flat, grouped order), groups, needs, rejected (debug), hidden, ...)"""
    r = Recommender(st, settings, feedback, community_rows)
    groups, rejected = r.run(debug=debug)
    items = [it for g in groups for it in g["items"]]
    shown = [it for it in items if it["tier"] in SHOWN]
    return dict(items=items, groups=[dict(category=g["category"], need=g["need"], shown=g["shown"],
                                          names=[it["name"] for it in g["items"]]) for g in groups],
                needs=needs_summary(r.needs), shown=len(shown), hidden=len(items) - len(shown),
                rejected=sorted(rejected, key=lambda x: -(x.get("signals") or {}).get("score", 0))[:60],
                mode=r.mode, recommender=r)


def commander_key(st):
    return community.commander_key([(st.cd.info(c["card_name"]) or {}).get("oracle_id") for c in st.ctx.commanders])


def why_not(udb, store, deck_id, settings, name, st=None):
    """Debug: why is (or isn't) this card recommended for this deck?"""
    st = st or analyser.prepare(udb, store, deck_id, settings)
    r = Recommender(st, settings, udb.suggestion_feedback(deck_id), community.load(commander_key(st)))
    return r.why_not(name)
