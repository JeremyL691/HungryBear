# src/telegram_bot.py

from __future__ import annotations

import os
from datetime import datetime
from typing import List, Optional

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import unicodedata

from .constants import DINING_LOCATIONS, LOCATION_ALIASES
from .menu_scraper import MenuScraper

# Conversation states (just numbers)
CHOOSING_LOCATION, CHOOSING_MEAL = range(2)

BACK = "⬅️ Back"
CANCEL = "❌ Cancel"

CB_LOC = "loc"
CB_MEAL = "meal"
CB_BACK = "back"        # go back / restart
CB_CANCEL = "cancel"    # close


def now_str() -> str:
    # Student dev note: printing local time helps users trust "today".
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def norm(s: str) -> str:
    s = strip_accents(s or "")
    return " ".join(s.strip().lower().split())


def build_keyboard(items: List[str], cols: int = 2, extra_rows: Optional[List[List[str]]] = None) -> ReplyKeyboardMarkup:
    """Build a reply keyboard (old-school Telegram keyboard)."""
    rows: List[List[str]] = []
    row: List[str] = []
    for it in items:
        row.append(it)
        if len(row) >= cols:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    if extra_rows:
        rows.extend(extra_rows)

    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


def build_inline_keyboard(buttons: List[InlineKeyboardButton], cols: int = 2, extra_rows: Optional[List[List[InlineKeyboardButton]]] = None) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for b in buttons:
        row.append(b)
        if len(row) >= cols:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if extra_rows:
        rows.extend(extra_rows)
    return InlineKeyboardMarkup(rows)


def match_location(user_text: str) -> Optional[str]:
    """Match user input to a canonical dining location.

    Accepts:
    - number like "3" (based on the current button list order)
    - canonical location name
    - legacy/alias names (website naming changes over time)
    """
    raw = (user_text or "").strip()
    if raw.isdigit():
        idx = int(raw)
        if 1 <= idx <= len(DINING_LOCATIONS):
            return DINING_LOCATIONS[idx - 1]

    user_norm = norm(raw)

    # canonical names
    for loc in DINING_LOCATIONS:
        if norm(loc) == user_norm:
            return loc

    # aliases
    for canon, aliases in LOCATION_ALIASES.items():
        if norm(canon) == user_norm:
            return canon
        for a in aliases:
            if norm(a) == user_norm:
                return canon

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


def _meal_pretty(m: str) -> str:
    return {
        "Breakfast": "🍳 Breakfast",
        "Lunch": "🥪 Lunch",
        "Dinner": "🍽️ Dinner",
        "All Day": "🕒 All Day",
    }.get(m, m)


async def _send_location_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool = False) -> None:
    buttons = [InlineKeyboardButton(loc, callback_data=f"{CB_LOC}|{loc}") for loc in DINING_LOCATIONS]
    kb = build_inline_keyboard(buttons, cols=2, extra_rows=[[InlineKeyboardButton(CANCEL, callback_data=CB_CANCEL)]])

    text = "HungryBear 🐻\n" f"Time now: {now_str()}\n\nPick a dining location:"

    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb)
    else:
        await update.message.reply_text(text, reply_markup=kb)


async def _send_meal_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, loc: str, meals: List[str]) -> None:
    buttons = [InlineKeyboardButton(_meal_pretty(m), callback_data=f"{CB_MEAL}|{m}") for m in meals]
    kb = build_inline_keyboard(
        buttons,
        cols=2,
        extra_rows=[[InlineKeyboardButton(BACK, callback_data=CB_BACK), InlineKeyboardButton(CANCEL, callback_data=CB_CANCEL)]],
    )

    text = f"Location: {loc}\n\nChoose a meal:"
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb)
    else:
        await update.message.reply_text(text, reply_markup=kb)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Create scraper once per user session
    context.user_data["scraper"] = MenuScraper()
    await _send_location_menu(update, context, edit=False)
    return CHOOSING_LOCATION


