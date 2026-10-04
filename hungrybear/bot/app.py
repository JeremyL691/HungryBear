# hungrybear/bot/app.py
"""Telegram bot over the published menu data (never scrapes sites itself).

Every inline button carries its full state in callback_data, handled by one global
CallbackQueryHandler - so buttons keep working on old messages and after restarts.

  c                                 campus picker
  C|<campus>                        choose campus (remembered per user)
  L|<campus>|<yyyymmdd>             location list for a day
  l|<campus>|<yyyymmdd>|<loc>       meal picker for a location
  m|<campus>|<yyyymmdd>|<loc>|<M>   show a meal (M = one-letter meal code)
  d|<diet>                          set diet filter ("-" clears)
  x                                 close

Run (polling, e.g. on a Mac):  python -m hungrybear.bot
Env: TELEGRAM_BOT_TOKEN; HUNGRYBEAR_DATA_URL (published data) or HUNGRYBEAR_DATA_DIR (local dir).
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional, Sequence

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    PicklePersistence,
)

from ..config import PACIFIC
from ..models import DayMenu, Location
from ..store import LocalReadStore, RemoteStore
from . import render

log = logging.getLogger("hungrybear.bot")
DEFAULT_DATA_URL = "https://jeremyl691.github.io/HungryBear"
Button = InlineKeyboardButton


def now_pacific() -> datetime:
    return datetime.now(PACIFIC)


def ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


def parse_ymd(s: str) -> date:
    return datetime.strptime(s, "%Y%m%d").date()


def cb(*parts: str) -> str:
    data = "|".join(parts)
    assert len(data.encode()) <= 64, data  # Telegram limit
    return data


def rows(buttons: Sequence[Button], cols: int) -> List[List[Button]]:
    return [list(buttons[i : i + cols]) for i in range(0, len(buttons), cols)]


class Bot:
    def __init__(self, store) -> None:
        self.store = store

    # ---------------- data helpers ----------------

    async def campuses(self) -> List[dict]:
        return (await self.store.index()).get("campuses", [])

    async def campus_info(self, campus: str) -> Optional[dict]:
        return next((c for c in await self.campuses() if c["id"] == campus), None)

    async def available_dates(self, campus: str) -> List[date]:
        info = await self.campus_info(campus)
        today = now_pacific().date()
        return [d for d in (date.fromisoformat(s) for s in (info or {}).get("dates", [])) if d >= today][:7]

    # ---------------- screens: (text, keyboard) ----------------

    async def campus_screen(self) -> tuple[str, InlineKeyboardMarkup]:
        campuses = await self.campuses()
        if not campuses:
            return "Menu data is unavailable right now. Please try again later.", InlineKeyboardMarkup([])
        buttons = [Button(c["short_name"], callback_data=cb("C", c["id"])) for c in campuses]
        kb = rows(buttons, 3) + [[Button("❌ Close", callback_data="x")]]
        return "HungryBear 🐻 — pick your campus:", InlineKeyboardMarkup(kb)

    async def locations_screen(self, campus: str, d: date) -> tuple[str, InlineKeyboardMarkup]:
        info = await self.campus_info(campus)
        name = info["name"] if info else campus
        day = await self.store.day(campus, d)
        dates = await self.available_dates(campus)
        today = now_pacific().date()
        date_row = [
            Button(("• " if x == d else "") + render.day_label(x, today), callback_data=cb("L", campus, ymd(x)))
            for x in dates[:4]
        ]
        nav = [Button("🏫 Campus", callback_data="c"), Button("❌ Close", callback_data="x")]
        if day is None:
            text = f"{name} · {render.day_label(d, today)}\n\nNo menu published for this day yet."
            return text, InlineKeyboardMarkup([date_row, nav])
        locs = render.sort_locations(day.locations)
        loc_buttons = [
            Button(render.location_button(loc), callback_data=cb("l", campus, ymd(d), loc.id)) for loc in locs
        ]
        cols = 1 if max((len(b.text) for b in loc_buttons), default=0) > 18 else 2
        text = f"{name} · {render.day_title(d, today)}\n\nPick a location:"
        return text, InlineKeyboardMarkup(rows(loc_buttons, cols) + [date_row, nav])

    async def meals_screen(self, campus: str, d: date, loc_id: str) -> tuple[str, InlineKeyboardMarkup]:
        day = await self.store.day(campus, d)
        loc = day.location(loc_id) if day else None
        back = [Button("⬅️ Locations", callback_data=cb("L", campus, ymd(d)))]
        if loc is None:
            return "That location isn't on the menu for this day.", InlineKeyboardMarkup([back])
        today, now = now_pacific().date(), now_pacific()
        if loc.status != "open" or not loc.meals:
            return f"{loc.name} is closed {render.day_label(d, today).lower()}.", InlineKeyboardMarkup([back])
        current = render.current_or_next_meal(loc, d, now)
        meal_buttons = [
            Button(
                render.meal_button(m, render.is_now(m, d, now)),
                callback_data=cb("m", campus, ymd(d), loc.id, render.MEAL_CODES.get(m.name, "A")),
            )
            for m in loc.meals
        ]
        dates = await self.available_dates(campus)
        date_row = [
            Button(("• " if x == d else "") + render.day_label(x, today), callback_data=cb("l", campus, ymd(x), loc.id))
            for x in dates[:4]
        ]
        hint = "\n▶ = serving now" if current and render.is_now(current, d, now) else ""
        text = f"{loc.name} · {render.day_title(d, today)}\n\nChoose a meal:{hint}"
        return text, InlineKeyboardMarkup(rows(meal_buttons, 1) + [date_row, back])

    def menu_nav(self, campus: str, d: date, loc_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    Button("⬅️ Meals", callback_data=cb("l", campus, ymd(d), loc_id)),
                    Button("🏠 Locations", callback_data=cb("L", campus, ymd(d))),
                    Button("✅ Done", callback_data="x"),
                ]
            ]
        )

    async def menu_parts(self, campus: str, d: date, loc_id: str, code: str, diet: Optional[str]) -> List[str]:
        day: Optional[DayMenu] = await self.store.day(campus, d)
        loc: Optional[Location] = day.location(loc_id) if day else None
        meal_name = render.CODE_MEALS.get(code)
        meal = next((m for m in loc.meals if m.name == meal_name), None) if loc else None
        if not day or not loc or not meal:
            return ["That menu is no longer available — it may have been updated. Tap ⬅️ to go back."]
        info = await self.campus_info(campus)
        text = render.menu_text(day, loc, meal, info["short_name"] if info else campus, diet, now_pacific())
        return render.split_message(text)

    # ---------------- handlers ----------------

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        campus = context.user_data.get("campus")
        if campus and await self.campus_info(campus):
            text, kb = await self.locations_screen(campus, now_pacific().date())
        else:
            text, kb = await self.campus_screen()
        await update.effective_message.reply_text(text, reply_markup=kb)

    async def campus_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text, kb = await self.campus_screen()
        await update.effective_message.reply_text(text, reply_markup=kb)

    async def now_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Current (or next) meal at the last location the user looked at."""
        campus, loc_id = context.user_data.get("campus"), context.user_data.get("location")
        if not campus or not loc_id:
            await update.effective_message.reply_text("Pick a location first with /start, then /now will remember it.")
            return
        d = now_pacific().date()
        day = await self.store.day(campus, d)
        loc = day.location(loc_id) if day else None
        meal = render.current_or_next_meal(loc, d, now_pacific()) if loc else None
        if meal is None:
            text, kb = await self.meals_screen(campus, d, loc_id)
            await update.effective_message.reply_text(text, reply_markup=kb)
            return
        await self.send_parts(update, context, campus, d, loc_id, render.MEAL_CODES.get(meal.name, "A"), edit=False)

    async def diet_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(self.diet_text(context), reply_markup=self.diet_kb())

    def diet_text(self, context) -> str:
        cur = context.user_data.get("diet")
        return f"Diet filter: {render.DIETS[cur] if cur else 'off (showing everything)'}\nShow only:"

    def diet_kb(self) -> InlineKeyboardMarkup:
        opts = [Button(label, callback_data=cb("d", key)) for key, label in render.DIETS.items()]
        return InlineKeyboardMarkup([opts, [Button("Show everything", callback_data=cb("d", "-"))]])

    async def help_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(
            "HungryBear 🐻 — UC dining menus for all 9 campuses.\n\n"
            "/start – browse menus (remembers your campus)\n"
            "/now – what's being served now at your last location\n"
            "/campus – switch campus\n"
            "/diet – only show vegetarian / vegan / halal dishes\n"
            "/help – this message\n\n"
            "Menus are collected from each campus's official dining site several times a day."
        )

    async def send_parts(self, update, context, campus: str, d: date, loc_id: str, code: str, *, edit: bool) -> None:
        parts = await self.menu_parts(campus, d, loc_id, code, context.user_data.get("diet"))
        nav = self.menu_nav(campus, d, loc_id)
        chat_id = update.effective_chat.id
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            kb = nav if last else None
            if i == 0 and edit and update.callback_query:
                await update.callback_query.edit_message_text(part, reply_markup=kb)
            else:
                await context.bot.send_message(chat_id, part, reply_markup=kb)

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        await q.answer()
        parts = (q.data or "").split("|")
        action, args = parts[0], parts[1:]
        try:
            if action == "x":
                await q.edit_message_reply_markup(reply_markup=None)
                return
            if action == "c":
                text, kb = await self.campus_screen()
            elif action == "C":
                context.user_data["campus"] = args[0]
                text, kb = await self.locations_screen(args[0], now_pacific().date())
            elif action == "L":
                text, kb = await self.locations_screen(args[0], parse_ymd(args[1]))
            elif action == "l":
                context.user_data.update(campus=args[0], location=args[2])
                text, kb = await self.meals_screen(args[0], parse_ymd(args[1]), args[2])
            elif action == "m":
                context.user_data.update(campus=args[0], location=args[2])
                await self.send_parts(update, context, args[0], parse_ymd(args[1]), args[2], args[3], edit=True)
                return
            elif action == "d":
                context.user_data["diet"] = None if args[0] == "-" else args[0]
                text, kb = self.diet_text(context), self.diet_kb()
            else:
                return
            await q.edit_message_text(text, reply_markup=kb)
        except BadRequest as e:
            if "not modified" not in str(e).lower():  # pressing the same button twice
                raise


