"""What jobs does a card do in a Commander deck? A small, readable rules engine.

classify(card) -> {role: Role(confidence, reason)} using the card's Oracle text, type
line, keywords and mana value (from cards.db's oracle_cards table). A card can have
several roles (e.g. Ramp + Creature + Sacrifice Outlet). Every match carries a short
plain-English reason, so recommendations can explain themselves.

To improve classification, edit RULES below (or add special cases in `classify`) and
bump CLASSIFIER_VERSION so cached results are recomputed. Deterministic and offline."""
import json
import re
from dataclasses import dataclass

CLASSIFIER_VERSION = 7

ROLES = [
    "Ramp", "Card Draw", "Removal", "Board Wipe", "Counterspell", "Protection", "Tutor", "Recursion",
    "Reanimation", "Graveyard Hate", "Graveyard Enabler", "Token Generation", "Sacrifice Outlet", "Aristocrats",
    "Lifegain", "Life Loss / Drain", "Mill", "Discard", "Blink / Flicker", "+1/+1 Counters", "Proliferate",
    "Equipment", "Auras", "Landfall", "Spellslinger", "Artifact Synergy", "Enchantment Synergy",
    "Creature Synergy", "Tribal / Typal Synergy", "Mana Sink", "Finisher", "Combo Piece", "Group Hug",
    "Land",
]

# The headline functions a deck is counted on (composition / weaknesses).
CORE_ROLES = ["Ramp", "Card Draw", "Removal", "Board Wipe", "Counterspell", "Protection", "Tutor", "Recursion",
              "Finisher"]


@dataclass
class Role:
    confidence: float  # 0..1: how sure the rule is that the card really does this
    reason: str


@dataclass
class Rule:
    role: str
    pattern: str            # regex on lower-case Oracle text (reminder text removed)
    reason: str
    confidence: float = 0.8
    types: tuple = ()       # card must have one of these types (if given)
    not_types: tuple = ()   # ...and none of these
    max_cmc: float = None
    min_cmc: float = None

    def __post_init__(self):
        self.rx = re.compile(self.pattern)


