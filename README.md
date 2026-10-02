# Finance bot for Malaysia

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/finance-agent-malaysia?utm_medium=integration&utm_source=button&utm_campaign=finance-agent-malaysia)

A personal finance tracker for people living in Malaysia. It reads Maybank and Touch 'n Go statements, keeps a ledger in Postgres and talks to you through a Telegram bot, with weekly and monthly reports and a web dashboard.

It is built for Russian-speaking expats: the bot and the dashboard speak Russian, and next to ringgit it tracks a ruble account and MYR → RUB transfers (plus crypto wallets, if you have them).

It is **single-user**: everyone deploys their own copy. Your statements stay in your own Railway project and your own database, and the bot answers only your Telegram account.

```
statements ──► parsers ──► ledger (Postgres) ──► reports ──► Telegram bot / weekly & monthly messages
  PDF/CSV from          dedup, rules,              │
  bot / API            transfers, pots           └──► dashboard (/)
Apple Pay events, payment screenshots ──► pending rows, confirmed by the statement later
```

## What it does

- **Statements**: send a TNG eWallet PDF or a Maybank PDF/CSV to the bot (password-protected PDFs included). Rows are deduplicated, so re-sending a statement or an overlapping one is safe.
- **Categories**: rules for common Malaysian merchants out of the box. For an unknown shop the bot asks once and remembers.
- **Transfers are not spending**:
  - Maybank → TNG top-ups pair up automatically;
  - Maybank Tabung savings pots are tracked;
  - money sent to people waits for your decision in `/review`: a transfer to your Russian account (with the rate), your other account, or an expense.
- **Rubles and cash**: type `1500₽ такси` or `25 rm обед` in the bot.
- **Reports**:
  - a weekly report every Monday;
  - a monthly report on the first day of your financial month, which can follow your payday;
  - `/week`, `/month`, `/balance` on demand;
  - a dashboard behind a login link from `/web`.
- **Budget**: `/budget` forecasts the next six months from your balances, salary and usual spending; `/plan` keeps future expenses; `/ask` lets Claude check whether a purchase fits (optional, needs `ANTHROPIC_API_KEY`).

## Deploy on Railway

You need a Railway account and about five minutes.

