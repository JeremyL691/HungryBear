# HungryBear 🐻🍽️

HungryBear is a small Python project that fetches **UC Berkeley Dining** menus from:

- https://dining.berkeley.edu/menus/

It started as a “I just want dinner” tool and ended up becoming a neat little project I can run anywhere.

> Jeremy’s note: I built this so I could stop clicking tiny dropdown arrows like it’s a boss fight.
> 

---

## What it does

- Lists dining locations
- Detects which meals are available **for the chosen location today**
- Fetches and parses the menu into:
    - categories (e.g. `Dessert`, `Soup`, etc.)
    - dish lists under each category
- Outputs results in a readable format
- Includes a deployable web service version (useful for chat interfaces)

---

## Repo

- GitHub: https://github.com/JeremyL691/HungryBear
- Author: https://github.com/JeremyL691

---

## Project Structure

```
HungryBear/
  src/
    __init__.py
    constants.py
    menu_scraper.py
    hungrybear.py
    telegram_bot.py
    telegram_webhook.py
  requirements.txt
  .gitignore
  .env (NOT committed)
  LICENSE
  README.md
```

---

## Requirements

- Python 3.10+ recommended
- Internet connection (HungryBear cannot eat offline)

---

## Local Setup

### 1) Clone

```bash
git clone https://github.com/JeremyL691/HungryBear.git
cd HungryBear
```

### 2) Create a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
```

### 3) Install dependencies

```bash
pip install -r requirements.txt
```

---

## Run Locally (CLI)

```bash
python -m src.hungrybear
```

---

## Notes / Gotchas

- This project depends on the current layout of `dining.berkeley.edu`.
    
    If the site changes, parsing rules might need updates.
    
- Menu availability changes daily and varies by location.
    
    If a meal “doesn’t exist” for a hall, that’s usually normal.
    

---

# Deploy a Telegram Chat Interface (Optional)

If you want to use HungryBear from your phone and share it with others, you can deploy the Telegram chat interface.

There are two ways:

- **Local polling** (easy for development)
- **Render webhook** (recommended for deployment)

---

## Option A — Local (Polling)

### 1) Create a Telegram bot token

In Telegram:

1. Search `@BotFather`
2. Run `/newbot`
3. Copy the token

### 2) Create `.env` in project root

Create a `.env` file next to `requirements.txt`:

```
TELEGRAM_BOT_TOKEN=YOUR_BOT_TOKEN_HERE
```

### 3) Run

```bash
python -m src.telegram_bot
```

Then in Telegram:

- `/start`

> Note: polling requires your computer to stay running.
> 

---

## Option B — Render Free Web Service (Webhook) (Recommended)

This setup is the easiest “almost free 24/7” deployment:

- Render free services can sleep when idle
- Telegram webhook traffic wakes them up automatically

### 1) Create a Render Web Service

- Connect this repo: https://github.com/JeremyL691/HungryBear
- Runtime: Python 3
- Branch: `main`
- Instance type: Free

### 2) Build & Start Commands

**Build Command**

```bash
pip install -r requirements.txt
```

**Start Command**

```bash
uvicorn src.telegram_webhook:app --host 0.0.0.0 --port $PORT
```

### 3) Environment Variables (Render → Environment)

Set:

- `TELEGRAM_BOT_TOKEN` = your BotFather token
- `WEBHOOK_BASE_URL` = your Render service URL
    
    Example:
    
    ```
    https://hungrybear.onrender.com
    ```
    
- `TELEGRAM_WEBHOOK_SECRET` (recommended) = any random string
    
    Example:
    
    ```
    hungrybear_secret_pls_dont_guess
    ```
    

### 4) Verify

Open:

```
https://YOUR-SERVICE.onrender.com/health
```

Expected response:

```
ok
```

Then test in Telegram:

- `/start`

> Render Free note: the service may “spin down” when idle, so the first request after a long break can be slower.
> 

---

## License

MIT License — see the `LICENSE` file for details.