"""Pyrogram ext"""

import asyncio
import functools
import html
import inspect
import os
import secrets
import struct
import time
from copy import deepcopy
from datetime import datetime
from functools import wraps
from io import BytesIO, StringIO
from mimetypes import MimeTypes
from typing import Callable, Iterable, List, Optional, Tuple, Union

import pyrogram
from loguru import logger
from pyrogram import enums, parser, types, utils
from pyrogram.client import Cache
from pyrogram.file_id import FileId
from pyrogram.methods.messages.inline_session import get_session
from pyrogram.enums import MessageEntityType
from pyrogram.errors import AuthBytesInvalid
from pyrogram.file_id import (
    FILE_REFERENCE_FLAG,
    PHOTO_TYPES,
    WEB_LOCATION_FLAG,
    FileType,
    ThumbnailSource,
    b64_decode,
    rle_decode,
)
from pyrogram.mime_types import mime_types
from pyrogram.session import Auth, Session

from module.app import (
    Application,
    CloudDriveUploadStat,
    DownloadStatus,
    ForwardStatus,
    TaskNode,
    UploadProgressStat,
    UploadStatus,
)
from module.download_stat import get_download_result
from module.language import Language, _t
from module.send_media_group_v2 import cache_media, send_media_group_v2
from utils.format import (
    create_progress_bar,
    extract_info_from_link,
    format_byte,
    truncate_filename,
)
from utils.meta_data import MetaData

_mimetypes = MimeTypes()
_mimetypes.readfp(StringIO(mime_types))
_download_cache = Cache(1024 * 1024 * 1024)


def reset_download_cache():
    """Reset download cache"""
    _download_cache.store.clear()


def _guess_mime_type(filename: str) -> Optional[str]:
    """Guess mime type"""
    return _mimetypes.guess_type(filename)[0]


def _guess_extension(mime_type: str) -> Optional[str]:
    """Guess extension"""
    return _mimetypes.guess_extension(mime_type)


def get_utf16_length(text: str) -> int:
    """
    Returns the length of UTF-16 units for the string text.

    Notes:
      - Using 'utf-16-le' encoding (without BOM), dividing the number of bytes by 2 gives the number of UTF-16 units in the string.
      - This correctly counts both regular characters (1 unit) and emoji characters outside the BMP (2 units).
    """
    # After encoding to utf-16-le, every 2 bytes represent 1 UTF-16 unit
    return len(text.encode("utf-16-le")) // 2


def get_media_obj(
    message: pyrogram.types.Message,
    media: str = None,
    caption: str = None,
    caption_entities: List[pyrogram.types.MessageEntity] = None,
    parse_mode: Optional[enums.ParseMode] = None,
) -> Union[
    types.InputMediaPhoto,
    types.InputMediaVideo,
    types.InputMediaAudio,
    types.InputMediaDocument,
    types.InputMediaAnimation,
]:
    """Get media object"""
    media_type = message.media
    if media_type == pyrogram.enums.MessageMediaType.PHOTO:
        return types.InputMediaPhoto(
            media,
            caption=caption,
            caption_entities=caption_entities,
            parse_mode=parse_mode,
        )

    if media_type == pyrogram.enums.MessageMediaType.VIDEO:
        return types.InputMediaVideo(
            media,
            caption=caption,
            caption_entities=caption_entities,
            width=message.video.width,
            height=message.video.height,
            duration=message.video.duration,
            parse_mode=parse_mode,
        )

    if media_type in [
        pyrogram.enums.MessageMediaType.AUDIO,
        pyrogram.enums.MessageMediaType.VOICE,
    ]:
        return types.InputMediaAudio(
            media,
            caption=caption,
            caption_entities=caption_entities,
            parse_mode=parse_mode,
        )

    if media_type == pyrogram.enums.MessageMediaType.DOCUMENT:
        return types.InputMediaDocument(
            media,
            caption=caption,
            caption_entities=caption_entities,
            parse_mode=parse_mode,
        )

    if media_type == pyrogram.enums.MessageMediaType.ANIMATION:
        return types.InputMediaAnimation(
            media,
            caption=caption,
            caption_entities=caption_entities,
            parse_mode=parse_mode,
        )

    return None


def _get_file_type(file_id: str):
    """Get file type"""
    decoded = rle_decode(b64_decode(file_id))

    # File id versioning. Major versions lower than 4 don't have a minor version
    major = decoded[-1]

    if major < 4:
        buffer = BytesIO(decoded[:-1])
    else:
        buffer = BytesIO(decoded[:-2])

    file_type, _ = struct.unpack("<ii", buffer.read(8))

    file_type &= ~WEB_LOCATION_FLAG
    file_type &= ~FILE_REFERENCE_FLAG

    try:
        file_type = FileType(file_type)
    except ValueError as exc:
        raise ValueError(f"Unknown file_type {file_type} of file_id {file_id}") from exc

    return file_type


def get_extension(file_id: str, mime_type: str, dot: bool = True) -> str:
    """Get extension"""

    if not file_id:
        if dot:
            return ".unknown"
        return "unknown"

    file_type = _get_file_type(file_id)

    guessed_extension = _guess_extension(mime_type)

    if file_type in PHOTO_TYPES:
        extension = "jpg"
    elif file_type == FileType.VOICE:
        extension = guessed_extension or "ogg"
    elif file_type in (FileType.VIDEO, FileType.ANIMATION, FileType.VIDEO_NOTE):
        extension = guessed_extension or "mp4"
    elif file_type == FileType.DOCUMENT:
        extension = guessed_extension or "zip"
    elif file_type == FileType.STICKER:
        extension = guessed_extension or "webp"
    elif file_type == FileType.AUDIO:
        extension = guessed_extension or "mp3"
    else:
        extension = "unknown"

    if dot:
        extension = "." + extension
    return extension


async def send_message_by_language(
    client: pyrogram.client.Client,
    language: Language,
    chat_id: Union[int, str],
    reply_to_message_id: int,
    language_str: List[str],
):
    """Record download status"""
    msg = language_str[language.value - 1]

    return await client.send_message(
        chat_id, msg, reply_to_message_id=reply_to_message_id
    )


