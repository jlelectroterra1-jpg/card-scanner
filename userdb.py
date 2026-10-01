"""Your permanent card data: collections (binders/boxes), the physical cards in them,
and decks - in data/user.db (SQLite), separate from Scryfall's data/cards.db.

Design
- A row in `collection_items` is a *lot*: N identical physical cards, i.e. the same
  printing (Scryfall ID) + finish + condition + language in the same collection.
  Scanning another copy just increments `quantity`.
- Card details live in Scryfall's cards.db and are looked up by `scryfall_id`. We keep
  a few fields (name, set, collector number, oracle id) here as well, so the collection
  stays readable and queryable on its own even if cards.db is rebuilt or a card vanishes.
- A deck card can point at the collection lot it physically comes from
  (`deck_cards.collection_item_id`). Copies allocated to decks vs still free are
  available from the `collection_item_availability` view.
- Commander / partner are deck cards with role 'commander' / 'partner' (at most one of
  each per deck, enforced by unique indexes); `deck_summary` shows them per deck.

Safety
- Schema version lives in `PRAGMA user_version`; `MIGRATIONS` upgrades step by step,
  each step in one transaction (all or nothing), after an automatic backup copy in
  data/backups/. A database from a *newer* app version is never touched.
- WAL journal + synchronous=FULL; foreign keys enforced; writes are transactional.
"""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
USER_DB_PATH = os.path.join(HERE, "data", "user.db")
BACKUP_DIR = os.path.join(HERE, "data", "backups")
CARDS_DB_PATH = os.path.join(HERE, "data", "cards.db")

FINISHES = ("nonfoil", "foil", "etched")
CONDITIONS = ("mint", "near_mint", "lightly_played", "moderately_played", "heavily_played", "damaged")
DECK_ROLES = ("commander", "partner", "companion", "main", "sideboard", "maybe")
DEFAULT_COLLECTION = "Main Collection"
NOW = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"


def _in(values):
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


# ---------------------------------------------------------------- schema migrations
# Each migration: (version, description, [SQL statements]). Never edit a released
# migration - add a new one. Statements run one by one inside a single transaction.

