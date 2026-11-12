"""Bot de Telegram: convierte el logo de un canal en un custom emoji redondo."""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    Chat,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputSticker,
    LinkPreviewOptions,
    Message,
    MessageOriginChannel,
)
from dotenv import load_dotenv

import animated
from converter import InvalidImage, round_emoji_bytes
from store import Store

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("logo-emoji-bot")

# Emoji base del sticker. Telegram lo usa como respaldo donde no hay custom emoji.
STICKER_EMOJI = "⭐"
SET_NAME_RE = re.compile(r"[^A-Za-z0-9_]")

HELP_TEXT = (
    "<b>Cómo funciona</b>\n\n"
    "1. Añádeme como <b>administrador</b> del canal (puedes hacerlo con el botón "
    "de añadir; el pack se crea solo al entrar).\n"
    "2. Recorto la foto del canal en círculo y a 100×100.\n"
    "3. Creo un pack de custom emojis con enlace <code>t.me/addemoji/…</code>.\n"
    "4. Si cambias la foto, el emoji del pack se actualiza solo.\n\n"
    "También puedes escribir <code>/convert @micanal</code> o reenviarme un "
    "mensaje del canal y responderle con <code>/convert</code>.\n\n"
    "<b>Emojis animados</b>\n"
    "Pulsa <b>✨ Emoji animado</b> o escribe <code>/animar</code>: eliges texto, "
    "fuente, paleta, fondo y borde, y lo guardas en tu propio pack animado.\n"
    "Solo escribe la palabra: el bot calcula su tamaño y la reparte "
    "automáticamente en varios emojis, cortando letras si hace falta para que "
    "quede compacta y sin huecos entre trozos.\n"
    "Puedes incluir <b>emojis premium</b> junto al texto: se dibujan dentro del "
    "emoji, como una letra más, y conservan su animación si la tienen.\n"
    "En <b>Mis canales</b>, abre un canal y pulsa <b>✨ Animar logo</b> para "
    "aplicarle brillo, moneda, latido, giro y más efectos.\n\n"
    "<b>Emoji desde imagen</b>\n"
    "Pulsa <b>🖼 Emoji desde imagen</b> y envíame una foto: eliges forma "
    "(círculo, cuadrado o cuadrado redondeado), uno de los 21 efectos y, si "
    "quieres, un fondo de color para que la imagen se mueva encima.\n\n"
    "⚠️ Para <b>enviar</b> el emoji necesitas Telegram Premium; el pack se ve "
    "igual sin Premium."
)


class NoChannelLogo(RuntimeError):
    """El canal no tiene foto de perfil."""


# --------------------------------------------------------------------------- #
# Nombres del pack
# --------------------------------------------------------------------------- #
def build_set_name(chat: Chat, bot_username: str) -> str:
    base = chat.username or f"ch{abs(chat.id)}"
    base = SET_NAME_RE.sub("_", base)
    base = re.sub(r"_+", "_", base).strip("_")
    if not base:
        base = "channel"
    if not base[0].isalpha():
        base = f"ch{base}"
    suffix = f"_by_{bot_username}"
    base = base[: 64 - len(suffix)].rstrip("_")
    return f"{base}{suffix}"


def with_tag(set_name: str, tag: str, bot_username: str) -> str:
    suffix = f"_by_{bot_username}"
    base = set_name[: -len(suffix)].rstrip("_")
    return f"{base}_{tag}{suffix}"[:64]


PACK_TITLE_DEFAULT = "Free Promotion Lists - @Valkyria02_Pro_bot"


def set_title() -> str:
    # Nombre visible del pack en Telegram (no el enlace). Se puede cambiar con PACK_TITLE.
    return (os.getenv("PACK_TITLE") or PACK_TITLE_DEFAULT).strip()[:64]


def parse_target(raw: str) -> int | str | None:
    value = raw.strip()
    match = re.search(r"t\.me/c/(\d+)", value)
    if match:
        return int(f"-100{match.group(1)}")
    value = re.sub(r"^https?://", "", value)
    value = re.sub(r"^t\.me/", "", value)
    value = value.split("?")[0].strip("/")
    if not value:
        return None
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return f"@{value.lstrip('@')}"


def chat_ref_from_message(message: Message) -> int | str | None:
    origin = getattr(message, "forward_origin", None)
    if isinstance(origin, MessageOriginChannel):
        return origin.chat.id
    if message.forward_from_chat:
        return message.forward_from_chat.id
    return None


# --------------------------------------------------------------------------- #
# Emojis premium dentro del texto
# --------------------------------------------------------------------------- #
_premium_cache: dict[str, list[bytes]] = {}


def _utf16_index(text: str, units: int) -> int:
    """Índice de Python a partir de un desplazamiento UTF-16 (el de Telegram)."""
    count = 0
    for index, char in enumerate(text):
        if count >= units:
            return index
        count += 2 if ord(char) > 0xFFFF else 1
    return len(text)


def extract_premium_emojis(
    text: str, entities: list[Any] | None
) -> tuple[str, list[str], list[str]]:
    """Sustituye cada emoji premium por un marcador y guarda id y respaldo."""
    items = sorted(
        (
            entity
            for entity in entities or []
            if entity.type == "custom_emoji" and entity.custom_emoji_id
        ),
        key=lambda entity: entity.offset,
    )
    if not items:
        return text, [], []

    parts: list[str] = []
    ids: list[str] = []
    fallbacks: list[str] = []
    position = 0
    for entity in items:
        start = _utf16_index(text, entity.offset)
        end = _utf16_index(text, entity.offset + entity.length)
        if start < position:
            continue
        parts.append(text[position:start])
        parts.append(animated.PREMIUM_PLACEHOLDER)
        ids.append(entity.custom_emoji_id)
        fallbacks.append(text[start:end])
        position = end
    parts.append(text[position:])
    return "".join(parts), ids, fallbacks


def premium_display_text(text: str, fallbacks: list[str] | None) -> str:
    """Texto legible: cada emoji premium vuelve a su carácter de respaldo."""
    values = fallbacks or []
    out: list[str] = []
    index = 0
    for char in text:
        if char == animated.PREMIUM_PLACEHOLDER:
            out.append(values[index] if index < len(values) else STICKER_EMOJI)
            index += 1
        else:
            out.append(char)
    return "".join(out)


def compact_letters(text: str) -> str:
    """Une letras sueltas: "P R O M O" pasa a "PROMO" para que no se separen."""
    parts = text.split(" ")
    if len(parts) > 1 and all(len(part) == 1 for part in parts):
        return "".join(parts)
    return text


async def _premium_asset(bot: Bot, sticker: Any) -> list[bytes]:
    """Fotogramas del emoji premium (animado si lo es) o su miniatura."""
    frames: list[bytes] = []
    if sticker.is_animated or sticker.is_video:
        try:
            tg_file = await bot.get_file(sticker.file_id)
            buffer = await bot.download_file(tg_file.file_path)
            frames = await asyncio.to_thread(
                animated.decode_premium_frames,
                buffer.getvalue(),
                sticker.is_animated,
                sticker.is_video,
            )
        except Exception:  # noqa: BLE001 - sin animación se usa la miniatura
            logger.warning(
                "No pude animar el emoji premium %s", sticker.custom_emoji_id,
                exc_info=True,
            )
    if frames:
        return frames

    # Respaldo estático: la miniatura funciona con estáticos, .tgs y .webm.
    thumbnail = sticker.thumbnail
    file_id = thumbnail.file_id if thumbnail else sticker.file_id
    tg_file = await bot.get_file(file_id)
    buffer = await bot.download_file(tg_file.file_path)
    return [buffer.getvalue()]


async def fetch_premium_images(bot: Bot, emoji_ids: list[str]) -> list[list[bytes]]:
    """Descarga cada emoji premium con sus fotogramas (animados si los tiene).

    Los .tgs se renderizan con rlottie y los .webm con ffmpeg; si no se puede,
    se usa la miniatura estática para que el emoji siga funcionando.
    """
    missing = [
        emoji_id
        for emoji_id in dict.fromkeys(emoji_ids)
        if emoji_id not in _premium_cache
    ]
    if missing:
        stickers = await bot.get_custom_emoji_stickers(custom_emoji_ids=missing)
        for emoji_id, sticker in zip(missing, stickers):
            _premium_cache[emoji_id] = await _premium_asset(bot, sticker)
    return [_premium_cache[emoji_id] for emoji_id in emoji_ids]


# --------------------------------------------------------------------------- #
# Lógica de conversión
# --------------------------------------------------------------------------- #
def _input_sticker(webp: bytes) -> InputSticker:
    return InputSticker(
        sticker=BufferedInputFile(webp, filename="emoji.webp"),
        format="static",
        emoji_list=[STICKER_EMOJI],
    )


async def _create_set(
    bot: Bot, chat: Chat, owner_id: int, sticker: InputSticker
) -> tuple[str, str | None, str | None]:
    me = await bot.me()
    set_name = build_set_name(chat, me.username)
    title = set_title()

    for attempt in range(4):
        candidate = (
            set_name
            if attempt == 0
            else with_tag(set_name, f"{int(time.time()) % 10000}{attempt}", me.username)
        )
        try:
            await bot.create_new_sticker_set(
                user_id=owner_id,
                name=candidate,
                title=title,
                stickers=[sticker],
                sticker_type="custom_emoji",
                needs_repainting=False,
            )
        except TelegramBadRequest as exc:
            text = exc.message.lower()
            if "already" in text or "occupied" in text:
                continue
            raise
        full = await bot.get_sticker_set(candidate)
        created = full.stickers[-1] if full.stickers else None
        return (
            candidate,
            created.file_id if created else None,
            created.custom_emoji_id if created else None,
        )

    raise RuntimeError("No pude encontrar un nombre libre para el pack de emojis.")


async def _ensure_pack_meta(
    bot: Bot, record: dict[str, Any]
) -> dict[str, Any]:
    """Completa packs creados antes: custom_emoji_id y título visible nuevos."""
    title = set_title()
    if record.get("custom_emoji_id") and record.get("pack_title") == title:
        return record

    try:
        full = await bot.get_sticker_set(record["set_name"])
    except TelegramBadRequest as exc:
        logger.warning("No pude leer el pack %s: %s", record["set_name"], exc.message)
        return record

    updated = dict(record)
    if not updated.get("custom_emoji_id") and full.stickers:
        newest = full.stickers[-1]
        updated["custom_emoji_id"] = newest.custom_emoji_id
        updated["sticker_file_id"] = updated.get("sticker_file_id") or newest.file_id

    # El título solo se puede fijar al crear; en packs viejos lo renombramos.
    if full.title != title:
        try:
            await bot.set_sticker_set_title(record["set_name"], title)
        except TelegramBadRequest as exc:
            logger.warning(
                "No pude renombrar el pack %s: %s", record["set_name"], exc.message
            )
    updated["pack_title"] = title
    return updated


async def _replace_in_set(
    bot: Bot, set_name: str, owner_id: int, sticker: InputSticker, old_file_id: str | None
) -> tuple[str | None, str | None]:
    await bot.add_sticker_to_set(user_id=owner_id, name=set_name, sticker=sticker)
    full = await bot.get_sticker_set(set_name)
    created = full.stickers[-1] if full.stickers else None
    new_id = created.file_id if created else None
    custom_id = created.custom_emoji_id if created else None

    # Quitamos la versión anterior para que el pack no crezca sin límite.
    if old_file_id and old_file_id != new_id:
        with suppress(TelegramBadRequest):
            await bot.delete_sticker_from_set(old_file_id)
    return new_id, custom_id


