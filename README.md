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

## Background (stops the playmat being scanned)
With the scan box **empty**, press **B** (or **Background > Learn**). The scanner saves that
empty view to `data/backgrounds/camera<N>.png` (+ details in `data/settings.json`) and
reloads it every time it starts. Auto-scan only reads something that is clearly different
from it, card-shaped and big enough, so the playmat's own artwork (even under brighter or
darker light) is ignored. The header shows **BG OK**, **NO BG**, **RELEARN BG** (camera or
box changed since it was learned) or **CHECK BG** (saved view doesn't match what the camera
sees). Relearn after moving the camera, changing playmat or big lighting changes;
**K** clears it. Drawing a new scan box learns a new background for it automatically.

## Tabs: Scanner | Collection | Decks | Analyse
Click the tabs at the top (or F1-F4). Auto-scan only runs on the Scanner tab. Decks and
Analyse are placeholders for later phases.

**Scanned cards -> collection.** The side panel shows **To: <collection>** (click it to pick
another, once you have more than one) and **Add N to Collection** (or press **A**). All
not-yet-added scans go into that collection in one go - identical printing + finish +
condition + language just increase the quantity - then it asks whether to clear the scan
list (a copy is saved to `exports\` first). Cards already added are remembered, so pressing
it again never adds them twice.

**Collection tab.** Collection picker, totals (cards | unique | value), search (name, set
code or collector number - just start typing), filters (set, finish, condition, rarity,
colour, type - colour/type need one **Update Prices** run first), sort (highest value first,
price, name, quantity, set, recently added; or click a column title) and a Statistics panel.
Click a card for its details: change quantity (+ / -, also keys + and -), finish,
condition, purchase price and notes; Move to another collection; Remove. Copies used in
decks can't be removed. **Manage** creates/renames/deletes collections (Main Collection
can't be deleted; deleting one with cards asks whether to move them or delete them).
**Refresh prices** copies the latest prices from your local card data into the collection
(run **Update Prices.bat** first for fresh prices). **Export** writes a ManaBox CSV and a
plain list. **Import CSV** reads a ManaBox CSV, shows a preview (matched by Scryfall ID, by
set + number, or name only) and only adds name-only matches if you tick them.

**USD / ZAR.** Top right: switch the display currency and set the rate (1 USD = R...).
Prices are always stored in USD; ZAR is only how they're shown.

## Collection database (foundation)
`data/user.db` (SQLite, created on first start) holds collections (a default
**Main Collection**, plus binders/boxes), the physical cards in them (printing, finish,
condition, language, quantity, prices, notes) and decks (commander/partner, cards, which
owned copy each deck card uses). See `userdb.py`. Schema upgrades are automatic, run in a
transaction, and back the file up to `data/backups/` first. The scanning list
(`data/session.json`) and Export work exactly as before; moving scans into the collection
is a later step (`UserDB.import_session` exists but isn't wired to a button yet).

## Side panel
Shows the card count, total value and scanning speed; the last card (picture, set,
rarity, price) with **‹ Print / Print ›**, **Foil** and **Remove** buttons; recent cards;
and **Export / Lock set / New list / Camera** buttons. "Which card?" choices and name
search are clickable too. All buttons also have keys:

## Keys
| Key | Does |
|---|---|
| drag | set scan box |
| B | learn / relearn the background (box must be empty) |
| K | clear the learned background |
| A | add the scanned cards to the collection |
| F1-F4 | Scanner / Collection / Decks / Analyse tab |
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

Tests: `python -m unittest tests.test_collection tests.test_background tests.test_userdb`
(collection, background, database - 52 tests, includes a 50,000-card speed test),
`python tests/render_ui.py` (renders the screens to tests/ui_shots for a visual check),
`python tests/check_far_pipeline.py` (far-away scans end to end),
`python tests/test_synthetic.py 100` (name reading), `python tests/test_visual.py 200`
(picture recognition of small, blurry cards) and
`python tests/test_scanner_smoke.py` (auto-scan loop).

## Phone version (web/)
The `web/` folder is the same scanner as a web page for iPhone/Android: the camera,
card finding (OpenCV.js) and name reading (the PP-OCRv5 English model via onnxruntime-web)
all run on the phone; card data and prices come straight from Scryfall.
It's deployed to GitHub Pages automatically on every push (see `.github/workflows/pages.yml`).

To try it locally: `python -m http.server 8765 --directory web` and open http://localhost:8765.
After `update_db.py`, run `python web/build_data.py` to refresh the phone's name list.
