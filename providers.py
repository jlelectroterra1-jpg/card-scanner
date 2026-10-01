"""Optional outside data for the analyser. The analyser works fully WITHOUT any of these.

AnalysisProvider is the interface; a provider only adds extra information (e.g. combos).
It never decides how good a card is. Providers are off unless the user turns them on.

- CommanderSpellbookProvider: combos in your deck / one card away, via Commander
  Spellbook's documented public API (https://backend.commanderspellbook.com/schema/swagger/).
  Their guidance: unauthenticated, a few calls per user action, ~80/min max, name your app
  in the User-Agent, credit and link back to https://commanderspellbook.com. We make ONE
  request per "Check combos" press and cache the answer.
- Community deck statistics (e.g. Archidekt): not implemented - no documented API; any
  future provider must stay optional and must not crawl.
- EDHREC: never automated. edhrec_url() only builds a link to open in your own browser."""
import re
import unicodedata

import requests

USER_AGENT = "HomeCardScanner/0.4 (personal Commander deck analyser; offline-first)"


class AnalysisProvider:
    """Base class for optional data sources."""
    name = "provider"
    description = ""
    online = False

    def combos(self, commanders, cards):
        """Return [dict(name, cards=[...], missing=[...], produces=[...], url, status)] where
        status is 'in_deck' (all pieces in the deck) or 'one_away' (one card missing)."""
        return []


class CommanderSpellbookProvider(AnalysisProvider):
    name = "Commander Spellbook"
    description = "Combo data from commanderspellbook.com (one request per check)"
    online = True
    URL = "https://backend.commanderspellbook.com/find-my-combos"

    def __init__(self, session=None, timeout=25):
        self.session = session or requests.Session()
        self.timeout = timeout
        self._cache = {}

    def combos(self, commanders, cards):
        key = (tuple(sorted(commanders)), tuple(sorted(cards)))
        if key in self._cache:
            return self._cache[key]
        body = {"commanders": [{"card": c} for c in commanders], "main": [{"card": c} for c in cards]}
        r = self.session.post(self.URL, json=body, timeout=self.timeout,
                              headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        if r.status_code == 429:
            raise RuntimeError("Commander Spellbook is busy - try again in a minute")
        r.raise_for_status()
        res = r.json().get("results", {})
        deck = {c.lower() for c in list(commanders) + list(cards)}
        out = []
        for status, key_name in (("in_deck", "included"), ("one_away", "almostIncluded")):
            for v in res.get(key_name, [])[:25]:
                pieces = [u["card"]["name"] for u in v.get("uses", []) if u.get("card")]
                missing = [p for p in pieces if p.lower() not in deck]
                out.append(dict(
                    name=" + ".join(pieces), cards=pieces, missing=missing, status=status,
                    produces=[p.get("feature", {}).get("name") for p in v.get("produces", []) if p.get("feature")][:3],
                    url=f"https://commanderspellbook.com/combo/{v.get('id')}/", source=self.name))
        self._cache[key] = out
        return out


def slug(name):
    t = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9\s-]", "", t.split(" // ")[0])
    return re.sub(r"[\s-]+", "-", t).strip("-")


def edhrec_url(commanders):
    """Link to the commander's EDHREC page, for opening in the user's own browser (manual
    reference only - the app never contacts EDHREC)."""
    names = sorted(slug(c) for c in commanders if c)
    return "https://edhrec.com/commanders/" + "-".join(names) if names else "https://edhrec.com/"
