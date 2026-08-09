from __future__ import annotations

import json

import httpx
import pytest

from gpttradder.db import Database
from gpttradder.notifications import NotificationService, TelegramNotifier


@pytest.mark.asyncio
async def test_telegram_health_and_send_use_official_http_methods(tmp_path):
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith('/getMe'):
            return httpx.Response(200, json={"ok": True, "result": {"username": "gpttradder_test"}})
        if request.url.path.endswith('/sendMessage'):
            body = json.loads(request.content.decode())
            assert body["chat_id"] == "12345"
            assert body["text"] == "hello"
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        return httpx.Response(404)

    notifier = TelegramNotifier(
        "123:TEST",
        "12345",
        transport=httpx.MockTransport(handler),
    )
    ok, detail = await notifier.healthcheck()
    assert ok is True
    assert detail == "@gpttradder_test"
    assert await notifier.send("hello") is True
    assert [request.url.path.rsplit('/', 1)[-1] for request in requests] == ["getMe", "sendMessage"]


@pytest.mark.asyncio
async def test_notification_service_dedupes_persistently(tmp_path):
    class FakeNotifier:
        def __init__(self):
            self.sent = []

        async def healthcheck(self):
            return True, "ok"

        async def send(self, text):
            self.sent.append(text)
            return True

    fake = FakeNotifier()
    db = Database(tmp_path / "notify.sqlite3")
    service = NotificationService(fake, db, cooldown_seconds=3600)
    assert await service.send_once("same", "first") is True
    assert await service.send_once("same", "second") is False
    assert fake.sent == ["first"]

    # A new service/process still sees the durable slot.
    service2 = NotificationService(fake, Database(tmp_path / "notify.sqlite3"), cooldown_seconds=3600)
    assert await service2.send_once("same", "third") is False
