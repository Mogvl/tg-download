"""Downloads media from telegram."""
import asyncio
import logging
import os
import shutil
import time
from typing import List, Optional, Tuple, Union

import pyrogram
from loguru import logger
from pyrogram.types import Audio, Document, Photo, Video, VideoNote, Voice
from rich.logging import RichHandler

from module.app import Application, ChatDownloadConfig, DownloadStatus, TaskNode
from module.bot import start_download_bot, stop_download_bot
from module.download_stat import update_download_status
from module.get_chat_history_v2 import get_chat_history_v2
from module.language import _t
from module.pyrogram_extension import (
    HookClient,
    fetch_message,
    get_extension,
    parallel_download_media,
    record_download_status,
    report_bot_download_status,
    set_max_concurrent_transmissions,
    set_meta_data,
    update_cloud_upload_stat,
    upload_telegram_chat,
)
from module.web import init_web
from utils.format import truncate_filename, validate_title
from utils.log import LogFilter
from utils.meta import print_meta
from utils.meta_data import MetaData
# 保留导入以兼容测试(mock.patch 目标)，实际不调用更新检查
from utils.updates import check_for_updates  # noqa: F401  pylint: disable=unused-import
import utils.db as db

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler()],
)

CONFIG_NAME = "config.yaml"
DATA_FILE_NAME = "data.yaml"
APPLICATION_NAME = "media_downloader"
app = Application(CONFIG_NAME, DATA_FILE_NAME, APPLICATION_NAME)

# 有界队列：生产者（分页扫描）远快于下载，无界队列会把整个频道的消息
# 堆进内存，且停机时 download_status/ids_to_retry 巨大、保存缓慢。
# 限长后生产者自然背压等待，worker 始终有活干；完成判定有 need_check
# 门控，不受入队节奏影响
queue: asyncio.Queue = asyncio.Queue(maxsize=max(app.max_download_task * 2, 20))
RETRY_TIME_OUT = 3

logging.getLogger("pyrogram.session.session").addFilter(LogFilter())
logging.getLogger("pyrogram.client").addFilter(LogFilter())

logging.getLogger("pyrogram").setLevel(logging.WARNING)


class _SizeMismatchError(Exception):
    """下载文件大小与远端不一致（用于区分于 BadRequest，避免误入引用过期重试）"""


def _check_download_finish(media_size: int, download_path: str, ui_file_name: str):
    """Check download task if finish

    Parameters
    ----------
    media_size: int
        The size of the downloaded resource
    download_path: str
        Resource download hold path
    ui_file_name: str
        Really show file name

    """
    download_size = os.path.getsize(download_path)
    if media_size == download_size:
        logger.success(f"{_t('Successfully downloaded')} - {ui_file_name}")
    else:
        logger.warning(
            f"{_t('Media downloaded with wrong size')}: "
            f"{download_size}, {_t('actual')}: "
            f"{media_size}, {_t('file name')}: {ui_file_name}"
        )
        os.remove(download_path)
        raise _SizeMismatchError()


def _move_to_download_path(temp_download_path: str, download_path: str):
    """Move file to download path

    Parameters
    ----------
    temp_download_path: str
        Temporary download path

    download_path: str
        Download path

    """

    directory, _ = os.path.split(download_path)
    os.makedirs(directory, exist_ok=True)
    shutil.move(temp_download_path, download_path)


def _check_timeout(retry: int, _: int):
    """Check if message download timeout, then add message id into failed_ids

    Parameters
    ----------
    retry: int
        Retry download message times

    message_id: int
        Try to download message 's id

    """
    if retry == 2:
        return True
    return False


def _can_download(_type: str, file_formats: dict, file_format: Optional[str]) -> bool:
    """
    Check if the given file format can be downloaded.

    Parameters
    ----------
    _type: str
        Type of media object.
    file_formats: dict
        Dictionary containing the list of file_formats
        to be downloaded for `audio`, `document` & `video`
        media types
    file_format: str
        Format of the current file to be downloaded.

    Returns
    -------
    bool
        True if the file format can be downloaded else False.
    """
    if _type in ["audio", "document", "video"]:
        # 用 get 避免 file_formats 缺对应键时 KeyError
        allowed_formats: list = file_formats.get(_type, [])
        if not allowed_formats:
            return False
        if not file_format in allowed_formats and allowed_formats[0] != "all":
            return False
    return True


