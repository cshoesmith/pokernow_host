"""FastAPI app: PokerNow host manager."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import db
from .scraper import manager as scraper_manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["money"] = lambda v: ("-" if (v or 0) < 0 else "") + f"${abs(float(v or 0)):,.2f}"


def _localtime_filter(value):
    """Render a UTC ISO timestamp as a <time> tag the browser script will localize."""
    if not value:
        return ""
    from markupsafe import Markup, escape

    s = escape(str(value))
    return Markup(f'<time class="lt" datetime="{s}">{s}</time>')


templates.env.filters["localtime"] = _localtime_filter


def _cards_filter(board: str) -> list[dict]:
    """Parse 'Rank of Suit ...' board string into a list of card dicts for rendering."""
    if not board:
        return []
    rank_abbr = {
        'ace': 'A', 'king': 'K', 'queen': 'Q', 'jack': 'J',
        '2': '2', '3': '3', '4': '4', '5': '5', '6': '6',
        '7': '7', '8': '8', '9': '9', '10': '10',
    }
    suit_sym = {'hearts': '\u2665', 'diamonds': '\u2666', 'clubs': '\u2663', 'spades': '\u2660'}
    suit_red = {'hearts', 'diamonds'}
    cards = []
    tokens = board.strip().lower().split()
    i = 0
    while i + 2 <= len(tokens) - 1:
        if tokens[i + 1] == 'of':
            rank = rank_abbr.get(tokens[i], tokens[i].upper())
            suit_raw = tokens[i + 2]
            cards.append({'rank': rank, 'suit': suit_sym.get(suit_raw, '?'), 'red': suit_raw in suit_red})
            i += 3
        else:
            i += 1
    return cards


templates.env.filters["cards"] = _cards_filter


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    # Auto-resume scrapers for open sessions that have a PokerNow URL.
    open_sessions = db.query(
        "SELECT id, pokernow_url FROM sessions WHERE status='open' AND pokernow_url IS NOT NULL"
    )
    for s in open_sessions:
        logging.info("auto-resuming scraper for session %s (%s)", s["id"], s["pokernow_url"])
        scraper_manager.start(s["id"], s["pokernow_url"])
    yield
    scraper_manager.stop_all()


app = FastAPI(title="PokerNow Host Manager", lifespan=lifespan)


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------

@app.get("/")
def index(request: Request):
    sessions = db.query(
        "SELECT * FROM sessions ORDER BY status='open' DESC, started_at DESC"
    )
    return templates.TemplateResponse(
        request,
        "index.html",
        {"sessions": sessions},
    )


@app.get("/sessions/{session_id}")
def session_view(session_id: int, request: Request):
    session = db.query_one("SELECT * FROM sessions WHERE id = ?", (session_id,))
    if not session:
        raise HTTPException(404)
    players = db.session_player_summary(session_id)
    ledger = db.query(
        """
        SELECT le.*, p.name AS player_name
        FROM ledger_entries le
        JOIN players p ON p.id = le.player_id
        WHERE le.session_id = ?
        ORDER BY le.created_at DESC
        """,
        (session_id,),
    )
    hands = db.query(
        "SELECT * FROM hands WHERE session_id = ? ORDER BY hand_number DESC LIMIT 25",
        (session_id,),
    )
    hand_ids = [h["id"] for h in hands]
    winners_by_hand: dict[int, list[dict]] = {hid: [] for hid in hand_ids}
    if hand_ids:
        placeholders = ",".join("?" * len(hand_ids))
        rows = db.query(
            f"SELECT * FROM hand_winners WHERE hand_id IN ({placeholders})", hand_ids
        )
        for r in rows:
            winners_by_hand[r["hand_id"]].append(dict(r))

    scraper_status = scraper_manager.status(session_id)
    totals = {
        "bought_in": sum(float(p["bought_in"]) for p in players),
        "cashed_out": sum(float(p["cashed_out"]) for p in players),
        "live_stacks": sum(float(p["current_stack"] or 0) for p in players),
    }
    podium = db.session_hand_wins(session_id)[:3]
    big_hand = db.biggest_hand(session_id)
    return templates.TemplateResponse(
        request,
        "session.html",
        {
            "session": session,
            "players": players,
            "ledger": ledger,
            "hands": hands,
            "winners_by_hand": winners_by_hand,
            "scraper": scraper_status,
            "totals": totals,
            "podium": podium,
            "big_hand": big_hand,
            "pl_timeline": db.session_pl_timeline(session_id),
        },
    )


@app.get("/players")
def players_view(request: Request):
    return templates.TemplateResponse(
        request,
        "players.html",
        {
            "rows": db.lifetime_pl(),
            "podium": db.lifetime_hand_wins()[:3],
            "big_hand": db.biggest_hand(None),
            "big_session": db.biggest_session_win(),
        },
    )


@app.get("/sessions/{session_id}/tools")
def tools_view(session_id: int, request: Request):
    session = db.query_one("SELECT * FROM sessions WHERE id = ?", (session_id,))
    if not session:
        raise HTTPException(404)
    players = db.session_player_summary(session_id)
    ledger = db.query(
        """
        SELECT le.*, p.name AS player_name
        FROM ledger_entries le
        JOIN players p ON p.id = le.player_id
        WHERE le.session_id = ?
        ORDER BY le.created_at DESC
        """,
        (session_id,),
    )
    return templates.TemplateResponse(
        request,
        "tools.html",
        {
            "session": session,
            "players": players,
            "ledger": ledger,
            "scraper": scraper_manager.status(session_id),
        },
    )


# --------------------------------------------------------------------------
# Mutations
# --------------------------------------------------------------------------

@app.post("/sessions")
def create_session(name: str = Form(...), pokernow_url: str = Form(""),
                   auto_ledger: str = Form(""), headless: str = Form("")):
    table_url = _normalize_pokernow_url(pokernow_url)
    sid = db.execute(
        "INSERT INTO sessions (name, pokernow_url, status, auto_ledger, started_at) "
        "VALUES (?, ?, 'open', ?, ?)",
        (
            name.strip() or "Untitled session",
            table_url or None,
            1 if auto_ledger else 0,
            db.now_iso(),
        ),
    )
    if table_url:
        scraper_manager.start(sid, table_url, headless=bool(headless))
    return RedirectResponse(f"/sessions/{sid}", status_code=303)


@app.post("/sessions/{session_id}/close")
def close_session(session_id: int):
    session = db.query_one("SELECT * FROM sessions WHERE id = ?", (session_id,))
    if session and session["auto_ledger"]:
        db.auto_cashout_all_seated(session_id)
    db.execute(
        "UPDATE sessions SET status='closed', ended_at=? WHERE id=?",
        (db.now_iso(), session_id),
    )
    scraper_manager.stop(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@app.post("/sessions/{session_id}/reopen")
def reopen_session(session_id: int):
    db.execute(
        "UPDATE sessions SET status='open', ended_at=NULL WHERE id=?",
        (session_id,),
    )
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@app.post("/sessions/{session_id}/delete")
def delete_session(session_id: int):
    scraper_manager.stop(session_id)
    db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
    return RedirectResponse("/", status_code=303)


@app.post("/sessions/{session_id}/set_auto_ledger")
def set_auto_ledger(session_id: int, auto_ledger: str = Form("")):
    db.execute(
        "UPDATE sessions SET auto_ledger=? WHERE id=?",
        (1 if auto_ledger else 0, session_id),
    )
    return RedirectResponse(f"/sessions/{session_id}/tools", status_code=303)


@app.post("/sessions/{session_id}/players")
def add_player(session_id: int, name: str = Form(...), seat_name: str = Form("")):
    pid = db.get_or_create_player(name)
    db.add_session_player(session_id, pid, seat_name.strip() or name.strip())
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@app.post("/sessions/{session_id}/ledger")
def add_ledger(
    session_id: int,
    player_id: int = Form(...),
    kind: str = Form(...),
    amount: float = Form(...),
    note: str = Form(""),
):
    if kind not in {"buyin", "rebuy", "cashout", "adjust"}:
        raise HTTPException(400, "invalid kind")
    db.execute(
        "INSERT INTO ledger_entries (session_id, player_id, kind, amount, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, player_id, kind, float(amount), note.strip() or None, db.now_iso()),
    )
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@app.post("/sessions/{session_id}/ledger/{entry_id}/delete")
def delete_ledger(session_id: int, entry_id: int):
    db.execute(
        "DELETE FROM ledger_entries WHERE id=? AND session_id=?",
        (entry_id, session_id),
    )
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@app.post("/sessions/{session_id}/scraper/start")
def start_scraper(session_id: int, url: str = Form(""), headless: str = Form("")):
    session = db.query_one("SELECT * FROM sessions WHERE id=?", (session_id,))
    if not session:
        raise HTTPException(404)
    table_url = _normalize_pokernow_url(url.strip() or (session["pokernow_url"] or ""))
    if not table_url:
        raise HTTPException(400, "PokerNow URL or game ID required")
    if table_url != (session["pokernow_url"] or ""):
        db.execute(
            "UPDATE sessions SET pokernow_url=? WHERE id=?",
            (table_url, session_id),
        )
    scraper_manager.start(session_id, table_url, headless=bool(headless))
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


def _normalize_pokernow_url(value: str) -> str:
    """Accept full URL, /games/<id>, or bare game id."""
    v = (value or "").strip()
    if not v:
        return ""
    if v.startswith("http://") or v.startswith("https://"):
        return v
    # Strip any leading path fragments
    if "/games/" in v:
        v = v.split("/games/", 1)[1]
    v = v.strip("/").split("?", 1)[0]
    if not v:
        return ""
    return f"https://www.pokernow.club/games/{v}"


@app.post("/sessions/{session_id}/scraper/stop")
def stop_scraper(session_id: int):
    scraper_manager.stop(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)
