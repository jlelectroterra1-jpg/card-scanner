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
3. Press **Start Scan** (side panel, or **P**). The scanner always opens with scanning
   **OFF** so nothing is scanned while you set up; the header shows **SCANNER OFF** (red
   dot) or **SCANNING** (green dot). **Stop Scan** (or **P** again) only pauses automatic
   scanning - the camera stays live and the background, scan list, deck scan and
   collection destination are untouched. **SPACE** still scans one card while it's off.
4. **One card = one scan.** After a card is scanned (or sent to review, skipped, or
   ignored as a deck duplicate) the scanner waits for that physical card to leave: the box
   must show the empty mat again, or a clearly different card must be lying there (drop
   the next card straight on top). Hands, glare, light changes, small nudges, Stop/Start
   and repeated SPACE never scan it twice. An identical copy laid exactly on top looks the
   same to the camera - press **R** to count it, or slide the previous card away.
   Put a card in the box. When it settles you'll hear a **beep** and it's added.
   Take it out (or drop the next card on top) and keep going.
5. **Printing.** Once the card is known, its printing is picked by comparing the photo
   with every printing's artwork (printing.py). Printings with identical art + frame
   can't be told apart by picture, so the set code / number from the card's corner, a
   locked set or a clearly closer frame decides; without that the panel shows **PRINTING
   UNCERTAIN**, a provisional **~price** (the cheapest regular printing is assumed - never
   an expensive variant) and the card/printing confidence. Press **Choose** (or **O**) to
   pick the right one. Uncertain printings get a note in the collection ("Printing not
   confirmed by scanner") so you can fix them with Change printing later.
6. Double low beep = not sure: press **1-5** to pick, **S** to type the name, **X** to skip.

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
Click the tabs at the top (or F1-F4). Auto-scan only runs on the Scanner tab.

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
Click a card for its details: change quantity (+ / -, also keys + and -), finish (only
the finishes that printing really has - the price follows), condition, purchase price and
notes; **Change printing** lists every printing of the card (picture, set, number, date,
rarity, finishes, prices; type to filter by set name, code or number) so you can correct
the exact version you own. It stays the same entry (quantity, condition, purchase price,
notes, date added, collection and deck links kept); if you already have an identical
entry the two are merged (it asks first when their purchase prices or notes differ); Move to another collection; Remove. Copies used in
decks can't be removed. **Manage** creates/renames/deletes collections (Main Collection
can't be deleted; deleting one with cards asks whether to move them or delete them).
**Refresh prices** copies the latest prices from your local card data into the collection
(run **Update Prices.bat** first for fresh prices). **Export** writes a ManaBox CSV and a
plain list. **Import CSV** reads a ManaBox CSV, shows a preview (matched by Scryfall ID, by
set + number, or name only) and only adds name-only matches if you tick them.

**Decks tab (Commander).** New Deck -> name + format -> choose the commander (search, or
put it under the camera: "Set X as commander?"). **Scan Deck** switches the Scanner into
deck mode ("SCANNING DECK", e.g. 37 / 100): feed the whole deck through - same camera,
background, recognition and printing/foil handling as always, but the cards go into that
deck's own session (`data/deck_scan.json`, saved after every card; if the app closes you're
asked to resume). A copy Commander doesn't allow (a second Sol Ring, or a card already in
the deck) is ignored automatically - no pop-up, the status line says "Duplicate ignored:
..." with a short mid beep, and it isn't scanned again until you take it away or drop the
next card on top. Basic lands, "any number" cards (Relentless Rats...) and Seven Dwarves /
Nazgûl (up to 7 / 9) follow their own rules. Normal Collection scanning keeps every copy. **Finish scan** puts the cards in the deck, links each
one to a FREE copy in your Collection (exact printing+finish first, then same printing,
then any printing - never a copy another deck uses) and offers to add the rest to your
Collection. **Pause** keeps the session for later.
The deck view groups cards by type (or All A-Z, by name or price; the card list scrolls
with the mouse wheel, its scrollbar (click or drag), Page Up / Page Down and Home / End
while the header stays put) with ownership per card
(owned exact / other printing, deck only, not owned, used in another deck), colour
identity, warnings (count, colour identity, Commander legality, duplicates - never
changed automatically), value and most valuable cards. Click a card to change quantity,
finish, printing, make it commander/partner, link/unlink a collection copy or remove it.
Add Card searches by name and printing. More: rename, duplicate (the copy only uses free
copies), import (plain/Moxfield/Archidekt/ManaBox text or CSV, with a preview), export
(decklist + CSV), link owned copies, delete (confirmation; Collection cards stay, their
copies become free).

