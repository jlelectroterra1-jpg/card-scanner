"""Commander deck rules used for warnings (never to change a deck automatically).

Kept deliberately small and data-driven so special cases can be added later:
add names to ANY_NUMBER / UP_TO, or extend `warnings()`."""

DECK_SIZE = 100
BASIC_NAMES = {"Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes",
               "Snow-Covered Plains", "Snow-Covered Island", "Snow-Covered Swamp",
               "Snow-Covered Mountain", "Snow-Covered Forest", "Snow-Covered Wastes"}
# "A deck can have any number of cards named ..."
ANY_NUMBER = {"Relentless Rats", "Shadowborn Apostle", "Persistent Petitioners", "Rat Colony", "Dragon's Approach",
              "Slime Against Humanity", "Hare Apparent", "Templar Knight", "Tempest Hawk", "Cid, Timeless Artificer"}
# "A deck can have up to N cards named ..."
UP_TO = {"Seven Dwarves": 7, "Nazgûl": 9}

COLOUR_NAMES = {"W": "White", "U": "Blue", "B": "Black", "R": "Red", "G": "Green"}
WUBRG = "WUBRG"

# Grouping for the deck view (first match wins, in this order).
TYPE_GROUPS = ["Creature", "Planeswalker", "Battle", "Instant", "Sorcery", "Artifact", "Enchantment", "Land"]
GROUP_LABELS = {"Commander": "Commander", "Creature": "Creatures", "Planeswalker": "Planeswalkers",
                "Battle": "Battles", "Instant": "Instants", "Sorcery": "Sorceries", "Artifact": "Artifacts",
                "Enchantment": "Enchantments", "Land": "Lands", "Other": "Other"}
GROUP_ORDER = ["Commander", "Creature", "Planeswalker", "Battle", "Instant", "Sorcery", "Artifact", "Enchantment",
               "Land", "Other"]


def is_basic(name, type_line=None):
    return name in BASIC_NAMES or bool(type_line and "Basic Land" in type_line)


def copy_limit(name, type_line=None):
    """How many copies a Commander deck may have (None = no limit)."""
    if is_basic(name, type_line) or name in ANY_NUMBER:
        return None
    return UP_TO.get(name, 1)


def group_of(type_line, role="main"):
    """'Commander' for the commander(s), else the card's main type for the deck view.
    Creatures win over Artifact/Enchantment/Land (artifact creatures are creatures);
    for double-faced cards the front face decides."""
    if role in ("commander", "partner"):
        return "Commander"
    front = (type_line or "").split(" // ")[0]
    for t in TYPE_GROUPS:
        if t in front:
            return t
    return "Other"


def colour_identity(*identities):
    """Combine colour identities ('WU', 'B' -> 'WUB'), in WUBRG order."""
    have = set("".join(i or "" for i in identities))
    return "".join(c for c in WUBRG if c in have)


def colour_words(identity):
    return "/".join(COLOUR_NAMES[c] for c in identity) if identity else "Colourless"


def warnings(cards, commander_identity, has_commander=True):
    """cards: [{card_name, quantity, role, type_line, color_identity, legal_commander}].
    Returns [{kind, card, text}] - kinds: 'count', 'commander', 'colour', 'legality', 'duplicate'."""
    out = []
    total = sum(c["quantity"] for c in cards if c.get("role") in ("commander", "partner", "main"))
    if not has_commander:
        out.append(dict(kind="commander", card=None, text="No commander chosen yet"))
    if total != DECK_SIZE and total:
        out.append(dict(kind="count", card=None,
                        text=f"{total} cards - a Commander deck has {DECK_SIZE}"
                             f" ({'remove ' + str(total - DECK_SIZE) if total > DECK_SIZE else 'add ' + str(DECK_SIZE - total)})"))
    allowed = set(commander_identity or "")
    names = {}
    for c in cards:
        if c.get("role") not in ("commander", "partner", "main"):
            continue
        names[c["card_name"]] = names.get(c["card_name"], 0) + c["quantity"]
        if c.get("role") in ("commander", "partner") and c.get("type_line") and "Legendary" not in c["type_line"]:
            out.append(dict(kind="commander", card=c["card_name"],
                            text=f"{c['card_name']} isn't legendary - check it can be your commander"))
        outside = set(c.get("color_identity") or "") - allowed
        if has_commander and outside and c.get("role") == "main":
            out.append(dict(kind="colour", card=c["card_name"],
                            text=f"{c['card_name']} is {colour_words(colour_identity(c.get('color_identity')))} but your "
                                 f"commander is {colour_words(commander_identity)}"))
        legal = c.get("legal_commander")
        if legal and legal != "legal":
            out.append(dict(kind="legality", card=c["card_name"],
                            text=f"{c['card_name']} is {legal.replace('_', ' ')} in Commander"))
    type_of = {c["card_name"]: c.get("type_line") for c in cards}
    for name, n in names.items():
        limit = copy_limit(name, type_of.get(name))
        if limit is not None and n > limit:
            out.append(dict(kind="duplicate", card=name,
                            text=f"{n} copies of {name} - Commander allows {limit}"))
    return out
