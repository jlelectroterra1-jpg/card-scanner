"""Find a cut: "If I want THIS card in the deck, what should come out?"

A swap is judged by the DECK, not by the card leaving: the deck after the swap is compared
with the deck before it. A perfectly reasonable card can still be the right cut when the
new card improves the deck more - it is never called "bad" for that.

swap_delta(IN, OUT) adds up, each part with a plain-English line:
  roles      ramp / draw / removal / wipes / protection / recursion / finishers ... against
             THIS deck's targets: creating a shortage costs a lot, trimming a surplus is a
             small gain, an over-covered role a small loss
  engine     the plan's pieces (token makers / payoffs, sacrifice outlets / death payoffs,
             counters, spells, landfall ...) and the tribe, the same way
  links      strength-weighted interactions with the rest of the deck (strong 1, moderate 0.25,
             x3 with the commander; weak links never count)
  commander  how directly each card feeds what the commander does
  quality    how good each card is at its job (what it does + Commander popularity; never price)
  curve      mana value, when the deck's curve is high or the job is the same
  combos     completing a combo one card away (scaled by the deck goal) - never decisive
  cost       what makes the OUT card matter that the parts above don't already count
             (engine piece, Game Changer, only card doing something, can win games...)

Never an OUT card: commanders, Never cut, Core, cards in a combo the deck already has, cards
the analyser can't understand, and swaps that would leave a key job short (unless the IN
card does that job too). Everything else may be compared.

The same engine answers "Find a cut for this card" (manual, from Consider, your collection
or any card), builds the bulk upgrade plan (greedy: best swap, apply it, re-evaluate - so
each OUT is used once and alternatives are listed separately) and ranks the Weakest slots
("if you need room, look here first")."""
import math
from collections import Counter

import analyser
import cardroles
import community
import recommend
import synergy

HIGH, GOOD, OPTIONAL = 0.30, 0.16, 0.07     # net deck improvement needed for each swap tier
TIER_LABEL = {"HIGH": "HIGH CONFIDENCE", "GOOD": "GOOD UPGRADE", "OPTIONAL": "OPTIONAL / PREFERENCE"}
ESSENTIAL = 4.0          # cut-review protection at least this high AND in the deck's top quarter
ESSENTIAL_SHARE = 0.75   #   = an essential piece of THIS deck: never an OUT
CRITICAL = {"Ramp", "Card Draw", "Removal"}  # never pushed below target by a swap that doesn't replace them
COMBO_BONUS = {"casual": 0.03, "improve": 0.08, "high": 0.15}
COST_PROTECTIONS = (("Official Game Changer", 1.0), ("One of the most played Commander cards", 0.6), ("Part of the", 0.6), ("Repeatable engine", 0.5),
                    ("Only card in the deck doing", 0.8), ("Can win games", 0.5), ("Payoff for", 0.4),
                    ("Supports the", 0.3), ("One-sided wipe", 1.0))
SPECIAL = ("Only card in the deck doing", "One-sided wipe", "Can win games", "Repeatable engine", "Part of the")
STRUCTURAL = 0.06        # role / engine gain needed to replace a card the cut review has no doubts about
REDUNDANT = 0.15         # adding one more of an over-covered job (before the roles weight)
MAX_PLAN = 25            # safety stop for the greedy plan (it normally stops far earlier, by itself)


def _v_role(c, lo, hi):
    """How well a count meets a target range: shortages hurt (steeply), surplus a little."""
    if c < lo:
        return -(lo - c) / max(1, lo)
    if c > hi:
        return -0.04 * (c - hi)
    return 0.0


def _v_piece(c, enough):
    if c < enough:
        return -(enough - c) / max(1, enough)
    if c > 2 * enough:
        return -0.02 * (c - 2 * enough)
    return 0.0


class Deck:
    """The comparable state of the deck: role counts, engine-piece counts, tribe, curve."""

    def __init__(self, cf, names):
        self.cf = cf
        self.names = set(names)
        self.roles = Counter()
        self.pieces = Counter()
        self.tribe = 0
        self.mv, self.spells = 0.0, 0
        for n in self.names:
            self.add(n, 1)

    def add(self, name, k):
        f = self.cf.facts(name)
        for r in f["roles"]:
            self.roles[r] += k
        for p in f["pieces"]:
            self.pieces[p] += k
        self.tribe += k * f["tribe"]
        if not f["land"]:
            self.mv += k * f["cmc"]
            self.spells += k
        (self.names.add if k > 0 else self.names.discard)(name)

    def avg_mv(self):
        return self.mv / self.spells if self.spells else 0.0


