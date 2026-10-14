"""What a card DOES in a deck, beyond its broad role - used by the analyser so it never
treats cards as interchangeable just because they share a role.

* functions(): role SUBROLES - e.g. Card Draw: burst / repeatable / death-trigger /
  combat / token-or-creature based / impulse / loot / wheel; Ramp: mana rock / land ramp /
  treasure / mana creature / cost reduction / ritual / repeatable mana engine; Removal,
  Board Wipe, Token Generation, Aristocrats, Finisher, Protection and Tutor likewise.
  A repeatable engine and a one-shot spell are different jobs.
* is_engine(): a permanent that keeps producing value (triggers / repeatable abilities).
* InteractionGraph: what each card PROVIDES (tokens, sacrifice fodder, deaths, +1/+1
  counters, artifacts, spells, extra lands, attackers...) and what it REWARDS. An edge =
  one card provides what another rewards, with a plain-English reason and a STRENGTH:
    strong   - a card that specifically produces the resource feeds a payoff for it
               (token maker -> token payoff, sacrifice outlet -> death trigger)
    moderate - a card provides it just by being its type and the payoff counts that type
               (any creature -> "whenever a creature enters"; any instant -> magecraft)
    weak     - incidental (any creature "could die") - shown, but never counted in headlines
* engines: groups of cards linked strongly/moderately through one resource that contain
  both providers and payoffs (e.g. a sacrifice engine: fodder + outlets + death payoffs).

All local, deterministic and explainable (Oracle text from cards.db)."""
import re
from collections import defaultdict

import cardroles

# ---------------------------------------------------------------- function subtypes

ENGINE_SUBTYPES = {"repeatable draw", "creature-engine draw", "death-trigger draw", "combat draw",
                   "token/creature-based draw", "repeatable removal", "repeatable recursion", "repeatable token engine",
                   "attack-token generation", "death-token generation", "repeatable mana engine", "token doubler",
                   "death payoff", "death drain", "sacrifice outlet"}


def _text(info):
    return cardroles.normalise_text(info.get("oracle_text"), info.get("name", ""))


def _front_types(info):
    return (info.get("type_line") or "").split(" // ")[0]


def is_permanent(info):
    t = _front_types(info)
    return not ("Instant" in t or "Sorcery" in t)


REPEAT = re.compile(r"\bwhenever\b|\bat the beginning of\b|\{t\}[^.:]*:|^\{[^}]+\}(?:, \{[^}]+\})*:|\bfor each\b", re.M)


def is_engine(info, roles):
    """A permanent with a repeatable trigger / ability that does one of its jobs."""
    if not is_permanent(info) or "Land" in _front_types(info):
        return False
    text = _text(info)
    useful = [r for r, v in roles.items() if r != "Land" and v.confidence >= 0.6]
    return bool(useful) and bool(REPEAT.search(text))


def _has(text, *patterns):
    return any(re.search(p, text) for p in patterns)