COMMANDS = [
    ("start", "Browse menus"),
    ("now", "What's being served now at your last location"),
    ("campus", "Switch campus"),
    ("diet", "Vegetarian / vegan / halal filter"),
    ("help", "How to use HungryBear"),
]


async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands(COMMANDS)


def build_app(token: str, store, persistence_path: Path) -> Application:
    persistence_path.parent.mkdir(parents=True, exist_ok=True)
    app = (
        ApplicationBuilder()
        .token(token)
        .persistence(PicklePersistence(filepath=persistence_path))
        .post_init(_post_init)
        .build()
    )
    bot = Bot(store)
    app.add_handler(CommandHandler("start", bot.start))
    app.add_handler(CommandHandler("campus", bot.campus_cmd))
    app.add_handler(CommandHandler("now", bot.now_cmd))
    app.add_handler(CommandHandler("diet", bot.diet_cmd))
    app.add_handler(CommandHandler("help", bot.help_cmd))
    app.add_handler(CallbackQueryHandler(bot.on_button))
    return app


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN (put it in .env)")
    data_dir = os.getenv("HUNGRYBEAR_DATA_DIR")
    store = (
        LocalReadStore(Path(data_dir)) if data_dir else RemoteStore(os.getenv("HUNGRYBEAR_DATA_URL", DEFAULT_DATA_URL))
    )
    persistence = Path(os.getenv("HUNGRYBEAR_STATE", Path.home() / ".hungrybear" / "bot.pickle"))
    log.info("data: %s", data_dir or os.getenv("HUNGRYBEAR_DATA_URL", DEFAULT_DATA_URL))
    build_app(token, store, persistence).run_polling(allowed_updates=Update.ALL_TYPES)
