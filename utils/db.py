"""SQLite 数据库：持久化下载历史"""

import logging
import os
import sqlite3
import time

logger = logging.getLogger("tdl.db")

DB_PATH = os.path.join(os.path.abspath("."), "downloads", "downloads.sqlite3")
_db_ok = False


def _conn():
    return sqlite3.connect(DB_PATH)


def init_db():
    """建表（幂等）"""
    global _db_ok
    try:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        with _conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS download_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    file_name TEXT NOT NULL,
                    file_size INTEGER NOT NULL,
                    file_path TEXT,
                    media_type TEXT,
                    download_timestamp REAL NOT NULL
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_ts ON download_history(download_timestamp DESC)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_chat ON download_history(chat_id)")
        _db_ok = True
    except Exception as e:
        logger.error(f"数据库初始化失败: {e}")


def record_download(chat_id, message_id, file_name, file_size, file_path="", media_type=""):
    """记录一次成功下载"""
    if not _db_ok:
        return
    try:
        with _conn() as c:
            c.execute(
                "INSERT INTO download_history (chat_id,message_id,file_name,file_size,file_path,media_type,download_timestamp) VALUES (?,?,?,?,?,?,?)",
                (str(chat_id), message_id, file_name, file_size, file_path, media_type, time.time()),
            )
    except Exception as e:
        logger.error(f"记录下载失败 {file_name}: {e}")


def get_history(page=1, per_page=30, search="", media_type="All", sort_by="download_timestamp", sort_desc=True):
    """查询下载历史（带搜索/筛选/排序/分页）"""
    if not _db_ok:
        return [], 0
    valid_sort = {
        "download_timestamp": "download_timestamp",
        "chat_id": "chat_id",
        "file_name": "file_name",
        "file_size": "file_size",
        "media_type": "media_type",
    }
    col = valid_sort.get(sort_by, "download_timestamp")
    direction = "DESC" if sort_desc else "ASC"

    where, params = ["1=1"], []
    if search:
        where.append("(file_name LIKE ? OR chat_id LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]
    if media_type and media_type != "All":
        where.append("media_type = ?")
        params.append(media_type)

    where_sql = " AND ".join(where)
    offset = (page - 1) * per_page

    try:
        with _conn() as c:
            c.row_factory = sqlite3.Row
            total = c.execute(f"SELECT COUNT(*) as n FROM download_history WHERE {where_sql}", params).fetchone()["n"]
            rows = c.execute(
                f"SELECT * FROM download_history WHERE {where_sql} ORDER BY {col} {direction} LIMIT ? OFFSET ?",
                params + [per_page, offset],
            ).fetchall()
            return [dict(r) for r in rows], total
    except Exception as e:
        logger.error(f"查询历史失败: {e}")
        return [], 0


def clear_history():
    """清空下载历史"""
    if not _db_ok:
        return
    try:
        with _conn() as c:
            c.execute("DELETE FROM download_history")
    except Exception as e:
        logger.error(f"清空历史失败: {e}")


def backfill_from_dir(base_dir, media_type="document"):
    """启动时扫描下载目录，把数据库中不存在的旧文件补录进历史。

    用于兼容数据库功能上线前已下载的文件。文件名+大小作为去重键。
    """
    if not _db_ok or not base_dir or not os.path.isdir(base_dir):
        return 0
    added = 0
    try:
        with _conn() as c:
            existing = {
                (r[0], r[1])
                for r in c.execute("SELECT file_name, file_size FROM download_history").fetchall()
            }
            for root, _dirs, files in os.walk(base_dir):
                for name in files:
                    if name.startswith(".") or name.endswith((".session", ".sqlite3")):
                        continue
                    fpath = os.path.join(root, name)
                    try:
                        size = os.path.getsize(fpath)
                    except OSError:
                        continue
                    if (name, size) in existing:
                        continue
                    rel = os.path.relpath(fpath, base_dir)
                    mtime = os.path.getmtime(fpath)
                    c.execute(
                        "INSERT INTO download_history (chat_id,message_id,file_name,file_size,file_path,media_type,download_timestamp) VALUES (?,?,?,?,?,?,?)",
                        ("-", 0, name, size, rel, media_type, mtime),
                    )
                    existing.add((name, size))
                    added += 1
    except Exception as e:
        logger.error(f"补录历史失败: {e}")
    if added:
        logger.info(f"从目录补录 {added} 个历史文件")
    return added