MIGRATIONS = [
    (1, "Collections, collection items, decks and deck cards", [
        f"""CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT ({NOW}))""",
        """CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)""",

        f"""CREATE TABLE collections (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            kind TEXT NOT NULL DEFAULT 'collection'
                CHECK (kind IN ('collection', 'binder', 'box', 'deckbox', 'other')),
            is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
            notes TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT ({NOW}))""",
        """CREATE UNIQUE INDEX collections_one_default ON collections(is_default) WHERE is_default = 1""",

        f"""CREATE TABLE collection_items (
            id INTEGER PRIMARY KEY,
            collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE RESTRICT,
            scryfall_id TEXT NOT NULL,              -- the exact printing
            oracle_id TEXT,                         -- the card regardless of printing (when known)
            card_name TEXT NOT NULL,
            set_code TEXT NOT NULL,
            collector_number TEXT NOT NULL,
            finish TEXT NOT NULL DEFAULT 'nonfoil' CHECK (finish IN {_in(FINISHES)}),
            condition TEXT NOT NULL DEFAULT 'near_mint' CHECK (condition IN {_in(CONDITIONS)}),
            language TEXT NOT NULL DEFAULT 'en',
            quantity INTEGER NOT NULL DEFAULT 1 CHECK (quantity >= 0),
            market_price REAL,                      -- per copy, last known (see refresh_prices)
            market_price_currency TEXT NOT NULL DEFAULT 'USD',
            price_updated_at TEXT,
            purchase_price REAL,                    -- per copy, optional
            purchase_currency TEXT,
            notes TEXT,
            added_at TEXT NOT NULL DEFAULT ({NOW}),
            updated_at TEXT NOT NULL DEFAULT ({NOW}),
            UNIQUE (collection_id, scryfall_id, finish, condition, language))""",
        """CREATE INDEX collection_items_name ON collection_items(card_name COLLATE NOCASE)""",
        """CREATE INDEX collection_items_oracle ON collection_items(oracle_id)""",
        """CREATE INDEX collection_items_scryfall ON collection_items(scryfall_id)""",
        f"""CREATE TRIGGER collection_items_touch AFTER UPDATE ON collection_items
            WHEN NEW.updated_at = OLD.updated_at
            BEGIN UPDATE collection_items SET updated_at = {NOW} WHERE id = NEW.id; END""",

        f"""CREATE TABLE decks (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            format TEXT NOT NULL DEFAULT 'commander',
            notes TEXT,
            created_at TEXT NOT NULL DEFAULT ({NOW}),
            updated_at TEXT NOT NULL DEFAULT ({NOW}))""",

        f"""CREATE TABLE deck_cards (
            id INTEGER PRIMARY KEY,
            deck_id INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
            card_name TEXT NOT NULL,
            oracle_id TEXT,
            scryfall_id TEXT,                       -- preferred printing, optional
            quantity INTEGER NOT NULL DEFAULT 1 CHECK (quantity > 0),
            finish TEXT CHECK (finish IS NULL OR finish IN {_in(FINISHES)}),
            role TEXT NOT NULL DEFAULT 'main' CHECK (role IN {_in(DECK_ROLES)}),
            collection_item_id INTEGER REFERENCES collection_items(id) ON DELETE SET NULL,
            added_at TEXT NOT NULL DEFAULT ({NOW}))""",
        """CREATE UNIQUE INDEX deck_one_commander ON deck_cards(deck_id) WHERE role = 'commander'""",
        """CREATE UNIQUE INDEX deck_one_partner ON deck_cards(deck_id) WHERE role = 'partner'""",
        """CREATE INDEX deck_cards_deck ON deck_cards(deck_id)""",
        """CREATE INDEX deck_cards_name ON deck_cards(card_name COLLATE NOCASE)""",
        """CREATE INDEX deck_cards_item ON deck_cards(collection_item_id)""",
        f"""CREATE TRIGGER deck_cards_ins AFTER INSERT ON deck_cards
            BEGIN UPDATE decks SET updated_at = {NOW} WHERE id = NEW.deck_id; END""",
        f"""CREATE TRIGGER deck_cards_upd AFTER UPDATE ON deck_cards
            BEGIN UPDATE decks SET updated_at = {NOW} WHERE id = NEW.deck_id; END""",
        f"""CREATE TRIGGER deck_cards_del AFTER DELETE ON deck_cards
            BEGIN UPDATE decks SET updated_at = {NOW} WHERE id = OLD.deck_id; END""",

        # How many copies of each lot are used by decks, and how many are free.
        """CREATE VIEW collection_item_availability AS
            SELECT ci.id AS collection_item_id, ci.collection_id, ci.card_name, ci.scryfall_id, ci.finish,
                   ci.quantity,
                   COALESCE(SUM(dc.quantity), 0) AS allocated,
                   ci.quantity - COALESCE(SUM(dc.quantity), 0) AS available
            FROM collection_items ci LEFT JOIN deck_cards dc ON dc.collection_item_id = ci.id
            GROUP BY ci.id""",
        # Everything owned, per card name (all printings, finishes, collections).
        """CREATE VIEW owned_cards AS
            SELECT card_name, MAX(oracle_id) AS oracle_id, SUM(quantity) AS copies,
                   COUNT(DISTINCT scryfall_id) AS printings
            FROM collection_items WHERE quantity > 0
            GROUP BY card_name COLLATE NOCASE""",
        """CREATE VIEW deck_summary AS
            SELECT d.id AS deck_id, d.name, d.format, d.created_at, d.updated_at,
                   (SELECT card_name FROM deck_cards WHERE deck_id = d.id AND role = 'commander') AS commander,
                   (SELECT card_name FROM deck_cards WHERE deck_id = d.id AND role = 'partner') AS partner,
                   (SELECT COALESCE(SUM(quantity), 0) FROM deck_cards WHERE deck_id = d.id
                        AND role IN ('commander', 'partner', 'companion', 'main')) AS card_count
            FROM decks d""",

        f"""INSERT INTO collections (name, kind, is_default) VALUES ('{DEFAULT_COLLECTION}', 'collection', 1)""",
    ]),
    (2, "Indexes for browsing large collections; import log", [
        """CREATE INDEX collection_items_coll_name ON collection_items(collection_id, card_name COLLATE NOCASE)""",
        """CREATE INDEX collection_items_added ON collection_items(added_at)""",
        """CREATE INDEX collection_items_price ON collection_items(market_price)""",
        f"""CREATE TABLE import_log (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL,                   -- 'scanner' or 'manabox_csv'
            collection_id INTEGER REFERENCES collections(id) ON DELETE SET NULL,
            cards INTEGER NOT NULL,                 -- physical copies added
            details TEXT,
            created_at TEXT NOT NULL DEFAULT ({NOW}))""",
    ]),
    (3, "Commander decks: physical deck cards, ownership view, deck card counts", [
        # 1 = this card is physically in the deck (it was scanned); 0 = planned / imported
        """ALTER TABLE deck_cards ADD COLUMN physical INTEGER NOT NULL DEFAULT 0""",
        """ALTER TABLE deck_cards ADD COLUMN source TEXT""",  # 'scan', 'manual', 'import', 'duplicate'
        """CREATE INDEX deck_cards_scryfall ON deck_cards(scryfall_id)""",
        # How each deck card is owned - the question Phase 4 asks most.
        """CREATE VIEW deck_card_ownership AS
            SELECT dc.id AS deck_card_id, dc.deck_id, dc.card_name, dc.oracle_id, dc.scryfall_id, dc.finish,
                   dc.quantity, dc.role, dc.physical, dc.collection_item_id,
                   ci.scryfall_id AS owned_scryfall_id, ci.finish AS owned_finish, ci.collection_id,
                   CASE WHEN dc.collection_item_id IS NOT NULL THEN
                            CASE WHEN ci.scryfall_id = dc.scryfall_id AND (dc.finish IS NULL OR ci.finish = dc.finish)
                                     THEN 'exact'
                                 WHEN ci.scryfall_id = dc.scryfall_id THEN 'different_finish'
                                 ELSE 'different_printing' END
                        WHEN dc.physical = 1 THEN 'deck_only'
                        ELSE 'missing' END AS ownership
            FROM deck_cards dc LEFT JOIN collection_items ci ON ci.id = dc.collection_item_id""",
        # Commander decks count commander + partner + main deck (not companion / sideboard / maybe).
        """DROP VIEW deck_summary""",
        """CREATE VIEW deck_summary AS
            SELECT d.id AS deck_id, d.name, d.format, d.notes, d.created_at, d.updated_at,
                   (SELECT card_name FROM deck_cards WHERE deck_id = d.id AND role = 'commander') AS commander,
                   (SELECT scryfall_id FROM deck_cards WHERE deck_id = d.id AND role = 'commander') AS commander_scryfall_id,
                   (SELECT card_name FROM deck_cards WHERE deck_id = d.id AND role = 'partner') AS partner,
                   (SELECT scryfall_id FROM deck_cards WHERE deck_id = d.id AND role = 'partner') AS partner_scryfall_id,
                   (SELECT COALESCE(SUM(quantity), 0) FROM deck_cards WHERE deck_id = d.id
                        AND role IN ('commander', 'partner', 'main')) AS card_count
            FROM decks d""",
    ]),
    (4, "Deck analyser: role cache, analysis settings, analyses, deck snapshots, applied changes", [
        # Card roles worked out by cardroles.py, cached (rebuilt when the classifier or cards.db changes).
        """CREATE TABLE card_role_cache (oracle_id TEXT PRIMARY KEY, name TEXT NOT NULL, roles TEXT NOT NULL)""",
        f"""CREATE TABLE analysis_profiles (
            deck_id INTEGER PRIMARY KEY REFERENCES decks(id) ON DELETE CASCADE,
            mode TEXT NOT NULL DEFAULT 'collection' CHECK (mode IN ('collection', 'all')),
            goal TEXT NOT NULL DEFAULT 'improve' CHECK (goal IN ('casual', 'improve', 'high')),
            tags TEXT,              -- JSON list of deck goals chosen by the user; NULL = use detected
            budget_usd REAL,        -- All Cards mode: total spend allowed; NULL = unlimited
            updated_at TEXT NOT NULL DEFAULT ({NOW}))""",
        f"""CREATE TABLE deck_analyses (
            id INTEGER PRIMARY KEY,
            deck_id INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
            fingerprint TEXT NOT NULL,   -- deck + collection + settings + card data it was based on
            settings TEXT NOT NULL,
            result TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT ({NOW}))""",
        """CREATE INDEX deck_analyses_deck ON deck_analyses(deck_id, id)""",
        f"""CREATE TABLE deck_snapshots (
            id INTEGER PRIMARY KEY,
            deck_id INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
            reason TEXT,
            cards TEXT NOT NULL,         -- JSON copy of the deck's deck_cards rows
            created_at TEXT NOT NULL DEFAULT ({NOW}))""",
        f"""CREATE TABLE applied_changes (
            id INTEGER PRIMARY KEY,
            deck_id INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
            analysis_id INTEGER REFERENCES deck_analyses(id) ON DELETE SET NULL,
            snapshot_id INTEGER REFERENCES deck_snapshots(id) ON DELETE SET NULL,
            swaps TEXT NOT NULL,          -- JSON list of swaps (out / in cards)
            created_at TEXT NOT NULL DEFAULT ({NOW}),
            undone_at TEXT)""",
        """CREATE INDEX applied_changes_deck ON applied_changes(deck_id, id)""",
    ]),
]
LATEST_VERSION = MIGRATIONS[-1][0]


