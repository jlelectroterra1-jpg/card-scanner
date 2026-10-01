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
        if quantity < 0:
            raise ValueError("quantity can't be negative")
        with self.transaction() as c:
            c.execute("UPDATE collection_items SET quantity = ? WHERE id = ?", (quantity, item_id))

    def remove_copies(self, item_id, n=1):
        """Take n copies out of a lot (the lot row stays with quantity 0 if it empties)."""
        with self.transaction() as c:
            c.execute("UPDATE collection_items SET quantity = MAX(quantity - ?, 0) WHERE id = ?", (n, item_id))

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
        """Update market prices of every lot from Scryfall's cards.db. Returns rows updated."""
        rows = self.conn.execute("SELECT id, scryfall_id, finish FROM collection_items").fetchall()
        src = sqlite3.connect(cards_db_path)
        try:
            updates = []
            for r in rows:
                p = src.execute("SELECT usd, usd_foil, usd_etched FROM cards WHERE id = ?", (r["scryfall_id"],)).fetchone()
                if not p:
                    continue
                usd, foil, etched = p
                price = {"foil": foil, "etched": etched}.get(r["finish"]) or usd
                if price is not None:
                    updates.append((float(price), r["id"]))
        finally:
            src.close()
        with self.transaction() as c:
            c.executemany(f"UPDATE collection_items SET market_price = ?, price_updated_at = {NOW} WHERE id = ?", updates)
        return len(updates)

    # ---- decks ------------------------------------------------------------------

    def create_deck(self, name, format="commander", notes=None):
        with self.transaction() as c:
            return c.execute("INSERT INTO decks (name, format, notes) VALUES (?, ?, ?)", (name, format, notes)).lastrowid

    def deck(self, deck_id):
        r = self.conn.execute("SELECT * FROM deck_summary WHERE deck_id = ?", (deck_id,)).fetchone()
        return dict(r) if r else None

    def decks(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM deck_summary ORDER BY updated_at DESC")]

    def add_deck_card(self, deck_id, card_name, quantity=1, role="main", scryfall_id=None, finish=None,
                      collection_item_id=None, oracle_id=None):
        """Add a card to a deck. collection_item_id = the collection lot the physical card
        comes from (checked: it must have enough free copies). Returns the deck-card id."""
        if role not in DECK_ROLES:
            raise ValueError(f"role must be one of {DECK_ROLES}")
        with self.transaction() as c:
            if collection_item_id is not None:
                self._check_available(c, collection_item_id, quantity)
            return c.execute(
                """INSERT INTO deck_cards (deck_id, card_name, oracle_id, scryfall_id, quantity, finish, role,
                                           collection_item_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (deck_id, card_name, oracle_id, scryfall_id, quantity, finish, role, collection_item_id)).lastrowid

    def set_commander(self, deck_id, card_name, scryfall_id=None, partner=False, collection_item_id=None):
        role = "partner" if partner else "commander"
        with self.transaction() as c:
            c.execute("DELETE FROM deck_cards WHERE deck_id = ? AND role = ?", (deck_id, role))
            if collection_item_id is not None:
                self._check_available(c, collection_item_id, 1)
            return c.execute("INSERT INTO deck_cards (deck_id, card_name, scryfall_id, quantity, role, "
                             "collection_item_id) VALUES (?, ?, ?, 1, ?, ?)",
                             (deck_id, card_name, scryfall_id, role, collection_item_id)).lastrowid

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

    def import_session(self, entries, card_lookup, collection_id=None):
        """Add the scanner's session entries ({id, finish}) to a collection in one
        transaction. card_lookup(scryfall_id) -> cards.db row dict. Returns copies added."""
        added = 0
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
        return added