**Analyse tab (Commander deck analyser).** Pick a deck, then choose:
- **Suggest cards from:** *My Collection* (default - only cards you own with a free copy,
  cost R0) or *All Cards* (any legal card in the commander's colours, with a **total**
  budget for the whole package: R0 / R250 / R500 / R1,000 / No limit / Other).
- **Deck goal:** Casual (few, on-theme changes), Improve It (default), High Power.
- **Strategy:** detected from the commander and the deck; **Edit** to choose your own
  themes (they override the detected ones) or go back to the detected ones.

The analyser first works out what the COMMANDER does (its mechanics, game plan, engine
chain and what the 99 need to supply - for any commander), then the deck's engines (an
interaction graph with strong / moderate / weak links), each card's functions (burst vs
repeatable draw, mana rock vs treasure, one-sided vs symmetrical wipe...), and target
ranges adjusted to THIS deck (each change with its reason). Overview shows all of it plus an
**analysis confidence** (commander understood, strategy certainty, cards understood).
Card review / Weakest let you mark cards **Never cut**, **Core** (what the deck is about -
strong evidence for its strategy) or **Not important** (a little less protection).
Popularity: Scryfall's EDHREC rank is used as one small optional signal once Update Prices
has run; price and print counts are never treated as card quality.

**Analyse Deck** runs in the background (a few seconds) and shows:
- **Overview:** lands (basic / other), ramp, draw, removal, wipes, counterspells,
  protection, tutors, recursion and finishers against typical ranges for the goal; mana
  curve and average mana value; colour symbols; detected strategy with reasons; potential
  weaknesses.
- **Swaps:** an upgrade PLAN. For every idea (Consider, and in My Collection mode every free
  card you own that could matter) the analyser asks "what is the least painful card to
  remove for it?" - by comparing the deck BEFORE and AFTER each possible swap (roles against
  this deck's targets, the plan's engine pieces, tribe, interactions, commander synergy, how
  good each card is at its job, mana curve, combos). A card doesn't have to be "bad" to be
  the right cut. Swaps are made one at a time, each judged on the deck with the ones above it
  already applied, so every OUT card is used once and the set fits together; other cards that
  wanted the same slot are listed under it. Grouped as **HIGH CONFIDENCE**, **GOOD UPGRADES**
  and **OPTIONAL CHANGES** - the counts vary by deck, and "No change recommended" is a valid
  result. Each shows OUT -> IN with WHY OUT, WHY IN, WHAT IMPROVES, WHAT YOU LOSE and a NET
  line. Never cut: commanders, Never cut / Core cards, pieces of a combo already in the deck
  (from Check combos), cards the analyser can't understand, the deck's essential pieces, and
  any swap that would leave ramp, draw or removal short. Nothing is ticked: tick the ones you
  want and press **Apply selected changes** - you see the full list first, the deck is
  snapshotted, then changed in one step. **Undo last change** puts it back. In My Collection
  mode only free copies are part of the plan; cards used in another deck are shown separately
  ("OWNED - USED IN ...").