async def sync_channel(
    bot: Bot, store: Store, chat: Chat, owner_id: int, force: bool = False
) -> tuple[dict[str, Any], bool]:
    """Crea o actualiza el pack. Devuelve (registro, cambio_real)."""
    photo = chat.photo
    if photo is None:
        raise NoChannelLogo("El canal no tiene foto de perfil.")

    record = store.get(chat.id)
    if (
        record
        and not force
        and record.get("photo_unique_id") == photo.big_file_unique_id
        and record.get("set_name")
    ):
        updated = await _ensure_pack_meta(bot, record)
        if updated != record:
            await store.put(chat.id, updated)
        return updated, False

    tg_file = await bot.get_file(photo.big_file_id)
    buffer = await bot.download_file(tg_file.file_path)
    webp = round_emoji_bytes(buffer.getvalue())
    sticker = _input_sticker(webp)

    if record and record.get("set_name"):
        try:
            new_id, custom_id = await _replace_in_set(
                bot,
                record["set_name"],
                record["owner_id"],
                sticker,
                record.get("sticker_file_id"),
            )
            set_name = record["set_name"]
        except TelegramBadRequest as exc:
            logger.warning(
                "Pack %s no reutilizable (%s), creando uno nuevo",
                record.get("set_name"),
                exc.message,
            )
            set_name, new_id, custom_id = await _create_set(
                bot, chat, owner_id, sticker
            )
        else:
            # Packs creados con el nombre viejo: lo renombramos al pasar por aquí.
            with suppress(TelegramBadRequest):
                await bot.set_sticker_set_title(set_name, set_title())
    else:
        set_name, new_id, custom_id = await _create_set(bot, chat, owner_id, sticker)

    new_record = {
        "set_name": set_name,
        "sticker_file_id": new_id,
        "custom_emoji_id": custom_id,
        "pack_title": set_title(),
        "photo_unique_id": photo.big_file_unique_id,
        "owner_id": owner_id,
        "title": chat.title,
        "username": chat.username,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }

    # Si el canal ya tenía un pack animado, lo regeneramos con el logo nuevo.
    if record and record.get("anim_set_name"):
        effect = record.get("anim_effect") or "shine"
        for key in (
            "anim_set_name",
            "anim_effect",
            "anim_sticker_file_id",
            "anim_custom_emoji_id",
            "anim_photo_unique_id",
        ):
            if key in record:
                new_record[key] = record[key]
        try:
            anim_webm = await asyncio.to_thread(
                animated.generate_logo, buffer.getvalue(), effect
            )
            new_record = await append_channel_anim(
                bot, store, chat, new_record, effect, anim_webm
            )
        except Exception:  # noqa: BLE001 - no romper la actualización estática
            logger.exception("No pude actualizar el pack animado de %s", chat.id)

    await store.put(chat.id, new_record)
    return new_record, True


async def admin_warning(bot: Bot, chat: Chat) -> str | None:
    me = await bot.me()
    try:
        member = await bot.get_chat_member(chat.id, me.id)
    except TelegramBadRequest:
        return (
            "No pude leer ese canal. Si es privado, añádeme como "
            "<b>administrador</b> y vuelve a intentarlo."
        )
    if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
        return (
            f"No soy administrador de <b>{chat.title or chat.id}</b>. "
            "Añádeme como admin (así puedo leer la foto del canal) y repite el comando."
        )
    return None


def pack_link(record: dict[str, Any]) -> str:
    return f"https://t.me/addemoji/{record['set_name']}"


def anim_pack_title() -> str:
    default = f"{set_title()} · Animados"
    return (os.getenv("ANIM_PACK_TITLE") or default).strip()[:64]


def build_anim_set_name(user_id: int, bot_username: str) -> str:
    base = f"anim{user_id}"
    suffix = f"_by_{bot_username}"
    return f"{base[: 64 - len(suffix)].rstrip('_')}{suffix}"


def _video_sticker(webm: bytes) -> InputSticker:
    return InputSticker(
        sticker=BufferedInputFile(webm, filename="emoji.webm"),
        format="video",
        emoji_list=[STICKER_EMOJI],
    )


async def add_animated_emojis(
    bot: Bot, store: Store, user_id: int, webms: list[bytes]
) -> tuple[str, list[str | None], bool]:
    """Añade uno o varios WebM al pack animado del usuario."""
    if not webms:
        raise animated.EmojiError("No hay ningún emoji que añadir.")

    record = store.get(user_id)
    created = False
    if record is None:
        me = await bot.me()
        set_name = build_anim_set_name(user_id, me.username)
        try:
            await _api_retry(
                bot.create_new_sticker_set,
                user_id=user_id,
                name=set_name,
                title=anim_pack_title(),
                stickers=[_video_sticker(webm) for webm in webms[:50]],
                sticker_type="custom_emoji",
                needs_repainting=False,
            )
            created = True
        except TelegramBadRequest as exc:
            message = exc.message.lower()
            if "already" not in message and "occupied" not in message:
                raise
            # El pack ya existía (por ejemplo, si se perdió animated.json).
    else:
        set_name = record["set_name"]
        if record.get("count", 0) + len(webms) > 200:
            raise animated.EmojiError(
                "Tu pack animado no admite más emojis (máximo 200 en Telegram)."
            )

    if not created:
        for webm in webms:
            await _api_retry(
                bot.add_sticker_to_set,
                user_id=user_id,
                name=set_name,
                sticker=_video_sticker(webm),
            )
            await asyncio.sleep(1)

    full = await bot.get_sticker_set(set_name)
    newest = full.stickers[-len(webms) :]
    new_record = {
        "set_name": set_name,
        "count": len(full.stickers),
        "custom_emoji_id": newest[-1].custom_emoji_id if newest else None,
        "sticker_file_id": newest[-1].file_id if newest else None,
        "owner_id": user_id,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }
    await store.put(user_id, new_record)
    return set_name, [sticker.custom_emoji_id for sticker in newest], created


async def add_animated_emoji(
    bot: Bot, store: Store, user_id: int, webm: bytes
) -> tuple[str, str | None, bool]:
    set_name, emoji_ids, created = await add_animated_emojis(
        bot, store, user_id, [webm]
    )
    return set_name, emoji_ids[-1] if emoji_ids else None, created


# --------------------------------------------------------------------------- #
# Logo animado de un canal
# --------------------------------------------------------------------------- #
def build_channel_anim_set_name(chat: Chat, bot_username: str) -> str:
    base = chat.username or f"ch{abs(chat.id)}"
    base = SET_NAME_RE.sub("_", base)
    base = re.sub(r"_+", "_", base).strip("_")
    if not base:
        base = "channel"
    if not base[0].isalpha():
        base = f"ch{base}"
    suffix = f"_anim_by_{bot_username}"
    return f"{base[: 64 - len(suffix)].rstrip('_')}{suffix}"


def channel_anim_title(chat: Chat) -> str:
    title = f"{set_title()} · {chat.title or chat.id}"
    return title[:64]


async def _channel_logo_bytes(bot: Bot, chat: Chat) -> bytes:
    photo = chat.photo
    if photo is None:
        raise NoChannelLogo("El canal no tiene foto de perfil.")
    tg_file = await bot.get_file(photo.big_file_id)
    buffer = await bot.download_file(tg_file.file_path)
    return buffer.getvalue()


async def generate_channel_anim(bot: Bot, chat: Chat, effect: str) -> bytes:
    source = await _channel_logo_bytes(bot, chat)
    return await asyncio.to_thread(animated.generate_logo, source, effect)


async def append_channel_anim(
    bot: Bot,
    store: Store,
    chat: Chat,
    record: dict[str, Any],
    effect: str,
    webm: bytes,
) -> dict[str, Any]:
    """Crea o actualiza el pack animado del canal con un solo emoji."""
    updated = dict(record)
    set_name = updated.get("anim_set_name")
    added = False

    if set_name:
        try:
            await bot.add_sticker_to_set(
                user_id=updated["owner_id"],
                name=set_name,
                sticker=_video_sticker(webm),
            )
            added = True
        except TelegramBadRequest as exc:
            logger.warning(
                "Pack animado %s no reutilizable (%s), creando uno nuevo",
                set_name,
                exc.message,
            )
            set_name = None

    if not set_name:
        me = await bot.me()
        set_name = build_channel_anim_set_name(chat, me.username)
        try:
            await bot.create_new_sticker_set(
                user_id=updated["owner_id"],
                name=set_name,
                title=channel_anim_title(chat),
                stickers=[_video_sticker(webm)],
                sticker_type="custom_emoji",
                needs_repainting=False,
            )
            added = True
        except TelegramBadRequest as exc:
            message = exc.message.lower()
            if "already" not in message and "occupied" not in message:
                raise
            # El set existe: añadimos el sticker nuevo.
            await bot.add_sticker_to_set(
                user_id=updated["owner_id"],
                name=set_name,
                sticker=_video_sticker(webm),
            )

    full = await bot.get_sticker_set(set_name)
    newest = full.stickers[-1] if full.stickers else None

    # Dejamos solo el emoji nuevo en el pack del canal.
    old_file_id = updated.get("anim_sticker_file_id")
    if old_file_id and newest and old_file_id != newest.file_id:
        with suppress(TelegramBadRequest):
            await bot.delete_sticker_from_set(old_file_id)

    updated.update(
        {
            "anim_set_name": set_name,
            "anim_effect": effect,
            "anim_sticker_file_id": newest.file_id if newest else None,
            "anim_custom_emoji_id": newest.custom_emoji_id if newest else None,
            "anim_photo_unique_id": (
                chat.photo.big_file_unique_id if chat.photo else None
            ),
        }
    )
    await store.put(chat.id, updated)
    return updated


def emoji_preview(record: dict[str, Any]) -> str:
    """El emoji tal cual se vería al enviarlo (necesita Premium en el cliente)."""
    emoji_id = record.get("custom_emoji_id")
    if emoji_id:
        return f'<tg-emoji emoji-id="{emoji_id}">{STICKER_EMOJI}</tg-emoji>'
    return STICKER_EMOJI


# --------------------------------------------------------------------------- #
# Panel / teclados
# --------------------------------------------------------------------------- #
def main_menu(bot_username: str, count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Añadir a un canal",
                    url=f"https://t.me/{bot_username}?startchannel=true",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"📋 Mis canales ({count})", callback_data="menu:list"
                )
            ],
            [
                InlineKeyboardButton(
                    text="✨ Emoji animado", callback_data="anim:new"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🖼 Emoji desde imagen", callback_data="img:new"
                )
            ],
            [InlineKeyboardButton(text="ℹ️ Ayuda", callback_data="menu:help")],
        ]
    )


