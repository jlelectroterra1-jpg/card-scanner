"""Phase 3 tests: Commander decks - creation, commander/partner, cards, scanning into a
deck (with resume), duplicates, counts, ownership matching, allocation, finishing a scan,
warnings, value, import/export, and the questions Phase 4 will ask.

    python -m unittest tests.test_decks -v

Uses the real Scryfall cards.db (read-only) and temporary user databases.
"""
import csv
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import deckrules  # noqa: E402
import userdb  # noqa: E402
from currency import money  # noqa: E402
from decks import DeckStore  # noqa: E402
from deckscan import DeckScanTarget  # noqa: E402
from userdb import UserDB, UserDBError  # noqa: E402

CARDS_DB = os.path.join(ROOT, "data", "cards.db")


def needs_cards_db(cls):
    if not os.path.exists(CARDS_DB):
        return unittest.skip("no data/cards.db")(cls)
    con = sqlite3.connect(CARDS_DB)
    cols = {r[1] for r in con.execute("PRAGMA table_info(cards)")}
    con.close()
    if "color_identity" not in cols:
        return unittest.skip("cards.db is older than Phase 1 - run Update Prices")(cls)
    return cls


@needs_cards_db
class DeckBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tmp_decks_", dir=HERE)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        old = userdb.BACKUP_DIR
        userdb.BACKUP_DIR = os.path.join(self.tmp, "backups")
        self.addCleanup(setattr, userdb, "BACKUP_DIR", old)
        self.db = UserDB(os.path.join(self.tmp, "user.db"))
        self.addCleanup(self.db.close)
        self.st = DeckStore(self.db, CARDS_DB)

    def card(self, name):
        c = self.st.default_printing(name)
        self.assertIsNotNone(c, name)
        return c

    def other_printing(self, name):
        first = self.card(name)
        others = [p for p in self.st.printings(name) if p["id"] != first["id"] and p["lang"] == "en"]
        self.assertTrue(others, f"{name} needs a second printing for this test")
        return others[0]

    def own(self, card, finish="nonfoil", quantity=1, collection_id=None):
        return self.db.add_card(card["id"], card["name"], card["set_code"], card["collector_number"], finish=finish,
                                quantity=quantity, collection_id=collection_id)

    def deck_with_commander(self, name="Test Deck", commander="Atraxa, Praetors' Voice"):
        deck = self.st.create_deck(name)
        self.st.add_card(deck, self.card(commander), role="commander")
        return deck

    def row(self, deck, name):
        return next(r for r in self.st.deck_cards(deck) if r["card_name"] == name and r["role"] != "commander")