def functions(info, roles):
    """{role: [subroles]} for the card's roles."""
    text, types = _text(info), _front_types(info)
    perm = is_permanent(info)
    repeat = bool(REPEAT.search(text)) and perm
    out = {}
    if "Card Draw" in roles:
        f = []
        if _has(text, r"each player discards (?:their|his or her) hand"):
            f.append("wheel")
        if _has(text, r"(?:dies|die|is put into a graveyard)[^.]*draw|whenever [^.]*dies, draw|sacrifice [^:.]*: draw"):
            f.append("death-trigger draw")
        if _has(text, r"deals? combat damage[^.]*draw"):
            f.append("combat draw")
        if _has(text, r"exile the top [^.]*(?:you may (?:play|cast)|until end of turn)"):
            f.append("impulse draw")
        if _has(text, r"draw[^.]*then discard|discard [^.]*then draw|draw[^.]*\. then discard"):
            f.append("loot/filter")
        if repeat and _has(text, r"creature|token|attacks"):
            f.append("token/creature-based draw" if _has(text, r"token|creatures? you control") else "creature-engine draw")
        elif repeat:
            f.append("repeatable draw")
        if not f:
            f.append("burst draw")
        out["Card Draw"] = f
    if "Ramp" in roles:
        f = []
        if _has(text, r"spells? (?:you cast )?cost(?:s)? \{?\d|costs? \{\d\} less"):
            f.append("cost reduction")
        if _has(text, r"search your library for [^.]*land|put [^.]*land cards? [^.]*onto the battlefield|"
                      r"play an additional land"):
            f.append("land ramp")
        if _has(text, r"\btreasure"):
            f.append("treasure generation")
        if _has(text, r"\badd\b"):
            if "Artifact" in types:
                f.append("mana rock")
            elif "Creature" in types:
                f.append("mana creature")
            elif not perm:
                f.append("ritual")
            else:
                f.append("repeatable mana engine")
        if repeat and "treasure generation" in f:
            f.append("repeatable mana engine")
        out["Ramp"] = f or ["ramp"]
    if "Removal" in roles:
        f = []
        if repeat:
            f.append("repeatable removal")
        if _has(text, r"\bexile target\b"):
            f.append("exile")
        if _has(text, r"return target [^.]*to (?:its|their) owner'?s? hand"):
            f.append("bounce")
        if _has(text, r"(?:each|target) (?:player|opponent) sacrifices"):
            f.append("sacrifice (edict)")
        if _has(text, r"deals? (?:\d+|x|damage equal)[^.]*damage to (?:any target|target creature)"):
            f.append("damage")
        if _has(text, r"target (?:nonland )?permanent|target (?:artifact|enchantment)"):
            f.append("targeted permanent")
        if _has(text, r"target (?:creature|attacking|blocking)") or not f:
            f.append("targeted creature")
        out["Removal"] = f
    if "Board Wipe" in roles:
        f = []
        if _has(text, r"destroy all|destroy each"):
            f.append("destroy all")
        if _has(text, r"exile all|exile each"):
            f.append("exile all")
        if _has(text, r"damage to each creature|each creature [^.]*damage|all creatures get -"):
            f.append("damage wipe")
        if _has(text, r"you don't control|your opponents control|opponents control"):
            f.append("asymmetric wipe")
        if _has(text, r"(?:all|each) (?:other )?creatures?\b") and not _has(text, r"nonland permanents|artifacts|enchantments"):
            f.append("creature-only")
        if _has(text, r"with (?:mana value|power|toughness)|nontoken|that (?:aren't|isn't)"):
            f.append("selective wipe")
        out["Board Wipe"] = f or ["board wipe"]
    if "Token Generation" in roles:
        f = []
        if _has(text, r"\btreasure"):
            f.append("treasure generation")
        if _has(text, r"tapped and attacking|whenever [^.]*attacks?[^.]*create|\bmobilize\b") or "mobilize" in text:
            f.append("attack-token generation")
        if _has(text, r"(?:dies|die)[^.]*create|whenever [^.]*dies[^.]*token"):
            f.append("death-token generation")
        if repeat and "repeatable token engine" not in f:
            f.append("repeatable token engine")
        if not f:
            f.append("one-shot token generation")
        out["Token Generation"] = f
    if "Token Doubler" in roles:
        out["Token Doubler"] = ["token doubler"]
    if "Anthem" in roles:
        out["Anthem"] = ["token payoff (anthem)"] if _has(text, r"token") else ["team anthem"]
    if "Sacrifice Outlet" in roles:
        out["Sacrifice Outlet"] = ["sacrifice outlet"] if roles["Sacrifice Outlet"].confidence >= 0.6 else ["sacrifice as a cost"]
    if "Aristocrats" in roles:
        f = []
        if _has(text, r"(?:dies|die)[^.]*(?:loses? \w+ life|damage to each opponent)"):
            f.append("death drain")
        if _has(text, r"(?:dies|die)[^.]*draw"):
            f.append("death-trigger draw")
        if _has(text, r"(?:dies|die)[^.]*create"):
            f.append("death-token generation")
        f.append("death payoff")
        out["Aristocrats"] = f
    if "Finisher" in roles:
        f = []
        if _has(text, r"you win the game|loses? the game"):
            f.append("alternate win")
        if _has(text, r"creatures you control get \+|creatures you control gain trample"):
            f.append("overrun")
        if _has(text, r"each opponent loses|loses? \w+ life|deals? \w+ damage to each opponent|damage equal to the number"):
            f.append("mass drain")
        if _has(text, r"twice that much damage|double that damage|double the damage"):
            f.append("damage multiplier")
        if _has(text, r"additional combat"):
            f.append("extra combat")
        if _has(text, r"(?:have|gain) (?:flying|menace|trample|skulk)|can't be blocked"):
            f.append("mass evasion")
        if _has(text, r"\binfect\b|untap all|extra turn"):
            f.append("combo")
        if "Creature" in types and not f:
            f.append("combat finisher")
        out["Finisher"] = f or ["inevitability / value engine"]
    if "Protection" in roles:
        f = []
        for pat, name in ((r"creatures you control (?:gain|have)|permanents you control (?:gain|have)", "board protection"),
                          (r"(?:equipped|enchanted) creature|target (?:creature|commander) you control", "commander protection"),
                          (r"hexproof|shroud", "hexproof/shroud"), (r"indestructible", "indestructible"),
                          (r"phase[sd]? out", "phase out"), (r"protection from", "protection"),
                          (r"return [^.]*from your graveyard", "recursion as protection"),
                          (r"can't cast spells|can't be countered", "counter protection")):
            if _has(text, pat):
                f.append(name)
        out["Protection"] = f or ["protection"]
    if "Tutor" in roles:
        f = []
        if _has(text, r"search your library for (?:a|an|up to one) card(?! named)"):
            f.append("unrestricted tutor")
        for t in ("creature", "artifact", "enchantment", "instant", "sorcery", "planeswalker", "legendary", "equipment",
                  "aura"):
            if _has(text, rf"search your library for (?:a|an|up to one) {t}") or _has(text, rf"until you reveal (?:a|an) {t}"):
                f.append(f"{t} tutor")
        if _has(text, r"from your graveyard[^.]*search|search your graveyard"):
            f.append("graveyard tutor")
        if _has(text, r"on top of (?:your|their) library|put (?:it|that card) on top"):
            f.append("top-deck tutor")
        out["Tutor"] = f or ["tutor"]
    if "Recursion" in roles:
        out["Recursion"] = ["repeatable recursion"] if repeat else ["one-shot recursion"]
    if "Trigger Doubler" in roles:
        out["Trigger Doubler"] = ["trigger doubler"]
    if "Attack Triggers" in roles:
        out["Attack Triggers"] = ["attack trigger"]
    return out


