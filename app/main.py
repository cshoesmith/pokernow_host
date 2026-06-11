"""FastAPI app: PokerNow host manager."""
from __future__ import annotations

import logging
import os
import secrets
import time as _time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

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


# ---------------------------------------------------------------------------
# Server-side page-data cache
# Multiple viewers polling every 5 s would each trigger the same heavy DB
# queries.  Cache the expensive per-session context for a short TTL so they
# all share one DB roundtrip.  session / scraper / current_user are always
# fetched fresh (they're cheap or per-user).
# ---------------------------------------------------------------------------
_SESSION_CTX_CACHE: dict[int, tuple[float, dict]] = {}
_SESSION_CTX_TTL = 4.0  # seconds


def _invalidate_session_cache(session_id: int) -> None:
    _SESSION_CTX_CACHE.pop(session_id, None)


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
    scraper_manager.kill_orphan_chromes()
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

SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, https_only=False)


# --------------------------------------------------------------------------
# Auth helpers
# --------------------------------------------------------------------------

class RedirectException(Exception):
    def __init__(self, url: str):
        self.url = url


@app.exception_handler(RedirectException)
async def _redirect_handler(request: Request, exc: RedirectException):
    return RedirectResponse(exc.url, status_code=303)


def get_current_user(request: Request) -> dict:
    """FastAPI dependency: returns the logged-in user dict or redirects."""
    if not db.has_any_user():
        raise RedirectException("/setup")
    user_id = request.session.get("user_id")
    if not user_id:
        raise RedirectException("/login")
    user = db.get_user(user_id)
    if not user:
        request.session.clear()
        raise RedirectException("/login")
    return user


def _require_manager(user: dict) -> None:
    if user["role"] != "manager":
        raise HTTPException(403, "Manager access required")


def _get_session_or_404(session_id: int, group_id: int):
    session = db.query_one(
        "SELECT * FROM sessions WHERE id = ? AND group_id = ?", (session_id, group_id)
    )
    if not session:
        raise HTTPException(404)
    return session


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------

@app.get("/")
def index(request: Request, current_user: dict = Depends(get_current_user)):
    sessions = db.query(
        "SELECT * FROM sessions WHERE group_id = ? ORDER BY status='open' DESC, started_at DESC",
        (current_user["group_id"],),
    )
    flash_invite = request.session.get("flash_invite")
    if flash_invite:
        del request.session["flash_invite"]
    return templates.TemplateResponse(
        request,
        "index.html",
        {"sessions": sessions, "current_user": current_user, "flash_invite": flash_invite},
    )


@app.get("/sessions/{session_id}")
def session_view(session_id: int, request: Request, current_user: dict = Depends(get_current_user)):
    session = _get_session_or_404(session_id, current_user["group_id"])

    cached = _SESSION_CTX_CACHE.get(session_id)
    if cached and (_time.monotonic() - cached[0]) < _SESSION_CTX_TTL:
        ctx = cached[1]
    else:
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

        totals = {
            "bought_in": sum(float(p["bought_in"]) for p in players),
            "cashed_out": sum(float(p["cashed_out"]) for p in players),
            "live_stacks": sum(float(p["current_stack"] or 0) for p in players),
        }
        ctx = {
            "players": players,
            "ledger": ledger,
            "hands": hands,
            "winners_by_hand": winners_by_hand,
            "totals": totals,
            "podium": db.session_hand_wins(session_id)[:3],
            "big_hand": db.biggest_hand(session_id),
            "most_buyins": db.session_most_buyins(session_id)[:3],
            "pl_timeline": db.session_pl_timeline(session_id),
        }
        _SESSION_CTX_CACHE[session_id] = (_time.monotonic(), ctx)

    return templates.TemplateResponse(
        request,
        "session.html",
        {
            **ctx,
            "session": session,                              # always fresh (auth + status)
            "scraper": scraper_manager.status(session_id),  # always fresh (in-memory)
            "current_user": current_user,                   # per-user
        },
    )


@app.get("/players")
def players_view(request: Request, current_user: dict = Depends(get_current_user)):
    gid = current_user["group_id"]
    return templates.TemplateResponse(
        request,
        "players.html",
        {
            "rows": db.lifetime_pl(gid),
            "podium": db.lifetime_hand_wins(gid)[:3],
            "big_hand": db.biggest_hand(None, gid),
            "big_session": db.biggest_session_win(gid),
            "most_buyins": db.lifetime_most_buyins(gid)[:3],
            "current_user": current_user,
        },
    )