class UserDBError(Exception):
    pass


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def backup_database(path, reason="backup"):
    """Consistent copy of the database (SQLite online backup) into data/backups/."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(BACKUP_DIR, f"{os.path.splitext(os.path.basename(path))[0]}-{stamp}-{reason}.db")
    n = 1
    while os.path.exists(dest):
        dest = dest[:-3] + f"-{n}.db"
        n += 1
    src = sqlite3.connect(path)
    try:
        out = sqlite3.connect(dest)
        with out:
            src.backup(out)
        out.close()
    finally:
        src.close()
    return dest


def migrate(conn, path=None, migrations=None):
    """Bring the schema up to date. Returns the list of versions applied."""
    migrations = migrations or MIGRATIONS
    latest = migrations[-1][0]
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > latest:
        raise UserDBError(f"{path or 'database'} is schema v{current}, newer than this app (v{latest}). "
                          "Update the app; the database was not changed.")
    pending = [m for m in migrations if m[0] > current]
    if not pending:
        return []
    if current > 0 and path and os.path.exists(path):
        backup_database(path, f"before-v{pending[-1][0]}")
    applied = []
    for version, description, statements in pending:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for sql in statements:
                conn.execute(sql)
            conn.execute("INSERT INTO schema_migrations (version, description) VALUES (?, ?)", (version, description))
            conn.execute(f"PRAGMA user_version = {int(version)}")
            conn.execute("COMMIT")
        except Exception as e:
            conn.execute("ROLLBACK")
            raise UserDBError(f"Upgrading the database to v{version} failed, nothing was changed: {e}") from e
        applied.append(version)
    return applied


class UserDB:
    def __init__(self, path=USER_DB_PATH, migrations=None):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        # isolation_level=None: we manage transactions explicitly (see transaction()).
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = FULL")
        try:
            self.applied = migrate(self.conn, path, migrations)
        except BaseException:
            self.conn.close()  # don't leave the file locked when we refuse to open it
            raise

    def close(self):
        self.conn.close()

    @contextmanager
    def transaction(self):
        """All writes inside happen together or not at all."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    @property
    def version(self):
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def backup(self, reason="manual"):
        return backup_database(self.path, reason)

    # ---- collections ----------------------------------------------------------

    def collections(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM collections ORDER BY sort_order, id")]

    def default_collection_id(self):
        return self.conn.execute("SELECT id FROM collections WHERE is_default = 1").fetchone()[0]

    def create_collection(self, name, kind="collection", notes=None):
        with self.transaction() as c:
            return c.execute("INSERT INTO collections (name, kind, notes) VALUES (?, ?, ?)",
                             (name.strip(), kind, notes)).lastrowid

    def rename_collection(self, collection_id, name):
        name = (name or "").strip()
        if not name:
            raise ValueError("a collection needs a name")
        with self.transaction() as c:
            c.execute("UPDATE collections SET name = ? WHERE id = ?", (name, collection_id))

    def collection_counts(self, collection_id):
        r = self.conn.execute("""SELECT COUNT(*), COALESCE(SUM(quantity), 0) FROM collection_items
                                 WHERE collection_id = ? AND quantity > 0""", (collection_id,)).fetchone()
        return dict(lots=r[0], copies=r[1])

    def delete_collection(self, collection_id, contents="refuse", move_to=None):
        """Delete a collection. contents: 'refuse' (only if empty), 'move' (cards go to
        `move_to`) or 'delete' (cards are deleted too - refused if any are in decks).
        The default collection (Main Collection) can never be deleted."""
        with self.transaction() as c:
            row = c.execute("SELECT is_default, name FROM collections WHERE id = ?", (collection_id,)).fetchone()
            if row is None:
                raise ValueError("no such collection")
            if row["is_default"]:
                raise UserDBError(f"{row['name']} is your main collection and can't be deleted")
            items = [r[0] for r in c.execute("SELECT id FROM collection_items WHERE collection_id = ? AND quantity > 0",
                                             (collection_id,))]
            if items and contents == "refuse":
                raise UserDBError(f"{row['name']} still has cards in it")
            if items and contents == "move":
                if move_to is None or move_to == collection_id:
                    raise ValueError("choose another collection to move the cards to")
                for i in items:
                    self._move_lot(c, i, move_to)
            if contents == "delete" or not items:
                used = c.execute("""SELECT COALESCE(SUM(dc.quantity), 0) FROM deck_cards dc
                                    JOIN collection_items ci ON ci.id = dc.collection_item_id
                                    WHERE ci.collection_id = ?""", (collection_id,)).fetchone()[0]
                if used:
                    raise UserDBError(f"{used} card(s) in {row['name']} are used in decks - move them instead")
                c.execute("DELETE FROM collection_items WHERE collection_id = ?", (collection_id,))
            c.execute("DELETE FROM collection_items WHERE collection_id = ? AND quantity = 0", (collection_id,))
            c.execute("DELETE FROM collections WHERE id = ?", (collection_id,))

    def log_import(self, source, collection_id, cards, details=None, _conn=None):
        sql = "INSERT INTO import_log (source, collection_id, cards, details) VALUES (?, ?, ?, ?)"
        if _conn is not None:
            _conn.execute(sql, (source, collection_id, cards, details))
        else:
            with self.transaction() as c:
                c.execute(sql, (source, collection_id, cards, details))

    def collection_id(self, name):
        r = self.conn.execute("SELECT id FROM collections WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
        return r[0] if r else None

    # ---- collection items -------------------------------------------------------

    def add_card(self, scryfall_id, card_name, set_code, collector_number, finish="nonfoil", quantity=1,
                 collection_id=None, condition="near_mint", language="en", market_price=None,
                 purchase_price=None, purchase_currency=None, notes=None, oracle_id=None, _conn=None):
        """Add copies of a printing; adds to the existing lot if there is one. Returns the lot id."""
        if finish not in FINISHES:
            raise ValueError(f"finish must be one of {FINISHES}")
        if condition not in CONDITIONS:
            raise ValueError(f"condition must be one of {CONDITIONS}")
        if quantity < 1:
            raise ValueError("quantity must be at least 1")
        sql = f"""INSERT INTO collection_items
                    (collection_id, scryfall_id, oracle_id, card_name, set_code, collector_number, finish,
                     condition, language, quantity, market_price, price_updated_at, purchase_price,
                     purchase_currency, notes)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CASE WHEN ? IS NULL THEN NULL ELSE {NOW} END, ?, ?, ?)
                  ON CONFLICT (collection_id, scryfall_id, finish, condition, language) DO UPDATE SET
                    quantity = quantity + excluded.quantity,
                    market_price = COALESCE(excluded.market_price, market_price),
                    price_updated_at = COALESCE(excluded.price_updated_at, price_updated_at),
                    oracle_id = COALESCE(oracle_id, excluded.oracle_id),
                    purchase_price = COALESCE(purchase_price, excluded.purchase_price),
                    purchase_currency = COALESCE(purchase_currency, excluded.purchase_currency),
                    notes = COALESCE(notes, excluded.notes)
                  RETURNING id"""
        args = (collection_id or self.default_collection_id(), scryfall_id, oracle_id, card_name, set_code.lower(),
                str(collector_number), finish, condition, language, quantity, market_price, market_price,
                purchase_price, purchase_currency, notes)
        if _conn is not None:
            return _conn.execute(sql, args).fetchone()[0]
        with self.transaction() as c:
            return c.execute(sql, args).fetchone()[0]

    def set_quantity(self, item_id, quantity):
        """Set how many copies are owned. Can't go below the copies used in decks."""
        if quantity < 0:
            raise ValueError("quantity can't be negative")
        with self.transaction() as c:
            self._check_not_below_allocated(c, item_id, quantity)
            c.execute("UPDATE collection_items SET quantity = ? WHERE id = ?", (quantity, item_id))

    def remove_copies(self, item_id, n=1):
        """Take n copies out of a lot (the lot row stays with quantity 0 if it empties)."""
        with self.transaction() as c:
            q = c.execute("SELECT quantity FROM collection_items WHERE id = ?", (item_id,)).fetchone()
            if q is None:
                raise ValueError("no such collection item")
            self._check_not_below_allocated(c, item_id, max(q[0] - n, 0))
            c.execute("UPDATE collection_items SET quantity = MAX(quantity - ?, 0) WHERE id = ?", (n, item_id))

    @staticmethod
    def _allocated(c, item_id):
        return c.execute("SELECT COALESCE(SUM(quantity), 0) FROM deck_cards WHERE collection_item_id = ?",
                         (item_id,)).fetchone()[0]

    def _check_not_below_allocated(self, c, item_id, new_quantity):
        used = self._allocated(c, item_id)
        if new_quantity < used:
            raise UserDBError(f"{used} cop{'y is' if used == 1 else 'ies are'} used in decks - "
                              f"take {'it' if used == 1 else 'them'} out of the deck first")

    _UNSET = object()

    def update_item(self, item_id, quantity=None, finish=None, condition=None,
                    purchase_price=_UNSET, purchase_currency=_UNSET, notes=_UNSET, market_price=_UNSET):
        """Edit a lot. Changing finish/condition to match another lot of the same card in
        the same collection merges the two (quantities added, deck links kept). Returns
        the id of the lot that now holds the cards."""
        with self.transaction() as c:
            item = c.execute("SELECT * FROM collection_items WHERE id = ?", (item_id,)).fetchone()
            if item is None:
                raise ValueError("no such collection item")
            if finish is not None and finish not in FINISHES:
                raise ValueError(f"finish must be one of {FINISHES}")
            if condition is not None and condition not in CONDITIONS:
                raise ValueError(f"condition must be one of {CONDITIONS}")
            if quantity is not None:
                if quantity < 0:
                    raise ValueError("quantity can't be negative")
                self._check_not_below_allocated(c, item_id, quantity)
            sets, args = [], []
            for col, val in (("quantity", quantity), ("finish", None), ("condition", None)):
                if val is not None:
                    sets.append(f"{col} = ?")
                    args.append(val)
            for col, val in (("purchase_price", purchase_price), ("purchase_currency", purchase_currency),
                             ("notes", notes), ("market_price", market_price)):
                if val is not self._UNSET:
                    sets.append(f"{col} = ?")
                    args.append(val)
            if market_price is not self._UNSET:
                sets.append(f"price_updated_at = {NOW}")
            if sets:
                c.execute(f"UPDATE collection_items SET {', '.join(sets)} WHERE id = ?", (*args, item_id))
            new_finish, new_condition = finish or item["finish"], condition or item["condition"]
            if (new_finish, new_condition) != (item["finish"], item["condition"]):
                return self._move_lot(c, item_id, item["collection_id"], new_finish, new_condition)
            return item_id

    def _move_lot(self, c, item_id, collection_id, finish=None, condition=None):
        """Move a lot to (collection, finish, condition), merging into an existing
        identical lot if there is one. Deck links follow the cards. Returns the lot id."""
        item = c.execute("SELECT * FROM collection_items WHERE id = ?", (item_id,)).fetchone()
        finish, condition = finish or item["finish"], condition or item["condition"]
        other = c.execute("""SELECT id FROM collection_items WHERE collection_id = ? AND scryfall_id = ?
                             AND finish = ? AND condition = ? AND language = ? AND id != ?""",
                          (collection_id, item["scryfall_id"], finish, condition, item["language"], item_id)).fetchone()
        if other is None:
            c.execute("UPDATE collection_items SET collection_id = ?, finish = ?, condition = ? WHERE id = ?",
                      (collection_id, finish, condition, item_id))
            return item_id
        target = other[0]
        c.execute("""UPDATE collection_items SET quantity = quantity + ?,
                        purchase_price = COALESCE(purchase_price, ?), purchase_currency = COALESCE(purchase_currency, ?),
                        notes = CASE WHEN notes IS NULL THEN ? WHEN ? IS NULL OR ? = notes THEN notes
                                     ELSE notes || ' / ' || ? END,
                        added_at = MIN(added_at, ?)
                     WHERE id = ?""",
                  (item["quantity"], item["purchase_price"], item["purchase_currency"], item["notes"],
                   item["notes"], item["notes"], item["notes"], item["added_at"], target))
        c.execute("UPDATE deck_cards SET collection_item_id = ? WHERE collection_item_id = ?", (target, item_id))
        c.execute("DELETE FROM collection_items WHERE id = ?", (item_id,))
        return target

    def move_items(self, item_ids, collection_id):
        """Move lots to another collection (merging with identical lots there)."""
        with self.transaction() as c:
            if not c.execute("SELECT 1 FROM collections WHERE id = ?", (collection_id,)).fetchone():
                raise ValueError("no such collection")
            return [self._move_lot(c, i, collection_id) for i in item_ids]

    def delete_item(self, item_id):
        """Remove a lot completely. Refused while any copy is used in a deck."""
        with self.transaction() as c:
            used = self._allocated(c, item_id)
            if used:
                raise UserDBError(f"{used} cop{'y is' if used == 1 else 'ies are'} used in decks - "
                                  "take them out of the deck first")
            c.execute("DELETE FROM collection_items WHERE id = ?", (item_id,))

    def item(self, item_id):
        r = self.conn.execute("SELECT * FROM collection_items WHERE id = ?", (item_id,)).fetchone()
        return dict(r) if r else None

    def items(self, collection_id=None):
        if collection_id is None:
            rows = self.conn.execute("SELECT * FROM collection_items WHERE quantity > 0 ORDER BY card_name")
        else:
            rows = self.conn.execute("SELECT * FROM collection_items WHERE quantity > 0 AND collection_id = ? "
                                     "ORDER BY card_name", (collection_id,))
        return [dict(r) for r in rows]

    def copies_owned(self, card_name):
        """Copies of a card owned across all printings, finishes and collections."""
        r = self.conn.execute("SELECT copies FROM owned_cards WHERE card_name = ? COLLATE NOCASE", (card_name,)).fetchone()
        return r[0] if r else 0

    def printings_owned(self, card_name):
        """Which printings/finishes of a card are owned, where, and how many are free."""
        return [dict(r) for r in self.conn.execute(
            """SELECT ci.id, ci.scryfall_id, ci.set_code, ci.collector_number, ci.finish, ci.condition,
                      ci.quantity, a.allocated, a.available, c.name AS collection
               FROM collection_items ci
               JOIN collections c ON c.id = ci.collection_id
               JOIN collection_item_availability a ON a.collection_item_id = ci.id
               WHERE ci.card_name = ? COLLATE NOCASE AND ci.quantity > 0
               ORDER BY c.sort_order, ci.set_code""", (card_name,))]

    def which_owned(self, card_names):
        """{name: copies} for the given names that are owned (e.g. checking recommendations)."""
        out = {}
        for name in card_names:
            n = self.copies_owned(name)
            if n:
                out[name] = n
        return out

    def collection_value(self, collection_id=None):
        sql = "SELECT COALESCE(SUM(quantity * COALESCE(market_price, 0)), 0) FROM collection_items"
        if collection_id is None:
            return self.conn.execute(sql).fetchone()[0]
        return self.conn.execute(sql + " WHERE collection_id = ?", (collection_id,)).fetchone()[0]

    def refresh_prices(self, cards_db_path=CARDS_DB_PATH):
        """Copy the latest market price for each lot's printing + finish from Scryfall's
        local cards.db (run Update Prices first to get fresh ones). One SQL update, so
        it's quick even for 50,000 cards. Returns the number of lots updated."""
        self.attach_cards_db(cards_db_path)
        price = """(SELECT CAST(COALESCE(CASE collection_items.finish WHEN 'foil' THEN s.usd_foil
                                                         WHEN 'etched' THEN s.usd_etched END, s.usd) AS REAL)
                    FROM scry.cards s WHERE s.id = collection_items.scryfall_id)"""
        with self.transaction() as c:
            cur = c.execute(f"""UPDATE collection_items SET market_price = {price}, price_updated_at = {NOW}
                                WHERE {price} IS NOT NULL""")
            return cur.rowcount

    # ---- decks ------------------------------------------------------------------

    def create_deck(self, name, format="commander", notes=None):
        with self.transaction() as c:
            return c.execute("INSERT INTO decks (name, format, notes) VALUES (?, ?, ?)", (name, format, notes)).lastrowid

    def rename_deck(self, deck_id, name):
        name = (name or "").strip()
        if not name:
            raise ValueError("a deck needs a name")
        with self.transaction() as c:
            c.execute("UPDATE decks SET name = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
                      (name, deck_id))

    def delete_deck(self, deck_id):
        """Delete a deck and its card list. Collection cards are untouched: their deck
        allocations simply disappear with the deck cards (copies become free again)."""
        with self.transaction() as c:
            c.execute("DELETE FROM decks WHERE id = ?", (deck_id,))

    def duplicate_deck(self, deck_id, name):
        """Copy a deck's card list into a new deck. The copy does NOT claim the original's
        physical cards: links to collection copies and 'physically in the deck' are not
        copied (re-link with decks.DeckStore.allocate). Returns the new deck id."""
        with self.transaction() as c:
            src = c.execute("SELECT * FROM decks WHERE id = ?", (deck_id,)).fetchone()
            if src is None:
                raise ValueError("no such deck")
            new = c.execute("INSERT INTO decks (name, format, notes) VALUES (?, ?, ?)",
                            (name, src["format"], src["notes"])).lastrowid
            c.execute("""INSERT INTO deck_cards (deck_id, card_name, oracle_id, scryfall_id, quantity, finish, role,
                                                 collection_item_id, physical, source)
                         SELECT ?, card_name, oracle_id, scryfall_id, quantity, finish, role, NULL, 0, 'duplicate'
                         FROM deck_cards WHERE deck_id = ? ORDER BY id""", (new, deck_id))
            return new

    def deck_card(self, deck_card_id):
        r = self.conn.execute("SELECT * FROM deck_cards WHERE id = ?", (deck_card_id,)).fetchone()
        return dict(r) if r else None

    def remove_deck_card(self, deck_card_id):
        """Take a card out of a deck (its collection copy becomes free again)."""
        with self.transaction() as c:
            c.execute("DELETE FROM deck_cards WHERE id = ?", (deck_card_id,))

    def update_deck_card(self, deck_card_id, quantity=None, finish=_UNSET, scryfall_id=None, card_name=None,
                         oracle_id=_UNSET, physical=None, unlink=False):
        """Edit a deck card. Changing quantity is checked against its linked collection
        copy (can't use more copies than are free); a new printing/finish/name or
        unlink=True drops the link to the collection copy."""
        with self.transaction() as c:
            dc = c.execute("SELECT * FROM deck_cards WHERE id = ?", (deck_card_id,)).fetchone()
            if dc is None:
                raise ValueError("no such deck card")
            sets, args = [], []
            drop_link = unlink
            if scryfall_id is not None and scryfall_id != dc["scryfall_id"]:
                sets.append("scryfall_id = ?")
                args.append(scryfall_id)
                drop_link = True
            if card_name is not None and card_name != dc["card_name"]:
                sets.append("card_name = ?")
                args.append(card_name)
                drop_link = True
            if finish is not self._UNSET and finish != dc["finish"]:
                if finish is not None and finish not in FINISHES:
                    raise ValueError(f"finish must be one of {FINISHES}")
                sets.append("finish = ?")
                args.append(finish)
                drop_link = True
            if oracle_id is not self._UNSET:
                sets.append("oracle_id = ?")
                args.append(oracle_id)
            if physical is not None:
                sets.append("physical = ?")
                args.append(1 if physical else 0)
            if quantity is not None:
                if quantity < 1:
                    raise ValueError("quantity must be at least 1 (remove the card instead)")
                if dc["collection_item_id"] is not None and not drop_link:
                    self._check_available(c, dc["collection_item_id"], quantity, ignore_deck_card=deck_card_id)
                sets.append("quantity = ?")
                args.append(quantity)
            if drop_link and dc["collection_item_id"] is not None:
                sets.append("collection_item_id = NULL")
            if sets:
                c.execute(f"UPDATE deck_cards SET {', '.join(sets)} WHERE id = ?", (*args, deck_card_id))

    def set_role(self, deck_card_id, role):
        """Make a deck card the commander / partner / a main-deck card. The previous
        commander (or partner) becomes a main-deck card."""
        if role not in DECK_ROLES:
            raise ValueError(f"role must be one of {DECK_ROLES}")
        with self.transaction() as c:
            dc = c.execute("SELECT deck_id, quantity FROM deck_cards WHERE id = ?", (deck_card_id,)).fetchone()
            if dc is None:
                raise ValueError("no such deck card")
            if role in ("commander", "partner"):
                c.execute("UPDATE deck_cards SET role = 'main' WHERE deck_id = ? AND role = ?", (dc["deck_id"], role))
                if dc["quantity"] > 1:  # only one copy can be the commander; the rest stay in the deck
                    c.execute("""INSERT INTO deck_cards (deck_id, card_name, oracle_id, scryfall_id, quantity, finish,
                                                         role, physical, source)
                                 SELECT deck_id, card_name, oracle_id, scryfall_id, quantity - 1, finish, 'main',
                                        physical, source FROM deck_cards WHERE id = ?""", (deck_card_id,))
                    c.execute("UPDATE deck_cards SET quantity = 1 WHERE id = ?", (deck_card_id,))
            c.execute("UPDATE deck_cards SET role = ? WHERE id = ?", (role, deck_card_id))

    def deck(self, deck_id):
        r = self.conn.execute("SELECT * FROM deck_summary WHERE deck_id = ?", (deck_id,)).fetchone()
        return dict(r) if r else None

    def decks(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM deck_summary ORDER BY updated_at DESC")]

    def add_deck_card(self, deck_id, card_name, quantity=1, role="main", scryfall_id=None, finish=None,
                      collection_item_id=None, oracle_id=None, physical=False, source=None, _conn=None):
        """Add a card to a deck. collection_item_id = the collection lot the physical card
        comes from (checked: it must have enough free copies). Returns the deck-card id."""
        if role not in DECK_ROLES:
            raise ValueError(f"role must be one of {DECK_ROLES}")

        def run(c):
            if collection_item_id is not None:
                self._check_available(c, collection_item_id, quantity)
            return c.execute(
                """INSERT INTO deck_cards (deck_id, card_name, oracle_id, scryfall_id, quantity, finish, role,
                                           collection_item_id, physical, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (deck_id, card_name, oracle_id, scryfall_id, quantity, finish, role, collection_item_id,
                 1 if physical else 0, source)).lastrowid
        if _conn is not None:
            return run(_conn)
        with self.transaction() as c:
            return run(c)

    def set_commander(self, deck_id, card_name, scryfall_id=None, partner=False, collection_item_id=None,
                      finish=None, oracle_id=None, physical=False, source="manual"):
        """Set the commander (or partner). The previous one is removed from the deck."""
        role = "partner" if partner else "commander"
        with self.transaction() as c:
            c.execute("DELETE FROM deck_cards WHERE deck_id = ? AND role = ?", (deck_id, role))
            if collection_item_id is not None:
                self._check_available(c, collection_item_id, 1)
            return c.execute("INSERT INTO deck_cards (deck_id, card_name, oracle_id, scryfall_id, quantity, finish, "
                             "role, collection_item_id, physical, source) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)",
                             (deck_id, card_name, oracle_id, scryfall_id, finish, role, collection_item_id,
                              1 if physical else 0, source)).lastrowid

    def allocate(self, deck_card_id, collection_item_id):
        """Say which owned physical copy a deck card uses."""
        with self.transaction() as c:
            q = c.execute("SELECT quantity FROM deck_cards WHERE id = ?", (deck_card_id,)).fetchone()
            if not q:
                raise ValueError("no such deck card")
            self._check_available(c, collection_item_id, q[0], ignore_deck_card=deck_card_id)
            c.execute("UPDATE deck_cards SET collection_item_id = ? WHERE id = ?", (collection_item_id, deck_card_id))

    @staticmethod
    def _check_available(c, item_id, n, ignore_deck_card=None):
        r = c.execute("""SELECT ci.quantity - COALESCE((SELECT SUM(quantity) FROM deck_cards
                                                        WHERE collection_item_id = ci.id AND id IS NOT ?), 0)
                         FROM collection_items ci WHERE ci.id = ?""", (ignore_deck_card, item_id)).fetchone()
        if r is None:
            raise ValueError("no such collection item")
        if r[0] < n:
            raise UserDBError(f"only {r[0]} free cop{'y' if r[0] == 1 else 'ies'} of that card - "
                              "the rest are already in other decks")

    def deck_cards(self, deck_id):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM deck_cards WHERE deck_id = ? ORDER BY role, card_name", (deck_id,))]

    def decks_using(self, card_name):
        """Decks that contain a card, with how many copies and whether they're owned copies."""
        return [dict(r) for r in self.conn.execute(
            """SELECT d.id AS deck_id, d.name AS deck, dc.role, dc.quantity, dc.collection_item_id
               FROM deck_cards dc JOIN decks d ON d.id = dc.deck_id
               WHERE dc.card_name = ? COLLATE NOCASE ORDER BY d.name""", (card_name,))]

    def availability(self, item_id):
        r = self.conn.execute("SELECT * FROM collection_item_availability WHERE collection_item_id = ?",
                              (item_id,)).fetchone()
        return dict(r) if r else None

    # ---- deck analyser: settings, history, snapshots ------------------------------------

    def meta_get(self, key, default=None):
        r = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return r[0] if r else default

    def meta_set(self, key, value, _conn=None):
        sql = "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value"
        if _conn is not None:
            _conn.execute(sql, (key, value))
        else:
            with self.transaction() as c:
                c.execute(sql, (key, value))

    def analysis_profile(self, deck_id):
        import json
        r = self.conn.execute("SELECT * FROM analysis_profiles WHERE deck_id = ?", (deck_id,)).fetchone()
        if r is None:
            return dict(deck_id=deck_id, mode="collection", goal="improve", tags=None, budget_usd=None)
        d = dict(r)
        d["tags"] = json.loads(d["tags"]) if d["tags"] else None
        return d

    def save_analysis_profile(self, deck_id, mode=None, goal=None, tags=_UNSET, budget_usd=_UNSET):
        import json
        cur = self.analysis_profile(deck_id)
        mode = mode or cur["mode"]
        goal = goal or cur["goal"]
        tags = cur["tags"] if tags is self._UNSET else tags
        budget = cur["budget_usd"] if budget_usd is self._UNSET else budget_usd
        with self.transaction() as c:
            c.execute(f"""INSERT INTO analysis_profiles (deck_id, mode, goal, tags, budget_usd) VALUES (?, ?, ?, ?, ?)
                          ON CONFLICT (deck_id) DO UPDATE SET mode = excluded.mode, goal = excluded.goal,
                          tags = excluded.tags, budget_usd = excluded.budget_usd, updated_at = {NOW}""",
                      (deck_id, mode, goal, json.dumps(tags) if tags is not None else None, budget))

    def save_analysis(self, deck_id, fingerprint, settings, result):
        import json
        with self.transaction() as c:
            return c.execute("INSERT INTO deck_analyses (deck_id, fingerprint, settings, result) VALUES (?, ?, ?, ?)",
                             (deck_id, fingerprint, json.dumps(settings), json.dumps(result))).lastrowid

    def latest_analysis(self, deck_id):
        import json
        r = self.conn.execute("SELECT * FROM deck_analyses WHERE deck_id = ? ORDER BY id DESC LIMIT 1",
                              (deck_id,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["settings"], d["result"] = json.loads(d["settings"]), json.loads(d["result"])
        return d

    def snapshot_deck(self, deck_id, reason="", _conn=None):
        """Save a copy of the deck's card list (for undo). Returns the snapshot id."""
        import json

        def run(c):
            rows = [dict(r) for r in c.execute("SELECT * FROM deck_cards WHERE deck_id = ? ORDER BY id", (deck_id,))]
            return c.execute("INSERT INTO deck_snapshots (deck_id, reason, cards) VALUES (?, ?, ?)",
                             (deck_id, reason, json.dumps(rows))).lastrowid
        if _conn is not None:
            return run(_conn)
        with self.transaction() as c:
            return run(c)

    def restore_snapshot(self, snapshot_id):
        """Put a deck's card list back exactly as it was in a snapshot. Links to collection
        copies are kept only while those copies are still free (never taken from another
        deck); the rest are left unlinked. Returns the deck id."""
        import json
        with self.transaction() as c:
            snap = c.execute("SELECT * FROM deck_snapshots WHERE id = ?", (snapshot_id,)).fetchone()
            if snap is None:
                raise ValueError("no such snapshot")
            deck_id = snap["deck_id"]
            c.execute("DELETE FROM deck_cards WHERE deck_id = ?", (deck_id,))
            for r in json.loads(snap["cards"]):
                item = r.get("collection_item_id")
                if item is not None:
                    lot = c.execute("SELECT quantity FROM collection_items WHERE id = ?", (item,)).fetchone()
                    used = c.execute("SELECT COALESCE(SUM(quantity), 0) FROM deck_cards WHERE collection_item_id = ?",
                                     (item,)).fetchone()[0]
                    if lot is None or lot[0] - used < r["quantity"]:
                        item = None
                c.execute("""INSERT INTO deck_cards (deck_id, card_name, oracle_id, scryfall_id, quantity, finish, role,
                                                     collection_item_id, physical, source, added_at)
                             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                          (deck_id, r["card_name"], r.get("oracle_id"), r.get("scryfall_id"), r["quantity"],
                           r.get("finish"), r["role"], item, r.get("physical") or 0, r.get("source"),
                           r.get("added_at") or _now()))
            return deck_id

    def log_applied(self, deck_id, analysis_id, snapshot_id, swaps, _conn=None):
        import json
        sql = "INSERT INTO applied_changes (deck_id, analysis_id, snapshot_id, swaps) VALUES (?, ?, ?, ?)"
        args = (deck_id, analysis_id, snapshot_id, json.dumps(swaps))
        if _conn is not None:
            return _conn.execute(sql, args).lastrowid
        with self.transaction() as c:
            return c.execute(sql, args).lastrowid

    def last_applied(self, deck_id):
        import json
        r = self.conn.execute("""SELECT * FROM applied_changes WHERE deck_id = ? AND undone_at IS NULL
                                 ORDER BY id DESC LIMIT 1""", (deck_id,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["swaps"] = json.loads(d["swaps"])
        return d

    def undo_applied(self, applied_id):
        """Restore the deck from the snapshot taken before these changes."""
        r = self.conn.execute("SELECT * FROM applied_changes WHERE id = ?", (applied_id,)).fetchone()
        if r is None or r["undone_at"] or r["snapshot_id"] is None:
            raise UserDBError("those changes can't be undone")
        deck_id = self.restore_snapshot(r["snapshot_id"])
        with self.transaction() as c:
            c.execute(f"UPDATE applied_changes SET undone_at = {NOW} WHERE id = ?", (applied_id,))
        return deck_id

    # ---- joins with Scryfall's card data ------------------------------------------

    def attach_cards_db(self, path=CARDS_DB_PATH):
        """Make Scryfall's cards.db queryable as `scry.cards` (read-only use)."""
        names = [r[1] for r in self.conn.execute("PRAGMA database_list")]
        if "scry" not in names:
            self.conn.execute("ATTACH DATABASE ? AS scry", (path,))
        return self

    def owned_cards_for_commander(self, color_identity, cards_db_path=CARDS_DB_PATH):
        """Owned cards that are Commander-legal and fit a colour identity like 'WUB'.
        Needs cards.db built by the current update_db.py (color_identity / legal_commander
        columns); raises UserDBError if it's an older one."""
        self.attach_cards_db(cards_db_path)
        cols = {r[1] for r in self.conn.execute("PRAGMA scry.table_info(cards)")}
        if not {"color_identity", "legal_commander"} <= cols:
            raise UserDBError("cards.db is too old for this - run Update Prices.bat once")
        allowed = set(color_identity.upper())
        rows = self.conn.execute(
            """SELECT ci.card_name, SUM(ci.quantity) AS copies, MAX(s.color_identity) AS color_identity
               FROM collection_items ci JOIN scry.cards s ON s.id = ci.scryfall_id
               WHERE ci.quantity > 0 AND s.legal_commander = 'legal'
               GROUP BY ci.card_name ORDER BY ci.card_name""").fetchall()
        return [dict(r) for r in rows if set(r["color_identity"] or "") <= allowed]

    # ---- scanner session -> collection (not wired to the UI yet) --------------------

    def import_session(self, entries, card_lookup, collection_id=None, mark=False):
        """Add the scanner's session entries ({id, finish}) to a collection in one
        transaction. card_lookup(scryfall_id) -> cards.db row dict. Returns copies added.
        mark=True sets entry["in_collection"] on each entry that was added (only once the
        transaction has committed), so a second import doesn't add them twice."""
        added, done = 0, []
        collection_id = collection_id or self.default_collection_id()
        with self.transaction() as c:
            for e in entries:
                card = card_lookup(e["id"])
                if not card:
                    continue
                price = {"foil": card.get("usd_foil"), "etched": card.get("usd_etched")}.get(e.get("finish")) or card.get("usd")
                self.add_card(e["id"], card["name"], card["set_code"], card["collector_number"],
                              finish=e.get("finish") or "nonfoil", collection_id=collection_id,
                              language=card.get("lang") or "en", oracle_id=card.get("oracle_id"),
                              market_price=float(price) if price else None, _conn=c)
                added += 1
                done.append(e)
            if added:
                self.log_import("scanner", collection_id, added, _conn=c)
        if mark:
            for e in done:
                e["in_collection"] = True
        return added
