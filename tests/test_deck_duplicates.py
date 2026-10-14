"""Scan Deck mode: a copy Commander doesn't allow is ignored automatically (no pop-up), and
the scanner waits for that card to be taken away before scanning again. Basics, "any
number" cards and limited cards (Seven Dwarves) follow their rules; normal Collection
scanning keeps every copy.

    python -m unittest tests.test_deck_duplicates -v

Temporary databases and folders only - never your real data.
"""
import os
import sys
import time
import unittest

import cv2

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import deckrules  # noqa: E402
import scanner  # noqa: E402
import test_deck_workflow as tw  # noqa: E402
import test_decks as td  # noqa: E402
from deckscan import DeckScanTarget, SessionTarget  # noqa: E402
from test_background import lit, with_card  # noqa: E402


def entry(card):
    return dict(id=card["id"], finish="nonfoil")


class CopyLimitTests(unittest.TestCase):
    def test_limits(self):
        self.assertEqual(deckrules.copy_limit("Sol Ring", "Artifact"), 1)
        self.assertEqual(deckrules.copy_limit("Talisman of Creativity", "Artifact"), 1)
        for basic in ("Island", "Snow-Covered Forest", "Wastes"):
            self.assertIsNone(deckrules.copy_limit(basic, "Basic Land"))
        self.assertIsNone(deckrules.copy_limit("Relentless Rats", "Creature"))
        self.assertIsNone(deckrules.copy_limit("Sphinx's Approach", "Sorcery"))
        self.assertEqual(deckrules.copy_limit("Seven Dwarves", "Creature"), 7)
        self.assertEqual(deckrules.copy_limit("Nazgûl", "Creature"), 9)
        # straight from the rules text, for cards not in the lists
        self.assertIsNone(deckrules.copy_limit("Future Rat", "Creature",
                                               "A deck can have any number of cards named Future Rat."))
        self.assertEqual(deckrules.copy_limit("Future Dwarf", "Creature",
                                              "A deck can have up to five cards named Future Dwarf."), 5)


class DeckScanTargetTests(td.DeckBase):
    def target(self, deck):
        return DeckScanTarget(os.path.join(self.tmp, "deck_scan.json"), deck, "Test Deck", self.st)

    def test_second_copy_is_ignored_and_counts_do_not_change(self):
        deck = self.deck_with_commander()
        t = self.target(deck)
        sol = self.card("Sol Ring")
        self.assertIsNone(t.add(None, entry(sol), sol))
        before = t.stats(self.st.card)
        res = t.add(None, entry(sol), sol)
        self.assertTrue(res["rejected"])
        self.assertEqual(res["name"], "Sol Ring")
        self.assertEqual(sum(1 for e in t.entries if e["name"] == "Sol Ring"), 1)
        after = t.stats(self.st.card)
        self.assertEqual((after["count"], after["unique"], after["value"]),
                         (before["count"], before["unique"], before["value"]))
        saved = DeckScanTarget.load(os.path.join(self.tmp, "deck_scan.json"))
        self.assertEqual(len(saved["entries"]), 1)  # not saved either

    def test_card_already_in_the_deck_is_ignored_and_allocation_unchanged(self):
        deck = self.deck_with_commander()
        sol = self.card("Sol Ring")
        lot = self.own(sol)
        self.st.add_card(deck, sol)  # the deck already has its Sol Ring, linked to the owned copy
        alloc = self.db.availability(lot)
        rows = [(r["card_name"], r["quantity"], r["collection_item_id"]) for r in self.st.deck_cards(deck)]
        t = self.target(deck)
        self.assertTrue(t.add(None, entry(sol), sol)["rejected"])
        self.assertEqual(t.entries, [])
        self.st.finish_scan(deck, t.entries)
        self.assertEqual(self.db.availability(lot), alloc)
        self.assertEqual([(r["card_name"], r["quantity"], r["collection_item_id"]) for r in self.st.deck_cards(deck)],
                         rows)
        self.assertEqual(self.st.summary(deck)["count"], 2)

    def test_commander_card_counts_once(self):
        deck = self.deck_with_commander()
        t = self.target(deck)
        cmd = self.card("Atraxa, Praetors' Voice")
        self.assertIsNone(t.add(None, entry(cmd), cmd))  # the physical commander from the pile
        self.assertTrue(t.add(None, entry(cmd), cmd)["rejected"])  # a second one isn't
        self.assertEqual(len(t.entries), 1)

    def test_basics_and_any_number_cards_are_kept(self):
        deck = self.deck_with_commander()
        t = self.target(deck)
        for name, n in (("Island", 5), ("Snow-Covered Island", 3), ("Relentless Rats", 4), ("Sphinx's Approach", 3)):
            c = self.card(name)
            for _ in range(n):
                self.assertIsNone(t.add(None, entry(c), c), name)
        self.assertEqual(len(t.entries), 15)

    def test_limited_cards_obey_their_limit(self):
        deck = self.deck_with_commander()
        t = self.target(deck)
        dwarves = self.card("Seven Dwarves")
        for _ in range(7):
            self.assertIsNone(t.add(None, entry(dwarves), dwarves))
        self.assertTrue(t.add(None, entry(dwarves), dwarves)["rejected"])
        self.assertEqual(len(t.entries), 7)

    def test_oracle_text_rule_is_used(self):
        deck = self.deck_with_commander()
        t = self.target(deck)
        c = dict(self.card("Sol Ring"), oracle_text="A deck can have up to two cards named Sol Ring.")
        self.assertIsNone(t.add(None, entry(c), c))
        self.assertIsNone(t.add(None, entry(c), c))
        self.assertTrue(t.add(None, entry(c), c)["rejected"])

    def test_collection_scanning_keeps_duplicates(self):
        t = SessionTarget(os.path.join(self.tmp, "session.json"))
        sol = self.card("Sol Ring")
        for _ in range(3):
            self.assertIsNone(t.add(None, entry(sol), sol))
        self.assertEqual(len(t.entries), 3)