async def download_thumbnail(
    client: pyrogram.Client,
    temp_path: str,
    message: pyrogram.types.Message,
):
    """Downloads the thumbnail of a video message to a temporary file.

    Args:
        client: A Pyrogram client instance.
        temp_path: The path to a temporary directory where the thumbnail file
                   will be stored.
        message: A Pyrogram Message object representing the video message.

    Returns:
        A string representing the path of the thumbnail file, or None if the
        download failed.

    Raises:
        ValueError: If the downloaded thumbnail file size doesn't match the
                    expected file size.
    """
    thumbnail_file = None
    if message.video.thumbs:
        message = await fetch_message(client, message)
        thumbnail = message.video.thumbs[0] if message.video.thumbs else None
        unique_name = os.path.join(
            temp_path,
            "thumbnail",
            f"thumb-{int(time.time())}-{secrets.token_hex(8)}.jpg",
        )

        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                thumbnail_file = await client.download_media(
                    thumbnail, file_name=unique_name
                )

                if os.path.getsize(thumbnail_file) == thumbnail.file_size:
                    break

                raise ValueError(
                    f"Thumbnail file size is {os.path.getsize(thumbnail_file)}"
                    f" bytes, actual {thumbnail.file_size}: {thumbnail_file}"
                )

            except Exception as e:
                # 清理本次尝试留下的半成品文件，避免 temp 目录持续膨胀
                if os.path.exists(unique_name):
                    try:
                        os.remove(unique_name)
                    except OSError:
                        pass
                if attempt == max_attempts:
                    logger.exception(
                        f"Failed to download thumbnail after {max_attempts}"
                        f" attempts: {e}"
                    )
                else:
                    message = await fetch_message(client, message)
                    logger.warning(
                        f"Attempt {attempt} to download thumbnail failed: {e}"
                    )
                    # Wait 2 seconds before retrying
                    await asyncio.sleep(2)

                thumbnail_file = None
                # 重新从（可能已刷新的）消息取缩略图；置 None 会让下一次
                # download_media(None) 直接失败，重试形同虚设
                thumbnail = (
                    message.video.thumbs[0]
                    if getattr(message, "video", None) and message.video.thumbs
                    else None
                )
    return thumbnail_file


async def upload_telegram_chat(
    client: pyrogram.Client,
    upload_user: pyrogram.Client,
    app: Application,
    node: TaskNode,
    message: pyrogram.types.Message,
    download_status: DownloadStatus,
    file_name: str = None,
):
    """Upload telegram chat"""
    # upload telegram
    if node.upload_telegram_chat_id:
        if download_status is DownloadStatus.SkipDownload and message.media:
            if message.media_group_id:
                await proc_cache_forward(client, node, message, True, app)
            return

        if download_status is DownloadStatus.SuccessDownload or (
            download_status is DownloadStatus.SkipDownload and not message.media
        ):
            forward_status = None
            try:
                forward_status = await upload_telegram_chat_message(
                    client,
                    upload_user,
                    app,
                    node,
                    message,
                    file_name,
                )
            except Exception as e:
                logger.exception(f"Upload file {file_name} error: {e}")
            finally:
                # 仅在确认非失败时删除本地文件；上传失败/异常时保留文件供重试，
                # 避免"下载成功但一次转发失败"导致已下载内容丢失
                if (
                    file_name
                    and app.after_upload_telegram_delete
                    and forward_status is not None
                    and forward_status is not ForwardStatus.FailedForward
                ):
                    try:
                        os.remove(file_name)
                    except OSError as e:
                        logger.warning(f"Remove {file_name} failed: {e}")

            # forward text
            # FIXME: fix upload text
            # if (
            #     download_status is DownloadStatus.SkipDownload
            #     and message.text
            #     and bot
            # ):
            #     await upload_telegram_chat(
            #         client, app, node.upload_telegram_chat_id, message, file_name
            #     )


async def upload_telegram_chat_message(
    client: pyrogram.Client,
    upload_user: pyrogram.Client,
    app: Application,
    node: TaskNode,
    message: pyrogram.types.Message,
    file_name: str = None,
) -> ForwardStatus:
    """See upload telegram_chat"""
    forward_status = ForwardStatus.FailedForward
    max_attempts = 3
    for _ in range(1, max_attempts + 1):
        try:
            forward_status = await _upload_telegram_chat_message(
                client, upload_user, app, node, message, file_name
            )
            break
        except pyrogram.errors.exceptions.flood_420.FloodWait as wait_err:
            await asyncio.sleep(wait_err.value * 2)
            logger.warning(
                "Upload Message[{}]: FlowWait {}", message.id, wait_err.value
            )
        except Exception as e:
            logger.exception(f"Upload file {file_name} error: {e}")
            return ForwardStatus.FailedForward

    if forward_status != ForwardStatus.CacheForward:
        node.stat_forward(forward_status)
        # 记录转发到 Telegram 频道的时间（用于 Web 日志列表「频道上传时间」列）
        if forward_status is ForwardStatus.SuccessForward and message.id:
            import utils.db as _db
            _db.record_upload_time(node.chat_id, message.id)
    return forward_status


# pylint: disable=R0912
async def _upload_signal_message(
    client: pyrogram.Client,
    upload_user: pyrogram.Client,
    app: Application,
    node: TaskNode,
    upload_telegram_chat_id: Union[int, str, None],
    message: pyrogram.types.Message,
    file_name: Optional[str],
    caption: Optional[str] = None,
    text: Optional[str] = None,
):
    """
    Uploads a video or message to a Telegram chat.

    Parameters:
        client (pyrogram.Client): The pyrogram client.
        upload_telegram_chat_id (Union[int, str]): The ID of the chat to upload to.
        message (pyrogram.types.Message): The message to upload.
        file_name (str): The name of the file to upload.
    """
    ui_file_name = file_name
    if file_name:
        ui_file_name = (
            f"****{os.path.splitext(file_name)[-1]}"
            if app.hide_file_name
            else file_name
        )

    if message.video:
        # Download thumbnail
        thumbnail_file = await download_thumbnail(client, app.temp_save_path, message)
        try:
            # TODO(tangyoha): add more log when upload video more than 2000MB failed
            # Send video to the destination chat
            if node.reply_to_message:
                await node.reply_to_message.reply_video(
                    file_name,
                    caption=caption,
                    message_thread_id=node.topic_id,
                    thumb=thumbnail_file,
                    width=message.video.width,
                    height=message.video.height,
                    duration=message.video.duration,
                    parse_mode=pyrogram.enums.ParseMode.HTML,
                )
            else:
                await upload_user.send_video(
                    upload_telegram_chat_id,
                    file_name,
                    thumb=thumbnail_file,
                    width=message.video.width,
                    height=message.video.height,
                    duration=message.video.duration,
                    caption=caption,
                    parse_mode=pyrogram.enums.ParseMode.HTML,
                    progress=update_upload_stat,
                    progress_args=(
                        message.id,
                        ui_file_name,
                        time.time(),
                        node,
                        upload_user,
                    ),
                    message_thread_id=node.topic_id,
                )
        except Exception as e:
            raise e
        finally:
            if thumbnail_file:
                os.remove(str(thumbnail_file))

    elif message.photo:
        if node.reply_to_message:
            await node.reply_to_message.reply_photo(
                file_name,
                caption=caption,
                message_thread_id=node.topic_id,
            )
        else:
            await upload_user.send_photo(
                upload_telegram_chat_id,
                file_name,
                caption=caption,
                progress=update_upload_stat,
                progress_args=(
                    message.id,
                    ui_file_name,
                    time.time(),
                    node,
                    upload_user,
                ),
                message_thread_id=node.topic_id,
            )
    elif message.document:
        if node.reply_to_message:
            await node.reply_to_message.reply_document(
                file_name,
                caption=caption,
                message_thread_id=node.topic_id,
            )
        else:
            await upload_user.send_document(
                upload_telegram_chat_id,
                file_name,
                caption=caption,
                progress=update_upload_stat,
                progress_args=(
                    message.id,
                    ui_file_name,
                    time.time(),
                    node,
                    upload_user,
                ),
                message_thread_id=node.topic_id,
            )
    elif message.voice:
        if node.reply_to_message:
            await node.reply_to_message.reply_voice(
                file_name,
                caption=caption,
                message_thread_id=node.topic_id,
            )
        else:
            await upload_user.send_voice(
                upload_telegram_chat_id,
                file_name,
                caption=caption,
                progress=update_upload_stat,
                progress_args=(
                    message.id,
                    ui_file_name,
                    time.time(),
                    node,
                    upload_user,
                ),
                message_thread_id=node.topic_id,
            )
    elif message.video_note:
        if node.reply_to_message:
            await node.reply_to_message.reply_video_note(
                file_name,
                caption=caption,
                message_thread_id=node.topic_id,
            )
        else:
            await upload_user.send_video_note(
                upload_telegram_chat_id,
                file_name,
                caption=caption,
                progress=update_upload_stat,
                progress_args=(
                    message.id,
                    ui_file_name,
                    time.time(),
                    node,
                    upload_user,
                ),
                message_thread_id=node.topic_id,
            )
    elif message.text:
        if node.reply_to_message:
            await node.reply_to_message.reply(
                message.text if text is None else text, message_thread_id=node.topic_id
            )
        else:
            await upload_user.send_message(
                upload_telegram_chat_id,
                message.text if text is None else text,
                message_thread_id=node.topic_id,
            )


