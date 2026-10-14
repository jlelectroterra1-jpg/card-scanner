"""Realistic Commander decklists for checking the analyser's judgement (not toy decks).

Each deck has real staples, engines, combo/synergy pieces and a few pet / unusual cards
(which must NOT be cut), plus some genuinely weak filler and one off-colour card (which
may be). MUST_KEEP lists cards a sensible human would never cut.

    python tests/real_decks.py            # print the analysis of every deck
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

DECKS = {
    "Atraxa Superfriends": dict(
        commander="Atraxa, Praetors' Voice",
        cards="""Sol Ring; Arcane Signet; Fellwar Stone; Chromatic Lantern; Smothering Tithe; Rhystic Study;
Cyclonic Rift; Teferi's Protection; Swords to Plowshares; Path to Exile; Anguished Unmaking; Assassin's Trophy;
Counterspell; Swan Song; Doubling Season; Deepglow Skate; Hardened Scales; Evolution Sage; Flux Channeler;
Inexorable Tide; Contagion Engine; Vorinclex, Monstrous Raider; Teferi, Hero of Dominaria; Oko, Thief of Crowns;
Tamiyo, Field Researcher; Vraska, Golgari Queen; Nissa, Who Shakes the World; Narset, Parter of Veils;
The Chain Veil; Oath of Teferi; Pir, Imaginative Rascal; Kalonian Hydra; Farseek; Nature's Lore; Three Visits;
Kodama's Reach; Cultivate; Demonic Tutor; Vampiric Tutor; Toxic Deluge; Supreme Verdict; Heroic Intervention;
Guardian Project; Tezzeret's Gambit; Grateful Apparition; Thrummingbird; Karn's Bastion; Ezuri's Predation;
Colossal Dreadmaw; Ulamog's Crusher; Divination; Krosan Tusker; Lightning Bolt""",
        lands="Command Tower; Exotic Orchard; Breeding Pool; Hallowed Fountain; Godless Shrine; Overgrown Tomb; "
              "Watery Grave; Temple Garden; Zagoth Triome; Raffine's Tower; 6 Forest; 5 Island; 4 Plains; 4 Swamp",
        must_keep=["Doubling Season", "Deepglow Skate", "Smothering Tithe", "Rhystic Study", "Cyclonic Rift",
                   "Teferi's Protection", "Hardened Scales", "Evolution Sage", "Flux Channeler", "Oath of Teferi",
                   "The Chain Veil", "Vorinclex, Monstrous Raider", "Inexorable Tide", "Contagion Engine",
                   "Demonic Tutor", "Sol Ring", "Teferi, Hero of Dominaria", "Narset, Parter of Veils",
                   "Swords to Plowshares", "Counterspell", "Pir, Imaginative Rascal"]),
    "Krenko Goblins": dict(
        commander="Krenko, Mob Boss",
        cards="""Goblin Chieftain; Goblin King; Goblin Warchief; Skirk Prospector; Goblin Bombardment; Impact Tremors;
Purphoros, God of the Forge; Siege-Gang Commander; Beetleback Chief; Goblin Matron; Goblin Recruiter;
Muxus, Goblin Grandee; Conspicuous Snoop; Legion Warboss; Goblin Rabblemaster; Goblin Instigator;
Mogg War Marshal; Krenko, Tin Street Kingpin; Battle Hymn; Shared Animosity; Coat of Arms; Sol Ring; Arcane Signet;
Mind Stone; Thrill of Possibility; Faithless Looting; Chaos Warp; Blasphemous Act; Lightning Bolt; Vandalblast;
Goblin Sharpshooter; Gempalm Incinerator; Pashalik Mons; Lightning Greaves; Swiftfoot Boots; Skullclamp;
Goblin Ringleader; Boggart Harbinger; Goblin Piledriver; Ib Halfheart, Goblin Tactician; Sling-Gang Lieutenant;
Wort, Boggart Auntie; Goblin Lackey; Mogg Fanatic; Reckless Bushwhacker; Hellrider; Fervor; Fireshrieker;
Ruby Medallion; Jeska's Will; Wheel of Misfortune; Hill Giant; Gray Ogre; Counterspell; Craw Wurm""",
        lands="Castle Embereth; Den of the Bugbear; Valakut, the Molten Pinnacle; Rogue's Passage; Reliquary Tower; 32 Mountain",
        must_keep=["Goblin Bombardment", "Impact Tremors", "Purphoros, God of the Forge", "Skirk Prospector",
                   "Goblin Chieftain", "Goblin King", "Goblin Warchief", "Siege-Gang Commander", "Skullclamp",
                   "Shared Animosity", "Coat of Arms", "Muxus, Goblin Grandee", "Krenko, Tin Street Kingpin",
                   "Goblin Matron", "Goblin Recruiter", "Sol Ring", "Lightning Greaves", "Pashalik Mons"]),
    "Meren Aristocrats": dict(
        commander="Meren of Clan Nel Toth",
        cards="""Spore Frog; Eternal Witness; Viscera Seer; Carrion Feeder; Blood Artist; Zulaport Cutthroat; Grave Pact;
Dictate of Erebos; Sakura-Tribe Elder; Satyr Wayfinder; Fleshbag Marauder; Plaguecrafter; Archon of Cruelty;
Gray Merchant of Asphodel; Shriekmaw; Ravenous Chupacabra; Sylvan Library; Phyrexian Altar; Ashnod's Altar;
Pernicious Deed; Living Death; Animate Dead; Reanimate; Buried Alive; Entomb; Demonic Tutor; Sol Ring;
Birds of Paradise; Llanowar Elves; Cultivate; Kodama's Reach; Rampant Growth; Dread Return; Victimize;
Golgari Grave-Troll; Stinkweed Imp; Grisly Salvage; Liliana, Death's Majesty; The Gitrog Monster;
Mazirek, Kraul Death Priest; Woodland Bellower; Fierce Empath; Ramunap Excavator; Deathrite Shaman;
Polluted Bonds; Skullclamp; Beast Within; Assassin's Trophy; Sidisi, Brood Tyrant; Night's Whisper;
Grizzly Bears; Divination""",
        lands="Command Tower; Overgrown Tomb; Woodland Cemetery; Llanowar Wastes; Golgari Rot Farm; Bojuka Bog; "
              "Phyrexian Tower; High Market; Evolving Wilds; 13 Forest; 12 Swamp",
        must_keep=["Archon of Cruelty", "Spore Frog", "Viscera Seer", "Carrion Feeder", "Blood Artist",
                   "Zulaport Cutthroat", "Grave Pact", "Dictate of Erebos", "Eternal Witness", "Phyrexian Altar",
                   "Ashnod's Altar", "Sylvan Library", "Living Death", "Gray Merchant of Asphodel",
                   "The Gitrog Monster", "Demonic Tutor", "Skullclamp", "Fleshbag Marauder", "Woodland Bellower",
                   "Ramunap Excavator"]),
    "Talrand Spells": dict(
        commander="Talrand, Sky Summoner",
        cards="""Archmage Emeritus; Murmuring Mystic; Ponder; Preordain; Brainstorm; Opt; Consider; Counterspell;
Arcane Denial; Mana Drain; Swan Song; Negate; Cyclonic Rift; Rhystic Study; Mystic Remora; Sol Ring; Arcane Signet;
Mind Stone; Thought Vessel; Reality Shift; Pongify; Rapid Hybridization; Snap; Frantic Search; Fact or Fiction;
Mystic Confluence; Pull from Tomorrow; Blue Sun's Zenith; Commit // Memory; Dig Through Time; Treasure Cruise;
Mizzix's Mastery; Galvanic Iteration; Reverberate; Ghostly Flicker; Narset's Reversal; Delay; Mana Leak;
Into the Roil; Capsize; Aetherflux Reservoir; Sapphire Medallion; Baral, Chief of Compliance;
Young Pyromancer; Saheeli's Directive; Giant Octopus; Air Elemental; Coral Eel""",
        lands="Mystic Sanctuary; Reliquary Tower; Halimar Depths; Cavern of Souls; 32 Island",
        must_keep=["Archmage Emeritus", "Murmuring Mystic", "Rhystic Study", "Mystic Remora", "Cyclonic Rift",
                   "Mana Drain", "Counterspell", "Sol Ring", "Ponder", "Brainstorm", "Aetherflux Reservoir",
                   "Baral, Chief of Compliance", "Mystic Confluence", "Ghostly Flicker"]),
}


