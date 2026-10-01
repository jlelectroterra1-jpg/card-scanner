"""What is a Commander deck trying to do? Themes (strategies) and how to spot them.

Detection looks at the COMMANDER first (its Oracle text, keywords, creature types and
roles), then lets the rest of the deck nudge the result. The user can always override
it with their own deck goals - detection is a suggestion, not a verdict."""
import re
from collections import Counter

import cardroles

# Theme -> card roles that serve it, and extra text cues for commanders/cards.
THEMES = {
    "Counters": dict(roles=["+1/+1 Counters", "Proliferate"], cues=[r"\+1/\+1 counter", r"\bcounters?\b"]),
    "Proliferate": dict(roles=["Proliferate", "+1/+1 Counters"], cues=[r"\bproliferate\b"]),
    "Tokens": dict(roles=["Token Generation", "Creature Synergy"], cues=[r"\btokens?\b", r"\bpopulate\b"]),
    "Aristocrats": dict(roles=["Sacrifice Outlet", "Aristocrats", "Life Loss / Drain"],
                        cues=[r"\bsacrifice\b", r"\bdies\b", r"\bdying\b"]),
    "Graveyard": dict(roles=["Graveyard Enabler", "Recursion", "Reanimation"],
                      cues=[r"\bgraveyard\b", r"\bmill\b"]),
    "Reanimator": dict(roles=["Reanimation", "Graveyard Enabler"], cues=[r"from (?:a|your) graveyard onto the battlefield"]),
    "Voltron": dict(roles=["Equipment", "Auras", "Protection"],
                    cues=[r"\bequipped\b", r"\benchanted\b", r"commander damage", r"double strike"]),
    "Equipment": dict(roles=["Equipment"], cues=[r"\bequip", r"\bequipment\b"]),
    "Auras": dict(roles=["Auras"], cues=[r"\bauras?\b", r"\benchanted\b"]),
    "Spellslinger": dict(roles=["Spellslinger"],
                         cues=[r"instant (?:and|or) sorcery", r"whenever you cast (?:an instant|a noncreature)",
                               r"copy (?:target|that|it|the next) (?:instant|sorcery|spell)"]),
    "Artifacts": dict(roles=["Artifact Synergy"], cues=[r"\bartifacts?\b"]),
    "Enchantments": dict(roles=["Enchantment Synergy", "Auras"], cues=[r"\benchantments?\b", r"\bconstellation\b"]),
    "Landfall": dict(roles=["Landfall"], cues=[r"\blandfall\b", r"whenever (?:a|one or more) lands? enters",
                                              r"additional lands?"]),
    "Lifegain": dict(roles=["Lifegain"], cues=[r"gain(?:s)? life", r"\blifelink\b"]),
    "Group Hug": dict(roles=["Group Hug"], cues=[r"each player"]),
    "Control": dict(roles=["Counterspell", "Removal", "Board Wipe"], cues=[r"\bcounter target\b"]),
    "Tribal": dict(roles=["Tribal / Typal Synergy"], cues=[]),
    "Blink": dict(roles=["Blink / Flicker"], cues=[r"\bexile\b[^.]*\breturn\b", r"enters the battlefield"]),
    "Mill": dict(roles=["Mill"], cues=[r"\bmills?\b"]),
    "Creatures": dict(roles=["Creature Synergy"], cues=[r"creatures? you control", r"creature spells"]),
    "Planeswalkers": dict(roles=[], cues=[r"\bplaneswalkers?\b", r"loyalty"]),
    "Big Mana": dict(roles=["Ramp", "Mana Sink"], cues=[r"\bx\b", r"mana value \d or greater"]),
}
THEME_NAMES = list(THEMES)

# Common creature types worth treating as a typal theme when the commander names them.
TYPES = ["Elf", "Goblin", "Zombie", "Vampire", "Dragon", "Angel", "Wizard", "Merfolk", "Sliver", "Dinosaur",
         "Cat", "Dog", "Knight", "Soldier", "Warrior", "Pirate", "Rogue", "Cleric", "Spirit", "Human", "Beast",
         "Elemental", "Faerie", "Rat", "Squirrel", "Snake", "Demon", "Horror", "Eldrazi", "Phyrexian", "Ninja",
         "Samurai", "Shaman", "Druid", "Treefolk", "Hydra", "Sphinx", "Giant", "Dwarf", "Kithkin", "Ally",
         "Assassin", "Insect", "Bird", "Wolf", "Werewolf", "Skeleton", "Construct", "Myr", "Thopter", "Golem",
         "Kraken", "Octopus", "Serpent", "Fish", "Frog", "Rabbit", "Bat", "Mouse", "Otter", "Lizard", "Minotaur"]


