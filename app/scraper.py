"""Background Selenium scraper that mirrors a live PokerNow.club table into the DB.

Wraps the Zehmosu/PokerNow library (`pip install PokerNow`).
One worker per session_id; manage via ScraperManager.
"""
from __future__ import annotations

import logging
import os
import platform
import re
import signal
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import db

log = logging.getLogger("scraper")

# Matches PokerNow game-log "collected" lines, e.g.:
#   "Shoey collected 1500 from pot"
#   "Alice collected 300 from main pot"
#   "Bob collected 200 from side pot-1"
_COLLECTED_RE = re.compile(
    r'^(.+?)\s+collected\s+([\d,]+(?:\.\d+)?)\s+from\b',
    re.IGNORECASE,
)

PID_DIR = db.DATA_DIR / "chrome_pids"


def _write_pid(session_id: int, pid: int) -> None:
    PID_DIR.mkdir(parents=True, exist_ok=True)
    (PID_DIR / f"session_{session_id}.pid").write_text(str(pid))


def _clear_pid(session_id: int) -> None:
    try:
        (PID_DIR / f"session_{session_id}.pid").unlink(missing_ok=True)
    except Exception:
        pass


def kill_orphan_chromes() -> None:
    """Kill any Chrome processes whose PIDs were written by a previous run."""
    if not PID_DIR.exists():
        return
    for pid_file in PID_DIR.glob("*.pid"):
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            log.info("killed orphan Chrome PID %s", pid)
        except (ProcessLookupError, PermissionError):
            pass  # already gone
        except Exception as e:
            log.warning("could not kill orphan Chrome PID from %s: %s", pid_file, e)
        finally:
            pid_file.unlink(missing_ok=True)


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

    POLL_SECONDS = 1.0

    def __init__(self, session_id: int, url: str, cookie_path: str | None = None,
                 headless: bool = False) -> None:
        super().__init__(name=f"scraper-s{session_id}", daemon=True)
        self.session_id = session_id
        self.url = url
        self.cookie_path = cookie_path or f"data/cookies_session_{session_id}.pkl"
        self.headless = headless
        self.status = ScraperStatus()
        self._stop_event = threading.Event()
        self._thread_done = threading.Event()  # set when run() exits; avoids join() which breaks in Python 3.11.12+
        self._driver = None
        self._client = None
        self._last_winners_signature: str | None = None
        self._last_stacks: dict[str, float] = {}
        # Peak pot value seen since the last hand was recorded.
        self._peak_pot: float = 0.0
        # Fallback hand detection: track pot transition and pre-hand stack baseline.
        # Winner banners only show ~3 s; each poll takes 6–15 s so they're often missed.
        self._prev_pot_nonzero: bool = False
        self._stacks_before_hand: dict[str, float] = {}
        # Auto-ledger state
        self._players_at_table: set[str] = set()   # who we saw last poll
        self._last_hand_winners: set[str] = set()  # names who won most recent hand (stack rise expected)
        # Game-log scraping state (primary hand detection path)
        self._log_watermark: int = -1   # -1 = not yet initialised; >=0 = entries already processed
        self._last_board: str | None = None  # community cards cached from last poll

    # -- lifecycle -----------------------------------------------------------

    def stop(self) -> None:
        self._stop_event.set()

    # Exceptions that indicate the Chrome session is dead and needs full reinit.
    _FATAL_EXC = ("InvalidSessionIdException", "WebDriverException",
                  "NoSuchWindowException", "SessionNotCreatedException")

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
            while not self._stop_event.is_set():
                try:
                    self._poll_once()
                    self.status.last_error = None
                except Exception as exc:
                    self.status.last_error = f"{exc.__class__.__name__}: {exc}"
                    log.warning("poll error: %s\n%s", exc, traceback.format_exc())
                    # Dead browser session — tear down and reinitialise Chrome.
                    if type(exc).__name__ in self._FATAL_EXC:
                        log.warning("fatal driver error — reinitialising Chrome for session %s", self.session_id)
                        self._teardown_driver()
                        self._stop_event.wait(5)
                        if self._stop_event.is_set():
                            break
                        try:
                            self._init_driver()
                            self.status.last_error = None
                        except Exception as init_exc:
                            self.status.last_error = f"reinit failed: {init_exc}"
                            log.exception("reinit failed for session %s", self.session_id)
                            break  # give up; watchdog will restart the thread
                self._stop_event.wait(self.POLL_SECONDS)
        finally:
            self.status.running = False
            self._teardown_driver()
            self._thread_done.set()

    # -- selenium setup ------------------------------------------------------

    def _init_driver(self) -> None:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from PokerNow import PokerClient  # type: ignore
        # Silence the PokerNow library's verbose print() debug output (it uses
        # print() directly rather than the logging module, which floods Fly logs).
        import PokerNow.managers as _pkr_mgr
        import PokerNow.client as _pkr_cli
        _noop = lambda *a, **kw: None  # noqa: E731
        _pkr_mgr.print = _noop  # type: ignore[attr-defined]
        _pkr_cli.print = _noop  # type: ignore[attr-defined]

        opts = Options()
        headless = self.headless or _must_run_headless()
        if headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--window-size=1280,900")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-first-run")
        opts.add_argument("--no-default-browser-check")
        opts.add_argument("--disable-extensions")
        opts.add_argument("--remote-debugging-port=0")
        # Use a session-specific profile dir so each scraper gets its own Chrome instance.
        profile_dir = db.DATA_DIR / f"chrome_profile_{self.session_id}"
        profile_dir.mkdir(parents=True, exist_ok=True)
        _clear_stale_chrome_profile_locks(profile_dir)
        opts.add_argument(f"--user-data-dir={profile_dir.resolve()}")
        log.info(
            "starting Chrome for session %s (headless=%s, profile=%s)",
            self.session_id,
            headless,
            profile_dir,
        )

        self._driver = webdriver.Chrome(options=opts)
        # Record the Chrome PID so we can kill it if the server is force-quit.
        try:
            _write_pid(self.session_id, self._driver.service.process.pid)
        except Exception:
            pass
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
        _clear_pid(self.session_id)

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
                    # First sight of this player this run.
                    # Check DB: if they already have a ledger entry this session
                    # we're resuming after a restart — don't double-record a buyin.
                    existing = db.query_one(
                        "SELECT 1 FROM ledger_entries "
                        "WHERE session_id=? AND player_id=("
                        "  SELECT id FROM players WHERE name=? COLLATE NOCASE LIMIT 1"
                        ") LIMIT 1",
                        (self.session_id, name),
                    )
                    if existing:
                        log.info("scraper resume: skipping duplicate buyin for %s", name)
                    elif stack > 0:
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

        # Save stacks as the pre-hand baseline only when genuinely idle between
        # hands (pot has been 0 for at least two consecutive polls).  We must
        # NOT overwrite the baseline on the same poll where the fallback runs
        # (pot just cleared), otherwise both sides of the delta would be
        # identical and no winner could be inferred.
        if pot_now == 0 and not self._prev_pot_nonzero:
            self._stacks_before_hand = dict(self._last_stacks)

        # Cache community cards whenever visible (used by log-path recording
        # when the board may have cleared by the time we read the log).
        community_now = getattr(gs, "community_cards", []) or []
        if community_now:
            self._last_board = " ".join(str(c) for c in community_now)

        # 2. Track running peak pot for this hand.
        if pot_now > self._peak_pot:
            self._peak_pot = pot_now

        # 3. Hand / winner detection.
        # Primary: game log — persistent, never disappears after ~3 s like the banner.
        _hand_recorded = self._poll_log(gs)

        # Secondary: winner banner (fast path when log is unavailable).
        if not _hand_recorded:
            winners = getattr(gs, "winners", None) or []
            signature = _winners_signature(winners)
            if signature and signature != self._last_winners_signature:
                self._last_winners_signature = signature
                self._last_hand_winners = {
                    str(w.get("name", "")).strip() for w in winners if w.get("name")
                }
                self._record_hand(gs, winners)
                self._peak_pot = 0.0
                _hand_recorded = True

        # Tertiary: stack-delta fallback when both log and banner missed the hand.
        if not _hand_recorded and pot_now == 0 and self._prev_pot_nonzero and self._peak_pot > 0:
            log.info(
                "scraper: pot cleared without log/banner (session %s, peak_pot=%.0f) — stack fallback",
                self.session_id, self._peak_pot,
            )
            self._record_hand_fallback(gs)
            self._peak_pot = 0.0

        self._prev_pot_nonzero = (pot_now > 0)

    # -- game-log scraping (primary hand detection) --------------------------

    def _read_log_entries(self) -> list[str]:
        """Return text of all game-log entries via a single JS call."""
        try:
            result = self._driver.execute_script(
                "return Array.from(document.querySelectorAll('.log-modal-entries p.content'))"
                ".map(p => p.innerText.trim()).filter(t => t.length > 0);"
            )
            return result or []
        except Exception:
            return []

    def _poll_log(self, gs: Any) -> bool:
        """Read new game-log entries and record completed hands found there.

        Returns True if at least one hand was recorded.  This is the primary
        detection path — the persistent log never disappears (unlike the ~3 s
        winner banner), so it catches every hand regardless of poll latency.
        """
        entries = self._read_log_entries()
        if not entries:
            return False

        if self._log_watermark < 0:
            # First time entries are visible — skip history, only process future entries.
            self._log_watermark = len(entries)
            log.info("scraper: log watermark initialised at %d entries (session %s)",
                     len(entries), self.session_id)
            return False

        if len(entries) <= self._log_watermark:
            return False  # nothing new (or log hidden/reset)

        new_entries = entries[self._log_watermark:]
        self._log_watermark = len(entries)

        recorded = False
        pending: list[dict] = []

        def flush() -> None:
            nonlocal recorded
            if not pending:
                return
            winners = list(pending)
            pending.clear()
            sig = _winners_signature(winners)
            if sig == self._last_winners_signature:
                log.debug("scraper: log hand skipped (already recorded via banner): %s", sig)
                return
            self._last_winners_signature = sig
            self._last_hand_winners = {w["name"] for w in winners}
            # Use cached board if gs community cards are already cleared
            community = getattr(gs, "community_cards", []) or []
            board = self._last_board if not community else None
            self._record_hand_from_log(winners, community or board)
            self._peak_pot = 0.0
            recorded = True

        for entry in new_entries:
            log.debug("scraper log: %s", entry)
            m = _COLLECTED_RE.match(entry)
            if m:
                name = m.group(1).strip()
                amount = float(m.group(2).replace(",", ""))
                pending.append({"name": name, "stack_info": f"(+{amount:.0f})"})
            else:
                flush()  # non-collected entry = boundary between hands
        flush()
        return recorded

    def _record_hand_from_log(self, winners: list[dict[str, Any]], community: Any) -> None:
        """Persist a hand detected via the game log."""
        if isinstance(community, list):
            board = " ".join(str(c) for c in community) if community else None
        else:
            board = community or None  # already a string or None

        # Pot = sum of collected amounts (more reliable than _peak_pot here)
        total = sum(
            float(w["stack_info"].lstrip("(+").rstrip(")"))
            for w in winners
            if w.get("stack_info") and "(+" in w["stack_info"]
        )
        pot = total if total > 0 else (self._peak_pot or None)

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
                cx.execute(
                    "INSERT INTO hand_winners (hand_id, player_name, amount_won, winner_cards, winner_hand_desc) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (hand_id, w["name"], _parse_winner_prize(w.get("stack_info")), None, None),
                )
        self.status.hands_recorded += 1
        log.info(
            "scraper: hand #%d recorded via log — winner(s): %s, pot: %s (session %s)",
            next_no, [w["name"] for w in winners], pot, self.session_id,
        )

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

    def _record_hand_fallback(self, gs: Any) -> None:
        """Record a hand using stack changes when the winner banner was not polled.

        PokerNow only shows winner banners for ~3 s; each get_game_state() call
        takes 6–15 s, so most hands are missed by the normal detection path.
        Instead, we compare current stacks against the pre-hand baseline
        (_stacks_before_hand) to find who gained chips.
        """
        if not self._stacks_before_hand:
            log.debug(
                "scraper: fallback hand skipped — no pre-hand stack baseline (session %s)",
                self.session_id,
            )
            return

        fake_winners = []
        for name, post in self._last_stacks.items():
            pre = self._stacks_before_hand.get(name)
            if pre is None:
                continue
            gain = post - pre
            if gain > 0:
                fake_winners.append({"name": name, "stack_info": f"{post:.0f} (+{gain:.0f})"})

        if not fake_winners:
            log.warning(
                "scraper: fallback hand — no stack gains found for session %s "
                "(pre=%s post=%s)",
                self.session_id,
                dict(self._stacks_before_hand),
                dict(self._last_stacks),
            )
            return

        log.info(
            "scraper: fallback hand recorded — inferred winner(s): %s",
            [w["name"] for w in fake_winners],
        )
        self._record_hand(gs, fake_winners)
        # Reset signature so the next real winner banner is always detected
        # (avoids missing a hand where the same player wins back-to-back).
        self._last_winners_signature = ""


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