def decklist(deck):
    cards = [c.strip() for c in deck["cards"].replace("\n", " ").split(";") if c.strip()]
    lines = ["Commander", f"1 {deck['commander']}", "Deck"] + [f"1 {c}" for c in cards]
    for part in deck["lands"].split(";"):
        part = part.strip()
        n, _, name = part.partition(" ")
        lines.append(part if n.isdigit() else f"1 {part}")
    return "\n".join(lines)


def build(udb, store, name):
    deck = DECKS[name]
    deck_id = store.create_deck(name)
    parsed = store.parse_decklist(decklist(deck))
    missing = [r["name"] for r in parsed if not r["card"]]
    store.commit_import(deck_id, parsed)
    return deck_id, missing


def main():
    import analyser
    import userdb
    from decks import DeckStore
    from userdb import UserDB
    tmp = tempfile.mkdtemp(prefix="tmp_realdecks_", dir=HERE)
    try:
        userdb.BACKUP_DIR = os.path.join(tmp, "b")
        udb = UserDB(os.path.join(tmp, "u.db"))
        st = DeckStore(udb)
        for name, deck in DECKS.items():
            deck_id, missing = build(udb, st, name)
            n = st.summary(deck_id)["count"]
            for mode, budget in (("all", None),):
                res = analyser.analyse(udb, st, deck_id, dict(mode=mode, goal="improve", tags=None, budget_usd=budget))
                print(f"\n=== {name} ({n} cards{', MISSING ' + str(missing) if missing else ''}) - {mode} cards ===")
                print(f"strategy: {res['detected']['primary']} / {res['detected']['secondary']} "
                      f"{'UNCERTAIN: ' + res['strategy_note'] if res['strategy_uncertain'] else ''}  scores {res['detected']['scores']}")
                for s in res["swaps"]:
                    print(f"  SWAP [{s['confidence']}] cut {s['cut_confidence']:.0%} / repl {s['replacement_confidence']:.0%}: "
                          f"{s['out']} -> {s['into']}")
                    print(f"       why cut: {' | '.join(s['why_out'])}")
                    print(f"       why add: {' | '.join(s['why_in'])}")
                if not res["swaps"]:
                    print("  No high-confidence swaps found.")
                print("  worth considering:", ", ".join(c["name"] for c in res["considered"]))
                outs = {s["out"] for s in res["swaps"]}
                bad = [c for c in deck["must_keep"] if c in outs]
                print("  MUST-KEEP CARDS SUGGESTED AS CUTS:", bad or "none")
                elig = [v["name"] for v in res["cut_review"] if v["status"].startswith(("possible", "suggested"))]
                print("  cut candidates:", elig)
        udb.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