def _is_exist(file_path: str) -> bool:
    """
    Check if a file exists and it is not a directory.

    Parameters
    ----------
    file_path: str
        Absolute path of the file to be checked.

    Returns
    -------
    bool
        True if the file exists else False.
    """
    return not os.path.isdir(file_path) and os.path.exists(file_path)


# pylint: disable = R0912


async def _get_media_meta(
    chat_id: Union[int, str],
    message: pyrogram.types.Message,
    media_obj: Union[Audio, Document, Photo, Video, VideoNote, Voice],
    _type: str,
) -> Tuple[str, str, Optional[str]]:
    """Extract file name and file id from media object.

    Parameters
    ----------
    media_obj: Union[Audio, Document, Photo, Video, VideoNote, Voice]
        Media object to be extracted.
    _type: str
        Type of media object.

    Returns
    -------
    Tuple[str, str, Optional[str]]
        file_name, file_format
    """
    if _type in ["audio", "document", "video"]:
        # mime_type 可能为 None（文档类消息），避免 split 崩溃
        file_format: Optional[str] = (media_obj.mime_type or "application/octet-stream").split("/")[-1]
    else:
        file_format = None

    file_name = None
    temp_file_name = None
    dirname = validate_title(f"{chat_id}")
    if message.chat and message.chat.title:
        dirname = validate_title(f"{message.chat.title}")

    if message.date:
        datetime_dir_name = message.date.strftime(app.date_format)
    else:
        datetime_dir_name = "0"

    if _type in ["voice", "video_note"]:
        # pylint: disable = C0209
        file_format = media_obj.mime_type.split("/")[-1]  # type: ignore
        file_save_path = app.get_file_save_path(_type, dirname, datetime_dir_name)
        file_name = "{} - {}_{}.{}".format(
            message.id,
            _type,
            media_obj.date.isoformat(),  # type: ignore
            file_format,
        )
        file_name = validate_title(file_name)
        temp_file_name = os.path.join(app.temp_save_path, dirname, file_name)

        file_name = os.path.join(file_save_path, file_name)
    else:
        file_name = getattr(media_obj, "file_name", None)
        caption = getattr(message, "caption", None)

        file_name_suffix = ".unknown"
        if not file_name:
            file_name_suffix = get_extension(
                media_obj.file_id, getattr(media_obj, "mime_type", "")
            )
        else:
            # file_name = file_name.split(".")[0]
            _, file_name_without_suffix = os.path.split(os.path.normpath(file_name))
            file_name, file_name_suffix = os.path.splitext(file_name_without_suffix)
            if not file_name_suffix:
                file_name_suffix = get_extension(
                    media_obj.file_id, getattr(media_obj, "mime_type", "")
                )

        if caption:
            caption = validate_title(caption)
            app.set_caption_name(chat_id, message.media_group_id, caption)
            app.set_caption_entities(
                chat_id, message.media_group_id, message.caption_entities
            )
        else:
            caption = app.get_caption_name(chat_id, message.media_group_id)

        if not file_name and message.photo:
            file_name = f"{message.photo.file_unique_id}"

        gen_file_name = (
            app.get_file_name(
                message.id, file_name, caption, publish_time=message.date
            )
            + file_name_suffix
        )

        file_save_path = app.get_file_save_path(_type, dirname, datetime_dir_name)

        temp_file_name = os.path.join(app.temp_save_path, dirname, gen_file_name)

        file_name = os.path.join(file_save_path, gen_file_name)
    return truncate_filename(file_name), truncate_filename(temp_file_name), file_format


async def add_download_task(
    message: pyrogram.types.Message,
    node: TaskNode,
):
    """Add Download task"""
    if message.empty:
        return False
    node.download_status[message.id] = DownloadStatus.Downloading
    await queue.put((message, node))
    node.total_task += 1
    return True


