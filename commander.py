"""Understand the COMMANDER before judging the 99 (works for any commander - nothing is
hard-coded to a particular card).

profile(commander_infos, deck_graph) reads the commander's Oracle text clause by clause and
returns a structured plan:

  mechanics  - what the text does / rewards (attack trigger, sacrifice, creates tokens,
               creates attackers, card advantage, damage / life loss, rewards going wide,
               death / enter triggers, counters, spells, graveyard, landfall, ...)
  plan       - primary game plan + engine chain (inputs -> what the commander does -> outputs)
  supporting - the other plans its text points to
  needs      - what the 99 have to supply for that plan (e.g. sacrifice fodder, token
               production, death payoffs) plus the basics every deck needs, each with how many
               deck cards currently supply it
  understanding - share of the commander's text clauses the analyser could interpret
"""
import re

import cardroles
import synergy

# (key, label, pattern, themes it points to, what it consumes, what it produces)
MECHANICS = [
    ("attack", "attack trigger", r"whenever you attack|whenever (?:this|it|this creature) attacks|whenever (?:a|one or "
     r"more) (?:[a-z-]+ )?creatures? you control attacks?", ["Combat"], ["attacking"], []),
    ("sacrifice", "sacrifices creatures (built-in sacrifice outlet)", r"sacrifice (?:another|a|an|one or more|x) "
     r"(?:other )?(?:creature|permanent|artifact|token)|you may sacrifice", ["Aristocrats"], ["sacrifice fodder"],
     ["creature deaths"]),
    ("tokens", "creates creature tokens", r"create [^.]*creature tokens?|create [^.]*\d+/\d+[^.]*token", ["Tokens"], [],
     ["tokens", "sacrifice fodder"]),
    ("attackers", "creates extra attackers", r"tapped and attacking|tokens? with haste|\bmobilize\b|additional combat",
     ["Combat", "Tokens"], [], ["attacking"]),
    ("draw", "card advantage", r"\bdraws? (?:a|two|three|x|\w+) cards?|exile the top [^.]*you may (?:play|cast)",
     [], [], ["card draw"]),
    ("damage", "direct damage / life loss", r"deals? (?:\d+|x|damage equal)[^.]*(?:target opponent|each opponent|any "
     r"target|target player)|(?:each|target) opponent loses", [], [], []),
    ("wide", "rewards going wide", r"number of (?:creature tokens|creatures|tokens|permanents) you control|for each "
     r"creature you control|creatures you control get \+", ["Tokens"], ["going wide"], []),
    ("deaths", "benefits from creatures dying", r"whenever [^.]*(?:creatures?|tokens?) (?:you control )?(?:dies|die)|"
     r"whenever you sacrifice", ["Aristocrats"], ["creature deaths"], []),
    ("enters", "benefits from creatures entering", r"whenever (?:a|another|one or more) (?:nontoken )?creatures? "
     r"(?:you control )?enters?", ["Tokens", "Creatures"], ["creatures entering"], []),
    ("token payoff", "rewards tokens", r"whenever (?:you create|one or more tokens)|tokens you control", ["Tokens"],
     ["tokens"], []),
    ("counters", "+1/+1 counters", r"\+1/\+1 counters?", ["Counters"], ["+1/+1 counters"], ["+1/+1 counters"]),
    ("proliferate", "proliferate", r"\bproliferate\b", ["Proliferate", "Counters"], [], ["+1/+1 counters"]),
    ("spells", "rewards casting instants/sorceries", r"whenever you cast (?:an instant|a noncreature|an instant or "
     r"sorcery)|\bmagecraft\b|copy (?:target|that) (?:instant|sorcery|spell)|instant or sorcery spells? you cast|"
     r"\bstorm\b", ["Spellslinger"],
     ["instants and sorceries"], []),
    ("graveyard", "uses the graveyard", r"from your graveyard|cards? in your graveyard|\bmill\b|\bmills\b",
     ["Graveyard"], ["cards in the graveyard"], []),
    ("reanimate", "reanimates", r"from (?:a|your) graveyard (?:on)?to the battlefield", ["Reanimator", "Graveyard"],
     ["cards in the graveyard"], []),
    ("landfall", "landfall / extra lands", r"\blandfall\b|whenever a land (?:you control )?enters|additional land",
     ["Landfall"], ["lands entering"], []),
    ("artifacts", "artifacts", r"\bartifacts?\b|\btreasure", ["Artifacts"], ["artifacts"], []),
    ("enchantments", "enchantments", r"\benchantments?\b|\bconstellation\b", ["Enchantments"], ["enchantments"], []),
    ("lifegain", "rewards life gain", r"whenever you gain life|for each 1 life you gained|if you would gain life",
     ["Lifegain"], ["life gain"], []),
    ("combat damage", "combat damage trigger", r"deals combat damage to (?:a player|an opponent)", ["Combat"],
     ["attacking"], []),
    ("pillowfort", "discourages attacks (pillowfort)", r"can't attack (?:you|thiss|planeswalkers)|attacks you", ["Control"],
     [], []),
    ("polymorph", "turns your own permanents into new ones", r"reveal cards from the top of your library until you reveal "
     r"a (?:creature|planeswalker)[^.]*|onto the battlefield and the rest on the bottom", ["Creatures"], [], []),
    ("recursion", "gets cards back from the graveyard", r"creature card in your graveyard|return it to the battlefield|"
     r"put it into your hand|experience counters?", ["Graveyard"], ["cards in the graveyard"], []),
    ("voltron", "suits up / commander damage", r"\bequipped\b|\benchanted\b|\bequipment\b|\bauras?\b|double strike",
     ["Voltron"], ["equipment"], []),
    ("blink", "blinks / enter effects", r"exile [^.]*return (?:it|them|that card)|enters the battlefield", ["Blink"],
     ["blink targets"], []),
    ("planeswalkers", "planeswalkers", r"\bplaneswalkers?\b|loyalty", ["Planeswalkers"], ["planeswalkers"], []),
    ("removal", "built-in removal", r"(?:destroy|exile) target|deals? \d+ damage to target creature|target opponent "
     r"sacrifices", ["Control"], [], []),
    ("mana", "makes mana / ramp", r"\badd \{|treasure|search your library for [^.]*land", ["Big Mana"], [], []),
]
NEED_LABEL = {"sacrifice fodder": "Sacrifice fodder (expendable creatures / tokens)",
              "attacking": "Creatures that attack (or arrive attacking)", "going wide": "Lots of bodies (going wide)",
              "creature deaths": "Ways to make creatures die (sacrifice outlets)", "tokens": "Token production",
              "creatures entering": "Creatures entering often", "+1/+1 counters": "+1/+1 counter sources",
              "instants and sorceries": "Cheap instants and sorceries", "cards in the graveyard": "Graveyard filling",
              "lands entering": "Extra land drops / land ramp", "artifacts": "Artifacts", "enchantments": "Enchantments",
              "life gain": "Life gain sources", "equipment": "Equipment / auras", "blink targets": "Enter-effect creatures",
              "planeswalkers": "Planeswalkers"}