class CutFinder:
    def __init__(self, st, settings, feedback=None, community_rows=None, rec=None):
        self.st, self.settings = st, settings
        self.ctx, self.cd = st.ctx, st.cd
        self.goal = self.ctx.goal
        self.rec = rec or recommend.Recommender(st, settings, feedback, community_rows)
        self.community = community_rows or {}
        self.review = {v["name"]: v for v in st.review}
        ps = sorted(v["P"] for v in st.review if v["P"] < 50)  # (Never cut = 99)
        self.p_median = ps[len(ps) // 2] if ps else 0.0
        self.essential = max(ESSENTIAL, ps[int(len(ps) * ESSENTIAL_SHARE)] if ps else ESSENTIAL)
        self.flags = st.flags
        self.cmd_names = {c["card_name"] for c in self.ctx.commanders}
        self._facts = {}
        self.graph = st.graph
        # what counts: core roles with a target, the plan's engine pieces, the tribe
        self.role_targets = {r: self.ctx.guideline(r) for r in recommend.ROLE_CATEGORY if self.ctx.guideline(r)}
        self.piece_needs = {}
        for key, n in self.rec.needs.items():
            if key.startswith("piece:"):
                w = n.get("weight", 0.5) * (0.35 if n.get("niche") else 1.0)
                self.piece_needs[n["piece"]] = (n["enough"], w, n)
        self.tribe_need = self.rec.needs.get("tribe")
        self.combo_pieces = {}   # card -> "completes a combo with ..." (one card away)
        self.in_combo = {}       # card -> combo it is part of (already in the deck)
        for name, evs in self.community.items():
            for ev in evs:
                if ev["kind"] == "combo piece":
                    self.combo_pieces[name] = ev["detail"]
                elif ev["kind"] == "combo in deck":
                    self.in_combo[name] = ev["detail"]
        deck_main = [r["card_name"] for r in st.deck_rows if r["role"] == "main"]
        self.deck = Deck(self, deck_main + list(self.cmd_names))
        self.mv_target = analyser.AVG_MV_TARGET[self.goal]
        self._links = {}
        self._gen = 0  # bumps whenever the deck graph changes

    # ---- facts about one card ------------------------------------------------------------
    def facts(self, name):
        f = self._facts.get(name)
        if f is not None:
            return f
        info = self.cd.info(name) or {}
        real = info.get("name") or name
        roles = self.cd.roles.get(real, {})
        res = recommend.card_resources(self.cd, real, info, roles) if info else (set(), set(), set())
        front = (info.get("type_line") or "").split(" // ")[0]
        pieces = {p for p in recommend.PIECES if recommend._has_piece(res, p, info, roles)} if info else set()
        tribe = 0
        tn = self.rec.needs.get("tribe")
        if tn and tn["tribe"] in cardroles.creature_types(info.get("type_line")):
            tribe = 1
        q = self.rec.card_quality(real) if info else 0.0
        fit = self.rec.commander_fit(res) if info else 0.0
        if tribe:  # one of the tribe: direct synergy only when the commander itself names the tribe
            fit = max(fit, 0.8 if self.rec.cmd_tribal else 0.4)
        ev = self.rec.support.evaluate(info, roles) if info else dict(requirements=[], enabled=None,
                                                                       unsupported=[], text="")
        import enablement
        share = enablement.payoff_share(info, roles, ev) if info else 0.0
        unsupported = share * (1 - ev["enabled"]) if ev["enabled"] is not None else 0.0
        f = dict(name=real, info=info, roles={r for r in roles if r != "Land"}, role_conf=roles, res=res,
                 solid_roles={r for r, v in roles.items() if r != "Land" and v.confidence >= 0.6},
                 pieces=pieces, tribe=tribe, cmc=float(info.get("cmc") or 0), land="Land" in front,
                 quality=q, cmd_fit=fit, enablement=ev, unsupported=unsupported,
                 funcs=synergy.flat_functions(info, roles) if info else set())
        self._facts[name] = f
        return f

    def _all_links(self, name):
        key = (name, self._gen)
        links = self._links.get(key)
        if links is None:
            f = self.facts(name)
            if name in self.graph.cards:
                links = self.graph.detailed(name)
            else:
                links = self.graph.candidate_links(f["info"], self.cd.roles.get(f["name"], {}))
            self._links[key] = links
        return links

    def link_weight(self, name, exclude=None):
        """Interaction QUALITY with the current deck (synergy.link_value) - scaled down when the
        links only exist through a payoff the deck doesn't support."""
        links = [l for l in self._all_links(name) if l[0] != exclude]
        w = synergy.link_value(links, self.cmd_names) * (1 - 0.7 * self.facts(name)["unsupported"])
        return w, sum(1 for l in links if l[2] == "strong"), len(links), links

    def typical_links(self):
        """Median link weight of the deck's own cards - what 'well connected' means in THIS deck
        (in a go-wide deck almost every creature has 20+ links, so raw counts mean little)."""
        key = ("median", self._gen)
        if key not in self._links:
            ws = sorted(self.link_weight(n)[0] for n in self.graph.cards if n not in self.cmd_names)
            self._links[key] = ws[len(ws) // 2] if ws else 5.0
        return self._links[key]

    def illegal(self, out):
        v = self.review.get(out) or {}
        t = next((t for t, _ in v.get("evidence", []) if t.startswith(("Outside your", "Not legal"))), None)
        return t.lower() if t else None

    def doubted(self, out):
        """Is this a card the deck could part with easily: the cut review has reasons to think it's
        replaceable (or you said so), and it isn't a valued card - a Commander staple, a Game
        Changer, or a card doing three or more jobs?"""
        v = self.review.get(out) or {}
        if "not_important" in self.flags.get(out, set()) or self.illegal(out):
            return True
        if v.get("E", 0) < max(0.3, 0.5 * v.get("P", 0)):
            return False  # no real doubts, or far more reasons to keep it
        valued = any(t.startswith(("Official Game Changer", "One of the most played")) for t, _ in v["protection"])
        jobs = [r for r in v.get("roles", []) if r in analyser.GUIDELINES]
        return not valued and len(jobs) < 3

    def valued(self, out):
        """A card the deck shouldn't lose to a model's judgement: a Commander staple, a Game Changer,
        a card doing three or more jobs, or one the deck relies on more than its typical card."""
        v = self.review.get(out) or {}
        if any(t.startswith(("Official Game Changer", "One of the most played")) for t, _ in v.get("protection", [])):
            return True
        if len([r for r in v.get("roles", []) if r in analyser.GUIDELINES]) >= 3:
            return True
        return v.get("P", 0) > self.p_median

    def easy_slot(self, d):
        """Slots the bulk plan may use by itself (anything else is left to you via Find a cut):
        (a) a card the cut review has real doubts about; (b) a card in a deck job that's over
        THIS deck's target; (c) a clearly better card for the same core job (ramp, draw,
        removal...). (b) and (c) never touch a valued card."""
        out = d["out"]
        if self.doubted(out):
            return True
        if self.valued(out):
            return False
        if self.surplus_only(out):
            return True
        fi, fo = self.facts(d["into"]), self.facts(out)
        return bool(fi["solid_roles"] & fo["roles"] & set(self.role_targets)) and fi["quality"] >= fo["quality"] + 0.12

    def surplus_only(self, out, deck=None):
        """Every core job the card does is over THIS deck's target (e.g. the 5th board wipe in a
        deck that wants 0-2) - and it isn't a valued card."""
        deck = deck or self.deck
        fo = self.facts(out)
        core = fo["roles"] & set(self.role_targets)
        return bool(core) and not self.valued(out) and all(
            deck.roles[r] > self.role_targets[r][1] for r in core)

    def in_evidence(self, name):
        """(E, reasons): the cut review's doubts about a card as if it were in the deck."""
        key = ("in_ev", name)
        if key not in self._links:
            f = self.facts(name)
            if not f["info"]:
                self._links[key] = (0.0, [])
            else:
                v = analyser.review_card(self.ctx, self.st, f["name"], f["info"], self.cd.roles.get(f["name"], {}))
                # (doubts about links / engines only mean something for cards already in the deck graph)
                ev = [(t, w) for t, w in v["evidence"] if not t.startswith(
                    ("Not legal", "Outside your", "Its theme doesn't connect", "No clear deck job", "Rarely played",
                     "A "))]
                self._links[key] = (sum(w for _, w in ev), [t for t, _ in sorted(ev, key=lambda e: -e[1])])
        return self._links[key]

    # ---- can it leave at all? ---------------------------------------------------------------
    def blocked(self, out):
        """-> reason the card may never be an OUT card, or None."""
        if out in self.cmd_names:
            return "it's your commander"
        flags = self.flags.get(out, set())
        if "never_cut" in flags:
            return "you marked it Never cut"
        if "core" in flags:
            return "you marked it Core"
        if out in self.in_combo:
            return f"part of a combo in the deck ({self.in_combo[out]})"
        v = self.review.get(out)
        if v is None:
            return "lands are never cut here"
        if not v["understood"]:
            return "the analyser can't tell what it does - it may be there for a reason it can't see"
        if v["P"] >= self.essential:
            return "an essential piece of the deck (" + "; ".join(t for t, _ in v["protection"][:2]) + ")"
        return None

    # ---- the comparison ------------------------------------------------------------------
    def delta(self, in_name, out, deck=None):
        """Deck after (IN for OUT) vs before. in_name=None: just the cost of removing OUT.
        -> dict(net, parts, gains, losses, blocked, kind ...)."""
        deck = deck or self.deck
        fo = self.facts(out)
        fi = self.facts(in_name) if in_name else None
        gains, losses, parts = [], [], {}
        block = self.blocked(out)
        illegal = self.illegal(out)
        if illegal:  # it can't be played in this deck at all: whatever it "does" counts for nothing
            fo = dict(fo, roles=set(), solid_roles=set(), pieces=set(), tribe=0, quality=0.0, cmd_fit=0.0,
                      funcs=set())
            gains.append(f"replaces a card that can't be played here ({illegal})")
        # roles
        r_delta, shortage = 0.0, None
        for role, (lo, hi) in self.role_targets.items():
            before = deck.roles[role]
            after = before - (role in fo["roles"]) + (bool(fi) and role in fi["solid_roles"])
            if after == before:
                continue
            w = recommend.ROLE_WEIGHT.get(role, 0.6)
            d = w * (_v_role(after, lo, hi) - _v_role(before, lo, hi))
            if after > before and after > hi:
                d -= REDUNDANT  # one more of a job the deck already has too much of
            r_delta += d
            noun = recommend.ROLE_NOUN[role][0 if abs(after - before) == 1 else 1]
            state = f"{before} -> {after}" + (f", below this deck's {lo}-{hi}" if after < lo else
                                               f", above this deck's {lo}-{hi}" if after > hi else "")
            if after > before:
                if after <= hi:  # one more of something already over-covered is no gain
                    gains.append(f"+1 {noun} ({state})")
            else:
                losses.append(f"-1 {noun} ({state})")
                if after < lo and before >= lo and (role in CRITICAL or w >= 0.7) and shortage is None:
                    shortage = f"would leave the deck short of {recommend.ROLE_NOUN[role][1]} ({after}, this deck " \
                               f"wants {lo}-{hi})"
                elif after < lo and before < lo and role in CRITICAL and shortage is None:
                    shortage = f"the deck is already short of {recommend.ROLE_NOUN[role][1]} ({before}, wants " \
                               f"{lo}-{hi})"
        parts["roles"] = 0.6 * r_delta
        # engine pieces + tribe
        p_delta = 0.0
        for piece, (enough, w, n) in self.piece_needs.items():
            before = deck.pieces[piece]
            after = before - (piece in fo["pieces"]) + (bool(fi) and piece in fi["pieces"])
            if after == before:
                continue
            p_delta += w * (_v_piece(after, enough) - _v_piece(before, enough))
            text = f"1 {n['singular']} ({before} -> {after})"
            (gains if after > before else losses).append(("+" if after > before else "-") + text)
        if self.tribe_need:
            t_after = deck.tribe - fo["tribe"] + (fi["tribe"] if fi else 0)
            if t_after != deck.tribe:
                enough = self.tribe_need["enough"]
                p_delta += 0.6 * (_v_piece(t_after, enough) - _v_piece(deck.tribe, enough)) + 0.02 * (t_after - deck.tribe)
                (gains if t_after > deck.tribe else losses).append(
                    f"{'+' if t_after > deck.tribe else '-'}1 {self.tribe_need['tribe']} ({deck.tribe} -> {t_after})")
        parts["engine"] = 0.5 * p_delta
        # interactions
        wo, so, no, _ = (0.0, 0, 0, []) if illegal else self.link_weight(out)
        if fi:
            wi, si, ni, links_in = self.link_weight(in_name, exclude=out)
        else:
            wi, si, ni, links_in = 0.0, 0, 0, []
        parts["links"] = 0.15 * math.tanh((wi - wo) / max(8.0, self.typical_links()))
        if si != so or ni != no:
            text = (f"{si - so:+d} strong interactions ({so} -> {si}; {no} -> {ni} in all)" if si != so else
                    f"{ni - no:+d} interactions ({no} -> {ni})")
            (gains if wi >= wo else losses).append(text)
        # commander
        ci, co = (fi["cmd_fit"] if fi else 0.0), fo["cmd_fit"]
        parts["commander"] = 0.15 * (ci - co)
        cmd = next(iter(self.cmd_names), "your commander").split(",")[0]
        if ci >= co + 0.3:
            gains.append(f"more direct {cmd} synergy ({_fit_word(co)} -> {_fit_word(ci)})")
        elif co >= ci + 0.3:
            losses.append(f"less direct {cmd} synergy ({_fit_word(co)} -> {_fit_word(ci)})")
        # quality at its job
        qi, qo = (fi["quality"] if fi else 0.0), fo["quality"]
        parts["quality"] = 0.8 * (qi - qo) if fi else -0.3 * qo
        if fi and qi >= qo + 0.12:
            gains.append("a stronger card at its job (more efficient / more widely played)")
        elif fi and qo >= qi + 0.12:
            losses.append(f"{out} is the stronger card on its own")
        # curve / efficiency
        c_delta = 0.0
        if fi:
            dmv = fo["cmc"] - fi["cmc"]
            shared = fi["roles"] & fo["roles"]
            high = deck.avg_mv() > self.mv_target + 0.15
            if dmv and (high or shared):
                c_delta = max(-0.12, min(0.12, (0.03 if high else 0.015) * dmv))
                if dmv >= 1:
                    gains.append(f"cheaper: mana value {int(fo['cmc'])} -> {int(fi['cmc'])}")
                elif dmv <= -2:
                    losses.append(f"more expensive: mana value {int(fo['cmc'])} -> {int(fi['cmc'])}")
        parts["curve"] = c_delta
        # combos (influence, never control)
        parts["combo"] = 0.0
        if fi and in_name in self.combo_pieces:
            parts["combo"] = COMBO_BONUS.get(self.goal, 0.08)
            gains.append(f"Commander Spellbook: {self.combo_pieces[in_name]}")
        # payoff enablement: an IN card whose main ability has nothing to work with here is worth
        # much less than its text suggests; an OUT card in the same situation costs less to lose
        parts["enablement"] = -0.3 * (fi["unsupported"] if fi else 0.0) + 0.15 * fo["unsupported"]
        if fi and fi["enablement"]["unsupported"]:
            losses.append("note: " + fi["enablement"]["text"])
        elif fi and fi["enablement"]["text"]:
            gains.append(fi["enablement"]["text"])
        # a swap that changes what the slot does (board wipe -> creature) needs more evidence than a
        # like-for-like one; trimming an over-covered job needs less
        out_jobs = fo["roles"] & set(self.role_targets)
        if fi and out_jobs and not (fi["solid_roles"] & out_jobs):
            surplus = all(deck.roles[r] > self.role_targets[r][1] for r in out_jobs)
            parts["role_change"] = -0.03 if surplus else -0.08
        # what else makes OUT matter
        v = self.review.get(out) or dict(protection=[], evidence=[], E=0)
        cost, keep = 0.0, []
        in_funcs = fi["funcs"] if fi else set()
        for t, w in v["protection"]:
            for prefix, mult in COST_PROTECTIONS:
                if t.startswith(prefix):
                    if prefix == "Only card in the deck doing" and fi and fo["funcs"] & in_funcs:
                        break  # the new card does that job too
                    cost += mult * max(0.0, w)
                    keep.append(t)
                    break
        # how much more the deck relies on OUT than on its typical card
        parts["protection"] = -0.12 * cost - 0.05 * max(0.0, v.get("P", 0) - self.p_median)
        # name the special function that leaves with it (not just "-1 board wipe")
        if fi:
            losses += [f"loses: {t[0].lower() + t[1:]}" for t in keep if t.startswith(SPECIAL)][:1]
        # the same doubts the cut review has about a deck card, for the card coming IN
        # (a 6th board wipe in a go-wide deck, a card with no job here...)
        if fi:
            e_in, ev_in = self.in_evidence(in_name)
            parts["in_doubts"] = -0.08 * e_in
            losses += [f"note: {t}" for t in ev_in[:1]]
        # reasons the OUT card may be replaceable help a swap that already improves the deck -
        # they never turn a worse card into an upgrade
        before_evidence = sum(parts.values())
        bonus = min(0.12, 0.08 * v.get("E", 0)) + (0.05 if "not_important" in self.flags.get(out, set()) else 0.0)
        parts["evidence"] = bonus if (not fi or before_evidence > 0) else 0.0
        net = sum(parts.values())
        if shortage and not (fi and fi["roles"] & fo["roles"]):
            block = block or shortage
        shared_roles = bool(fi and fi["roles"] & fo["roles"])
        if fi and not block and fo["cmd_fit"] >= 0.8 and ci < fo["cmd_fit"] - 0.05 and qi < qo + 0.1:
            block = f"{out} feeds your commander directly; {in_name} doesn't as much"
        structural = parts["roles"] + parts["engine"]
        doubted = self.doubted(out)
        easy = doubted or self.surplus_only(out)
        if fi and not block and not easy and structural < STRUCTURAL and not (shared_roles and qi >= qo + 0.08):
            block = (f"the deck has no reason to cut {out}, and {in_name} doesn't fill anything missing or do the "
                     "same job clearly better")
        net_tier = tier_of(net)
        if fi and not doubted and net_tier == "HIGH":
            net_tier = "GOOD"  # replacing a card nothing argues against is never 'high confidence'
        if fi and net_tier == "HIGH" and (qo > qi + 0.05 or any("below this deck" in l for l in losses)):
            net_tier = "GOOD"
        if fi and net_tier in ("HIGH", "GOOD") and qo > qi + 0.15:
            net_tier = "OPTIONAL"
        kind = None
        if fi:
            shared = fi["roles"] & fo["roles"]
            if shared and fi["cmc"] <= fo["cmc"] - 2:
                kind = "curve correction"
            elif shared:
                kind = "role-for-role"
            elif any(r in fo["roles"] and deck.roles[r] > self.role_targets[r][1] for r in self.role_targets):
                kind = "redundancy reduction"
            elif fi["pieces"] or fi["cmd_fit"] > fo["cmd_fit"] or fi["tribe"] > fo["tribe"]:
                kind = "strategy sharpening"
            else:
                kind = "upgrade"
        return dict(into=in_name, out=out, net=round(net, 3), tier=net_tier,
                    parts={k: round(x, 3) for k, x in parts.items()},
                    gains=gains, losses=losses, blocked=block, kind=kind, keep=keep, links_in=links_in,
                    shortage=shortage)

    # ---- one IN card ----------------------------------------------------------------------
    def options(self, in_name, deck=None, limit=5, varied=True):
        """Every sensible OUT for one IN card, best first: (options, rejected_summary)."""
        deck = deck or self.deck
        f = self.facts(in_name)
        outs, blocked = [], Counter()
        for out in sorted(deck.names - self.cmd_names):
            if out == f["name"] or self.facts(out)["land"]:
                continue
            d = self.delta(in_name, out, deck)
            if d["blocked"]:
                blocked[_block_kind(d["blocked"])] += 1
                continue
            outs.append(d)
        outs.sort(key=lambda d: -d["net"])
        good = [d for d in outs if d["net"] >= OPTIONAL]
        return (self._varied(good, f, limit) if varied else good[:limit]), dict(
            blocked=dict(blocked), best=outs[0] if outs else None,
                                                  compared=len(outs))

    def _varied(self, good, f, limit):
        """Best cut first; then the best like-for-like cut (same job) and options from different
        kinds of slot, so the list isn't five cards that all leave for the same reason."""
        if not good:
            return []
        picked = [good[0]]
        same_job = next((d for d in good[1:] if self.facts(d["out"])["roles"] & f["roles"]), None)
        if same_job is not None:
            picked.append(same_job)

        def slot(d):
            return frozenset(self.facts(d["out"])["roles"] & set(self.role_targets)) or frozenset({d["kind"]})
        seen = {slot(d) for d in picked}
        for d in good[1:]:
            if len(picked) >= limit:
                break
            if d not in picked and slot(d) not in seen:
                picked.append(d)
                seen.add(slot(d))
        for d in good[1:]:  # fill up with the next best if there aren't enough different slots
            if len(picked) >= limit:
                break
            if d not in picked:
                picked.append(d)
        return picked[:1] + sorted(picked[1:limit], key=lambda d: -d["net"])

    def explain(self, d):
        """Plain-English case for one swap (what the UI shows)."""
        out, into = d["out"], d["into"]
        v = self.review.get(out) or {}
        fo, fi = self.facts(out), self.facts(into)
        why_out = [t for t, _ in sorted(v.get("evidence", []), key=lambda e: -e[1])]
        slot = self.flexibility(out)
        if not why_out:
            why_out = slot["why"][:2] or [f"{into} improves the deck more than {out} does in this slot"]
        why_in = []
        n_in = self.rec.needs
        for key in self.rec._addressed(fi["info"], self.cd.roles.get(fi["name"], {}), fi["res"]):
            n = n_in[key]
            if n["severity"] >= recommend.MIN_NEED:
                why_in.append(self.rec._need_line(n, self.cd.roles.get(fi["name"], {}), fi["info"]))
                break
        if d["links_in"]:
            strong = [o for o, _, s in d["links_in"] if s == "strong"]
            why_in.append(f"Works with {len(d['links_in'])} cards here ({len(strong)} strong; e.g. "
                          f"{', '.join((strong or [o for o, _, _ in d['links_in']])[:3])})")
        if fi["cmd_fit"] >= 0.7:
            why_in.append(self.rec._commander_line(fi["res"], next(
                (l for l in d["links_in"] if l[0] in self.cmd_names), (next(iter(self.cmd_names)), "", "strong"))))
        if not why_in:
            why_in.append("A stronger card for this deck's plan")
        net, tier = d["net"], d.get("tier")
        verdict = ("not an improvement" if net < 0 else "about even" if net < OPTIONAL else
                   {"HIGH": "clearly better for this deck", "GOOD": "better for the current plan"}.get(
                       tier, "a small, preference-level improvement"))
        summary = []
        for g in d["gains"][:3]:
            summary.append(g.split(" (")[0])
        for l in d["losses"][:2]:
            summary.append(l.split(" (")[0])
        return dict(why_out=why_out[:3], why_in=why_in[:3], gains=d["gains"], losses=d["losses"],
                    keep=d["keep"][:2], net=f"{verdict}: " + (", ".join(summary) if summary else "small changes"),
                    kind=d["kind"], changes=self.changes(d), cut_word=_cut_word(slot, self.doubted(out), v),
                    replacement_word=_replacement_word(d))

    def changes(self, d):
        """The before/after differences by kind: roles, synergy, mana curve, interactions."""
        lines = d["gains"] + d["losses"]
        roles = [x for x in lines if x.startswith(("+1 ", "-1 ")) and "interactions" not in x]
        synergy = [x for x in lines if "synergy" in x or x.startswith(("+1 Human", "-1 Human"))
                   or "Commander Spellbook" in x]
        curve = [x for x in lines if "mana value" in x]
        links = [x for x in lines if "interactions" in x]
        fo, fi = self.facts(d["out"]), self.facts(d["into"])
        if not curve and fo["cmc"] != fi["cmc"]:
            curve = [f"mana value {int(fo['cmc'])} -> {int(fi['cmc'])}"]
        return dict(roles=roles or ["no change to the deck's jobs"], synergy=synergy or ["about the same"],
                    curve=curve or ["same mana value"], interactions=links or ["about the same"])

    def find(self, in_name, limit=5):
        """Manual 'Find a cut for this card'."""
        info = self.cd.info(in_name)
        if info is None:
            raise ValueError(f"unknown card {in_name}")
        name = info["name"]
        if name in self.deck.names:
            return dict(into=name, options=[], already=True, verdict="It's already in the deck.", reasons=[])
        avail = self.rec.availability(name, info)
        notes = []
        if avail and ("colour identity" in avail or "not legal" in avail):
            return dict(into=name, options=[], verdict=f"Can't go in this deck: {avail}.", reasons=[])
        own = analyser.ownership_of(self.ctx, name)
        fin = self.facts(name)
        over = [r for r in fin["roles"] & set(self.role_targets) if self.deck.roles[r] >= self.role_targets[r][1]]
        if over and not (fin["roles"] & set(self.role_targets)) - set(over):
            r = over[0]
            notes.append(f"The deck already has {self.deck.roles[r]} {recommend.ROLE_NOUN[r][1]} (this deck wants "
                         f"{self.role_targets[r][0]}-{self.role_targets[r][1]}) - one more mainly pays off if it frees a "
                         "surplus slot or replaces a weaker one")
        opts, info_rej = self.options(name, limit=limit)
        out = []
        for i, d in enumerate(opts):
            e = self.explain(d)
            out.append(dict(out=d["out"], tier=d["tier"], tier_label=TIER_LABEL[d["tier"]],
                            rank=("BEST CUT" if i == 0 else "ALTERNATIVE"), net_value=d["net"],
                            cut_ease=self.flexibility(d["out"])["level"], **e, parts=d["parts"],
                            cut_confidence=_conf(d["net"]), why_cut=e["why_out"], why_keep=e["keep"]))
        reasons = []
        if not out:
            b = info_rej["blocked"]
            if b:
                reasons.append("protected or essential: " + ", ".join(f"{n} {k}" for k, n in b.items()))
            best = info_rej["best"]
            if best is not None:
                reasons.append(f"the closest option ({best['out']}) would not improve the deck "
                               f"({self.explain(best)['net']})")
            reasons.append(f"compared against {info_rej['compared']} cards that could leave")
        return dict(into=name, ownership=own, options=out, notes=notes,
                    verdict=None if out else "No safe cut found.", reasons=reasons,
                    replacement_confidence=max([o["net_value"] for o in out] + [0.0]),
                    why_in=out[0]["why_in"] if out else [])

    # ---- slot flexibility (the Weakest tab) -----------------------------------------------
    def flexibility(self, out):
        key = ("flex", out, self._gen)
        if key in self._links:
            return self._links[key]
        d = self.delta(None, out)
        cost = -d["net"]
        level = "HIGH" if cost < 0.15 else "MEDIUM" if cost < 0.35 else "LOW"
        fo = self.facts(out)
        why, keep = [], []
        v = self.review.get(out) or {}
        why += [t for t, _ in v.get("evidence", [])][:2]
        for role in fo["roles"] & set(self.role_targets):
            lo, hi = self.role_targets[role]
            have = self.deck.roles[role]
            if have > hi:
                why.append(f"{recommend.ROLE_NOUN[role][1]} are over-covered ({have}; this deck wants {lo}-{hi})")
            elif have - 1 < lo:
                keep.append(f"one of only {have} {recommend.ROLE_NOUN[role][1]} (this deck wants {lo}-{hi})")
        similar = sum(1 for n in self.deck.names if n != out and self.facts(n)["funcs"] & fo["funcs"])
        if similar >= 4:
            why.append(f"{similar} other cards do a similar job")
        w, s, n, _ = self.link_weight(out)
        if s <= 1:
            why.append(f"few interactions here ({s} strong, {n} in all)")
        else:
            keep.append(f"{s} strong interactions with the deck")
        if fo["cmd_fit"] < 0.3:
            why.append("little to do with your commander")
        keep += d["keep"]
        res = dict(level=level, cost=round(cost, 3), why=why, keep=keep, losses=d["losses"], blocked=d["blocked"])
        self._links[key] = res
        return res

    def weakest_slots(self, n=10):
        rows = []
        for out in self.deck.names - self.cmd_names:
            if self.facts(out)["land"] or self.blocked(out):
                continue
            fl = self.flexibility(out)
            if fl["blocked"]:
                continue  # e.g. removing it would leave the deck short of removal
            rows.append((fl["cost"], out, fl))
        rows.sort()
        return rows[:n]

    # ---- the bulk upgrade plan --------------------------------------------------------------
    def plan(self, candidates, budget=None):
        """candidates: [(name, tier_from_consider_or_None, ownership)] -> (swaps, alternatives).
        Greedy: take the best swap, apply it, re-evaluate. Each OUT and each IN at most once."""
        deck = Deck(self, self.deck.names)  # a working copy: the real deck state stays as it is
        swaps, alternatives, spent = [], [], 0.0
        used_in = set()
        while len(swaps) < MAX_PLAN:
            best, rivals = None, []
            for name, ctier, own in candidates:
                if name in used_in or name in deck.names:
                    continue
                if budget is not None and (own.get("cost") is None or spent + own["cost"] > budget + 1e-9) \
                        and own.get("status") != "available":
                    continue
                opts, _ = self.options(name, deck, limit=60, varied=False)
                # an idea that is only WORTH CONSIDERING (or not on the Consider list) may only take
                # the slot of a card the analyser already has doubts about
                # the plan only uses slots the deck can part with easily, or makes a clearly better
                # like-for-like swap; any other cut is left to you (Find a cut shows it, labelled)
                opts = [o for o in opts if self.easy_slot(o)]
                if ctier not in ("EXCELLENT", "STRONG"):  # weaker ideas: only slots the deck clearly doesn't need
                    opts = [o for o in opts if self.doubted(o["out"]) or self.surplus_only(o["out"], deck)]
                if not opts:
                    continue
                d = opts[0]
                adj = d["net"] - _penalty(ctier)
                rivals.append((adj, d, ctier, own, opts))
                if best is None or adj > best[0]:
                    best = (adj, d, ctier, own, opts)
            if best is None or best[0] < OPTIONAL:
                break
            adj, d, ctier, own, opts = best
            tier = _min_tier(tier_of(adj), d["tier"])
            if tier == "HIGH" and self.st.uncertain and not self.illegal(d["out"]):
                tier = "GOOD"  # strategy unclear: nothing is 'high confidence'
            d = dict(d, tier=tier)
            e = self.explain(d)
            same_out = sorted([(r[0], r[1]["into"]) for r in rivals if r[1]["out"] == d["out"]
                               and r[1]["into"] != d["into"] and r[0] >= OPTIONAL], reverse=True)
            swaps.append(dict(d=d, e=e, tier=tier, ownership=own, consider_tier=ctier, adj=adj,
                              other_outs=[o["out"] for o in opts[1:3]],
                              other_ins=[n for _, n in same_out[:3]]))
            if same_out:
                alternatives.append(dict(out=d["out"], chosen=d["into"], also=[n for _, n in same_out[:4]]))
            used_in.add(d["into"])
            if own.get("status") != "available":
                spent += own.get("cost") or 0.0
            deck.add(d["out"], -1)
            deck.add(d["into"], 1)
            self._rebuild_graph(deck)
        self.graph = self.st.graph
        self._gen += 1
        self._links.clear()
        return swaps, alternatives

    def _rebuild_graph(self, deck):
        cards = {}
        for n in deck.names:
            f = self.facts(n)
            if f["info"] and not f["land"]:
                cards[n] = (f["info"], self.cd.roles.get(f["name"], {}))
        self.graph = synergy.InteractionGraph(cards, list(self.cmd_names), self.ctx.tribe)
        self._gen += 1


def _fit_word(x):
    return "direct" if x >= 0.95 else "strong" if x >= 0.7 else "indirect" if x > 0 else "none"


def _block_kind(text):
    kinds = (("Never cut", "never-cut"), ("Core", "core"), ("combo", "combo piece"), ("essential", "essential"),
             ("short", "would create a shortage"), ("can't tell", "not understood"),
             ("no reason to cut", "nothing to gain over them"), ("feeds your commander", "feed the commander more"),
             ("your commander", "commander"))
    for k, label in kinds:
        if k.lower() in text.lower():
            return label
    return "other"


def _penalty(consider_tier):
    """WORTH CONSIDERING (and cards not on the Consider list) need stronger comparative evidence."""
    return {"EXCELLENT": 0.0, "STRONG": 0.0, "WORTH": 0.06, "NICHE": 0.12}.get(consider_tier, 0.08)


def _cut_word(slot, doubted, review):
    """How comfortable the cut is: HIGH = a slot the deck can spare, LOW = a card it relies on."""
    if doubted or slot["level"] == "HIGH":
        return "HIGH - an easy slot to free"
    if slot["level"] == "MEDIUM" and review.get("P", 0) < 2.5:
        return "MEDIUM - a reasonable card, but not one the deck depends on"
    return "LOW - the deck gets real value from it; only if you want the new card"


def _replacement_word(d):
    return {"HIGH": "HIGH - clearly improves the deck", "GOOD": "MEDIUM - better for the current plan"}.get(
        d.get("tier"), "LOW - a matter of preference")


def _min_tier(a, b):
    order = ("HIGH", "GOOD", "OPTIONAL")
    return order[max(order.index(a), order.index(b))]


def tier_of(net):
    return "HIGH" if net >= HIGH else "GOOD" if net >= GOOD else "OPTIONAL"


def _conf(net):
    return round(max(0.0, min(0.95, 0.4 + net)), 2)


def finder_for(udb, store, deck_id, settings, st=None):
    st = st or analyser.prepare(udb, store, deck_id, settings)
    comm = community.load(recommend.commander_key(st))
    return CutFinder(st, settings, udb.suggestion_feedback(deck_id), comm), st


def collection_candidates(cf, limit=120):
    """Free copies you own that could plausibly matter here (legal, in colours, not in the deck)."""
    out = []
    for name, own in cf.ctx.ownership.items():
        if own["free"] <= 0:
            continue
        info = cf.cd.info(name)
        if info is None or cf.rec.availability(info["name"], info):
            continue
        f = cf.facts(info["name"])
        w, s, n, _ = cf.link_weight(info["name"])
        pre = f["quality"] + 0.03 * s + 0.3 * f["cmd_fit"] + 0.2 * bool(f["roles"] & set(cf.role_targets))
        out.append((pre, info["name"]))
    out.sort(reverse=True)
    return [n for _, n in out[:limit]]
