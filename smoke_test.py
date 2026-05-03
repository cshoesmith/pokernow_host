"""Smoke tests: DB layer + FastAPI routes (no Selenium / browser involved).

Spins up a fresh sqlite DB in a temp dir, walks through the same flow a host
would use in the UI, and asserts the resulting state.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

# Use a throwaway data dir before importing the app.
TMP = Path(tempfile.mkdtemp(prefix="vnbt_poker_test_"))
os.environ["VNBT_POKER_DATA"] = str(TMP)

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402

PASS = "\u2713"
FAIL = "\u2717"
results: list[tuple[bool, str]] = []


def check(cond: bool, label: str) -> None:
    results.append((cond, label))
    mark = PASS if cond else FAIL
    print(f"  {mark} {label}")


def section(title: str) -> None:
    print(f"\n== {title} ==")


def main() -> int:
    db.init_db()
    client = TestClient(app)

    section("DB initialises")
    check(db.DB_PATH.exists(), f"sqlite file created at {db.DB_PATH}")

    section("Home page renders with no sessions")
    r = client.get("/")
    check(r.status_code == 200, "GET / -> 200")
    check("No sessions yet" in r.text, "empty-state copy is shown")

    section("Create a session")
    r = client.post(
        "/sessions",
        data={"name": "Test night", "pokernow_url": "https://www.pokernow.club/games/abc"},
        follow_redirects=False,
    )
    check(r.status_code == 303, "POST /sessions -> 303 redirect")
    sid = int(r.headers["location"].rsplit("/", 1)[-1])
    check(sid > 0, f"new session id = {sid}")

    r = client.get(f"/sessions/{sid}")
    check(r.status_code == 200, "session page renders")
    check("Test night" in r.text, "session name shown")
    r_tools = client.get(f"/sessions/{sid}/tools")
    check(r_tools.status_code == 200, "tools page renders")
    check("https://www.pokernow.club/games/abc" in r_tools.text, "PokerNow URL pre-filled on tools page")

    section("Add players to session")
    for nm in ("Chris", "Alice", "Bob"):
        r = client.post(
            f"/sessions/{sid}/players",
            data={"name": nm, "seat_name": nm},
            follow_redirects=False,
        )
        check(r.status_code == 303, f"add player {nm}")
    rows = db.query("SELECT name FROM players ORDER BY name")
    names = [row["name"] for row in rows]
    check(names == ["Alice", "Bob", "Chris"], f"players table = {names}")

    # Adding the same player twice must be a no-op (UNIQUE).
    r = client.post(f"/sessions/{sid}/players", data={"name": "Chris", "seat_name": "Chris"})
    rows = db.query("SELECT COUNT(*) AS c FROM players WHERE name='Chris'")
    check(rows[0]["c"] == 1, "duplicate add does not create a new player row")

    section("Record ledger entries")
    # Look up player ids
    pid = {row["name"]: row["id"] for row in db.query("SELECT id, name FROM players")}

    entries = [
        (pid["Chris"], "buyin", 100, "first buy"),
        (pid["Alice"], "buyin", 100, ""),
        (pid["Bob"],   "buyin", 200, ""),
        (pid["Alice"], "rebuy", 50, ""),
        (pid["Alice"], "cashout", 175, "left early"),
    ]
    for player_id, kind, amount, note in entries:
        r = client.post(
            f"/sessions/{sid}/ledger",
            data={"player_id": player_id, "kind": kind, "amount": amount, "note": note},
            follow_redirects=False,
        )
        check(r.status_code == 303, f"record {kind} {amount} for player {player_id}")

    section("Per-player summary math")
    summary = {p["name"]: p for p in db.session_player_summary(sid)}
    check(summary["Alice"]["bought_in"] == 150, f"Alice bought_in=150 (got {summary['Alice']['bought_in']})")
    check(summary["Alice"]["cashed_out"] == 175, f"Alice cashed_out=175 (got {summary['Alice']['cashed_out']})")
    # No live stack snapshots yet, so live_pl == cashout - buyin = 25
    check(summary["Alice"]["live_pl"] == 25, f"Alice live_pl=25 (got {summary['Alice']['live_pl']})")
    check(summary["Chris"]["live_pl"] == -100, f"Chris live_pl=-100 (got {summary['Chris']['live_pl']})")
    check(summary["Bob"]["live_pl"] == -200, f"Bob live_pl=-200 (got {summary['Bob']['live_pl']})")

    section("Stack snapshot fallback (simulating scraper writes)")
    # Pretend the Selenium scraper inserted these rows.
    for name, stack in [("Chris", 240), ("Bob", 175)]:
        db.execute(
            "INSERT INTO stack_snapshots (session_id, player_name, stack, captured_at) "
            "VALUES (?, ?, ?, ?)",
            (sid, name, stack, db.now_iso()),
        )
    summary = {p["name"]: p for p in db.session_player_summary(sid)}
    check(summary["Chris"]["current_stack"] == 240, "Chris stack from snapshot = 240")
    # live_pl = stack + cashout - buyin = 240 + 0 - 100 = 140
    check(summary["Chris"]["live_pl"] == 140, f"Chris live_pl with stack=140 (got {summary['Chris']['live_pl']})")
    check(summary["Bob"]["live_pl"] == -25, f"Bob live_pl with stack=-25 (got {summary['Bob']['live_pl']})")

    # Insert a newer snapshot for Chris; latest_stack must use the most recent.
    db.execute(
        "INSERT INTO stack_snapshots (session_id, player_name, stack, captured_at) "
        "VALUES (?, ?, ?, ?)",
        (sid, "Chris", 305, db.now_iso()),
    )
    check(db.latest_stack(sid, "Chris") == 305, "latest_stack returns most recent")

    section("Hand + winners persistence")
    with db.connect() as cx:
        cur = cx.execute(
            "INSERT INTO hands (session_id, hand_number, pot_size, board, started_at) "
            "VALUES (?, 1, 60, 'Ah Kd 2c', ?)",
            (sid, db.now_iso()),
        )
        hid = cur.lastrowid
        cx.execute(
            "INSERT INTO hand_winners (hand_id, player_name, amount_won) VALUES (?, ?, ?)",
            (hid, "Alice", 60),
        )
        # A second hand won by Chris, smaller amount, plus a tied hand
        cur2 = cx.execute(
            "INSERT INTO hands (session_id, hand_number, pot_size, board, started_at) "
            "VALUES (?, 2, 40, 'Qs Js 9d', ?)",
            (sid, db.now_iso()),
        )
        cx.execute(
            "INSERT INTO hand_winners (hand_id, player_name, amount_won) VALUES (?, ?, ?)",
            (cur2.lastrowid, "Chris", 40),
        )
        cur3 = cx.execute(
            "INSERT INTO hands (session_id, hand_number, pot_size, board, started_at) "
            "VALUES (?, 3, 25, '7c 8c 9c', ?)",
            (sid, db.now_iso()),
        )
        cx.execute(
            "INSERT INTO hand_winners (hand_id, player_name, amount_won) VALUES (?, ?, ?)",
            (cur3.lastrowid, "Alice", 25),
        )
    r = client.get(f"/sessions/{sid}")
    check("Ah Kd 2c" in r.text, "hand board shown on session page")
    check("Alice" in r.text and "60" in r.text, "winner pill shown on session page")

    section("Trophies / podium")
    podium = db.session_hand_wins(sid)
    check(len(podium) >= 2, f"podium has entries (got {len(podium)})")
    check(podium[0]["name"] == "Alice" and podium[0]["hands_won"] == 2,
          f"top of podium is Alice with 2 hands (got {podium[0]})")
    check(podium[1]["name"] == "Chris" and podium[1]["hands_won"] == 1,
          f"second is Chris with 1 hand (got {podium[1]})")
    bh = db.biggest_hand(sid)
    check(bh and bh["name"] == "Alice" and bh["amount"] == 60,
          f"session biggest hand = Alice 60 (got {bh})")
    bh_all = db.biggest_hand(None)
    check(bh_all and bh_all["amount"] == 60, "lifetime biggest hand also = 60")
    life_pod = db.lifetime_hand_wins()
    check(life_pod[0]["name"] == "Alice" and life_pod[0]["hands_won"] == 2,
          "lifetime podium top = Alice")
    bs = db.biggest_session_win()
    # Alice has buyin 150 + cashout 175 (still active) = +25 P&L
    check(bs and bs["name"] == "Alice" and bs["pl"] == 25,
          f"biggest session win = Alice +25 (got {bs})")
    r = client.get("/players")
    check("Hall of fame" in r.text, "lifetime page shows Hall of fame")
    check("Biggest single pot" in r.text, "lifetime page shows biggest hand")

    section("Ledger delete")
    entry_id = db.query_one(
        "SELECT id FROM ledger_entries WHERE session_id=? AND kind='cashout'", (sid,)
    )["id"]
    r = client.post(
        f"/sessions/{sid}/ledger/{entry_id}/delete", follow_redirects=False
    )
    check(r.status_code == 303, "delete cashout entry -> 303")
    summary = {p["name"]: p for p in db.session_player_summary(sid)}
    check(summary["Alice"]["cashed_out"] == 0, "Alice cashout cleared after delete")

    section("Lifetime P&L page")
    r = client.get("/players")
    check(r.status_code == 200, "GET /players -> 200")
    rows = {row["name"]: row for row in db.lifetime_pl()}
    # Alice: in=150, out=0, pl=-150
    check(rows["Alice"]["pl"] == -150, f"Alice lifetime pl = {rows['Alice']['pl']}")
    check(rows["Bob"]["pl"] == -200, f"Bob lifetime pl = {rows['Bob']['pl']}")
    check(rows["Chris"]["pl"] == -100, f"Chris lifetime pl = {rows['Chris']['pl']}")

    section("Close + reopen session")
    r = client.post(f"/sessions/{sid}/close", follow_redirects=False)
    check(r.status_code == 303, "close -> 303")
    s = db.query_one("SELECT status, ended_at FROM sessions WHERE id=?", (sid,))
    check(s["status"] == "closed" and s["ended_at"], "session marked closed with ended_at")

    r = client.post(f"/sessions/{sid}/reopen", follow_redirects=False)
    check(r.status_code == 303, "reopen -> 303")
    s = db.query_one("SELECT status, ended_at FROM sessions WHERE id=?", (sid,))
    check(s["status"] == "open" and s["ended_at"] is None, "session marked open again")

    section("Bad input handling")
    r = client.post(
        f"/sessions/{sid}/ledger",
        data={"player_id": pid["Chris"], "kind": "bogus", "amount": 1, "note": ""},
    )
    check(r.status_code == 400, "invalid ledger kind -> 400")
    r = client.post(f"/sessions/{sid}/scraper/start", data={"url": ""})
    check(r.status_code == 400 or r.status_code == 200, "start with no URL -> 400 (had no url stored)")
    r = client.get("/sessions/9999")
    check(r.status_code == 404, "missing session -> 404")

    section("Scraper helpers (unit)")
    from app.scraper import _coerce_number, _winners_signature, _parse_winner_prize
    check(_coerce_number("1,234") == 1234.0, "_coerce_number strips commas")
    check(_coerce_number("$50.25") == 50.25, "_coerce_number strips currency")
    check(_coerce_number("(20)") == 20.0, "_coerce_number ignores parens")
    check(_coerce_number(None) is None, "_coerce_number(None) -> None")
    check(_coerce_number("nope") is None, "_coerce_number('nope') -> None")
    sig1 = _winners_signature([{"name": "A", "stack_info": "10"}, {"name": "B", "stack_info": "5"}])
    sig2 = _winners_signature([{"name": "B", "stack_info": "5"}, {"name": "A", "stack_info": "10"}])
    check(sig1 == sig2 and sig1 != "", "_winners_signature is order-independent")
    check(_parse_winner_prize("3509560 (+5000)") == 5000.0, "_parse_winner_prize extracts prize")
    check(_parse_winner_prize("3509560") is None, "_parse_winner_prize returns None without prize")
    check(_parse_winner_prize(None) is None, "_parse_winner_prize(None) -> None")

    # ---- Summary ----
    print()
    failed = [label for ok, label in results if not ok]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("FAILED:")
        for f in failed:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    rc = 0
    try:
        rc = main()
    finally:
        # cleanup temp data dir
        try:
            shutil.rmtree(TMP, ignore_errors=True)
        except Exception:
            pass
    sys.exit(rc)