async def save_msg_to_file(
    app, chat_id: Union[int, str], message: pyrogram.types.Message
):
    """Write message text into file"""
    dirname = validate_title(
        message.chat.title if message.chat and message.chat.title else str(chat_id)
    )
    datetime_dir_name = message.date.strftime(app.date_format) if message.date else "0"

    file_save_path = app.get_file_save_path("msg", dirname, datetime_dir_name)
    # file_save_path 已含 save_path 前缀，不要再拼 temp_save_path（否则成 temp/app/downloads/...）
    file_name = os.path.join(
        file_save_path,
        f"{app.get_file_name(message.id, None, None, publish_time=message.date)}.txt",
    )

    os.makedirs(os.path.dirname(file_name), exist_ok=True)

    if _is_exist(file_name):
        return DownloadStatus.SkipDownload, None

    with open(file_name, "w", encoding="utf-8") as f:
        f.write(message.text or "")

    return DownloadStatus.SuccessDownload, file_name


async def download_task(
    client: pyrogram.Client, message: pyrogram.types.Message, node: TaskNode
):
    """Download and Forward media"""

    download_status, file_name = await download_media(
        client, message, app.media_types, app.file_formats, node
    )

    if app.enable_download_txt and message.text and not message.media:
        download_status, file_name = await save_msg_to_file(app, node.chat_id, message)

    if not node.bot:
        app.set_download_id(node, message.id, download_status)

    node.download_status[message.id] = download_status

    try:
        file_size = os.path.getsize(file_name) if file_name else 0
    except OSError:
        # 文件可能在转发后被删除（after_upload_telegram_delete），不影响计数
        file_size = 0

    await upload_telegram_chat(
        client,
        node.upload_user if node.upload_user else client,
        app,
        node,
        message,
        download_status,
        file_name,
    )

    # rclone upload
    if (
        not node.upload_telegram_chat_id
        and download_status is DownloadStatus.SuccessDownload
    ):
        ui_file_name = file_name
        if app.hide_file_name:
            ui_file_name = f"****{os.path.splitext(file_name)[-1]}"
        if await app.upload_file(
            file_name, update_cloud_upload_stat, (node, message.id, ui_file_name)
        ):
            node.upload_success_count += 1

    await report_bot_download_status(
        node.bot,
        node,
        download_status,
        file_size,
    )


# pylint: disable = R0915,R0914