def truncate_caption(
    text: str,
    entities: Optional[List[pyrogram.raw.base.MessageEntity]] = None,
    limit: int = 1024,
) -> Tuple[str, Optional[List[pyrogram.types.MessageEntity]]]:
    """
    Truncate caption to ensure it doesn't exceed Telegram limits

    Args:
        text: Original text
        entities: List of text entities
        limit: UTF-16 encoding unit limit (default 1024)

    Returns:
        Tuple[str, Optional[List[pyrogram.raw.types.MessageEntity]]]: Truncated text and corresponding entity list
    """
    if not text:
        return text, entities

    # Calculate UTF-16 length
    utf16_length = get_utf16_length(text)

    if utf16_length <= limit:
        return text, entities

    # If exceeds limit, need to truncate
    # Use binary search to find suitable truncation position
    left, right = 0, len(text)
    while left < right:
        mid = (left + right + 1) // 2
        if get_utf16_length(text[:mid]) <= limit:
            left = mid
        else:
            right = mid - 1

    truncated_text = text[:left]

    # If there are entities, need to adjust entity list
    if entities:
        truncated_entities = []
        for entity in entities:
            if entity.offset >= left:
                continue
            if entity.offset + entity.length <= left:
                truncated_entities.append(entity)
            else:
                # For entities that cross the truncation point, adjust length
                new_entity = deepcopy(entity)
                new_entity.length = left - entity.offset
                truncated_entities.append(new_entity)
        return truncated_text, truncated_entities

    return truncated_text, None


async def process_caption(
    client,
    app,
    upload_telegram_chat_id,
    caption: str,
    caption_entities: Optional[List[pyrogram.types.MessageEntity]],
):
    """
    Process message caption: Use plain text without formatting for ad filtering and synchronously update caption_entities.
    After removing matched ad text, remove or adjust corresponding MessageEntity objects.

    Args:
        client: Pyrogram client instance
        app: Application object containing replace_advertisement_list property
            caption: Original caption text
            caption_entities: List of MessageEntity objects

        Returns:
        str: Cleaned caption
    """
    if not caption:
        return None

    update_caption = caption
    if caption and caption_entities:
        update_caption = pyrogram.parser.Parser.unparse(caption, caption_entities, True)

    for ad_text in app.replace_advertisement_list:
        update_caption = update_caption.replace(ad_text, "")

    advertisement = app.group_add_advertisement.get(upload_telegram_chat_id, "")

    ad_length = get_utf16_length(f"\n{advertisement}" if advertisement else "")

    max_caption_length = 4096 if client.me and client.me.is_premium else 1024
    available_length = max_caption_length - ad_length

    try:
        new_caption, new_entities = await convect_caption_entities(
            client, update_caption
        )
    except Exception as e:
        logger.exception(f"Error parsing caption: {e}")
        new_caption = update_caption
        new_entities = None

    truncated_caption, truncated_entities = truncate_caption(
        new_caption, new_entities, available_length
    )

    if advertisement:
        truncated_caption += f"\n{advertisement}"

    try:
        if truncated_entities:
            truncated_entities = convert_entities(truncated_entities)
            return pyrogram.parser.Parser.unparse(
                truncated_caption, truncated_entities, True
            )
    except Exception as e:
        logger.exception(f"Error unparsing caption: {e}")
        return truncated_caption

    return truncated_caption


def convert_message_entity(client, entity: "pyrogram.raw.base.MessageEntity") -> Optional["pyrogram.types.MessageEntity"]:
    # Special case for InputMessageEntityMentionName -> MessageEntityType.TEXT_MENTION
    # This happens in case of UpdateShortSentMessage inside send_message() where entities are parsed from the input
    if isinstance(entity, pyrogram.raw.types.InputMessageEntityMentionName):
        entity_type = enums.MessageEntityType.TEXT_MENTION
        user_id = entity.user_id.user_id
    else:
        entity_type = enums.MessageEntityType(entity.__class__)
        user_id = getattr(entity, "user_id", None)

    return pyrogram.types.MessageEntity(
        type=entity_type,
        offset=entity.offset,
        length=entity.length,
        url=getattr(entity, "url", None),
        user=types.User(id=user_id),
        language=getattr(entity, "language", None),
        custom_emoji_id=getattr(entity, "document_id", None),
        expandable=getattr(entity, "collapsed", None),
        client=client
    )

def convert_entities(
    entities: List[pyrogram.raw.base.MessageEntity],
) -> List[pyrogram.types.MessageEntity]:
    """Convert raw message entities to types message entities"""
    if not entities:
        return []

    try:
        return [
            convert_message_entity(None, entity) for entity in entities
        ]
    except Exception as e:
        logger.warning(f"Failed to convert entities: {e}")
        return []


async def convect_caption_entities(client, text):
    # Convert back to entities format
    try:
        return (await client.parser.parse(text, None)).values()
    except Exception as e:
        print(f"Error parsing markdown: {e}")
        # If parsing fails, return cleaned text without entities
        return text, None


