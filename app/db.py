"""SQLite layer for the PokerNow host manager."""
from __future__ import annotations

import os
import secrets as _secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from itertools import groupby
from pathlib import Path
from typing import Any, Iterable, Iterator

from passlib.context import CryptContext

_pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

DATA_DIR = Path(os.environ.get("VNBT_POKER_DATA", "data")).resolve()
DB_PATH = DATA_DIR / "poker.sqlite"

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    pokernow_url  TEXT,
    status        TEXT NOT NULL DEFAULT 'open',  -- open | closed
    auto_ledger   INTEGER NOT NULL DEFAULT 0,    -- 0=manual 1=auto
    started_at    TEXT NOT NULL,
    ended_at      TEXT
);

CREATE TABLE IF NOT EXISTS session_players (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    player_id    INTEGER NOT NULL REFERENCES players(id)  ON DELETE CASCADE,
    seat_name    TEXT,                  -- name as displayed at the table
    UNIQUE(session_id, player_id)
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    player_id   INTEGER NOT NULL REFERENCES players(id)  ON DELETE CASCADE,
    kind        TEXT NOT NULL,          -- buyin | rebuy | cashout | adjust
    amount      REAL NOT NULL,
    note        TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hands (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    hand_number   INTEGER NOT NULL,
    pot_size      REAL,
    board         TEXT,
    started_at    TEXT NOT NULL,
    UNIQUE(session_id, hand_number)
);

CREATE TABLE IF NOT EXISTS hand_winners (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    hand_id       INTEGER NOT NULL REFERENCES hands(id) ON DELETE CASCADE,
    player_name   TEXT NOT NULL,
    amount_won    REAL,
    winner_cards  TEXT,
    winner_hand_desc TEXT
);

CREATE TABLE IF NOT EXISTS stack_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    player_name   TEXT NOT NULL,
    stack         REAL NOT NULL,
    captured_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snap_session_player
    ON stack_snapshots(session_id, player_name, captured_at DESC);

CREATE INDEX IF NOT EXISTS idx_ledger_session
    ON ledger_entries(session_id);

CREATE INDEX IF NOT EXISTS idx_hands_session
    ON hands(session_id, hand_number DESC);

CREATE TABLE IF NOT EXISTS groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id      INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'manager',  -- manager | viewer
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS group_invites (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id   INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    token      TEXT NOT NULL UNIQUE,
    role       TEXT NOT NULL DEFAULT 'viewer',
    used       INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as cx:
        cx.executescript(SCHEMA)
        # Migrations: add columns that may not exist in older DBs.
        existing = {row[1] for row in cx.execute("PRAGMA table_info(sessions)")}
        if "auto_ledger" not in existing:
            cx.execute("ALTER TABLE sessions ADD COLUMN auto_ledger INTEGER NOT NULL DEFAULT 0")
        if "group_id" not in existing:
            cx.execute("ALTER TABLE sessions ADD COLUMN group_id INTEGER REFERENCES groups(id)")
        hw_cols = {row[1] for row in cx.execute("PRAGMA table_info(hand_winners)")}
        if "winner_cards" not in hw_cols:
            cx.execute("ALTER TABLE hand_winners ADD COLUMN winner_cards TEXT")
        if "winner_hand_desc" not in hw_cols:
            cx.execute("ALTER TABLE hand_winners ADD COLUMN winner_hand_desc TEXT")
        # Ensure a default group exists for legacy/existing data.
        if not list(cx.execute("SELECT id FROM groups LIMIT 1")):
            cx.execute(
                "INSERT INTO groups (name, created_at) VALUES ('Default Group', ?)", (now_iso(),)
            )
        # Assign any unscoped sessions to group 1.
        cx.execute("UPDATE sessions SET group_id = 1 WHERE group_id IS NULL")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cx = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES, timeout=10)
    cx.row_factory = sqlite3.Row
    cx.execute("PRAGMA foreign_keys = ON")
    try:
        with _lock:
            yield cx
            cx.commit()
    finally:
        cx.close()


# ---------- helpers ---------------------------------------------------------

def query(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    with connect() as cx:
        return list(cx.execute(sql, tuple(params)))


def query_one(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    with connect() as cx:
        cur = cx.execute(sql, tuple(params))
        return cur.lastrowid or 0


# ---------- domain operations ----------------------------------------------

def get_or_create_player(name: str) -> int:
    name = name.strip()
    if not name:
        raise ValueError("Player name required")
    row = query_one("SELECT id FROM players WHERE name = ?", (name,))
    if row:
        return int(row["id"])
    return execute(
        "INSERT INTO players (name, created_at) VALUES (?, ?)",
        (name, now_iso()),
    )


def add_session_player(session_id: int, player_id: int, seat_name: str | None) -> None:
    with connect() as cx:
        cx.execute(
            "INSERT OR IGNORE INTO session_players (session_id, player_id, seat_name) "
            "VALUES (?, ?, ?)",
            (session_id, player_id, seat_name),
        )
        if seat_name:
            cx.execute(
                "UPDATE session_players SET seat_name = ? "
                "WHERE session_id = ? AND player_id = ?",
                (seat_name, session_id, player_id),
            )


def session_player_summary(session_id: int) -> list[dict[str, Any]]:
    """Per-player ledger totals + most recent stack snapshot for a session."""
    rows = query(
        """
        SELECT
            p.id            AS player_id,
            p.name          AS name,
            sp.seat_name    AS seat_name,
            COALESCE(SUM(CASE WHEN le.kind IN ('buyin','rebuy')      THEN le.amount END), 0) AS bought_in,
            COALESCE(SUM(CASE WHEN le.kind = 'cashout'                THEN le.amount END), 0) AS cashed_out,
            COALESCE(SUM(CASE WHEN le.kind = 'adjust'                 THEN le.amount END), 0) AS adjustments
        FROM session_players sp
        JOIN players p ON p.id = sp.player_id
        LEFT JOIN ledger_entries le
               ON le.session_id = sp.session_id AND le.player_id = sp.player_id
        WHERE sp.session_id = ?
        GROUP BY p.id, p.name, sp.seat_name
        ORDER BY p.name COLLATE NOCASE
        """,
        (session_id,),
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["current_stack"] = latest_stack(session_id, d["seat_name"] or d["name"])
        bought = float(d["bought_in"] or 0)
        cashed = float(d["cashed_out"] or 0)
        adj = float(d["adjustments"] or 0)
        live = float(d["current_stack"] or 0)
        # P&L while still seated = current_stack + cashout - buyin + adjust.
        d["live_pl"] = live + cashed - bought + adj
        # Settled P&L (after cashout, no live stack assumed) = cashout - buyin + adjust
        d["settled_pl"] = cashed - bought + adj
        out.append(d)
    return out


def latest_stack(session_id: int, player_name: str) -> float | None:
    row = query_one(
        "SELECT stack FROM stack_snapshots "
        "WHERE session_id = ? AND player_name = ? "
        "ORDER BY captured_at DESC, id DESC LIMIT 1",
        (session_id, player_name),
    )
    return float(row["stack"]) if row else None


def session_is_auto_ledger(session_id: int) -> bool:
    row = query_one("SELECT auto_ledger FROM sessions WHERE id = ?", (session_id,))
    return bool(row and row["auto_ledger"])


def auto_record_ledger(session_id: int, player_name: str, kind: str, amount: float,
                       note: str = "") -> None:
    """Insert a ledger entry only if the session is in auto mode and `amount` > 0."""
    if amount <= 0:
        return
    pid = get_or_create_player(player_name)
    add_session_player(session_id, pid, player_name)
    execute(
        "INSERT INTO ledger_entries (session_id, player_id, kind, amount, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, pid, kind, amount, note or "auto", now_iso()),
    )


def auto_cashout_all_seated(session_id: int) -> None:
    """Cash out every player who has a stack snapshot but no cashout entry yet (auto mode)."""
    players = query(
        """
        SELECT DISTINCT ss.player_name AS seat_name,
               COALESCE(NULLIF(sp.seat_name,''), p.name) AS stored_seat,
               p.id AS player_id, p.name AS name
        FROM stack_snapshots ss
        JOIN session_players sp ON sp.session_id = ss.session_id
        JOIN players p ON p.id = sp.player_id
        WHERE ss.session_id = ?
          AND (sp.seat_name = ss.player_name OR p.name = ss.player_name)
        """,
        (session_id,),
    )
    for row in players:
        pid = row["player_id"]
        seat = row["seat_name"]
        # Skip if already has a cashout entry
        existing = query_one(
            "SELECT id FROM ledger_entries WHERE session_id=? AND player_id=? AND kind='cashout'",
            (session_id, pid),
        )
        if existing:
            continue
        last = latest_stack(session_id, seat)
        if last and last > 0:
            execute(
                "INSERT INTO ledger_entries (session_id, player_id, kind, amount, note, created_at)"
                " VALUES (?, ?, 'cashout', ?, 'auto-close', ?)",
                (session_id, pid, last, now_iso()),
            )


def lifetime_pl(group_id: int) -> list[dict[str, Any]]:
    rows = query(
        """
        SELECT
            p.id   AS player_id,
            p.name AS name,
            COUNT(DISTINCT le.session_id) AS sessions_played,
            COALESCE(SUM(CASE WHEN le.kind IN ('buyin','rebuy') THEN le.amount END), 0) AS total_in,
            COALESCE(SUM(CASE WHEN le.kind = 'cashout'           THEN le.amount END), 0) AS total_out,
            COALESCE(SUM(CASE WHEN le.kind = 'adjust'            THEN le.amount END), 0) AS total_adj
        FROM players p
        JOIN ledger_entries le ON le.player_id = p.id
        JOIN sessions s ON s.id = le.session_id
        WHERE s.group_id = ?
        GROUP BY p.id, p.name
        ORDER BY (COALESCE(SUM(CASE WHEN le.kind = 'cashout' THEN le.amount END),0)
                - COALESCE(SUM(CASE WHEN le.kind IN ('buyin','rebuy') THEN le.amount END),0)
                + COALESCE(SUM(CASE WHEN le.kind = 'adjust' THEN le.amount END),0)) DESC
        """,
        (group_id,),
    )
    out = []
    for r in rows:
        d = dict(r)
        d["pl"] = float(d["total_out"] or 0) - float(d["total_in"] or 0) + float(d["total_adj"] or 0)
        out.append(d)
    return out


# ---------- auth / tenancy -------------------------------------------------

def has_any_user() -> bool:
    return bool(query_one("SELECT id FROM users LIMIT 1"))


def create_group(name: str) -> int:
    return execute(
        "INSERT INTO groups (name, created_at) VALUES (?, ?)",
        (name.strip() or "My Group", now_iso()),
    )


def create_user(group_id: int, username: str, password: str, role: str = "manager") -> int:
    hashed = _pwd_context.hash(password)
    return execute(
        "INSERT INTO users (group_id, username, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
        (group_id, username.strip(), hashed, role, now_iso()),
    )


def verify_user(username: str, password: str) -> dict[str, Any] | None:
    row = query_one("SELECT * FROM users WHERE username = ?", (username.strip(),))
    if not row:
        _pwd_context.dummy_verify()  # constant-time guard against user enumeration
        return None
    if not _pwd_context.verify(password, row["password_hash"]):
        return None
    return dict(row)


def get_user(user_id: int) -> dict[str, Any] | None:
    row = query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    return dict(row) if row else None


def create_invite(group_id: int, role: str = "viewer") -> str:
    token = _secrets.token_urlsafe(32)
    execute(
        "INSERT INTO group_invites (group_id, token, role, used, created_at) VALUES (?, ?, ?, 0, ?)",
        (group_id, token, role, now_iso()),
    )
    return token


def get_invite(token: str) -> dict[str, Any] | None:
    row = query_one(
        "SELECT gi.*, g.name AS group_name FROM group_invites gi "
        "JOIN groups g ON g.id = gi.group_id "
        "WHERE gi.token = ? AND gi.used = 0",
        (token,),
    )
    return dict(row) if row else None


def consume_invite(token: str) -> None:
    execute("UPDATE group_invites SET used = 1 WHERE token = ?", (token,))


# ---------- trophies / podiums ---------------------------------------------

def session_hand_wins(session_id: int) -> list[dict[str, Any]]:
    """Top players by number of hands won in this session."""
    return [dict(r) for r in query(
        """
        SELECT hw.player_name AS name,
               COUNT(*)       AS hands_won,
               SUM(CASE
                     WHEN hw.amount_won IS NULL THEN COALESCE(h.pot_size, 0)
                     WHEN h.pot_size > 0 AND hw.amount_won > h.pot_size * 1.05 THEN h.pot_size
                     WHEN (h.pot_size IS NULL OR h.pot_size = 0) AND hw.amount_won > 10000 THEN 0
                     ELSE hw.amount_won
                   END) AS total_won
        FROM hand_winners hw
        JOIN hands h ON h.id = hw.hand_id
        WHERE h.session_id = ? AND TRIM(hw.player_name) <> ''
        GROUP BY hw.player_name
        ORDER BY hands_won DESC, total_won DESC
        """,
        (session_id,),
    )]


def lifetime_hand_wins(group_id: int) -> list[dict[str, Any]]:
    """Top players by number of hands won across all sessions in this group."""
    return [dict(r) for r in query(
        """
        SELECT hw.player_name AS name,
               COUNT(*)       AS hands_won,
               SUM(CASE
                     WHEN hw.amount_won IS NULL THEN COALESCE(h.pot_size, 0)
                     WHEN h.pot_size > 0 AND hw.amount_won > h.pot_size * 1.05 THEN h.pot_size
                     WHEN (h.pot_size IS NULL OR h.pot_size = 0) AND hw.amount_won > 10000 THEN 0
                     ELSE hw.amount_won
                   END) AS total_won,
               COUNT(DISTINCT h.session_id) AS sessions_played
        FROM hand_winners hw
        JOIN hands h ON h.id = hw.hand_id
        JOIN sessions s ON s.id = h.session_id
        WHERE s.group_id = ? AND TRIM(hw.player_name) <> ''
        GROUP BY hw.player_name
        ORDER BY hands_won DESC, total_won DESC
        """,
        (group_id,),
    )]


def biggest_hand(session_id: int | None = None, group_id: int | None = None) -> dict[str, Any] | None:
    """Single largest hand win. Falls back to pot size if amount_won is null."""
    # Sanitize: legacy rows stored a player's full stack as amount_won. If the
    # recorded prize is wildly larger than the pot, fall back to pot_size; if
    # there's no pot context and the value is huge, ignore that row entirely.
    sane_amount = (
        "CASE "
        "WHEN hw.amount_won IS NULL THEN h.pot_size "
        "WHEN h.pot_size > 0 AND hw.amount_won > h.pot_size * 1.05 THEN h.pot_size "
        "WHEN (h.pot_size IS NULL OR h.pot_size = 0) AND hw.amount_won > 10000 THEN NULL "
        "ELSE hw.amount_won END"
    )
    if session_id is None:
        rows = query(
            f"""
            SELECT hw.player_name AS name,
                   {sane_amount} AS amount,
                   h.session_id AS session_id,
                   h.hand_number AS hand_number,
                   h.started_at  AS started_at,
                   s.name        AS session_name
            FROM hand_winners hw
            JOIN hands    h ON h.id = hw.hand_id
            JOIN sessions s ON s.id = h.session_id
            WHERE {sane_amount} IS NOT NULL
              AND TRIM(hw.player_name) <> ''
              AND (? IS NULL OR s.group_id = ?)
            ORDER BY amount DESC
            LIMIT 1
            """,
            (group_id, group_id),
        )
    else:
        rows = query(
            f"""
            SELECT hw.player_name AS name,
                   {sane_amount} AS amount,
                   h.session_id AS session_id,
                   h.hand_number AS hand_number,
                   h.started_at  AS started_at,
                   NULL          AS session_name
            FROM hand_winners hw
            JOIN hands h ON h.id = hw.hand_id
            WHERE h.session_id = ?
              AND {sane_amount} IS NOT NULL
              AND TRIM(hw.player_name) <> ''
            ORDER BY amount DESC
            LIMIT 1
            """,
            (session_id,),
        )
    return dict(rows[0]) if rows else None


def biggest_session_win(group_id: int) -> dict[str, Any] | None:
    """Largest net P&L for one player in one (closed) session, ledger-based."""
    rows = query(
        """
        SELECT p.name AS name,
               s.id   AS session_id,
               s.name AS session_name,
               s.ended_at AS ended_at,
               (COALESCE(SUM(CASE WHEN le.kind = 'cashout' THEN le.amount END), 0)
              - COALESCE(SUM(CASE WHEN le.kind IN ('buyin','rebuy') THEN le.amount END), 0)
              + COALESCE(SUM(CASE WHEN le.kind = 'adjust' THEN le.amount END), 0)) AS pl
        FROM ledger_entries le
        JOIN players  p ON p.id = le.player_id
        JOIN sessions s ON s.id = le.session_id
        WHERE s.group_id = ?
        GROUP BY p.id, s.id
        ORDER BY pl DESC
        LIMIT 1
        """,
        (group_id,),
    )
    if not rows:
        return None
    row = dict(rows[0])
    return row if (row.get("pl") or 0) > 0 else None


def session_most_buyins(session_id: int) -> list[dict[str, Any]]:
    """Top players by number of buy-ins + rebuys in this session."""
    return [dict(r) for r in query(
        """
        SELECT p.name      AS name,
               COUNT(*)    AS buyin_count,
               SUM(le.amount) AS total_in
        FROM ledger_entries le
        JOIN players p ON p.id = le.player_id
        WHERE le.session_id = ? AND le.kind IN ('buyin', 'rebuy')
        GROUP BY p.id, p.name
        ORDER BY buyin_count DESC, total_in DESC
        """,
        (session_id,),
    )]


def lifetime_most_buyins(group_id: int) -> list[dict[str, Any]]:
    """Top players by number of buy-ins + rebuys across all sessions in this group."""
    return [dict(r) for r in query(
        """
        SELECT p.name      AS name,
               COUNT(*)    AS buyin_count,
               SUM(le.amount) AS total_in,
               COUNT(DISTINCT le.session_id) AS sessions_played
        FROM ledger_entries le
        JOIN players p ON p.id = le.player_id
        JOIN sessions s ON s.id = le.session_id
        WHERE le.kind IN ('buyin', 'rebuy') AND s.group_id = ?
        GROUP BY p.id, p.name
        ORDER BY buyin_count DESC, total_in DESC
        """,
        (group_id,),
    )]


def session_pl_timeline(session_id: int) -> list[dict[str, Any]]:
    """Return live P&L over time for each player in the session.

    Walks ledger entries and stack snapshots together, ordered by time, and
    emits a (timestamp, live_pl) point per event for each player. Live P&L =
    current_stack + cashouts - buyins/rebuys + adjustments.
    """
    # Map session_players seat_name -> player_name (for snapshot lookups)
    players = query(
        """
        SELECT p.id   AS player_id,
               p.name AS name,
               COALESCE(NULLIF(sp.seat_name, ''), p.name) AS seat_name
        FROM session_players sp
        JOIN players p ON p.id = sp.player_id
        WHERE sp.session_id = ?
        ORDER BY p.name COLLATE NOCASE
        """,
        (session_id,),
    )
    if not players:
        return []

    ledger = query(
        """
        SELECT le.player_id AS player_id, le.kind AS kind,
               le.amount AS amount, le.created_at AS ts
        FROM ledger_entries le
        WHERE le.session_id = ?
        ORDER BY le.created_at ASC, le.id ASC
        """,
        (session_id,),
    )
    snaps = query(
        """
        SELECT player_name AS seat_name, stack AS stack, captured_at AS ts
        FROM stack_snapshots
        WHERE session_id = ?
        ORDER BY captured_at ASC, id ASC
        """,
        (session_id,),
    )

    out: list[dict[str, Any]] = []
    for p in players:
        pid = int(p["player_id"])
        seat = p["seat_name"]
        # Per-player running totals
        bought = 0.0
        cashed = 0.0
        adj = 0.0
        stack = 0.0
        # Merge this player's events
        events: list[tuple[str, str, float]] = []  # (ts, kind, amount/stack)
        for e in ledger:
            if int(e["player_id"]) == pid:
                events.append((e["ts"], f"L:{e['kind']}", float(e["amount"] or 0)))
        for s in snaps:
            if s["seat_name"] == seat:
                events.append((s["ts"], "S", float(s["stack"] or 0)))
        # Sort by time; within a timestamp process snapshots before ledger events.
        events.sort(key=lambda x: (x[0], 0 if x[1] == "S" else 1))
        points: list[dict[str, Any]] = []
        has_ledger_entry = False
        # Group by timestamp so a rebuy snapshot + ledger entry at the same
        # second are collapsed into one point (avoids false P&L spike).
        for ts, grp in groupby(events, key=lambda x: x[0]):
            marker = None
            marker_amount = None
            has_snap = False
            for _, kind, val in grp:
                if kind == "S":
                    stack = val
                    has_snap = True
                elif kind.startswith("L:"):
                    has_ledger_entry = True
                    k = kind.split(":", 1)[1]
                    if k in ("buyin", "rebuy"):
                        bought += val
                        marker = "buyin"
                        marker_amount = val
                    elif k == "cashout":
                        cashed += val
                        marker = "cashout"
                        marker_amount = val
                    elif k == "adjust":
                        adj += val
                        marker = "adjust"
                        marker_amount = val
            if not has_ledger_entry and has_snap:
                continue  # skip pre-buyin snapshots
            pl = stack + cashed - bought + adj
            points.append({"ts": ts, "pl": pl, "marker": marker, "amount": marker_amount})
        if points:
            out.append({"name": p["name"], "points": points})
    return out