def card_themes(card, roles=None):
    """Which themes a single card serves: {theme: weight 0..1}."""
    roles = roles if roles is not None else cardroles.classify(card)
    out = {}
    for theme, spec in THEMES.items():
        w = sum(roles[r].confidence for r in spec["roles"] if r in roles)
        if w:
            out[theme] = min(1.0, w)
    types = card.get("type_line") or ""
    if "Planeswalker" in types:
        out["Planeswalkers"] = 1.0
    if "Artifact" in types:
        out["Artifacts"] = max(out.get("Artifacts", 0), 0.5)
    if "Enchantment" in types:
        out["Enchantments"] = max(out.get("Enchantments", 0), 0.5)
    if "Instant" in types or "Sorcery" in types:
        out["Spellslinger"] = max(out.get("Spellslinger", 0), 0.35)
    if "Equipment" in types:
        out["Voltron"] = max(out.get("Voltron", 0), 0.5)
    return out


def detect(commanders, deck_cards=()):
    """commanders / deck_cards: card dicts (oracle_text, type_line, keywords, ...).
    Returns dict(primary=[...], secondary=[...], scores={theme: score}, reasons={theme: [..]},
    tribe=<creature type or None>). Commander evidence counts most."""
    scores, reasons = Counter(), {}

    def note(theme, w, why):
        scores[theme] += w
        reasons.setdefault(theme, [])
        if why not in reasons[theme]:
            reasons[theme].append(why)

    tribe_votes = Counter()
    for c in commanders:
        text = cardroles.normalise_text(c.get("oracle_text"), c.get("name", ""))
        roles = cardroles.classify(c)
        for theme, w in card_themes(c, roles).items():
            note(theme, 3.0 * w, f"{c['name']} " + ", ".join(roles[r].reason for r in THEMES[theme]["roles"]
                                                             if r in roles)[:80] if THEMES[theme]["roles"] and any(
                r in roles for r in THEMES[theme]["roles"]) else f"{c['name']} is that kind of card")
        for theme, spec in THEMES.items():
            hits = [cue for cue in spec["cues"] if re.search(cue, text)]
            if hits:
                note(theme, 1.5 * len(hits), f"{c['name']}'s text mentions {_cue_words(hits[0])}")
        for t in TYPES:
            if cares_about_type(text, t):
                tribe_votes[t] += 8
    # The deck: what do the other cards lean towards?
    deck_theme = Counter()
    n = 0
    for c in deck_cards:
        if "Land" in (c.get("type_line") or "").split(" // ")[0]:
            continue
        n += 1
        for theme, w in card_themes(c).items():
            deck_theme[theme] += w
        for t in cardroles.creature_types(c.get("type_line")):
            tribe_votes[t] += 1
    for theme, total in deck_theme.items():
        share = total / max(1, n)
        if share >= 0.08:
            note(theme, 6.0 * share, f"{int(round(total))} cards in the deck support it")
    tribe = None
    if tribe_votes:
        t, v = tribe_votes.most_common(1)[0]
        if v >= max(8, 0.25 * max(1, n)):
            tribe = t
            note("Tribal", 2.0 + min(4.0, v / 6), f"{t}s: the commander or many deck cards care about them")
    ranked = [t for t, s in scores.most_common() if s >= 1.5]
    primary = ranked[:2]
    secondary = ranked[2:5]
    return dict(primary=primary, secondary=secondary, scores=dict(scores), reasons=reasons, tribe=tribe)


PLURAL = {"Elf": "elves", "Wolf": "wolves", "Dwarf": "dwarves", "Werewolf": "werewolves", "Fish": "fish",
          "Mouse": "mice", "Octopus": "octopuses", "Sphinx": "sphinxes", "Ally": "allies", "Faerie": "faeries"}


def cares_about_type(text, t):
    """Does this (lower-case) text care about a creature type ('Goblins you control',
    'Elf spells', 'number of Zombies') - not just make a token of it?"""
    one, many = t.lower(), PLURAL.get(t, t.lower() + "s")
    return bool(re.search(rf"\b(?:other |each |another )?(?:{one}|{many}) you control|\b{one} spells?\b|"
                          rf"number of {many}|\bother {many}\b|whenever (?:a|another) {one}\b|"
                          rf"\beach {one}\b|\b{one} creatures? you control", text))


def _cue_words(cue):
    return re.sub(r"\\b|\\|\?:|\(|\)|\?|s\?", "", cue).replace("(", "").replace(")", "").strip() or cue