async def _upload_telegram_chat_message(
    client: pyrogram.Client,
    upload_user: pyrogram.Client,
    app: Application,
    node: TaskNode,
    message: pyrogram.types.Message,
    file_name: str = None,
):
    """
    Uploads a Telegram chat message to the destination chat.

    Args:
        client (pyrogram.Client): The client used to interact with the Telegram API.
        upload_user (pyrogram.Client): The client used to upload the message.
        app (Application): The application instance.
        node (TaskNode): The task node associated with the message.
        message (pyrogram.types.Message): The Telegram chat message to be uploaded.
        file_name (str): The name of the file to be uploaded.

    Returns:
        None
    """
    await app.forward_limit_call.wait(node)

    caption = await process_caption(
        client,
        app,
        node.upload_telegram_chat_id,
        message.caption,
        message.caption_entities,
    )

    new_text = None
    # proc only text
    if not message.media and message.text:
        new_text = await process_caption(
            client, app, node.upload_telegram_chat_id, message.text, message.entities
        )

    if message.caption and message.media_group_id:
        app.set_caption_name(node.chat_id, message.media_group_id, message.caption)
        app.set_caption_entities(
            node.chat_id, message.media_group_id, message.caption_entities
        )

    if not message.media_group_id:
        if not node.has_protected_content:
            if node.reply_to_message:
                if message.text:
                    await node.reply_to_message.reply(
                        message.text,
                        message_thread_id=node.topic_id,
                    )
                elif message.photo:
                    await node.reply_to_message.reply_photo(
                        message.photo.file_id,
                        caption=caption,
                        message_thread_id=node.topic_id,
                    )
                elif message.video:
                    await node.reply_to_message.reply_video(
                        message.video.file_id,
                        caption=caption,
                        message_thread_id=node.topic_id,
                    )
                elif message.document:
                    await node.reply_to_message.reply_document(
                        message.document.file_id,
                        caption=caption,
                        message_thread_id=node.topic_id,
                    )
                elif message.audio:
                    await node.reply_to_message.reply_audio(
                        message.audio.file_id,
                        caption=caption,
                        message_thread_id=node.topic_id,
                    )
            else:
                if new_text:
                    await client.send_message(
                        node.upload_telegram_chat_id,
                        new_text,
                        parse_mode=enums.ParseMode.HTML,
                    )
                else:
                    await message.copy(
                        node.upload_telegram_chat_id,
                        caption=caption,
                        parse_mode=enums.ParseMode.HTML,
                    )
        else:
            await _upload_signal_message(
                client,
                upload_user,
                app,
                node,
                node.upload_telegram_chat_id,
                message,
                file_name,
                caption,
                new_text,
            )
        return ForwardStatus.SuccessForward

    return await forward_multi_media(
        client, upload_user, app, node, message, caption, file_name
    )


# pylint: disable=R0912
async def forward_multi_media(
    client: pyrogram.Client,
    _: pyrogram.Client,
    app: Application,
    node: TaskNode,
    message: pyrogram.types.Message,
    caption: Optional[str] = None,
    file_name: Optional[str] = None,
):
    """Forward multi media by cache"""
    media_obj = get_media_obj(
        message, file_name, caption
    )  # , parse_mode=enums.ParseMode.HTML)
    if not node.has_protected_content:
        media = getattr(message, message.media.value)
        if not media:
            return ForwardStatus.SkipForward
        media_obj.media = media.file_id if media else ""

    need_upload = False
    async with node.media_group_ids_lock:
        if not node.media_group_ids.get(message.media_group_id):
            node.media_group_ids[message.media_group_id] = {}

        if not node.media_group_ids[message.media_group_id]:
            media_group = await get_media_group_with_retry(
                client, node.chat_id, message.id, 5
            )
            if not media_group:
                logger.error("Get Media Group Error! message id: {}", message.id)
                return ForwardStatus.FailedForward

            for it in media_group:
                node.media_group_ids[message.media_group_id][it.id] = None
                node.upload_status[message.id] = None

        if not node.media_group_ids[message.media_group_id][message.id]:
            node.upload_status[message.id] = UploadStatus.Uploading
            need_upload = True

    _media = None
    if need_upload:
        try:
            ui_file_name = file_name
            if file_name:
                ui_file_name = (
                    f"****{os.path.splitext(file_name)[-1]}"
                    if app.hide_file_name
                    else file_name
                )
                media_obj.thumb = (
                    await download_thumbnail(client, app.temp_save_path, message)
                    if message.video
                    else None
                )

            _media = await cache_media(
                client,
                node.upload_telegram_chat_id,  # type: ignore
                media_obj,
                progress=update_upload_stat,
                progress_args=(
                    message.id,
                    ui_file_name,
                    time.time(),
                    node,
                    client,
                ),
            )
        except Exception as e:
            logger.exception(f"{e}")
        finally:
            if file_name and message.video and media_obj.thumb:
                os.remove(str(media_obj.thumb))

        async with node.media_group_ids_lock:
            if not _media:
                node.upload_status[message.id] = UploadStatus.FailedUpload
                return ForwardStatus.FailedForward

            node.media_group_ids[message.media_group_id][message.id] = _media
            node.upload_status[message.id] = UploadStatus.SuccessUpload

    return await proc_cache_forward(client, node, message, bool(file_name), app)


async def proc_cache_forward(
    client: pyrogram.Client,
    node: TaskNode,
    message: pyrogram.types.Message,
    check_download_status: bool,
    app: Application,
):
    """Process other cache forward"""
    multi_media: List[pyrogram.raw.types.InputSingleMedia] = []

    async with node.media_group_ids_lock:
        # Check if the message's media group is valid
        media_group = node.media_group_ids.get(message.media_group_id)
        if not media_group:
            return

        # Check if all items are in a valid state for forwarding
        for key, media_item in media_group.items():
            download_status = node.download_status.get(key, DownloadStatus.Downloading)
            upload_status = node.upload_status.get(key, UploadStatus.Uploading)

            # Skip if download is not needed or failed
            if node.skip_msg_id(key) or download_status in {
                DownloadStatus.SkipDownload,
                DownloadStatus.FailedDownload,
            }:
                continue

            # SkipUpload 是终态（bot 广告/过滤命中有意跳过，永不变化），
            # 与下载跳过一致：排除该成员继续组发送。当成等待条件会让
            # 整组 return CacheForward 永久卡死（media_group_ids 也不释放）
            if upload_status == UploadStatus.SkipUpload:
                continue

            # Return if any media is still downloading or uploading
            if (
                check_download_status
                and download_status == DownloadStatus.Downloading
            ) or upload_status == UploadStatus.Uploading:
                return ForwardStatus.CacheForward

            # Collect the media items that are valid for forwarding
            if media_item:
                multi_media.append(media_item)

        if len(multi_media) > 1:
            caption_item = None
            for item in multi_media:
                if item.message:
                    caption_item = item
                    break
            if caption_item:
                for item in multi_media:
                    if item is not caption_item:
                        item.message = ""
                        item.entities = None

        node.media_group_ids.pop(message.media_group_id)

    forward_status = ForwardStatus.SuccessForward

    reply_to_message_id = None
    message_thread_id = node.topic_id
    business_connection_id = None
    upload_telegram_chat_id = node.upload_telegram_chat_id
    if node.reply_to_message:
        if node.reply_to_message.chat.type != pyrogram.enums.ChatType.PRIVATE:
            reply_to_message_id = node.reply_to_message.id
        message_thread_id = node.reply_to_message.message_thread_id
        business_connection_id = node.reply_to_message.business_connection_id
        upload_telegram_chat_id = node.reply_to_message.chat.id
    if not multi_media:
        # 组内成员全部被过滤/跳过：无可发送内容，释放组并按跳过处理，
        # 避免对 SendMultiMedia 传空列表必然 400
        logger.warning(
            f"Media group {message.media_group_id} has no media left to forward, skip"
        )
        forward_status = ForwardStatus.SkipForward
    elif len(multi_media) == 1:
        # Telegram 媒体组要求 2-10 个元素：只剩 1 个时降级为单条 SendMedia，
        # 否则 SendMultiMedia 必 400 且组已 pop 无重试
        single = multi_media[0]
        peer = await client.resolve_peer(upload_telegram_chat_id)
        try:
            await client.invoke(
                pyrogram.raw.functions.messages.SendMedia(
                    peer=peer,
                    media=single.media,
                    random_id=single.random_id,
                    message=single.message,
                    entities=single.entities,
                    reply_to=utils.get_reply_to(
                        reply_to_message_id=reply_to_message_id,
                        message_thread_id=message_thread_id,
                    ),
                ),
                sleep_threshold=60,
            )
        except Exception as e:
            logger.exception(f"Send single media from group error: {e}")
            forward_status = ForwardStatus.FailedForward
    elif not await send_media_group_v2(
        client,
        upload_telegram_chat_id,  # type: ignore
        multi_media,
        message_thread_id=message_thread_id,
        reply_to_message_id=reply_to_message_id,
    ):
        forward_status = ForwardStatus.FailedForward

    node.stat_forward(forward_status, len(multi_media))

    return ForwardStatus.CacheForward


