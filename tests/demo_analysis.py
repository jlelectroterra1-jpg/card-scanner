"""Run the analyser on a realistic Atraxa counters deck with a small collection (temporary database).
    python tests/demo_analysis.py [mode] [goal] [budget_zar]"""
import os, shutil, sys, tempfile, time
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, os.path.dirname(HERE))
import userdb, analyser
from userdb import UserDB
from decks import DeckStore

DECK = ["Sol Ring", "Arcane Signet", "Burnished Hart", "Solemn Simulacrum", "Hardened Scales", "Doubling Season",
        "Evolution Sage", "Flux Channeler", "Karn's Bastion", "Swords to Plowshares", "Counterspell", "Wrath of God",
        "Inspiring Call", "Rishkar, Peema Renegade", "Grateful Apparition", "Contagion Clasp", "Thrummingbird",
        "Ezuri's Predation", "Gilded Lotus", "Darksteel Ingot", "Mind Stone", "Hedron Archive", "Thran Dynamo",
        "Worn Powerstone", "Commander's Sphere", "Gruul Signet", "Pelakka Wurm", "Ulamog's Crusher", "Colossal Dreadmaw",
        "Krosan Tusker", "Cultivate", "Kodama's Reach", "Farseek"]
OWNED = ["Rhystic Study", "Beast Whisperer", "Guardian Project", "Inexorable Tide", "Pir, Imaginative Rascal",
         "Toxic Deluge", "Path to Exile", "Heroic Intervention", "Fathom Mage", "Bred for the Hunt", "Three Visits",
         "Nature's Lore", "Lightning Bolt", "Smothering Tithe", "Kami of Whispered Hopes"]


def build(d):
    userdb.BACKUP_DIR = os.path.join(d, "b")
    db = UserDB(os.path.join(d, "user.db"))
    st = DeckStore(db)
    deck = st.create_deck("Atraxa Counters")
    lines = ["Commander", "1 Atraxa, Praetors' Voice", "Deck"] + [f"1 {n}" for n in DECK] + \
        ["8 Forest", "8 Plains", "8 Island", "5 Swamp", "1 Command Tower", "1 Exotic Orchard"]
    st.commit_import(deck, st.parse_decklist("\n".join(lines)))
    for n in OWNED:
        c = st.default_printing(n)
        db.add_card(c["id"], c["name"], c["set_code"], c["collector_number"])
    other = st.create_deck("Other Deck")
    st.add_card(other, st.default_printing("Rhystic Study"))
    return db, st, deck


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "collection"
    goal = sys.argv[2] if len(sys.argv) > 2 else "improve"
    budget = float(sys.argv[3]) / 17.25 if len(sys.argv) > 3 else None
    d = tempfile.mkdtemp(prefix="tmp_demo_", dir=HERE)
    try:
        db, st, deck = build(d)
        res = analyser.analyse(db, st, deck, dict(mode=mode, goal=goal, tags=None, budget_usd=budget))
        print(f"{mode}/{goal} budget={budget}: {res['seconds']}s, cost ${res['cost']}, {res['candidates_considered']} candidates")
        for s in res["swaps"]:
            print(f"  {s['out']:24} -> {s['into']:30} {s['ownership']['status']:10} ${s['ownership']['cost'] or 0:6.2f}  {s['confidence']}")
            print("        " + " | ".join(s["why_in"][:2]))
        db.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)
