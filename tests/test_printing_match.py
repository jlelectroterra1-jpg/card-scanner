"""Stage 2 of recognition - which PRINTING / artwork (printing.py):

* the printing whose artwork matches the photo wins; metadata (newest, price, set
  code text, promo status) can never make a different artwork win
* identical-art reprints need secondary evidence (set code / number, frame match, locked
  set); without it the printing is "uncertain" and the cheapest regular one is assumed
* printing confidence is separate from card confidence
* real webcam captures (tests/fixtures): Thought Vessel must be PLG22 #2, not SLD #1665
* price follows the chosen printing; an uncertain printing is marked in the collection
* the Choose printing dialog

    python -m unittest tests.test_printing_match -v
"""
import os
import shutil
import sys
import tempfile
import types
import unittest

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIX = os.path.join(HERE, "fixtures")
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import printing  # noqa: E402
from printing import SAME_LOOK, PrintingMatcher  # noqa: E402

CARDS_DB = os.path.join(ROOT, "data", "cards.db")
HAVE_MODEL = os.path.exists(os.path.join(ROOT, "data", "vis", "card_embed.onnx")) and os.path.exists(CARDS_DB)


# ---------------------------------------------------------------- logic, with a fake model

def unit(*v):
    v = np.array(v, np.float32)
    return v / np.linalg.norm(v)


ART_A, ART_B, ART_C = unit(1, 0, 0, 0), unit(0, 1, 0, 0), unit(0, 0, 1, 0)


def prt(pid, set_code, number, usd, released="2020-01-01", promo=0, lang="en"):
    return dict(id=pid, name="Test Card", set_code=set_code, collector_number=number, usd=usd, released_at=released,
                promo=promo, lang=lang, finishes="nonfoil,foil", usd_foil=None, usd_etched=None)


class FakeDB:
    def __init__(self, prints):
        self.prints = prints

    def printings(self, name):
        return list(self.prints)


class FakeModel:
    """embed(photo) returns the photo's 'artwork vector'; clean pictures come from feats."""
    def __init__(self, arts):
        self.ids = np.array(list(arts))
        self.feats = np.array([arts[i] for i in arts], np.float32)

    def embed(self, img):
        return img  # the "photo" is just its vector in these tests


def matcher(prints, arts):
    m = PrintingMatcher(FakeDB(prints), FakeModel(arts))
    m._frame_match = staticmethod(lambda img, group: None)  # pictures only (no frame evidence) unless a test says so
    return m


