"""Payoff enablement: does THIS deck give a card's main ability something to work with?

Many cards are good only when the deck supplies what they pay off: proliferate needs counters
worth growing, "whenever you gain life" needs life gain, "equipped creature" needs Equipment,
magecraft needs instants and sorceries, a tribal lord needs the tribe... A card whose payoff
has nothing to work with is mostly dead text here, however many technical interactions it has.

DeckSupport(cards, commanders, tribe).evaluate(info) ->
    dict(requirements=[dict(label, need, have, enough, ratio)], enabled=0..1 (None = the card
         needs nothing special), unsupported=[labels], text=plain-English summary)

Support is counted from what the deck's cards SPECIFICALLY do (a token maker, a counter
source, an Equipment), never from merely being a permanent or a creature."""
import re

import cardroles
import synergy

_C = lambda s: re.compile(s)  # noqa: E731
# counters worth growing with proliferate / that counters payoffs care about
COUNTER_SOURCE = _C(r"put (?:a|an|one|two|three|x|that many|\w+) [+-]?\d*/?[+-]?\d* ?(?:\+1/\+1|loyalty|charge|quest|"
                    r"lore|oil|experience|poison|energy|muster|time|\w+) counters?|enters with [^.]*counters?|"
                    r"\bget(?:s)? (?:an? |\w+ )?(?:experience|poison|energy) counters?|\bmuster\b|\binfect\b|"
                    r"\btoxic\b|\bpoisonous\b|\bamass\b|\bmodular\b|\bgraft\b|\bevolve\b|\badapt\b|\boutlast\b|"
                    r"\bbolster\b|\bsupport \d|\bmonstrosity\b|\btraining\b|\bbackup\b")

DISTRIBUTE = _C(r"distribute (?:a|an|one|two|three|four|five|x|\w+) (?:\+1/\+1 )?counters?")
AMPLIFIER = _C(r"\bproliferate\b|twice that many [^.]*counters|that many plus (?:one|two)|double the number of "
               r"(?:each kind of )?counters?")

# (label, card-text pattern for the payoff, what supports it, how many make it reliable)
REQUIREMENTS = [
    ("proliferate", _C(r"\bproliferate\b"), "counters", 20),  # counters decks run 15-25 sources
    ("+1/+1 counter payoff", _C(r"(?:with|has|have) (?:a|one or more) \+1/\+1 counters?|for each \+1/\+1 counter|"
                                r"\+1/\+1 counters? (?:would be|is|are) put|double the number of [^.]*counters"),
     "counters", 12),
    ("life gain payoff", _C(r"whenever you gain life|if you would gain life|for each 1 life you gained|"
                            r"you(?:'ve)? gained (?:\d+ or more )?life this turn"), "life gain", 7),
    ("Equipment payoff", _C(r"equipped creatures? you control|for each equipment|equipment you control|"
                            r"whenever [^.]*becomes equipped|equip abilities"), "equipment", 6),
    ("Aura payoff", _C(r"enchanted creatures? you control|for each aura|auras? you control|"
                       r"whenever [^.]*becomes enchanted"), "auras", 6),
    ("spells payoff", _C(r"whenever you cast (?:an|a) (?:instant|sorcery|noncreature)|\bmagecraft\b|\bprowess\b|"
                         r"instant (?:and|or) sorcery spells? you cast|whenever you copy"), "spells", 15),
    ("graveyard payoff", _C(r"cards? (?:leave|leaves) your graveyard|for each (?:creature )?card in your graveyard|"
                            r"\bthreshold\b|\bdelirium\b|cards? in your graveyard"), "graveyard", 6),
    ("token payoff", _C(r"whenever (?:you create|one or more [^.]*tokens? (?:you control )?enter|a (?:creature )?token)|"
                        r"tokens? you control (?:get|have|gain)|for each token|number of (?:creature )?tokens you "
                        r"control|twice that many (?:of those )?tokens"), "tokens", 8),
    ("death payoff", _C(r"whenever (?:a|another|one or more) (?:nontoken )?creatures? (?:you control )?dies|"
                        r"whenever you sacrifice"), "deaths", 8),
    ("artifact payoff", _C(r"whenever (?:an|another|one or more) (?:nontoken )?artifacts?|for each artifact|"
                           r"\baffinity for artifacts\b|\bimprovise\b|\bmetalcraft\b|whenever you cast an artifact"),
     "artifacts", 12),
    ("enchantment payoff", _C(r"\bconstellation\b|whenever (?:an|another) enchantment|for each enchantment|"
                              r"whenever you cast an? (?:aura|enchantment)"), "enchantments", 10),
    ("landfall", _C(r"\blandfall\b|whenever (?:a|one or more) lands? (?:you control )?enters?"), "landfall", 6),
    ("planeswalker payoff", _C(r"planeswalkers? you control|loyalty abilit"), "planeswalkers", 4),
]