@record_download_status
async def download_media(
    client: pyrogram.client.Client,
    message: pyrogram.types.Message,
    media_types: List[str],
    file_formats: dict,
    node: TaskNode,
):
    """
    Download media from Telegram.

    Each of the files to download are retried 3 times with a
    delay of 5 seconds each.

    Parameters
    ----------
    client: pyrogram.client.Client
        Client to interact with Telegram APIs.
    message: pyrogram.types.Message
        Message object retrieved from telegram.
    media_types: list
        List of strings of media types to be downloaded.
        Ex : `["audio", "photo"]`
        Supported formats:
            * audio
            * document
            * photo
            * video
            * voice
    file_formats: dict
        Dictionary containing the list of file_formats
        to be downloaded for `audio`, `document` & `video`
        media types.

    Returns
    -------
    int
        Current message id.
    """

    # pylint: disable = R0912

    file_name: str = ""
    ui_file_name: str = ""
    task_start_time: float = time.time()
    media_size = 0
    _media = None
    message = await fetch_message(client, message)
    try:
        for _type in media_types:
            _media = getattr(message, _type, None)
            if _media is None:
                continue
            file_name, temp_file_name, file_format = await _get_media_meta(
                node.chat_id, message, _media, _type
            )
            media_size = getattr(_media, "file_size", 0)

            ui_file_name = file_name
            if app.hide_file_name:
                ui_file_name = f"****{os.path.splitext(file_name)[-1]}"

            if _can_download(_type, file_formats, file_format):
                if _is_exist(file_name):
                    file_size = os.path.getsize(file_name)
                    # 仅当磁盘文件大小与远端一致时才判定为已下载；
                    # 原 `file_size or file_size == media_size` 在文件损坏/未下完时也会误判跳过
                    if file_size and file_size == media_size:
                        logger.info(
                            f"id={message.id} {ui_file_name} "
                            f"{_t('already download,download skipped')}.\n"
                        )
                        # 已存在的文件也补录到数据库，保证历史完整。
                        # 注意：DB 必须存真实 file_name（掩码只用于展示），
                        # 否则 hide_file_name 开启时 backfill 去重按文件名
                        # 匹配不上，已下载文件会被重复补录
                        db.record_download(
                            chat_id=node.chat_id,
                            message_id=message.id,
                            file_name=file_name,
                            file_size=file_size or media_size,
                            file_path=file_name,
                            media_type=_type if _type else "",
                            status="skip",
                            publish_time=message.date.timestamp() if message.date else None,
                        )

                        return DownloadStatus.SkipDownload, None
            else:
                return DownloadStatus.SkipDownload, None

            break
    except Exception as e:
        logger.error(
            f"Message[{message.id}]: "
            f"{_t('could not be downloaded due to following exception')}:\n[{e}].",
            exc_info=True,
        )
        return DownloadStatus.FailedDownload, None
    if _media is None:
        return DownloadStatus.SkipDownload, None

    message_id = message.id

    for retry in range(3):
        try:
            # 临时文件名加消息 ID：不同消息可能生成同名文件（频道重发同名
            # 文件），并发下载共享同一 .temp 会互相截断/删除对方文件，
            # 引发连锁大小不符与下载失败
            temp_file_name_unique = f"{temp_file_name}.{message_id}.part"
            # 大文件且开启并行分块时先尝试多连接下载；返回 None（不支持/
            # 失败）自动回退 pyrogram 默认顺序下载，正确性不受影响
            parallel_path = None
            if app.concurrent_chunks_per_file > 1:
                parallel_path = await parallel_download_media(
                    client,
                    message,
                    media_size,
                    temp_file_name_unique,
                    app.concurrent_chunks_per_file,
                    progress=update_download_status,
                    progress_args=(
                        message_id,
                        ui_file_name,
                        task_start_time,
                        node,
                        client,
                    ),
                    use_takeout=app.download_use_takeout,
                )

            temp_download_path = parallel_path or await client.download_media(
                message,
                file_name=temp_file_name_unique,
                progress=update_download_status,
                progress_args=(
                    message_id,
                    ui_file_name,
                    task_start_time,
                    node,
                    client,
                ),
            )

            if temp_download_path and isinstance(temp_download_path, str):
                _check_download_finish(media_size, temp_download_path, ui_file_name)
                await asyncio.sleep(0.5)
                _move_to_download_path(temp_download_path, file_name)
                # 记录到数据库（存真实名，掩码仅用于展示，保证去重/补录可用）
                db.record_download(
                    chat_id=node.chat_id,
                    message_id=message_id,
                    file_name=file_name,
                    file_size=media_size,
                    file_path=file_name,
                    media_type=_type if _type else "",
                    publish_time=message.date.timestamp() if message.date else None,
                )
                return DownloadStatus.SuccessDownload, file_name
        except pyrogram.errors.exceptions.bad_request_400.BadRequest:
            logger.warning(
                f"Message[{message.id}]: {_t('file reference expired, refetching')}..."
            )
            await asyncio.sleep(RETRY_TIME_OUT)
            message = await fetch_message(client, message)
            if _check_timeout(retry, message.id):
                # pylint: disable = C0301
                logger.error(
                    f"Message[{message.id}]: "
                    f"{_t('file reference expired for 3 retries, download skipped.')}"
                )
        except _SizeMismatchError:
            # 文件大小与远端不一致：重试无意义，直接判定失败
            logger.error(
                f"Message[{message.id}]: {_t('media size mismatch, download skipped.')}"
            )
            break
        except pyrogram.errors.exceptions.flood_420.FloodWait as wait_err:
            await asyncio.sleep(wait_err.value)
            logger.warning("Message[{}]: FlowWait {}", message.id, wait_err.value)
            _check_timeout(retry, message.id)
        except TypeError:
            # pylint: disable = C0301
            logger.warning(
                f"{_t('Timeout Error occurred when downloading Message')}[{message.id}], "
                f"{_t('retrying after')} {RETRY_TIME_OUT} {_t('seconds')}"
            )
            await asyncio.sleep(RETRY_TIME_OUT)
            if _check_timeout(retry, message.id):
                logger.error(
                    f"Message[{message.id}]: {_t('Timing out after 3 reties, download skipped.')}"
                )
        except Exception as e:
            # pylint: disable = C0301
            logger.error(
                f"Message[{message.id}]: "
                f"{_t('could not be downloaded due to following exception')}:\n[{e}].",
                exc_info=True,
            )
            break

    # 记录失败下载到数据库（供 Web 日志列表「状态」列展示；存真实名）
    if db._db_ok:
        db.record_download(
            chat_id=node.chat_id,
            message_id=message_id,
            file_name=file_name or f"{message_id}",
            file_size=media_size,
            file_path=file_name,
            media_type=_type if _type else "",
            status="failed",
            publish_time=message.date.timestamp() if message.date else None,
        )
    return DownloadStatus.FailedDownload, None