def channels_keyboard(items: list[tuple[str, dict[str, Any]]]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"📢 {rec.get('title') or chat_id}", callback_data=f"ch:{chat_id}"
            )
        ]
        for chat_id, rec in items
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Volver", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def channel_keyboard(chat_id: str, record: dict[str, Any]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text="🔄 Actualizar ahora", callback_data=f"ch:refresh:{chat_id}"
            )
        ],
        [InlineKeyboardButton(text="🔗 Abrir pack", url=pack_link(record))],
    ]
    if record.get("anim_set_name"):
        rows.append(
            [
                InlineKeyboardButton(
                    text="🎞 Abrir pack animado",
                    url=f"https://t.me/addemoji/{record['anim_set_name']}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="✨ Animar logo", callback_data=f"ch:anim:{chat_id}"
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="🗑 Quitar canal", callback_data=f"ch:del:{chat_id}"
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="⬅️ Volver", callback_data="menu:list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def channel_text(record: dict[str, Any], chat_id: str) -> str:
    state = record.get("updated_at") or "—"
    photo = record.get("photo_unique_id")
    anim_line = ""
    if record.get("anim_set_name"):
        effect = record.get("anim_effect") or "shine"
        label = animated.LOGO_EFFECTS.get(effect, effect)
        anim_line = (
            f"Pack animado: https://t.me/addemoji/{record['anim_set_name']}\n"
            f"Efecto: <b>{label}</b>\n"
        )
    return (
        f"📢 <b>{record.get('title') or chat_id}</b>\n\n"
        f"Pack: {pack_link(record)}\n"
        f"{anim_line}"
        f"Última actualización: {state}\n"
        f"Foto actual: <code>{photo[:12] if photo else '—'}…</code>\n\n"
        "Al cambiar la foto del canal, el emoji se actualiza solo."
    )


def effects_keyboard(chat_id: str) -> InlineKeyboardMarkup:
    keys = list(animated.LOGO_EFFECTS)
    rows = []
    for index in range(0, len(keys), 2):
        rows.append(
            [
                InlineKeyboardButton(
                    text=animated.LOGO_EFFECTS[key],
                    callback_data=f"ch:fx:{chat_id}:{key}",
                )
                for key in keys[index : index + 2]
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="⬅️ Volver", callback_data=f"ch:{chat_id}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_effect_picker(
    callback: CallbackQuery, chat_id: str, record: dict[str, Any]
) -> None:
    current = ""
    if record.get("anim_effect"):
        label = animated.LOGO_EFFECTS.get(record["anim_effect"], record["anim_effect"])
        current = f"\nAhora tiene el efecto <b>{label}</b>."
    await callback.message.edit_text(
        "✨ <b>Animar el logo del canal</b>\n\n"
        "Elige un efecto y verás una vista previa con el logo real."
        f"{current}\n\n"
        "Se guarda en un pack animado aparte: el pack estático no se toca.",
        reply_markup=effects_keyboard(chat_id),
    )
    await callback.answer()


async def preview_channel_effect(
    callback: CallbackQuery,
    bot: Bot,
    chat_id: str,
    record: dict[str, Any],
    effect: str,
) -> None:
    label = animated.LOGO_EFFECTS.get(effect, effect)
    await callback.answer("Generando…")
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(f"🎨 Generando el efecto <b>{label}</b>…")
    try:
        chat = await bot.get_chat(int(chat_id))
        webm = await generate_channel_anim(bot, chat, effect)
    except (NoChannelLogo, animated.EmojiError, TelegramBadRequest) as exc:
        with suppress(TelegramBadRequest):
            await callback.message.edit_text(
                f"No pude animar el logo: <i>{html.escape(str(exc))}</i>",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="⬅️ Volver", callback_data=f"ch:{chat_id}"
                            )
                        ]
                    ]
                ),
            )
        return

    await callback.message.answer_sticker(
        BufferedInputFile(webm, filename="emoji.webm")
    )
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            f"✨ <b>{label}</b>\n\n"
            f"Así se vería el logo de <b>{html.escape(record.get('title') or chat_id)}</b>.\n\n"
            "¿Lo guardo en el pack animado del canal?",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="✅ Guardar en pack animado",
                            callback_data=f"ch:fxsave:{chat_id}:{effect}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🔄 Elegir otro efecto",
                            callback_data=f"ch:anim:{chat_id}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="⬅️ Volver", callback_data=f"ch:{chat_id}"
                        )
                    ],
                ]
            ),
        )


async def save_channel_effect(
    callback: CallbackQuery,
    bot: Bot,
    store: Store,
    chat_id: str,
    record: dict[str, Any],
    effect: str,
) -> None:
    label = animated.LOGO_EFFECTS.get(effect, effect)
    await callback.answer("Guardando…")
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            f"⏳ Generando <b>{label}</b> y actualizando el pack…"
        )
    try:
        chat = await bot.get_chat(int(chat_id))
        webm = await generate_channel_anim(bot, chat, effect)
        updated = await append_channel_anim(bot, store, chat, record, effect, webm)
    except (NoChannelLogo, animated.EmojiError, TelegramBadRequest) as exc:
        with suppress(TelegramBadRequest):
            await callback.message.edit_text(
                f"No pude guardar el pack animado: <i>{html.escape(str(exc))}</i>",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="⬅️ Volver", callback_data=f"ch:{chat_id}"
                            )
                        ]
                    ]
                ),
            )
        return

    await callback.message.edit_text(
        f"✅ Pack animado actualizado con el efecto <b>{label}</b>.\n\n"
        + channel_text(updated, chat_id),
        reply_markup=channel_keyboard(chat_id, updated),
    )


def mine(store: Store, user_id: int) -> list[tuple[str, dict[str, Any]]]:
    return [
        (chat_id, rec)
        for chat_id, rec in store.all().items()
        if rec.get("owner_id") == user_id
    ]


async def panel_view(bot: Bot, store: Store, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    me = await bot.me()
    items = mine(store, user_id)
    text = (
        "🎛 <b>Panel de control</b>\n\n"
        f"Canales gestionados: <b>{len(items)}</b>\n\n"
        "Añade el bot como administrador de un canal y el pack de emojis se "
        "crea solo con su logo.\n\n"
        "Con <b>✨ Emoji animado</b> creas un emoji con texto, degradados y "
        "bordes animados en tu propio pack."
    )
    if not items:
        text += (
            "\n\nTodavía no hay ninguno. Pulsa <b>➕ Añadir a un canal</b> o "
            "envía <code>/convert @micanal</code>."
        )
    return text, main_menu(me.username, len(items))


# --------------------------------------------------------------------------- #
# Emoji animado: wizard
# --------------------------------------------------------------------------- #
class AnimWizard(StatesGroup):
    text = State()
    font = State()
    palette = State()
    background = State()
    border = State()
    textcolor = State()
    letter_stroke = State()
    motion = State()
    preview = State()


ANIM_STYLE_DEFAULT: dict[str, Any] = {
    "text": "",
    "font": "bold",
    "palette": "neon",
    "background": "gradient",
    "border": "spin",
    "speed": "normal",
    "text_color": "auto",
    "stroke_color": "auto",
    "motion": "none",
}
ANIM_SPEED_ORDER = ("slow", "normal", "fast")


# --------------------------------------------------------------------------- #
# Emojis de ejemplo del menú
# --------------------------------------------------------------------------- #
# Cada opción del menú lleva un custom emoji de muestra para que se vea cómo
# queda antes de elegirla. El bot crea una vez un pack de ejemplos propio y
# guarda los ids en data/examples.json.
EXAMPLE_VERSION = 5
EXAMPLE_TEXT = "Abcd"
EXAMPLE_CACHE_KEY = 0
# Cada opción puede ocupar varios emojis (texto grande repartido en trozos).
_example_ids: dict[str, list[str]] = {}
_example_task: asyncio.Task[Any] | None = None

EXAMPLE_TITLES = {
    "font": "🔤 Elige la fuente:",
    "pal": "🎨 Elige una paleta:",
    "bg": "🖼 Elige el fondo:",
    "border": "💫 Elige el borde:",
    "tcol": "🅰 Elige el color de la letra:",
    "stroke": "✏️ Elige el borde de la letra:",
    "motion": "🎞 Elige la animación del texto:",
}


def example_specs() -> list[tuple[str, str, dict[str, Any], bool]]:
    """(clave, emoji de respaldo, estilo, ¿animado?) de cada ejemplo."""
    specs: list[tuple[str, str, dict[str, Any], bool]] = []
    for key in animated.FONT_CHOICES:
        # Fuentes: sin fondo, texto blanco con contorno negro y quieto.
        specs.append(
            (
                f"font:{key}",
                "🔤",
                {
                    "font_key": key,
                    "background": "transparent",
                    "text_color_key": "white",
                    "stroke_color_key": "black",
                },
                False,
            )
        )
    for key in animated.PALETTES:
        specs.append((f"pal:{key}", "🎨", {"palette": key}, True))
    for key in animated.BACKGROUNDS:
        specs.append((f"bg:{key}", "🖼", {"background": key}, True))
    for key in animated.BORDERS:
        specs.append((f"border:{key}", "💫", {"border": key}, True))
    for key in animated.TEXT_COLORS:
        specs.append((f"tcol:{key}", "🅰", {"text_color_key": key}, True))
    for key in animated.STROKE_COLORS:
        specs.append((f"stroke:{key}", "✏️", {"stroke_color_key": key}, True))
    for key in animated.MOTIONS:
        specs.append((f"motion:{key}", "🎞", {"motion": key}, True))
    return specs


def example_entries(kind: str) -> list[tuple[str, str, str]]:
    """(clave, emoji de respaldo, etiqueta) de un menú concreto."""
    if kind == "font":
        return [
            (f"font:{key}", "🔤", str(data["label"]))
            for key, data in animated.FONT_CHOICES.items()
        ]
    if kind == "pal":
        return [
            (f"pal:{key}", "🎨", str(data["label"]))
            for key, data in animated.PALETTES.items()
        ]
    if kind == "bg":
        return [
            (f"bg:{key}", "🖼", str(label))
            for key, label in animated.BACKGROUNDS.items()
        ]
    if kind == "tcol":
        return [
            (f"tcol:{key}", "🅰", str(label))
            for key, label in animated.TEXT_COLORS.items()
        ]
    if kind == "stroke":
        return [
            (f"stroke:{key}", "✏️", str(label))
            for key, label in animated.STROKE_COLORS.items()
        ]
    if kind == "motion":
        return [
            (f"motion:{key}", "🎞", str(label))
            for key, label in animated.MOTIONS.items()
        ]
    return [
        (f"border:{key}", "💫", str(label))
        for key, label in animated.BORDERS.items()
    ]


def example_set_name(bot_username: str) -> str:
    suffix = f"_by_{bot_username}"
    base = f"ejemplos_v{EXAMPLE_VERSION}"
    return f"{base[: 64 - len(suffix)].rstrip('_')}{suffix}"


def example_pack_title() -> str:
    default = f"{set_title()} · Ejemplos"
    return (os.getenv("EXAMPLE_PACK_TITLE") or default).strip()[:64]


def _example_tag(key: str, fallback: str) -> str:
    emoji_ids = _example_ids.get(key) or []
    if not emoji_ids:
        return fallback
    # Sin espacios: los trozos son cortes del mismo texto y deben ir pegados.
    return "".join(
        f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'
        for emoji_id in emoji_ids
    )


def anim_menu_text(kind: str, with_examples: bool = True) -> str:
    """Texto de un menú de estilo con un emoji de muestra por opción."""
    lines = [EXAMPLE_TITLES[kind], ""]
    for key, fallback, label in example_entries(kind):
        tag = _example_tag(key, fallback) if with_examples else fallback
        lines.append(f"{tag} {label}")
    return "\n".join(lines)


def _example_ids_from_set(
    stickers: list[Any],
    specs: list[tuple[str, str, dict[str, Any], bool]],
    groups: list[list[bytes]],
) -> dict[str, list[str]]:
    """Agrupa los ids del pack en el mismo orden en que se generaron."""
    ids: dict[str, list[str]] = {}
    index = 0
    for (key, _, _, _), group in zip(specs, groups):
        part = stickers[index : index + len(group)]
        index += len(group)
        if len(part) != len(group) or not all(s.custom_emoji_id for s in part):
            return {}
        ids[key] = [str(sticker.custom_emoji_id) for sticker in part]
    return ids


async def _api_retry(func: Any, *args: Any, **kwargs: Any) -> Any:
    """Ejecuta una llamada a Telegram esperando si pide flood control."""
    for _ in range(5):
        try:
            return await func(*args, **kwargs)
        except TelegramRetryAfter as exc:
            wait = exc.retry_after + 1
            logger.info("Flood control en Telegram: espero %ss", wait)
            await asyncio.sleep(wait)
    return await func(*args, **kwargs)


async def ensure_examples(bot: Bot, store: Store, user_id: int) -> None:
    """Crea (una sola vez) el pack de emojis de ejemplo y cachea sus ids."""
    global _example_ids
    if _example_ids:
        return
    override = (os.getenv("EXAMPLES_OWNER_ID") or "").strip()
    if override.isdigit():
        user_id = int(override)
    me = await bot.me()
    set_name = example_set_name(me.username)
    cached = store.get(EXAMPLE_CACHE_KEY) or {}
    if cached.get("version") == EXAMPLE_VERSION and cached.get("set_name") == set_name:
        ids = cached.get("ids") or {}
        if ids:
            _example_ids = {
                str(key): [str(item) for item in value]
                if isinstance(value, list)
                else [str(value)]
                for key, value in ids.items()
            }
            return

    specs = example_specs()
    try:
        existing = await bot.get_sticker_set(set_name)
    except TelegramBadRequest:
        existing = None
    stickers = list(existing.stickers) if existing else []

    groups = [
        await asyncio.to_thread(
            animated.generate_example, EXAMPLE_TEXT, moving=moving, **style
        )
        for _, _, style, moving in specs
    ]
    flat = [webm for group in groups for webm in group]

    if len(stickers) < len(flat):
        created = False
        if existing is None:
            try:
                await _api_retry(
                    bot.create_new_sticker_set,
                    user_id=user_id,
                    name=set_name,
                    title=example_pack_title(),
                    stickers=[_video_sticker(webm) for webm in flat[:50]],
                    sticker_type="custom_emoji",
                    needs_repainting=False,
                )
                created = True
            except TelegramBadRequest as exc:
                if "already" not in exc.message.lower():
                    raise
        if created:
            pending = flat[50:]
        else:
            current = await bot.get_sticker_set(set_name)
            stickers = list(current.stickers)
            pending = flat[len(stickers) :]
        # Telegram acepta como mucho 50 stickers al crear el pack; el resto se
        # añade uno a uno (con una pausa para no chocar con el flood control).
        for webm in pending:
            await _api_retry(
                bot.add_sticker_to_set,
                user_id=user_id,
                name=set_name,
                sticker=_video_sticker(webm),
            )
            await asyncio.sleep(1)
        existing = await bot.get_sticker_set(set_name)
        stickers = list(existing.stickers)

    ids = _example_ids_from_set(stickers, specs, groups)
    if not ids:
        return
    _example_ids = ids
    await store.put(
        EXAMPLE_CACHE_KEY,
        {"version": EXAMPLE_VERSION, "set_name": set_name, "ids": ids},
    )
    logger.info(
        "Emojis de ejemplo listos (%d opciones, %d emojis)",
        len(ids),
        sum(len(value) for value in ids.values()),
    )


async def _ensure_examples_logged(bot: Bot, store: Store, user_id: int) -> None:
    try:
        await ensure_examples(bot, store, user_id)
    except Exception:  # noqa: BLE001 - los menús funcionan sin ejemplos
        logger.warning("No pude preparar los emojis de ejemplo", exc_info=True)


def start_examples(bot: Bot, store: Store, user_id: int) -> None:
    """Genera los ejemplos en segundo plano sin bloquear el menú."""
    global _example_task
    if _example_ids:
        return
    if _example_task is not None and not _example_task.done():
        return
    _example_task = asyncio.create_task(_ensure_examples_logged(bot, store, user_id))


async def _answer_menu(
    message: Message, kind: str, keyboard: InlineKeyboardMarkup, prefix: str = ""
) -> None:
    """Manda un menú con ejemplos; si Telegram los rechaza, va sin ellos."""
    head = f"{prefix}\n\n" if prefix else ""
    try:
        await message.answer(
            head + anim_menu_text(kind), reply_markup=keyboard
        )
    except TelegramBadRequest:
        await message.answer(
            head + anim_menu_text(kind, with_examples=False), reply_markup=keyboard
        )


async def _edit_menu(
    callback: CallbackQuery, kind: str, keyboard: InlineKeyboardMarkup, prefix: str = ""
) -> None:
    """Edita el mensaje con el menú de ejemplos (con respaldo sin emojis)."""
    head = f"{prefix}\n\n" if prefix else ""
    try:
        await callback.message.edit_text(
            head + anim_menu_text(kind), reply_markup=keyboard
        )
    except TelegramBadRequest:
        with suppress(TelegramBadRequest):
            await callback.message.edit_text(
                head + anim_menu_text(kind, with_examples=False),
                reply_markup=keyboard,
            )


def anim_cancel_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="❌ Cancelar", callback_data="anim:cancel")]


