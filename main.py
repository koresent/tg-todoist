import asyncio
import html
import logging
import os
from datetime import datetime, timedelta, timezone

import aiosqlite
import dateparser
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from todoist_api_python.api_async import TodoistAPIAsync

load_dotenv()

# ================= Configuration =================


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TODOIST_API_TOKEN = os.getenv("TODOIST_API_TOKEN")
USER_TELEGRAM_ID = int(os.getenv("USER_TELEGRAM_ID", 0))
DB_PATH = os.getenv("DB_PATH", "bot_data.db")
POLL_INTERVAL_MINUTES = int(os.getenv("POLL_INTERVAL_MINUTES", 1))

if not all([TELEGRAM_BOT_TOKEN, TODOIST_API_TOKEN, USER_TELEGRAM_ID]):
    raise ValueError("Missing required environment variables (Tokens or User ID).")

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
todoist_api = TodoistAPIAsync(TODOIST_API_TOKEN)
scheduler = AsyncIOScheduler()
dp.message.filter(F.from_user.id == USER_TELEGRAM_ID)
dp.callback_query.filter(F.from_user.id == USER_TELEGRAM_ID)

# ================= Database Layer =================


class Database:
    def __init__(self, path: str):
        self.path = path
        self.db = None

    async def connect(self):
        self.db = await aiosqlite.connect(self.path)
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS notified_tasks (
                task_id TEXT PRIMARY KEY,
                last_due_date TEXT
            )
        """)
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS snoozed_reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT,
                run_at TEXT,
                content TEXT,
                description TEXT
            )
        """)
        await self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_run_at ON snoozed_reminders(run_at)"
        )
        await self.db.commit()

    async def close(self):
        if self.db:
            await self.db.close()

    async def is_notified(self, task_id: str, due_date: str) -> bool:
        async with self.db.execute(
            "SELECT last_due_date FROM notified_tasks WHERE task_id = ?", (task_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row is not None and row[0] == due_date

    async def mark_notified(self, task_id: str, due_date: str):
        await self.db.execute(
            "INSERT OR REPLACE INTO notified_tasks (task_id, last_due_date) VALUES (?, ?)",
            (task_id, due_date),
        )
        await self.db.commit()

    async def add_snooze(
        self, task_id: str, run_at: datetime, content: str, description: str
    ):
        await self.db.execute(
            "INSERT INTO snoozed_reminders (task_id, run_at, content, description) VALUES (?, ?, ?, ?)",
            (task_id, run_at.isoformat(), content, description),
        )
        await self.db.commit()

    async def get_pending_snoozes(self) -> list:
        now = datetime.now(timezone.utc).isoformat()
        async with self.db.execute(
            "SELECT id, task_id, content, description FROM snoozed_reminders WHERE run_at <= ?",
            (now,),
        ) as cursor:
            return await cursor.fetchall()

    async def remove_snooze(self, snooze_id: int):
        await self.db.execute(
            "DELETE FROM snoozed_reminders WHERE id = ?", (snooze_id,)
        )
        await self.db.commit()


db_manager = Database(DB_PATH)

# ================= Callbacks & States =================


class TaskCallback(CallbackData, prefix="t"):
    action: str
    task_id: str
    val: str = ""


class CustomTimeState(StatesGroup):
    waiting_for_time = State()


# ================= UI & Logic Helpers =================


def get_main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="Open Todoist",
                    web_app=WebAppInfo(url="https://todoist.com/app/today"),
                )
            ]
        ],
        resize_keyboard=True,
    )


def get_task_keyboard(task_id: str):
    builder = InlineKeyboardBuilder()
    for t in ["15m", "30m", "1h", "1d"]:
        builder.button(
            text=t, callback_data=TaskCallback(action="snooze", task_id=task_id, val=t)
        )
    builder.button(
        text="Enter time", callback_data=TaskCallback(action="custom", task_id=task_id)
    )
    builder.button(
        text="Done", callback_data=TaskCallback(action="done", task_id=task_id)
    )
    builder.adjust(4, 2)
    return builder.as_markup()


def extract_task_data(text: str) -> tuple[str, str]:
    parts = text.split("\n\n", 1)
    content = parts[0].removeprefix("📢 ")
    desc = parts[1] if len(parts) > 1 else ""
    return content, desc


# ================= Core Logic =================


