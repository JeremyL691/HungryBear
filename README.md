# HungryBear - Cal Student Dining Guide 🐻🍽️

A tiny UC Berkeley dining menu helper.

- Data source: https://dining.berkeley.edu/menus/
- Telegram bot (public): https://t.me/HungryBearsBot

## What you can do

- Pick a dining location
- Pick a meal (Breakfast/Lunch/Dinner/All Day when available)
- Get today’s menu with hours + categories

## Run locally

```bash
git clone https://github.com/JeremyL691/HungryBear.git
cd HungryBear
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
python -m src.telegram_bot
```

Create a `.env` file in the project root:

```env
TELEGRAM_BOT_TOKEN=YOUR_BOT_TOKEN_HERE
```

## License

MIT — see `LICENSE`.
