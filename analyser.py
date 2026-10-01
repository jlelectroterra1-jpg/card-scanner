"""The Commander deck analyser - our own, local, explainable engine (no EDHREC, no AI).

analyse(udb, store, deck_id, settings) works through:
  1. the deck: card roles (cardroles), composition, mana curve, colour pips, lands
  2. the plan: commander strategy (strategy.detect) or the user's own deck goals
  3. weaknesses: role counts against configurable guideline ranges (GUIDELINES)
  4. scoring: every card gets separate components (commander synergy, strategy, role
     need, mana efficiency, interaction with the deck, general quality) - kept so each
     suggestion can explain itself; the weighted total depends on the deck goal
  5. possible cuts: lowest-scoring non-commander, non-land cards, with reasons
  6. replacements: staged filtering of legal cards (legal -> colour identity -> relevant
     role/theme -> owned & free / budget -> scoring), paired with cuts
  7. a package of swaps within the budget, each with plain-English reasons + confidence
Nothing changes a deck until apply_swaps() is called (which snapshots first; undo_last()
puts it back)."""
import hashlib
import json
import time
from collections import Counter

import cardroles
import deckrules
import strategy

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
PACKAGE_SIZE = {"casual": 4, "improve": 7, "high": 10}
MIN_GAIN = {"casual": 0.10, "improve": 0.08, "high": 0.06}
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
ANALYSER_VERSION = 4


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
        self.prices = {r[0]: r[1] for r in self.conn.execute(
            """SELECT name, MIN(CAST(usd AS REAL)) FROM scry.cards WHERE usd IS NOT NULL AND lang = 'en'
               GROUP BY name""")}
        # How widely a card has been (re)printed - especially in Commander products - is a
        # rough, local signal of how playable it is (Scryfall data, nothing from EDHREC).
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
    blob = json.dumps([ANALYSER_VERSION, deck, coll, alloc, settings, stamp], sort_keys=True, default=str)
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

    def guideline(self, role):
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


STAPLE_PRICE = 8.0  # USD: a card this sought-after is rarely the weak link


def is_staple(ctx, s):
    """Strong cards we never offer as cuts in Improve / High Power: Wizards' Game Changer list
    (Scryfall data) or a widely reprinted card that still costs a lot (local price data)."""
    name = s["info"].get("name") or s["name"]
    price = ctx.cd.prices.get(name) or 0
    return bool(s["info"].get("game_changer")) or (price >= STAPLE_PRICE and ctx.cd.reprints.get(name, 0) >= 0.5)


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
    q = 0.1 * min(3, len(useful)) + 0.15 * max([v.confidence for v in roles.values()] + [0])
    q += 0.45 * cd.reprints.get(info.get("name") or name, 0)
    if any("every time" in v.reason or "repeat" in v.reason for v in roles.values()):
        q += 0.15
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


def weaknesses(ctx, comp):
    out = []
    for role in GUIDELINES:
        g = ctx.guideline(role)
        if not g:
            continue
        have = comp["lands"] if role == "Lands" else ctx.counts.get(role, 0)
        label = "lands" if role == "Lands" else f"{role.lower()} cards" if role not in ("Ramp", "Removal") else role.lower()
        if have < g[0]:
            out.append(dict(role=role, kind="low", have=have, range=g,
                            text=f"Only {have} {label} - many {GOALS[ctx.goal].lower()} Commander decks run about "
                                 f"{g[0]}-{g[1]}"))
        elif have > g[1] and role in ("Lands", "Finisher", "Board Wipe", "Tutor"):
            out.append(dict(role=role, kind="high", have=have, range=g,
                            text=f"{have} {label} - more than the usual {g[0]}-{g[1]}; a slot or two could do other jobs"))
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


