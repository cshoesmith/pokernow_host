# PokerNow Host Manager

A small local web app for the **host of a PokerNow.club game**. It tracks
buy-ins / rebuys / cash-outs, captures live stacks and hand winners from the
table via Selenium (using the [Zehmosu/PokerNow](https://github.com/Zehmosu/PokerNow)
client), and rolls everything up into per-session and lifetime P&L.

## Stack
- **FastAPI** + Jinja2 templates (server-rendered HTML, no JS build step)
- **SQLite** (file under `data/poker.sqlite`)
- **Selenium + PokerNow** for live table scraping (Chrome by default)

## Install

Requires Python 3.10+ and Google Chrome installed.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Run

```powershell
python run.py
```

Then open http://127.0.0.1:8000.

## Workflow

1. **Create a session** on the home page (give it a name and the PokerNow
   table URL — the URL is optional, you can add it later).
2. **Add the players** that are sitting at the table. The "seat name" is the
   exact name shown at the PokerNow seat — leave blank to default to the
   player name. This is what links a player to their stack snapshots.
3. **Record buy-ins / rebuys / cash-outs** as they happen.
4. Click **Start / restart** under *Live PokerNow scraper* to launch a Chrome
   window. The first time, complete the PokerNow login manually in that
   window; cookies are saved to `data/cookies_session_<id>.pkl` so subsequent
   restarts are automatic.
5. The scraper polls every ~3 seconds and stores:
   - **Stack snapshots** whenever a player's stack changes.
   - **Hands** with pot size, board, and winners whenever a winner banner
     appears at the table.
6. The session view shows live P/L per player (`stack + cashout − buyin`),
   the most recent 25 hands, and the full session ledger.
7. **Close** the session when the game ends. Lifetime P&L across all
   sessions is on the **Lifetime P&L** page.

## Data location
All state lives in `data/poker.sqlite`. Back it up by copying that file.

## Notes
- The scraper is read-only; it does not perform any in-game actions.
- Headless mode is offered, but PokerNow login is easier in a visible window
  the first time. After cookies are saved, you can re-run headless.
- Selenium will need a Chrome browser; ChromeDriver is auto-managed by
  modern Selenium 4.