def flat_functions(info, roles):
    """Every (role, subrole) the card performs, e.g. {('Card Draw', 'repeatable draw')}."""
    fs = functions(info, roles)
    out = {(r, s) for r, subs in fs.items() for s in subs}
    for r in roles:
        if r not in fs and r != "Land":
            out.add((r, r.lower()))
    return out


def compatible(out_info, out_roles, in_info, in_roles, role):
    """Can the IN card take over the OUT card's job in `role`? A repeatable engine can't be
    replaced by a one-shot spell, a creature-based engine not by an instant, etc."""
    of, inf = functions(out_info, out_roles).get(role, []), functions(in_info, in_roles).get(role, [])
    out_engine = bool(set(of) & ENGINE_SUBTYPES) or is_engine(out_info, out_roles)
    in_engine = bool(set(inf) & ENGINE_SUBTYPES) or is_engine(in_info, in_roles)
    if out_engine and not in_engine:
        return False, f"{out_info.get('name')} is a repeatable {role.lower()} engine; the replacement is a one-off"
    if role == "Finisher" and "combat finisher" in of and not (set(inf) & {"combat finisher", "overrun", "mass drain",
                                                                           "alternate win", "damage multiplier"}):
        return False, "a different kind of finisher"
    return True, ""


# ---------------------------------------------------------------- interactions

