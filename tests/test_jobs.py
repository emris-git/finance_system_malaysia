from finance import jobs


async def test_weekly_report_sent_once(session, monkeypatch):
    sent = []

    async def fake_notify(text):
        sent.append(text)

    monkeypatch.setattr(jobs, "notify_owner", fake_notify)
    assert await jobs.send_weekly_report() is True
    assert await jobs.send_weekly_report() is False
    assert len(sent) == 1 and "Неделя" in sent[0]
