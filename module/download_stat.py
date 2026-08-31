"""Download Stat"""
import asyncio
import time
from collections import OrderedDict
from enum import Enum

from pyrogram import Client

from module.app import TaskNode


class DownloadState(Enum):
    """Download state"""

    Downloading = 1
    StopDownload = 2


# 每个 chat 最多保留的下载进度条目，防止长期运行内存无限增长
_MAX_RESULT_PER_CHAT = 500

# 速度窗口：累计至少这么久才刷新一次显示值。回调按 1MB 分块触发，
# 窗口太短会让速度随分块到达节奏大幅跳变（1MB 集中计入 1s 窗口被高估）
_SPEED_WINDOW_SECONDS = 3.0
# 空闲判定：超过这么久没有任何字节回调才把总速度归零。
# 文件切换（收尾+取下一任务+建连）与频道间扫描存在数秒的真实字节间隙，
# 阈值过小会在下载进行中反复闪 0；30s 只在真正停止/长时间卡顿时归零
_TOTAL_IDLE_SECONDS = 30.0

_download_result: dict = {}
_download_state: DownloadState = DownloadState.Downloading

# 总速度独立计算：累加所有任务每次回调的增量，真实反映总体吞吐量
_total_download_speed: int = 0
_total_download_size: int = 0
_last_download_time: float = time.time()
# 最后一次收到字节的回调时间，用于判断下载是否已空闲（速度归零）
_last_byte_time: float = time.time()


def get_download_result() -> dict:
    """get global download result"""
    return _download_result


def get_total_download_speed() -> int:
    """get total download speed

    独立计算的全局速度：累加所有任务每次回调增量，按窗口均值更新。
    读取时主动刷新窗口（不依赖回调触发），保证前端每次轮询都能拿到最新值。
    """
    global _total_download_speed, _total_download_size, _last_download_time
    now = time.time()
    dt = now - _last_download_time
    if dt >= _SPEED_WINDOW_SECONDS:
        if _total_download_size > 0:
            # 窗口均值：窗口内的空闲间隙也被摊入，平滑分块到达的跳变
            _total_download_speed = int(_total_download_size / dt)
            _total_download_speed = max(_total_download_speed, 0)
            _total_download_size = 0
            _last_download_time = now
        elif now - _last_byte_time > _TOTAL_IDLE_SECONDS:
            # 窗口内无新数据且超过空闲阈值没有任何字节回调：下载已空闲，
            # 速度归零（保留旧值会一直显示陈旧速度）
            _total_download_speed = 0
            _last_download_time = now
        else:
            # 短暂无新数据：保留上次速度（不闪 0），窗口顺延，
            # 字节恢复后把间隙一起摊入均值
            pass
    return _total_download_speed


def get_download_state() -> DownloadState:
    """get download state"""
    return _download_state


# pylint: disable = W0603
def set_download_state(state: DownloadState):
    """set download state"""
    global _download_state
    _download_state = state


async def update_download_status(
    down_byte: int,
    total_size: int,
    message_id: int,
    file_name: str,
    start_time: float,
    node: TaskNode,
    client: Client,
):
    """update_download_status"""
    cur_time = time.time()
    # pylint: disable = W0603
    global _total_download_speed, _total_download_size, _last_download_time
    global _last_byte_time

    if node.is_stop_transmission:
        client.stop_transmission()

    chat_id = node.chat_id

    while get_download_state() == DownloadState.StopDownload:
        if node.is_stop_transmission:
            client.stop_transmission()
        await asyncio.sleep(1)

    if not _download_result.get(chat_id):
        _download_result[chat_id] = OrderedDict()

    existing = _download_result[chat_id].get(message_id)
    # 字节倒退 = 同一消息进入了新一轮重试（重试从 0 重新上报字节）。
    # 此时按"首次回调"处理：不累计负增量、重置条目，否则大负数会污染
    # 每任务窗口并把全局累加器打成负数，导致总速度长时间显示陈旧值
    if existing and down_byte < existing["down_byte"]:
        existing = None

    if existing:
        last_download_byte = existing["down_byte"]
        last_time = existing["end_time"]
        download_speed = existing["download_speed"]
        each_second_total_download = existing["each_second_total_download"]
        end_time = existing["end_time"]

        # 仅累计增量（每行速度 + 全局总速度）
        each_second_total_download += down_byte - last_download_byte
        _total_download_size += down_byte - last_download_byte

        if cur_time - last_time >= 1.0:
            download_speed = int(each_second_total_download / (cur_time - last_time))
            end_time = cur_time
            each_second_total_download = 0

        download_speed = max(download_speed, 0)

        existing["down_byte"] = down_byte
        existing["end_time"] = end_time
        existing["download_speed"] = download_speed
        existing["each_second_total_download"] = each_second_total_download
        # 保持插入序，便于淘汰最旧
        _download_result[chat_id].move_to_end(message_id)
    else:
        each_second_total_download = 0  # 首次回调作为窗口起点，不把初始字节算进窗口速率
        _download_result[chat_id][message_id] = {
            "down_byte": down_byte,
            "total_size": total_size,
            "file_name": file_name,
            "start_time": start_time,
            "end_time": cur_time,
            "download_speed": down_byte / (cur_time - start_time) if cur_time > start_time else 0,
            "each_second_total_download": each_second_total_download,
            "task_id": node.task_id,
        }

        # 限制每个 chat 的条目数，防止内存无限增长
        while len(_download_result[chat_id]) > _MAX_RESULT_PER_CHAT:
            _download_result[chat_id].popitem(last=False)

    # 有字节流动即刷新空闲判定基准
    _last_byte_time = cur_time
    # 窗口刷新统一由 get_total_download_speed() 读取时执行，
    # 避免回调驱动与读取驱动重复清零 _total_download_size