class DeckSupport:
    """Counts what the deck supplies, from its non-land cards (commanders count double: they
    are always available)."""

    def __init__(self, cards, commanders=(), tribe=None):
        """cards: {name: (info, roles)} - the deck's non-land cards (incl. commanders)."""
        self.tribe = tribe
        self.have = {k: 0.0 for k in ("counters", "life gain", "equipment", "auras", "spells", "graveyard",
                                      "tokens", "deaths", "outlets", "artifacts", "enchantments", "landfall",
                                      "planeswalkers", "tribe")}
        for name, (info, roles) in cards.items():
            self.add(info, roles, 2.0 if name in commanders else 1.0)

    def add(self, info, roles, k=1.0):
        text = synergy._text(info)
        prov = synergy.resources(info, roles)[0]
        tl = info.get("type_line") or ""
        h = self.have
        if COUNTER_SOURCE.search(text) or DISTRIBUTE.search(text) or "Planeswalker" in tl:
            h["counters"] += k
        elif AMPLIFIER.search(text):  # proliferate / doublers grow counters others make
            h["counters"] += 0.5 * k
        h["life gain"] += k * ("life gain" in prov)
        h["equipment"] += k * ("Equipment" in tl)
        h["auras"] += k * ("Aura" in tl)
        h["spells"] += k * ("Instant" in tl or "Sorcery" in tl)
        h["graveyard"] += k * ("cards in the graveyard" in prov)
        h["tokens"] += k * ("tokens" in prov)
        h["outlets"] += k * ("creature deaths" in prov)
        h["deaths"] += k * ("sacrifice fodder" in prov or "Creature" in tl)
        h["artifacts"] += k * ("Artifact" in tl or "artifacts" in prov)
        h["enchantments"] += k * ("Enchantment" in tl)
        h["landfall"] += k * ("lands entering" in prov)
        h["planeswalkers"] += k * ("Planeswalker" in tl)
        if self.tribe and self.tribe in cardroles.creature_types(tl):
            h["tribe"] += k

    def _ratio(self, need, enough, own_info=None):
        h = self.have
        if need == "deaths":  # death payoffs need both a way to sacrifice AND bodies to lose
            outlets = min(1.0, h["outlets"] / 2)
            return min(1.0, h["deaths"] / enough) * (0.4 + 0.6 * outlets), h["deaths"], enough
        if need == "landfall":  # every land drop triggers it; extra land effects make it good
            return min(1.0, 0.5 + 0.5 * h["landfall"] / enough), h["landfall"], enough
        have = h[need]
        return min(1.0, have / enough), have, enough

    def evaluate(self, info, roles=None, exclude_self=True):
        text = synergy._text(info)
        reqs = []
        for label, pat, need, enough in REQUIREMENTS:
            if pat.search(text):
                ratio, have, enough = self._ratio(need, enough)
                reqs.append(dict(label=label, need=need, have=round(have, 1), enough=enough, ratio=round(ratio, 2)))
        if self.tribe and self.tribe.lower() in text and not text.startswith(f"{self.tribe.lower()} "):
            have = self.have["tribe"]
            reqs.append(dict(label=f"{self.tribe} payoff", need="tribe", have=have, enough=15,
                             ratio=round(min(1.0, have / 15), 2)))
        if not reqs:
            return dict(requirements=[], enabled=None, unsupported=[], text="")
        enabled = sum(r["ratio"] for r in reqs) / len(reqs)
        unsupported = [r for r in reqs if r["ratio"] < UNSUPPORTED]
        text_out = ""
        if unsupported:
            r = unsupported[0]
            text_out = (f"Its {r['label']} has little to work with here: the deck has {r['have']:g} "
                        f"{NEED_NOUN[r['need']]} (about {r['enough']} make it reliable)")
        elif enabled >= 0.8:
            r = reqs[0]
            text_out = f"Its {r['label']} is well supported: {r['have']:g} {NEED_NOUN[r['need']]} in the deck"
        return dict(requirements=reqs, enabled=round(enabled, 2), unsupported=[r["label"] for r in unsupported],
                    text=text_out)


UNSUPPORTED = 0.4  # below this share of the support it needs, a payoff counts as mostly dead text
NEED_NOUN = {"counters": "cards that put counters worth growing", "life gain": "life gain sources",
             "equipment": "Equipment", "auras": "Auras", "spells": "instants and sorceries",
             "graveyard": "cards that fill the graveyard", "tokens": "token makers",
             "deaths": "creatures/tokens to sacrifice", "artifacts": "artifacts", "enchantments": "enchantments",
             "landfall": "extra-land effects", "planeswalkers": "planeswalkers", "tribe": "creatures of the tribe"}


def payoff_share(info, roles, evaluation):
    """How much of the card's value is its payoff (vs other jobs it does regardless)."""
    if not evaluation["requirements"]:
        return 0.0
    other_jobs = [r for r, v in roles.items() if r in ("Ramp", "Card Draw", "Removal", "Board Wipe", "Counterspell",
                                                         "Protection", "Tutor", "Recursion", "Token Generation")
                  and v.confidence >= 0.6]
    return 1.0 if not other_jobs else 0.5