async def choose_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Fallback for users typing instead of tapping inline buttons."""
    scraper: MenuScraper = context.user_data.get("scraper") or MenuScraper()
    context.user_data["scraper"] = scraper

    txt = (update.message.text or "").strip()
    if txt == CANCEL:
        return await cancel(update, context)

    loc = match_location(txt)
    if not loc:
        await update.message.reply_text("Couldn't match that location. Send /start to try again.")
        return ConversationHandler.END

    context.user_data["location"] = loc

    meals, dbg = scraper.get_available_meals(loc)
    if not meals:
        await update.message.reply_text(
            "Sorry — I couldn't detect available meals for that location today.\n" f"Debug: {dbg or '(none)'}"
        )
        return ConversationHandler.END

    context.user_data["available_meals"] = meals
    await _send_meal_menu(update, context, loc, meals)
    return CHOOSING_MEAL


async def choose_meal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Fallback when user types instead of tapping inline meal buttons."""
    scraper: MenuScraper = context.user_data.get("scraper") or MenuScraper()
    loc: str = context.user_data.get("location")
    meals: List[str] = context.user_data.get("available_meals") or []

    if not loc or not meals:
        await update.message.reply_text("Session lost 😅 Please send /start again.")
        return ConversationHandler.END

    txt = (update.message.text or "").strip()
    if txt in (BACK, CANCEL):
        return await cancel(update, context)

    user = norm(txt)
    user = user.replace("🍳", "").replace("🥪", "").replace("🍽️", "").replace("🕒", "")
    user = norm(user)

    meal = None
    for m in meals:
        if norm(m) == user:
            meal = m
            break

    if not meal:
        await update.message.reply_text("That meal isn't available. Send /start to try again.")
        return ConversationHandler.END

    await update.message.reply_text(f"Fetching: {loc} / {meal} …")

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

    await update.message.reply_text("Send /start to look up another menu.")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Canceled. Send /start to begin again.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Commands:\n"
        "/start - start menu flow\n"
        "/cancel - cancel\n"
        "/help - help\n\n"
        "Tip: Use the inline buttons (no need to type)."
    )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle inline button presses."""
    q = update.callback_query
    if not q:
        return ConversationHandler.END

    await q.answer()
    data = (q.data or "").strip()

    scraper: MenuScraper = context.user_data.get("scraper") or MenuScraper()
    context.user_data["scraper"] = scraper

    if data == CB_CANCEL:
        await q.edit_message_text("OK. See you next time.")
        return ConversationHandler.END

    if data == CB_BACK:
        # Back to location picker
        context.user_data.pop("location", None)
        context.user_data.pop("available_meals", None)
        await _send_location_menu(update, context, edit=True)
        return CHOOSING_LOCATION

    if data.startswith(CB_LOC + "|"):
        loc = data.split("|", 1)[1]
        context.user_data["location"] = loc
        meals, dbg = scraper.get_available_meals(loc)
        if not meals:
            await q.edit_message_text(f"Sorry — couldn't detect meals for {loc}.\nDebug: {dbg or '(none)'}")
            return ConversationHandler.END
        context.user_data["available_meals"] = meals
        await _send_meal_menu(update, context, loc, meals)
        return CHOOSING_MEAL

    if data.startswith(CB_MEAL + "|"):
        meal = data.split("|", 1)[1]
        loc = context.user_data.get("location")
        meals: List[str] = context.user_data.get("available_meals") or []
        if not loc or not meals:
            await q.edit_message_text("Session lost 😅 Send /start again.")
            return ConversationHandler.END
        if meal not in meals:
            await q.edit_message_text("That meal isn't available. Send /start again.")
            return ConversationHandler.END

        # acknowledge + show result in new messages (avoid giant edits)
        await q.edit_message_text(f"Fetching: {loc} / {meal} …")
        result = scraper.get_menu(location=loc, meal=meal)
        if not result.categories:
            await context.bot.send_message(
                chat_id=q.message.chat_id,
                text=f"{loc} | {meal}\n\nI couldn't find menu items today.\nDebug:\n{result.debug or '(none)'}",
            )
            return ConversationHandler.END

        msg = format_menu(result)
        for part in split_long_message(msg):
            await context.bot.send_message(chat_id=q.message.chat_id, text=part)

        # Provide a clean "next action" inline menu instead of telling the user to type.
        next_kb = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("🔄 Look up another", callback_data=CB_BACK),
                InlineKeyboardButton("✅ Done", callback_data=CB_CANCEL),
            ]]
        )
        await context.bot.send_message(chat_id=q.message.chat_id, text="Look up another menu?", reply_markup=next_kb)
        return ConversationHandler.END

    return ConversationHandler.END


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
            CHOOSING_LOCATION: [
                CallbackQueryHandler(on_button),
                MessageHandler(filters.TEXT & ~filters.COMMAND, choose_location),
            ],
            CHOOSING_MEAL: [
                CallbackQueryHandler(on_button),
                MessageHandler(filters.TEXT & ~filters.COMMAND, choose_meal),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(on_button),
        ],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("help", help_cmd))

    # run_polling is the simplest beginner-friendly way to run the bot. :contentReference[oaicite:2]{index=2}
    app.run_polling()


if __name__ == "__main__":
    main()
