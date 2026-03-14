# HungryBear 🐻🍽️

HungryBear is a tiny Python project that fetches **UC Berkeley Dining** menus from:

- https://dining.berkeley.edu/menus/

It started as a “I just want dinner” script and slowly turned into something I can actually use from my phone.

> Jeremy’s note: I built this so I could stop clicking tiny dropdown arrows like it’s a boss fight.

---

## What it does

- Lets you pick a dining location
- Detects which meals are available **for that location today** (Breakfast/Lunch/Dinner/All Day)
- Fetches + parses the menu into:
  - date + hours
  - categories (e.g. Dessert, Soup…)
  - dish lists under each category
- Optional chat interface:
  - Telegram polling bot (great for testing)
  - Telegram webhook service (deployable)

---

## Requirements

- Python 3.10+ recommended (3.11 tested)
- Internet connection (HungryBear cannot eat offline)

---

## Quick start (local)

```bash
git clone https://github.com/JeremyL691/HungryBear.git
cd HungryBear
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
python -m src.hungrybear
```

---

## Reliability notes (a.k.a. why it sometimes used to fail)

`dining.berkeley.edu` changes naming and layout over time.

To make scraping stable, HungryBear now:

- Matches dining hall names **accent-insensitively** (Café/Cafe both work)
- Supports **aliases** for older UI names (e.g. “Clark Kerr Campus” → “Clark Kerr”, “Den” → “The Den”)
- Uses a short **retry + backoff** on fetch
- Uses a short **in-memory cache** (so one lookup flow doesn’t fetch twice)

---

# Telegram interface (optional)

This repo includes a Telegram UI because sometimes you just want to query menus while walking.

## Option A — Local polling (easy)

### 1) Create a Telegram bot token

In Telegram:

1. Search `@BotFather`
2. Run `/newbot`
3. Copy the token

### 2) Create `.env` in the project root

Create a `.env` file next to `requirements.txt`:

```env
TELEGRAM_BOT_TOKEN=YOUR_BOT_TOKEN_HERE
```

### 3) Run

```bash
python -m src.telegram_bot
```

Then in Telegram:

- `/start`

The Telegram flow is **English-only** and uses **inline buttons** (Back/Cancel) to avoid spamming the chat.

---

## Option B — Webhook service (deployable)

Start the webhook server (example):

```bash
uvicorn src.telegram_webhook:app --host 0.0.0.0 --port $PORT
```

Environment variables:

- `TELEGRAM_BOT_TOKEN`
- `WEBHOOK_BASE_URL` (your public base URL)
- `TELEGRAM_WEBHOOK_SECRET` (recommended)

---

## macOS LaunchAgent (my laptop mode)

If you want the polling bot to keep running on a Mac in the background, you can use a LaunchAgent.

This repo may include a local `run_telegram_bot.sh` on your machine, but **do not commit** your `.env` or venv.

---

## License

MIT — see `LICENSE`.
