"""Offline tests: no real MAX token, no polling, no network access."""
import csv
import io
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from hermes_plugin.max_group_export import (
    _source, maxexport, maxid, register,
)
from hermes_plugin.max_group_export.client import (
    MaxClient, MaxExportError, Member, build_csv, safe_upload_url,
)


def reply(status=200, **data):
    return httpx.Response(status, json=data)


class TestExportData(unittest.TestCase):
    def test_csv_only_ids_and_full(self):
        rows = [Member(2, '=1+1', "Alice", "@bob", False), Member(3)]
        ids = build_csv(rows, "ids").decode("utf-8-sig")
        self.assertEqual(list(csv.reader(io.StringIO(ids))), [["user_id"], ["2"], ["3"]])
        full = build_csv(rows, "full").decode("utf-8-sig")
        data = list(csv.reader(io.StringIO(full)))
        self.assertEqual(data[0], ["user_id", "first_name", "last_name", "username", "is_bot"])
        self.assertEqual(data[1][1], "'=1+1")
        self.assertEqual(data[1][3], "'@bob")
        self.assertNotIn("phone", full.lower())

    def test_safe_cdn_upload_url(self):
        self.assertTrue(safe_upload_url("https://upload.max.ru/file"))
        self.assertTrue(safe_upload_url("https://fu.oneme.ru/upload"))
        self.assertFalse(safe_upload_url("http://upload.max.ru/file"))
        self.assertFalse(safe_upload_url("https://evil.example/file"))
        self.assertFalse(safe_upload_url("https://max.ru.evil.example/"))
        self.assertFalse(safe_upload_url("https://localhost/"))
        self.assertFalse(safe_upload_url("https://max.ru@evil.example/"))

    def test_source_requires_real_max_transport(self):
        source = SimpleNamespace(user_id="12", chat_type="group", chat_id="chat:-3")
        self.assertEqual(_source("max", source), (12, "group", -3))
        with self.assertRaises(MaxExportError):
            _source("telegram", source)
        with self.assertRaises(MaxExportError):
            _source("max", None)
        with self.assertRaises(MaxExportError):
            _source("max", SimpleNamespace(user_id="12", chat_type="dm", chat_id="chat:12"))

    def test_registration(self):
        class FakeContext:
            commands = {}
            def register_command(self, name, handler, **kwargs):
                self.commands[name] = handler
        c = FakeContext()
        register(c)
        self.assertEqual(set(c.commands), {"maxid", "maxexport"})


class TestMaxApi(unittest.IsolatedAsyncioTestCase):
    async def test_pagination_and_dedup(self):
        paths = []
        def handler(req):
            paths.append(str(req.url))
            self.assertEqual(req.headers["Authorization"], "test-secret")
            self.assertEqual(req.method, "GET")
            marker = req.url.params.get("marker")
            if marker is None:
                return reply(members=[{"user_id": 11, "first_name": "One"},
                                      {"user_id": 12, "first_name": "Two"}], marker=99)
            self.assertEqual(marker, "99")
            return reply(members=[{"user_id": 12, "first_name": "Updated"},
                                  {"user_id": 13, "first_name": "Three"}], marker=None)
        async with MaxClient("test-secret", transport=httpx.MockTransport(handler)) as api:
            members = await api.members(-100)
        self.assertEqual([m.user_id for m in members], [11, 12, 13])
        self.assertEqual(members[1].first_name, "Updated")
        self.assertEqual(len(paths), 2)
        self.assertIn("count=100", paths[0])

    async def test_repeated_marker_fails_without_partial_csv(self):
        def handler(req):
            return reply(members=[{"user_id": 11}], marker=7)
        async with MaxClient("test-secret", transport=httpx.MockTransport(handler)) as api:
            with self.assertRaisesRegex(MaxExportError, "marker"):
                await api.members(12)

    async def test_unexpected_fields_and_limit_fail_closed(self):
        def handler(req):
            return reply(members=[{"user_id": 11}, {"user_id": 12}], marker=None)
        async with MaxClient("test-secret", transport=httpx.MockTransport(handler)) as api:
            with self.assertRaisesRegex(MaxExportError, "лимит"):
                await api.members(12, max_members=1)

    async def test_admin_checks_and_non_admin_rejected(self):
        def good(req):
            if req.url.path.endswith("/members/me"):
                return reply(user_id=99, is_admin=True)
            if req.url.path.endswith("/members"):
                return reply(members=[{"user_id": 17, "is_owner": True}])
            raise AssertionError(req.url)
        async with MaxClient("test-secret", transport=httpx.MockTransport(good)) as api:
            await api.check_admins(-100, 17)
        def bad(req):
            if req.url.path.endswith("/members/me"):
                return reply(user_id=99, is_admin=False)
            raise AssertionError("The bot is not admin, should stop before requester lookup")
        async with MaxClient("test-secret", transport=httpx.MockTransport(bad)) as api:
            with self.assertRaisesRegex(MaxExportError, "администратором"):
                await api.check_admins(-100, 17)

    async def test_upload_then_send_real_api_contract(self):
        sent = []
        def handler(req):
            sent.append(req)
            if req.url.path == "/uploads":
                self.assertEqual(req.url.params["type"], "file")
                return reply(url="https://upload.max.ru/file")
            if req.url.host == "upload.max.ru":
                self.assertEqual(req.method, "POST")
                self.assertIn(b"members.csv", req.content)
                return reply(token="cdn-file-token")
            if req.url.path == "/messages":
                self.assertEqual(req.url.params["user_id"], "17")
                body = json.loads(req.content)
                self.assertEqual(body["attachments"], [{"type": "file", "payload": {"token": "cdn-file-token"}}])
                return reply(message={"body": {"mid": "abc"}})
            raise AssertionError(req.url)
        async with MaxClient("test-secret", transport=httpx.MockTransport(handler)) as api:
            await api.send_csv(
                recipient_id=17, destination="dm", filename="members.csv",
                data=b"user_id\n17\n", caption="Test",
            )
        self.assertEqual(len(sent), 3)


