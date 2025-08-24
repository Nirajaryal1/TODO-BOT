"""
Productive Telegram Bot (aiogram + APScheduler + SQLite)
=======================================================

Quick start
-----------
1) Python 3.10+
2) `pip install -r requirements.txt`
3) Set env vars:
      TELEGRAM_BOT_TOKEN=123456:ABC...   # from @BotFather
      DEFAULT_TZ=America/Los_Angeles     # optional (your default)
4) `python bot.py`

What it does
------------
- Every day at *your* local **08:00** → sends a Today digest (with undone Yesterday tasks + carry-over)
- Every day at *your* local **10, 13, 16, 19, 22** → asks you to plan **tomorrow**
- Smart carry‑over at 08:00 → unfinished tasks from yesterday move into today
- Priorities, tags, due‑times per task; inline quick‑add buttons
- Pomodoro (/focus) 25/5 with an auto reminder
- Weekly stats (/week)
- Per‑user timezone via `/tz <Area/City>`

Deploy
------
- Any always‑on host (Render/Railway/Fly/VM). Polling is fine.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ----------------- ENV -----------------
DB_PATH = os.getenv("DB_PATH", "tasks.db")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DEFAULT_TZ = os.getenv("DEFAULT_TZ", "America/Los_Angeles")

if not BOT_TOKEN:
    raise SystemExit("Missing TELEGRAM_BOT_TOKEN env var. Get one from @BotFather.")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

# -------------- DATABASE --------------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  user_id INTEGER PRIMARY KEY,
  tz TEXT NOT NULL DEFAULT 'America/Los_Angeles'
);

CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  title TEXT NOT NULL,
  day DATE NOT NULL,
  due_time TEXT,
  priority TEXT DEFAULT 'med',
  tags TEXT DEFAULT '',
  status TEXT DEFAULT 'open',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sends (
  user_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  day DATE NOT NULL,
  hour INTEGER NOT NULL,
  PRIMARY KEY(user_id, kind, day, hour)
);
"""

with closing(db()) as conn:
    conn.executescript(SCHEMA)
    conn.commit()

# --------------- HELPERS --------------

@dataclass
class User:
    user_id: int
    tz: str


