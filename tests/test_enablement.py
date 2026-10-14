"""Payoff enablement and interaction QUALITY - a card's main ability needs something to work with,
and many incidental links must not outweigh a few real ones.

    python -m unittest tests.test_enablement -v
"""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import analyser  # noqa: E402
import cardroles  # noqa: E402
import community  # noqa: E402
import cutfinder  # noqa: E402
import enablement  # noqa: E402
import recommend  # noqa: E402
import synergy  # noqa: E402
import userdb  # noqa: E402
from decks import DeckStore  # noqa: E402
from test_analyser_understanding import FILLER, has_cards, oracle  # noqa: E402
from userdb import UserDB  # noqa: E402

TOKENS = ["Secure the Wastes", "Raise the Alarm", "Spectral Procession", "Lingering Souls", "Bitterblossom",
          "Increasing Devotion", "Martial Coup", "Assemble the Legion", "Intangible Virtue", "Anointed Procession",
          "Divine Visitation", "Elspeth, Sun's Champion", "Captain of the Watch", "Cathars' Crusade"]
COUNTERS = ["Hardened Scales", "Evolution Sage", "Flux Channeler", "Inexorable Tide", "Experiment One",
            "Champion of Lambholt", "Kalonian Hydra", "Corpsejack Menace", "Gyre Sage", "Master Biomancer",
            "Vorinclex, Monstrous Raider", "Deepglow Skate", "Walking Ballista", "Hangarback Walker",
            "Avenger of Zendikar", "Ajani, Mentor of Heroes", "Tamiyo, Field Researcher", "Steady Progress",
            "Grateful Apparition", "Thrummingbird", "Pir, Imaginative Rascal", "Bloom Tender"]


def support_for(names, commander=None, tribe=None):
    cards = {}
    for n in names + ([commander] if commander else []):
        o = oracle(n)
        cards[n] = (o, cardroles.classify(o))
    return enablement.DeckSupport(cards, [commander] if commander else [], tribe)


@unittest.skipUnless(has_cards(), "needs cards.db with oracle_cards")
class SupportTests(unittest.TestCase):
    def evangel(self, deck):
        o = oracle("Metastatic Evangel")
        return deck.evaluate(o, cardroles.classify(o))

    def test_proliferate_without_counters_is_low_support(self):
        ev = self.evangel(support_for(TOKENS + FILLER))
        self.assertLess(ev["enabled"], enablement.UNSUPPORTED)
        self.assertIn("proliferate", ev["unsupported"])
        self.assertIn("little to work with", ev["text"])

    def test_proliferate_in_a_counters_deck_is_high_support(self):
        ev = self.evangel(support_for(COUNTERS + FILLER, commander="Atraxa, Praetors' Voice"))
        self.assertGreaterEqual(ev["enabled"], 0.6)
        self.assertEqual(ev["unsupported"], [])
        low = self.evangel(support_for(TOKENS + FILLER))
        self.assertGreater(ev["enabled"], low["enabled"] + 0.3)

    def test_other_payoffs_need_their_enablers(self):
        no_gear = support_for(TOKENS)
        gear = support_for(TOKENS[:6] + ["Swiftfoot Boots", "Lightning Greaves", "Sword of Fire and Ice",
                                         "Bonesplitter", "Skullclamp", "Loxodon Warhammer", "Shadowspear"])
        puresteel = oracle("Puresteel Paladin")
        self.assertLess(no_gear.evaluate(puresteel)["enabled"], gear.evaluate(puresteel)["enabled"])
        self.assertEqual(support_for(TOKENS).evaluate(oracle("Sol Ring"))["enabled"], None)  # needs nothing

    def test_nontoken_payoffs_are_not_supported_by_token_makers(self):
        prov, rew, gen = synergy.resources(oracle("Metastatic Evangel"))
        self.assertIn("nontoken creatures entering", rew)
        self.assertNotIn("creatures entering", rew)
        cards = {n: (oracle(n), cardroles.classify(oracle(n))) for n in TOKENS}
        g = synergy.InteractionGraph(cards)
        o = oracle("Metastatic Evangel")
        links = g.candidate_links(o, cardroles.classify(o))
        self.assertFalse([l for l in links if l[2] == "strong" and "puts creatures onto the battlefield" in l[1]])