class TestCommands(unittest.IsolatedAsyncioTestCase):
    async def test_deny_empty_owners_and_chat_allowlist(self):
        src = SimpleNamespace(user_id=73412011, chat_type="group", chat_id="chat:-991")
        with patch.dict(os.environ, {"MAX_BOT_TOKEN": "secret",
                                     "MAX_EXPORT_OWNER_IDS": "",
                                     "MAX_EXPORT_CHAT_IDS": ""}):
            self.assertIn("MAX_EXPORT_OWNER_IDS", await maxexport("ids", platform="max", source=src))
        with patch.dict(os.environ, {"MAX_BOT_TOKEN": "secret",
                                     "MAX_EXPORT_OWNER_IDS": "73412011",
                                     "MAX_EXPORT_CHAT_IDS": ""}):
            self.assertIn("MAX_EXPORT_CHAT_IDS", await maxexport("ids", platform="max", source=src))

    async def test_full_group_export_with_both_admins(self):
        calls = []
        def handler(req):
            calls.append(req.url.path)
            path = req.url.path
            if path.endswith("/members/me"):
                return reply(user_id=55, is_admin=True)
            if path.endswith("/members"):
                if "user_ids" in req.url.params:
                    self.assertEqual(req.url.params["user_ids"], "73412011")
                    return reply(members=[{"user_id": 73412011, "is_admin": True}])
                return reply(members=[{"user_id": 73412011, "first_name": "Pavel"}], marker=None)
            if path == "/uploads":
                return reply(url="https://fu.oneme.ru/file")
            if req.url.host == "fu.oneme.ru":
                return reply(token="filetoken")
            if path == "/messages":
                self.assertEqual(req.url.params["chat_id"], "-991")
                return reply(message={})
            raise AssertionError(req.url)
        transport = httpx.MockTransport(handler)
        real = MaxClient
        src = SimpleNamespace(user_id=73412011, chat_type="group", chat_id="chat:-991")
        with patch.dict(os.environ, {"MAX_BOT_TOKEN": "secret",
                                     "MAX_EXPORT_OWNER_IDS": "73412011",
                                     "MAX_EXPORT_CHAT_IDS": "-991",
                                     "MAX_EXPORT_MAX_MEMBERS": "100"}):
            with patch("hermes_plugin.max_group_export.MaxClient",
                       side_effect=lambda token: real(token, transport=transport)):
                result = await maxexport("ids", platform="max", source=src)
        self.assertIn("✅", result)
        self.assertIn("1 записей", result)
        self.assertIn("/messages", calls)

    async def test_maxid_group(self):
        def handler(req):
            if req.url.path == "/me":
                return reply(user_id=99)
            if req.url.path == "/chats/-991":
                return reply(type="chat", title="Группа")
            raise AssertionError(req.url)
        real = MaxClient
        src = SimpleNamespace(user_id=17, chat_type="group", chat_id="chat:-991")
        with patch.dict(os.environ, {"MAX_BOT_TOKEN": "test"}):
            with patch("hermes_plugin.max_group_export.MaxClient",
                       side_effect=lambda token: real(token, transport=httpx.MockTransport(handler))):
                response = await maxid("", platform="max", source=src)
        self.assertIn("Chat/Channel ID: -991", response)
        self.assertIn("Bot ID: 99", response)