class DeckManagementTests(DeckBase):
    def test_create_rename_duplicate_delete(self):
        deck = self.st.create_deck("My Atraxa Deck")
        self.assertEqual(self.db.deck(deck)["format"], "commander")
        with self.assertRaises(ValueError):
            self.st.create_deck("  ")
        self.db.rename_deck(deck, "Atraxa Superfriends")
        self.assertEqual(self.db.deck(deck)["name"], "Atraxa Superfriends")
        self.st.add_card(deck, self.card("Atraxa, Praetors' Voice"), role="commander")
        self.st.add_card(deck, self.card("Sol Ring"))
        copy = self.st.duplicate(deck, "Atraxa v2")
        self.assertEqual(self.db.deck(copy)["commander"], "Atraxa, Praetors' Voice")
        self.assertEqual(self.db.deck(copy)["card_count"], 2)
        self.db.delete_deck(deck)
        self.assertIsNone(self.db.deck(deck))
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM deck_cards WHERE deck_id = ?", (deck,)).fetchone()[0], 0)
        self.assertIsNotNone(self.db.deck(copy))

    def test_commander_and_partner(self):
        deck = self.st.create_deck("Partners")
        self.st.add_card(deck, self.card("Thrasios, Triton Hero"), role="commander")
        self.st.add_card(deck, self.card("Tymna the Weaver"), role="partner")
        s = self.st.summary(deck)
        self.assertEqual([c["card_name"] for c in s["commanders"]], ["Thrasios, Triton Hero", "Tymna the Weaver"])
        self.assertEqual(s["identity"], "WUBG")  # G/U + W/B combined
        self.assertEqual(self.st.commander_identity(deck), "WUBG")
        # replacing the commander keeps one commander
        self.st.add_card(deck, self.card("Kraum, Ludevic's Opus"), role="commander")
        self.assertEqual(self.db.deck(deck)["commander"], "Kraum, Ludevic's Opus")
        # a main-deck card can be promoted; the old commander becomes a main-deck card
        sol = self.st.add_card(deck, self.card("Sol Ring"))
        self.db.set_role(sol, "commander")
        roles = {r["card_name"]: r["role"] for r in self.st.deck_cards(deck)}
        self.assertEqual(roles["Sol Ring"], "commander")
        self.assertEqual(roles["Kraum, Ludevic's Opus"], "main")
        warn_kinds = {w["kind"] for w in self.st.summary(deck)["warnings"]}
        self.assertIn("commander", warn_kinds)  # Sol Ring isn't legendary

    def test_add_edit_remove_cards(self):
        deck = self.deck_with_commander()
        sol = self.st.add_card(deck, self.card("Sol Ring"))
        isl = self.st.add_card(deck, self.card("Island"), quantity=3)
        self.st.add_card(deck, self.card("Island"), quantity=2)  # merges
        self.assertEqual(self.row(deck, "Island")["quantity"], 5)
        self.db.update_deck_card(isl, quantity=8)
        self.assertEqual(self.row(deck, "Island")["quantity"], 8)
        with self.assertRaises(ValueError):
            self.db.update_deck_card(isl, quantity=0)
        foilable = [p for p in self.st.printings("Sol Ring") if "foil" in (p["finishes"] or "")][0]
        self.st.change_printing(sol, foilable, "foil")
        r = self.row(deck, "Sol Ring")
        self.assertEqual((r["scryfall_id"], r["finish"]), (foilable["id"], "foil"))
        self.st.change_finish(sol, "nonfoil")
        self.assertEqual(self.row(deck, "Sol Ring")["finish"], "nonfoil")
        self.db.remove_deck_card(sol)
        self.assertNotIn("Sol Ring", [r["card_name"] for r in self.st.deck_cards(deck)])


class CountAndRulesTests(DeckBase):
    def test_deck_size_messages(self):
        deck = self.deck_with_commander()
        self.st.add_card(deck, self.card("Island"), quantity=98)
        self.st.add_card(deck, self.card("Sol Ring"))
        s = self.st.summary(deck)
        self.assertEqual(s["count"], 100)
        self.assertNotIn("count", {w["kind"] for w in s["warnings"]})
        self.st.add_card(deck, self.card("Arcane Signet"))
        s = self.st.summary(deck)
        self.assertEqual(s["count"], 101)
        self.assertIn("101 cards", next(w["text"] for w in s["warnings"] if w["kind"] == "count"))

    def test_colour_identity_and_legality_warnings(self):
        deck = self.st.create_deck("Esper")
        self.st.add_card(deck, self.card("Raffine, Scheming Seer"), role="commander")  # W/U/B
        self.st.add_card(deck, self.card("Lightning Bolt"))
        self.st.add_card(deck, self.card("Mana Crypt"))  # banned in Commander
        self.st.add_card(deck, self.card("Counterspell"))
        warns = self.st.summary(deck)["warnings"]
        colour = [w for w in warns if w["kind"] == "colour"]
        self.assertEqual([w["card"] for w in colour], ["Lightning Bolt"])
        self.assertIn("Red", colour[0]["text"])
        self.assertIn("White/Blue/Black", colour[0]["text"])
        self.assertEqual([w["card"] for w in warns if w["kind"] == "legality"], ["Mana Crypt"])
        # warnings only - nothing was removed
        self.assertEqual(len(self.st.deck_cards(deck)), 4)

    def test_duplicates_and_basic_lands(self):
        self.assertIsNone(deckrules.copy_limit("Island"))
        self.assertIsNone(deckrules.copy_limit("Snow-Covered Forest"))
        self.assertIsNone(deckrules.copy_limit("Relentless Rats"))
        self.assertEqual(deckrules.copy_limit("Seven Dwarves"), 7)
        self.assertEqual(deckrules.copy_limit("Sol Ring"), 1)
        deck = self.deck_with_commander()
        self.st.add_card(deck, self.card("Island"), quantity=12)
        self.st.add_card(deck, self.card("Sol Ring"), quantity=2)
        dup = [w for w in self.st.summary(deck)["warnings"] if w["kind"] == "duplicate"]
        self.assertEqual([w["card"] for w in dup], ["Sol Ring"])

    def test_grouping(self):
        deck = self.deck_with_commander()
        for n in ("Sol Ring", "Island", "Counterspell", "Swords to Plowshares", "Llanowar Elves", "Rhystic Study",
                  "Demonic Tutor", "Teferi, Hero of Dominaria", "Dryad Arbor"):
            self.st.add_card(deck, self.card(n))
        groups = {r["card_name"]: r["group"] for r in self.st.deck_cards(deck)}
        self.assertEqual(groups["Atraxa, Praetors' Voice"], "Commander")
        self.assertEqual(groups["Sol Ring"], "Artifact")
        self.assertEqual(groups["Island"], "Land")
        self.assertEqual(groups["Counterspell"], "Instant")
        self.assertEqual(groups["Demonic Tutor"], "Sorcery")
        self.assertEqual(groups["Rhystic Study"], "Enchantment")
        self.assertEqual(groups["Llanowar Elves"], "Creature")
        self.assertEqual(groups["Teferi, Hero of Dominaria"], "Planeswalker")
        self.assertEqual(groups["Dryad Arbor"], "Creature")  # creature land counts as a creature
        comp = self.st.composition(deck)
        self.assertEqual(comp["Commander"], 1)
        self.assertEqual(sum(comp.values()), 10)


