# Discord VC Kicker Bot

A Discord bot that enforces “productivity mode” by disconnecting opted-in users from voice calls based on schedules or timers.

## Commands
- `!steponme` — activate stepping, pick how long it should last
- `!dontsteponme` — 5-minute grace
- `!stepstatus` — check your state
- `!stepoff` — stop stepping

## Setup
1. Clone repo
2. Create `.env` with: 

```powershell
DISCORD_TOKEN=your_bot_token_here
TIMEZONE=America/Los_Angeles
TARGET_USER_ID=123456789012345678
```

3. Install dependencies:
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

4. Run:
```powershell
python vc_kicker.py
```
