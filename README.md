"""
Productive Telegram Bot (aiogram + APScheduler + SQLite)
=======================================================


Quick start
-----------
1) Python 3.10+
2) `pip install -r requirements.txt`
3) Set env vars:
TELEGRAM_BOT_TOKEN=123456:ABC... # from @BotFather
DEFAULT_TZ=America/Los_Angeles # optional (your default)
4) `python bot.py`


What it does
------------
- Every day at *your* local **08:00** → sends a Today digest (with undone Yesterday tasks + carry-over)
- Every day at *your* local **10, 13, 16, 19, 22** → asks you to plan **tomorrow**
- Smart carry‑over at 08:00 → unfinished tasks from yesterday move into today
- Priorities, tags, due‑times per task; inline quick‑add buttons
- Pomodoro (/focus) 25/5 with an auto reminder
- Weekly stats (/week)


Deploy
------
- Any always‑on host (Render/Railway/Fly/VM). Polling is fine.
"""
