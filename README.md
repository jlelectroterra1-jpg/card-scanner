# Card Scanner

A free, unlimited webcam scanner for Magic: The Gathering cards. It runs entirely on
your PC using Scryfall's free card data, so there are no accounts and no scan limits.

## Start
Double-click **Start Scanner.bat**. The first run downloads the card list (about 80 MB, 30 s)
and a small picture of every card artwork for picture recognition (about 500 MB, 15-30 min, once).
Double-click **Update Prices.bat** now and then for fresh prices and new sets.

## Scanning
1. Point the webcam down at your desk. Good, even light helps a lot; avoid glare on the card.
2. **Drag a box** on the video where cards will land, a bit bigger than a card.
   The box must be empty when you let go (it takes a photo of the empty desk).
   If it wasn't, clear it and press **B**.
3. Put a card in the box. When it settles you'll hear a **beep** and it's added.
   Take it out (or drop the next card on top) and keep going.
4. Double low beep = not sure: press **1-5** to pick, **S** to type the name, **X** to skip.

The bigger the card looks in the camera, the better: put the webcam about 20-30 cm above
the desk. A phone through Camo gives a sharper picture than the C270 (press **C** to
switch cameras).

## Side panel
Shows the card count, total value and scanning speed; the last card (picture, set,
rarity, price) with **‹ Print / Print ›**, **Foil** and **Remove** buttons; recent cards;
and **Export / Lock set / New list / Camera** buttons. "Which card?" choices and name
search are clickable too. All buttons also have keys:

## Keys
| Key | Does |
|---|---|
| drag | set scan box |
| B | re-take the empty-desk photo |
| SPACE | scan now |
| F | cycle foil / etched on the last card |
| G | make new cards default to foil |
| [ ] | previous / next printing of the last card |
| DEL / U | remove the last card |
| L | lock to a set (type the code in the terminal, e.g. `mkm`) |
| E | export |
| C | next camera |
| N N | export and start a new list |
| Q | quit (your list is kept for next time) |

## Export
**E** writes two files into `exports\`:
- `..._manabox.csv`: import into ManaBox (uses Scryfall IDs, so printings are exact).
- `..._decklist.txt`: `1 Lightning Bolt (M11) 149 *F*` lines for Moxfield, Archidekt, etc.

Prices are Scryfall's USD market prices.

## How it works
- `update_db.py` downloads Scryfall's bulk card data into `data/cards.db`.
- `recognizer.py` finds the card in the box, straightens it, and reads the name bar (RapidOCR).
- `carddb.py` fuzzy-matches the name against all ~34,000 cards.
- `visual.py` recognises the card by its picture, so it works from a distance where the
  name is unreadable. It uses a model trained by `train_model.py` (run `train.bat`, ~1 hour
  on an NVIDIA GPU) on millions of fake webcam shots of all ~49,000 artworks: small,
  blurry, glare, sleeves, fingers, colour casts. In tests it recognised 200/200 cards only
  ~110-150 px tall. Without the trained model it falls back to a MobileNetV2 +
  colour-thumbnail index (`build_visual_index.py`), which is much weaker.
- `printmatch.py` compares the photo with Scryfall's image of every printing to pick the
  exact one (images are cached in `data/img`).

Tests: `python tests/test_synthetic.py 100` (name reading), `python tests/test_visual.py 200`
(picture recognition of small, blurry cards) and
`python tests/test_scanner_smoke.py` (auto-scan loop).

## Phone version (web/)
The `web/` folder is the same scanner as a web page for iPhone/Android: the camera,
card finding (OpenCV.js) and name reading (the PP-OCRv5 English model via onnxruntime-web)
all run on the phone; card data and prices come straight from Scryfall.
It's deployed to GitHub Pages automatically on every push (see `.github/workflows/pages.yml`).

To try it locally: `python -m http.server 8765 --directory web` and open http://localhost:8765.
After `update_db.py`, run `python web/build_data.py` to refresh the phone's name list.