class OwnershipTests(DeckBase):
    def test_exact_finish_and_printing_matches(self):
        sol, arcane, bolt = self.card("Sol Ring"), self.card("Arcane Signet"), self.card("Lightning Bolt")
        self.own(sol)                                   # exact printing
        self.own(self.other_printing("Arcane Signet"))  # another printing
        foil_bolt = [p for p in self.st.printings("Lightning Bolt") if "foil" in (p["finishes"] or "")
                     and "nonfoil" in (p["finishes"] or "")][0]
        self.own(foil_bolt, "foil")                     # same printing, other finish
        deck = self.deck_with_commander()
        self.st.add_card(deck, sol)
        self.st.add_card(deck, arcane)
        self.st.add_card(deck, foil_bolt, "nonfoil")
        self.st.add_card(deck, self.card("Rhystic Study"))
        own = {r["card_name"]: r["ownership"] for r in self.st.deck_cards(deck)}
        self.assertEqual(own["Sol Ring"], "exact")
        self.assertEqual(own["Arcane Signet"], "different_printing")
        self.assertEqual(own["Lightning Bolt"], "different_finish")
        self.assertEqual(own["Rhystic Study"], "missing")
        s = self.st.summary(deck)
        self.assertEqual((s["exact"], s["different"], s["missing"]), (1, 2, 2))  # commander isn't owned either
        view = {r["card_name"]: r["ownership"] for r in self.db.conn.execute(
            "SELECT card_name, ownership FROM deck_card_ownership WHERE deck_id = ?", (deck,))}
        self.assertEqual(view["Arcane Signet"], "different_printing")

    def test_copy_used_by_another_deck_is_not_taken(self):
        rhystic = self.card("Rhystic Study")
        lot = self.own(rhystic)
        a = self.deck_with_commander("Deck A")
        self.st.add_card(a, rhystic)
        self.assertEqual(self.row(a, "Rhystic Study")["ownership"], "exact")
        b = self.deck_with_commander("Deck B")
        self.st.add_card(b, rhystic)
        self.assertEqual(self.row(b, "Rhystic Study")["ownership"], "in_use")
        copies = self.st.copies("Rhystic Study")
        self.assertEqual((copies[0]["quantity"], copies[0]["available"]), (1, 0))
        self.assertEqual(copies[0]["used_in"][0]["name"], "Deck A")
        with self.assertRaises(UserDBError):
            self.st.link(self.row(b, "Rhystic Study")["id"], lot)
        self.assertEqual(self.st.allocate(b), 0)
        # deleting deck A releases the copy; deck B can now use it
        self.db.delete_deck(a)
        self.assertEqual(self.st.copies("Rhystic Study")[0]["available"], 1)
        self.assertEqual(self.row(b, "Rhystic Study")["ownership"], "available")
        self.assertEqual(self.st.allocate(b), 1)
        self.assertEqual(self.row(b, "Rhystic Study")["ownership"], "exact")
        self.assertEqual(self.db.copies_owned("Rhystic Study"), 1)  # the card itself never left the collection

    def test_partial_copies_split_the_row(self):
        island = self.card("Island")
        self.own(island, quantity=6)
        deck = self.deck_with_commander()
        self.st.add_card(deck, island, quantity=10)
        rows = [r for r in self.st.deck_cards(deck) if r["card_name"] == "Island"]
        self.assertEqual(sorted((r["quantity"], r["ownership"]) for r in rows), [(4, "missing"), (6, "exact")])
        self.assertEqual(self.st.summary(deck)["count"], 11)

    def test_duplicate_deck_recalculates_ownership(self):
        sol = self.card("Sol Ring")
        self.own(sol, quantity=2)
        self.own(self.card("Rhystic Study"))
        deck = self.deck_with_commander()
        self.st.add_card(deck, sol)
        self.st.add_card(deck, self.card("Rhystic Study"))
        copy = self.st.duplicate(deck, "Copy")
        own = {r["card_name"]: r["ownership"] for r in self.st.deck_cards(copy)}
        self.assertEqual(own["Sol Ring"], "exact")         # the second, free copy
        self.assertEqual(own["Rhystic Study"], "in_use")   # the only copy stays with the original
        self.assertEqual(self.row(deck, "Rhystic Study")["ownership"], "exact")