def candidate_pool(ctx, deck_names, mode):
    """Stage 1-4 filtering: legal, in colour identity, not in the deck, relevant to a
    role/theme, and owned & free (My Collection) - cheap checks before scoring."""
    cd, allowed = ctx.cd, set(ctx.identity)
    want_roles = {r for r in GUIDELINES if ctx.guideline(r) and not ctx.surplus(r)}
    out = []
    for name, info in cd.cards.items():
        if info["legal_commander"] != "legal" or name in deck_names:
            continue
        if not set(info["color_identity"] or "") <= allowed:
            continue
        front = (info["type_line"] or "").split(" // ")[0]
        if "Land" in front or deckrules.is_basic(name, info["type_line"]):
            continue
        own = ctx.ownership.get(name)
        if mode == "collection" and not (own and own["free"] > 0):
            continue
        if mode == "all" and not own and cd.reprints.get(name, 0) < MIN_REPRINT_SCORE:
            continue  # unowned and rarely printed: too obscure to suggest buying
        roles = cd.roles.get(name, {})
        themes = strategy.card_themes(info, roles)
        relevant = (want_roles & set(roles)) or (set(themes) & set(ctx.themes)) or (
            ctx.tribe and ctx.tribe in cardroles.creature_types(info["type_line"]))
        if relevant:
            out.append(name)
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


def explain(ctx, out_s, in_s, counts_before):
    """Plain-English reasons + confidence for one swap."""
    why_out, why_in = [], []
    themes_txt = ", ".join(list(ctx.themes)[:2]) or "current"
    if out_s["comp"]["commander"] < 0.2 and out_s["comp"]["strategy"] < 0.2:
        why_out.append(f"Lower synergy with your {themes_txt} plan")
    surplus = [r for r in out_s["core"] if ctx.surplus(r, counts_before)]
    for r in surplus[:1]:
        g = ctx.guideline(r)
        why_out.append(f"The deck already has {counts_before[r]} {r.lower()} cards (typical {g[0]}-{g[1]}), so this "
                       "slot may be doing less work than alternatives")
    if out_s["comp"]["efficiency"] < 0.6 and out_s["cmc"] >= 5:
        why_out.append(f"Costs {int(out_s['cmc'])} mana - on the expensive side for what it does here")
    if not out_s["roles"]:
        why_out.append("We couldn't detect a clear job for it in this deck (it may be there for a reason we can't see)")
    if not why_out:
        why_out.append("Scores lowest of your non-land cards for this deck's plan")

    shared = [r for r in in_s["core"] if r in out_s["core"]]
    for r in shared[:1]:
        line = f"Also does {r.lower()} ({in_s['roles'][r].reason})"
        if in_s["cmc"] < out_s["cmc"]:
            line += f" for {int(out_s['cmc'] - in_s['cmc'])} less mana"
        why_in.append(line)
    for r in in_s["core"]:
        if r in shared:
            continue
        if ctx.need(r, counts_before) > 0.15:
            g = ctx.guideline(r)
            why_in.append(f"Adds {r.lower()}: the deck has only {counts_before[r]} (many decks run {g[0]}-{g[1]})")
            break
    cmd_themes = [t for t in in_s["themes"] if ctx.commander_themes.get(t, 0) >= 0.5]
    if cmd_themes:
        names = " + ".join(c["card_name"].split(",")[0] for c in ctx.commanders) or "your commander"
        why_in.append(f"Works with {names}'s {cmd_themes[0].lower()} theme")
    elif any(t in ctx.themes for t in in_s["themes"]):
        t = next(t for t in in_s["themes"] if t in ctx.themes)
        why_in.append(f"Fits the {t.lower()} strategy {'you chose' if ctx.user_tags else 'the deck seems to have'}")
    if ctx.tribe and ctx.tribe in cardroles.creature_types(in_s["info"].get("type_line")):
        why_in.append(f"Is a {ctx.tribe} - {ctx.tribe_count} of your cards are too")
    elif in_s["comp"]["interaction"] >= 0.3:
        t = max((t for t in in_s["themes"] if t in ctx.themes), key=lambda t: ctx.theme_counts.get(t, 0), default=None)
        if t and ctx.theme_counts.get(t, 0) >= 2:
            why_in.append(f"Works with {ctx.theme_counts.get(t, 0)} cards already in the deck that care about "
                          f"{t.lower()}")
    if in_s["cmc"] + 1 < out_s["cmc"] and not shared:
        why_in.append(f"Costs {int(out_s['cmc'] - in_s['cmc'])} less mana, lowering the curve")
    if not why_in:
        why_in.append("Scores higher for this deck's roles and plan")

    gain = in_s["total"] - out_s["total"]
    narrow = in_s["main_role"] in NARROW or (in_s["core"] == [] and set(in_s["roles"]) <= NARROW)
    theme_driven = not shared and in_s["comp"]["need"] < 0.1 and (in_s["comp"]["strategy"] > in_s["comp"]["need"])
    if narrow:
        confidence = "Situational"
    elif theme_driven and not ctx.user_tags:
        confidence = "Depends on your intended strategy"
    elif gain >= 0.22 and (shared or in_s["comp"]["need"] > 0.2):
        confidence = "Strong suggestion"
    else:
        confidence = "Worth considering"
    return why_out, why_in, confidence