class PrintingLogicTests(unittest.TestCase):
    def setUp(self):
        # two printings share art A (regular + Secret Lair-ish expensive), one has art B (newest, priciest)
        self.prints = [prt("a1", "c15", "55", "3.18", "2015-11-13"), prt("a2", "cmr", "346", "1.99", "2020-11-20"),
                       prt("b1", "sld", "1665", "10.65", "2026-07-29")]
        self.arts = {"a1": ART_A, "a2": ART_A, "b1": ART_B}

    def photo(self, *mix):
        return unit(*mix)

    def test_matching_artwork_wins_over_newer_pricier_printing(self):
        m = matcher(self.prints, self.arts)
        ps, ch = m.choose("Test Card", self.photo(0.9, 0.3, 0, 0.1))
        self.assertIn(ps[0]["id"], ("a1", "a2"))
        self.assertNotEqual(ps[0]["id"], "b1")
        self.assertGreaterEqual(ch["art_confidence"], 0.99)

    def test_metadata_cannot_beat_artwork(self):
        """Set code + number read for the OTHER artwork, newest, non-promo: still art A."""
        m = matcher(self.prints, self.arts)
        ps, ch = m.choose("Test Card", self.photo(0.9, 0.3, 0, 0.1), footer_text="SLD 1665 EN")
        self.assertIn(ps[0]["id"], ("a1", "a2"))
        self.assertEqual(ps[-1]["id"], "b1")

    def test_identical_art_without_evidence_is_uncertain_and_cheapest(self):
        m = matcher(self.prints, self.arts)
        ps, ch = m.choose("Test Card", self.photo(0.9, 0.3, 0, 0.1))
        self.assertTrue(ch["uncertain"])
        self.assertEqual(ch["set_confidence"], 0.5)
        self.assertEqual(ps[0]["id"], "a2")  # cheapest regular printing of art A ($1.99, not $3.18)
        self.assertIn("share this artwork", ch["reason"])

    def test_identical_art_resolved_by_set_code_and_number(self):
        m = matcher(self.prints, self.arts)
        ps, ch = m.choose("Test Card", self.photo(0.9, 0.3, 0, 0.1), footer_text="0055 R C15 EN")
        self.assertEqual(ps[0]["id"], "a1")
        self.assertFalse(ch["uncertain"])
        self.assertEqual(ch["set_confidence"], 1.0)

    def test_identical_art_resolved_by_frame_match(self):
        m = matcher(self.prints, self.arts)
        m._frame_match = lambda img, group: (next(p for p in group if p["id"] == "a1"), 1.4)
        ps, ch = m.choose("Test Card", self.photo(0.9, 0.3, 0, 0.1))
        self.assertEqual(ps[0]["id"], "a1")
        self.assertEqual(ch["set_confidence"], 0.8)
        self.assertFalse(ch["uncertain"])

    def test_identical_art_resolved_by_locked_set(self):
        m = matcher(self.prints, self.arts)
        ps, ch = m.choose("Test Card", self.photo(0.9, 0.3, 0, 0.1), locked_set="c15")
        self.assertEqual(ps[0]["id"], "a1")
        self.assertFalse(ch["uncertain"])

    def test_ambiguous_artwork_is_uncertain(self):
        prints = [prt("a1", "aaa", "1", "1"), prt("b1", "bbb", "2", "1")]
        m = matcher(prints, {"a1": ART_A, "b1": ART_B})
        ps, ch = m.choose("Test Card", self.photo(0.7, 0.68, 0.2, 0))  # looks about as much like either
        self.assertTrue(ch["uncertain"])
        self.assertLess(ch["art_confidence"], 0.5)

    def test_photo_resembling_no_artwork_is_uncertain(self):
        prints = [prt("a1", "aaa", "1", "1"), prt("b1", "bbb", "2", "1")]
        m = matcher(prints, {"a1": ART_A, "b1": ART_B})
        ps, ch = m.choose("Test Card", self.photo(0.12, 0.0, 0.99, 0))
        self.assertTrue(ch["uncertain"])

    def test_single_printing_is_certain(self):
        m = matcher([prt("a1", "aaa", "1", "1")], {"a1": ART_A})
        ps, ch = m.choose("Test Card", self.photo(1, 0, 0, 0))
        self.assertEqual((ps[0]["id"], ch["uncertain"], ch["confidence"]), ("a1", False, 1.0))

    def test_secret_lair_is_never_the_default_guess_for_identical_art(self):
        prints = [prt("s1", "sld", "99", "0.50", "2026-01-01"), prt("r1", "m19", "306", "0.75", "2018-07-13"),
                  prt("p1", "plst", "M19-306", "0.40", "2020-01-01")]
        m = matcher(prints, {"s1": ART_A, "r1": ART_A, "p1": ART_A})
        ps, ch = m.choose("Test Card", self.photo(1, 0, 0, 0))
        self.assertEqual(ps[0]["id"], "r1")  # regular printing, even though SLD / The List are cheaper/newer

    def test_debug_candidates_and_description(self):
        m = matcher(self.prints, self.arts)
        ps, ch = m.choose("Test Card", self.photo(0.9, 0.3, 0, 0.1))
        vis = {c["id"]: c["visual"] for c in ch["candidates"]}
        self.assertGreater(vis["a1"], vis["b1"])
        text = printing.describe("Test Card", ch, ps)
        self.assertIn("SLD #1665", text)
        self.assertIn("Selected:", text)
        self.assertIn("Printing confidence", text)

    def test_without_a_picture_model_it_says_so(self):
        m = PrintingMatcher(FakeDB(self.prints), None)
        ps, ch = m.choose("Test Card", None)
        self.assertTrue(ch["uncertain"])
        self.assertNotEqual(ps[0]["id"], "b1")  # still never the special / priciest guess


