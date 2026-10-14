"""Print the Consider recommendations (needs, tiers, groups, reasons) for the realistic decks
and the archetype decks - for checking the recommender's judgement by eye.

    python tests/eval_recommend.py               # real decks, All Cards
    python tests/eval_recommend.py archetypes    # the 11 archetype decks
    python tests/eval_recommend.py why "Meren Aristocrats" "Grizzly Bears"
"""
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import analyser  # noqa: E402
import real_decks  # noqa: E402
import recommend  # noqa: E402
import userdb  # noqa: E402
from decks import DeckStore  # noqa: E402
from userdb import UserDB  # noqa: E402

SET = dict(mode="all", goal="improve", tags=None, budget_usd=None)


def show(name, res, full=False):
    rec = res["recommendations"]
    print(f"\n=== {name}: {res['detected']['primary']} / {res['detected']['secondary']}  "
          f"plan: {res['commander_profile']['primary']}  ({res['seconds']} s)")
    print("  needs:", "; ".join(f"{n['label']} ({n['have']}/{n['enough']}, {n['severity']:.2f})" for n in rec["needs"]))
    print(f"  swaps: {[(s['out'], s['into']) for s in res['swaps']]}")
    print(f"  consider: {rec['shown']} shown, {len(res['considered']) - rec['shown']} hidden")
    by = {c["name"]: c for c in res["considered"]}
    for g in rec["groups"]:
        print(f"  [{g['category']}]  {g['need']}")
        for n in g["names"][:None if full else 4]:
            c = by[n]
            print(f"     {c['tier_label']:<27} {n}  ({c['ownership']['status']}, {c['signals']})")
            for line in c["why"][:4]:
                print("          -", line)
        if not full and len(g["names"]) > 4:
            print(f"     ... +{len(g['names']) - 4} more")


def inject_ranks():
    """RANKS_JSON={name: edhrec_rank} (e.g. from Scryfall's oracle bulk file) to try the
    recommender with popularity data before Update Prices has stored it in cards.db."""
    path = os.environ.get("RANKS_JSON")
    if not path:
        return
    import json
    ranks = json.load(open(path, encoding="utf-8"))
    orig = analyser.CardData.__init__

    def init(self, *a, **k):
        orig(self, *a, **k)
        self.popularity = ranks
    analyser.CardData.__init__ = init


def main():
    inject_ranks()
    tmp = tempfile.mkdtemp(prefix="tmp_evalrec_", dir=HERE)
    try:
        userdb.BACKUP_DIR = os.path.join(tmp, "b")
        udb = UserDB(os.path.join(tmp, "u.db"))
        st = DeckStore(udb)
        if len(sys.argv) > 1 and sys.argv[1] == "why":
            deck, _ = real_decks.build(udb, st, sys.argv[2])
            print(recommend.why_not(udb, st, deck, SET, sys.argv[3]))
            return
        if len(sys.argv) > 1 and sys.argv[1] == "archetypes":
            from test_analyser_understanding import ARCHETYPES, FILLER
            for arch, (cmd, plan, key, _, _) in ARCHETYPES.items():
                deck = st.create_deck(arch)
                text = f"Commander\n1 {cmd}\nDeck\n" + "\n".join("1 " + c for c in plan + [key] + FILLER) + "\n30 Wastes"
                st.commit_import(deck, st.parse_decklist(text))
                t = time.time()
                show(arch, analyser.analyse(udb, st, deck, SET))
            return
        for name in real_decks.DECKS:
            deck, _ = real_decks.build(udb, st, name)
            show(name, analyser.analyse(udb, st, deck, SET))
        udb.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