def analyse(udb, store, deck_id, settings, progress=None, providers=()):
    """Run a full analysis. settings: mode ('collection'|'all'), goal, tags (list|None),
    budget_usd (None = unlimited). Returns a JSON-able dict."""
    t0 = time.time()
    say = progress or (lambda p, t: None)
    say(0.02, "Loading card data...")
    cd = card_data(udb, progress)
    say(0.5, "Reading the deck...")
    rows = store.deck_cards(deck_id)
    deck_rows = [r for r in rows if r["role"] in ("commander", "partner", "main")]
    commander_cards = [cd.info(r["card_name"]) for r in deck_rows if r["role"] in ("commander", "partner")]
    commander_cards = [c for c in commander_cards if c]
    deck_infos = [cd.info(r["card_name"]) for r in deck_rows if r["role"] == "main"]
    detected = strategy.detect(commander_cards, [c for c in deck_infos if c])
    ownership = ownership_map(udb, deck_id)
    ctx = Context(cd, deck_rows, settings, ownership, detected)
    comp = composition(ctx)
    weak = weaknesses(ctx, comp)
    mode = settings.get("mode", "collection")
    budget = 0.0 if mode == "collection" else settings.get("budget_usd")

    say(0.6, "Looking for possible cuts...")
    counts = Counter(ctx.counts)
    cut_scores = []
    for r in ctx.cards:
        if r["role"] != "main":
            continue
        front = (r.get("type_line") or "").split(" // ")[0]
        if "Land" in front:  # lands are never suggested as cuts in this version
            continue
        s = score(ctx, r["card_name"], counts, in_deck=True)
        if ctx.goal != "casual" and is_staple(ctx, s):
            continue  # don't suggest cutting widely-played power cards when improving a deck
        cut_scores.append(s)
    cut_scores.sort(key=lambda s: s["total"])

    say(0.7, "Searching for replacements...")
    deck_names = {r["card_name"] for r in deck_rows}
    pool = candidate_pool(ctx, deck_names, mode)
    scored = [score(ctx, n, counts) for n in pool]
    scored.sort(key=lambda s: -s["total"])
    if ctx.goal == "casual":  # keep casual decks on-theme: no generic staple pushes
        scored = [s for s in scored if s["comp"]["commander"] + s["comp"]["strategy"] >= 0.3 or s["comp"]["need"] >= 0.3]
    scored = scored[:600]

    say(0.85, "Building the package...")
    size = PACKAGE_SIZE[ctx.goal]
    used, swaps, spent = set(), [], 0.0
    for out_s in cut_scores[:max(size * 3, 12)]:
        if len(swaps) >= size:
            break
        before = Counter(counts)
        best, best_val = None, None
        for cand in scored:
            if cand["name"] in used:
                continue
            fresh = score(ctx, cand["name"], counts)
            gain = fresh["total"] - out_s["total"]
            if gain < MIN_GAIN[ctx.goal]:
                continue
            shared_roles = set(fresh["core"]) & set(out_s["core"])
            shared = bool(shared_roles)
            fills = fresh["comp"]["need"] > 0.2
            if out_s["core"] and not shared and not fills and any(ctx.need(r, counts) > 0.2 for r in out_s["core"]):
                continue  # don't cut a needed job for something unrelated
            lost = [r for r in out_s["core"] if r not in shared_roles and ctx.guideline(r)
                    and counts[r] - 1 < ctx.guideline(r)[0] and out_s["roles"][r].confidence >= 0.6]
            if lost:
                continue  # would leave the deck short of a job it needs (e.g. its only counterspell)
            own = ownership_of(ctx, cand["name"])
            cost = own["cost"]
            if budget is not None:
                if cost is None or spent + cost > budget + 1e-9:
                    continue
            val = gain + (0.05 if shared else 0) + (0.03 if own["status"] == "available" else 0)
            if best_val is None or val > best_val:
                best, best_val = (fresh, own), val
        if best is None:
            continue
        in_s, own = best
        used.add(in_s["name"])
        spent += own["cost"] or 0.0
        for r in out_s["roles"]:
            counts[r] -= 1
        for r in in_s["roles"]:
            counts[r] += 1
        why_out, why_in, confidence = explain(ctx, out_s, in_s, before)
        swaps.append(dict(out=out_s["name"], into=in_s["name"], ownership=own, why_out=why_out, why_in=why_in,
                          confidence=confidence, gain=round(in_s["total"] - out_s["total"], 3),
                          out_components={k: round(v, 2) for k, v in out_s["comp"].items()},
                          in_components={k: round(v, 2) for k, v in in_s["comp"].items()},
                          in_roles=sorted(in_s["roles"]), out_roles=sorted(out_s["roles"]),
                          in_cmc=in_s["cmc"], out_cmc=out_s["cmc"]))

    improvements = []
    for role in GUIDELINES:
        d = counts[role] - ctx.counts[role]
        if d > 0 and ctx.guideline(role):
            improvements.append(f"+{d} {role.lower()}")
    mv_change = sum(s["in_cmc"] - s["out_cmc"] for s in swaps) / max(1, comp["spells"])
    if swaps and mv_change < -0.02:
        improvements.append(f"average mana value {comp['avg_mv']:.2f} -> {comp['avg_mv'] + mv_change:.2f}")
    if sum(s["in_components"]["commander"] - s["out_components"]["commander"] for s in swaps) > 0.5:
        improvements.append("better commander synergy")

    # owned cards that would help but are in other decks: shown as alternatives, never as free
    alternatives = []
    if mode == "collection":
        busy = [n for n, o in ownership.items() if o["free"] == 0 and n not in deck_names]
        alt_pool = candidate_pool(ctx, deck_names, "all")
        alt = [score(ctx, n, counts) for n in alt_pool if n in set(busy)]
        alt.sort(key=lambda s: -s["total"])
        for s in alt[:3]:
            alternatives.append(dict(name=s["name"], used_in=ownership[s["name"]]["used_in"],
                                     roles=sorted(s["core"] or s["roles"])[:3]))

    combos = []
    for p in providers:  # optional extras (off unless asked for); never affect the swaps above
        try:
            combos += p.combos([c["card_name"] for c in ctx.commanders],
                               [r["card_name"] for r in deck_rows if r["role"] == "main"])
        except Exception as e:  # noqa: BLE001
            combos.append(dict(error=f"{getattr(p, 'name', 'provider')}: {e}"))
    annotate_combos(udb, deck_id, [c for c in combos if "error" not in c])

    say(1.0, "Done")
    return dict(
        version=ANALYSER_VERSION, deck_id=deck_id, created=time.strftime("%Y-%m-%d %H:%M"),
        seconds=round(time.time() - t0, 2), settings=settings,
        commanders=[c["card_name"] for c in ctx.commanders], identity=ctx.identity,
        detected=dict(primary=detected["primary"], secondary=detected["secondary"], tribe=detected.get("tribe"),
                      reasons={k: v[:3] for k, v in detected["reasons"].items()}),
        active_themes=list(ctx.themes), user_tags=ctx.user_tags,
        composition=comp, weaknesses=weak, swaps=swaps, cost=round(spent, 2), budget=budget,
        improvements=improvements, alternatives=alternatives, candidates_considered=len(pool), combos=combos,
        cuts_considered=[dict(name=s["name"], score=round(s["total"], 3)) for s in cut_scores[:15]])


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