1. **Create a bot**: in Telegram open [@BotFather](https://t.me/BotFather), `/newbot`, and copy the token.
2. **Find your Telegram id**: open [@userinfobot](https://t.me/userinfobot) and copy the number. A `@username` does not work.
3. **Deploy**: press the button and paste those two values. The template adds Postgres, generates the secrets and a public domain; on every start the container runs migrations and loads the reference data.

   [![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/finance-agent-malaysia?utm_medium=integration&utm_source=button&utm_campaign=finance-agent-malaysia)

4. Open your bot and press **Start**, then send it a statement PDF. On start the app registers the Telegram webhook by itself.

Later you can change the variables in the service settings, for example `MONTH_START_DAY` to follow your payday or the PDF passwords.

### Manual setup

Without the template: create a Railway project, add **PostgreSQL**, add a service from this GitHub repo (Railway builds the `Dockerfile`), set the healthcheck path to `/healthz`, generate a public domain on port 8000, and set these variables:

| Variable | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | from @BotFather (required) |
| `TELEGRAM_OWNER_ID` | your numeric id from @userinfobot (required) |
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
| `PUBLIC_BASE_URL` | `https://${{RAILWAY_PUBLIC_DOMAIN}}` |
| `PORT` | `8000`, the port the public domain points to |
| `SECRET_KEY` | a long random string, signs dashboard logins |
| `API_TOKEN` | a long random string, for the CLI and integrations |
| `TELEGRAM_WEBHOOK_SECRET` | required: random letters and digits (Telegram's rule). Without it the webhook is not registered |
| `SCHEDULER_ENABLED` | `true`: weekly and monthly reports |
| `TZ_NAME` | `Asia/Kuala_Lumpur` |
| `MONTH_START_DAY` | `1` for calendar months, or the day after your payday (salary on the 25th → `26`) |
| `OWNER_NAME` | optional: your name as the bank prints it, so transfers to yourself pair up |
| `TNG_PDF_PASSWORD`, `MAYBANK_PDF_PASSWORD` | optional: lets the bot open protected statements |
| `ANTHROPIC_API_KEY` | optional: turns on `/ask` (the budget advisor). Leave empty and everything else works |
| `BUDGET_MODEL`, `BUDGET_HORIZON` | optional: the `/ask` model (default `claude-sonnet-5-5`) and how many months `/budget` covers (default `6`) |

Railway bills by usage. After the trial, the Hobby plan covers one small service plus Postgres.

## How the ledger works

Each row is one movement on one account, and its amount is signed in the account currency. `kind` is one of:

- `expense`: spending. A positive expense is a refund and nets out its category.
- `income`: salary, interest, money received.
- `transfer`: between your own accounts, never counted as spending. Legs are linked by `transfers`:
  - `internal`: a Maybank → TNG top-up, paired automatically by amount within ±3 days;
  - `fx`: MYR sent to someone → RUB received on the Russian account, marked in the bot.

Savings pots are accounts of kind `savings` (`pot_<name>`). A Maybank Tabung row `TRANSFER FROM A/C … FUND Holiday` (a top-up) or `… BOOSTER Holiday` (an automatic save-up) becomes a transfer into «Копилка Holiday». `/pot Holiday 5000` sets a pot's real value.

Months are **financial months**. With `MONTH_START_DAY=26` a month runs from the 26th to the 25th, and its report arrives on the 26th. The bot's `/month`, the dashboard presets and the monthly chart all use the same boundaries.

## Budget, the plan and /ask

`/budget` (and the dashboard card «Бюджет на полгода») forecasts the next `BUDGET_HORIZON` financial months (default 6, the current one included). It starts from the spendable money — MYR bank, wallet and cash; savings pots marked as a reserve (`/pot Reserve резерв`) are shown apart, goal pots (`/pot Holiday цель`, the default) are never counted — adds the expected salary (median of the last three months, on its usual day), subtracts the usual spending (per category, median of the last three months with full data; averaged with the same month a year earlier once the data reaches back that far) and the planned items. The current month is written out as a sum, and the table has a start column, so every row adds up. Trips are left out of the usual spending: a trip goes into the plan. For each month it shows the closing money and the low point just before the salary; spending that usually comes right after the salary (the rent) is not counted before it, judging by the latest month the category appeared in. RF transfers are not in the forecast unless planned.

The salary is income in the «Зарплата» category (`salary`). The built-in rule catches `SALARY`, `GAJI` and `PAYROLL`; if your employer's transfer says something else, give it the «Зарплата» category in `/review` or on the dashboard. Until then `/budget` says the salary was not found.

Planned items (`planned_items`) are future money moves: `/plan 1800 отель Бали 25.10`, `/plan 1600 экскурсия ноябрь`, `/plan 2000 РФ ежемесячно с 03.10 #перевод` (a transfer: not spending, but it leaves the spendable money), `/plan +3000 бонус 12.2026` (income). `/plan` lists them with ✅ paid / ✖️ cancelled; an unpaid one-off whose date passed stays in the current month.

Between statements the real balance of Maybank or TNG can be typed with `/setbalance maybank 4210` (`balance_checks`): balances start from the newest of the last statement row and the check, plus the rows booked after it; the next statement takes over.

`/ask` starts a talk with Claude about a purchase or an expense. It needs `ANTHROPIC_API_KEY` (console.anthropic.com → API keys); without it `/ask` answers that the advisor is off, and `/budget`, `/plan` and the dashboard work as usual. Claude (model `BUDGET_MODEL`, default `claude-sonnet-5-5`) gets the forecast as data, re-runs it with what-if purchases (`simulate`) and answers whether it fits and with which options (a later month, in parts, cuts, the reserve pot). Its final message is JSON (the answer and an optional plan item, shown as a «📌 В план» button): newer models return text written between tool calls as hidden thinking, so the answer must come after the last tool call. It never writes to the ledger. Follow-up questions keep the context; `/done` ends the talk, and after 30 idle minutes text is a cash entry again.

## Optional integrations

### A daily categorization agent

A scheduled [Claude Code](https://claude.com/claude-code) routine following `integrations/categorization-routine/PROMPT.md` categorizes rows the rules did not recognize, adds a short note, and reports to the bot.

The agent endpoints refuse transfers and payments to people: those stay your decision. The routine needs `FINANCE_API_URL` and `API_TOKEN`.

### Apple Pay via iOS Shortcuts

An iOS Shortcuts automation (trigger: Wallet transaction) builds a Dictionary `{amount, merchant, card}` and passes it as the **input** of "Run Script over SSH" to a machine that runs `finance event --stdin`. That command posts to `/api/events`. The row stays pending until the statement confirms it, and for an unknown merchant the bot asks for the category right away.

Without a Mac, the Shortcut can post to `/api/events` itself ("Get Contents of URL" with `Authorization: Bearer API_TOKEN`). The body can be JSON (numbers too, English or Russian keys in any case), a form, or `key: value` text; a body it cannot read is reported in the bot with what arrived.

On the SSH host (for example a Mac at home):

```bash
uv tool install git+https://github.com/emris-git/finance_system_malaysia   # installs ~/.local/bin/finance
mkdir -p ~/.config/finance && printf 'FINANCE_API_URL=https://<service>.up.railway.app\nAPI_TOKEN=<token>\n' > ~/.config/finance/client.env
chmod 600 ~/.config/finance/client.env
```

Use SSH key authentication in the Shortcut, and pin the key to this one command in `~/.ssh/authorized_keys`:

```
command="/Users/<you>/.local/bin/finance event --stdin",restrict ssh-ed25519 AAAA... iphone-finance
```

## Local development

```bash
docker run -d --name finance-pg -e POSTGRES_USER=finance -e POSTGRES_PASSWORD=finance -e POSTGRES_DB=finance -p 55432:5432 postgres:16-alpine
uv venv --python 3.12 && uv pip install -e ".[dev]"
cp .env.example .env            # DATABASE_URL already points at the container
alembic upgrade head && finance seed
finance demo                    # optional: a year of sample data
uvicorn finance.web.app:app --reload
finance link                    # open the printed URL to log into the dashboard
python -m finance.bot           # the bot in polling mode (needs TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_ID)
```

Tests need the `finance_test` database in the same container:

```bash
docker exec finance-pg psql -U finance -c "CREATE DATABASE finance_test"
pytest -q
```

Code, comments and docs are in English; bot and dashboard texts are in Russian.

## CLI

```bash
finance parse statement.pdf               # parsed rows as JSON, no DB
finance import statement.pdf              # into DATABASE_URL
finance import statement.csv --api https://<service>.up.railway.app   # upload to the deployed API (API_TOKEN)
finance report lastweek --send            # print and send to Telegram
finance job weekly|monthly [--force]      # the scheduled jobs, e.g. for a Railway cron
echo '{"amount": "RM12.50", "merchant": "ZUS"}' | finance event --stdin   # a card payment from the phone
```

### Payment screenshots

A TNG or Maybank MAE payment screenshot sent to the bot (as a photo or an image file) is read by Claude (`ANTHROPIC_API_KEY`, model `SCREENSHOT_MODEL`, default `claude-sonnet-5-5`): amount, receiver, remark, date and time. It becomes a pending expense on that account, confirmed by the statement later like an Apple Pay row (the remark goes to the note). A known merchant gets its category from the rules; otherwise the bot asks right away: a shop is remembered, a transfer to a person only on request ("📌 Всегда так"). A payment the ledger already has (same account and amount, ±1 day, time within 10 min when both know it) is not added twice: the bot shows that row, asks its category if it still needs one, and offers "➕ Нет, это другой платёж". Incoming money, other currencies and transfers to the owner's own name are not recorded; for another app the bot asks which account paid.

## HTTP API

Browser: `/web` in the bot gives a 15-minute link, which sets a 30-day cookie. Machines: `Authorization: Bearer $API_TOKEN`.

| Method | Path | |
|---|---|---|
| GET | `/api/summary?start&end` | totals by currency and category, transfers, balances, data freshness |
| GET | `/api/monthly?currency=MYR&months=12`, `/api/daily` | chart series |
| GET | `/api/transactions?start&end&account&category&kind&q&review` | list |
| PATCH | `/api/transactions/{id}` | `{category, remember, note, action: keep/own/delete}` |
| POST | `/api/transactions/{id}/fx` | `{rub_amount}`: mark as a transfer to the ruble account |
| POST | `/api/imports` | multipart `file` (+ `account`) |
| POST | `/api/events` | `{amount, merchant, account or card}`: Apple Pay automation, token only |
| POST | `/api/agent/categorize` | `{transaction_id, category, note, remember}`; refuses transfers, token only |
| POST | `/api/notify` | `{text}` → a Telegram message to the owner, token only |
| GET | `/api/budget` | the budget forecast |
| GET, POST | `/api/plans` | open planned items; `{title, amount, due_on, kind, repeat_months}` adds one |
| PATCH | `/api/plans/{id}` | `{status: done/cancelled}` |
| POST | `/telegram/webhook` | Telegram |

## Privacy

- Only `TELEGRAM_OWNER_ID` can talk to the bot; everyone else is ignored. An update's body names its sender, so the webhook accepts only requests carrying `TELEGRAM_WEBHOOK_SECRET`, which only Telegram knows.
- Without `SECRET_KEY` the dashboard login is off. Every response carries a Content-Security-Policy and anti-framing headers; uploads are capped at 15 MB.
- Secrets live only in your Railway variables. `.gitignore` keeps PDF and CSV statements out of git; never commit real ones.
- `/ask` sends the budget forecast (balances, per-category totals, pot names, plan titles) and your question to the Anthropic API; no transactions or statements. Without `ANTHROPIC_API_KEY` nothing leaves your project.
- Telegram bot chats are not end-to-end encrypted. Statements you send to the bot pass through Telegram's servers.

## License

[MIT](LICENSE)
