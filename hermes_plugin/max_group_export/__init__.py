"""Hermes Agent MAX IDs/group export plugin.

Uses the existing max-platform Gateway; never starts a second MAX poller.
The platform-provided source identity, not LLM-provided text, authorizes exports.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone

from .client import ABSOLUTE_MAX_MEMBERS, MaxClient, MaxExportError, build_csv

_ID = re.compile(r"-?\d+\Z")


def _id(value) -> int:
    if isinstance(value, bool) or not _ID.fullmatch(str(value or "")):
        raise MaxExportError("Не удалось определить числовой MAX ID.")
    return int(value)


def _set(name: str) -> set[int]:
    items: set[int] = set()
    for piece in os.getenv(name, "").replace(";", ",").split(","):
        piece = piece.strip()
        if not piece:
            continue
        items.add(_id(piece))
    return items


def _source(platform: str, source):
    """Trusted gateway identity; disallow CLI/Desktop and guessed sources."""
    if platform != "max" or source is None:
        raise MaxExportError("Команды доступны только из авторизованного чата MAX.")
    user_id = _id(getattr(source, "user_id", None))
    chat_type = str(getattr(source, "chat_type", "")).lower()
    scoped = str(getattr(source, "chat_id", "") or "")
    if chat_type == "group":
        if not scoped.startswith("chat:"):
            raise MaxExportError("Неизвестный контекст группового чата.")
        return user_id, "group", _id(scoped.removeprefix("chat:"))
    if chat_type == "dm":
        if scoped != f"user:{user_id}":
            raise MaxExportError("Контекст личного чата не совпал с аккаунтом.")
        return user_id, "dm", None
    raise MaxExportError("Тип этого MAX-чата не поддерживается.")


def _owners() -> set[int]:
    return _set("MAX_EXPORT_OWNER_IDS")


def _allowed_chat(chat_id: int) -> None:
    if chat_id not in _set("MAX_EXPORT_CHAT_IDS"):
        raise MaxExportError(
            "Этот чат не разрешён для выгрузки. Добавьте его ID в MAX_EXPORT_CHAT_IDS."
        )


def _max_members() -> int:
    raw = os.getenv("MAX_EXPORT_MAX_MEMBERS", "5000")
    if not raw.isdecimal() or not 1 <= int(raw) <= ABSOLUTE_MAX_MEMBERS:
        raise MaxExportError("MAX_EXPORT_MAX_MEMBERS должен быть от 1 до 10000.")
    return int(raw)


def _token() -> str:
    token = os.getenv("MAX_BOT_TOKEN", "").strip()
    if not token:
        raise MaxExportError("MAX_BOT_TOKEN отсутствует в активном Hermes .env.")
    return token


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, MaxExportError):
        return f"⚠️ {exc}"
    # Never echo exception repr: SDK/network exceptions may contain secrets.
    return "⚠️ Не удалось выполнить команду. Проверьте логи Gateway (без токенов)."


async def maxid(raw_args: str, *, platform: str = "", source=None) -> str:
    """Read-only identification from the actual MAX session context."""
    try:
        user_id, kind, current_id = _source(platform, source)
        # The existing MAX Gateway already enforces MAX_ALLOWED_USERS.
        target = current_id
        if raw_args.strip():
            if kind != "dm" or len(raw_args.split()) != 1:
                return "Использование: /maxid [числовой chat_id в личном чате]"
            if user_id not in _owners():
                raise MaxExportError("Просмотр другого чата разрешён только владельцу.")
            target = _id(raw_args.strip())
            _allowed_chat(target)
        async with MaxClient(_token()) as api:
            bot = await api.get_me()
            bot_id = bot.get("user_id")
            if target is None:
                return (
                    f"MAX User ID: {user_id}\n"
                    "Контекст: личный диалог\n"
                    f"Bot ID: {bot_id}\n"
                    "ID личного чата не выдаётся из маршрута user:<id>."
                )
            if kind == "dm":
                await api.check_admins(target, user_id)
            chat = await api.get_chat(target)
            return (
                f"MAX User ID: {user_id}\n"
                f"Chat/Channel ID: {target}\n"
                f"Тип по MAX API: {chat.get('type') or 'не указан'}\n"
                f"Название: {chat.get('title') or 'не указано'}\n"
                f"Bot ID: {bot_id}"
            )
    except Exception as exc:
        return _safe_error(exc)


async def maxexport(raw_args: str, *, platform: str = "", source=None) -> str:
    """Admin-only explicit CSV export, delivered only to the originating chat/DM."""
    try:
        user_id, kind, current_id = _source(platform, source)
        if user_id not in _owners():
            raise MaxExportError("Выгрузка доступна только владельцам из MAX_EXPORT_OWNER_IDS.")
        parts = raw_args.strip().split()
        if kind == "group":
            if len(parts) > 1 or (parts and parts[0] not in ("ids", "full")):
                return "Группа: /maxexport ids или /maxexport full"
            target = current_id
            mode = parts[0] if parts else "ids"
            delivery = "group"
            recipient = target
        else:
            if len(parts) not in (1, 2):
                return "Личный чат: /maxexport ЧИСЛОВОЙ_CHAT_ID [ids|full]"
            target = _id(parts[0])
            mode = parts[1] if len(parts) == 2 else "ids"
            delivery = "dm"
            recipient = user_id
        if mode not in ("ids", "full"):
            return "Режимы: ids (только ID) или full (ID, имя, никнейм, is_bot)."
        _allowed_chat(target)
        async with MaxClient(_token()) as api:
            await api.check_admins(target, user_id)
            members = await api.members(target, max_members=_max_members())
            if not members:
                return "MAX вернул пустой список участников; документ не сформирован."
            content = build_csv(members, mode)
            date = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            filename = f"max-members-{abs(target)}-{mode}-{date}.csv"
            await api.send_csv(
                recipient_id=recipient,
                destination=delivery,
                filename=filename,
                data=content,
                caption=f"MAX: {len(members)} участников, режим {mode}; чат {target}.",
            )
            return (
                f"✅ {filename}: {len(members)} записей отправлено "
                + ("в этот чат." if delivery == "group" else "вам в личный чат.")
                + "\nТелефоны: официальный список участников MAX их не предоставляет."
            )
    except Exception as exc:
        return _safe_error(exc)


def register(ctx) -> None:
    """Native Hermes plugin registration (the max-platform adapter stays active)."""
    ctx.register_command(
        "maxid", maxid,
        description="MAX: ID пользователя, бота, текущего чата или канала",
        args_hint="[chat_id]",
    )
    ctx.register_command(
        "maxexport", maxexport,
        description="MAX: выгрузка участников в CSV только по запросу администратора",
        args_hint="[chat_id] [ids|full]",
    )
