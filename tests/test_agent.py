import httpx
import pytest

TOKEN = {"Authorization": "Bearer test-token"}


@pytest.fixture
async def client(session):
    from finance.web.app import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def upload(client, rows):
    csv = "Date,Description,Debit,Credit\n" + "".join(f"{d},{desc},{amt},\n" for d, desc, amt in rows)
    r = await client.post("/api/imports", headers=TOKEN, files={"file": ("s.csv", csv.encode())})
    assert r.status_code == 200, r.text


async def txn_id(client, text):
    items = (await client.get(f"/api/transactions?start=2026-09-01&end=2026-09-30&q={text}", headers=TOKEN)).json()["items"]
    return items[0]["id"]


async def test_agent_categorize_with_rule_and_notify(client, monkeypatch):
    import finance.bot

    sent = []

    async def fake_notify(text):
        sent.append(text)

    monkeypatch.setattr(finance.bot, "notify_owner", fake_notify)
    await upload(client, [("22/09/2026", "KEDAI RUNCIT AMINAH 01", "12.00"), ("23/09/2026", "KEDAI RUNCIT AMINAH 02", "8.00")])
    first = await txn_id(client, "01")
    r = await client.post(
        "/api/agent/categorize", headers=TOKEN,
        json={"transaction_id": first, "category": "groceries", "note": "мини-маркет у дома", "remember": True},
    )
    body = r.json()
    assert body["category"] == "groceries" and body["also_updated"] == 1 and body["note"] == "🤖 мини-маркет у дома"
    await client.post("/api/notify", headers=TOKEN, json={"text": "Разобрал 2 <b>"})
    assert sent[-1] == "Разобрал 2 &lt;b&gt;"  # plain text: markup from the agent is escaped


async def test_agent_leaves_payments_to_people_alone(client):
    await upload(client, [("21/09/2026", "DUITNOW TRANSFER TO ALI", "500.00")])
    p2p = await txn_id(client, "ALI")
    refused = await client.post("/api/agent/categorize", headers=TOKEN, json={"transaction_id": p2p, "category": "food"})
    assert refused.status_code == 409
