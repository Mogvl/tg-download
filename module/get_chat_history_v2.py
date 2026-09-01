"""Rewrite pyrogram.get_chat_history"""

from datetime import datetime
from typing import AsyncGenerator, Optional, Union

import pyrogram
from loguru import logger

# pylint: disable = W0611
from pyrogram import raw, types, utils


async def get_chunk_v2(
    *,
    client: pyrogram.Client,
    chat_id: Union[int, str],
    limit: int = 0,
    offset: int = 0,
    max_id: int = 0,
    from_message_id: int = 0,
    from_date: datetime = utils.zero_datetime(),
    reverse: bool = False
):
    """get chunk"""
    from_message_id = from_message_id or (1 if reverse else 0)

    messages = await utils.parse_messages(
        client,
        await client.invoke(
            raw.functions.messages.GetHistory(
                peer=await client.resolve_peer(chat_id),
                offset_id=from_message_id,
                offset_date=utils.datetime_to_timestamp(from_date),
                add_offset=offset * (-1 if reverse else 1) - (limit if reverse else 0),
                limit=limit,
                max_id=max_id,
                min_id=0,
                hash=0,
            ),
            sleep_threshold=60,
        ),
        replies=0,
    )

    if reverse:
        messages.reverse()

    return messages


# pylint: disable = C0301
async def get_chat_history_v2(
    self: pyrogram.Client,
    chat_id: Union[int, str],
    limit: int = 0,
    max_id: int = 0,
    offset: int = 0,
    offset_id: int = 0,
    offset_date: datetime = utils.zero_datetime(),
    reverse: bool = False,
) -> Optional[AsyncGenerator["types.Message", None]]:
    """Get messages from a chat history."""
    current = 0
    total = limit or (1 << 31) - 1
    limit = min(100, total)

    prev_offset_id = None
    page_no = 0
    while True:
        messages = await get_chunk_v2(
            client=self,
            chat_id=chat_id,
            limit=limit,
            offset=offset,
            max_id=max_id + 1 if max_id else 0,
            from_message_id=offset_id,
            from_date=offset_date,
            reverse=reverse,
        )

        if not messages:
            # 没有更多消息，结束分页
            return

        offset_id = messages[-1].id + (1 if reverse else 0)
        # offset 保持调用方初值恒定，仅推进 offset_id 分页。
        # reverse 的窗口语义：id ≥ offset_id 的范围里从 offset 处取 limit 条，
        # offset_id(左界) 与 add_offset(-offset-limit 的右移量) 若同时推进
        # 会双重跳页——恒定 offset 下每页恰好衔接上一页（已实测验证覆盖）。
        # 空隙说明：频道消息 id 有空洞（非媒体/删除消息），本页末 id 加一
        # 后下一页自动从空洞后继续，无需 offset 参与

        # reverse 模式推进保护：offset_id 必须严格前移，否则说明该页与
        # 上一页重叠（服务端边界行为差异），继续会无限重复拉取
        if reverse and prev_offset_id is not None and offset_id <= prev_offset_id:
            logger.warning(
                f"chat {chat_id}: page not advancing "
                f"(offset_id {offset_id} <= {prev_offset_id}), stop pagination"
            )
            return
        prev_offset_id = offset_id
        page_no += 1
        logger.debug(
            f"chat {chat_id}: page {page_no}: "
            f"ids {messages[0].id}..{messages[-1].id} ({len(messages)} msgs)"
        )

        for message in messages:
            yield message

            current += 1

            if current >= total:
                return