def record_download_status(func):
    """Record download status"""

    @wraps(func)
    async def inner(
        client: pyrogram.client.Client,
        message: pyrogram.types.Message,
        media_types: List[str],
        file_formats: dict,
        node: TaskNode,
    ):
        key = (node.chat_id, message.id)
        if _download_cache[key] is DownloadStatus.Downloading:
            return DownloadStatus.Downloading, None

        _download_cache[key] = DownloadStatus.Downloading
        try:
            status, file_name = await func(client, message, media_types, file_formats, node)
            return status, file_name
        finally:
            # 终态后置 None（读取方等价于缺失）：缓存的并发去重仅在
            # Downloading 期间生效，非 Downloading 一律放行，行为不变；
            # 同时修复原 finally 把成功下载也写成 FailedDownload 的语义问题，
            # 并避免终态残留误导后续"已下载"判断。
            # 注意：_download_cache 是 pyrogram 的 Cache 对象，无 .get()/
            # __delitem__，须用下标访问，置 None 等价清空
            _download_cache[key] = None

    return inner


async def report_bot_download_status(
    client: pyrogram.Client,
    node: TaskNode,
    download_status: DownloadStatus,
    download_size: int = 0,
):
    """
    Sends a message with the current status of the download bot.

    Parameters:
        client (pyrogram.Client): The client instance.
        node (TaskNode): The download task node.
        download_status (DownloadStatus): The current download status.

    Returns:
        None
    """
    node.stat(download_status)
    node.total_download_byte += download_size
    await report_bot_status(client, node)


async def report_bot_forward_status(
    client: pyrogram.Client,
    node: TaskNode,
    status: ForwardStatus,
):
    """
    Sends a message with the current status of the download bot.

    Parameters:
        client (pyrogram.Client): The client instance.
        node (TaskNode): The download task node.
        status (ForwardStatus): The current forward status.

    Returns:
        None
    """
    node.stat_forward(status)
    await report_bot_status(client, node)


async def report_bot_status(
    client: pyrogram.Client,
    node: TaskNode,
    immediate_reply=False,
):
    """see _report_bot_status"""
    try:
        return await _report_bot_status(client, node, immediate_reply)
    except Exception as e:
        logger.debug(f"{e}")


async def _report_bot_status(
    client: pyrogram.Client,
    node: TaskNode,
    immediate_reply=False,
):
    """
    Sends a message with the current status of the download bot.

    Parameters:
        client (pyrogram.Client): The client instance.
        node (TaskNode): The download task node.
        immediate_reply(bool): Immediate reply

    Returns:
        None
    """
    if not node.reply_message_id or not node.bot:
        return

    if immediate_reply or node.can_reply():
        if node.upload_telegram_chat_id:
            node.forward_msg_detail_str = (
                f"\n🔄 {_t('Forward')}\n"
                f"├─ 📁 {_t('Total')}: {node.total_forward_task}\n"
                f"├─ ✅ {_t('Success')}: {node.success_forward_task}\n"
                f"├─ ❌ {_t('Failed')}: {node.failed_forward_task}\n"
                f"└─ ⏩ {_t('Skipped')}: {node.skip_forward_task}\n"
            )

        upload_msg_detail_str: str = ""

        if node.upload_success_count:
            upload_msg_detail_str = (
                f"\n☁️ {_t('Upload')}\n"
                f"└─ ✅ {_t('Success')}: {node.upload_success_count}\n"
            )

        for idx, value in node.cloud_drive_upload_stat_dict.items():
            if value.transferred == value.total:
                continue

            temp_file_name = truncate_filename(os.path.basename(value.file_name), 10)
            # rclone 进度常为小数（如 "12.3%"），int("12.3") 抛 ValueError
            # 会让整条 bot 状态消息静默不再更新
            try:
                upload_percentage = int(float(value.percentage.rstrip("%")))
            except (TypeError, ValueError):
                upload_percentage = 0
            upload_msg_detail_str += (
                f" ├─ 🆔 {_t('Message ID')}: {idx}\n"
                f" │   ├─ 📁 : {temp_file_name}\n"
                f" │   ├─ 📏 : {value.total}\n"
                f" │   ├─ ⏫ : {value.speed}\n"
                f" │   └─ 📊 : ["
                f"{create_progress_bar(upload_percentage)}]"
                f" ({value.percentage})%\n"
            )

        download_result_str = ""
        download_result = get_download_result()
        if node.chat_id in download_result:
            messages = download_result[node.chat_id]
            for idx, value in messages.items():
                task_id = value["task_id"]
                if task_id != node.task_id or value["down_byte"] == value["total_size"]:
                    continue

                temp_file_name = truncate_filename(
                    os.path.basename(value["file_name"]), 10
                )
                progress = int(value["down_byte"] / value["total_size"] * 100)
                download_result_str += (
                    f" ├─ 🆔 {_t('Message ID')}: {idx}\n"
                    f" │   ├─ 📁 : {temp_file_name}\n"
                    f" │   ├─ 📏 : {format_byte(value['total_size'])}\n"
                    f" │   ├─ ⏬ : {format_byte(value['download_speed'])}/s\n"
                    f" │   └─ 📊 : [{create_progress_bar(progress)}]"
                    f" ({progress}%)\n"
                )

            if download_result_str:
                download_result_str = (
                    f"\n📥 {_t('Download Progresses')}:\n" + download_result_str
                )

        upload_result_str = ""
        for idx, value in node.upload_stat_dict.items():
            if value.total_size == value.upload_size:
                continue

            temp_file_name = truncate_filename(os.path.basename(value.file_name), 10)
            progress = int(value.upload_size / value.total_size * 100)
            upload_result_str += (
                f" ├─ 🆔 {_t('Message ID')}: {idx}\n"
                f" │   ├─ 📁 : {temp_file_name}\n"
                f" │   ├─ 📏 : {format_byte(value.total_size)}\n"
                f" │   ├─ ⏫ : {format_byte(value.upload_speed)}/s\n"
                f" │   └─ 📊 : [{create_progress_bar(progress)}]"
                f" ({progress}%)\n"
            )

        if upload_result_str:
            upload_result_str = f"\n📤 {_t('Upload Progresses')}:\n" + upload_result_str

        new_msg_str = (
            f"`\n"
            f"🆔 task id: {node.task_id}\n"
            f"📥 {_t('Downloading')}: {format_byte(node.total_download_byte)}\n"
            f"├─ 📁 {_t('Total')}: {node.total_download_task}\n"
            f"├─ ✅ {_t('Success')}: {node.success_download_task}\n"
            f"├─ ❌ {_t('Failed')}: {node.failed_download_task}\n"
            f"└─ ⏩ {_t('Skipped')}: {node.skip_download_task}\n"
            f"{node.forward_msg_detail_str}"
            f"{upload_msg_detail_str}"
            f"{upload_result_str}"
            f"{download_result_str}\n`"
        )

        if new_msg_str != node.last_edit_msg:
            node.last_edit_msg = new_msg_str
            await client.edit_message_text(
                node.from_user_id,
                node.reply_message_id,
                new_msg_str,
                parse_mode=pyrogram.enums.ParseMode.MARKDOWN,
            )