_P = lambda s: re.compile(s)  # noqa: E731
# (resource, provides-pattern, rewards-pattern, provider label, payoff label)
RESOURCES = [
    ("tokens", _P(r"\bcreate[s]? [^.]*\btokens?\b|\bmobilize\b|\bsquad\b|\bamass\b|\bpopulate\b"),
     _P(r"whenever (?:you create|one or more [^.]*tokens? (?:you control )?enter|a (?:creature )?token)|"
        r"tokens? you control (?:get|have|gain)|number of (?:creature )?tokens you control|for each token|"
        r"twice that many (?:of those )?tokens|sacrifice a token"),
     "creates tokens", "rewards tokens"),
    ("sacrifice fodder", _P(r"\bcreate[s]? [^.]*\b(?:creature )?tokens?\b|\bmobilize\b|\bsquad\b"),
     _P(r"sacrifice (?:a|an|another|one or more|x|two|three) [^:.,]*:|whenever you sacrifice|you may sacrifice another "
        r"creature|as an additional cost to cast this spell, sacrifice (?:a|an) (?:artifact or )?creature"),
     "makes bodies to sacrifice", "sacrifices them for value"),
    ("creature deaths", _P(r"sacrifice (?:a|an|another|one or more|x|two|three) [^:.,]*:|you may sacrifice another creature|"
                           r"each (?:player|opponent) sacrifices"),
     _P(r"whenever [^.]*\b(?:creatures?|permanents?|tokens?|[a-z]+s?) (?:you control )?(?:dies|die|is put into a graveyard "
        r"from the battlefield)\b|creature dying causes|whenever you sacrifice|whenever a player sacrifices"),
     "makes creatures die (sacrifice outlet)", "triggers when creatures die"),
    ("creatures entering", None,
     _P(r"whenever (?:a|another|one or more) (?:nontoken )?(?:creatures?|[a-z]+ creatures?) (?:you control )?enters?"),
     "puts creatures onto the battlefield", "triggers when creatures enter"),
    # "nontoken" payoffs (Metastatic Evangel, Judith...): tokens never trigger them, so token makers
    # are no support - only actual creature cards (set in resources())
    ("nontoken creatures entering", None, None, "is a nontoken creature", "triggers when nontoken creatures enter"),
    ("nontoken creature deaths", None, None, "is a nontoken creature that can die",
     "triggers when nontoken creatures die"),
    ("attacking", _P(r"tapped and attacking|\bmobilize\b|\bhaste\b|additional combat"),
     _P(r"whenever you attack|whenever (?:a|one or more|another) (?:[a-z-]+ )?creatures? you control attacks?|"
        r"creature attacking causes|attacking (?:creatures|tokens) you control"),
     "brings extra attackers", "rewards attacking"),
    ("going wide", None,
     _P(r"creatures you control get \+|(?:tokens|creatures) you control [^.]*get \+|for each creature you control|"
        r"number of (?:creatures|creature tokens|tokens) you control|creatures you control (?:have|gain) "
        r"(?:flying|menace|trample|indestructible)|other [a-z]+(?: creatures)? (?:you control )?get \+\d|"
        r"each creature gets \+\d/\+\d for each"),
     "adds bodies to a wide board", "rewards a wide board"),
    ("+1/+1 counters", _P(r"put (?:a|an|one|two|three|four|five|x|that many|\w+) \+1/\+1 counters?|enters with [^.]*\+1/\+1 "
                          r"counters?|\bproliferate\b"),
     _P(r"(?:with|has|have) (?:a|one or more) \+1/\+1 counters?|\+1/\+1 counters? would be put|for each \+1/\+1 counter|"
        r"whenever [^.]*\+1/\+1 counters? (?:is|are) put|\bproliferate\b|double the number of [^.]*counters?"),
     "puts +1/+1 counters", "rewards +1/+1 counters"),
    ("artifacts", _P(r"create[s]? [^.]*(?:treasure|clue|food|blood|map|powerstone|artifact)"),
     _P(r"whenever (?:an|another|one or more) (?:nontoken )?artifacts?|artifacts? you control|for each artifact|"
        r"\baffinity for artifacts\b|\bimprovise\b|\bmetalcraft\b|sacrifice (?:a|an|x) (?:artifact|treasure)|"
        r"whenever you cast an artifact|artifact spells? you cast"),
     "makes artifacts", "rewards artifacts"),
    ("enchantments", None,
     _P(r"\bconstellation\b|whenever (?:an|another) enchantment|enchantments? you control|for each enchantment|"
        r"whenever you cast an? (?:aura|enchantment)|(?:aura|enchantment) spells? you cast"),
     "is an enchantment", "rewards enchantments"),
    ("instants and sorceries", _P(r"copy (?:target|that) (?:instant|sorcery)"),
     _P(r"whenever you cast (?:an|a) (?:instant|sorcery|noncreature)|\bmagecraft\b|\bprowess\b|instant (?:and|or) "
        r"sorcery spells? you cast"),
     "is an instant/sorcery", "rewards casting spells"),
    ("cards in the graveyard", _P(r"\bmills?\b|\bsurveil\b|\bdiscard (?:a|two|three|x|your hand|that)|put the top "
                                  r"[^.]*into your graveyard|(?:put|puts) (?:that card|them|it|those cards|[^.]*cards?) "
                                  r"into your graveyard|\bdredge\b"),
     _P(r"from (?:your|a|any) graveyard|\bflashback\b|\bunearth\b|\bescape\b|\bdelve\b|\bthreshold\b|\bdelirium\b|"
        r"cards? in your graveyard|creature card in a graveyard|leave your graveyard|reanimate"),
     "fills your graveyard", "uses your graveyard"),
    ("lands entering", _P(r"search your library for [^.]*lands?[^.]*onto the battlefield|put [^.]*lands? cards? [^.]*onto "
                          r"the battlefield|play an additional land|additional lands?"),
     _P(r"\blandfall\b|whenever (?:a|one or more) lands? (?:you control )?enters?"),
     "puts extra lands onto the battlefield", "triggers on lands entering (landfall)"),
    ("life gain", _P(r"\byou gain \w+ life|\blifelink\b|gain life equal"),
     _P(r"whenever you gain life|if you would gain life"),
     "gains life", "rewards gaining life"),
    ("card draw", _P(r"\bdraws? (?:a|two|three|x|that many|\w+) cards?"),
     _P(r"whenever you draw|for each card you've drawn|draw your second card"),
     "draws cards", "rewards drawing cards"),
    ("blink targets", _P(r"exile (?:target|another|up to \w+ target)[^.]*(?:creatures?|permanents?)[^.]*return "
                         r"(?:it|them|that card|those cards)"),
     None, "blinks creatures (re-uses their enters effects)", "has an enters-the-battlefield effect"),
    # a creature with an enters (or dies) effect is exactly what creature recursion / reanimation
    # wants to bring back (Meren, Animate Dead, Living Death ...)
    ("creature recursion", None,
     _P(r"return (?:target |up to \w+ target |another target )?creature cards? from your graveyard|"
        r"creature cards? (?:from|in) (?:a|your|all) graveyards? (?:on)?to the battlefield|"
        r"put (?:target|a|up to \w+ target) creature cards? from (?:a|your) graveyard onto the battlefield|"
        r"return (?:target|that) card to the battlefield|each player (?:exiles all creature cards|returns)"),
     "has an enters / dies effect worth bringing back", "brings creatures back from the graveyard"),
    ("planeswalkers", None, _P(r"planeswalkers? you control|loyalty abilit|\bproliferate\b"),
     "is a planeswalker", "rewards planeswalkers"),
    ("equipment", None, _P(r"equipped creatures? you control|for each (?:equipment|aura)|(?:equipment|auras?) you control|"
                           r"whenever [^.]*becomes (?:equipped|enchanted)|equip abilities|attacks alone|"
                           r"whenever an (?:equipment|aura) (?:you control )?enters|for each aura and equipment|"
                           r"equipped or enchanted"),
     "is equipment", "rewards equipment"),
    ("attack triggers", _P(r"whenever you attack|whenever (?:a|one or more|another) (?:[a-z-]+ )?creatures? you control "
                           r"attacks?|whenever (?:this creature|this|it) attacks"),
     _P(r"creature attacking causes a triggered ability|attacking causes a triggered ability"),
     "has an attack trigger", "makes attack triggers happen twice"),
    ("death triggers", _P(r"whenever [^.]*\b(?:creatures?|tokens?) (?:you control )?(?:dies|die)\b|whenever you sacrifice"),
     _P(r"creature dying causes a triggered ability"),
     "has a death trigger", "makes death triggers happen twice"),
    ("triggered abilities", None, _P(r"if a triggered ability of (?:another|a) [^.]*triggers?[^.]*additional time|"
                                     r"causes a triggered ability of a permanent you control to trigger|"
                                     r"copy that ability"),
     "has triggered abilities", "doubles triggered abilities"),
]
_ETB = re.compile(r"when(?:ever)? (?:this|it)(?: creature| permanent)? enters")
LABELS = {name: (pl, rl) for name, _, _, pl, rl in RESOURCES}
# A type-only provider (any creature, any instant...) still matters for these payoffs.
MODERATE_GENERIC = {"creatures entering", "nontoken creatures entering", "nontoken creature deaths",
                    "instants and sorceries", "artifacts", "enchantments", "attacking",
                    "going wide", "planeswalkers", "equipment", "triggered abilities"}