async def send_reminder(task_id: str, title: str, description: str = "") -> bool:
    text = f"📢 <b>{html.escape(title)}</b>"
    if description:
        text += f"\n\n{html.escape(description)}"

    try:
        await bot.send_message(
            USER_TELEGRAM_ID,
            text,
            reply_markup=get_task_keyboard(task_id),
            parse_mode="HTML",
        )
        return True
    except Exception as e:
        logging.error(f"Notify error for task {task_id}: {e}")
        return False


async def check_missed_and_scheduled():
    pending = await db_manager.get_pending_snoozes()
    for s_id, t_id, content, desc in pending:
        success = await send_reminder(t_id, content, desc)
        if success:
            await db_manager.remove_snooze(s_id)

    try:
        iterator = await todoist_api.filter_tasks(query="today | overdue")

        async for tasks_batch in iterator:
            for task in tasks_batch:
                if not task.due or not task.due.date:
                    continue

                if not isinstance(task.due.date, datetime):
                    continue

                due_dt = task.due.date
                db_due_str = due_dt.isoformat()

                try:
                    if due_dt.tzinfo is not None:
                        now = datetime.now(timezone.utc)
                    else:
                        now = datetime.now()

                    if due_dt <= now:
                        if not await db_manager.is_notified(task.id, db_due_str):
                            success = await send_reminder(
                                task.id, task.content, task.description
                            )
                            if success:
                                await db_manager.mark_notified(task.id, db_due_str)
                except Exception as e:
                    logging.error(f"Error comparing dates for task {task.id}: {e}")
                    continue

    except Exception as e:
        logging.error(f"Todoist poll error: {e}")


# ================= Handlers =================


@dp.message(CommandStart())
async def start(m: Message):
    await m.answer("System active. Monitoring Todoist...", reply_markup=get_main_menu())


@dp.callback_query(TaskCallback.filter(F.action == "done"))
async def task_done(cb: CallbackQuery, callback_data: TaskCallback):
    try:
        await todoist_api.complete_task(task_id=callback_data.task_id)

        new_text = cb.message.html_text.replace("📢 ", "✅ ", 1)
        await cb.message.edit_text(new_text, parse_mode="HTML")
    except Exception as e:
        await cb.answer(f"Error: {e}", show_alert=True)


@dp.callback_query(TaskCallback.filter(F.action == "snooze"))
async def task_snooze(cb: CallbackQuery, callback_data: TaskCallback):
    delays = {"15m": 15, "30m": 30, "1h": 60, "1d": 1440}
    minutes = delays.get(callback_data.val, 0)
    run_at = datetime.now(timezone.utc) + timedelta(minutes=minutes)

    content, desc = extract_task_data(cb.message.text)
    await db_manager.add_snooze(callback_data.task_id, run_at, content, desc)

    await cb.message.delete()
    await cb.answer(f"Snoozed for {callback_data.val}")


@dp.callback_query(TaskCallback.filter(F.action == "custom"))
async def custom_snooze_start(
    cb: CallbackQuery, callback_data: TaskCallback, state: FSMContext
):
    content, desc = extract_task_data(cb.message.text)

    await state.update_data(task_id=callback_data.task_id, content=content, desc=desc)
    await state.set_state(CustomTimeState.waiting_for_time)

    await cb.message.answer("When should I remind you? (e.g. '2h')")
    await cb.answer()


@dp.message(CustomTimeState.waiting_for_time)
async def custom_snooze_finish(m: Message, state: FSMContext):
    parsed_date = await asyncio.to_thread(
        dateparser.parse,
        m.text,
        languages=["ru", "en"],
        settings={"PREFER_DATES_FROM": "future", "RELATIVE_BASE": datetime.now()},
    )

    if not parsed_date:
        await m.answer("Format not recognized. Try again.")
        return

    data = await state.get_data()
    run_at_utc = parsed_date.astimezone(timezone.utc)

    await db_manager.add_snooze(
        data["task_id"], run_at_utc, data["content"], data["desc"]
    )
    await m.answer(f"Scheduled for {parsed_date.strftime('%d.%m %H:%M')}")
    await state.clear()


# ================= Startup & Shutdown =================


@dp.startup()
async def on_startup():
    await db_manager.connect()
    await check_missed_and_scheduled()

    scheduler.add_job(
        check_missed_and_scheduled,
        "interval",
        minutes=POLL_INTERVAL_MINUTES,
        max_instances=1,
    )
    scheduler.start()


@dp.shutdown()
async def on_shutdown():
    scheduler.shutdown()
    await db_manager.close()
    await todoist_api.close()


async def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
    )

    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