class ScanSessionTests(DeckBase):
    def test_scanning_into_a_deck_with_duplicates_and_resume(self):
        deck = self.deck_with_commander()
        path = os.path.join(self.tmp, "deck_scan.json")
        t = DeckScanTarget(path, deck, "Test Deck", self.st)
        lookup = self.st.card
        sol, island, cmd = self.card("Sol Ring"), self.card("Island"), self.card("Atraxa, Praetors' Voice")
        self.assertIsNone(t.add(None, dict(id=cmd["id"], finish="nonfoil"), cmd))   # the commander from the pile
        self.assertIsNone(t.add(None, dict(id=sol["id"], finish="nonfoil"), sol))
        self.assertIsNone(t.add(None, dict(id=island["id"], finish="nonfoil"), island))
        self.assertIsNone(t.add(None, dict(id=island["id"], finish="nonfoil"), island))  # basics: fine
        dup = t.add(None, dict(id=sol["id"], finish="nonfoil"), sol)
        self.assertEqual((dup["name"], dup["count"], dup["limit"]), ("Sol Ring", 2, 1))  # flagged, not dropped
        self.assertEqual(len(t.entries), 5)
        st = t.stats(lookup)
        self.assertEqual(st["count"], 5)  # commander (already in the deck) + Sol Ring x2 + Island x2
        self.assertEqual(st["commander"], "Atraxa, Praetors' Voice")
        # "the app closed": a new session for the same deck picks up where it left off
        data = DeckScanTarget.load(path)
        self.assertEqual((data["deck_id"], len(data["entries"])), (deck, 5))
        t2 = DeckScanTarget(path, deck, "Test Deck", self.st, data=data)
        self.assertEqual(t2.stats(lookup)["count"], 5)
        t2.entries.pop()  # remove the duplicate
        rep = self.st.finish_scan(deck, t2.entries)
        self.assertEqual(rep["added"], 4)
        rows = {r["card_name"]: r for r in self.st.deck_cards(deck)}
        self.assertEqual(rows["Island"]["quantity"], 2)
        self.assertTrue(rows["Atraxa, Praetors' Voice"]["physical"])
        self.assertEqual(rows["Atraxa, Praetors' Voice"]["role"], "commander")
        self.assertEqual(self.st.summary(deck)["count"], 4)
        t2.discard()
        self.assertIsNone(DeckScanTarget.load(path))

    def test_finish_scan_links_owned_and_adds_the_rest(self):
        sol, signet, rhystic = self.card("Sol Ring"), self.card("Arcane Signet"), self.card("Rhystic Study")
        sol_lot = self.own(sol)
        self.own(rhystic)
        other = self.deck_with_commander("Other deck")
        self.st.add_card(other, rhystic)  # the only Rhystic Study is in another deck
        deck = self.deck_with_commander()
        entries = [dict(id=c["id"], finish="nonfoil") for c in (sol, signet, rhystic)]
        rep = self.st.finish_scan(deck, entries)
        self.assertEqual(rep["linked"], 1)
        own = {r["card_name"]: r for r in self.st.deck_cards(deck)}
        self.assertEqual(own["Sol Ring"]["collection_item_id"], sol_lot)
        self.assertEqual(own["Arcane Signet"]["ownership"], "deck_only")
        # (the commander was chosen by search, not scanned, so it isn't physically in the pile)
        self.assertEqual([self.db.deck_card(i)["card_name"] for i in rep["not_in_collection"]], ["Arcane Signet"])
        self.assertEqual([self.db.deck_card(i)["card_name"] for i in rep["in_use"]], ["Rhystic Study"])
        added = self.st.add_to_collection(rep["not_in_collection"])
        self.assertEqual(added, 1)
        own = {r["card_name"]: r["ownership"] for r in self.st.deck_cards(deck)}
        self.assertEqual(own["Arcane Signet"], "exact")
        self.assertEqual(self.db.copies_owned("Sol Ring"), 1)       # not duplicated
        self.assertEqual(self.db.copies_owned("Arcane Signet"), 1)
        self.assertEqual(self.db.copies_owned("Rhystic Study"), 1)  # still only the one copy
        log = self.db.conn.execute("SELECT source, cards FROM import_log").fetchall()
        self.assertEqual([tuple(r) for r in log], [("deck_scan", 1)])