def anim_back_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="⬅️ Volver", callback_data="anim:preview")]


def anim_font_kb(back: bool = False) -> InlineKeyboardMarkup:
    keys = list(animated.FONT_CHOICES)
    rows = []
    for index in range(0, len(keys), 2):
        rows.append(
            [
                InlineKeyboardButton(
                    text=animated.FONT_CHOICES[key]["label"],
                    callback_data=f"anim:font:{key}",
                )
                for key in keys[index : index + 2]
            ]
        )
    if back:
        rows.append(anim_back_row())
    rows.append(anim_cancel_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def anim_palette_kb(back: bool = False) -> InlineKeyboardMarkup:
    keys = list(animated.PALETTES)
    rows = []
    for index in range(0, len(keys), 3):
        rows.append(
            [
                InlineKeyboardButton(
                    text=animated.PALETTES[key]["label"],
                    callback_data=f"anim:pal:{key}",
                )
                for key in keys[index : index + 3]
            ]
        )
    if back:
        rows.append(anim_back_row())
    rows.append(anim_cancel_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def anim_background_kb(back: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=animated.BACKGROUNDS[key], callback_data=f"anim:bg:{key}"
            )
        ]
        for key in animated.BACKGROUNDS
    ]
    if back:
        rows.append(anim_back_row())
    rows.append(anim_cancel_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def anim_textcolor_kb(back: bool = False) -> InlineKeyboardMarkup:
    keys = list(animated.TEXT_COLORS)
    rows = []
    for index in range(0, len(keys), 3):
        rows.append(
            [
                InlineKeyboardButton(
                    text=animated.TEXT_COLORS[key], callback_data=f"anim:tcol:{key}"
                )
                for key in keys[index : index + 3]
            ]
        )
    if back:
        rows.append(anim_back_row())
    rows.append(anim_cancel_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def anim_stroke_kb(back: bool = False) -> InlineKeyboardMarkup:
    keys = list(animated.STROKE_COLORS)
    rows = []
    for index in range(0, len(keys), 2):
        rows.append(
            [
                InlineKeyboardButton(
                    text=animated.STROKE_COLORS[key], callback_data=f"anim:stroke:{key}"
                )
                for key in keys[index : index + 2]
            ]
        )
    if back:
        rows.append(anim_back_row())
    rows.append(anim_cancel_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def anim_border_kb(back: bool = False) -> InlineKeyboardMarkup:
    keys = list(animated.BORDERS)
    rows = []
    for index in range(0, len(keys), 2):
        rows.append(
            [
                InlineKeyboardButton(
                    text=animated.BORDERS[key], callback_data=f"anim:border:{key}"
                )
                for key in keys[index : index + 2]
            ]
        )
    if back:
        rows.append(anim_back_row())
    rows.append(anim_cancel_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def anim_motion_kb(back: bool = False) -> InlineKeyboardMarkup:
    keys = list(animated.MOTIONS)
    rows = []
    for index in range(0, len(keys), 2):
        rows.append(
            [
                InlineKeyboardButton(
                    text=animated.MOTIONS[key], callback_data=f"anim:motion:{key}"
                )
                for key in keys[index : index + 2]
            ]
        )
    if back:
        rows.append(anim_back_row())
    rows.append(anim_cancel_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def anim_preview_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Crear pack", callback_data="anim:create")],
            [
                InlineKeyboardButton(text="🎨 Paleta", callback_data="anim:palette"),
                InlineKeyboardButton(text="🖼 Fondo", callback_data="anim:background"),
            ],
            [
                InlineKeyboardButton(text="💫 Borde", callback_data="anim:border"),
                InlineKeyboardButton(text="🎞 Animación", callback_data="anim:motion"),
            ],
            [InlineKeyboardButton(text="⚡ Velocidad", callback_data="anim:speed")],
            [InlineKeyboardButton(text="🔤 Fuente", callback_data="anim:font")],
            [
                InlineKeyboardButton(text="🅰 Color letra", callback_data="anim:textcolor"),
                InlineKeyboardButton(
                    text="✏️ Borde letra", callback_data="anim:strokecolor"
                ),
            ],
            anim_cancel_row(),
        ]
    )


def anim_style(data: dict[str, Any]) -> dict[str, Any]:
    style = dict(ANIM_STYLE_DEFAULT)
    style.update(data.get("style") or {})
    return style


def anim_preview_text(style: dict[str, Any], premium_count: int = 0) -> str:
    palette = animated.PALETTES.get(style["palette"], {}).get("label", style["palette"])
    font_label = animated.FONT_CHOICES.get(
        str(style.get("font") or "bold"), {}
    ).get("label", "Sans negrita")
    count = animated.word_count(
        str(style["text"]), premium_count, str(style.get("font") or "bold")
    )
    plain = premium_display_text(
        str(style["text"]), style.get("premium_fallbacks")
    )
    if count > 1:
        text_line = (
            f"Texto: <b>{html.escape(plain)}</b>\n"
            f"Se reparte automáticamente en <b>{count}</b> emojis "
            "(los cortes parten letras, sin huecos)\n"
        )
    else:
        text_line = f"Texto: <b>{html.escape(plain)}</b>\n"
    text_color = animated.TEXT_COLORS.get(
        str(style.get("text_color") or "auto"), "Automático"
    )
    stroke_color = animated.STROKE_COLORS.get(
        str(style.get("stroke_color") or "auto"), "Automático"
    )
    motion = animated.MOTIONS.get(str(style.get("motion") or "none"), "Sin animación")
    return (
        "✨ <b>Tu emoji animado</b>\n\n"
        f"{text_line}"
        f"Paleta: {palette} · Fondo: {animated.BACKGROUNDS[style['background']]}\n"
        f"Fuente: {font_label} · Borde: {animated.BORDERS[style['border']]}\n"
        f"Color: {text_color} · Borde letra: {stroke_color}\n"
        f"Animación: {motion} · Velocidad: {animated.SPEED_LABELS[style['speed']]}\n\n"
        "Si te gusta, pulsa <b>✅ Crear pack</b>."
    )


async def generate_anim_chunks(
    style: dict[str, Any], premium_images: list[list[bytes]] | None = None
) -> list[bytes]:
    """Genera un WebM por trozo, cortando la palabra completa.

    El texto se dibuja una sola vez y se divide en cuadrados; así no hay
    separación entre letras y el fondo y el degradado son continuos.
    """
    return await asyncio.to_thread(
        animated.generate_word,
        str(style["text"]),
        style["palette"],
        style["background"],
        style["border"],
        style["speed"],
        premium_images or [],
        font_key=str(style.get("font") or "bold"),
        text_color_key=str(style.get("text_color") or "auto"),
        stroke_color_key=str(style.get("stroke_color") or "auto"),
        motion=str(style.get("motion") or "none"),
    )


async def generate_anim_preview(
    style: dict[str, Any], premium_images: list[list[bytes]] | None = None
) -> bytes:
    """Vista previa en una sola tira: así no se ven huecos entre trozos."""
    return await asyncio.to_thread(
        animated.generate_word_preview,
        str(style["text"]),
        style["palette"],
        style["background"],
        style["border"],
        style["speed"],
        premium_images or [],
        font_key=str(style.get("font") or "bold"),
        text_color_key=str(style.get("text_color") or "auto"),
        stroke_color_key=str(style.get("stroke_color") or "auto"),
        motion=str(style.get("motion") or "none"),
    )


async def send_anim_preview(
    callback: CallbackQuery, bot: Bot, state: FSMContext, style: dict[str, Any]
) -> None:
    data = await state.get_data()
    premium = data.get("premium_images") or []
    count = animated.word_count(
        str(style["text"]), len(premium), str(style.get("font") or "bold")
    )
    await callback.answer("Generando…")
    with suppress(TelegramBadRequest):
        await callback.message.edit_text("🎨 Generando la vista previa…")
    try:
        if count == 1:
            # Un solo emoji: la vista previa ES el sticker final (WebM con alfa).
            chunks: list[bytes] | None = await generate_anim_chunks(
                style, premium
            )
            video = None
        else:
            chunks = None
            video = await generate_anim_preview(style, premium)
    except animated.EmojiError as exc:
        await callback.message.edit_text(
            f"No pude generar el emoji: <i>{html.escape(str(exc))}</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[anim_cancel_row()]),
        )
        return
    except Exception:  # noqa: BLE001 - nunca dejar el mensaje en "Generando…"
        logger.exception("Fallo inesperado generando la vista previa")
        with suppress(TelegramBadRequest):
            await callback.message.edit_text(
                "No pude generar la vista previa. Prueba otra vez o cambia de estilo.",
                reply_markup=anim_preview_kb(),
            )
        return
    await state.set_state(AnimWizard.preview)
    if chunks is not None:
        await _api_retry(
            callback.message.answer_sticker,
            BufferedInputFile(chunks[0], filename="emoji.webm"),
        )
    else:
        try:
            await _api_retry(
                callback.message.answer_animation,
                BufferedInputFile(video, filename="vista-previa.mp4"),
            )
        except TelegramBadRequest:
            # Si Telegram no acepta la tira, al menos se ven los trozos reales.
            for chunk in await generate_anim_chunks(style, premium):
                await _api_retry(
                    callback.message.answer_sticker,
                    BufferedInputFile(chunk, filename="emoji.webm"),
                )
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            anim_preview_text(style, len(premium)), reply_markup=anim_preview_kb()
        )


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #
dp = Dispatcher(storage=MemoryStorage())


@dp.message(CommandStart())
async def cmd_start(
    message: Message, bot: Bot, store: Store, state: FSMContext
) -> None:
    logger.info("start de %s", message.from_user.id)
    await state.clear()
    text, keyboard = await panel_view(bot, store, message.from_user.id)
    await message.answer(text, reply_markup=keyboard)


@dp.message(Command("canales"))
async def cmd_channels(
    message: Message, bot: Bot, store: Store, state: FSMContext
) -> None:
    await state.clear()
    items = mine(store, message.from_user.id)
    if not items:
        text, keyboard = await panel_view(bot, store, message.from_user.id)
        await message.answer(text, reply_markup=keyboard)
        return
    await message.answer(
        "Tus canales:", reply_markup=channels_keyboard(items)
    )


@dp.callback_query(F.data == "menu:main")
async def cb_main(callback: CallbackQuery, bot: Bot, store: Store) -> None:
    text, keyboard = await panel_view(bot, store, callback.from_user.id)
    await callback.answer()
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(text, reply_markup=keyboard)


@dp.callback_query(F.data == "menu:list")
async def cb_list(callback: CallbackQuery, bot: Bot, store: Store) -> None:
    items = mine(store, callback.from_user.id)
    if items:
        await callback.answer()
    else:
        await callback.answer("No tienes canales todavía.")
    if not items:
        text, keyboard = await panel_view(bot, store, callback.from_user.id)
        with suppress(TelegramBadRequest):
            await callback.message.edit_text(text, reply_markup=keyboard)
        return
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            "Elige un canal para verlo o actualizarlo:",
            reply_markup=channels_keyboard(items),
        )


@dp.callback_query(F.data == "menu:help")
async def cb_help(callback: CallbackQuery) -> None:
    await callback.answer()
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            HELP_TEXT,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="⬅️ Volver", callback_data="menu:main")
                    ]
                ]
            ),
        )