def set_max_concurrent_transmissions(
    client: pyrogram.Client, max_concurrent_transmissions: int
):
    """Set maximum concurrent transmissions"""
    if getattr(client, "max_concurrent_transmissions", None):
        client.max_concurrent_transmissions = max_concurrent_transmissions
        client.save_file_semaphore = asyncio.Semaphore(
            client.max_concurrent_transmissions
        )
        client.get_file_semaphore = asyncio.Semaphore(
            client.max_concurrent_transmissions
        )


# ---------------------------------------------------------------------------
# 并行分块下载
#
# 背景：pyrogram 的 download_media 对单个文件是"串行 1MB/请求"拉取
# （client.get_file 内 while 循环顺序 invoke upload.GetFile），单文件速度
# 受每请求往返延迟限制（跨区 DC 常见 ~1.5-2MB/s），多文件并发是唯一的提速手段。
#
# 这里的实现按 FastTelethon 思路：对大文件建立多条到文件所在 DC 的会话，
# 各自并发拉取不同 offset 的 1MB 分块，os.pwrite 写入对应位置。
# 任何失败（CDN 重定向/鉴权/网络/FloodWait 超阈值）整体回退到
# client.download_media 顺序下载，保证正确性不受影响。
# ---------------------------------------------------------------------------

_DOWNLOAD_CHUNK_SIZE = 1024 * 1024
# 小文件往返次数少，并行收益低还多建连接，直接走顺序下载
_PARALLEL_MIN_SIZE = 20 * 1024 * 1024


def _build_file_location(file_id):
    """按 client.get_file 的逻辑构造文件下载 location（仅覆盖常用类型）。

    返回 None 表示类型不支持并行路径，调用方应回退顺序下载。
    """
    file_type = file_id.file_type

    if file_type == FileType.PHOTO:
        return pyrogram.raw.types.InputPhotoFileLocation(
            id=file_id.media_id,
            access_hash=file_id.access_hash,
            file_reference=file_id.file_reference,
            thumb_size=file_id.thumbnail_size,
        )
    if file_type == FileType.CHAT_PHOTO:
        if file_id.chat_id > 0:
            peer = pyrogram.raw.types.InputPeerUser(
                user_id=file_id.chat_id,
                access_hash=file_id.chat_access_hash,
            )
        else:
            if file_id.chat_access_hash == 0:
                peer = pyrogram.raw.types.InputPeerChat(
                    chat_id=-file_id.chat_id
                )
            else:
                peer = pyrogram.raw.types.InputPeerChannel(
                    channel_id=utils.get_channel_id(file_id.chat_id),
                    access_hash=file_id.chat_access_hash,
                )
        return pyrogram.raw.types.InputPeerPhotoFileLocation(
            peer=peer,
            photo_id=file_id.media_id,
            big=file_id.thumbnail_source == ThumbnailSource.CHAT_PHOTO_BIG,
        )
    if file_type in (
        FileType.VIDEO,
        FileType.ANIMATION,
        FileType.VIDEO_NOTE,
        FileType.AUDIO,
        FileType.VOICE,
        FileType.DOCUMENT,
        FileType.STICKER,
    ):
        return pyrogram.raw.types.InputDocumentFileLocation(
            id=file_id.media_id,
            access_hash=file_id.access_hash,
            file_reference=file_id.file_reference,
            thumb_size=file_id.thumbnail_size,
        )
    return None


async def _create_download_session(
    client: pyrogram.Client, dc_id: int
) -> Session:
    """新建一条到目标 DC 的媒体会话（不复用 client.media_sessions 缓存）。

    镜像 pyrogram/methods/messages/inline_session.py 的建会话流程：
    远端 DC 时创建新 auth key 并导入用户授权，否则直接复用主 auth key。
    """
    test_mode = await client.storage.test_mode()
    if dc_id == await client.storage.dc_id():
        auth_key = await client.storage.auth_key()
    else:
        auth_key = await Auth(client, dc_id, test_mode).create()

    session = Session(client, dc_id, auth_key, test_mode, is_media=True)
    await session.start()

    if dc_id != await client.storage.dc_id():
        for _ in range(3):
            exported_auth = await client.invoke(
                pyrogram.raw.functions.auth.ExportAuthorization(dc_id=dc_id)
            )
            try:
                await session.invoke(
                    pyrogram.raw.functions.auth.ImportAuthorization(
                        id=exported_auth.id, bytes=exported_auth.bytes
                    )
                )
            except AuthBytesInvalid:
                continue
            else:
                break
        else:
            await session.stop()
            raise AuthBytesInvalid

    return session