- **Find a cut for this card** - the main tool. **I want to add a card** (any card, by name),
  **Choose from my Collection**, or **Find a cut** on any Consider card: the best cut plus
  alternatives (up to 5, from different kinds of slot: like-for-like, surplus role, curve,
  sharpening the plan), each with why, gains, losses and net - or "No safe cut found" with
  the reasons (protected cards, shortages it would create, or the new card simply isn't better).
- **Consider:** cards worth ADDING - only when they solve a real problem, so there is no
  fixed number (a complete deck may get none; a half-built one many). The analyser first
  lists what the deck needs: missing jobs against this deck's targets (FILLS: Card Draw /
  Removal...), unbalanced engines (STRENGTHENS: Sacrifice Outlets - "14 cards make bodies to
  sacrifice but only 1 outlet"), a missing finisher (ADDS: Finisher), or a clearly better
  version of a card the analyser already doubts (UPGRADES). A job the deck already has
  plenty of gets no more cards. Each idea is weighed on separate signals - need, interaction
  strength (strong / moderate; weak links don't count), commander fit, engine fit, how good
  it is at that job (never price), mana cost - and gets a tier: EXCELLENT FIT, STRONG FIT,
  WORTH CONSIDERING, or NICHE - STRATEGY DEPENDENT (hidden until **Show lower-confidence
  ideas**). Ideas are grouped by purpose (CARD DRAW, SACRIFICE OUTLETS, CORE STRATEGY
  UPGRADES...), each card once, 3 per group until **Show more options**. Your strategy tags
  change what counts as a need. **Good idea** gives similar cards a small nudge next time
  (it never adds anything); **Not for this deck** hides a card for that deck for good.
  My Collection only suggests free copies you own ("No strong upgrades found in your
  available Collection" + **Explore All Cards** otherwise); All Cards marks OWNED / OWNED
  BUT USED IN ANOTHER DECK / NOT OWNED. **Find a card to replace** compares one card against
  the deck's plausible cuts and shows up to 3 options (Swap this / Never cut) - or says
  honestly that nothing is safe to remove. **Community ideas** (optional, online) asks
  [Recommander](https://recommander.cards)'s public API once for this commander + deck and
  caches the answer in data/commander_cache.db (with combo pieces from Check combos); it is
  only a small labelled extra - a card still has to fill a need in YOUR deck. Debug: set
  CARDSCAN_DEBUG_ANALYSER=1 for a **Why not?** button, or run
  `python tests/eval_recommend.py why "<deck>" "<card>"`.
- **Weakest slots:** "if you need room, look here first" - the slots the deck could free
  most cheaply (same before/after engine), each with why it's flexible, what it contributes
  and what you'd lose. Not a list of bad cards; protected and essential cards are left out.
- **Card review:** every card's cut evidence vs. protection (Game Changer, staple, links to
  the commander / other cards, strategy support, repeatable engine, only card doing a
  function, one of few cards in a role, finisher, "the analyser can't tell what it does"...).
  A cut needs evidence that clearly outweighs protection - not understanding a card
  protects it. **Never cut** (here, on a swap, or **Protect from cuts** in the Decks card
  panel) pins a card for that deck; Unprotect removes the pin.
- If the strategy can't be detected clearly, you're told so (and nothing is labelled
  Strong) - set it with **Edit**; your own strategy always wins over detection.
  Commanders and lands are never suggested as cuts.
- **Combos** (optional, online): **Check combos** sends the deck list once to
  [Commander Spellbook](https://commanderspellbook.com) and lists combos in the deck or one
  card away (with ownership of the missing piece). Nothing goes online unless you press it.

The analysis is saved and re-shown next time; a yellow banner says when the deck,
collection, settings or card data changed since. Everything works offline from your own
card data - nothing is ever fetched from EDHREC (its terms forbid automated queries;
**EDHREC (browser)** just opens the commander's page in your browser for your own reference;
Scryfall's documented edhrec_rank field is the only popularity number used) and no AI
service is called. Suggestions are advice:
the deck only changes when you apply them.

**Build a deck from your Collection** (Analyse -> **Build deck**). Choose a commander (search, or
**From my Collection** - every legendary creature you own), a style (Casual / Balanced / High
Power) and, if you like, your own strategy (the detected one is shown; yours always wins). The
builder works in stages, never "sort every card and take 99": the plan's engine (providers
first, then the payoffs they feed - a payoff whose enablers aren't there is left out), the
tribe if it matters, then ramp / draw / interaction / wipes / protection / recursion / finishers
to targets adjusted for the commander (e.g. a commander that draws needs less draw), then the
best synergy for the remaining slots, a curve check, a final pass that replaces unsupported or
dead cards, and a mana base built for the colours the chosen cards actually need (your dual /
tri / fetch / utility lands first, basics in proportion to the coloured pips, untapped lands
preferred at High Power). It uses only FREE cards you own by default (optionally cards used in
other decks - marked, never taken - or All Cards as a wishlist). Click a card for **why it's
here**, **Lock** the ones you like, **Regenerate** (same strategy, more aggressive, more
control, lower curve, more tribal, more combo, more casual) and **Save Deck** to create it with
free collection copies linked. If your collection can't make a sensible deck you're told how
many strong cards it found, with options (weaker owned cards, basics, cards from other decks,
All Cards) - it never pads silently. Nothing changes until you save.

**Print Deck (Word).** In a deck (**Print Deck**, next to Add Card) or in Build Deck (**Print Deck
(Word)**): a real, editable .docx with every card at the exact Magic card size, 63 x 88 mm,
nine to an A4 page (3 x 3) - print it at 100% / Actual Size (never Fit to Page), cut, sleeve.
It uses the deck's exact printings and quantities (30 Islands print 30 cards; a 100-card deck is
12 pages), Scryfall's large images (cached in data/img_xl, otherwise downloaded once in the
background, with progress) embedded in the file so it works offline, fixed-size table cells so
Word can't rescale or shift them, and optional thin light-grey cutting guides on the card edges.
Options: include the commander, include basic lands, the backs of double-faced cards on extra
pages (split / Room / adventure cards have one printed face and are left alone), cutting guides.
Shows the card and page count first, asks before replacing an existing file
(DeckName_Printable.docx in exports\ by default), and lists any card whose image couldn't be
found (its slot says IMAGE MISSING - another printing is never substituted). The deck and
collection are not changed. For personal use - playtesting your own decks; card images come
from Scryfall and are (c) Wizards of the Coast, so please don't sell printed proxies.

**Payoff enablement.** A card whose main ability needs something (proliferate needs counters,
"whenever you gain life" needs life gain, equipment payoffs need Equipment, magecraft needs
spells, tribal lords need the tribe...) is judged on whether the deck supplies it - in Consider,
Find a cut, the swap plan and the builder. Interactions count by QUALITY (a real mechanical
link counts fully, an incidental "is a creature" link barely), so dozens of incidental links
can't outweigh a few real ones.

**Large card viewer.** Click any card picture (Collection or Deck card panel, the
commander in a deck, Analyse swaps / Consider / the commander, card names in Weakest and
Card review, the last scanned card) to see that exact printing as big as fits - Scryfall's
high-resolution image, downloaded once in the background and cached in data/img_xl.
Wheel or + / - zooms, Front / Back (or Left / Right, or click the card) flips double-faced
cards, Esc / X / a click outside closes it.

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
| P | Start / Stop automatic scanning (always OFF when the app opens) |
| SPACE | scan one card now (also while scanning is stopped) |
| O | Choose the printing of the last card (most visually similar first) |
| R | The card in the box is another identical copy: scan it once more |
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
- `printing.py` picks the exact printing once the card is known (artwork match with the
  trained model, identical-art groups, set code / frame evidence, printing confidence);
  `printing_view.py` is the Choose printing dialog.
- `synergy.py` splits roles into functions (burst vs repeatable draw, mana rock vs land
  ramp...), spots engines and builds the deck's interaction graph used by the analyser.
- `cardroles.py` works out each card's jobs (ramp, draw, removal...) from its rules text with
  a confidence and reason; `strategy.py` detects deck themes; `analyser.py` scores cuts and
  replacements (commander synergy, strategy, need, mana efficiency, interaction, quality),
  builds the swap package within the budget and applies/undoes swaps; `analyse_view.py` is
  the screen; `providers.py` holds optional outside data (Commander Spellbook) and the
  EDHREC link.
- `printmatch.py` compares the photo with Scryfall's image of every printing to pick the
  exact one (images are cached in `data/img`).

Tests: `python -m unittest tests.test_decks tests.test_deck_workflow tests.test_collection tests.test_background tests.test_userdb`
(decks incl. a full scan-a-deck workflow, collection, background, database - 71 tests, includes
a 50,000-card speed test),
`python -m unittest tests.test_analyser_safety` (cut safety on realistic decks in
tests/real_decks.py), `python -m unittest tests.test_printing_match` (printing / artwork
choice incl. real captures in tests/fixtures), `python tests/eval_printing.py 150 --old`
(printing accuracy on far shots, old vs new),
`python -m unittest tests.test_analyser` (deck analyser: roles, strategy, composition, cuts,
replacements, modes, budget, ownership, apply/undo, caching, providers and the full Analyse
screen workflow - 36 tests), `python -m unittest tests.test_printing` (changing a
collection card's printing / finish - 20 tests), `python tests/demo_analysis.py all improve 500` (prints a sample
analysis),
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