ENGINES = {"sacrifice fodder": "Sacrifice engine", "creature deaths": "Sacrifice engine", "tokens": "Token engine",
           "going wide": "Go-wide engine", "attacking": "Attack-trigger engine",
           "creatures entering": "Creatures-entering engine", "+1/+1 counters": "Counters engine",
           "artifacts": "Artifact engine", "enchantments": "Enchantment engine",
           "instants and sorceries": "Spells engine", "cards in the graveyard": "Graveyard engine",
           "lands entering": "Landfall engine", "life gain": "Lifegain engine", "blink targets": "Blink engine",
           "planeswalkers": "Planeswalker engine", "equipment": "Equipment engine",
           "triggered abilities": "Trigger-doubling engine", "attack triggers": "Attack-trigger engine",
           "death triggers": "Sacrifice engine", "creature recursion": "Recursion engine",
           "nontoken creature deaths": "Sacrifice engine"}
STRENGTHS = ("strong", "moderate", "weak")


def resources(info, roles=None):
    """(provides, rewards, generic): resources the card produces specifically, resources it
    rewards, and resources it only provides by being a type (any creature, any instant...)."""
    text, types = _text(info), _front_types(info)
    provides, rewards, generic = set(), set(), set()
    for name, prov, rew, _, _ in RESOURCES:
        if prov is not None and prov.search(text):
            provides.add(name)
        if rew is not None and rew.search(text):
            rewards.add(name)
    if "Creature" in types:
        generic |= {"creatures entering", "creature deaths", "attacking", "going wide", "nontoken creatures entering",
                    "nontoken creature deaths"}
    if re.search(r"whenever (?:a|another|one or more) nontoken (?:[a-z]+ )?creatures? (?:you control )?enters?", text):
        rewards.discard("creatures entering")
        rewards.add("nontoken creatures entering")
    if re.search(r"whenever (?:a|another|one or more) nontoken (?:[a-z]+ )?creatures? (?:you control )?(?:dies|die)",
                 text):
        rewards.discard("creature deaths")
        rewards.add("nontoken creature deaths")
    if "tokens" in provides and re.search(r"creature tokens?|\d+/\d+ [^.]*tokens?|\bmobilize\b|\bsquad\b", text):
        provides |= {"creatures entering", "going wide"}
    if "Artifact" in types:
        generic.add("artifacts")
    if "Enchantment" in types:
        generic.add("enchantments")
    if "Instant" in types or "Sorcery" in types:
        generic.add("instants and sorceries")
    if "Planeswalker" in types:
        generic.add("planeswalkers")
    if "Equipment" in (info.get("type_line") or "") or "Aura" in (info.get("type_line") or ""):
        provides.add("equipment")  # the gear itself is exactly what an equipment / voltron payoff wants
    if re.search(r"\bwhenever\b|\bat the beginning of\b|\bwhen [^.]*enters\b", text):
        generic.add("triggered abilities")
    if "Creature" in types and _ETB.search(text):
        rewards.add("blink targets")  # an ETB creature is what blink rewards
    if "Creature" in types and (_ETB.search(text) or re.search(r"when(?:ever)? (?:this|it)(?: creature)? dies", text)):
        provides.add("creature recursion")
    # tokens handed to an OPPONENT (Generous Gift, Beast Within...) don't make this a token maker
    if "tokens" in provides and not re.search(r"(?<!its controller )(?<!that player )(?<!target opponent )"
                                              r"(?<!each opponent )\bcreate[s]? [^.]*\btokens?\b|\bmobilize\b|"
                                              r"\bsquad\b|\bamass\b|\bpopulate\b", text):
        provides -= {"tokens", "sacrifice fodder", "creatures entering", "going wide"}
    generic -= provides
    return provides, rewards, generic