class ValueTests(DeckBase):
    def test_value_and_currency(self):
        deck = self.deck_with_commander()
        sol = self.card("Sol Ring")
        self.st.add_card(deck, sol)
        self.st.add_card(deck, self.card("Island"), quantity=10)
        rows = self.st.deck_cards(deck)
        expected = sum(r["price"] * r["quantity"] for r in rows)
        s = self.st.summary(deck)
        self.assertAlmostEqual(s["value"], expected)
        self.assertAlmostEqual(self.st.deck_list()[0]["value"], expected, places=6)
        self.assertEqual(s["top"][0]["price"], max(r["price"] for r in rows))
        self.assertEqual(money(s["value"], {"currency": "ZAR", "usd_zar": 18.0}), f"R{s['value'] * 18:,.2f}")
        self.assertEqual(money(s["value"], {"currency": "USD"}), f"${s['value']:,.2f}")


class ImportExportTests(DeckBase):
    def test_import_text_formats(self):
        foil_sol = [p for p in self.st.printings("Sol Ring") if "foil" in (p["finishes"] or "").split(",")][0]
        text = f"""Commander
1 Thrasios, Triton Hero
1 Tymna the Weaver *CMDR*

Deck
1x Sol Ring ({foil_sol["set_code"].upper()}) {foil_sol["collector_number"]} *F*
1 Arcane Signet
10 Island
1 This Card Does Not Exist
// Sideboard
1 Swords to Plowshares
"""
        parsed = self.st.parse_decklist(text)
        roles = {(r["name"], r["role"]) for r in parsed}
        self.assertIn(("Thrasios, Triton Hero", "commander"), roles)
        self.assertIn(("Tymna the Weaver", "partner"), roles)
        self.assertIn(("Swords to Plowshares", "sideboard"), roles)
        sol = next(r for r in parsed if r["name"] == "Sol Ring")
        self.assertEqual((sol["status"], sol["card"]["id"], sol["finish"]), ("set", foil_sol["id"], "foil"))
        self.assertEqual(next(r for r in parsed if r["name"].startswith("This Card"))["status"], "none")
        self.own(self.card("Arcane Signet"))
        deck = self.st.create_deck("Imported")
        n = self.st.commit_import(deck, parsed)
        self.assertEqual(n, 15)  # 2 commanders + 1 + 1 + 10 + sideboard 1; unknown card skipped
        info = self.db.deck(deck)
        self.assertEqual((info["commander"], info["partner"], info["card_count"]),
                         ("Thrasios, Triton Hero", "Tymna the Weaver", 14))
        self.assertEqual(self.row(deck, "Arcane Signet")["ownership"], "exact")  # linked on import

    def test_import_csv_and_export(self):
        csv_text = "Name,Set code,Collector number,Foil,Quantity,Scryfall ID\nSol Ring,C21,263,normal,1,\n" \
                   f"Island,,,normal,5,{self.card('Island')['id']}\n"
        parsed = self.st.parse_decklist(csv_text)
        self.assertEqual([(r["name"], r["status"], r["quantity"]) for r in parsed],
                         [("Sol Ring", "set", 1), ("Island", "id", 5)])
        deck = self.deck_with_commander("Export Me")
        self.st.commit_import(deck, parsed)
        txt, csv_path = self.st.export(deck, os.path.join(self.tmp, "exports"))
        with open(txt, encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
        self.assertEqual(lines[0], "Commander")
        self.assertTrue(lines[1].startswith("1 Atraxa, Praetors' Voice ("))
        self.assertIn("1 Sol Ring (C21) 263", lines)
        self.assertEqual(lines[2], "Deck")
        with open(csv_path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual({r["Name"]: r["Board"] for r in rows}["Atraxa, Praetors' Voice"], "commander")
        # round trip: the exported list imports back to the same deck
        again = self.st.parse_decklist(open(txt, encoding="utf-8").read())
        self.assertTrue(all(r["status"] == "set" for r in again))
        self.assertEqual(sum(r["quantity"] for r in again), 7)


class PhaseFourQuestionsTests(DeckBase):
    def test_queries_the_analyser_will_need(self):
        self.own(self.card("Sol Ring"), quantity=2)
        self.own(self.card("Counterspell"))
        self.own(self.card("Lightning Bolt"))
        self.own(self.card("Mana Crypt"))
        deck = self.deck_with_commander()
        self.st.add_card(deck, self.card("Sol Ring"))
        self.st.add_card(deck, self.card("Rhystic Study"))
        self.assertEqual(self.st.commander_identity(deck), "WUBG")
        free = {r["card_name"]: r["free"] for r in self.st.free_owned_cards("WUBG")}
        self.assertEqual(free, {"Sol Ring": 1, "Counterspell": 1})  # Bolt is red, Mana Crypt is banned
        self.assertEqual([r["card_name"] for r in self.st.missing_cards(deck)],
                         ["Atraxa, Praetors' Voice", "Rhystic Study"])
        self.assertEqual(self.db.which_owned(["Sol Ring", "Rhystic Study"]), {"Sol Ring": 2})
        self.assertEqual([d["deck"] for d in self.db.decks_using("Sol Ring")], ["Test Deck"])


@needs_cards_db
class MigrationTests(unittest.TestCase):
    def test_phase2_database_upgrades_keeping_collection_and_decks(self):
        tmp = tempfile.mkdtemp(prefix="tmp_decks_mig_", dir=HERE)
        self.addCleanup(shutil.rmtree, tmp, True)
        old = userdb.BACKUP_DIR
        userdb.BACKUP_DIR = os.path.join(tmp, "backups")
        self.addCleanup(setattr, userdb, "BACKUP_DIR", old)
        path = os.path.join(tmp, "user.db")
        v2 = UserDB(path, migrations=userdb.MIGRATIONS[:2])  # a database made by the Phase 2 app
        lot = v2.add_card("x", "Sol Ring", "c21", "263", quantity=2)
        deck = v2.create_deck("Old deck")
        with v2.transaction() as c:  # (the Phase 2 schema has no 'physical' column)
            c.execute("INSERT INTO deck_cards (deck_id, card_name, quantity, collection_item_id) VALUES (?, ?, 1, ?)",
                      (deck, "Sol Ring", lot))
        v2.close()
        db = UserDB(path)
        self.addCleanup(db.close)
        self.assertEqual(db.version, userdb.LATEST_VERSION)
        self.assertEqual(db.copies_owned("Sol Ring"), 2)
        dc = db.deck_cards(deck)[0]
        self.assertEqual((dc["collection_item_id"], dc["physical"]), (lot, 0))
        self.assertEqual(db.availability(lot)["available"], 1)
        own = db.conn.execute("SELECT ownership FROM deck_card_ownership").fetchone()[0]
        self.assertEqual(own, "different_printing")  # deck card has no printing yet: linked to some printing
        self.assertTrue(any("before-v" in b for b in os.listdir(userdb.BACKUP_DIR)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