def get_user(user_id: int) -> User:
    with closing(db()) as conn:
        cur = conn.execute("SELECT user_id, tz FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if row:
            return User(row["user_id"], row["tz"])
        conn.execute("INSERT INTO users(user_id, tz) VALUES (?, ?)", (user_id, DEFAULT_TZ))
        conn.commit()
        return User(user_id, DEFAULT_TZ)


def set_tz(user_id: int, tz: str) -> None:
    with closing(db()) as conn:
        conn.execute(
            "INSERT INTO users(user_id, tz) VALUES(?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET tz=excluded.tz",
            (user_id, tz),
        )
        conn.commit()


def now_local(user: User) -> datetime:
    return datetime.now(ZoneInfo(user.tz))


def today_local(user: User) -> date:
    return now_local(user).date()


def fmt_task(row: sqlite3.Row) -> str:
    pr = {"low": "⬇️", "med": "⚪", "high": "⬆️"}.get(row["priority"], "⚪")
    due = f" @ {row['due_time']}" if row["due_time"] else ""
    tags = f" {row['tags']}" if row["tags"] else ""
    status = "✅" if row["status"] == "done" else ("😴" if row["status"] == "snoozed" else "⬜")
    return f"{status} {pr} {row['title']}{due}{tags} (#{row['id']})"


def quick_add_kb():
    kb = InlineKeyboardBuilder()
    for label in ["Gym", "Study", "Groceries", "Deep Work", "Call Mom"]:
        kb.button(text=f"➕ {label}", callback_data=f"quickadd|{label}")
    kb.button(text="New task…", callback_data="newtask")
    kb.adjust(2)
    return kb.as_markup()


def task_action_kb(task_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Done", callback_data=f"done|{task_id}")
    kb.button(text="😴 Snooze", callback_data=f"snooze|{task_id}")
    kb.button(text="🗑️ Delete", callback_data=f"delete|{task_id}")
    kb.adjust(3)
    return kb.as_markup()

# --------------- COMMANDS --------------

@dp.message(Command("start"))
async def cmd_start(message: Message):
    u = get_user(message.from_user.id)
    await message.reply(
        "👋 I’m your productive assistant!\n\n"
        "• I’ll ping you at 10/13/16/19/22 to plan **tomorrow**.\n"
        "• At 08:00 I’ll send your **Today** list and carry over unfinished items.\n"
        "• Use /add, /list, /done, /tomorrow, /tz, /focus, /week, /help.\n\n"
        f"Your timezone is **{u.tz}** (change via /tz <Area/City>).",
        reply_markup=quick_add_kb(),
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.reply(
        "/add <task> [#tag ...] [!low|!med|!high] [@HH:MM] — add for **today**\n"
        "/tomorrow <task> — add for **tomorrow**\n"
        "/list — show today’s tasks\n"
        "/done <id> — mark task done\n"
        "/tz <Area/City> — set your timezone (e.g., /tz America/Los_Angeles)\n"
        "/focus — start a 25/5 Pomodoro\n"
        "/week — show last‑7‑days stats",
    )


def parse_task_args(args: str):
    title, tags, priority, due_time = [], [], "med", None
    for tok in (args or "").split():
        if tok.startswith("#"):
            tags.append(tok)
        elif tok.startswith("!"):
            priority = tok[1:]
        elif tok.startswith("@"):
            due_time = tok[1:]
        else:
            title.append(tok)
    return " ".join(title).strip(), " ".join(tags), priority, due_time

#-------------- COMMANDS --------------

@dp.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject):
    """Add a task for TODAY. Example: /add Finish module #study !high @18:00"""
    u = get_user(message.from_user.id)
    if not command.args:
        await message.reply("Usage: /add Finish module #study !high @18:00")
        return
    title, tags, priority, due_time = parse_task_args(command.args)
    if not title:
        await message.reply("Please include a task title. Example: /add Finish module")
        return
    with closing(db()) as conn:
        conn.execute(
            "INSERT INTO tasks(user_id, title, day, due_time, priority, tags) VALUES (?,?,?,?,?,?)",
            (u.user_id, title, today_local(u).isoformat(), due_time, priority, tags),
        )
        conn.commit()
    await message.reply(f"Added for today: **{title}**")


@dp.message(Command("tomorrow"))
async def cmd_tomorrow(message: Message, command: CommandObject):
    """Add a task for TOMORROW. Example: /tomorrow Gym @07:00"""
    u = get_user(message.from_user.id)
    if not command.args:
        await message.reply("Usage: /tomorrow Gym @07:00")
        return
    title, tags, priority, due_time = parse_task_args(command.args)
    if not title:
        await message.reply("Please include a task title. Example: /tomorrow Gym")
        return
    with closing(db()) as conn:
        conn.execute(
            "INSERT INTO tasks(user_id, title, day, due_time, priority, tags) VALUES (?,?,?,?,?,?)",
            (u.user_id, title, (today_local(u) + timedelta(days=1)).isoformat(), due_time, priority, tags),
        )
        conn.commit()
    await message.reply(f"Queued for tomorrow: **{title}**")


@dp.message(Command("list"))
async def cmd_list(message: Message):
    u = get_user(message.from_user.id)
    with closing(db()) as conn:
        cur = conn.execute(
            "SELECT * FROM tasks WHERE user_id=? AND day=? "
            "ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'med' THEN 1 ELSE 2 END, "
            "COALESCE(due_time,'99:99')",
            (u.user_id, today_local(u).isoformat()),
        )
        rows = cur.fetchall()
    if not rows:
        await message.reply("No tasks for today. Use /add or the buttons below.", reply_markup=quick_add_kb())
        return
    text = "\n".join(fmt_task(r) for r in rows)
    await message.reply(text)


@dp.message(Command("done"))
async def cmd_done(message: Message, command: CommandObject):
    if not command.args or not command.args.isdigit():
        await message.reply("Usage: /done <task_id>")
        return
    task_id = int(command.args)
    with closing(db()) as conn:
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
        conn.commit()
    await message.reply("Nice! Marked ✅")


@dp.message(Command("tz"))
async def cmd_tz(message: Message, command: CommandObject):
    if not command.args:
        await message.reply("Usage: /tz Continent/City  (e.g., /tz America/Los_Angeles)")
        return
    try:
        ZoneInfo(command.args)
    except Exception:
        await message.reply("That timezone isn’t recognized. Try like America/New_York or Asia/Kathmandu")
        return
    set_tz(message.from_user.id, command.args)
    await message.reply(f"Timezone set to {command.args}. I’ll schedule reminders accordingly.")


@dp.message(Command("focus"))
async def cmd_focus(message: Message):
    await message.reply("⏱️ Focus started: 25 minutes. I’ll ping you when time’s up!")
    u = get_user(message.from_user.id)
    run_at = now_local(u) + timedelta(minutes=25)
    scheduler.add_job(
        send_message_job,
        trigger='date',
        run_date=run_at,
        args=(u.user_id, "⏰ 25 minutes done! Take a 5-min break (/focus to start again)."),
        id=f"focus-{u.user_id}-{int(run_at.timestamp())}",
        replace_existing=False,
    )


@dp.message(Command("week"))
async def cmd_week(message: Message):
    u = get_user(message.from_user.id)
    start = today_local(u) - timedelta(days=6)
    with closing(db()) as conn:
        cur = conn.execute(
            "SELECT status, COUNT(*) c FROM tasks WHERE user_id=? AND day BETWEEN ? AND ? GROUP BY status",
            (u.user_id, start.isoformat(), today_local(u).isoformat()),
        )
        stats = {row["status"]: row["c"] for row in cur.fetchall()}
    done = stats.get("done", 0)
    total = sum(stats.values()) or 1
    rate = round(100 * done / total)
    await message.reply(f"📊 Last 7 days: {done}/{total} done ({rate}% completion). Keep going!")

    


# ----------- SCHEDULED JOBS -----------

async def send_message_job(user_id: int, text: str):
    try:
        await bot.send_message(chat_id=user_id, text=text)
    except Exception as e:
        print("send_message_job error:", e)


def was_sent(user_id: int, kind: str, local_day: date, local_hour: int) -> bool:
    with closing(db()) as conn:
        cur = conn.execute(
            "SELECT 1 FROM sends WHERE user_id=? AND kind=? AND day=? AND hour=?",
            (user_id, kind, local_day.isoformat(), local_hour),
        )
        return cur.fetchone() is not None


def mark_sent(user_id: int, kind: str, local_day: date, local_hour: int) -> None:
    with closing(db()) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO sends(user_id, kind, day, hour) VALUES (?,?,?,?)",
            (user_id, kind, local_day.isoformat(), local_hour),
        )
        conn.commit()


async def do_daily_digest(u: User, local_now: datetime):
    if local_now.hour != 8 or was_sent(u.user_id, "digest", local_now.date(), 8):
        return
    # show yesterday’s undone
    yday = local_now.date() - timedelta(days=1)
    with closing(db()) as conn:
        cur = conn.execute(
            "SELECT * FROM tasks WHERE user_id=? AND day=? AND status='open' ORDER BY id",
            (u.user_id, yday.isoformat()),
        )
        y_open = cur.fetchall()
    # carry‑over
    await do_carry_over(u, local_now)
    # today’s tasks
    with closing(db()) as conn:
        cur = conn.execute(
            "SELECT * FROM tasks WHERE user_id=? AND day=? ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'med' THEN 1 ELSE 2 END, COALESCE(due_time,'99:99')",
            (u.user_id, local_now.date().isoformat()),
        )
        rows = cur.fetchall()
    sections = []
    if y_open:
        sections.append("⏮️ Yesterday (still open — copied to today):\n" + "\n".join(fmt_task(r) for r in y_open))
    if rows:
        sections.append("☀️ Today:\n" + "\n".join(fmt_task(r) for r in rows))
    else:
        sections.append("☀️ Today:\n(no tasks) — Use /add or /tomorrow to plan.")
    await send_message_job(u.user_id, "\n\n".join(sections))
    mark_sent(u.user_id, "digest", local_now.date(), 8)


async def do_carry_over(u: User, local_now: datetime):
    if was_sent(u.user_id, "carry", local_now.date(), 8):
        return
    yday = local_now.date() - timedelta(days=1)
    with closing(db()) as conn:
        cur = conn.execute(
            "SELECT id, title, due_time, priority, tags FROM tasks WHERE user_id=? AND day=? AND status='open'",
            (u.user_id, yday.isoformat()),
        )
        open_rows = cur.fetchall()
        for r in open_rows:
            conn.execute(
                "INSERT INTO tasks(user_id, title, day, due_time, priority, tags) VALUES (?,?,?,?,?,?)",
                (u.user_id, r["title"], local_now.date().isoformat(), r["due_time"], r["priority"], r["tags"]),
            )
        conn.commit()
    if open_rows:
        await send_message_job(u.user_id, f"↪️ Carried over {len(open_rows)} unfinished task(s) from yesterday.")
    mark_sent(u.user_id, "carry", local_now.date(), 8)


async def do_tomorrow_prompt(u: User, local_now: datetime):
    prompt_hours = {10, 13, 16, 19, 22}
    h = local_now.hour
    if h in prompt_hours and not was_sent(u.user_id, "prompt", local_now.date(), h):
        await send_message_job(u.user_id, "📝 What would you like to add for tomorrow? Use /tomorrow <task>.")
        mark_sent(u.user_id, "prompt", local_now.date(), h)


async def hourly_tick():
    with closing(db()) as conn:
        cur = conn.execute("SELECT user_id, tz FROM users")
        users = [User(r["user_id"], r["tz"]) for r in cur.fetchall()]
    for u in users:
        local_now = now_local(u).replace(minute=0, second=0, microsecond=0)
        await do_daily_digest(u, local_now)
        await do_tomorrow_prompt(u, local_now)

# ----------------- MAIN -----------------

async def main():
    scheduler.start()
    scheduler.add_job(hourly_tick, CronTrigger(minute=0))
    print("Bot is running…")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