@unittest.skipUnless(has_cards(), "needs cards.db with oracle_cards")
class InteractionQualityTests(unittest.TestCase):
    def test_incidental_links_cannot_outweigh_direct_ones(self):
        incidental = [(f"c{i}", "x", "moderate") for i in range(30)]
        direct = [(f"d{i}", "x", "strong") for i in range(6)]
        self.assertLess(synergy.link_value(incidental), synergy.link_value(direct))
        self.assertEqual(synergy.link_value([("w", "x", "weak")] * 40), 0)

    def test_tribe_alone_does_not_create_dozens_of_strong_links(self):
        humans = ["Thalia's Lieutenant" if False else "Champion of the Parish", "Kitesail Freebooter",
                  "Thraben Inspector", "Charming Prince", "Esper Sentinel", "Stoneforge Mystic", "Adeline, Resplendent Cathar",
                  "Mentor of the Meek", "Thalia, Guardian of Thraben", "Recruiter of the Guard"]
        cards = {n: (oracle(n), cardroles.classify(oracle(n))) for n in humans}
        g = synergy.InteractionGraph(cards, tribe="Human")
        plain = oracle("Mother of Runes")  # a Human that says nothing about Humans
        strong = [l for l in g.candidate_links(plain, cardroles.classify(plain)) if l[2] == "strong"]
        self.assertLessEqual(len(strong), 3)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tmp_enable_", dir=HERE)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        old = userdb.BACKUP_DIR
        userdb.BACKUP_DIR = os.path.join(self.tmp, "b")
        self.addCleanup(setattr, userdb, "BACKUP_DIR", old)
        old_cache = community.CACHE_PATH
        community.CACHE_PATH = os.path.join(self.tmp, "c.db")
        self.addCleanup(setattr, community, "CACHE_PATH", old_cache)
        self.db = UserDB(os.path.join(self.tmp, "u.db"))
        self.addCleanup(self.db.close)
        self.st = DeckStore(self.db)

    def deck(self, cmd, cards, basics=30):
        d = self.st.create_deck("t")
        parsed = self.st.parse_decklist(f"Commander\n1 {cmd}\nDeck\n" + "\n".join("1 " + c for c in cards) +
                                        f"\n{basics} Wastes")
        self.assertEqual([r["name"] for r in parsed if not r["card"]], [])
        self.st.commit_import(d, parsed)
        return d


@unittest.skipUnless(has_cards(), "needs cards.db with oracle_cards")
class SwapScoringTests(Base):
    SET = dict(mode="all", goal="high", tags=None, budget_usd=None)

    def test_supported_payoff_scores_higher_than_unsupported(self):
        tokens = self.deck("Rhys the Redeemed", TOKENS + ["Wrath of God", "Day of Judgment", "Hour of Revelation"] + FILLER)
        counters = self.deck("Atraxa, Praetors' Voice", COUNTERS + ["Wrath of God", "Supreme Verdict"] + FILLER)
        cf_t, _ = cutfinder.finder_for(self.db, self.st, tokens, self.SET)
        cf_c, _ = cutfinder.finder_for(self.db, self.st, counters, self.SET)
        self.assertGreater(cf_t.facts("Metastatic Evangel")["unsupported"], 0.5)
        self.assertLess(cf_c.facts("Metastatic Evangel")["unsupported"], 0.4)
        d_t = cf_t.delta("Metastatic Evangel", "Day of Judgment")
        d_c = cf_c.delta("Metastatic Evangel", "Supreme Verdict")
        self.assertLess(d_t["parts"]["enablement"], -0.1)  # unsupported payoff: penalty...
        self.assertTrue(any("little to work with" in l for l in d_t["losses"]))  # ...said out loud
        self.assertGreater(d_c["parts"]["enablement"], d_t["parts"]["enablement"])
        # and in Consider: the recommender says why, and scores it lower
        st_t = analyser.prepare(self.db, self.st, tokens, self.SET)
        ev = recommend.Recommender(st_t, self.SET).why_not("Metastatic Evangel")
        if ev["recommended"]:
            self.assertTrue(any("little to work with" in w for w in ev["why"]))

    def test_role_changing_swap_needs_more_evidence_than_like_for_like(self):
        d = self.deck("Rhys the Redeemed", TOKENS + ["Wrath of God", "Swords to Plowshares", "Path to Exile",
                                                     "Generous Gift", "Day of Judgment"] + FILLER)
        cf, _ = cutfinder.finder_for(self.db, self.st, d, self.SET)
        change = cf.delta("Welcoming Vampire", "Swords to Plowshares")  # removal -> draw creature
        same = cf.delta("Anguished Unmaking", "Swords to Plowshares")  # removal -> removal
        self.assertLess(change["parts"].get("role_change", 0), 0)
        self.assertEqual(same["parts"].get("role_change", 0), 0)

    def test_trimming_an_over_represented_role_is_still_valid(self):
        wipes = ["Wrath of God", "Day of Judgment", "Supreme Verdict", "Hour of Revelation", "Doomskar"]
        d = self.deck("Rhys the Redeemed", TOKENS + wipes + FILLER)
        cf, _ = cutfinder.finder_for(self.db, self.st, d, self.SET)
        self.assertGreater(cf.deck.roles["Board Wipe"], cf.role_targets["Board Wipe"][1])
        dd = cf.delta("Welcoming Vampire", "Doomskar")
        self.assertEqual(dd["parts"].get("role_change"), -0.03)  # smaller hurdle than a needed job
        r = cf.find("Mirkwood Bats" if False else "Welcoming Vampire")
        self.assertTrue(any(o["out"] in wipes for o in r["options"]), [o["out"] for o in r["options"]])

    def test_many_weak_interactions_do_not_beat_fewer_strong_ones(self):
        d = self.deck("Rhys the Redeemed", TOKENS + FILLER)
        cf, _ = cutfinder.finder_for(self.db, self.st, d, self.SET)
        w_evangel, s_evangel, n_evangel, _ = cf.link_weight("Metastatic Evangel")
        w_virtue, s_virtue, n_virtue, _ = cf.link_weight("Anointed Procession")
        self.assertGreater(w_virtue, w_evangel)  # a real token payoff beats a pile of incidental links


if __name__ == "__main__":
    unittest.main()