N = r"(?:a|an|one|two|three|four|five|x|\d+|that many|up to (?:one|two|three|\w+))"
RULES = [
    # --- mana
    Rule("Ramp", r"\badd (?:\{[wubrgcs]\}|one mana|two mana|three mana|x mana|mana of any|an amount of|that much)",
         "produces mana", 0.9, not_types=("Land",)),
    Rule("Ramp", r"search your library for (?:a|up to \w+|two|three) [^.]*?(?:land|forest|island|plains|swamp|"
                 r"mountain)[^.]*?(?:onto the battlefield|put (?:it|them|those cards) onto the battlefield)",
         "puts extra lands onto the battlefield", 0.95),
    Rule("Ramp", r"search your library for [^.]*?basic land[^.]*?(?:hand|battlefield)", "fetches lands", 0.75),
    Rule("Ramp", r"play (?:an|two|three|any number of) additional lands?|put (?:a|up to \w+) land cards? from your hand "
                 r"onto the battlefield", "puts extra lands into play", 0.85),
    Rule("Ramp", r"create (?:a|an|one|two|three|x|\w+) treasure", "makes Treasure", 0.7),
    Rule("Ramp", r"(?:spells|abilities) you cast cost \{\d\} less|cost \{\d\} less to cast", "makes spells cheaper",
         0.55),
    # --- cards
    Rule("Card Draw", rf"\bdraws? {N} (?:additional )?cards?", "draws cards", 0.85),
    Rule("Card Draw", r"\bdraw a card\b", "draws a card", 0.7),
    Rule("Card Draw", r"exile the top [^.]*?card[^.]*?(?:you may (?:play|cast)|until (?:the )?end of)",
         "card advantage (exile top cards and play them)", 0.7),
    Rule("Card Draw", r"\binvestigate\b|create (?:a|an|one|two|\w+) clue", "makes Clues to draw later", 0.5),
    Rule("Card Draw", r"(?:reveal|look at) the top [\s\S]{0,160}?puts? [^.]*?into (?:your|their owner's) hand",
         "gets extra cards into hand", 0.55),
    # --- interaction
    Rule("Removal", r"(?:destroy|exile) (?:target|another target|up to (?:one|two|\w+) target) "
                    r"(?:[^.]*?)(?:creature|artifact|enchantment|planeswalker|permanent|battle)",
         "destroys or exiles a target", 0.9),
    Rule("Removal", r"deals? (?:\d+|x|damage equal to [^.]*?) damage to (?:any target|target creature|"
                    r"target (?:creature or planeswalker|planeswalker|attacking))", "damages a creature", 0.7),
    Rule("Removal", r"target (?:player|opponent) sacrifices", "makes an opponent sacrifice", 0.75),
    Rule("Removal", r"each (?:other player|opponent) sacrifices", "makes opponents sacrifice", 0.6),
    Rule("Removal", r"owner of target [^.]*?shuffles it into|put target [^.]*?on the (?:top or )?bottom of its owner's "
                    r"library|target [^.]*?owner shuffles it", "tucks a permanent away", 0.75),
    Rule("Removal", r"return target (?:creature|nonland permanent|permanent|artifact)[^.]*? to its owner's hand",
         "bounces a permanent", 0.5),
    Rule("Removal", r"target creature (?:an opponent controls )?gets -\d+/-\d+|target creature gets -x/-x",
         "shrinks a creature", 0.6),
    Rule("Removal", r"fights? (?:target|another target|up to one target) creature", "fights a creature", 0.6),
    Rule("Board Wipe", r"(?:destroy|exile) all (?:creatures|nonland permanents|artifacts|enchantments|other "
                       r"creatures|permanents|planeswalkers|nonland)", "removes everything of a type", 0.95),
    Rule("Board Wipe", r"(?:all|each) (?:other )?creatures? (?:you don't control )?gets? -(?:\d+|x)/-(?:\d+|x)",
         "shrinks every creature", 0.85),
    Rule("Board Wipe", r"deals? (?:\d+|x) damage to each (?:creature|other creature|creature and each)",
         "damages every creature", 0.8),
    Rule("Board Wipe", r"return all (?:creatures|nonland permanents|other)[^.]*? to their owners' hands",
         "bounces everything", 0.8),
    Rule("Board Wipe", r"each (?:player|opponent) sacrifices (?:all|each|[^.]*?creatures)", "mass sacrifice", 0.7),
    Rule("Counterspell", r"counter target (?:[^.]*?)spell", "counters a spell", 0.95),
    Rule("Counterspell", r"counter target (?:activated|triggered) ability", "counters an ability", 0.6),
    Rule("Protection", r"(?:creatures|permanents) you control (?:gain|have|get [^.]*? and gain) "
                       r"(?:hexproof|indestructible|shroud|protection)", "protects your board", 0.9),
    Rule("Protection", r"target (?:creature|permanent|artifact|commander)[^.]*? you control (?:gains?|has) "
                       r"(?:hexproof|indestructible|shroud|protection)", "protects a key permanent", 0.8),
    Rule("Protection", r"(?:equipped|enchanted) creature (?:has|gains|gets [^.]*? and has) [^.]*?"
                       r"(?:hexproof|indestructible|shroud|protection)", "protects the creature it's on", 0.75),
    Rule("Protection", r"phase(?:s)? out", "phases things out to save them", 0.7),
    Rule("Protection", r"can't be countered", "can't be countered", 0.3),
    Rule("Tutor", r"search your library for (?:a|an|up to one|two|three|any) (?:[^.]*?)?card(?!s? named)",
         "searches your library for a card", 0.85),
    # --- graveyard
    Rule("Recursion", r"return (?:target|up to \w+ target|another target|all|each)? ?[^.]*?card[^.]*? from your "
                      r"graveyard to (?:your hand|the battlefield|its owner's hand)", "gets cards back from your graveyard",
         0.85),
    Rule("Recursion", r"you may (?:play|cast) [^.]*?from your graveyard", "lets you play cards from your graveyard", 0.8),
    Rule("Reanimation", r"(?:return|put) [^.]*?creature card[^.]*? from (?:a|your|any|an opponent's) graveyard onto the "
                        r"battlefield|from (?:a|your|all) graveyards? onto the battlefield|enchant creature card in a "
                        r"graveyard|return enchanted creature card to the battlefield", "reanimates creatures", 0.9),
    Rule("Graveyard Hate", r"exile (?:target player's graveyard|all graveyards|all cards from (?:all )?graveyards|"
                           r"each opponent's graveyard|target card from a graveyard|up to \w+ target cards? from "
                           r"(?:a single |)graveyards?)", "exiles graveyards", 0.9),
    Rule("Graveyard Hate", r"cards in graveyards (?:can't|lose)|if a card would be put into an opponent's graveyard",
         "stops graveyard strategies", 0.85),
    Rule("Graveyard Enabler", r"\bmill (?:\w+ )?cards?\b|\bmills? (?:\w+|x) cards?|put the top [^.]*? into your graveyard"
                              r"|\bsurveil\b|\bdredge\b|discard [^.]*?, then draw|\bself-mill", "fills your graveyard", 0.65),
    Rule("Mill", r"(?:target player|each opponent|target opponent|each player) mills|mills? (?:x|\d+|\w+) cards?",
         "mills cards", 0.7),
    # --- go wide / sacrifice
    Rule("Token Generation", r"\bcreate (?:a|an|one|two|three|four|five|x|that many|\d+|\w+)[^.]*?token",
         "creates tokens", 0.85),
    Rule("Token Generation", r"\bpopulate\b|\bamass\b|\bfabricate\b|\bincubate\b", "creates tokens", 0.75),
    Rule("Sacrifice Outlet", r"sacrifice (?:a|an|another|any number of) (?:creature|artifact|permanent|nonland)[^:.]*?:",
         "free sacrifice outlet", 0.85),
    Rule("Sacrifice Outlet", r"as an additional cost to cast this spell, sacrifice", "sacrifices as part of casting", 0.4),
    Rule("Aristocrats", r"whenever (?:a|another|one or more) (?:nontoken )?(?:creature|creatures)[^.]*?(?:you control )?"
                        r"(?:dies|die|is put into a graveyard)", "rewards creatures dying", 0.85),
    # --- life
    Rule("Lifegain", r"\byou gain (?:\d+|x|that much|life)|gains? \d+ life|whenever you gain life", "gains life", 0.7),
    Rule("Life Loss / Drain", r"(?:each opponent|target opponent|target player|each player) loses (?:\d+|x|that much|life)",
         "drains opponents", 0.8),
    Rule("Discard", r"(?:target (?:player|opponent)|each (?:opponent|player)) discards", "makes opponents discard", 0.75),
    # --- other mechanics
    Rule("Blink / Flicker", r"exile (?:target|another target|up to \w+ target|it|that creature|each|another)[^.]*?"
                            r"(?:\. |, then |)return (?:it|that card|them|those cards|the exiled card)[^.]*? to the "
                            r"battlefield", "blinks permanents", 0.85),
    Rule("+1/+1 Counters", r"\+1/\+1 counters?", "uses +1/+1 counters", 0.8),
    Rule("+1/+1 Counters", r"one or more counters|twice that many [^.]*?counters|counters? on (?:each|a|target) "
                           r"permanent you control", "adds or doubles counters", 0.6),
    Rule("Proliferate", r"\bproliferate\b", "proliferates", 0.95),
    Rule("Equipment", r"\bequipped creature\b|\bequip\b|equipment (?:you control|spells)", "Equipment", 0.8),
    Rule("Auras", r"\benchanted creature\b|aura spells|auras? you control|enchant creature", "Auras", 0.75),
    Rule("Landfall", r"\blandfall\b|whenever a land (?:you control )?enters|whenever (?:one or more )?lands? enters? "
                     r"the battlefield under your control", "landfall", 0.9),
    Rule("Spellslinger", r"whenever you cast (?:an instant or sorcery|a noncreature)|instant (?:and|or) sorcery spells "
                         r"you cast|\bmagecraft\b|\bprowess\b|copy target instant or sorcery|copy that spell",
         "rewards casting spells", 0.85),
    Rule("Artifact Synergy", r"whenever (?:an|another|one or more) (?:nontoken )?artifacts?|artifacts you control|"
                             r"for each artifact|artifact spells you cast|\bimprovise\b|\baffinity for artifacts\b",
         "works with artifacts", 0.8),
    Rule("Enchantment Synergy", r"\bconstellation\b|whenever (?:an|another) enchantment|enchantments you control|"
                                r"for each enchantment|enchantment spells you cast", "works with enchantments", 0.8),
    Rule("Creature Synergy", r"creatures you control get \+|whenever (?:a|another) creature (?:you control )?enters|"
                             r"for each creature you control|other creatures you control", "rewards having creatures",
         0.65),
    Rule("Tribal / Typal Synergy", r"choose a creature type|creatures of the chosen type|(?:other )?[a-z]+s you control "
                                   r"get \+|[a-z]+ spells you cast cost", "creature-type synergy", 0.5),
    Rule("Mana Sink", r"\{x\}[^.]*?:|\{\d+\}(?:\{[wubrg]\})*: [^.]*?(?:\+1/\+1|draw|create|deals)",
         "repeatable mana sink", 0.5),
    Rule("Finisher", r"creatures you control (?:gain [^.]*?and )?get \+(?:\d+|x)/\+(?:\d+|x)[^.]*?trample|"
                     r"creatures you control gain trample and get \+", "mass pump (overrun)", 0.85),
    Rule("Finisher", r"you win the game|each opponent loses (?:\d\d|half)|\binfect\b|additional combat phase|"
                     r"creatures you control get \+\d+/\+\d+ and gain trample|double (?:the )?damage|"
                     r"double that damage|loses the game", "can end the game", 0.75),
    Rule("Combo Piece", r"untap all (?:other )?(?:nonland )?permanents|untap target (?:artifact|permanent)[^.]*?"
                        r"\. |take an extra turn|copy target activated", "known combo-type effect", 0.4),
    Rule("Group Hug", r"each player (?:draws|may draw|may put a land|adds|gains)", "helps every player", 0.7),
]

