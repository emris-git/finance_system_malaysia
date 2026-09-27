# Finance bot for Malaysia

A personal finance tracker for people living in Malaysia. It reads Maybank and Touch 'n Go statements, keeps a ledger in Postgres and talks to you through a Telegram bot, with weekly and monthly reports and a web dashboard.

It is built for Russian-speaking expats: the bot and the dashboard speak Russian, and next to ringgit it tracks a ruble account and MYR → RUB transfers (plus crypto wallets, if you have them).

It is **single-user**: everyone deploys their own copy. Your statements stay in your own Railway project and your own database, and the bot answers only your Telegram account.

```
statements ──► parsers ──► ledger (Postgres) ──► reports ──► Telegram bot / weekly & monthly messages
  PDF/CSV from          dedup, rules,              │
  bot / API            transfers, pots           └──► dashboard (/)
Apple Pay events ──► pending rows, confirmed by the statement later
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

## Deploy on Railway

You need a Railway account and about ten minutes.

1. **Create a bot**: in Telegram open [@BotFather](https://t.me/BotFather), `/newbot`, and copy the token.
2. **Find your Telegram id**: open [@userinfobot](https://t.me/userinfobot) and copy the number. A `@username` does not work.
3. **Deploy**: in Railway create a project, add **PostgreSQL**, then add a service from this GitHub repo. Railway builds the `Dockerfile`; on every start the container runs migrations and loads the reference data. In the service settings set the healthcheck path to `/healthz`.
4. **Generate a public domain** for the service (Settings → Networking) and set the variables below.
5. Open your bot and press **Start**. On start the app registers the Telegram webhook by itself.

| Variable | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | from @BotFather (required) |
| `TELEGRAM_OWNER_ID` | your numeric id from @userinfobot (required) |
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
| `PUBLIC_BASE_URL` | `https://${{RAILWAY_PUBLIC_DOMAIN}}` |
| `SECRET_KEY` | a long random string, signs dashboard logins |
| `API_TOKEN` | a long random string, for the CLI and integrations |
| `TELEGRAM_WEBHOOK_SECRET` | required: random letters and digits (Telegram's rule). Without it the webhook is not registered |
| `SCHEDULER_ENABLED` | `true`: weekly and monthly reports |
| `TZ_NAME` | `Asia/Kuala_Lumpur` |
| `MONTH_START_DAY` | `1` for calendar months, or the day after your payday (salary on the 25th → `26`) |
| `OWNER_NAME` | optional: your name as the bank prints it, so transfers to yourself pair up |
| `TNG_PDF_PASSWORD`, `MAYBANK_PDF_PASSWORD` | optional: lets the bot open protected statements |

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
| POST | `/telegram/webhook` | Telegram |

## Privacy

- Only `TELEGRAM_OWNER_ID` can talk to the bot; everyone else is ignored. An update's body names its sender, so the webhook accepts only requests carrying `TELEGRAM_WEBHOOK_SECRET`, which only Telegram knows.
- Without `SECRET_KEY` the dashboard login is off. Every response carries a Content-Security-Policy and anti-framing headers; uploads are capped at 15 MB.
- Secrets live only in your Railway variables. `.gitignore` keeps PDF and CSV statements out of git; never commit real ones.
- Telegram bot chats are not end-to-end encrypted. Statements you send to the bot pass through Telegram's servers.

## License

[MIT](LICENSE)