def _strength(specific_provider, resource, either_commander):
    if specific_provider:
        return "strong"
    if resource in MODERATE_GENERIC:
        return "strong" if either_commander and resource in ("attacking", "creatures entering") else "moderate"
    return "weak"


class InteractionGraph:
    """Who works with whom in one deck. interactions(name) -> [(other, text)] for strong and
    moderate links (weak ones only via detailed(name, include_weak=True))."""

    def __init__(self, cards, commanders=(), tribe=None, core=()):
        """cards: {name: (info, roles)} for every non-land card in the deck (incl. commanders)."""
        self.cards = cards
        self.commanders = set(commanders)
        self.res = {n: resources(info, roles) for n, (info, roles) in cards.items()}
        self.links = defaultdict(dict)  # name -> {other: [(strength, text, resource)]}
        providers, payoffs = defaultdict(list), defaultdict(list)
        for n, (prov, rew, gen) in self.res.items():
            for r in prov:
                providers[r].append((n, True))
            for r in gen:
                providers[r].append((n, False))
            for r in rew:
                payoffs[r].append(n)
        for r, pays in payoffs.items():
            pl, rl = LABELS.get(r, (r, r))
            for payoff in pays:
                for prov, specific in providers.get(r, []):
                    if prov == payoff:
                        continue
                    st = _strength(specific, r, prov in self.commanders or payoff in self.commanders)
                    self._add(payoff, prov, st, f"{prov} - {pl if specific else _generic_label(r, pl)}", r)
                    self._add(prov, payoff, st, f"{payoff} - {rl}", r)
        if tribe:
            tribal = [n for n, (info, _) in cards.items() if tribe in cardroles.creature_types(info.get("type_line"))]
            carers = [n for n, (info, _) in cards.items() if tribe.lower() in _text(info)]
            for c in carers:
                for t in tribal:
                    if c != t:
                        self._add(c, t, "strong", f"{t} - is a {tribe}", "tribe")
                        self._add(t, c, "strong", f"{c} - rewards {tribe}s", "tribe")
        self._engines = self._find_engines()

    def _add(self, a, b, strength, text, resource):
        lst = self.links[a].setdefault(b, [])
        if not any(t == text for _, t, _ in lst):
            lst.append((strength, text, resource))

    def _best(self, name, other):
        lst = self.links.get(name, {}).get(other, [])
        return min(lst, key=lambda x: STRENGTHS.index(x[0])) if lst else None

    def detailed(self, name, include_weak=False):
        """[(other, text, strength)] - strongest link per card, strongest first."""
        out = []
        for other in self.links.get(name, {}):
            st, text, _ = self._best(name, other)
            if include_weak or st != "weak":
                out.append((other, text, st))
        return sorted(out, key=lambda x: (STRENGTHS.index(x[2]), x[0]))

    def interactions(self, name):
        """[(other card, reason)] - strong + moderate links only."""
        return [(o, t) for o, t, _ in self.detailed(name)]

    def count(self, name, exclude_commanders=True, strengths=("strong", "moderate")):
        return sum(1 for o, _, st in self.detailed(name, include_weak=True)
                   if st in strengths and not (exclude_commanders and o in self.commanders))

    def with_commander(self, name):
        return [(o, t) for o, t, st in self.detailed(name) if o in self.commanders]

    def commander_strength(self, name):
        sts = [st for o, _, st in self.detailed(name) if o in self.commanders]
        return sts[0] if sts else None

    # ---- engines
    def _find_engines(self):
        members = defaultdict(set)
        payoff_of = defaultdict(set)
        for a, others in self.links.items():
            for b, lst in others.items():
                for st, _, r in lst:
                    if st != "strong" or r not in ENGINES:  # engines are built from strong links only
                        continue
                    members[ENGINES[r]] |= {a, b}
                    if r in self.res.get(a, (set(), set(), set()))[1]:
                        payoff_of[ENGINES[r]].add(a)
        engines = {}
        for e, cards in members.items():
            payoffs = payoff_of[e]
            if len(cards) >= 3 and payoffs and len(cards - payoffs) >= 1:
                engines[e] = dict(members=sorted(cards), payoffs=sorted(payoffs),
                                  with_commander=bool(cards & self.commanders))
        return engines

    def engines(self):
        return self._engines

    def engines_of(self, name):
        return [e for e, v in self._engines.items() if name in v["members"]]

    # ---- cards from outside the deck
    def candidate_links(self, info, roles, exclude=None, include_weak=False):
        """Links an OUTSIDE card would have with this deck: [(deck card, reason, strength)]."""
        prov, rew, gen = resources(info, roles)
        out = {}
        for n, (p2, r2, g2) in self.res.items():
            if n == exclude:
                continue
            best = None
            for r in r2 & (prov | gen):
                st = _strength(r in prov, r, n in self.commanders)
                cand = (st, f"{n} - {LABELS.get(r, (r, r))[1]}")
                best = cand if best is None or STRENGTHS.index(st) < STRENGTHS.index(best[0]) else best
            for r in rew & (p2 | g2):
                st = _strength(r in p2, r, n in self.commanders)
                cand = (st, f"{n} - {LABELS.get(r, (r, r))[0] if r in p2 else _generic_label(r, LABELS.get(r, (r, r))[0])}")
                best = cand if best is None or STRENGTHS.index(st) < STRENGTHS.index(best[0]) else best
            if best and (include_weak or best[0] != "weak"):
                out[n] = best
        return sorted(((n, t, st) for n, (st, t) in out.items()), key=lambda x: (STRENGTHS.index(x[2]), x[0]))


def link_value(links, commanders=()):
    """Interaction QUALITY, not count: strong (a real mechanical relationship) = 1, moderate
    (shares a type / a broad trigger - incidental) = 0.15, weak = 0; links with the commander
    count double; and each further link counts a little less, so 30 incidental links can't
    outweigh a handful of direct ones."""
    vals = sorted(((1.0 if s == "strong" else 0.15 if s == "moderate" else 0.0) * (2 if o in commanders else 1)
                   for o, _, s in links), reverse=True)
    return sum(v / (1 + 0.08 * i) for i, v in enumerate(vals))


def _generic_label(resource, label):
    return {"creatures entering": "is a creature (triggers it when it enters)",
            "creature deaths": "is a creature (could die)", "attacking": "is a creature (can attack)",
            "going wide": "is a creature (one more body)", "artifacts": "is an artifact",
            "enchantments": "is an enchantment", "instants and sorceries": "is an instant/sorcery",
            "planeswalkers": "is a planeswalker", "equipment": "is equipment",
            "triggered abilities": "has a triggered ability"}.get(resource, label)