# ---------------------------------------------------------------- the real model and real captures

@unittest.skipUnless(HAVE_MODEL, "needs the trained card model and cards.db")
class RealPrintingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from carddb import CardDB
        from visual import load_index
        cls.db = CardDB()
        cls.m = PrintingMatcher(cls.db, load_index())

    def find(self, name, set_code, number):
        return next(p for p in self.db.printings(name) if p["set_code"] == set_code and p["collector_number"] == number)

    def same_look(self, a, b):
        ea, eb = self.m.clean_embedding(a), self.m.clean_embedding(b)
        return a == b or float(ea @ eb) >= SAME_LOOK

    def real(self, fname):
        img = cv2.imread(os.path.join(FIX, fname))
        if img is None:
            self.skipTest(f"no fixture {fname}")
        return img

    def test_real_thought_vessel_is_plg22_not_secret_lair(self):
        for f in ("real_thought_vessel_1.jpg", "real_thought_vessel_2.jpg"):
            ps, ch = self.m.choose("Thought Vessel", self.real(f))
            self.assertEqual((ps[0]["set_code"], ps[0]["collector_number"]), ("plg22", "2"), f)
            self.assertFalse(ch["uncertain"])
            sld = next(c for c in ch["candidates"] if c["set"] == "sld" and c["number"] == "1665") \
                if any(c["set"] == "sld" and c["number"] == "1665" for c in ch["candidates"]) else None
            if sld:
                self.assertLess(sld["visual"], ch["candidates"][0]["visual"] - 0.3)

    def test_real_captures_pick_the_photographed_artwork(self):
        """Real webcam scans where the old method chose a Secret Lair / SLZ artwork."""
        for f, name, want in (("real_nexus_of_fate.jpg", "Nexus of Fate", ("m19", "306")),
                              ("real_cultivate.jpg", "Cultivate", ("scd", "177")),
                              ("real_aminatou.jpg", "Aminatou, the Fateshifter", ("2x2", "169"))):
            ps, ch = self.m.choose(name, self.real(f))
            ref = self.find(name, *want)
            self.assertTrue(self.same_look(ps[0]["id"], ref["id"]), f"{name}: got {ps[0]['set_code']} #{ps[0]['collector_number']}")
            self.assertNotIn(ps[0]["set_code"], ("sld", "slz"), name)

    def test_far_shots_of_different_artworks(self):
        """Far webcam-like shots of each artwork of Thought Vessel: the photographed art wins."""
        from test_visual import desk, far_shot
        from recognizer import Recognizer
        rng = np.random.default_rng(3)
        for set_code, number in (("c15", "55"), ("sld", "1665"), ("plg22", "2"), ("who", "255")):
            truth = self.find("Thought Vessel", set_code, number)
            img = cv2.resize(cv2.imread(os.path.join(ROOT, "data", "img", truth["id"] + ".jpg")), (488, 680))
            shot, (x0, y0, x1, y1), empty = far_shot(img, desk(rng), rng)
            card, _ = Recognizer.find_card_checked(shot[y0:y1, x0:x1], empty[y0:y1, x0:x1])
            self.assertIsNotNone(card)
            ps, ch = self.m.choose("Thought Vessel", card)
            self.assertTrue(self.same_look(ps[0]["id"], truth["id"]),
                            f"{set_code} #{number} -> {ps[0]['set_code']} #{ps[0]['collector_number']}")

    def test_identical_reprints_are_grouped(self):
        c15, cmr, sld = (self.find("Thought Vessel", *k)["id"] for k in (("c15", "55"), ("cmr", "346"), ("sld", "1665")))
        self.assertTrue(self.same_look(c15, cmr))
        self.assertFalse(self.same_look(c15, sld))


