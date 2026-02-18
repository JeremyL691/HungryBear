# src/telegram_webhook.py

from __future__ import annotations

import os
from typing import Optional

from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from .constants import DINING_LOCATIONS
from .menu_scraper import MenuScraper

# ---------- same conversation flow as your local bot ----------
CHOOSING_LOCATION, CHOOSING_MEAL = range(2)


def _norm(s: str) -> str:
    return " ".join((s or "").strip().lower().split())


def _match_location(user_text: str) -> Optional[str]:
    raw = (user_text or "").strip()
    if raw.isdigit():
        idx = int(raw)
        if 1 <= idx <= len(DINING_LOCATIONS):
            return DINING_LOCATIONS[idx - 1]

    user_norm = _norm(raw)
    for loc in DINING_LOCATIONS:
        if _norm(loc) == user_norm:
            return loc
    return None


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Student dev note: create scraper per user session
    context.user_data["scraper"] = MenuScraper()

    loc_lines = [f"{i}. {loc}" for i, loc in enumerate(DINING_LOCATIONS, start=1)]
    text = (
        "Hi! I'm HungryBear 🐻\n\n"
        "Where do you want to eat today?\n"
        + "\n".join(loc_lines)
        + "\n\n"
        "Type a number (like 3) or type the location name."
    )
    await update.message.reply_text(text)
    return CHOOSING_LOCATION


async def choose_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    scraper: MenuScraper = context.user_data.get("scraper") or MenuScraper()
    context.user_data["scraper"] = scraper

    loc = _match_location(update.message.text)
    if not loc:
        await update.message.reply_text("I couldn't match that location. Please type the number or exact name.")
        return CHOOSING_LOCATION

    context.user_data["location"] = loc

    meals, dbg = scraper.get_available_meals(loc)
    if not meals:
        await update.message.reply_text(
            "Sorry, I couldn't detect available meals for that location today.\n"
            f"Debug: {dbg or '(none)'}"
        )
        return ConversationHandler.END

    context.user_data["available_meals"] = meals

    # Show only what exists for this hall today
    await update.message.reply_text(
        f"Location = {loc}\n\n"
        "Which meal? (Only showing meals that exist for this place today)\n"
        + "\n".join([f"- {m}" for m in meals])
        + "\n\nType the meal name exactly (e.g., Lunch / Dinner / All Day)."
    )
    return CHOOSING_MEAL


async def choose_meal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    scraper: MenuScraper = context.user_data.get("scraper") or MenuScraper()
    loc: str = context.user_data.get("location")
    meals = context.user_data.get("available_meals") or []

    if not loc or not meals:
        await update.message.reply_text("Session lost. Please send /start again.")
        return ConversationHandler.END

    user = _norm(update.message.text)
    meal = None
    for m in meals:
        if _norm(m) == user:
            meal = m
            break

    if not meal:
        await update.message.reply_text("That meal isn't available here today. Please type one from the list I showed.")
        return CHOOSING_MEAL

    await update.message.reply_text(f"OK! Fetching: {loc} / {meal} ...")

    result = scraper.get_menu(location=loc, meal=meal)
    if not result.categories:
        await update.message.reply_text(
            f"{loc} | {meal}\n\n"
            "I couldn't find menu items.\n"
            f"Debug:\n{result.debug or '(none)'}"
        )
        return ConversationHandler.END

    # Build output (plain text)
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

    text = "\n".join(lines).strip()

    # Telegram limit is 4096 chars; split if needed
    limit = 3800
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        await update.message.reply_text(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        await update.message.reply_text(text)

    return ConversationHandler.END


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Commands:\n"
        "/start - choose dining hall and meal\n"
        "/help - show this message\n\n"
        "Tip: I only show meals that exist for the selected hall today."
    )


# ---------- Webhook server (Starlette) ----------
ptb_app: Optional[Application] = None


async def health(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


async def telegram_webhook(request: Request) -> PlainTextResponse:
    """
    Telegram will POST updates here.
    We verify secret token header if you set one.
    Telegram sends header: X-Telegram-Bot-Api-Secret-Token when you set secret_token. :contentReference[oaicite:3]{index=3}
    """
    global ptb_app
    if ptb_app is None:
        return PlainTextResponse("bot not ready", status_code=503)

    secret_expected = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if secret_expected:
        secret_got = request.headers.get("x-telegram-bot-api-secret-token", "")
        if secret_got != secret_expected:
            return PlainTextResponse("forbidden", status_code=403)

    data = await request.json()
    update = Update.de_json(data, ptb_app.bot)
    await ptb_app.process_update(update)
    return PlainTextResponse("ok")


routes = [
    Route("/health", health, methods=["GET"]),
    Route("/telegram", telegram_webhook, methods=["POST"]),
]

app = Starlette(routes=routes)


@app.on_event("startup")
async def on_startup() -> None:
    """
    Start PTB and set webhook.
    Render provides PORT; base URL is your service URL.
    """
    global ptb_app

    load_dotenv()  # works locally; on Render, env vars come from dashboard

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")

    base_url = os.getenv("WEBHOOK_BASE_URL", "").strip().rstrip("/")
    if not base_url:
        raise RuntimeError("Missing WEBHOOK_BASE_URL (example: https://your-service.onrender.com)")

    # Build telegram application
    ptb_app = ApplicationBuilder().token(token).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start_cmd)],
        states={
            CHOOSING_LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_location)],
            CHOOSING_MEAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_meal)],
        },
        fallbacks=[],
    )

    ptb_app.add_handler(conv)
    ptb_app.add_handler(CommandHandler("help", help_cmd))

    await ptb_app.initialize()
    await ptb_app.start()

    # Set webhook (Telegram will push updates to your Render URL)
    webhook_url = f"{base_url}/telegram"
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if secret:
        await ptb_app.bot.set_webhook(url=webhook_url, secret_token=secret)
    else:
        await ptb_app.bot.set_webhook(url=webhook_url)

    # Student dev note: if this works, your bot is "always reachable".
    # Render may spin down when idle, but Telegram will hit this URL and wake it up. :contentReference[oaicite:4]{index=4}


@app.on_event("shutdown")
async def on_shutdown() -> None:
    global ptb_app
    if ptb_app is None:
        return

    # Optional: remove webhook on shutdown
    try:
        await ptb_app.bot.delete_webhook(drop_pending_updates=False)
    except Exception:
        pass

    await ptb_app.stop()
    await ptb_app.shutdown()
    ptb_app = None