W = tw.DeckWorkflowTest.__dict__


class ScannerDuplicateTests(unittest.TestCase):
    """The real scanner, fake camera frames: recognition, trigger and re-arming."""
    setUpClass, tearDownClass = W["setUpClass"], W["tearDownClass"]
    setUp, tearDown = W["setUp"], W["tearDown"]
    start, printing_with_image, scan, drain = W["start"], W["printing_with_image"], W["scan"], W["drain"]

    def hold(self, s, card, n=30, seed=0):
        """Leave a card lying in the box for n frames; return how many scans started."""
        img = cv2.imread(os.path.join(tw.IMG_DIR, card["id"] + ".jpg"))
        started = []
        real = s.start_scan

        def spy(frame, manual=False):
            started.append(manual)
            real(frame, manual)
        s.start_scan = spy
        for i in range(n):
            f = lit(with_card(self.mat, img), seed=seed + i)
            s.last_frame = f
            s.update_trigger(f)
            self.drain(s)
            if s.busy:
                self.drain(s, wait=True)
        s.start_scan = real
        return len(started)

    def empty(self, s, n=4):
        for i in range(n):
            f = lit(self.mat, seed=50 + i)
            s.last_frame = f
            s.update_trigger(f)
            self.drain(s)

    def deck_scanner(self):
        s = self.start()
        st, udb = s.deck_store, s.userdb
        deck = st.create_deck("Dup Deck")
        st.add_card(deck, st.default_printing("Atraxa, Praetors' Voice"), role="commander")
        sol = self.printing_with_image(s, "Sol Ring")
        lot = udb.add_card(sol["id"], sol["name"], sol["set_code"], sol["collector_number"])
        st.add_card(deck, sol)  # already in the deck, using the owned copy
        s.start_deck_scan(deck)
        self.assertEqual(s.target.kind, "deck")
        return s, deck, sol, lot

    def test_duplicate_ignored_without_popup_and_not_rescanned(self):
        s, deck, sol, lot = self.deck_scanner()
        st, udb = s.deck_store, s.userdb
        view = s.panel_view()[0]["deck_mode"]
        alloc = udb.availability(lot)
        self.empty(s)
        scans = self.hold(s, sol, n=40)  # the duplicate stays in the box for a long time
        self.assertEqual(scans, 1, "the rejected card must not be scanned over and over")
        self.assertIsNone(s.dialog)
        self.assertIn("Duplicate ignored: Sol Ring", s.status)
        self.assertEqual(s.target.entries, [])
        after = s.panel_view()[0]["deck_mode"]
        self.assertEqual((after["count"], after["unique"], after["value"]), (view["count"], view["unique"], view["value"]))
        self.assertEqual(udb.availability(lot), alloc)
        # taken away and put back: it's looked at again (and ignored again)
        self.empty(s)
        self.assertEqual(self.hold(s, sol, n=12, seed=100), 1)
        self.assertEqual(s.target.entries, [])
        # the next, different card is scanned normally
        self.empty(s)
        bolt = self.printing_with_image(s, "Counterspell")
        self.hold(s, bolt, n=12, seed=200)
        self.assertEqual([e["name"] for e in s.target.entries], ["Counterspell"])
        self.assertIsNone(s.dialog)

    def test_covering_the_rejected_card_with_another_rearms(self):
        s, deck, sol, lot = self.deck_scanner()
        self.empty(s)
        self.assertEqual(self.hold(s, sol, n=12), 1)
        # the next card dropped straight on top (no empty box in between)
        nxt = self.printing_with_image(s, "Counterspell")
        self.assertEqual(self.hold(s, nxt, n=12, seed=300), 1)
        self.assertEqual([e["name"] for e in s.target.entries], ["Counterspell"])

    def test_basic_lands_still_scan_twice(self):
        s, deck, sol, lot = self.deck_scanner()
        island = self.printing_with_image(s, "Island")
        self.scan(s, island)
        self.scan(s, island)
        self.assertEqual([e["name"] for e in s.target.entries], ["Island", "Island"])
        self.assertIsNone(s.dialog)

    def test_collection_scanning_keeps_duplicates(self):
        s = self.start()
        self.assertEqual(s.target.kind, "session")
        sol = self.printing_with_image(s, "Sol Ring")
        self.scan(s, sol)
        self.scan(s, sol)
        self.assertEqual([s.db.by_id(e["id"])["name"] for e in s.entries], ["Sol Ring", "Sol Ring"])
        self.assertIsNone(s.dialog)


if __name__ == "__main__":
    unittest.main()