def _must_run_headless() -> bool:
    """Return True when Chrome cannot open a visible window in this runtime."""
    if os.environ.get("VNBT_POKER_FORCE_HEADLESS", "").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    # Fly/Docker Linux containers generally have no X/Wayland display. Trying
    # non-headless Chrome there fails with "session not created: Chrome instance
    # exited" before Selenium can attach to DevTools.
    if platform.system() == "Linux" and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return True
    return False


def _clear_stale_chrome_profile_locks(profile_dir: Path) -> None:
    """Remove lock files left by a crashed Chrome using this profile directory."""
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie", "DevToolsActivePort"):
        try:
            (profile_dir / name).unlink(missing_ok=True)
        except Exception as exc:
            log.debug("could not clear stale Chrome profile file %s: %s", profile_dir / name, exc)


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

    def kill_orphan_chromes(self) -> None:
        kill_orphan_chromes()

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
                new_worker = _SessionScraper(sid, w.url, cookie_path=w.cookie_path, headless=w.headless)
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
            worker._thread_done.wait(timeout=10)

    def status(self, session_id: int) -> ScraperStatus | None:
        worker = self._workers.get(session_id)
        return worker.status if worker else None

    def stop_all(self) -> None:
        with self._lock:
            workers = list(self._workers.values())
        for w in workers:
            w.stop()
        for w in workers:
            w._thread_done.wait(timeout=10)


manager = ScraperManager()