# --------------------------------------------------------------------------- #
# Emoji animado: handlers
# --------------------------------------------------------------------------- #
ANIM_TEXT_PROMPT = (
    "✍️ Escribe la palabra del emoji.\n\n"
    "El máximo son 14 caracteres. No hace falta separar nada: el bot calcula "
    "su tamaño y la reparte automáticamente en varios emojis. Los cortes pueden "
    "partir una letra por la mitad para que la palabra quede compacta, sin "
    "huecos entre trozos.\n\n"
    "También puedes incluir <b>emojis premium</b> (los de Telegram Premium) "
    "junto al texto: se dibujan dentro del emoji, como una letra más, y "
    "conservan su animación si la tienen."
)


async def _start_anim_wizard(
    message: Message,
    state: FSMContext,
    bot: Bot,
    examples_store: Store,
    user_id: int,
) -> None:
    await state.clear()
    await state.set_state(AnimWizard.text)
    # Los ejemplos se generan en segundo plano: el primer menú sale sin ellos
    # y los siguientes ya los muestran.
    start_examples(bot, examples_store, user_id)
    await message.answer(
        ANIM_TEXT_PROMPT,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[anim_cancel_row()]),
    )


@dp.callback_query(F.data == "anim:new")
async def cb_anim_new(
    callback: CallbackQuery, state: FSMContext, bot: Bot, examples_store: Store
) -> None:
    await _start_anim_wizard(
        callback.message, state, bot, examples_store, callback.from_user.id
    )
    await callback.answer()


@dp.message(Command("animar"), F.chat.type == ChatType.PRIVATE)
async def cmd_animar(
    message: Message, state: FSMContext, bot: Bot, examples_store: Store
) -> None:
    assert message.from_user is not None
    await _start_anim_wizard(
        message, state, bot, examples_store, message.from_user.id
    )


@dp.callback_query(F.data == "anim:cancel")
async def cb_anim_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            "Creación cancelada.\n\nPuedes empezar otra vez desde el panel.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="✨ Emoji animado", callback_data="anim:new"
                        )
                    ],
                    [InlineKeyboardButton(text="🎛 Panel", callback_data="menu:main")],
                ]
            ),
        )
    await callback.answer()


@dp.message(AnimWizard.text)
async def anim_text(message: Message, bot: Bot, state: FSMContext) -> None:
    raw = message.text or ""
    if not raw.strip():
        await message.answer("Envíame el texto del emoji, por ejemplo: <b>HOLA</b>")
        return

    # Los emojis premium llegan como entidades: se sustituyen por un marcador
    # que luego se dibuja dentro del emoji, como una letra más.
    text, emoji_ids, fallbacks = extract_premium_emojis(raw, message.entities)
    # Los separadores antiguos ya no hacen falta: P|R|O|M|O se lee como PROMO.
    text = " ".join(text.replace("|", " ").split())
    if not emoji_ids:
        # "P R O M O" también se une: el bot reparte las letras solo.
        text = compact_letters(text)
    if not text:
        await message.answer("Envíame una palabra, por ejemplo: <b>PROMO</b>")
        return
    if len(text) > 14:
        await message.answer("Son demasiados caracteres: usa como máximo 14.")
        return

    premium_images: list[list[bytes]] = []
    if emoji_ids:
        try:
            premium_images = await fetch_premium_images(bot, emoji_ids)
        except TelegramBadRequest as exc:
            await message.answer(
                "No pude leer esos emojis premium: "
                f"<i>{html.escape(exc.message)}</i>"
            )
            return

    logger.info(
        "Emoji animado: %d premium (%s) en %r",
        len(premium_images),
        [len(frames) for frames in premium_images],
        text,
    )

    style = dict(ANIM_STYLE_DEFAULT)
    style["text"] = text
    style["premium_fallbacks"] = fallbacks
    await state.update_data(style=style, premium_images=premium_images)
    await state.set_state(AnimWizard.font)

    try:
        count = animated.word_count(
            text, len(premium_images), str(style.get("font") or "bold")
        )
    except animated.EmojiError as exc:
        await message.answer(f"No pude preparar el texto: {html.escape(str(exc))}")
        return

    if count > 1:
        summary = (
            f"Se repartirá automáticamente en <b>{count}</b> emojis "
            "(los cortes parten letras, sin huecos)."
        )
    else:
        summary = "Cabe en <b>un solo emoji</b>."

    display = premium_display_text(text, fallbacks)
    await _answer_menu(
        message,
        "font",
        anim_font_kb(),
        f"Texto: <b>{html.escape(display)}</b>\n\n{summary}",
    )


@dp.callback_query(
    F.data.startswith("anim:font:"),
    StateFilter(AnimWizard.font, AnimWizard.preview),
)
async def cb_anim_font(
    callback: CallbackQuery, bot: Bot, state: FSMContext
) -> None:
    key = callback.data.split(":", 2)[2]
    if key not in animated.FONT_CHOICES:
        await callback.answer("Esa fuente no existe.", show_alert=True)
        return
    style = anim_style(await state.get_data())
    style["font"] = key
    await state.update_data(style=style)

    if await state.get_state() == AnimWizard.preview.state:
        await send_anim_preview(callback, bot, state, style)
        return

    await state.set_state(AnimWizard.palette)
    await callback.answer()
    display = premium_display_text(
        str(style["text"]), style.get("premium_fallbacks")
    )
    await _edit_menu(
        callback,
        "pal",
        anim_palette_kb(),
        f"Texto: <b>{html.escape(display)}</b>",
    )


@dp.callback_query(
    F.data.startswith("anim:pal:"),
    StateFilter(AnimWizard.palette, AnimWizard.preview),
)
async def cb_anim_palette(
    callback: CallbackQuery, bot: Bot, state: FSMContext
) -> None:
    key = callback.data.split(":", 2)[2]
    if key not in animated.PALETTES:
        await callback.answer("Esa paleta no existe.", show_alert=True)
        return
    style = anim_style(await state.get_data())
    style["palette"] = key
    await state.update_data(style=style)

    if await state.get_state() == AnimWizard.preview.state:
        await send_anim_preview(callback, bot, state, style)
        return

    await state.set_state(AnimWizard.background)
    await callback.answer()
    display = premium_display_text(
        str(style["text"]), style.get("premium_fallbacks")
    )
    await _edit_menu(
        callback,
        "bg",
        anim_background_kb(),
        f"Texto: <b>{html.escape(display)}</b>",
    )