@app.get("/sessions/{session_id}/tools")
def tools_view(session_id: int, request: Request, current_user: dict = Depends(get_current_user)):
    session = _get_session_or_404(session_id, current_user["group_id"])
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
            "current_user": current_user,
        },
    )


# --------------------------------------------------------------------------
# Mutations
# --------------------------------------------------------------------------

@app.post("/sessions")
def create_session(request: Request, name: str = Form(...), pokernow_url: str = Form(""),
                   auto_ledger: str = Form(""), headless: str = Form(""),
                   current_user: dict = Depends(get_current_user)):
    _require_manager(current_user)
    table_url = _normalize_pokernow_url(pokernow_url)
    sid = db.execute(
        "INSERT INTO sessions (name, pokernow_url, status, auto_ledger, started_at, group_id) "
        "VALUES (?, ?, 'open', ?, ?, ?)",
        (
            name.strip() or "Untitled session",
            table_url or None,
            1 if auto_ledger else 0,
            db.now_iso(),
            current_user["group_id"],
        ),
    )
    if table_url:
        scraper_manager.start(sid, table_url, headless=bool(headless))
    return RedirectResponse(f"/sessions/{sid}", status_code=303)


@app.post("/sessions/{session_id}/close")
def close_session(session_id: int, current_user: dict = Depends(get_current_user)):
    _require_manager(current_user)
    session = _get_session_or_404(session_id, current_user["group_id"])
    if session["auto_ledger"]:
        db.auto_cashout_all_seated(session_id)
    db.execute(
        "UPDATE sessions SET status='closed', ended_at=? WHERE id=?",
        (db.now_iso(), session_id),
    )
    scraper_manager.stop(session_id)
    _invalidate_session_cache(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@app.post("/sessions/{session_id}/reopen")
def reopen_session(session_id: int, current_user: dict = Depends(get_current_user)):
    _require_manager(current_user)
    _get_session_or_404(session_id, current_user["group_id"])
    db.execute(
        "UPDATE sessions SET status='open', ended_at=NULL WHERE id=?",
        (session_id,),
    )
    _invalidate_session_cache(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@app.post("/sessions/{session_id}/delete")
def delete_session(session_id: int, current_user: dict = Depends(get_current_user)):
    _require_manager(current_user)
    _get_session_or_404(session_id, current_user["group_id"])
    scraper_manager.stop(session_id)
    db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
    return RedirectResponse("/", status_code=303)


@app.post("/sessions/{session_id}/set_auto_ledger")
def set_auto_ledger(session_id: int, auto_ledger: str = Form(""),
                    current_user: dict = Depends(get_current_user)):
    _require_manager(current_user)
    _get_session_or_404(session_id, current_user["group_id"])
    db.execute(
        "UPDATE sessions SET auto_ledger=? WHERE id=?",
        (1 if auto_ledger else 0, session_id),
    )
    return RedirectResponse(f"/sessions/{session_id}/tools", status_code=303)


@app.post("/sessions/{session_id}/players")
def add_player(session_id: int, name: str = Form(...), seat_name: str = Form(""),
               current_user: dict = Depends(get_current_user)):
    _require_manager(current_user)
    _get_session_or_404(session_id, current_user["group_id"])
    pid = db.get_or_create_player(name)
    db.add_session_player(session_id, pid, seat_name.strip() or name.strip())
    _invalidate_session_cache(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@app.post("/sessions/{session_id}/ledger")
def add_ledger(
    session_id: int,
    player_id: int = Form(...),
    kind: str = Form(...),
    amount: float = Form(...),
    note: str = Form(""),
    current_user: dict = Depends(get_current_user),
):
    _require_manager(current_user)
    _get_session_or_404(session_id, current_user["group_id"])
    if kind not in {"buyin", "rebuy", "cashout", "adjust"}:
        raise HTTPException(400, "invalid kind")
    db.execute(
        "INSERT INTO ledger_entries (session_id, player_id, kind, amount, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, player_id, kind, float(amount), note.strip() or None, db.now_iso()),
    )
    _invalidate_session_cache(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@app.post("/sessions/{session_id}/ledger/{entry_id}/delete")
def delete_ledger(session_id: int, entry_id: int,
                  current_user: dict = Depends(get_current_user)):
    _require_manager(current_user)
    _get_session_or_404(session_id, current_user["group_id"])
    db.execute(
        "DELETE FROM ledger_entries WHERE id=? AND session_id=?",
        (entry_id, session_id),
    )
    _invalidate_session_cache(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@app.post("/sessions/{session_id}/scraper/start")
def start_scraper(session_id: int, url: str = Form(""), headless: str = Form(""),
                  current_user: dict = Depends(get_current_user)):
    _require_manager(current_user)
    session = _get_session_or_404(session_id, current_user["group_id"])
    table_url = _normalize_pokernow_url(url.strip() or (session["pokernow_url"] or ""))
    if not table_url:
        raise HTTPException(400, "PokerNow URL or game ID required")
    if table_url != (session["pokernow_url"] or ""):
        db.execute(
            "UPDATE sessions SET pokernow_url=? WHERE id=?",
            (table_url, session_id),
        )
    scraper_manager.start(session_id, table_url, headless=bool(headless))
    _invalidate_session_cache(session_id)
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
def stop_scraper(session_id: int, current_user: dict = Depends(get_current_user)):
    _require_manager(current_user)
    _get_session_or_404(session_id, current_user["group_id"])
    scraper_manager.stop(session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


# --------------------------------------------------------------------------
# Auth routes (no login required)
# --------------------------------------------------------------------------

@app.get("/login")
def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"current_user": None, "error": None})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    user = db.verify_user(username, password)
    if not user:
        return templates.TemplateResponse(
            request, "login.html",
            {"current_user": None, "error": "Invalid username or password"},
            status_code=401,
        )
    request.session["user_id"] = user["id"]
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/setup")
def setup_page(request: Request):
    if db.has_any_user():
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "setup.html", {"current_user": None, "error": None})


@app.post("/setup")
def setup(
    request: Request,
    group_name: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(...),
):
    if db.has_any_user():
        return RedirectResponse("/", status_code=303)
    if password != confirm:
        return templates.TemplateResponse(
            request, "setup.html",
            {"current_user": None, "error": "Passwords do not match"},
            status_code=400,
        )
    if len(password) < 8:
        return templates.TemplateResponse(
            request, "setup.html",
            {"current_user": None, "error": "Password must be at least 8 characters"},
            status_code=400,
        )
    # Use the default group created by init_db (id=1), update its name.
    existing = db.query_one("SELECT id FROM groups WHERE id = 1")
    if existing:
        db.execute("UPDATE groups SET name = ? WHERE id = 1", (group_name.strip() or "My Group",))
        gid = 1
    else:
        gid = db.create_group(group_name)
    db.create_user(gid, username, password, "manager")
    user = db.verify_user(username, password)
    request.session["user_id"] = user["id"]
    return RedirectResponse("/", status_code=303)


@app.get("/invite/{token}")
def invite_page(token: str, request: Request):
    invite = db.get_invite(token)
    if not invite:
        raise HTTPException(404, "Invite not found or already used")
    return templates.TemplateResponse(
        request, "invite.html",
        {"current_user": None, "invite": invite, "token": token, "error": None},
    )


@app.post("/invite/{token}")
def accept_invite(
    token: str,
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(...),
):
    invite = db.get_invite(token)
    if not invite:
        raise HTTPException(404, "Invite not found or already used")
    if password != confirm:
        return templates.TemplateResponse(
            request, "invite.html",
            {"current_user": None, "invite": invite, "token": token, "error": "Passwords do not match"},
            status_code=400,
        )
    if len(password) < 8:
        return templates.TemplateResponse(
            request, "invite.html",
            {"current_user": None, "invite": invite, "token": token, "error": "Password must be at least 8 characters"},
            status_code=400,
        )
    try:
        uid = db.create_user(invite["group_id"], username, password, invite["role"])
    except Exception:
        return templates.TemplateResponse(
            request, "invite.html",
            {"current_user": None, "invite": invite, "token": token, "error": "Username already taken"},
            status_code=400,
        )
    db.consume_invite(token)
    request.session["user_id"] = uid
    return RedirectResponse("/", status_code=303)


# --------------------------------------------------------------------------
# Settings (manager only)
# --------------------------------------------------------------------------

@app.get("/settings")
def settings_page(request: Request, current_user: dict = Depends(get_current_user)):
    _require_manager(current_user)
    flash_invite = request.session.get("flash_invite")
    if flash_invite:
        del request.session["flash_invite"]
    return templates.TemplateResponse(
        request, "settings.html",
        {"current_user": current_user, "flash_invite": flash_invite},
    )


@app.post("/settings/invite")
def create_invite_link(
    request: Request,
    role: str = Form("viewer"),
    current_user: dict = Depends(get_current_user),
):
    _require_manager(current_user)
    token = db.create_invite(current_user["group_id"], role)
    request.session["flash_invite"] = token
    return RedirectResponse("/settings", status_code=303)