async def parallel_download_media(
    client: pyrogram.Client,
    message: pyrogram.types.Message,
    file_size: int,
    file_name: str,
    chunk_concurrency: int,
    progress: Callable = None,
    progress_args: tuple = (),
) -> Optional[str]:
    """并行分块下载单个大文件。

    Parameters
    ----------
    file_size: 远端文件大小（<=0 或过小时调用方不应走此路径）
    file_name: 目标路径（语义与 client.download_media 的 file_name 一致：
               先写 "<file_name>.temp"，完成后改名并返回 file_name）
    chunk_concurrency: 单文件并行会话数

    Returns
    -------
    成功返回 file_name；任何失败返回 None（调用方回退顺序下载）。
    """
    # 会话 0 复用主媒体会话，其余新建
    sessions: list = []
    abort = asyncio.Event()
    done_bytes = 0
    fd = None
    temp_path = os.path.abspath(file_name) + ".temp"
    try:
        # message.media 可能是任意形态（如 bool），取值失败一律回退顺序下载
        media = getattr(message, getattr(message.media, "value", ""), None)
        file_id_str = getattr(media, "file_id", None)
        if not file_id_str or not file_size or file_size < _PARALLEL_MIN_SIZE:
            return None

        file_id = FileId.decode(file_id_str)
        location = _build_file_location(file_id)
        if location is None:
            return None

        dc_id = file_id.dc_id
        total_chunks = -(-file_size // _DOWNLOAD_CHUNK_SIZE)  # ceil
        chunk_concurrency = max(1, min(chunk_concurrency, total_chunks))

        async def _progress(done_bytes: int):
            if progress is None:
                return
            func = functools.partial(
                progress, min(done_bytes, file_size), file_size, *progress_args
            )
            if inspect.iscoroutinefunction(progress):
                await func()
            else:
                await client.loop.run_in_executor(client.executor, func)

        os.makedirs(os.path.dirname(temp_path), exist_ok=True)
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        os.ftruncate(fd, file_size)

        sessions.append(await get_session(client, dc_id))
        while len(sessions) < chunk_concurrency:
            sessions.append(await _create_download_session(client, dc_id))

        next_chunk = {"i": 0}
        lock = asyncio.Lock()

        async def worker(session: Session):
            nonlocal done_bytes
            while not abort.is_set():
                async with lock:
                    idx = next_chunk["i"]
                    next_chunk["i"] = idx + 1
                if idx >= total_chunks:
                    return
                offset = idx * _DOWNLOAD_CHUNK_SIZE
                try:
                    r = await session.invoke(
                        pyrogram.raw.functions.upload.GetFile(
                            location=location,
                            offset=offset,
                            limit=_DOWNLOAD_CHUNK_SIZE,
                        ),
                        sleep_threshold=10,
                    )
                except Exception:
                    # 该会话失败即放弃整体并行（回退顺序下载），不逐块重试
                    abort.set()
                    return
                if not isinstance(r, pyrogram.raw.types.upload.File):
                    # CDN 重定向等场景交给顺序路径处理
                    abort.set()
                    return
                chunk = r.bytes
                os.pwrite(fd, chunk, offset)
                done_bytes += len(chunk)
                await _progress(done_bytes)

        await asyncio.gather(*(worker(s) for s in sessions))

        if abort.is_set() or done_bytes < file_size:
            return None

        os.close(fd)
        fd = None
        os.replace(temp_path, os.path.abspath(file_name))
        logger.info(
            f"parallel download {file_name} done: {file_size} bytes "
            f"with {chunk_concurrency} connections"
        )
        return file_name
    except Exception as e:
        logger.warning(f"parallel download {file_name} failed, fallback: {e}")
        return None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        # 未成功 rename 的残留临时文件一律清理（成功路径 temp 已被 replace 走）
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        # 只清理本函数新建的会话（sessions[0] 是主媒体会话缓存，保留）
        for session in sessions[1:]:
            try:
                await session.stop()
            except Exception:
                pass


async def fetch_message(client: pyrogram.Client, message: pyrogram.types.Message):
    """
    This function retrieves a message from a specified chat using the Pyrogram library.
     Args:
        client (pyrogram.Client): A client instance created using Pyrogram.
        message (pyrogram.types.Message): A message instance returned from Pyrogram.
     Returns:
        pyrogram.types.Message: A message object retrieved from the specified chat.
    """
    return await client.get_messages(
        chat_id=message.chat.id,
        message_ids=message.id,
    )


async def retry(func: Callable, args: tuple = (), max_attempts=3, wait_second=15):
    """
    Asynchronously retries the provided function
    a specified number of times with a specified wait time between retries.

    :param func: The function to be retried.
    :param args: The arguments to be passed to the function.
    :param max_attempts: The maximum number of attempts to retry the function.
        Defaults to 3.
    :param wait_second: The wait time in seconds between each retry attempt.
        Defaults to 15.

    :return: The result of the function
    if it succeeds within the maximum number of attempts, otherwise None.
    """

    for _ in range(1, max_attempts + 1):
        try:
            return await func(*args)
        except pyrogram.errors.exceptions.flood_420.FloodWait as wait_err:
            logger.warning("bad call retry: FlowWait {}", wait_err.value)
            await asyncio.sleep(wait_err.value)
        except Exception as e:
            logger.exception("Error: {}", e)
            await asyncio.sleep(wait_second)

    logger.error("Failed after {} attempts", max_attempts)
    return None


async def get_media_group_with_retry(
    client: pyrogram.Client,
    chat_id: Union[int, str],
    message_id: int,
    max_attempts: int = 3,
    wait_second: int = 15,
):
    """
    get_media_group_with_retry
    """
    for attempt in range(1, max_attempts + 1):
        try:
            return await client.get_media_group(chat_id, message_id)
        except Exception as e:
            if attempt == max_attempts:
                logger.error("Failed Get Media Group[{}]", message_id)
                return types.List()

            logger.exception("Get Message[{}]: Error {}", message_id, e)
            await asyncio.sleep(wait_second)
    return types.List()


async def check_user_permission(
    client: pyrogram.Client, user_id: Union[int, str], chat_id: Union[int, str]
) -> bool:
    """
    Check if the user has permission to send videos in the group.

    Args:
        client (pyrogram.Client): A client instance created using Pyrogram.
        user_id (Union[int, str]): User Id
        chat_id (Union[int, str]): Chat Id

     Returns:
        if can_send_media_messages return True
    """
    try:
        member = await client.get_chat_member(chat_id, user_id)
        return member and (
            not member.permissions or member.permissions.can_send_media_messages
        )
    except Exception:
        # logger.exception(e)
        pass

    return False


def set_meta_data(
    meta_data: MetaData, message: pyrogram.types.Message, caption: str = None
):
    """Get all meta data"""
    # message
    meta_data.message_date = getattr(message, "date", None)
    if caption:
        meta_data.message_caption = caption
    else:
        meta_data.message_caption = getattr(message, "caption", None) or ""
    meta_data.message_id = getattr(message, "id", None)

    from_user = getattr(message, "from_user")
    meta_data.sender_id = from_user.id if from_user else 0
    meta_data.sender_name = (from_user.username if from_user else "") or ""
    meta_data.reply_to_message_id = getattr(
        message, "reply_to_message_id", 1
    )  # 1 for General

    meta_data.message_thread_id = getattr(message, "message_thread_id", 1)
    # media
    for kind in meta_data.AVAILABLE_MEDIA:
        media_obj = getattr(message, kind, None)
        if media_obj is not None:
            meta_data.media_type = kind
            break
    else:
        return
    meta_data.media_file_name = getattr(media_obj, "file_name", None) or ""
    meta_data.media_file_size = getattr(media_obj, "file_size", None)
    meta_data.media_width = getattr(media_obj, "width", None)
    meta_data.media_height = getattr(media_obj, "height", None)
    meta_data.media_duration = getattr(media_obj, "duration", None)
    meta_data.file_extension = get_extension(
        media_obj.file_id, getattr(media_obj, "mime_type", ""), False
    )


async def parse_link(client: pyrogram.Client, link_str: str):
    """Parse link"""
    link = extract_info_from_link(link_str)
    if link.comment_id:
        chat = await client.get_chat(link.group_id)
        if chat:
            return chat.linked_chat.id, link.comment_id, link.topic_id

    return link.group_id, link.post_id, link.topic_id


async def update_cloud_upload_stat(
    transferred: str,
    total: str,
    percentage: str,
    speed: str,
    eta: str,
    node: TaskNode,
    message_id: int,
    file_name: str,
):
    """
    Update the cloud upload statistics with the given information.

    Args:
        transferred (str): The amount of data transferred.
        total (str): The total size of the file.
        percentage (str): The percentage of the file uploaded.
        speed (str): The upload speed.
        eta (str): The estimated time of arrival for the upload to complete.
        node (TaskNode): The task node associated with the upload.
        message_id (int): The ID of the message.
        file_name (str): The name of the file being uploaded.

    Returns:
        None
    """
    node.cloud_drive_upload_stat_dict[message_id] = CloudDriveUploadStat(
        file_name=file_name,
        transferred=transferred,
        total=total,
        percentage=percentage,
        speed=speed,
        eta=eta,
    )


async def update_upload_stat(
    upload_size: int,
    total_size: int,
    message_id: int,
    file_name: str,
    start_time: float,
    node: TaskNode,
    client: pyrogram.Client,
):
    """update_upload_status"""
    cur_time = time.time()

    if node.is_stop_transmission:
        client.stop_transmission()

    # TODO(tyh): web control upload stop

    if node.upload_stat_dict.get(message_id):
        upload_stat = node.upload_stat_dict[message_id]

        if cur_time - upload_stat.last_stat_time >= 1.0:
            upload_stat.upload_speed = max(
                int(
                    (upload_size - upload_stat.upload_size)
                    / (cur_time - upload_stat.last_stat_time)
                ),
                0,
            )
            upload_stat.last_stat_time = cur_time
            upload_stat.upload_size = upload_size

        node.upload_stat_dict[message_id] = upload_stat
    else:
        duration = cur_time - start_time
        upload_stat = UploadProgressStat(
            file_name=file_name,
            total_size=total_size,
            upload_size=upload_size,
            start_time=start_time,
            last_stat_time=cur_time,
            upload_speed=upload_size / (duration if duration > 0 else 1),
        )
        node.upload_stat_dict[message_id] = upload_stat


# pylint: enable=W0201
class HookSession(pyrogram.session.Session):
    """Hook Session"""

    def start_timeout(self: pyrogram.session.Session, start_timeout: int):
        """
        Set the start timeout for the session.

        Args:
            start_timeout (int): The start timeout value in seconds.

        Returns:
            None
        """
        self.START_TIMEOUT = start_timeout


# pylint: disable=all
class HookClient(pyrogram.Client):
    """Hook Client"""

    # pylint: disable=R0901
    START_TIME_OUT = 60

    def __init__(self, name: str, **kwargs):
        if "start_timeout" in kwargs:
            value = kwargs.get("start_timeout")
            if value:
                self.START_TIME_OUT = value
            kwargs.pop("start_timeout")

        super().__init__(name, **kwargs)

    async def connect(
        self,
    ) -> bool:
        """
        Connects the client to the server.

        Returns:
            bool: True if the client successfully
                connects to the server, False otherwise.

        Raises:
            ConnectionError: If the client is already connected.

        """
        if self.is_connected:  # type: ignore
            raise ConnectionError("Client is already connected")

        await self.load_session()

        self.session = HookSession(
            self,
            await self.storage.dc_id(),
            await self.storage.auth_key(),
            await self.storage.test_mode(),
        )
        self.session.start_timeout(self.START_TIME_OUT)

        await self.session.start()

        self.is_connected = True

        return bool(await self.storage.user_id())

    async def start(self):
        """
        Starts the client by performing necessary initialization steps.

        Returns:
            The initialized client instance.
        """
        is_authorized = await self.connect()

        try:
            if not is_authorized:
                await self.authorize()

            if not await self.storage.is_bot() and self.takeout:
                self.takeout_id = (
                    await self.invoke(
                        pyrogram.raw.functions.account.InitTakeoutSession()
                    )
                ).id
                logger.warning(f"Takeout session {self.takeout_id} initiated")

            await self.invoke(pyrogram.raw.functions.updates.GetState())
        except (Exception, KeyboardInterrupt):
            await self.disconnect()
            raise
        else:
            self.me = await self.get_me()
            await self.initialize()

            return self


# pylint: disable=R0914,R0913
async def forward_messages(
    client: pyrogram.Client,
    chat_id: Union[int, str, None],
    from_chat_id: Union[int, str],
    message_ids: Union[int, Iterable[int]],
    disable_notification: bool = None,
    schedule_date: datetime = None,
    protect_content: bool = None,
    drop_author: bool = None,
    topic_id: int = None,
    caption: str = None,
    caption_entities: List[pyrogram.types.MessageEntity] = None,
) -> Union["types.Message", List["types.Message"]]:
    """Forward messages of any kind."""

    is_iterable = not isinstance(message_ids, int)
    message_ids = list(message_ids) if is_iterable else [message_ids]  # type: ignore

    r = await client.invoke(
        pyrogram.raw.functions.messages.ForwardMessages(
            to_peer=await client.resolve_peer(chat_id),
            from_peer=await client.resolve_peer(from_chat_id),
            id=message_ids,
            silent=disable_notification or None,
            random_id=[client.rnd_id() for _ in message_ids],
            schedule_date=pyrogram.utils.datetime_to_timestamp(schedule_date),
            noforwards=protect_content,
            drop_author=drop_author,
            top_msg_id=topic_id,
        )
    )

    forwarded_messages = []

    users = {i.id: i for i in r.users}
    chats = {i.id: i for i in r.chats}

    for i in r.updates:
        if isinstance(
            i,
            (
                pyrogram.raw.types.UpdateNewMessage,
                pyrogram.raw.types.UpdateNewChannelMessage,
                pyrogram.raw.types.UpdateNewScheduledMessage,
            ),
        ):
            forwarded_messages.append(
                # pylint: disable=W0212
                await types.Message._parse(client, i.message, users, chats)
            )

    if caption and not is_iterable and forwarded_messages:
        await client.edit_message_caption(
            chat_id,
            forwarded_messages[0].id,
            caption=caption,
            caption_entities=caption_entities,
        )

    return types.List(forwarded_messages) if is_iterable else forwarded_messages[0]