@dp.callback_query(
    F.data.startswith("anim:bg:"),
    StateFilter(AnimWizard.background, AnimWizard.preview),
)
async def cb_anim_background(
    callback: CallbackQuery, bot: Bot, state: FSMContext
) -> None:
    key = callback.data.split(":", 2)[2]
    if key not in animated.BACKGROUNDS:
        await callback.answer("Ese fondo no existe.", show_alert=True)
        return
    style = anim_style(await state.get_data())
    style["background"] = key
    await state.update_data(style=style)

    if await state.get_state() == AnimWizard.preview.state:
        await send_anim_preview(callback, bot, state, style)
        return

    await state.set_state(AnimWizard.border)
    await callback.answer()
    await _edit_menu(callback, "border", anim_border_kb())


@dp.callback_query(
    F.data.startswith("anim:border:"),
    StateFilter(AnimWizard.border, AnimWizard.preview),
)
async def cb_anim_border(
    callback: CallbackQuery, bot: Bot, state: FSMContext
) -> None:
    key = callback.data.split(":", 2)[2]
    if key not in animated.BORDERS:
        await callback.answer("Ese borde no existe.", show_alert=True)
        return
    style = anim_style(await state.get_data())
    style["border"] = key
    await state.update_data(style=style)

    if await state.get_state() == AnimWizard.preview.state:
        await send_anim_preview(callback, bot, state, style)
        return

    await state.set_state(AnimWizard.textcolor)
    await callback.answer()
    await _edit_menu(callback, "tcol", anim_textcolor_kb())


@dp.callback_query(
    F.data.startswith("anim:tcol:"),
    StateFilter(AnimWizard.textcolor, AnimWizard.preview),
)
async def cb_anim_text_color(
    callback: CallbackQuery, bot: Bot, state: FSMContext
) -> None:
    key = callback.data.split(":", 2)[2]
    if key not in animated.TEXT_COLORS:
        await callback.answer("Ese color no existe.", show_alert=True)
        return
    style = anim_style(await state.get_data())
    style["text_color"] = key
    await state.update_data(style=style)

    if await state.get_state() == AnimWizard.preview.state:
        await send_anim_preview(callback, bot, state, style)
        return

    await state.set_state(AnimWizard.letter_stroke)
    await callback.answer()
    await _edit_menu(callback, "stroke", anim_stroke_kb())


@dp.callback_query(
    F.data.startswith("anim:stroke:"),
    StateFilter(AnimWizard.letter_stroke, AnimWizard.preview),
)
async def cb_anim_stroke_color(
    callback: CallbackQuery, bot: Bot, state: FSMContext
) -> None:
    key = callback.data.split(":", 2)[2]
    if key not in animated.STROKE_COLORS:
        await callback.answer("Ese borde no existe.", show_alert=True)
        return
    style = anim_style(await state.get_data())
    style["stroke_color"] = key
    await state.update_data(style=style)

    if await state.get_state() == AnimWizard.preview.state:
        await send_anim_preview(callback, bot, state, style)
        return

    await state.set_state(AnimWizard.motion)
    await callback.answer()
    await _edit_menu(callback, "motion", anim_motion_kb())


@dp.callback_query(
    F.data.startswith("anim:motion:"),
    StateFilter(AnimWizard.motion, AnimWizard.preview),
)
async def cb_anim_motion(
    callback: CallbackQuery, bot: Bot, state: FSMContext
) -> None:
    key = callback.data.split(":", 2)[2]
    if key not in animated.MOTIONS:
        await callback.answer("Esa animación no existe.", show_alert=True)
        return
    style = anim_style(await state.get_data())
    style["motion"] = key
    await state.update_data(style=style)
    await send_anim_preview(callback, bot, state, style)


@dp.callback_query(F.data == "anim:motion", AnimWizard.preview)
async def cb_anim_open_motion(callback: CallbackQuery) -> None:
    await _edit_menu(callback, "motion", anim_motion_kb(back=True))
    await callback.answer()


@dp.callback_query(F.data == "anim:textcolor", AnimWizard.preview)
async def cb_anim_open_text_color(callback: CallbackQuery) -> None:
    await _edit_menu(callback, "tcol", anim_textcolor_kb(back=True))
    await callback.answer()


@dp.callback_query(F.data == "anim:strokecolor", AnimWizard.preview)
async def cb_anim_open_stroke_color(callback: CallbackQuery) -> None:
    await _edit_menu(callback, "stroke", anim_stroke_kb(back=True))
    await callback.answer()


@dp.callback_query(F.data == "anim:speed", AnimWizard.preview)
async def cb_anim_speed(
    callback: CallbackQuery, bot: Bot, state: FSMContext
) -> None:
    style = anim_style(await state.get_data())
    index = ANIM_SPEED_ORDER.index(style["speed"])
    style["speed"] = ANIM_SPEED_ORDER[(index + 1) % len(ANIM_SPEED_ORDER)]
    await state.update_data(style=style)
    await send_anim_preview(callback, bot, state, style)


@dp.callback_query(F.data == "anim:palette", AnimWizard.preview)
async def cb_anim_open_palette(callback: CallbackQuery) -> None:
    await _edit_menu(callback, "pal", anim_palette_kb(back=True))
    await callback.answer()


@dp.callback_query(F.data == "anim:background", AnimWizard.preview)
async def cb_anim_open_background(callback: CallbackQuery) -> None:
    await _edit_menu(callback, "bg", anim_background_kb(back=True))
    await callback.answer()


@dp.callback_query(F.data == "anim:font", AnimWizard.preview)
async def cb_anim_open_font(callback: CallbackQuery) -> None:
    await _edit_menu(callback, "font", anim_font_kb(back=True))
    await callback.answer()


@dp.callback_query(F.data == "anim:border", AnimWizard.preview)
async def cb_anim_open_border(callback: CallbackQuery) -> None:
    await _edit_menu(callback, "border", anim_border_kb(back=True))
    await callback.answer()


@dp.callback_query(F.data == "anim:preview", AnimWizard.preview)
async def cb_anim_preview(
    callback: CallbackQuery, bot: Bot, state: FSMContext
) -> None:
    style = anim_style(await state.get_data())
    await send_anim_preview(callback, bot, state, style)


@dp.callback_query(F.data == "anim:create", AnimWizard.preview)
async def cb_anim_create(
    callback: CallbackQuery, bot: Bot, state: FSMContext, anim_store: Store
) -> None:
    await callback.answer("Creando el pack…")
    data = await state.get_data()
    premium_images: list[list[bytes]] = data.get("premium_images") or []
    style = anim_style(data)
    try:
        expected = animated.word_count(
            str(style["text"]),
            len(premium_images),
            str(style.get("font") or "bold"),
        )
    except animated.EmojiError:
        expected = 1
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            f"⏳ Generando {expected} emoji(s) y añadiéndolos al pack…"
        )

    try:
        webms = await generate_anim_chunks(style, premium_images)
        count = len(webms)
        set_name, emoji_ids, created = await add_animated_emojis(
            bot, anim_store, callback.from_user.id, webms
        )
    except (animated.EmojiError, TelegramBadRequest, TelegramRetryAfter) as exc:
        logger.warning("No pude crear el pack animado: %s", exc)
        with suppress(TelegramBadRequest):
            await callback.message.edit_text(
                f"No pude crear el pack: <i>{html.escape(str(exc))}</i>",
                reply_markup=anim_preview_kb(),
            )
        return

    await state.clear()
    record = anim_store.get(callback.from_user.id) or {}
    link = f"https://t.me/addemoji/{set_name}"
    if created and count > 1:
        head = f"✅ Pack animado creado con {count} emojis"
    elif created:
        head = "✅ Pack animado creado"
    elif count > 1:
        head = f"✅ {count} emojis añadidos a tu pack"
    else:
        head = "✅ Emoji añadido a tu pack"

    tags = [
        (
            f'<tg-emoji emoji-id="{emoji_id}">{STICKER_EMOJI}</tg-emoji>'
            if emoji_id
            else STICKER_EMOJI
        )
        for emoji_id in emoji_ids
    ]
    # Sin espacios: los trozos son cortes de la misma palabra y deben quedar
    # pegados para que el degradado y las letras encajen.
    preview = "".join(tags) or STICKER_EMOJI
    plain = premium_display_text(
        str(style["text"]), style.get("premium_fallbacks")
    )
    text = (
        f"{head}\n\n"
        f"Así se verá: {preview}\n"
        f"<b>{html.escape(plain)}</b>\n\n"
        f"{link}\n"
        f"Emojis en tu pack: <b>{record.get('count', 1)}</b>"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Abrir pack", url=link)],
            [InlineKeyboardButton(text="✨ Crear otro", callback_data="anim:new")],
            [InlineKeyboardButton(text="🎛 Panel", callback_data="menu:main")],
        ]
    )
    no_preview = LinkPreviewOptions(is_disabled=True)
    try:
        await callback.message.edit_text(
            text, reply_markup=keyboard, link_preview_options=no_preview
        )
    except TelegramBadRequest:
        # Si Telegram rechaza el custom emoji en el texto, mandamos el normal.
        await callback.message.edit_text(
            text.replace(preview, plain),
            reply_markup=keyboard,
            link_preview_options=no_preview,
        )


@dp.callback_query(F.data.startswith("anim:"))
async def cb_anim_fallback(callback: CallbackQuery) -> None:
    """Responde a botones del asistente cuyo estado ya no existe.

    Con MemoryStorage el estado se pierde al reiniciar el bot (o al caducar la
    sesión); sin esto, el botón se queda cargando y parece que no responde.
    """
    await callback.answer(
        "Ese botón ya no está activo. Usa los botones del mensaje actual "
        "o escribe /animar para empezar de nuevo.",
        show_alert=True,
    )


# --------------------------------------------------------------------------- #
# Emoji desde una imagen
# --------------------------------------------------------------------------- #
IMG_SHAPES = {
    "circle": "⭕ Círculo",
    "square": "⬜ Cuadrado",
    "rounded": "🔲 Redondeado",
}


class ImgWizard(StatesGroup):
    image = State()
    shape = State()
    effect = State()
    preview = State()


def img_shape_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=label, callback_data=f"img:shape:{shape}"
                )
                for shape, label in IMG_SHAPES.items()
            ],
            [InlineKeyboardButton(text="❌ Cancelar", callback_data="img:cancel")],
        ]
    )