def _load_config():
    """Load config"""
    app.load_config()


def _check_config() -> bool:
    """Check config"""
    print_meta(logger)
    try:
        _load_config()
        db.init_db()
        db.backfill_from_dir(app.save_path)
        logger.add(
            os.path.join(app.log_file_path, "tdl.log"),
            rotation="10 MB",
            retention="10 days",
            level=app.log_level,
        )
    except Exception as e:
        logger.exception(f"load config error: {e}")
        return False

    return True


async def worker(client: pyrogram.client.Client):
    """Work for download task"""
    while app.is_running:
        try:
            item = await queue.get()
            message = item[0]
            node: TaskNode = item[1]

            if node.is_stop_transmission:
                # 停止传输时也补记状态，避免 total_task/finish_task 计数失衡
                # 导致 run_until_all_task_finish 判定错位
                app.set_download_id(node, message.id, DownloadStatus.SkipDownload)
                node.download_status[message.id] = DownloadStatus.SkipDownload
                continue

            if node.client:
                await download_task(node.client, message, node)
            else:
                await download_task(client, message, node)
        except Exception as e:
            logger.exception(f"{e}")
            # 异常时补记 finish_task，避免 total_task/finish_task 计数失衡导致悬挂。
            # 但仅当该消息尚未被 download_task 记账时补记，防止 set_download_id 双计
            # （download_task 内 set_download_id 成功后，后续 upload 抛异常会走到这里）
            try:
                if message.id not in node.download_status:
                    app.set_download_id(node, message.id, DownloadStatus.FailedDownload)
                    node.download_status[message.id] = DownloadStatus.FailedDownload
            except Exception:
                pass


async def download_chat_task(
    client: pyrogram.Client,
    chat_download_config: ChatDownloadConfig,
    node: TaskNode,
):
    """Download all task"""
    messages_iter = get_chat_history_v2(
        client,
        node.chat_id,
        limit=node.limit,
        max_id=node.end_offset_id,
        offset_id=chat_download_config.last_read_message_id,
        reverse=True,
    )

    chat_download_config.node = node

    if chat_download_config.ids_to_retry:
        logger.info(f"{_t('Downloading files failed during last run')}...")
        skipped_messages: list = await client.get_messages(  # type: ignore
            chat_id=node.chat_id, message_ids=chat_download_config.ids_to_retry
        )

        for message in skipped_messages:
            await add_download_task(message, node)

    async for message in messages_iter:  # type: ignore
        meta_data = MetaData()

        caption = message.caption
        if caption:
            caption = validate_title(caption)
            app.set_caption_name(node.chat_id, message.media_group_id, caption)
            app.set_caption_entities(
                node.chat_id, message.media_group_id, message.caption_entities
            )
        else:
            caption = app.get_caption_name(node.chat_id, message.media_group_id)
        set_meta_data(meta_data, message, caption)

        if app.need_skip_message(chat_download_config, message.id):
            continue

        if app.exec_filter(chat_download_config, meta_data):
            await add_download_task(message, node)
        else:
            node.download_status[message.id] = DownloadStatus.SkipDownload
            if message.media_group_id:
                await upload_telegram_chat(
                    client,
                    node.upload_user,
                    app,
                    node,
                    message,
                    DownloadStatus.SkipDownload,
                )

    chat_download_config.need_check = True
    # 确保 total_task 等于最终入队数（即使异常中断也赋值，避免 finish 判定永远不等）
    chat_download_config.total_task = node.total_task
    node.is_running = True


