"""Reference data: accounts, categories, default rules. Idempotent (`finance seed`)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finance.models import Account, Category, CategoryRule

ACCOUNTS = [
    # code, name, currency, kind
    ("maybank", "Maybank", "MYR", "bank"),
    ("tng", "TNG eWallet", "MYR", "ewallet"),
    ("cash_myr", "Наличные RM", "MYR", "cash"),
    ("ru", "Российский счёт", "RUB", "bank"),
]

CATEGORIES = [
    # code, name, emoji, kind
    ("food", "Еда вне дома", "🍜", "expense"),
    ("groceries", "Продукты", "🛒", "expense"),
    ("transport", "Транспорт", "🚕", "expense"),
    ("housing", "Жильё", "🏠", "expense"),
    ("utilities", "Связь и коммуналка", "💡", "expense"),
    ("subscriptions", "Подписки", "📦", "expense"),
    ("projects", "Собственные проекты", "🚀", "expense"),
    ("health", "Здоровье и спорт", "💪", "expense"),
    ("fun", "Кайф", "🎮", "expense"),
    ("shopping", "Покупки", "🛍", "expense"),
    ("family", "Семья и подарки", "🎁", "expense"),
    ("travel", "Путешествия", "✈️", "expense"),
    ("education", "Обучение", "📚", "expense"),
    ("fees", "Комиссии и налоги", "🏦", "expense"),
    ("other", "Другое", "❓", "expense"),
    ("salary", "Зарплата", "💼", "income"),
    ("interest", "Проценты и кэшбэк", "📈", "income"),
    ("other_income", "Прочий доход", "➕", "income"),
]

# pattern, category, kind, direction, account, priority
# Higher priority wins. User rules created from the bot get 1000.
RULES: list[tuple[str, str | None, str | None, str | None, str | None, int]] = [
    # --- money between own accounts
    (r"RELOAD|TOP.?UP|ADD MONEY", None, "internal", "in", "tng", 300),
    (r"\bTNG\b|TOUCH\s?'?N\s?GO|TNGD", None, "internal", "out", "maybank", 300),
    (r"GO\s?\+|GOPLUS", None, "own", None, "tng", 300),
    # --- income
    (r"SALARY|GAJI|PAYROLL", "salary", "income", "in", None, 400),
    (r"HIBAH|INTEREST|PROFIT PAID|DIVIDEND|CASHBACK|EARNINGS", "interest", "income", "in", None, 400),
    (r"REFUND|REVERSAL", None, "expense", "in", None, 350),
    # --- transfers to/from people: the bot asks what they were
    # (paying a shop's DuitNow QR is a purchase, not a transfer)
    (r"DUITNOW\s?QR", None, "expense", "out", None, 250),
    # (Maybank IBK writes "FUND TFR")
    (r"DUITNOW|IBG|INSTANT TRANSFER|FUND TRANSFER|INTERBANK|SEND MONEY|TRANSFER|\bTRF\b|\bTFR\b", None, "p2p", "out", None, 200),
    (r"DUITNOW|IBG|INSTANT TRANSFER|FUND TRANSFER|INTERBANK|RECEIVE|TRANSFER|\bTRF\b|\bTFR\b", None, "p2p_in", "in", None, 200),
    # --- categories (Malaysian merchants)
    (r"GRAB\s?\*?\s?FOOD|FOODPANDA|SHOPEE\s?FOOD|STARBUCKS|COFFEE BEAN|\bZUS\b|TEALIVE|BOBA|CHAGEE|"
     r"MCDONALD|KFC|PIZZA|BURGER|SUBWAY|DOMINO|RESTAURANT|RESTORAN|CAFE|KAFE|BAKERY|KOPITIAM|MAMAK|"
     r"NASI|IRON WOK|BABSANG|NAN XIANG|VILLAGE PARK|SUSHI|RAMEN|KITCHEN|BISTRO", "food", None, None, None, 120),
    (r"JAYA GROCER|VILLAGE GROCER|LOTUS|AEON|MYDIN|SPEEDMART|FAMILY\s?MART|7.?ELEVEN|KK (MART|SUPER)|"
     r"CU MART|GIANT|\bNSK\b|MERCATO|GROCER|SUPERMARKET|HERO MARKET", "groceries", None, None, None, 110),
    (r"RENT\b|SEWA|SPEEDHOME|MAINTENANCE FEE|CONDO", "housing", None, None, None, 105),
    (r"GRAB|BOLT|MAXIM|RAPID\s?KL|\bMRT\b|\bLRT\b|MONORAIL|PRASARANA|\bKTM\b|\bERL\b|PARKING|CARPARK|"
     r"SHELL|PETRONAS|PETRON|CALTEX|\bBHP\b|TOLL|PLUS MALAYSIA|SMARTTAG|RFID", "transport", None, None, None, 100),
    (r"CLAUDE\.AI|ANTHROPIC|FIGMA|GITHUB|NOTION|LINKEDIN|GOOGLE\s?\*|APPLE\.COM|ICLOUD|"
     r"SPOTIFY|NETFLIX|YOUTUBE|DISNEY|CHATGPT|OPENAI|PERPLEXITY|UX PILOT|CURSOR", "subscriptions", None, None, None, 100),
    (r"VERCEL|RAILWAY|GODADDY|NETLIFY|DIGITALOCEAN|NAMECHEAP", "projects", None, None, None, 100),
    (r"ANYTIME\s?FIT|FITNESS|\bGYM\b|NUTRIPOD|PHARMACY|FARMASI|GUARDIAN|WATSON|CLINIC|KLINIK|HOSPITAL|"
     r"DENTAL|CARING", "health", None, None, None, 100),
    (r"STEAM|PLAYSTATION|XBOX|NINTENDO|EPIC GAMES|CINEMA|\bGSC\b|\bTGV\b|MOVIE|KARAOKE|BOWLING|KLOOK",
     "fun", None, None, None, 100),
    (r"CELCOMDIGI|\bDIGI\b|CELCOM|MAXIS|HOTLINK|U\s?MOBILE|UNIFI|TIME DOTCOM|\bTNB\b|TENAGA|"
     r"SYABAS|AIR SELANGOR|INDAH WATER|ASTRO", "utilities", None, None, None, 100),
    (r"SHOPEE|LAZADA|UNIQLO|IKEA|DECATHLON|MR\.?\s?DIY|H&M|ZARA|MUJI|DAISO|TAOBAO|AMAZON|SEPHORA",
     "shopping", None, None, None, 100),
    (r"AIRASIA|MALAYSIA AIRLINES|BATIK|FIREFLY|AGODA|BOOKING\.COM|AIRBNB|TRIP\.COM|TRAVELOKA|HOTEL|EXPEDIA",
     "travel", None, None, None, 95),
    (r"UDEMY|COURSERA|GOPRACTICE|SKILLSHARE|DUOLINGO", "education", None, None, None, 100),
    (r"\bFEE\b|CHARGE|\bCAJ\b|SERVICE TAX|LHDN|STAMP DUTY|\bSST\b", "fees", None, None, None, 90),
    # --- Russian keywords for manual RUB/cash entries from the bot
    (r"кафе|ресторан|кофе|обед|ужин|завтрак|еда|бар\b|доставк", "food", None, None, None, 90),
    (r"продукт|магазин|пят[её]рочк|перекр[её]ст|вкусвилл|ашан|лента|магнит", "groceries", None, None, None, 90),
    (r"такси|метро|автобус|бензин|парковк|поезд|электричк|каршер", "transport", None, None, None, 90),
    (r"аренд|квартплат|жкх|коммуналк", "housing", None, None, None, 90),
    (r"связь|телефон|интернет|мобильн", "utilities", None, None, None, 90),
    (r"подписк", "subscriptions", None, None, None, 90),
    (r"аптек|врач|клиник|стоматолог|спортзал|анализ", "health", None, None, None, 90),
    (r"кино|игр|концерт|развлеч", "fun", None, None, None, 90),
    (r"одежд|озон|ozon|wildberries|покупк", "shopping", None, None, None, 90),
    (r"подар|родител", "family", None, None, None, 90),
    (r"отел|билет|самол[её]т|авиа|путешеств", "travel", None, None, None, 90),
    (r"курс|книг|обучен", "education", None, None, None, 90),
    (r"комисси|налог|штраф", "fees", None, None, None, 90),
]


async def seed(session: AsyncSession) -> dict[str, int]:
    added = {"accounts": 0, "categories": 0, "rules": 0, "rules_removed": 0}

    existing_accounts = set((await session.scalars(select(Account.code))).all())
    for sort, (code, name, currency, kind) in enumerate(ACCOUNTS):
        if code not in existing_accounts:
            session.add(Account(code=code, name=name, currency=currency, kind=kind, sort=sort))
            added["accounts"] += 1

    categories = {c.code: c for c in (await session.scalars(select(Category))).all()}
    for sort, (code, name, emoji, kind) in enumerate(CATEGORIES):
        if code not in categories:
            categories[code] = Category(code=code, name=name, emoji=emoji, kind=kind, sort=sort)
            session.add(categories[code])
            added["categories"] += 1
        else:
            # keep the bot's button order when a category is added in the middle of the list
            categories[code].sort = sort
    await session.flush()

    # seed rules belong to this file: an edited pattern replaces the old one
    wanted = {pattern for pattern, *_ in RULES}
    seeded = set()
    for rule in (await session.scalars(select(CategoryRule).where(CategoryRule.origin == "seed"))).unique().all():
        if rule.pattern in wanted:
            seeded.add(rule.pattern)
        else:
            await session.delete(rule)
            added["rules_removed"] += 1
    for pattern, category, kind, direction, account, priority in RULES:
        if pattern in seeded:
            continue
        session.add(
            CategoryRule(
                pattern=pattern,
                category_id=categories[category].id if category else None,
                kind=kind,
                direction=direction,
                account_code=account,
                priority=priority,
                origin="seed",
            )
        )
        added["rules"] += 1

    await session.commit()
    return added