PAYOFF_LABEL = {"tokens": "Token payoffs", "creature deaths": "Death / sacrifice payoffs",
                "+1/+1 counters": "Counter payoffs", "attacking": "Attack payoffs",
                "sacrifice fodder": "Sacrifice outlets / payoffs"}
INPUT_PHRASE = {"attacking": "attack", "sacrifice fodder": "have expendable creatures to sacrifice",
                "going wide": "build a wide board", "creature deaths": "make creatures die",
                "creatures entering": "put creatures onto the battlefield", "tokens": "make tokens",
                "+1/+1 counters": "put +1/+1 counters out", "instants and sorceries": "cast instants and sorceries",
                "cards in the graveyard": "fill the graveyard", "lands entering": "play extra lands",
                "artifacts": "play / make artifacts", "enchantments": "play enchantments", "life gain": "gain life",
                "equipment": "suit up the commander", "blink targets": "re-use enter effects",
                "planeswalkers": "play planeswalkers"}
OUTPUT_PHRASE = {"tokens": "more creature tokens", "attackers": "extra attackers", "draw": "cards",
                 "damage": "damage / life loss for opponents", "counters": "+1/+1 counters", "removal": "removal",
                 "mana": "mana"}
PLAN_NAME = {"Tokens": "Go-wide creature tokens", "Aristocrats": "Sacrifice & death triggers (aristocrats)",
             "Combat": "Attack triggers / combat", "Counters": "+1/+1 counters", "Proliferate": "Proliferate counters",
             "Spellslinger": "Instants & sorceries (spellslinger)", "Graveyard": "Graveyard value",
             "Reanimator": "Reanimation", "Landfall": "Landfall", "Artifacts": "Artifacts", "Enchantments": "Enchantress",
             "Lifegain": "Life gain", "Voltron": "Voltron (commander damage)", "Blink": "Blink / enter effects",
             "Planeswalkers": "Superfriends (planeswalkers)", "Control": "Control", "Big Mana": "Big mana",
             "Creatures": "Creature beatdown", "Tribal": "Tribal"}