async def download_all_chat(client: pyrogram.Client):
    """Download All chat"""
    chat_count = len(app.chat_download_config)
    if chat_count == 0:
        logger.warning("未配置任何下载目标（chat），请在 Web 界面「配置」页添加频道/群组后保存并重启")
    else:
        logger.info(f"开始下载 {chat_count} 个频道/群组")
    for key, value in app.chat_download_config.items():
        value.node = TaskNode(chat_id=key)
        try:
            await download_chat_task(client, value, value.node)
        except Exception as e:
            logger.warning(f"Download {key} error: {e}")
        finally:
            # 异常时也确保计数一致，避免 total_task=0 而 finish_task>0 导致判定悬挂
            value.total_task = value.node.total_task
            value.need_check = True


app._all_downloads_done = False


async def run_until_all_task_finish():
    """Normal download"""
    while True:
        finish: bool = True
        for _, value in app.chat_download_config.items():
            if not value.need_check or value.total_task != value.finish_task:
                finish = False

        # 所有频道下载完成
        if finish and app.chat_download_config and not app._all_downloads_done:
            app._all_downloads_done = True
            total = sum(v.total_task for v in app.chat_download_config.values())
            logger.success(f"所有下载任务已完成，共 {total} 条消息")

        # 有配置的频道时保持运行等待下载；无频道且无bot时才退出
        if (not app.bot_token and finish and not app.chat_download_config) or app.restart_program:
            break

        await asyncio.sleep(1)


def _exec_loop():
    """Exec loop"""

    app.loop.run_until_complete(run_until_all_task_finish())


async def start_server(client: pyrogram.Client):
    """
    Start the server using the provided client.
    """
    await client.start()


async def stop_server(client: pyrogram.Client):
    """
    Stop the server using the provided client.
    """
    await client.stop()


def main():
    """Main function of the downloader."""
    import signal

    def _sigterm_handler(signum, frame):
        """收到 SIGTERM 时先保存配置再退出

        给保存留足时间后主动退出（NAS/容器默认 SIGKILL 宽限约 10s），
        大频道停机时 ids_to_retry 可能数千条，非原子写又超时被杀会把
        config.yaml 写坏——主动退出保证保存完整。
        """
        logger.info("收到终止信号，正在保存配置...")
        try:
            app.update_config()
            logger.success("配置已保存")
        except Exception as e:
            logger.error(f"保存配置失败: {e}")
        finally:
            os._exit(0)

    signal.signal(signal.SIGTERM, _sigterm_handler)

    tasks = []
    client = HookClient(
        "media_downloader",
        api_id=app.api_id,
        api_hash=app.api_hash,
        proxy=app.proxy,
        workdir=app.session_file_path,
        start_timeout=app.start_timeout,
        no_updates=True,
    )
    try:
        app.pre_run()
        init_web(app)

        set_max_concurrent_transmissions(client, app.max_concurrent_transmissions)

        app.loop.run_until_complete(start_server(client))
        logger.success(_t("Successfully started (Press Ctrl+C to stop)"))

        app.loop.create_task(download_all_chat(client))
        for _ in range(app.max_download_task):
            task = app.loop.create_task(worker(client))
            tasks.append(task)

        if app.bot_token:
            app.loop.run_until_complete(
                start_download_bot(app, client, add_download_task, download_chat_task)
            )
        _exec_loop()
    except KeyboardInterrupt:
        logger.info(_t("KeyboardInterrupt"))
    except Exception as e:
        logger.exception("{}", e)
    finally:
        app.is_running = False
        if app.bot_token:
            app.loop.run_until_complete(stop_download_bot())
        try:
            app.loop.run_until_complete(stop_server(client))
        except Exception as e:
            # 客户端可能已在异常路径被 terminate，避免二次异常掩盖主错误
            logger.warning(f"stop client 忽略异常: {e}")
        for task in tasks:
            task.cancel()
        logger.info(_t("Stopped!"))
        # check_for_updates(app.proxy)
        logger.info(f"{_t('update config')}......")
        app.update_config()
        logger.success(
            f"{_t('Updated last read message_id to config file')},"
            f"{_t('total download')} {app.total_download_task}, "
            f"{_t('total upload file')} "
            f"{app.cloud_drive_config.total_upload_success_file_count}"
        )


if __name__ == "__main__":
    if _check_config():
        main()
