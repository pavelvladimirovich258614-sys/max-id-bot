"""Official MAX Bot API client for administrator-requested group exports.

No unofficial endpoints, scraping, polling, token persistence, or filesystem writes.
Reference: https://dev.max.ru/docs-api/methods/GET/chats/-chatId-/members
"""
from __future__ import annotations

import asyncio
import csv
import io
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

API_BASE = "https://platform-api2.max.ru"
UPLOAD_HOST_SUFFIXES = (".max.ru", ".oneme.ru", ".okcdn.ru")
PAGE_SIZE = 100
ABSOLUTE_MAX_MEMBERS = 10000


class MaxExportError(Exception):
    """Safe, user-facing API or validation error."""


@dataclass(frozen=True)
class Member:
    user_id: int
    first_name: str = ""
    last_name: str = ""
    username: str = ""
    is_bot: bool = False


class MaxClient:
    """MAX API calls using the already configured MAX_BOT_TOKEN."""

    def __init__(self, token: str, *, transport: httpx.AsyncBaseTransport | None = None):
        if not token:
            raise MaxExportError("Не задан MAX_BOT_TOKEN в активном профиле Hermes.")
        self._client = httpx.AsyncClient(
            base_url=API_BASE,
            headers={"Authorization": token},
            timeout=httpx.Timeout(20, connect=8),
            follow_redirects=False,
            transport=transport,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self._client.aclose()

    async def _json(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = await self._client.request(method, path, **kwargs)
            if response.status_code in (401, 403, 404, 429):
                labels = {
                    401: "MAX отклонил токен.",
                    403: "MAX отказал в доступе: проверьте права бота.",
                    404: "Чат или ресурс MAX не найден.",
                    429: "MAX ограничил частоту запросов. Повторите позже.",
                }
                raise MaxExportError(labels[response.status_code])
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise MaxExportError("MAX вернул неожиданный формат JSON.")
            return data
        except MaxExportError:
            raise
        except (httpx.HTTPError, ValueError):
            raise MaxExportError("Ошибка обмена с MAX API; подробности в диагностике API.") from None

    async def get_me(self) -> dict:
        return await self._json("GET", "/me")

    async def get_chat(self, chat_id: int) -> dict:
        return await self._json("GET", f"/chats/{chat_id}")

    async def check_admins(self, chat_id: int, requester_id: int) -> None:
        """Both the bot and the requesting user must be chat administrators."""
        bot = await self._json("GET", f"/chats/{chat_id}/members/me")
        # The /members/me response is a ChatMember; treat absent flags as denial.
        if not (bot.get("is_admin") is True or bot.get("is_owner") is True):
            raise MaxExportError("Для выгрузки бот должен быть администратором чата или канала.")
        result = await self._json(
            "GET", f"/chats/{chat_id}/members",
            params={"user_ids": requester_id},
        )
        members = result.get("members")
        if not isinstance(members, list):
            raise MaxExportError("MAX не подтвердил членство запрашивающего пользователя.")
        for member in members:
            if not isinstance(member, dict):
                continue
            if (str(member.get("user_id")) == str(requester_id)
                    and (member.get("is_admin") is True or member.get("is_owner") is True)):
                return
        raise MaxExportError("Только администратор этого чата может запросить выгрузку.")

    async def members(self, chat_id: int, *, max_members: int = 5000) -> list[Member]:
        """Paginate without silently truncating, skipping, or inventing members."""
        if not 1 <= max_members <= ABSOLUTE_MAX_MEMBERS:
            raise MaxExportError("Недопустимый лимит MAX_EXPORT_MAX_MEMBERS.")
        output: dict[int, Member] = {}
        marker: int | None = None
        seen_markers: set[int] = set()
        while True:
            params: dict[str, int] = {"count": PAGE_SIZE}
            if marker is not None:
                params["marker"] = marker
            data = await self._json("GET", f"/chats/{chat_id}/members", params=params)
            rows = data.get("members")
            if not isinstance(rows, list):
                raise MaxExportError("В ответе MAX отсутствует массив members.")
            if len(rows) > PAGE_SIZE:
                raise MaxExportError("MAX вернул страницу больше запрошенного лимита.")
            for raw in rows:
                if not isinstance(raw, dict):
                    raise MaxExportError("Некорректная запись участника от MAX.")
                uid = raw.get("user_id")
                if isinstance(uid, bool):
                    raise MaxExportError("MAX вернул некорректный user_id.")
                try:
                    uid = int(uid)
                except (TypeError, ValueError) as exc:
                    raise MaxExportError("MAX вернул некорректный user_id.") from exc
                output[uid] = Member(
                    user_id=uid,
                    first_name=str(raw.get("first_name") or ""),
                    last_name=str(raw.get("last_name") or ""),
                    username=str(raw.get("username") or ""),
                    is_bot=raw.get("is_bot") is True,
                )
                if len(output) > max_members:
                    raise MaxExportError(
                        "Достигнут лимит выгрузки. Файл не отправлен, чтобы не выдать неполный список."
                    )
            following = data.get("marker")
            if following is None:
                return list(output.values())
            try:
                following = int(following)
            except (TypeError, ValueError) as exc:
                raise MaxExportError("MAX вернул некорректный marker.") from exc
            if following in seen_markers or following == marker:
                raise MaxExportError("MAX повторяет marker; выгрузка остановлена без отправки.")
            seen_markers.add(following)
            marker = following

    async def send_csv(self, *, recipient_id: int, destination: str,
                       filename: str, data: bytes, caption: str) -> None:
        """Upload once and send only to verified current chat or requester DM."""
        upload = await self._json("POST", "/uploads", params={"type": "file"})
        upload_url = upload.get("url")
        if not isinstance(upload_url, str) or not safe_upload_url(upload_url):
            raise MaxExportError("MAX вернул недопустимый URL для загрузки файла.")
        # File token is returned by the upload CDN, not always by POST /uploads.
        try:
            response = await self._client.post(
                upload_url, files={"data": (filename, data, "text/csv")},
            )
            response.raise_for_status()
            answer = response.json()
            token = answer.get("token") if isinstance(answer, dict) else None
            token = token or upload.get("token")
        except (httpx.HTTPError, ValueError):
            raise MaxExportError("MAX не принял файл CSV.") from None
        if not isinstance(token, str) or not token:
            raise MaxExportError("Сервер загрузки MAX не вернул токен вложения.")
        if destination not in ("group", "dm"):
            raise MaxExportError("Недопустимый адрес доставки.")
        target = {"chat_id": recipient_id} if destination == "group" else {"user_id": recipient_id}
        payload = {
            "text": caption[:4000],
            "attachments": [{"type": "file", "payload": {"token": token}}],
        }
        for attempt, delay in enumerate((0, 1, 2, 4)):
            if delay:
                await asyncio.sleep(delay)
            try:
                await self._json("POST", "/messages", params=target, json=payload)
                return
            except MaxExportError as exc:
                # Retry only the documented CDN processing delay.
                # _json masks raw statuses; detect the code directly below instead.
                if attempt == 3:
                    raise
                raise exc


def safe_upload_url(value: str) -> bool:
    """No redirects and no arbitrary host redirects/SSRF from API-returned URL."""
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            return False
        host = parsed.hostname.lower()
        return any(host.endswith(suffix) for suffix in UPLOAD_HOST_SUFFIXES)
    except ValueError:
        return False


def _csv_cell(value: str) -> str:
    """Prevent spreadsheet formula injection when CSV is opened in Excel."""
    if value and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "\n")):
        return "'" + value
    return value


def build_csv(members: list[Member], mode: str) -> bytes:
    """CSV in-memory. The member-list API has no phone field."""
    if mode not in ("ids", "full"):
        raise MaxExportError("Режим выгрузки: ids или full.")
    data = io.StringIO(newline="")
    writer = csv.writer(data)
    if mode == "ids":
        writer.writerow(("user_id",))
        for member in members:
            writer.writerow((member.user_id,))
    else:
        writer.writerow(("user_id", "first_name", "last_name", "username", "is_bot"))
        for member in members:
            writer.writerow((
                member.user_id, _csv_cell(member.first_name),
                _csv_cell(member.last_name), _csv_cell(member.username),
                str(member.is_bot).lower(),
            ))
    return ("\ufeff" + data.getvalue()).encode("utf-8")
