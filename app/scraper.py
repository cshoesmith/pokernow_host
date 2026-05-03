"""Background Selenium scraper that mirrors a live PokerNow.club table into the DB.

Wraps the Zehmosu/PokerNow library (`pip install PokerNow`).
One worker per session_id; manage via ScraperManager.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any

from . import db

log = logging.getLogger("scraper")


@dataclass
class ScraperStatus:
    running: bool = False
    last_error: str | None = None
    last_poll_at: str | None = None
    hands_recorded: int = 0
    snapshots_recorded: int = 0
    players_seen: list[str] = field(default_factory=list)


class _SessionScraper(threading.Thread):
    """Polls a PokerNow table and writes snapshots / hands to SQLite."""

    POLL_SECONDS = 3.0

    def __init__(self, session_id: int, url: str, cookie_path: str | None = None,
                 headless: bool = False) -> None:
        super().__init__(name=f"scraper-s{session_id}", daemon=True)
        self.session_id = session_id
        self.url = url
        self.cookie_path = cookie_path or f"data/cookies_session_{session_id}.pkl"
        self.headless = headless
        self.status = ScraperStatus()
        self._stop = threading.Event()
        self._driver = None
        self._client = None
        self._last_winners_signature: str | None = None
        self._last_stacks: dict[str, float] = {}
        # Peak pot value seen since the last hand was recorded.
        self._peak_pot: float = 0.0
        # Auto-ledger state
        self._players_at_table: set[str] = set()   # who we saw last poll
        self._last_hand_winners: set[str] = set()  # names who won most recent hand (stack rise expected)

    # -- lifecycle -----------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:  # noqa: C901 - pragmatic
        try:
            self._init_driver()
        except Exception as exc:  # browser / library missing, etc.
            self.status.last_error = f"init failed: {exc}"
            log.exception("scraper init failed for session %s", self.session_id)
            self.status.running = False
            return

        self.status.running = True
        try:
            while not self._stop.is_set():
                try:
                    self._poll_once()
                    self.status.last_error = None
                except Exception as exc:
                    self.status.last_error = f"{exc.__class__.__name__}: {exc}"
                    log.warning("poll error: %s\n%s", exc, traceback.format_exc())
                self._stop.wait(self.POLL_SECONDS)
        finally:
            self.status.running = False
            self._teardown_driver()

    # -- selenium setup ------------------------------------------------------

    def _init_driver(self) -> None:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from PokerNow import PokerClient  # type: ignore

        opts = Options()
        if self.headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--window-size=1280,900")

        self._driver = webdriver.Chrome(options=opts)
        self._client = PokerClient(self._driver, cookie_path=self.cookie_path)
        # Open table directly. If not logged in, the user can complete login
        # in the visible window; subsequent polls will resume.
        self._client.navigate(self.url)
        time.sleep(2)

    def _teardown_driver(self) -> None:
        try:
            if self._client and getattr(self._client, "cookie_manager", None):
                self._client.cookie_manager.save_cookies()
        except Exception:
            pass
        try:
            if self._driver:
                self._driver.quit()
        except Exception:
            pass

    # -- polling -------------------------------------------------------------

    def _poll_once(self) -> None:
        assert self._client is not None
        gs: Any = self._client.game_state_manager.get_game_state()
        self.status.last_poll_at = db.now_iso()
        auto = db.session_is_auto_ledger(self.session_id)

        # 1. Stack snapshots + auto buy-in / rebuy detection
        pot_now = _coerce_number(getattr(gs, "pot_size", None)) or 0.0
        between_hands = (pot_now == 0 and self._peak_pot == 0)

        names_seen: list[str] = []
        current_names: set[str] = set()
        for pl in getattr(gs, "players", []) or []:
            name = (getattr(pl, "name", "") or "").strip()
            if not name:
                continue
            names_seen.append(name)
            current_names.add(name)
            # Auto-discover: ensure this seat is registered to the session.
            try:
                pid = db.get_or_create_player(name)
                db.add_session_player(self.session_id, pid, name)
            except Exception:
                pass
            stack_raw = getattr(pl, "stack", None)
            stack = _coerce_number(stack_raw)
            if stack is None:
                continue

            if auto:
                prev = self._last_stacks.get(name)
                if prev is None:
                    # First time we see this player — record buy-in.
                    if stack > 0:
                        db.auto_record_ledger(self.session_id, name, "buyin", stack,
                                              note="auto-buyin")
                        log.info("auto buyin: %s %.0f", name, stack)
                elif stack > prev and between_hands and name not in self._last_hand_winners:
                    # Stack rose between hands and this player didn't just win — rebuy.
                    db.auto_record_ledger(self.session_id, name, "rebuy", stack - prev,
                                          note="auto-rebuy")
                    log.info("auto rebuy: %s +%.0f", name, stack - prev)

            if self._last_stacks.get(name) == stack:
                continue
            self._last_stacks[name] = stack
            db.execute(
                "INSERT INTO stack_snapshots (session_id, player_name, stack, captured_at) "
                "VALUES (?, ?, ?, ?)",
                (self.session_id, name, stack, db.now_iso()),
            )
            self.status.snapshots_recorded += 1

        if names_seen:
            self.status.players_seen = sorted(set(names_seen))

        # Auto cash-out players who left the table this poll.
        if auto:
            left = self._players_at_table - current_names
            for name in left:
                last = self._last_stacks.get(name)
                if last and last > 0:
                    db.auto_record_ledger(self.session_id, name, "cashout", last,
                                          note="auto-cashout")
                    log.info("auto cashout (left table): %s %.0f", name, last)
        self._players_at_table = current_names

        # 2. Track running peak pot for this hand.
        if pot_now > self._peak_pot:
            self._peak_pot = pot_now

        # 3. Hand / winner detection — when winners change, log a new hand.
        winners = getattr(gs, "winners", None) or []
        signature = _winners_signature(winners)
        if signature and signature != self._last_winners_signature:
            self._last_winners_signature = signature
            self._last_hand_winners = {
                str(w.get("name", "")).strip() for w in winners if w.get("name")
            }
            self._record_hand(gs, winners)
            self._peak_pot = 0.0  # reset for the next hand

    def _record_hand(self, gs: Any, winners: list[dict[str, Any]]) -> None:
        community = getattr(gs, "community_cards", []) or []
        board = " ".join(str(c) for c in community) if community else None
        # Use the peak pot observed during the hand; pot is $0 by the time
        # the winners banner shows.
        pot = self._peak_pot if self._peak_pot > 0 else _coerce_number(getattr(gs, "pot_size", None))

        # Build a name→cards/hand-desc map from current player state (visible at showdown).
        player_cards: dict[str, str] = {}
        player_hand_desc: dict[str, str] = {}
        for pl in getattr(gs, "players", []) or []:
            name = (getattr(pl, "name", "") or "").strip()
            cards = getattr(pl, "cards", None) or []
            if name and cards:
                player_cards[name] = " | ".join(str(c) for c in cards)
            hand_msg = (getattr(pl, "hand_message", "") or "").strip()
            if name and hand_msg:
                player_hand_desc[name] = hand_msg

        with db.connect() as cx:
            row = cx.execute(
                "SELECT COALESCE(MAX(hand_number), 0) AS m FROM hands WHERE session_id = ?",
                (self.session_id,),
            ).fetchone()
            next_no = int(row["m"]) + 1
            cur = cx.execute(
                "INSERT INTO hands (session_id, hand_number, pot_size, board, started_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (self.session_id, next_no, pot, board, db.now_iso()),
            )
            hand_id = cur.lastrowid
            for w in winners:
                wname = str(w.get("name", "")).strip()
                wdesc = player_hand_desc.get(wname)
                wcards = player_cards.get(wname)
                # No cards visible = everyone folded
                if not wdesc and not wcards:
                    wdesc = "All folded"
                cx.execute(
                    "INSERT INTO hand_winners (hand_id, player_name, amount_won, winner_cards, winner_hand_desc) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        hand_id,
                        wname,
                        _parse_winner_prize(w.get("stack_info")),
                        wcards,
                        wdesc,
                    ),
                )
        self.status.hands_recorded += 1


def _coerce_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "").replace("$", "")
    # Strip parens and trailing labels.
    keep = []
    for ch in s:
        if ch.isdigit() or ch in ".-":
            keep.append(ch)
    cleaned = "".join(keep)
    try:
        return float(cleaned) if cleaned not in ("", "-", ".") else None
    except ValueError:
        return None


def _winners_signature(winners: list[dict[str, Any]]) -> str:
    if not winners:
        return ""
    parts = sorted(
        f"{(w.get('name') or '').strip()}|{(w.get('stack_info') or '')}" for w in winners
    )
    return "::".join(parts)


def _parse_winner_prize(stack_info: Any) -> float | None:
    """PokerNow returns winners as e.g. '3509560 (+5000)'. The '(+...)' value
    is the prize. If absent we return None rather than the raw stack."""
    if stack_info is None:
        return None
    text = str(stack_info)
    if "(+" in text:
        inside = text.split("(+", 1)[1].split(")", 1)[0]
        return _coerce_number(inside)
    return None


# ---------------------------------------------------------------------------
# Manager (singleton)
# ---------------------------------------------------------------------------

class ScraperManager:
    WATCHDOG_INTERVAL = 15.0  # seconds between liveness checks

    def __init__(self) -> None:
        self._workers: dict[int, _SessionScraper] = {}
        self._lock = threading.Lock()
        self._manually_stopped: set[int] = set()  # sessions the user explicitly stopped
        self._watchdog = threading.Thread(target=self._watchdog_loop,
                                          name="scraper-watchdog", daemon=True)
        self._watchdog.start()

    def _watchdog_loop(self) -> None:
        while True:
            time.sleep(self.WATCHDOG_INTERVAL)
            with self._lock:
                dead = [
                    (sid, w) for sid, w in self._workers.items()
                    if not w.is_alive() and sid not in self._manually_stopped
                ]
            for sid, w in dead:
                log.warning("watchdog: scraper for session %s died — restarting", sid)
                new_worker = _SessionScraper(sid, w.url, headless=w.headless)
                with self._lock:
                    self._workers[sid] = new_worker
                new_worker.start()

    def start(self, session_id: int, url: str, headless: bool = False) -> ScraperStatus:
        with self._lock:
            self._manually_stopped.discard(session_id)
            existing = self._workers.get(session_id)
            if existing and existing.is_alive():
                return existing.status
            worker = _SessionScraper(session_id, url, headless=headless)
            self._workers[session_id] = worker
            worker.start()
            return worker.status

    def stop(self, session_id: int) -> None:
        with self._lock:
            self._manually_stopped.add(session_id)
            worker = self._workers.get(session_id)
        if worker:
            worker.stop()
            worker.join(timeout=10)

    def status(self, session_id: int) -> ScraperStatus | None:
        worker = self._workers.get(session_id)
        return worker.status if worker else None

    def stop_all(self) -> None:
        with self._lock:
            workers = list(self._workers.values())
        for w in workers:
            w.stop()
        for w in workers:
            w.join(timeout=10)


manager = ScraperManager()
