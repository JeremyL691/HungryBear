# src/telegram_bot.py

from __future__ import annotations

import os
from datetime import datetime
from typing import List, Optional

from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .constants import DINING_LOCATIONS
from .menu_scraper import MenuScraper

# Conversation states (just numbers)
CHOOSING_LOCATION, CHOOSING_MEAL = range(2)


def now_str() -> str:
    # Student dev note: printing local time helps users trust "today".
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def norm(s: str) -> str:
    return " ".join((s or "").strip().lower().split())


def build_keyboard(items: List[str], cols: int = 2) -> ReplyKeyboardMarkup:
    """
    Student dev note:
    Telegram wants a list of rows, each row is a list of button texts.
    """
    rows: List[List[str]] = []
    row: List[str] = []
    for it in items:
        row.append(it)
        if len(row) >= cols:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


def match_location(user_text: str) -> Optional[str]:
    """
    Accept:
    - number like "3"
    - exact location name (case-insensitive)
    """
    raw = (user_text or "").strip()
    if raw.isdigit():
        idx = int(raw)
        if 1 <= idx <= len(DINING_LOCATIONS):
            return DINING_LOCATIONS[idx - 1]

    user_norm = norm(raw)
    for loc in DINING_LOCATIONS:
        if norm(loc) == user_norm:
            return loc

    return None


def split_long_message(text: str, limit: int = 3800) -> List[str]:
    """
    Telegram max message length is 4096 chars.
    We'll split safely to avoid errors.
    """
    if len(text) <= limit:
        return [text]

    parts: List[str] = []
    cur: List[str] = []
    cur_len = 0

    for line in text.splitlines():
        add = len(line) + 1
        if cur and (cur_len + add > limit):
            parts.append("\n".join(cur))
            cur = [line]
            cur_len = add
        else:
            cur.append(line)
            cur_len += add

    if cur:
        parts.append("\n".join(cur))

    return parts


def format_menu(result) -> str:
    header = f"{result.location} | {result.meal}"
    if result.date_str:
        header += f" | {result.date_str}"

    lines = [header]
    if result.hours:
        lines.append(f"Hours: {result.hours}")
    lines.append("")

    for cat, items in result.categories.items():
        lines.append(f"[{cat}]")
        for it in items:
            lines.append(f"- {it}")
        lines.append("")

    return "\n".join(lines).strip()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Create scraper once per user session
    context.user_data["scraper"] = MenuScraper()

    loc_lines = [f"{i}. {loc}" for i, loc in enumerate(DINING_LOCATIONS, start=1)]
    text = (
        f"Hi! I'm HungryBear 🐻\n"
        f"Time now: {now_str()}\n\n"
        f"Where do you want to eat today?\n"
        + "\n".join(loc_lines)
        + "\n\n"
        "Type a number (like 3) or tap a location button."
    )

    await update.message.reply_text(text, reply_markup=build_keyboard(DINING_LOCATIONS, cols=2))
    return CHOOSING_LOCATION


async def choose_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    scraper: MenuScraper = context.user_data.get("scraper") or MenuScraper()
    context.user_data["scraper"] = scraper

    loc = match_location(update.message.text)
    if not loc:
        await update.message.reply_text(
            "I couldn't match that location. Please type the number or tap a button.",
            reply_markup=build_keyboard(DINING_LOCATIONS, cols=2),
        )
        return CHOOSING_LOCATION

    context.user_data["location"] = loc

    # IMPORTANT: only show meals that actually exist for this hall today
    meals, dbg = scraper.get_available_meals(loc)
    if not meals:
        await update.message.reply_text(
            "Sorry, I couldn't detect available meals for that location today.\n"
            f"Debug: {dbg or '(none)'}",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    context.user_data["available_meals"] = meals

    await update.message.reply_text(
        f"Location = {loc}\nTime now: {now_str()}\n\n"
        "Which meal do you want? (Only showing meals this place actually has today)",
        reply_markup=build_keyboard(meals, cols=2),
    )
    return CHOOSING_MEAL


async def choose_meal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    scraper: MenuScraper = context.user_data.get("scraper") or MenuScraper()
    loc: str = context.user_data.get("location")
    meals: List[str] = context.user_data.get("available_meals") or []

    if not loc or not meals:
        await update.message.reply_text("Session lost 😅 Please send /start again.")
        return ConversationHandler.END

    user = norm(update.message.text)
    meal = None
    for m in meals:
        if norm(m) == user:
            meal = m
            break

    if not meal:
        await update.message.reply_text(
            "That meal isn't in the available list. Please tap one of the buttons.",
            reply_markup=build_keyboard(meals, cols=2),
        )
        return CHOOSING_MEAL

    await update.message.reply_text(
        f"OK! I will fetch: {loc} / {meal} ...",
        reply_markup=ReplyKeyboardRemove(),
    )

    result = scraper.get_menu(location=loc, meal=meal)

    if not result.categories:
        await update.message.reply_text(
            f"{loc} | {meal}\n\n"
            "I couldn't find menu items for that location/meal today.\n"
            f"Debug info:\n{result.debug or '(none)'}"
        )
        return ConversationHandler.END

    msg = format_menu(result)
    for part in split_long_message(msg):
        await update.message.reply_text(part)

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Canceled. Send /start to try again.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Commands:\n"
        "/start - choose dining hall and meal\n"
        "/cancel - cancel the current flow\n"
        "/help - show this message\n\n"
        "Tip: I only show meals that exist for the selected hall today."
    )


def main() -> None:
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN. Put it in .env (project root).")

    # ApplicationBuilder is the recommended modern pattern. :contentReference[oaicite:1]{index=1}
    app = ApplicationBuilder().token(token).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING_LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_location)],
            CHOOSING_MEAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_meal)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("help", help_cmd))

    # run_polling is the simplest beginner-friendly way to run the bot. :contentReference[oaicite:2]{index=2}
    app.run_polling()


if __name__ == "__main__":
    main()
