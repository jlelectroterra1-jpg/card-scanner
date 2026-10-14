"""The Commander deck analyser - our own, local, explainable engine (no EDHREC, no AI).

analyse(udb, store, deck_id, settings) works through:
  1. the deck: card roles (cardroles), composition, mana curve, colour pips, lands
  2. the plan: commander strategy (strategy.detect) or the user's own deck goals
  3. weaknesses: role counts against configurable guideline ranges (GUIDELINES)
  4. scoring: every card gets separate components (commander synergy, strategy, role
     need, mana efficiency, interaction with the deck, general quality) - kept so each
     suggestion can explain itself; the weighted total depends on the deck goal
  5. cut review: every card gets itemised CUT EVIDENCE (off-plan, redundant function,
     inefficient vs same-function cards, illegal, self-defeating wipe...) and itemised
     PROTECTION (Game Changer, staple, commander/deck interactions from the synergy graph,
     strategy support, engine, unique function, sole role, finisher, "we don't understand
     it", your own pins). Only clear evidence that outweighs protection makes a cut.
  6. replacements: staged filtering of legal cards (legal -> colour identity -> relevant
     role/theme -> owned & free / budget -> scoring) with a separate replacement confidence;
     a replacement must be able to do the OUT card's job (same function, see synergy.py)
  7. swaps only where cut AND replacement confidence are both high enough (the package size
     is a maximum, never a target); good additions without a confident cut are listed as
     "cards worth considering"
Nothing changes a deck until apply_swaps() is called (which snapshots first; undo_last()
puts it back)."""
import hashlib
import json
import os
import re
import time
from collections import Counter

import cardroles
import deckrules
import commander
import strategy
import synergy

# ---------------------------------------------------------------- guidance (edit freely)

GOALS = {"casual": "Casual", "improve": "Improve It", "high": "High Power"}
# Typical ranges by deck goal - guidelines, not rules.
GUIDELINES = {
    "Lands": {"casual": (36, 39), "improve": (35, 38), "high": (31, 36)},
    "Ramp": {"casual": (8, 12), "improve": (9, 13), "high": (10, 15)},
    "Card Draw": {"casual": (7, 12), "improve": (9, 14), "high": (10, 16)},
    "Removal": {"casual": (5, 9), "improve": (6, 10), "high": (7, 12)},
    "Board Wipe": {"casual": (1, 4), "improve": (2, 4), "high": (1, 3)},
    "Counterspell": {"casual": (0, 4), "improve": (1, 5), "high": (3, 8)},  # only for decks with blue
    "Protection": {"casual": (1, 4), "improve": (2, 5), "high": (2, 6)},
    "Tutor": {"casual": (0, 3), "improve": (0, 4), "high": (2, 8)},
    "Recursion": {"casual": (1, 5), "improve": (2, 5), "high": (1, 5)},
    "Finisher": {"casual": (2, 6), "improve": (2, 5), "high": (1, 4)},
}
AVG_MV_TARGET = {"casual": 3.6, "improve": 3.3, "high": 2.9}
# All Cards mode: cards you'd have to BUY must show some sign of being widely played -
# printed in a few sets or in a Commander product (local Scryfall data). Owned cards are exempt.
MIN_REPRINT_SCORE = 0.5
WEIGHTS = {  # how much each score component counts, by deck goal
    "casual": dict(commander=.30, strategy=.28, need=.15, efficiency=.05, interaction=.16, quality=.06),
    "improve": dict(commander=.22, strategy=.20, need=.22, efficiency=.12, interaction=.12, quality=.12),
    "high": dict(commander=.12, strategy=.12, need=.24, efficiency=.24, interaction=.06, quality=.22),
}
IDEAL_MV = {"Ramp": 2, "Card Draw": 3, "Removal": 2, "Board Wipe": 4, "Counterspell": 2, "Protection": 2,
            "Tutor": 2, "Recursion": 3, "Finisher": 6, "Reanimation": 3}
NARROW = {"Graveyard Hate", "Mill", "Discard", "Group Hug", "Lifegain"}
COMPONENT_LABELS = dict(commander="Commander synergy", strategy="Deck strategy", need="Fills a needed role",
                        efficiency="Mana efficiency", interaction="Works with the deck", quality="General strength")
ANALYSER_VERSION = 7


# ---------------------------------------------------------------- card data

class CardData:
    """Oracle data + cached roles for every card (loaded once, ~1-2 s; roles are cached in
    user.db and rebuilt only when the classifier or cards.db changes)."""

    def __init__(self, udb, progress=None):
        self.udb, self.conn = udb, udb.conn
        udb.attach_cards_db()
        self.stamp = self._stamp()
        self.cards = {}
        for r in self.conn.execute("""SELECT name, oracle_id, mana_cost, cmc, type_line, oracle_text, keywords, power,
                                             color_identity, legal_commander, game_changer FROM scry.oracle_cards"""):
            self.cards[r["name"]] = dict(r)
        self.roles = self._load_roles(progress)
        # Optional, minor signal: Scryfall's documented edhrec_rank (overall Commander popularity).
        # Present once Update Prices has rebuilt cards.db with it; never the definition of quality.
        cols = {r[1] for r in self.conn.execute("PRAGMA scry.table_info(oracle_cards)")}
        self.popularity = {r[0]: r[1] for r in self.conn.execute(
            "SELECT name, edhrec_rank FROM scry.oracle_cards WHERE edhrec_rank IS NOT NULL")} \
            if "edhrec_rank" in cols else {}
        self.prices = {r[0]: r[1] for r in self.conn.execute(
            """SELECT name, MIN(CAST(usd AS REAL)) FROM scry.cards WHERE usd IS NOT NULL AND lang = 'en'
               GROUP BY name""")}
        # How often a card has been printed: METADATA only - used to keep obscure cards out of
        # "buy this" suggestions when no popularity data exists. Not a measure of card quality.
        import math
        self.reprints = {}
        for r in self.conn.execute(
                """SELECT name, COUNT(DISTINCT set_code), SUM(set_name LIKE '%Commander%') FROM scry.cards
                   WHERE lang = 'en' GROUP BY name"""):
            self.reprints[r[0]] = min(1.0, math.log2(1 + r[1]) / 5 + (0.2 if r[2] else 0))

    def _stamp(self):
        r = self.conn.execute("SELECT COUNT(*), MAX(oracle_id) FROM scry.oracle_cards").fetchone()
        p = self.conn.execute("SELECT COUNT(*), SUM(CAST(COALESCE(usd, 0) AS REAL)) FROM scry.cards").fetchone()
        return f"{cardroles.CLASSIFIER_VERSION}:{r[0]}:{r[1]}:{p[0]}:{round(p[1] or 0)}"

    def _load_roles(self, progress):
        role_stamp = f"{cardroles.CLASSIFIER_VERSION}:{len(self.cards)}"
        if self.udb.meta_get("role_cache_stamp") == role_stamp:
            return {r[0]: cardroles.roles_from_json(r[1]) for r in self.conn.execute(
                "SELECT name, roles FROM card_role_cache")}
        out, rows = {}, []
        for i, (name, c) in enumerate(self.cards.items()):
            roles = cardroles.classify(c)
            out[name] = roles
            rows.append((c["oracle_id"], name, cardroles.roles_to_json(roles)))
            if progress and i % 3000 == 0:
                progress(0.05 + 0.4 * i / len(self.cards), "Working out what every card does (first time only)...")
        with self.udb.transaction() as c:
            c.execute("DELETE FROM card_role_cache")
            c.executemany("INSERT OR REPLACE INTO card_role_cache (oracle_id, name, roles) VALUES (?, ?, ?)", rows)
            self.udb.meta_set("role_cache_stamp", role_stamp, _conn=c)
        return out

    def info(self, name):
        c = self.cards.get(name)
        if c is None and " // " not in name:
            c = next((v for k, v in self.cards.items() if k.startswith(name + " // ")), None)
        return c


