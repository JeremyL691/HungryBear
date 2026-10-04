# HungryBear 🐻🍽️

Dining-hall menus for all **9 UC undergraduate campuses**, collected automatically several times a day,
published as a free JSON API, and served by a Telegram bot.

- Telegram bot: https://t.me/HungryBearsBot
- JSON API: `https://jeremyl691.github.io/HungryBear/v1/index.json` (GitHub Pages, `data` branch)

| Campus | Source | Notes |
|---|---|---|
| Berkeley | dining.berkeley.edu (WordPress `cal-dining`) | 7 days, diet tags + allergens |
| UCLA | dining.ucla.edu menus-at-a-glance + /hours/ | 7 days; quick-service spots are hours-only |
| UCSB | apps.dining.ucsb.edu | 7 days |
| UCSC | nutrition.sa.ucsc.edu (FoodPro) | 7 days; server sends an incomplete TLS chain (see `hungrybear/certs/`) |
| UCR | foodpro.ucr.edu (FoodPro) | 7 days; no hours published |
| UCSD | hdh-web.ucsd.edu (HDH) | 7 days, calories; no per-meal hours |
| Davis | housing.ucdavis.edu dining commons pages | current Sun–Sat week only |
| UCI | uci.mydininghub.com (Aramark GraphQL) | 7 days, calories, holiday-aware hours |
| Merced | bigZpoon menu widget API | weekly cycle menu, current week only |

## Architecture

```
GitHub Actions (cron)                                          your Mac / any host
collect.yml ─ collector ─ 9 adapters → validate → data branch ─┬─ GitHub Pages = public JSON API
                    │                                          └─ Telegram bot (reads JSON only)
                    └ report.py ─ issue + Telegram alert ─ dispatch autofix.yml ─ Claude → PR (you merge)
```

- **Adapters** (`hungrybear/adapters/`) – one per menu platform; each turns a site into the shared
  schema in `hungrybear/models.py`. Bound to campuses in `hungrybear/campuses.yaml`.
- **Validator** (`hungrybear/validate.py`) – the guard against *silent* breakage. A day is `broken` if a
  main hall is missing, a hall's biggest meal is implausibly thin, item names look like page chrome, or the
  item count collapses versus previous weeks. `closed` (weekends/breaks) is not an error.
- **Collector** (`hungrybear/collector.py`) – fetch → validate → write `v1/{campus}/{date}.json`. A broken
  day never overwrites the last good file. `status.json` tracks consecutive failures per campus.
- **Alerts** (`hungrybear/report.py`) – after 2 consecutive failures: GitHub issue (`scraper-broken`,
  `campus:<id>`), Telegram message to the admin, and an `autofix` run. Recovery closes the issue.
- **Autofix** (`.github/workflows/autofix.yml`) – Claude Code gets the live failure + raw responses and
  edits the adapter. A PR is opened only if it stayed within `adapters/`, `campuses.yaml`, `tests/`,
  `pytest` passes, and a fresh live collection validates. You review and merge.
- **Fixtures** (`tests/fixtures/<campus>/`) – recorded responses + a human-readable golden summary
  (`expected.txt`). Tests replay them with no network.

## Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[bot,dev]"
pytest -q

# collect live data into ./data (same as CI)
python -m hungrybear.collector --campus all --days 7 --out data
python -m hungrybear.collector --campus ucla --days 2 --out /tmp/out -v

# refresh a campus fixture after a site change you've handled
python -m hungrybear.devtools fixture ucla --days 2
python -m hungrybear.devtools golden ucla       # re-render expected.txt from existing recording
```

## Running the bot (polling, e.g. on a Mac)

`.env` in the project root:

```env
TELEGRAM_BOT_TOKEN=...
# optional: read a local data dir instead of the published API
# HUNGRYBEAR_DATA_DIR=data
```

```bash
python -m hungrybear.bot
```

Commands: `/start` (remembers your campus), `/now`, `/campus`, `/diet`, `/help`. User preferences are
stored in `~/.hungrybear/bot.pickle`.

## One-time GitHub setup

1. **Default branch** – scheduled workflows only run on the default branch; merge this work there.
2. **Pages** – Settings → Pages → Deploy from a branch → `data` / `(root)` (after the first collect run
   creates the branch).
3. **Actions permissions** – Settings → Actions → General → Workflow permissions: *Read and write*, and
   allow GitHub Actions to create pull requests (needed by autofix).
4. **Secrets** – `ANTHROPIC_API_KEY` (autofix), `TELEGRAM_BOT_TOKEN` + `ADMIN_CHAT_ID` (alerts; optional).
5. Run **collect** once manually (Actions → collect → Run workflow).

PRs opened by the autofix bot use the workflow token, so they don't trigger `ci.yml` by themselves; the
autofix job already ran the tests and a live validation before opening the PR.

## License

MIT — see `LICENSE`.