# ---------------------------------------------------------------- recognizer, scan entry, collection, dialog

@unittest.skipUnless(HAVE_MODEL, "needs the trained card model and cards.db")
class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from carddb import CardDB
        from recognizer import Recognizer
        from visual import load_index
        cls.db = CardDB()
        cls.rec = Recognizer(cls.db, load_index())

    def test_card_and_printing_confidence_are_separate(self):
        r = self.rec.identify(cv2.imread(os.path.join(FIX, "real_thought_vessel_1.jpg")))
        self.assertEqual(r["printings"][0]["name"], "Thought Vessel")
        self.assertTrue(r["confident"])
        self.assertIn("card_confidence", r)
        self.assertIn("confidence", r["printing"])
        self.assertEqual((r["printings"][0]["set_code"], r["printings"][0]["collector_number"]), ("plg22", "2"))

    def test_scan_entry_and_collection_value(self):
        import userdb
        from userdb import UNCONFIRMED_NOTE, UserDB
        tmp = tempfile.mkdtemp(prefix="tmp_pm_", dir=HERE)
        self.addCleanup(shutil.rmtree, tmp, True)
        old = userdb.BACKUP_DIR
        userdb.BACKUP_DIR = os.path.join(tmp, "b")
        self.addCleanup(setattr, userdb, "BACKUP_DIR", old)
        udb = UserDB(os.path.join(tmp, "u.db"))
        self.addCleanup(udb.close)
        prints = [p for p in self.db.printings("Thought Vessel") if p["lang"] == "en"]
        cheap = min((p for p in prints if p["usd"]), key=lambda p: float(p["usd"]))
        sld = next(p for p in prints if p["set_code"] == "sld" and p["collector_number"] == "1665")
        entries = [dict(id=cheap["id"], finish="nonfoil", printing_uncertain=True),
                   dict(id=sld["id"], finish="foil", printing_uncertain=False)]
        udb.import_session(entries, self.db.by_id)
        items = {i["scryfall_id"]: i for i in udb.items()}
        self.assertAlmostEqual(items[cheap["id"]]["market_price"], float(cheap["usd"]))  # price of THAT printing
        self.assertEqual(items[cheap["id"]]["notes"], UNCONFIRMED_NOTE)
        foil = sld.get("usd_foil")
        self.assertEqual(items[sld["id"]]["market_price"], float(foil) if foil else None)  # never the non-foil price
        self.assertIsNone(items[sld["id"]]["notes"])

    def test_choose_printing_dialog(self):
        from printing_view import ChoosePrintingDialog
        ps, ch = self.rec.printings_for("Thought Vessel", cv2.imread(os.path.join(FIX, "real_thought_vessel_1.jpg")))
        entry = dict(id=ps[0]["id"], finish="nonfoil", alts=[p["id"] for p in ps[:40]], alt_idx=0,
                     printing_uncertain=True, alt_vis={c["id"]: c["visual"] for c in ch["candidates"]},
                     printing_reason=ch["reason"])
        picked = []
        app = types.SimpleNamespace(db=self.db, thumb=lambda cid, w: None, money=lambda u: f"${u:.2f}",
                                    set_printing=lambda e, cid: picked.append(cid))
        dlg = ChoosePrintingDialog(app, entry)
        dlg.draw(np.zeros((650, 1400, 3), np.uint8))
        rows = [h for h in dlg.hits if h[4] and h[4][0] == "print"]
        self.assertEqual(rows[0][4][1], ps[0]["id"])  # most similar first
        x0, y0, x1, y1, a = rows[2]
        dlg.click((x0 + x1) // 2, (y0 + y1) // 2)
        self.assertEqual(picked, [a[1]])
        self.assertTrue(dlg.done)


if __name__ == "__main__":
    unittest.main()