_CARDDATA = {}


def card_data(udb, progress=None):
    """Shared CardData per database file, reloaded when cards.db or the classifier changes."""
    key = udb.path
    cd = _CARDDATA.get(key)
    if cd is not None:
        cd.udb, cd.conn = udb, udb.conn
        udb.attach_cards_db()
        if cd._stamp() == cd.stamp:
            return cd
    cd = CardData(udb, progress)
    _CARDDATA[key] = cd
    return cd


# ---------------------------------------------------------------- fingerprint (cache key)

def fingerprint(udb, deck_id, settings, stamp=""):
    deck = [tuple(r) for r in udb.conn.execute(
        """SELECT card_name, scryfall_id, quantity, role, finish, collection_item_id FROM deck_cards
           WHERE deck_id = ? ORDER BY id""", (deck_id,))]
    coll = tuple(udb.conn.execute("""SELECT COUNT(*), COALESCE(SUM(quantity), 0), MAX(updated_at), MAX(id)
                                     FROM collection_items""").fetchone())
    alloc = tuple(udb.conn.execute("""SELECT COUNT(*), COALESCE(SUM(quantity), 0), COALESCE(SUM(collection_item_id), 0)
                                      FROM deck_cards WHERE collection_item_id IS NOT NULL""").fetchone())
    pins = sorted(tuple(r) for r in udb.conn.execute(
        "SELECT card_name, flag FROM deck_card_flags WHERE deck_id = ?", (deck_id,)))
    blob = json.dumps([ANALYSER_VERSION, deck, coll, alloc, settings, stamp, pins], sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()


# ---------------------------------------------------------------- the analysis

class Context:
    """Everything about one deck that scoring needs."""

    def __init__(self, cd, deck_rows, settings, ownership, detected):
        self.cd, self.settings = cd, settings
        self.goal = settings.get("goal", "improve")
        self.commanders = [r for r in deck_rows if r["role"] in ("commander", "partner")]
        self.cards = [r for r in deck_rows if r["role"] in ("commander", "partner", "main")]
        self.identity = deckrules.colour_identity(*(r.get("color_identity") for r in self.commanders))
        self.detected = detected
        tags = settings.get("tags")
        self.user_tags = bool(tags)
        primary = tags if tags else detected["primary"]
        secondary = [] if tags else detected["secondary"]
        self.themes = {t: 1.0 for t in primary}
        self.themes.update({t: 0.55 for t in secondary if t not in self.themes})
        self.tribe = detected.get("tribe") if ("Tribal" in self.themes or not tags) else None
        self.commander_themes = {}
        for c in self.commanders:
            info = cd.info(c["card_name"]) or {}
            for t, w in strategy.card_themes(info, cd.roles.get(info.get("name"), {})).items():
                self.commander_themes[t] = max(self.commander_themes.get(t, 0), w)
        if not tags:  # your own strategy tags win: detected themes don't get a say then
            for t in detected["primary"]:
                self.commander_themes[t] = max(self.commander_themes.get(t, 0), 0.8)
        self.ownership = ownership
        self.counts = Counter()
        self.theme_counts = Counter()
        self.tribe_count = 0
        for r in self.cards:
            info = cd.info(r["card_name"]) or {}
            for role in cd.roles.get(info.get("name"), {}):
                self.counts[role] += r["quantity"]
            for t in strategy.card_themes(info, cd.roles.get(info.get("name"), {})):
                self.theme_counts[t] += r["quantity"]
            if self.tribe and self.tribe in cardroles.creature_types(info.get("type_line")):
                self.tribe_count += r["quantity"]
        self.counts["Lands"] = sum(r["quantity"] for r in self.cards
                                   if "Land" in (r.get("type_line") or "").split(" // ")[0])

    targets = {}   # deck-specific target ranges (deck_targets), filled in by prepare()
    profile = None  # commander profile (commander.py)

    def guideline(self, role):
        if role in self.targets:
            return self.targets[role]
        if role == "Counterspell" and "U" not in self.identity:
            return None
        g = GUIDELINES.get(role)
        return g[self.goal] if g else None

    def need(self, role, counts=None):
        """0 = plenty, 1 = badly missing."""
        g = self.guideline(role)
        if not g:
            return 0.0
        have = (counts or self.counts)[role]
        return max(0.0, min(1.0, (g[0] - have) / max(1, g[0])))

    def surplus(self, role, counts=None):
        g = self.guideline(role)
        return bool(g and (counts or self.counts)[role] > g[1])


def _obscure(cd, name):
    """Rarely played: by Scryfall's EDHREC rank when known, else (metadata only) rarely printed."""
    rank = cd.popularity.get(name)
    if rank is not None:
        return rank > 15000
    return cd.reprints.get(name, 0) < 0.5  # no popularity data yet (run Update Prices): stricter filter


def is_staple(ctx, s):
    """Official Game Changer, or (when the data is there) one of the most played Commander cards."""
    name = s["info"].get("name") or s["name"]
    rank = ctx.cd.popularity.get(name)
    return bool(s["info"].get("game_changer")) or (rank is not None and rank <= 250)


def score(ctx, name, counts=None, in_deck=False):
    """Score components (0..1 each) + weighted total for a card in this deck."""
    cd = ctx.cd
    info = cd.info(name) or {}
    roles = cd.roles.get(info.get("name") or name, {})
    themes = strategy.card_themes(info, roles)
    comp = {}
    comp["commander"] = min(1.0, sum(min(w, ctx.commander_themes.get(t, 0)) for t, w in themes.items()))
    if ctx.tribe and ctx.tribe in cardroles.creature_types(info.get("type_line")):
        comp["commander"] = min(1.0, comp["commander"] + 0.5)
    comp["strategy"] = min(1.0, sum(w * ctx.themes.get(t, 0) for t, w in themes.items()))
    core = [r for r in roles if r in GUIDELINES]
    comp["need"] = max([ctx.need(r, counts) * roles[r].confidence for r in core] + [0.0])
    cmc = float(info.get("cmc") or 0)
    main_role = max(core, key=lambda r: roles[r].confidence) if core else None
    ideal = IDEAL_MV.get(main_role, 3.5)
    comp["efficiency"] = max(0.0, min(1.0, 1 - max(0.0, cmc - ideal) / 4))
    share = sum(ctx.theme_counts.get(t, 0) - (1 if in_deck else 0) for t in themes if t in ctx.themes)
    comp["interaction"] = min(1.0, max(0.0, share) / 20)
    useful = [r for r in roles if r != "Land"]
    # General strength from what the card DOES (jobs, how sure we are, repeatable engine) -
    # price and print count are not quality. Popularity (EDHREC rank via Scryfall) is a small
    # optional extra when present.
    q = 0.2 * min(3, len(useful)) + 0.35 * max([v.confidence for v in roles.values()] + [0])
    if synergy.is_engine(info, roles):
        q += 0.15
    rank = cd.popularity.get(info.get("name") or name)
    if rank:
        import math
        q += 0.15 * max(0.0, 1 - math.log10(rank) / 4.5)
    if info.get("game_changer"):
        q += 0.35 if ctx.goal == "high" else 0.1
    comp["quality"] = min(1.0, q)
    w = dict(WEIGHTS[ctx.goal])
    if ctx.settings.get("mode") == "all":  # when buying cards, general strength matters more
        w["quality"] += 0.08
        w["commander"] -= 0.04
        w["strategy"] -= 0.04
    total = sum(w[k] * comp[k] for k in w)
    if in_deck and core and all(ctx.surplus(r, counts) for r in core):
        total -= 0.06  # its jobs are already over-covered
    return dict(name=name, total=total, comp=comp, roles=roles, themes=themes, cmc=cmc, core=core,
                main_role=main_role, info=info)


def composition(ctx):
    cd = ctx.cd
    curve, pips = Counter(), Counter()
    total_mv, n_spells, basics, nonbasics = 0.0, 0, 0, 0
    for r in ctx.cards:
        info = cd.info(r["card_name"]) or {}
        front = (r.get("type_line") or info.get("type_line") or "").split(" // ")[0]
        if "Land" in front:
            if deckrules.is_basic(r["card_name"], r.get("type_line")):
                basics += r["quantity"]
            else:
                nonbasics += r["quantity"]
            continue
        mv = float(info.get("cmc") or 0)
        bucket = min(6, int(mv))
        curve[bucket] += r["quantity"]
        total_mv += mv * r["quantity"]
        n_spells += r["quantity"]
        cost = info.get("mana_cost") or ""
        for sym in cost.replace("}", "").split("{"):
            for ch in sym:
                if ch in "WUBRG":
                    pips[ch] += (1 if len(sym) == 1 else 0.5) * r["quantity"]
    roles = {role: ctx.counts.get(role, 0) for role in cardroles.ROLES if role != "Land" and ctx.counts.get(role)}
    return dict(lands=basics + nonbasics, basics=basics, nonbasics=nonbasics, curve={k: curve.get(k, 0) for k in range(7)},
                avg_mv=round(total_mv / n_spells, 2) if n_spells else 0.0, spells=n_spells,
                pips={c: round(pips[c], 1) for c in "WUBRG" if pips[c]}, roles=roles,
                core={r: ctx.counts.get(r, 0) for r in GUIDELINES if ctx.guideline(r)})


def weaknesses(ctx, comp, targets=None):
    out = []
    for role in GUIDELINES:
        g = ctx.guideline(role)
        if not g:
            continue
        have = comp["lands"] if role == "Lands" else ctx.counts.get(role, 0)
        label = "lands" if role == "Lands" else f"{role.lower()} cards" if role not in ("Ramp", "Removal") else role.lower()
        why = "; ".join((targets or {}).get(role, {}).get("why", []))
        tgt = f"{g[0]}-{g[1]} for this deck" + (f" ({why})" if why else "")
        if role == "Finisher" and have == 0:
            out.append(dict(role=role, kind="low", have=0, range=g,
                            text="No obvious dedicated finisher detected - check how the deck closes games "
                                 f"(target {tgt})"))
        elif have < g[0]:
            out.append(dict(role=role, kind="low", have=have, range=g, text=f"Only {have} {label} - target {tgt}"))
        elif have > g[1] and role in ("Lands", "Board Wipe"):
            out.append(dict(role=role, kind="high", have=have, range=g,
                            text=f"{have} {label} - more than the {tgt}; a slot or two could do other jobs"))
    target = AVG_MV_TARGET[ctx.goal]
    if comp["avg_mv"] > target + 0.25:
        out.append(dict(role="Curve", kind="high", have=comp["avg_mv"], range=(0, target),
                        text=f"Average mana value {comp['avg_mv']:.2f} is on the high side (around {target} is "
                             f"typical for {GOALS[ctx.goal]}); cheaper cards would make the deck faster"))
    if comp["curve"].get(6, 0) >= 12:
        out.append(dict(role="Curve", kind="high", have=comp["curve"][6], range=(0, 10),
                        text=f"{comp['curve'][6]} cards cost 6 or more - that can make starts slow"))
    return out


def ownership_map(udb, deck_id):
    """{card name: dict(owned, free, used_in=[deck names other than this one], collection)}"""
    out = {}
    for r in udb.conn.execute("""SELECT a.card_name, SUM(a.quantity) AS owned, SUM(a.available) AS free,
                                        MIN(c.name) AS collection
                                 FROM collection_item_availability a JOIN collection_items ci ON ci.id = a.collection_item_id
                                 JOIN collections c ON c.id = ci.collection_id
                                 WHERE a.quantity > 0 GROUP BY a.card_name"""):
        out[r["card_name"]] = dict(owned=r["owned"], free=r["free"], used_in=[], collection=r["collection"])
    for r in udb.conn.execute("""SELECT DISTINCT ci.card_name, d.name FROM deck_cards dc
                                 JOIN collection_items ci ON ci.id = dc.collection_item_id JOIN decks d ON d.id = dc.deck_id
                                 WHERE dc.deck_id != ?""", (deck_id,)):
        if r[0] in out:
            out[r[0]]["used_in"].append(r[1])
    # free collection where the free copy lives
    for r in udb.conn.execute("""SELECT a.card_name, c.name FROM collection_item_availability a
                                 JOIN collection_items ci ON ci.id = a.collection_item_id
                                 JOIN collections c ON c.id = ci.collection_id WHERE a.available > 0
                                 ORDER BY c.is_default DESC"""):
        if r[0] in out and not out[r[0]].get("free_in"):
            out[r[0]]["free_in"] = r[1]
    return out


def ownership_of(ctx, name):
    own = ctx.ownership.get(name)
    price = ctx.cd.prices.get(name)
    if own and own["free"] > 0:
        return dict(status="available", label=f"Owned - {own.get('free_in') or own['collection']}",
                    free=own["free"], cost=0.0, price=price)
    if own:
        where = ", ".join(own["used_in"][:2]) or "another deck"
        return dict(status="in_use", label=f"Owned, but currently used in: {where}", free=0,
                    cost=price, price=price, used_in=own["used_in"])
    return dict(status="not_owned", label="Not owned", free=0, cost=price, price=price)


# ---------------------------------------------------------------- deck understanding
#
# COMMANDER -> STRATEGY -> ENGINES / SYNERGIES -> CARD FUNCTIONS -> DECK NEEDS ->
# WEAK / REDUNDANT PIECES -> POSSIBLE UPGRADES -> SAFE SWAPS.
# A card is only suggested as a cut when there is POSITIVE evidence that it may be
# replaceable, and that evidence clearly outweighs everything that makes it important.
# Not understanding a card is protection, not evidence. Every item keeps its reason.

MIN_EVIDENCE = 0.9     # a cut needs at least this much evidence...
MIN_MARGIN = 0.6       # ...and this much more evidence than protection
STRATEGY_SURE = 4.5    # detected primary theme score needed to call the strategy clear
GO_WIDE_PLANS = {"Tokens", "Combat", "Creatures", "Tribal", "Aristocrats"}


def strategy_certainty(detected, user_tags):
    """(uncertain?, why). Your own strategy tags are always taken as certain."""
    if user_tags:
        return False, "you chose the strategy"
    scores = sorted(detected.get("scores", {}).values(), reverse=True)
    if not detected.get("primary") or not scores:
        return True, "no clear strategy detected"
    s1 = scores[0]
    s3 = scores[2] if len(scores) > 2 else 0
    if s1 < STRATEGY_SURE:
        return True, "the detected strategy is weak"
    if s3 and s1 < 1.3 * s3:
        return True, "several strategies score about the same"
    return False, "clear"


def deck_targets(ctx, comp, prof, graph):
    """Generic guideline ranges adjusted to THIS deck (curve, commander cost, what the
    commander does itself, the plan) - each change with its reason."""
    goal, avg = ctx.goal, comp["avg_mv"]
    cmd_mv = max([float((ctx.cd.info(c["card_name"]) or {}).get("cmc") or 0) for c in ctx.commanders] or [0])
    mech = {m["key"] for m in prof["mechanics"]}
    plan = prof.get("primary")
    # main plans only: a theme detected just from the deck's own wipes shouldn't raise the wipe target
    plans = {plan, *(prof.get("supporting") or [])[:2]} | {t for t, w in ctx.themes.items() if w >= 1.0}
    repeat_draw = sum(1 for n, (i, r) in graph.cards.items() if n not in graph.commanders and set(
        synergy.functions(i, r).get("Card Draw", [])) & synergy.ENGINE_SUBTYPES)
    out = {}
    for role, by_goal in GUIDELINES.items():
        if role == "Counterspell" and "U" not in ctx.identity:
            continue
        lo, hi = by_goal[goal]
        why = []
        if role in ("Ramp", "Lands"):
            if avg and avg <= 2.8:
                lo, hi = lo - 2, hi - 2
                why.append(f"low average mana value ({avg:.2f})")
            elif avg and avg <= 3.1:
                lo, hi = lo - 1, hi - 1
                why.append(f"fairly low average mana value ({avg:.2f})")
            elif avg >= 3.8:
                lo, hi = lo + 1, hi + 2
                why.append(f"high average mana value ({avg:.2f})")
            if role == "Ramp" and cmd_mv >= 6:
                lo, hi = lo + 1, hi + 1
                why.append(f"the commander costs {int(cmd_mv)}")
            elif role == "Ramp" and cmd_mv and cmd_mv <= 3:
                why.append(f"the commander costs only {int(cmd_mv)}")
        if role == "Card Draw":
            if "draw" in mech:
                lo, hi = lo - 2, hi - 2
                why.append("the commander draws cards itself")
            if repeat_draw >= 3:
                lo, hi = lo - 1, hi - 1
                why.append(f"{repeat_draw} repeatable draw engines already")
        if role == "Removal":
            if "removal" in mech:
                lo, hi = lo - 1, hi - 1
                why.append("the commander provides removal")
            if "Control" in plans:
                lo, hi = lo + 1, hi + 1
                why.append("control plan")
        if role == "Board Wipe":
            if plans & GO_WIDE_PLANS and "Control" not in plans:
                lo, hi = max(0, lo - 1), max(1, hi - 1)
                why.append("wipes also hit your own wide board")
            elif "Control" in plans:
                lo, hi = lo + 1, hi + 1
                why.append("control plan")
        if role == "Protection":
            if "Voltron" in plans:
                lo, hi = lo + 2, hi + 2
                why.append("voltron: the commander must survive")
            elif plans & GO_WIDE_PLANS:
                lo, hi = lo + 1, hi + 1
                why.append("a wide board needs protection from wipes")
        if role == "Finisher" and mech & {"damage", "wide"}:
            lo, hi = max(0, lo - 1), max(1, hi - 1)
            why.append("the commander can close games itself")
        if role == "Recursion" and plans & {"Graveyard", "Reanimator"}:
            lo, hi = lo + 1, hi + 2
            why.append("graveyard plan")
        lo = max(0, lo)
        out[role] = dict(generic=list(by_goal[goal]), target=[lo, max(lo, hi)], why=why)
    return out


def analysis_confidence(prof, review_rows, uncertain, uncertain_why):
    """How far to trust the recommendations: commander understanding, strategy certainty
    and how many cards the analyser actually understands."""
    total = len(review_rows)
    understood = sum(1 for v in review_rows if v["understood"])
    cov = understood / total if total else 1.0
    levels = {"HIGH": 2, "MEDIUM": 1, "LOW": 0}
    reasons, parts = [], {}
    cu = prof.get("understanding", 0)
    parts["commander"] = "HIGH" if cu >= 0.75 else "MEDIUM" if cu >= 0.5 else "LOW"
    if parts["commander"] != "HIGH":
        reasons.append(f"only {prof.get('understood')}/{prof.get('clauses')} of the commander's rules text was "
                       "interpreted")
    parts["strategy"] = "MEDIUM" if uncertain else "HIGH"
    if uncertain:
        reasons.append(f"strategy uncertain ({uncertain_why})")
    parts["coverage"] = "HIGH" if cov >= 0.88 else "MEDIUM" if cov >= 0.75 else "LOW"
    if parts["coverage"] != "HIGH":
        poor = [v["name"] for v in review_rows if not v["understood"]]
        reasons.append(f"{total - understood} of {total} cards poorly understood (e.g. {', '.join(poor[:4])})")
    overall = min(parts.values(), key=lambda p: levels[p])
    return dict(overall=overall, commander=parts["commander"], strategy=parts["strategy"],
                coverage=parts["coverage"], understood=understood, total=total, reasons=reasons)


def _func_counts(ctx, graph_cards):
    counts = Counter()
    for name, (info, roles) in graph_cards.items():
        if name in {c["card_name"] for c in ctx.commanders}:
            continue
        for f in synergy.flat_functions(info, roles):
            counts[f] += 1
    return counts


def review_card(ctx, st, name, info, roles):
    """Cut evidence vs protection for one deck card (with the deck's engines, the
    commander plan and your own feedback)."""
    graph, func_counts, counts, flags = st.graph, st.func_counts, st.counts, st.flags.get(name, set())
    s = score(ctx, name, counts, in_deck=True)
    ev, prot = [], []
    strong = graph.count(name, strengths=("strong",))
    moderate = graph.count(name, strengths=("moderate",))
    n_links = strong + moderate
    cmd_links = graph.with_commander(name)
    cmd_strength = graph.commander_strength(name)
    links = graph.interactions(name)
    engines = graph.engines_of(name)
    funcs = synergy.functions(info, roles)
    core = [r for r in roles if r in GUIDELINES and roles[r].confidence >= 0.5]
    cmc = float(info.get("cmc") or 0)
    understood = any(v.confidence >= 0.6 for r, v in roles.items() if r != "Land")
    # a big body whose ONLY recognised trait is size (e.g. Colossal Dreadmaw) - never a card the
    # analyser simply failed to understand
    vanilla_body = ("Creature" in (info.get("type_line") or "") and cmc >= 5
                    and set(roles) == {"Finisher"} and all(v.confidence <= 0.55 for v in roles.values()))
    base = dict(name=name, understood=understood or vanilla_body, links=n_links, strong=strong, moderate=moderate,
                commander_synergy=cmd_strength or "none", engines=engines, flags=sorted(flags),
                roles=sorted(r for r in roles if r != "Land"),
                functions=sorted(f for subs in funcs.values() for f in subs), score=s)
    if "never_cut" in flags:
        return dict(base, status="pinned", evidence=[], protection=[("You protected this card from cuts", 99.0)],
                    E=0.0, P=99.0, cut_confidence=0.0, eligible=False, verdict="KEEP - you protected it")
    plan = ctx.profile.get("primary_name") if ctx.profile else None
    # ---- evidence that it may be replaceable
    if info.get("legal_commander") not in (None, "legal"):
        ev.append(("Not legal in Commander", 3.0))
    if not set(info.get("color_identity") or "") <= set(ctx.identity):
        ev.append((f"Outside your commander's colours ({info.get('color_identity')})", 3.0))
    if "not_important" in flags:
        ev.append(("You marked it as not important", 0.3))
    has_job = any(r != "Finisher" or roles[r].confidence > 0.55 for r in core)
    if understood and not has_job and s["comp"]["commander"] < 0.1 and s["comp"]["strategy"] < 0.1 \
            and strong == 0 and n_links <= 1 and not cmd_links and not engines:
        ev.append((f"No clear deck job (ramp, draw, removal...) and nothing to do with {plan or 'the plan'}; "
                   "no interactions with other cards", 0.4 if st.uncertain else 1.0))
    elif vanilla_body and s["comp"]["commander"] < 0.1 and n_links <= 1 and not cmd_links and not engines:
        ev.append((f"A {int(cmc)}-mana creature whose only job is attacking, with no link to your commander or "
                   "other cards", 0.4 if st.uncertain else 1.0))
    one_sided = "asymmetric wipe" in funcs.get("Board Wipe", [])
    for role in core:
        g = ctx.guideline(role)
        if not g or counts[role] <= g[1] or role == "Finisher":  # many ways to win is never a problem
            continue
        sub = next((x for x in funcs.get(role, [role.lower()]) if func_counts[(role, x)] - 1 >= 3), None)
        over = counts[role] - g[1]
        if sub:
            ev.append((f"{func_counts[(role, sub)] - 1} other cards already do {sub} (the deck has {counts[role]} "
                       f"{role.lower()} cards; target for this deck {g[0]}-{g[1]})", 0.8 if over >= 2 else 0.6))
            break
        if over >= 2 and not (role == "Board Wipe" and one_sided) and len(core) == 1:
            ev.append((f"The deck has {counts[role]} {role.lower()} cards - {over} more than the {g[0]}-{g[1]} "
                       "this deck needs", 0.5))
            break
    reduces_itself = re.search(r"costs? \{?\d+\}? less|\bconvoke\b|\bdelve\b|\bimprovise\b|\baffinity\b|"
                               r"\bundaunted\b|\bemerge\b|without paying its mana cost",
                               cardroles.normalise_text(info.get("oracle_text"), name))
    for role in core:
        ideal = IDEAL_MV.get(role)
        if ideal is None or cmc < ideal + 2 or reduces_itself:
            continue
        subs = set(funcs.get(role, [role.lower()]))
        sub = sorted(subs)[0]
        cheaper = [n for n, (i2, r2) in graph.cards.items() if n != name and role in r2
                   and subs <= {x for rr, x in synergy.flat_functions(i2, r2) if rr == role}
                   and float(i2.get("cmc") or 0) <= cmc - 2]
        if len(cheaper) >= 2:
            ev.append((f"Costs {int(cmc)} mana; {len(cheaper)} other cards do {sub} for {int(cmc) - 2} or less "
                       f"(e.g. {', '.join(sorted(cheaper)[:2])})", 0.6))
            break
    text = cardroles.normalise_text(info.get("oracle_text"), name)
    creatures = sum(1 for n, (i2, _) in graph.cards.items() if "Creature" in (i2.get("type_line") or ""))
    if "Board Wipe" in roles and re.search(r"destroy all|destroy each|exile all|all creatures get -|damage to each creature",
                                           text) and "your opponents control" not in text \
            and "you don't control" not in text and "nontoken" not in text:
        if ctx.profile and ctx.profile.get("primary") in GO_WIDE_PLANS:
            ev.append((f"Wipes your own board too, but the plan is {plan} ({creatures} creatures)", 0.7))
        elif creatures >= 25:
            ev.append((f"Wipes your own board too, and the deck runs {creatures} creatures", 0.6))
    if st.graph.engines() and understood and not core and not engines and strong == 0 and n_links <= 2 \
            and not cmd_links and s["comp"]["strategy"] < 0.25:
        ev.append(("Its theme doesn't connect to any of the deck's engines or your commander", 0.4))
    rank = ctx.cd.popularity.get(name)
    if rank and rank > 20000 and n_links <= 1 and not cmd_links:
        ev.append((f"Rarely played in Commander (EDHREC rank {rank:,}, via Scryfall) and no interactions here", 0.2))

    # ---- protection: what makes it important in THIS deck
    if "core" in flags:
        prot.append(("You marked this as a core card for this deck", 2.5))
    if "not_important" in flags:
        prot.append(("(you marked it as not important)", -0.6))
    if info.get("game_changer"):
        prot.append(("Official Game Changer - among the most powerful cards in Commander", 2.0))
    if rank and rank <= 250:
        prot.append((f"One of the most played Commander cards (EDHREC rank {rank}, via Scryfall)", 0.5))
    if cmd_links:
        prot.append((f"{cmd_strength.title()} synergy with your commander: {cmd_links[0][1]}",
                     1.2 if cmd_strength == "strong" else 0.5))
    if engines:
        cmd_engines = [e for e in engines if st.graph.engines()[e]["with_commander"]]
        payoff = [e for e in engines if name in st.graph.engines()[e]["payoffs"]]
        prot.append((f"Part of the {', '.join(engines[:2]).lower()}" + (" (a payoff)" if payoff else ""),
                     min(1.5, 0.6 * len(engines) + (0.6 if payoff and cmd_engines else 0))))
    if n_links:
        ex = ", ".join(o for o, _ in links[:3])
        prot.append((f"Interacts with {n_links} other card{'s' if n_links != 1 else ''} ({strong} strong; e.g. {ex})",
                     min(1.0, 0.08 * strong + 0.03 * moderate)))
    if s["comp"]["strategy"] >= 0.5:
        prot.append((f"Supports the {'/'.join(t for t in s['themes'] if t in ctx.themes)[:40]} strategy", 0.8))
    elif s["comp"]["strategy"] >= 0.25:
        prot.append(("Supports a secondary strategy", 0.4))
    engine_jobs = sorted({f for subs in funcs.values() for f in subs} & synergy.ENGINE_SUBTYPES)
    if engine_jobs and synergy.is_engine(info, roles):  # a value engine - not just a mana rock
        prot.append((f"Repeatable engine ({', '.join(engine_jobs[:2])})", 0.6))
    uniq = []
    for (role, sub) in synergy.flat_functions(info, roles):
        if role == "Land" or (roles.get(role) and roles[role].confidence < 0.5):
            continue
        surplus = role in GUIDELINES and ctx.guideline(role) and counts[role] > ctx.guideline(role)[1]
        if func_counts[(role, sub)] <= 1 and not surplus and (
                role in GUIDELINES or role in ("Graveyard Hate", "Sacrifice Outlet", "Token Doubler", "Trigger Doubler")
                or sub in synergy.ENGINE_SUBTYPES):
            uniq.append(sub)
    if uniq:
        prot.append((f"Only card in the deck doing: {', '.join(uniq[:3])}", min(1.5, 1.0 + 0.25 * (len(uniq) - 1))))
    for role in core:
        g = ctx.guideline(role)
        if g and counts[role] <= g[0]:
            prot.append((f"One of only {counts[role]} {role.lower()} cards (target for this deck {g[0]}-{g[1]})", 0.8))
            break
    if len(core) >= 2:
        prot.append((f"Does several jobs: {', '.join(core)}", min(0.6, 0.3 * (len(core) - 1))))
    if one_sided and ctx.profile and ctx.profile.get("primary") in GO_WIDE_PLANS:
        prot.append(("One-sided wipe: clears the way without hitting your own wide board", 0.8))
    if "Finisher" in roles and roles["Finisher"].confidence >= 0.6:
        prot.append((f"Can win games ({', '.join(funcs.get('Finisher', ['finisher'])[:2])})", 0.7))
    if not base["understood"]:
        prot.append(("The analyser can't tell what this card does in this deck - it may be there for a reason "
                     "it can't see, so it won't suggest cutting it", 1.5))
    rewards = graph.res.get(name, (set(), set(), set()))[1]
    payoff_for = [r for r in rewards if sum(1 for n, x in graph.res.items() if r in x[0] and n != name) >= 3]
    if payoff_for:
        prot.append((f"Payoff for {payoff_for[0]} the deck produces", 0.6))

    E = sum(w for _, w in ev)
    P = max(0.0, sum(w for _, w in prot))
    eligible = E >= MIN_EVIDENCE and E - P >= MIN_MARGIN
    cut_conf = max(0.0, min(1.0, (E - P) / 1.5)) if eligible else 0.0
    if eligible:
        status, verdict = "possible cut", "POSSIBLE CUT"
    elif not ev:
        status = "no reason to cut found"
    else:
        status = "protected - more reasons to keep it than to cut it"
    if not eligible:
        if "core" in flags:
            verdict = "KEEP - you marked it core"
        elif engines and P >= 3:
            verdict = "KEEP - core engine piece"
        elif P >= 2:
            verdict = "KEEP - important to this deck"
        elif not base["understood"]:
            verdict = "KEEP - not understood (protected)"
        elif ev:
            verdict = "KEEP - reasons to cut don't outweigh its value"
        else:
            verdict = "KEEP - no reason to cut"
    return dict(base, status=status, verdict=verdict, evidence=ev, protection=prot, E=round(E, 2), P=round(P, 2),
                cut_confidence=round(cut_conf, 3), eligible=eligible)


class _State:
    pass


def prepare(udb, store, deck_id, settings, progress=None):
    """Everything the analysis needs to understand the deck (no replacement search)."""
    say = progress or (lambda p, t: None)
    cd = card_data(udb, progress)
    say(0.5, "Reading the deck...")
    rows = store.deck_cards(deck_id)
    return prepare_rows(cd, [r for r in rows if r["role"] in ("commander", "partner", "main")], settings,
                        udb.card_flags(deck_id), ownership_map(udb, deck_id), progress)


def prepare_rows(cd, deck_rows, settings, flags=None, ownership=None, progress=None, review=True, tribe=None):
    """prepare() for any list of deck rows (dict(card_name, role, quantity, type_line,
    color_identity)) - also a deck that exists only in memory, e.g. one being built.
    review=False skips the per-card cut review (faster); tribe overrides the detected tribe."""
    say = progress or (lambda p, t: None)
    st = _State()
    st.cd = cd
    st.deck_rows = deck_rows
    st.flags = flags or {}
    commander_cards = [c for c in (cd.info(r["card_name"]) for r in deck_rows if r["role"] in ("commander", "partner"))
                       if c]
    deck_infos = [cd.info(r["card_name"]) for r in deck_rows if r["role"] == "main"]
    core_infos = [cd.info(n) for n, f in st.flags.items() if "core" in f and cd.info(n)]
    st.detected = detected = strategy.detect(commander_cards, [c for c in deck_infos if c], core_infos)
    if tribe:
        detected["tribe"] = tribe
    st.ownership = ownership if ownership is not None else {}
    st.ctx = ctx = Context(cd, deck_rows, settings, st.ownership, detected)
    st.uncertain, st.uncertain_why = strategy_certainty(detected, ctx.user_tags)
    say(0.56, "Understanding the commander and mapping how the cards work together...")
    st.graph_cards = graph_cards = {}
    for r in deck_rows:
        info = cd.info(r["card_name"])
        if info and "Land" not in (info.get("type_line") or "").split(" // ")[0]:
            graph_cards[r["card_name"]] = (info, cd.roles.get(info["name"], {}))
    st.graph = graph = synergy.InteractionGraph(graph_cards, [c["card_name"] for c in ctx.commanders], ctx.tribe)
    st.profile = prof = commander.profile(commander_cards, graph, detected.get("tribe"))
    ctx.profile = prof
    st.comp = comp = composition(ctx)
    st.targets = deck_targets(ctx, comp, prof, graph)
    ctx.targets = {r: tuple(v["target"]) for r, v in st.targets.items()}
    comp["core"] = {r: ctx.counts.get(r, 0) for r in GUIDELINES if ctx.guideline(r)}
    st.func_counts = _func_counts(ctx, graph_cards)
    st.counts = Counter(ctx.counts)
    say(0.64, "Checking every card before suggesting any cut...")
    st.review = []
    for r in ctx.cards if review else []:
        if r["role"] != "main" or r["card_name"] not in graph_cards:
            continue  # commanders and lands are never cuts
        info, roles = graph_cards[r["card_name"]]
        st.review.append(review_card(ctx, st, r["card_name"], info, roles))
    st.review.sort(key=lambda v: (-v["cut_confidence"], v["P"] - v["E"]))
    st.confidence = analysis_confidence(prof, st.review, st.uncertain, st.uncertain_why)
    return st


def weakest_cards(st, n=8):
    """'What would you cut?' - the most plausible cut candidates, each with reasons to cut AND
    to keep. Nothing is removed; pinned and core cards are left out."""
    rows = [v for v in st.review if v["status"] != "pinned" and "core" not in v["flags"]]
    with_reasons = sorted([v for v in rows if v["E"] > 0], key=lambda v: (-(v["E"] - v["P"]), v["P"]))
    ctx = st.ctx

    def needed_job(v):  # ramp / draw / removal... while that job is not over target: never "weakest" padding
        return any(r in GUIDELINES and st.counts[r] <= (ctx.guideline(r) or (0, 99))[1] for r in v["roles"])
    rest = sorted([v for v in rows if v["E"] <= 0 and v["understood"] and not needed_job(v)
                   and not any(t.startswith("Only card in the deck") for t, _ in v["protection"])],
                  key=lambda v: v["P"])
    out = []
    for v in (with_reasons + rest)[:n]:
        expend = max(0.05, min(0.95, 0.5 + (v["E"] - v["P"]) / 4))
        out.append(dict(name=v["name"], expendability=round(expend, 2), cut_confidence=v["cut_confidence"],
                        reasons_to_cut=[t for t, _ in sorted(v["evidence"], key=lambda e: -e[1])],
                        reasons_to_keep=[t for t, w in sorted(v["protection"], key=lambda e: -e[1]) if w > 0],
                        links=v["links"], strong=v["strong"], roles=v["roles"], functions=v["functions"],
                        engines=v["engines"], P=v["P"], E=v["E"], verdict=v["verdict"]))
    return out


def slot_weakest(cf, st, n=10):
    """'If you need to free slots, inspect these first' - from the cut engine (cutfinder):
    the cards whose removal costs the deck least, with what each contributes and what
    you'd lose. Never 'bad cards'; protected / essential / shortage-making cards are left out."""
    rev = {v["name"]: v for v in st.review}
    out = []
    for cost, name, fl in cf.weakest_slots(n):
        v = rev.get(name, {})
        out.append(dict(name=name, flexibility=fl["level"], expendability=round(max(0.05, min(0.95, 0.6 - cost)), 2),
                        cut_confidence=v.get("cut_confidence", 0), reasons_to_cut=fl["why"],
                        reasons_to_keep=fl["keep"], losses=fl["losses"], links=v.get("links", 0),
                        strong=v.get("strong", 0), roles=v.get("roles", []), functions=v.get("functions", []),
                        engines=v.get("engines", []), P=v.get("P", 0), E=v.get("E", 0), verdict=v.get("verdict")))
    return out


def find_a_cut(udb, store, deck_id, settings, in_name, st=None, limit=5):
    """'I want to add THIS card - what should come out?' Ranked OUT options (deck before vs
    after each swap) with gains, losses and the net effect - or 'No safe cut found' with why."""
    import cutfinder
    cf, st = cutfinder.finder_for(udb, store, deck_id, settings, st)
    return cf.find(in_name, limit=limit)


def find_cuts_for(udb, store, deck_id, settings, in_name, st=None, limit=3):
    """Kept for callers of the old name: the same as find_a_cut."""
    return find_a_cut(udb, store, deck_id, settings, in_name, st, limit)


def analyse(udb, store, deck_id, settings, progress=None, providers=(), quick=False):
    """Run a full analysis. settings: mode ('collection'|'all'), goal, tags (list|None),
    budget_usd (None = unlimited). quick=True skips the replacement search (tests).
    Returns a JSON-able dict."""
    t0 = time.time()
    say = progress or (lambda p, t: None)
    say(0.02, "Loading card data...")
    st = prepare(udb, store, deck_id, settings, progress)
    ctx, cd, graph, comp, counts = st.ctx, st.cd, st.graph, st.comp, st.counts
    deck_rows, ownership, review = st.deck_rows, st.ownership, st.review
    weak = weaknesses(ctx, comp, st.targets)
    mode = settings.get("mode", "collection")
    budget = 0.0 if mode == "collection" else settings.get("budget_usd")
    uncertain = st.uncertain
    cuts = [v for v in review if v["eligible"]]
    if os.environ.get("CARDSCAN_DEBUG_ANALYSER"):
        print(describe_review(review))

    deck_names = {r["card_name"] for r in deck_rows}
    pool, swaps, considered, rec_info, slot_alternatives, used_elsewhere = [], [], [], {}, [], []
    spent, counts_after = 0.0, Counter(counts)
    weakest = weakest_cards(st)
    if not quick:
        # 1. cards worth ADDING: need-driven, tiered, grouped - no fixed count (recommend.py)
        say(0.72, "Working out what the deck needs and which cards solve it...")
        import community
        import cutfinder
        import recommend
        feedback = udb.suggestion_feedback(deck_id)
        comm = community.load(recommend.commander_key(st))
        recs = recommend.recommend(st, settings, feedback, comm, debug=bool(os.environ.get("CARDSCAN_DEBUG_ANALYSER")))
        pool = recs["items"]
        # 2. the upgrade plan: for each idea, "what is the least painful card to remove for it?" -
        #    judged on the deck before vs after, applied one swap at a time (cutfinder.py)
        say(0.82, "Finding the least painful cut for each idea and building a compatible plan...")
        cf = cutfinder.CutFinder(st, settings, feedback, comm, rec=recs["recommender"])
        # every idea that passed the need test (recommend.py already searched ALL free cards you own in
        # My Collection mode); strategy-dependent (NICHE) ideas stay on Consider
        tiers = {it["name"]: it["tier"] for it in recs["items"]}
        names = [it["name"] for it in recs["items"] if it["tier"] != "NICHE"]
        cands = [(n, tiers.get(n), ownership_of(ctx, n)) for n in names
                 if not (feedback.get(n) == "not_for_deck")]
        plan, slot_alternatives = cf.plan(cands, budget=budget)
        graph_now = st.graph
        for w in plan:
            d, e = w["d"], w["e"]
            fi, fo = cf.facts(d["into"]), cf.facts(d["out"])
            v = next((x for x in review if x["name"] == d["out"]), {})
            own = w["ownership"]
            if own.get("status") != "available":
                spent += own.get("cost") or 0.0
            for r in fo["roles"]:
                counts_after[r] -= 1
            for r in fi["roles"]:
                counts_after[r] += 1
            links_in = d["links_in"]
            swaps.append(dict(
                out=d["out"], into=d["into"], ownership=own, tier=w["tier"], tier_label=cutfinder.TIER_LABEL[w["tier"]],
                confidence=cutfinder.TIER_LABEL[w["tier"]], kind=d["kind"], consider_tier=w["consider_tier"],
                net=e["net"], net_value=round(w["adj"], 3), gains=e["gains"], losses=e["losses"],
                why_out=e["why_out"], why_in=e["why_in"], out_kept=e["keep"],
                cut_confidence=cutfinder._conf(w["adj"]), replacement_confidence=cutfinder._conf(w["adj"]),
                swap_confidence=cutfinder._conf(w["adj"]), parts=d["parts"],
                other_outs=w["other_outs"], other_ins=w["other_ins"],
                in_links=[dict(card=n, why=t, strength=x) for n, t, x in links_in[:20]], in_links_total=len(links_in),
                out_links=[dict(card=n, why=t, strength=x) for n, t, x in graph_now.detailed(d["out"])[:20]],
                in_roles=sorted(fi["roles"]), out_roles=sorted(fo["roles"]), in_cmc=fi["cmc"], out_cmc=fo["cmc"],
                in_functions=sorted({x for _, x in fi["funcs"]})[:4], out_functions=v.get("functions", []),
                gain=round(w["adj"], 3)))
            if v:
                v["status"] = "suggested cut"
        used = {s["into"] for s in swaps}
        considered = [it for it in recs["items"] if it["name"] not in used]
        rec_info = dict(groups=[dict(g, names=[n for n in g["names"] if n not in used]) for g in recs["groups"]],
                        needs=recs["needs"], mode=recs["mode"],
                        shown=sum(1 for it in considered if it["tier"] in ("EXCELLENT", "STRONG", "WORTH")),
                        rejected=recs["rejected"],
                        no_collection_upgrades=mode == "collection" and not any(
                            it["tier"] in ("EXCELLENT", "STRONG", "WORTH") for it in considered))
        rec_info["groups"] = [g for g in rec_info["groups"] if g["names"]]
        # 3. owned, but in another deck: shown separately, never part of the free plan
        if mode == "collection":
            busy = [n for n, o in ownership.items() if o["free"] == 0 and n not in deck_names]
            scored_busy = []
            for n in busy:
                info = cd.info(n)
                why_not = cf.rec.availability(info["name"], info) if info else "unknown card"
                if why_not and "used in another deck" not in why_not:
                    continue  # off-colour, not legal, a land...
                f = cf.facts(info["name"])
                scored_busy.append((f["quality"] + 0.3 * f["cmd_fit"], info["name"]))
            for _, n in sorted(scored_busy, reverse=True)[:20]:
                opts, _ = cf.options(n, limit=1)
                if opts and opts[0]["tier"] in ("HIGH", "GOOD"):
                    used_elsewhere.append(dict(name=n, used_in=ownership[n]["used_in"], out=opts[0]["out"],
                                               tier_label=cutfinder.TIER_LABEL[opts[0]["tier"]],
                                               net=cf.explain(opts[0])["net"]))
                if len(used_elsewhere) >= 5:
                    break
        weakest = slot_weakest(cf, st)

    improvements = []
    for role in GUIDELINES:
        d = counts_after[role] - ctx.counts[role]
        if d and ctx.guideline(role):
            improvements.append(f"{d:+d} {role.lower()}")
    mv_change = sum(s["in_cmc"] - s["out_cmc"] for s in swaps) / max(1, comp["spells"])
    if swaps and abs(mv_change) >= 0.02:
        improvements.append(f"average mana value {comp['avg_mv']:.2f} -> {comp['avg_mv'] + mv_change:.2f}")
    alternatives = [dict(name=u["name"], used_in=u["used_in"], out=u["out"]) for u in used_elsewhere]

    combos = []
    for p in providers:  # optional extras (off unless asked for); never affect the swaps above
        try:
            combos += p.combos([c["card_name"] for c in ctx.commanders],
                               [r["card_name"] for r in deck_rows if r["role"] == "main"])
        except Exception as e:  # noqa: BLE001
            combos.append(dict(error=f"{getattr(p, 'name', 'provider')}: {e}"))
    annotate_combos(udb, deck_id, [c for c in combos if "error" not in c])

    detected = st.detected
    say(1.0, "Done")
    prof = st.profile
    return dict(
        version=ANALYSER_VERSION, deck_id=deck_id, created=time.strftime("%Y-%m-%d %H:%M"),
        seconds=round(time.time() - t0, 2), settings=settings,
        commanders=[c["card_name"] for c in ctx.commanders], identity=ctx.identity,
        detected=dict(primary=detected["primary"], secondary=detected["secondary"], tribe=detected.get("tribe"),
                      reasons={k: v[:3] for k, v in detected["reasons"].items()},
                      scores={k: round(v, 1) for k, v in sorted(detected["scores"].items(), key=lambda kv: -kv[1])[:6]}),
        active_themes=list(ctx.themes), user_tags=ctx.user_tags,
        strategy_uncertain=uncertain, strategy_note=st.uncertain_why,
        commander_profile=dict(mechanics=prof["mechanics"], primary=prof["primary_name"],
                               supporting=prof["supporting_names"], chain=prof["chain"], needs=prof["needs"],
                               understanding=prof["understanding"]),
        engines={e: dict(size=len(v["members"]), payoffs=v["payoffs"][:6], with_commander=v["with_commander"])
                 for e, v in sorted(graph.engines().items(), key=lambda kv: -len(kv[1]["members"]))},
        targets=st.targets, confidence=st.confidence,
        composition=comp, weaknesses=weak, swaps=swaps, considered=considered, recommendations=rec_info,
        weakest=weakest, slot_alternatives=slot_alternatives, used_elsewhere=used_elsewhere,
        cost=round(spent, 2), budget=budget,
        improvements=improvements, alternatives=alternatives, candidates_considered=len(pool), combos=combos,
        protected=sorted(n for n, f in st.flags.items() if "never_cut" in f and n in deck_names),
        flags={n: sorted(f) for n, f in st.flags.items() if n in deck_names},
        cut_review=[dict(name=v["name"], status=v["status"], verdict=v["verdict"], cut_confidence=v["cut_confidence"],
                         E=v["E"], P=v["P"], evidence=[[t, round(w, 2)] for t, w in v["evidence"]],
                         protection=[[t, round(w, 2)] for t, w in v["protection"]], links=v["links"],
                         strong=v["strong"], moderate=v["moderate"], commander_synergy=v["commander_synergy"],
                         engines=v["engines"], roles=v["roles"], functions=v.get("functions", []),
                         flags=v["flags"], understood=v["understood"]) for v in review])


def describe_review(review):
    """Debug text: why each card was / wasn't a cut candidate."""
    lines = []
    for v in review:
        lines.append(f"Card: {v['name']}  ->  {v.get('verdict', v['status'])} (evidence {v['E']}, protection {v['P']}, "
                     f"cut confidence {v['cut_confidence']:.0%})")
        for t, w in v["evidence"]:
            lines.append(f"   cut evidence +{w}: {t}")
        for t, w in v["protection"]:
            lines.append(f"   protection  +{w}: {t}")
    return "\n".join(lines)


# ---------------------------------------------------------------- applying / undoing

def apply_swaps(udb, store, deck_id, swaps, analysis_id=None):
    """Make the chosen swaps: snapshot first, then in one transaction take each OUT card
    out and put each IN card in (the owned printing when you have a free copy), then link
    free collection copies. Never touches other decks' copies. Returns the history id."""
    snap = udb.snapshot_deck(deck_id, "before applying analysis")
    new_ids = []
    with udb.transaction() as c:
        for s in swaps:
            row = c.execute("""SELECT id, quantity FROM deck_cards WHERE deck_id = ? AND card_name = ? AND role = 'main'
                               ORDER BY collection_item_id IS NOT NULL, id LIMIT 1""", (deck_id, s["out"])).fetchone()
            if row is None:
                raise ValueError(f"{s['out']} isn't in the deck any more")
            if row["quantity"] > 1:
                c.execute("UPDATE deck_cards SET quantity = quantity - 1 WHERE id = ?", (row["id"],))
            else:
                c.execute("DELETE FROM deck_cards WHERE id = ?", (row["id"],))
            lot = c.execute("""SELECT ci.scryfall_id, ci.finish FROM collection_item_availability a
                               JOIN collection_items ci ON ci.id = a.collection_item_id
                               WHERE a.card_name = ? AND a.available > 0 ORDER BY ci.id LIMIT 1""",
                            (s["into"],)).fetchone()
            if lot:
                sid, finish = lot["scryfall_id"], lot["finish"]
            else:
                p = store.default_printing(s["into"])
                if p is None:
                    raise ValueError(f"no printing of {s['into']} found")
                sid, finish = p["id"], (p.get("finishes") or "nonfoil").split(",")[0]
                if "nonfoil" in (p.get("finishes") or ""):
                    finish = "nonfoil"
            card = store.card(sid) or {}
            new_ids.append(c.execute(
                """INSERT INTO deck_cards (deck_id, card_name, oracle_id, scryfall_id, quantity, finish, role, physical,
                                           source) VALUES (?, ?, ?, ?, 1, ?, 'main', 0, 'analysis')""",
                (deck_id, card.get("name") or s["into"], card.get("oracle_id"), sid, finish)).lastrowid)
        applied = udb.log_applied(deck_id, analysis_id, snap, [dict(out=s["out"], into=s["into"]) for s in swaps], _conn=c)
    store.allocate(deck_id, new_ids)
    return applied


def undo_last(udb, store, deck_id):
    """Put the deck back as it was before the last applied analysis. Returns the swaps undone."""
    last = udb.last_applied(deck_id)
    if last is None:
        return None
    udb.undo_applied(last["id"])
    store.allocate(deck_id)
    return last["swaps"]


# ---------------------------------------------------------------- helpers for the screen

def cards_stamp():
    """Changes whenever cards.db is rebuilt (Update Prices) or the classifier changes."""
    import os
    from userdb import CARDS_DB_PATH
    try:
        st = os.stat(CARDS_DB_PATH)
        return f"{cardroles.CLASSIFIER_VERSION}:{int(st.st_mtime)}:{st.st_size}"
    except OSError:
        return str(cardroles.CLASSIFIER_VERSION)


def current_fingerprint(udb, deck_id, settings):
    return fingerprint(udb, deck_id, settings, cards_stamp())


def annotate_combos(udb, deck_id, combos):
    """Mark each missing combo piece as owned & free / owned but used elsewhere / not owned."""
    own = ownership_map(udb, deck_id)
    for c in combos:
        c["missing_ownership"] = {}
        for piece in c.get("missing", []):
            o = own.get(piece)
            if o and o["free"] > 0:
                c["missing_ownership"][piece] = dict(status="available", label=f"Owned - {o.get('free_in') or o['collection']}")
            elif o:
                c["missing_ownership"][piece] = dict(status="in_use",
                                                     label="Owned, used in " + (", ".join(o["used_in"][:2]) or "a deck"))
            else:
                c["missing_ownership"][piece] = dict(status="not_owned", label="Not owned")
    return combos