def img_effect_kb() -> InlineKeyboardMarkup:
    keys = list(animated.LOGO_EFFECTS)
    rows = []
    for index in range(0, len(keys), 3):
        rows.append(
            [
                InlineKeyboardButton(
                    text=animated.LOGO_EFFECTS[key],
                    callback_data=f"img:fx:{key}",
                )
                for key in keys[index : index + 3]
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="🔷 Cambiar forma", callback_data="img:back:shape"
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="❌ Cancelar", callback_data="img:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def img_background_kb(current: str = "transparent") -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=("✅ " if key == current else "") + label,
                callback_data=f"img:bg:{key}",
            )
        ]
        for key, label in animated.BACKGROUNDS.items()
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Volver a la vista previa", callback_data="img:preview"
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="❌ Cancelar", callback_data="img:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def img_palette_kb(current: str = "neon") -> InlineKeyboardMarkup:
    keys = list(animated.PALETTES)
    rows = []
    for index in range(0, len(keys), 3):
        rows.append(
            [
                InlineKeyboardButton(
                    text=("✅ " if key == current else "")
                    + animated.PALETTES[key]["label"],
                    callback_data=f"img:pal:{key}",
                )
                for key in keys[index : index + 3]
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Volver a la vista previa", callback_data="img:preview"
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="❌ Cancelar", callback_data="img:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def img_preview_kb(background: str = "transparent") -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text="✅ Guardar en mi pack", callback_data="img:save"
            )
        ],
        [
            InlineKeyboardButton(
                text="🔷 Cambiar forma", callback_data="img:back:shape"
            ),
            InlineKeyboardButton(
                text="🎞 Cambiar efecto", callback_data="img:back:effect"
            ),
        ],
        [InlineKeyboardButton(text="🎨 Fondo", callback_data="img:background")],
    ]
    if background != "transparent":
        rows[-1].append(
            InlineKeyboardButton(text="🎨 Paleta", callback_data="img:palette")
        )
    rows.append([InlineKeyboardButton(text="❌ Cancelar", callback_data="img:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _download_file(bot: Bot, file_id: str) -> bytes:
    tg_file = await bot.get_file(file_id)
    buffer = await bot.download_file(tg_file.file_path)
    return buffer.getvalue()


@dp.callback_query(F.data == "img:new")
async def cb_img_new(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(ImgWizard.image)
    await callback.answer()
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            "🖼 <b>Emoji desde una imagen</b>\n\n"
            "Envíame una foto o una imagen (mejor como archivo para que no "
            "pierda calidad).\n\n"
            "Después eliges la forma y el efecto de animación.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="❌ Cancelar", callback_data="img:cancel")]
                ]
            ),
        )


@dp.message(Command("emoji"), F.chat.type == ChatType.PRIVATE)
async def cmd_emoji(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(ImgWizard.image)
    await message.answer(
        "🖼 Envíame la imagen que quieres convertir en emoji animado.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="❌ Cancelar", callback_data="img:cancel")]
            ]
        ),
    )


@dp.message(ImgWizard.image)
async def img_received(message: Message, state: FSMContext) -> None:
    file_id = None
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        file_id = message.document.file_id

    if not file_id:
        await message.answer(
            "📷 Envíame una imagen (foto o archivo de imagen), no texto."
        )
        return

    await state.update_data(
        image_file_id=file_id,
        shape="rounded",
        background="transparent",
        palette="neon",
    )
    await state.set_state(ImgWizard.shape)
    await message.answer(
        "🔷 <b>Elige la forma</b> del emoji:", reply_markup=img_shape_kb()
    )


@dp.callback_query(
    F.data.startswith("img:shape:"),
    StateFilter(ImgWizard.shape, ImgWizard.preview),
)
async def cb_img_shape(callback: CallbackQuery, state: FSMContext) -> None:
    shape = callback.data.rsplit(":", 1)[1]
    if shape not in IMG_SHAPES:
        await callback.answer("Esa forma no existe.", show_alert=True)
        return
    await state.update_data(shape=shape)
    await state.set_state(ImgWizard.effect)
    await callback.answer()
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            f"🔷 Forma: <b>{IMG_SHAPES[shape]}</b>\n\n"
            "🎞 <b>Elige el efecto</b> de animación:",
            reply_markup=img_effect_kb(),
        )


async def _img_generate_preview(
    callback: CallbackQuery, bot: Bot, state: FSMContext
) -> None:
    """Genera la imagen animada con la forma, efecto, fondo y paleta actuales."""
    data = await state.get_data()
    file_id = data.get("image_file_id")
    if not file_id:
        await callback.message.edit_text(
            "La imagen se perdió; envíala otra vez.",
            reply_markup=img_shape_kb(),
        )
        await state.set_state(ImgWizard.image)
        return

    effect = str(data.get("effect") or "shine")
    shape = str(data.get("shape") or "rounded")
    background = str(data.get("background") or "transparent")
    palette = str(data.get("palette") or "neon")
    label = animated.LOGO_EFFECTS.get(effect, effect)

    try:
        source = await _download_file(bot, file_id)
        webm = await asyncio.to_thread(
            animated.generate_image_emoji,
            source,
            shape,
            effect,
            palette,
            background,
        )
    except (InvalidImage, animated.EmojiError, TelegramBadRequest) as exc:
        with suppress(TelegramBadRequest):
            await callback.message.edit_text(
                f"No pude animar la imagen: <i>{html.escape(str(exc))}</i>",
                reply_markup=img_preview_kb(background),
            )
        return

    await state.update_data(webm=webm, background=background, palette=palette)
    await callback.message.answer_sticker(
        BufferedInputFile(webm, filename="emoji.webm")
    )
    await state.set_state(ImgWizard.preview)
    details = (
        f"✨ <b>{label}</b> · forma {IMG_SHAPES.get(shape, shape)}\n"
        f"Fondo: {animated.BACKGROUNDS.get(background, background)}"
    )
    if background != "transparent":
        details += f" · Paleta: {animated.PALETTES.get(palette, {}).get('label', palette)}"
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            f"{details}\n\n¿Lo guardo en tu pack animado?",
            reply_markup=img_preview_kb(background),
        )


@dp.callback_query(
    F.data.startswith("img:fx:"),
    StateFilter(ImgWizard.effect, ImgWizard.preview),
)
async def cb_img_effect(
    callback: CallbackQuery, bot: Bot, state: FSMContext
) -> None:
    effect = callback.data.rsplit(":", 1)[1]
    if effect not in animated.LOGO_EFFECTS:
        await callback.answer("Ese efecto no existe.", show_alert=True)
        return

    data = await state.get_data()
    if not data.get("image_file_id"):
        await callback.answer(
            "La imagen se perdió; envíala otra vez.", show_alert=True
        )
        await state.set_state(ImgWizard.image)
        return

    await state.update_data(effect=effect)
    label = animated.LOGO_EFFECTS[effect]
    await callback.answer("Generando…")
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(f"🎨 Generando <b>{label}</b>…")
    await _img_generate_preview(callback, bot, state)


@dp.callback_query(F.data == "img:background", ImgWizard.preview)
async def cb_img_open_background(
    callback: CallbackQuery, state: FSMContext
) -> None:
    data = await state.get_data()
    await callback.answer()
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            "🎨 <b>Elige el fondo</b> del emoji:\n\n"
            "El emoji animado se mueve encima del fondo elegido.",
            reply_markup=img_background_kb(
                str(data.get("background") or "transparent")
            ),
        )


@dp.callback_query(F.data.startswith("img:bg:"), ImgWizard.preview)
async def cb_img_background(
    callback: CallbackQuery, bot: Bot, state: FSMContext
) -> None:
    key = callback.data.rsplit(":", 1)[1]
    if key not in animated.BACKGROUNDS:
        await callback.answer("Ese fondo no existe.", show_alert=True)
        return
    await state.update_data(background=key)
    await callback.answer("Generando…")
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            f"🎨 Generando el fondo <b>{animated.BACKGROUNDS[key]}</b>…"
        )
    await _img_generate_preview(callback, bot, state)


@dp.callback_query(F.data == "img:palette", ImgWizard.preview)
async def cb_img_open_palette(
    callback: CallbackQuery, state: FSMContext
) -> None:
    data = await state.get_data()
    if data.get("background") in (None, "transparent"):
        await callback.answer(
            "Elige primero un fondo de color para usar una paleta.",
            show_alert=True,
        )
        return
    await callback.answer()
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            "🎨 <b>Elige la paleta</b> del fondo:",
            reply_markup=img_palette_kb(str(data.get("palette") or "neon")),
        )


@dp.callback_query(F.data.startswith("img:pal:"), ImgWizard.preview)
async def cb_img_palette(
    callback: CallbackQuery, bot: Bot, state: FSMContext
) -> None:
    key = callback.data.rsplit(":", 1)[1]
    if key not in animated.PALETTES:
        await callback.answer("Esa paleta no existe.", show_alert=True)
        return
    await state.update_data(palette=key)
    await callback.answer("Generando…")
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            f"🎨 Generando con la paleta "
            f"<b>{animated.PALETTES[key]['label']}</b>…"
        )
    await _img_generate_preview(callback, bot, state)


@dp.callback_query(F.data == "img:preview", ImgWizard.preview)
async def cb_img_preview(
    callback: CallbackQuery, bot: Bot, state: FSMContext
) -> None:
    await callback.answer("Generando…")
    with suppress(TelegramBadRequest):
        await callback.message.edit_text("🎨 Generando la vista previa…")
    await _img_generate_preview(callback, bot, state)


@dp.callback_query(F.data == "img:back:shape", StateFilter(ImgWizard.effect, ImgWizard.preview))
async def cb_img_back_shape(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ImgWizard.shape)
    await callback.answer()
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            "🔷 <b>Elige la forma</b> del emoji:", reply_markup=img_shape_kb()
        )


@dp.callback_query(F.data == "img:back:effect", ImgWizard.preview)
async def cb_img_back_effect(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ImgWizard.effect)
    await callback.answer()
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            "🎞 <b>Elige el efecto</b> de animación:", reply_markup=img_effect_kb()
        )


@dp.callback_query(F.data == "img:save", ImgWizard.preview)
async def cb_img_save(
    callback: CallbackQuery, bot: Bot, state: FSMContext, anim_store: Store
) -> None:
    data = await state.get_data()
    webm = data.get("webm")
    if not webm:
        await callback.answer(
            "La vista previa caducó; elige el efecto otra vez.", show_alert=True
        )
        return

    await callback.answer("Guardando…")
    try:
        set_name, emoji_id, created = await add_animated_emoji(
            bot, anim_store, callback.from_user.id, webm
        )
    except (animated.EmojiError, TelegramBadRequest) as exc:
        with suppress(TelegramBadRequest):
            await callback.message.edit_text(
                f"No pude guardar el emoji: <i>{html.escape(str(exc))}</i>",
                reply_markup=img_preview_kb(),
            )
        return

    await state.clear()
    link = f"https://t.me/addemoji/{set_name}"
    preview = (
        f'<tg-emoji emoji-id="{emoji_id}">{STICKER_EMOJI}</tg-emoji>'
        if emoji_id
        else STICKER_EMOJI
    )
    head = "✅ Emoji creado" if created else "✅ Emoji añadido a tu pack"
    text = (
        f"{head}\n\n"
        f"Así se verá: {preview}\n\n"
        f"{link}"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Abrir pack", url=link)],
            [
                InlineKeyboardButton(
                    text="🖼 Crear otro", callback_data="img:new"
                )
            ],
            [InlineKeyboardButton(text="🎛 Panel", callback_data="menu:main")],
        ]
    )
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            text,
            reply_markup=keyboard,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )


@dp.callback_query(F.data == "img:cancel")
async def cb_img_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            "Creación cancelada.\n\nPuedes empezar otra vez desde el panel.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🖼 Emoji desde imagen", callback_data="img:new"
                        )
                    ],
                    [InlineKeyboardButton(text="🎛 Panel", callback_data="menu:main")],
                ]
            ),
        )


@dp.callback_query(F.data.startswith("img:"))
async def cb_img_fallback(callback: CallbackQuery) -> None:
    await callback.answer(
        "Ese botón ya no está activo. Pulsa /emoji o abre el panel para empezar.",
        show_alert=True,
    )