_REMINDER = re.compile(r"\([^)]*\)")


def normalise_text(text, name=""):
    """Lower-case Oracle text with reminder text removed and the card's own name
    replaced by 'this', so patterns don't need to know card names."""
    t = _REMINDER.sub("", text or "")
    if name:
        for part in {name, *name.split(" // ")}:
            if part:
                t = t.replace(part, "this")
                first = part.split(",")[0]
                if len(first) > 3:
                    t = t.replace(first, "this")
    return t.lower()


def classify(card):
    """card: dict with name, oracle_text, type_line, keywords (json/list), cmc,
    power. Returns {role: Role}."""
    name = card.get("name") or ""
    text = normalise_text(card.get("oracle_text"), name)
    types = card.get("type_line") or ""
    kw = card.get("keywords") or []
    if isinstance(kw, str):
        try:
            kw = json.loads(kw)
        except ValueError:
            kw = []
    kw = {k.lower() for k in kw}
    cmc = float(card.get("cmc") or 0)
    out = {}

    def add(role, conf, reason):
        if role not in out or out[role].confidence < conf:
            out[role] = Role(conf, reason)

    front = types.split(" // ")[0]
    if "Land" in front:
        add("Land", 1.0, "is a land")
    for r in RULES:
        if r.types and not any(t in types for t in r.types):
            continue
        if r.not_types and any(t in front for t in r.not_types):
            continue
        if r.max_cmc is not None and cmc > r.max_cmc:
            continue
        if r.min_cmc is not None and cmc < r.min_cmc:
            continue
        if r.rx.search(text):
            add(r.role, r.confidence, r.reason)

    # --- special cases the plain patterns can't express ---------------------------------
    if "Equipment" in types:
        add("Equipment", 1.0, "is Equipment")
    if "Aura" in types:
        add("Auras", 0.9, "is an Aura")
    if "Ramp" in out and "Land" in front:
        del out["Ramp"]  # lands make mana by definition; land-based ramp is counted as lands
    # "Draw a card" on a creature's ETB is a cantrip, not much card advantage
    if "Card Draw" in out and out["Card Draw"].reason == "draws a card":
        repeat = re.search(r"\bwhenever\b[^.]*draw a card|at the beginning of[^.]*draw a card", text)
        out["Card Draw"] = Role(0.75 if repeat else 0.45,
                                "draws a card every time" if repeat else "draws one card (cantrip)")
    if "Tutor" in out:
        searches = re.findall(r"search your library for ([^.]*?)(?:,|\.| and put| reveal)", text)
        landy = re.compile(r"land|forest|island|plains|swamp|mountain|gate|desert")
        if searches and all(landy.search(x) for x in searches):
            del out["Tutor"]  # searching for lands is ramp / fixing, not a tutor
    if "Card Draw" in out and not _you_draw(text) and not re.search(r"exile the top|investigate|clue|into your hand",
                                                                        text):
        del out["Card Draw"]  # e.g. "whenever an opponent draws a card"
    if "Removal" in out and out["Removal"].reason == "destroys or exiles a target":
        hits = re.findall(r"(?:destroy|exile) (?:target|another target|up to (?:one|two|\w+) target) ([^.]*)", text)
        if hits and all(("you control" in h and "don't control" not in h) or "graveyard" in h or "card from" in h
                        for h in hits):
            del out["Removal"]  # your own permanent (blink) or a card in a graveyard - not removal
    for k, role, reason in (("hexproof", "Protection", "has hexproof"), ("indestructible", "Protection",
                                                                        "is indestructible"),
                            ("proliferate", "Proliferate", "proliferates"), ("landfall", "Landfall", "landfall"),
                            ("lifelink", "Lifegain", "has lifelink (minor)"), ("infect", "Finisher", "has infect"),
                            ("mill", "Mill", "mills"), ("treasure", "Ramp", "makes Treasure"),
                            ("investigate", "Card Draw", "investigates")):
        if k in kw:
            add(role, 0.5 if role == "Protection" else 0.35 if k == "lifelink" else 0.7, reason)
    # Big efficient threats count as finishers (not everything big - only when it's real)
    try:
        power = float(card.get("power") or 0)
    except ValueError:
        power = 0
    if "Creature" in front and power >= 6 and ({"trample", "flying", "double strike"} & kw):
        add("Finisher", 0.55, f"big evasive threat ({int(power)} power)")
    if "Board Wipe" in out and "Removal" in out and out["Removal"].confidence < out["Board Wipe"].confidence:
        # "destroy all creatures" shouldn't also count as targeted removal
        if not re.search(r"(?:destroy|exile) target", text):
            del out["Removal"]
    if "Removal" in out and re.search(r"\boverload\b", text):
        add("Board Wipe", 0.7, "overload turns it into a one-sided wipe")
    return out


_DRAW = re.compile(r"(\b[\w']+\b)?\s*\b(draws?)\b (?:a|an|one|two|three|four|x|\d+|that many|cards?|up to)")
_OTHERS = {"opponent", "opponents", "player", "players", "controller", "they", "owner"}


def _you_draw(text):
    """Is there a draw that YOU get (not 'an opponent draws')?"""
    for m in _DRAW.finditer(text):
        prev, verb = (m.group(1) or "").lower(), m.group(2)
        if verb == "draw" or prev not in _OTHERS:
            return True
    return False


def roles_to_json(roles):
    return json.dumps({k: [round(v.confidence, 2), v.reason] for k, v in roles.items()})


def roles_from_json(text):
    return {k: Role(v[0], v[1]) for k, v in json.loads(text or "{}").items()}


def creature_types(type_line):
    """'Legendary Creature — Elf Druid' -> {'Elf', 'Druid'}"""
    if not type_line or "Creature" not in type_line:
        return set()
    parts = re.split(r"\s+[—-]\s+", type_line.split(" // ")[0], maxsplit=1)
    return set(parts[1].split()) if len(parts) > 1 else set()
