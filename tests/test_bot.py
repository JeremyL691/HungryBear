# tests/test_bot.py
"""Drive the bot's button flow end-to-end against fixture data, with Telegram mocked."""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from hungrybear.bot import app as botapp
from hungrybear.bot import render
from hungrybear.collector import update_status
from hungrybear.config import PACIFIC, load_campuses
from hungrybear.devtools import FIXTURES, replay_days
from hungrybear.store import LocalReadStore, LocalStore

CAMPUSES = sorted(p.name for p in FIXTURES.iterdir() if (p / "meta.json").exists())
NOW = datetime(2026, 10, 5, 12, 30, tzinfo=PACIFIC)  # Monday lunch


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    root = tmp_path_factory.mktemp("data")
    local = LocalStore(root)
    for campus in ("ucb", "ucla"):
        for menu in replay_days(campus):
            local.write_day(menu)
    update_status(local, {c: cfg for c, cfg in load_campuses().items() if c in ("ucb", "ucla")}, [], NOW.date())
    return LocalReadStore(root)


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    monkeypatch.setattr(botapp, "now_pacific", lambda: NOW)


def press(bot, data, user_data):
    q = MagicMock(data=data, answer=AsyncMock(), edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock())
    update = MagicMock(callback_query=q)
    update.effective_chat.id = 1
    ctx = MagicMock(user_data=user_data)
    ctx.bot.send_message = AsyncMock()
    asyncio.run(bot.on_button(update, ctx))
    edits = [(c.args[0], c.kwargs.get("reply_markup")) for c in q.edit_message_text.call_args_list]
    sent = [(c.args[1], c.kwargs.get("reply_markup")) for c in ctx.bot.send_message.call_args_list]
    return edits + sent


def buttons(markup):
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row] if markup else []


def test_full_flow_campus_location_meal_and_back(store):
    bot, ud = botapp.Bot(store), {}
    ((text, kb),) = press(bot, "C|ucb", ud)
    assert ud["campus"] == "ucb" and "Pick a location" in text
    assert ("Café 3", "l|ucb|20261005|cafe-3") in buttons(kb)

    ((text, kb),) = press(bot, "l|ucb|20261005|cafe-3", ud)
    labels = [t for t, _ in buttons(kb)]
    assert any(t.startswith("▶ ") and "Lunch" in t for t in labels), labels  # serving now at 12:30

    out = press(bot, "m|ucb|20261005|cafe-3|L", ud)
    text, nav = out[-1]
    assert "Café 3 · Lunch" in out[0][0] and "【" in out[0][0]
    # The buttons under a menu are stateless, so they work on old messages too (old bot: dead buttons).
    for _, data in buttons(nav):
        if data != "x":
            assert press(bot, data, {}), data


def test_closed_location_and_unknown_day(store):
    bot = botapp.Bot(store)
    ((text, _),) = press(bot, "l|ucb|20261005|browns", {})
    assert "closed" in text
    ((text, _),) = press(bot, "L|ucb|20261020", {})
    assert "No menu published" in text


def test_diet_filter_hides_non_matching_items(store):
    bot = botapp.Bot(store)
    plain = "".join(t for t, _ in press(bot, "m|ucb|20261005|cafe-3|L", {}))
    vegan = "".join(t for t, _ in press(bot, "m|ucb|20261005|cafe-3|L", {"diet": "vegan"}))
    assert len(vegan) < len(plain) and "Filter: 🌱 Vegan" in vegan


def test_callback_data_fits_telegram_limit_for_every_location():
    for campus in CAMPUSES:
        for menu in replay_days(campus):
            for loc in menu.locations:
                for m in loc.meals:
                    botapp.cb("m", campus, botapp.ymd(menu.date), loc.id, render.MEAL_CODES[m.name])


def test_split_message_respects_limit():
    text = "\n".join(f"• dish number {i}" for i in range(2000))
    parts = render.split_message(text)
    assert len(parts) > 1 and all(len(p) <= render.TELEGRAM_LIMIT for p in parts)
    assert "\n".join(parts) == text