BASIC_NEEDS = [("Ramp", "Ramp"), ("Card Draw", "Card advantage"), ("Removal", "Interaction (removal)"),
               ("Protection", "Protection"), ("Finisher", "Finishers")]


KEYWORDS = (r"(?:flying|vigilance|haste|trample|menace|deathtouch|lifelink|first strike|double strike|reach|hexproof|"
            r"indestructible|defender|flash|prowess|ward \{?\d\}?|partner|this can be your commander)")


def _clauses(text):
    """Clauses worth interpreting: keyword lines, ability-word labels ("Ceaseless Tempest")
    and boilerplate aren't counted."""
    out = []
    for part in re.split(r"\.\s+|\n+|•|—", text):
        part = part.strip(" .")
        if len(part) <= 3 or re.fullmatch(rf"{KEYWORDS}(?:,? {KEYWORDS})*", part):
            continue
        if len(part.split()) <= 3 and not re.search(r"\d|\{", part):
            continue  # an ability word / name, not rules text
        out.append(part)
    return out


def profile(commander_infos, graph=None, tribe=None):
    """Structured understanding of the commander(s). graph: the deck's InteractionGraph (to
    count which deck cards supply each need)."""
    mechanics, themes, inputs, outputs = [], {}, [], []
    clauses, understood = 0, 0
    for info in commander_infos:
        text = cardroles.normalise_text(info.get("oracle_text"), info.get("name", ""))
        roles = cardroles.classify(info)
        for cl in _clauses(text):
            clauses += 1
            hit = any(re.search(pat, cl) for _, _, pat, _, _, _ in MECHANICS) or bool(cardroles.classify(
                dict(info, oracle_text=cl)))
            understood += hit
        for key, label, pat, th, consumes, produces in MECHANICS:
            if re.search(pat, text):
                if key not in [m["key"] for m in mechanics]:
                    mechanics.append(dict(key=key, label=label))
                weight = 1.5 if consumes else 1.0  # what it REWARDS says more about the plan than what it makes
                for i, t in enumerate(th):
                    themes[t] = themes.get(t, 0) + (weight if i == 0 else 0.6)
                inputs += [c for c in consumes if c not in inputs]
                if key in OUTPUT_PHRASE and OUTPUT_PHRASE[key] not in outputs:
                    outputs.append(OUTPUT_PHRASE[key])
        if "Card Draw" in roles and "cards" not in outputs:
            outputs.append("cards")
        if tribe and tribe.lower() in text:
            themes["Tribal"] = themes.get("Tribal", 0) + 1.0
            mechanics.append(dict(key="tribal", label=f"cares about {tribe}s"))
    ranked = sorted(themes.items(), key=lambda kv: -kv[1])
    primary = ranked[0][0] if ranked else None
    supporting = [t for t, _ in ranked[1:5]]
    # engine chain: what goes in -> what the commander does -> what comes out
    trigger = next((m["label"] for m in mechanics if m["key"] in ("attack", "enters", "deaths", "spells", "landfall")),
                   None)
    chain = []
    if inputs:
        chain.append("you " + " and ".join(INPUT_PHRASE.get(i, i) for i in inputs[:3]))
    if any(m["key"] == "sacrifice" for m in mechanics):
        chain.append("sacrifice a creature")
    if trigger:
        chain.append(f"{_short(commander_infos)} triggers ({trigger})")
    if outputs:
        chain.append(" / ".join(outputs[:4]))
    needs = []
    for res in inputs:
        n = _supply(graph, res, "provides")
        needs.append(dict(key=res, label=NEED_LABEL.get(res, res), have=n))
    produced = {r for info in commander_infos for r in synergy.resources(info)[0]}
    for res in produced:
        if res in PAYOFF_LABEL and not any(n["key"] == res + ":payoff" for n in needs):
            needs.append(dict(key=res + ":payoff", label=PAYOFF_LABEL[res], have=_supply(graph, res, "rewards")))
    for role, label in BASIC_NEEDS:
        needs.append(dict(key=role, label=label, have=None))
    return dict(mechanics=mechanics, primary=primary, primary_name=PLAN_NAME.get(primary, primary or "unclear"),
                supporting=supporting, supporting_names=[PLAN_NAME.get(t, t) for t in supporting],
                themes=dict(ranked), chain=chain, needs=needs,
                understanding=round(understood / clauses, 2) if clauses else 0.0,
                clauses=clauses, understood=understood)


def _supply(graph, resource, side):
    if graph is None:
        return None
    idx = 0 if side == "provides" else 1
    return sum(1 for n, r in graph.res.items() if resource in r[idx] and n not in graph.commanders)


def _short(infos):
    return " + ".join((i.get("name") or "").split(",")[0] for i in infos) or "the commander"