@dp.callback_query(F.data.startswith("ch:"))
async def cb_channel(callback: CallbackQuery, bot: Bot, store: Store) -> None:
    parts = callback.data.split(":")
    # "ch:<id>" abre el canal; "ch:<acción>:<id>" ejecuta la acción.
    if len(parts) >= 3:
        action, chat_id = parts[1], parts[2]
    else:
        action, chat_id = "view", parts[1]
    param = parts[3] if len(parts) > 3 else None
    record = store.get(int(chat_id))
    if record is None:
        await callback.answer("Ese canal ya no está en el panel.", show_alert=True)
        await cb_list(callback, bot, store)
        return
    if record.get("owner_id") != callback.from_user.id:
        await callback.answer("Este canal no está en tu panel.", show_alert=True)
        return

    if action == "anim":
        await show_effect_picker(callback, chat_id, record)
        return

    if action == "fx":
        await preview_channel_effect(
            callback, bot, chat_id, record, param or "shine"
        )
        return

    if action == "fxsave":
        await save_channel_effect(
            callback, bot, store, chat_id, record, param or "shine"
        )
        return

    if action == "refresh":
        try:
            chat = await bot.get_chat(int(chat_id))
            record, changed = await sync_channel(
                bot, store, chat, record["owner_id"], force=True
            )
        except NoChannelLogo:
            await callback.answer("El canal no tiene foto.", show_alert=True)
            return
        except (InvalidImage, TelegramBadRequest) as exc:
            await callback.answer(f"No pude actualizar: {exc}", show_alert=True)
            return
        await callback.answer()
        with suppress(TelegramBadRequest):
            await callback.message.edit_text(
                ("✅ Emoji actualizado.\n\n" if changed else "ℹ️ Sin cambios.\n\n")
                + channel_text(record, chat_id),
                reply_markup=channel_keyboard(chat_id, record),
            )
        return

    if action == "del":
        await callback.answer()
        with suppress(TelegramBadRequest):
            await callback.message.edit_text(
                f"¿Quitar <b>{record.get('title') or chat_id}</b> del bot?\n\n"
                "Se borrarán también sus packs de emojis (estático y animado) en "
                "Telegram. Esta acción no se puede deshacer.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="Sí, borrar",
                                callback_data=f"ch:delok:{chat_id}",
                            ),
                            InlineKeyboardButton(
                                text="Cancelar", callback_data=f"ch:{chat_id}"
                            ),
                        ]
                    ]
                ),
            )
        return

    if action == "delok":
        with suppress(TelegramBadRequest):
            await bot.delete_sticker_set(record["set_name"])
        if record.get("anim_set_name"):
            with suppress(TelegramBadRequest):
                await bot.delete_sticker_set(record["anim_set_name"])
        await store.delete(int(chat_id))
        await callback.answer("Canal quitado.")
        text, keyboard = await panel_view(bot, store, callback.from_user.id)
        with suppress(TelegramBadRequest):
            await callback.message.edit_text(text, reply_markup=keyboard)
        return

    await callback.answer()
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            channel_text(record, chat_id),
            reply_markup=channel_keyboard(chat_id, record),
        )


@dp.message(Command("convert"), F.chat.type == ChatType.PRIVATE)
async def cmd_convert_private(
    message: Message, command: CommandObject, bot: Bot, store: Store
) -> None:
    raw = (command.args or "").strip()
    target: int | str | None = parse_target(raw) if raw else None
    if target is None and message.reply_to_message:
        target = chat_ref_from_message(message.reply_to_message)
    if target is None:
        await message.answer(
            "Dime el canal: <code>/convert @micanal</code>, o reenvíame un "
            "mensaje suyo y responde con <code>/convert</code>.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ Panel", callback_data="menu:main")]
                ]
            ),
        )
        return

    try:
        chat = await bot.get_chat(target)
    except TelegramBadRequest as exc:
        await message.answer(
            f"No pude abrir <code>{raw or target}</code>. Comprueba el enlace y "
            f"que el bot esté en el canal.\n\n<i>{exc.message}</i>"
        )
        return

    warning = await admin_warning(bot, chat)
    if warning:
        await message.answer(warning)
        return

    status = await message.answer("⏳ Convirtiendo el logo…")
    existing = store.get(chat.id)
    try:
        record, changed = await sync_channel(bot, store, chat, message.from_user.id)
    except NoChannelLogo:
        await status.edit_text("Ese canal no tiene foto de perfil.")
        return
    except InvalidImage as exc:
        await status.edit_text(str(exc))
        return
    except TelegramBadRequest as exc:
        await status.edit_text(f"Telegram rechazó el emoji: <i>{exc.message}</i>")
        return

    await status.edit_text(
        result_text(record, changed, created=existing is None),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔗 Abrir pack", url=pack_link(record))],
                [InlineKeyboardButton(text="🎛 Panel", callback_data="menu:main")],
            ]
        ),
    )


@dp.channel_post(Command("convert"))
async def cmd_convert_channel(message: Message, bot: Bot, store: Store) -> None:
    record = store.get(message.chat.id)
    if record is None:
        await message.reply(
            "Todavía no tengo el pack de este canal. Escríbeme por privado y "
            "usa <code>/convert @este_canal</code> una vez."
        )
        return
    chat = await bot.get_chat(message.chat.id)
    try:
        new_record, changed = await sync_channel(bot, store, chat, record["owner_id"])
    except (NoChannelLogo, InvalidImage, TelegramBadRequest) as exc:
        await message.reply(f"No pude actualizar el emoji: <i>{exc}</i>")
        return
    await message.reply(result_text(new_record, changed, created=False))


@dp.my_chat_member()
async def on_bot_membership(
    event: ChatMemberUpdated, bot: Bot, store: Store
) -> None:
    """El bot acaba de entrar como admin en un canal: creamos el pack solo."""
    chat = event.chat
    if chat.type not in (ChatType.CHANNEL, ChatType.SUPERGROUP):
        return
    if event.new_chat_member.status not in (
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.CREATOR,
    ):
        return
    if event.old_chat_member.status == event.new_chat_member.status:
        return

    owner_id = event.from_user.id
    try:
        chat = await bot.get_chat(chat.id)
        record, _ = await sync_channel(bot, store, chat, owner_id)
    except NoChannelLogo:
        logger.info("Canal %s sin foto; no se crea pack", chat.id)
        return
    except Exception:  # noqa: BLE001 - no romper el arranque del bot
        logger.exception("No pude crear el pack al añadirme a %s", chat.id)
        return

    logger.info("Pack creado al añadirme a %s: %s", chat.id, record["set_name"])
    name = record.get("title") or chat.id
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Abrir pack", url=pack_link(record))],
            [InlineKeyboardButton(text="🎛 Panel", callback_data="menu:main")],
        ]
    )
    # El custom emoji en el texto solo lo acepta Telegram en ciertas cuentas;
    # si lo rechaza, reenviamos el mensaje con el emoji normal.
    text = (
        f"✅ Listo el pack de <b>{name}</b>:\n\n"
        f"Así se verá: {emoji_preview(record)} <b>{name}</b>\n\n"
        f"{pack_link(record)}\n"
        "Te he añadido también al panel."
    )
    no_preview = LinkPreviewOptions(is_disabled=True)
    with suppress(Exception):
        try:
            await bot.send_message(
                owner_id,
                text,
                reply_markup=keyboard,
                link_preview_options=no_preview,
            )
        except TelegramBadRequest:
            await bot.send_message(
                owner_id,
                f"✅ Listo el pack de <b>{name}</b>:\n\n"
                f"Así se verá: {STICKER_EMOJI} <b>{name}</b>\n\n"
                f"{pack_link(record)}\n"
                "Te he añadido también al panel.",
                reply_markup=keyboard,
                link_preview_options=no_preview,
            )


@dp.channel_post(F.new_chat_photo)
async def on_channel_photo(message: Message, bot: Bot, store: Store) -> None:
    """La foto cambió y Telegram sí manda este update: refrescamos al momento."""
    record = store.get(message.chat.id)
    if record is None:
        return
    chat = await bot.get_chat(message.chat.id)
    try:
        _, changed = await sync_channel(bot, store, chat, record["owner_id"])
    except (NoChannelLogo, InvalidImage, TelegramBadRequest):
        logger.exception("No pude actualizar el emoji del canal %s", message.chat.id)
        return
    if changed:
        logger.info("Emoji actualizado por cambio de foto en %s", message.chat.id)


async def poll_logos(bot: Bot, store: Store, interval: int) -> None:
    """Respaldo del update anterior: revisa las fotos cada `interval` segundos."""
    while True:
        await asyncio.sleep(interval)
        for chat_id, record in store.all().items():
            try:
                chat = await bot.get_chat(int(chat_id))
                _, changed = await sync_channel(bot, store, chat, record["owner_id"])
                if changed:
                    logger.info("Emoji actualizado por polling en %s", chat_id)
            except NoChannelLogo:
                continue
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - el polling no debe morir por un canal
                logger.exception("Fallo revisando el canal %s", chat_id)
            await asyncio.sleep(0.5)


async def main() -> None:
    load_dotenv()
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Falta BOT_TOKEN. Copia .env.example a .env y rellénalo.")

    interval = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
    data_file = os.getenv("DATA_FILE", "data/channels.json")
    anim_file = os.getenv("ANIM_DATA_FILE", "data/animated.json")
    examples_file = os.getenv("EXAMPLES_DATA_FILE", "data/examples.json")

    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    store = Store(data_file)
    anim_store = Store(anim_file)
    examples_store = Store(examples_file)
    dp["store"] = store
    dp["anim_store"] = anim_store
    dp["examples_store"] = examples_store

    with suppress(TelegramBadRequest):
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Panel de control"),
                BotCommand(command="canales", description="Mis canales"),
                BotCommand(command="convert", description="Convertir el logo de un canal"),
                BotCommand(command="animar", description="Crear un emoji animado"),
                BotCommand(command="emoji", description="Emoji desde una imagen"),
            ]
        )

    # Si ya hay un usuario conocido, los emojis de ejemplo se preparan al
    # arrancar; si no, se crean al abrir el asistente.
    known_owners = [
        record.get("owner_id")
        for record in (*store.all().values(), *anim_store.all().values())
        if record.get("owner_id")
    ]
    if known_owners:
        start_examples(bot, examples_store, int(known_owners[0]))

    logger.info("Bot @%s listo (%d canales registrados)", me.username, len(store.all()))
    poller = asyncio.create_task(poll_logos(bot, store, interval))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        poller.cancel()
        with suppress(asyncio.CancelledError):
            await poller
        await bot.session.close()


def result_text(record: dict[str, Any], changed: bool, created: bool) -> str:
    if created:
        head = "✅ Pack creado"
    elif changed:
        head = "🔄 Emoji actualizado"
    else:
        head = "ℹ️ Ya estaba al día"
    return (
        f"{head} para <b>{record.get('title') or 'el canal'}</b>.\n\n"
        f"Pack: {pack_link(record)}\n\n"
        "El logo queda recortado en círculo y a 100×100. Para usar el emoji "
        "necesitas <b>Telegram Premium</b>; sin Premium el pack se ve, pero no "
        "se puede enviar el custom emoji."
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit) as exc:
        if isinstance(exc, SystemExit) and exc.code:
            print(exc)
