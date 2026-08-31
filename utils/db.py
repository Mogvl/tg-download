"""SQLite 数据库：持久化下载历史"""

import logging
import os
import sqlite3
import time

logger = logging.getLogger("tdl.db")

# DB 路径默认锚定 CWD（与历史行为/Docker WORKDIR=/app 兼容）；
# 其他启动方式（systemd/手动指定目录）可用环境变量 TDL_DB_PATH 显式指定，
# 避免 DB 建到错误目录导致 downloads 卷持久化失效
DB_PATH = os.environ.get(
    "TDL_DB_PATH",
    os.path.join(os.path.abspath("."), "downloads", "downloads.sqlite3"),
)
_db_ok = False
# 写失败累计：磁盘满/卷只读/WAL 不可建时写全部静默失败，暴露给接口探查
_db_write_failures = 0
# 写锁：串行化并发写，配合 WAL + busy_timeout 避免 database is locked
_write_lock = None  # 延迟初始化（须在 init_db 后）


def get_write_failures() -> int:
    """累计写失败次数（供健康检查/接口暴露）"""
    return _db_write_failures


def _get_lock():
    global _write_lock
    if _write_lock is None:
        import threading

        _write_lock = threading.Lock()
    return _write_lock


def _conn():
    # check_same_thread=False：允许 Flask/下载多线程各自使用连接
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db():
    """建表（幂等）"""
    global _db_ok
    try:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        with _conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("""
                CREATE TABLE IF NOT EXISTS download_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    file_name TEXT NOT NULL,
                    file_size INTEGER NOT NULL,
                    file_path TEXT,
                    media_type TEXT,
                    download_timestamp REAL NOT NULL,
                    status TEXT,
                    upload_telegram_time REAL,
                    publish_time REAL,
                    UNIQUE(chat_id, message_id)
                )
            """)
            # 兼容旧库：新增字段（sqlite 不支持 ADD COLUMN IF NOT EXISTS）
            for col, typ in (("status", "TEXT"), ("upload_telegram_time", "REAL"), ("publish_time", "REAL")):
                try:
                    c.execute(f"ALTER TABLE download_history ADD COLUMN {col} {typ}")
                except Exception:
                    pass
            # 兼容旧库：更早版本建表可能没有 UNIQUE(chat_id, message_id)
            # （CREATE TABLE IF NOT EXISTS 不会升级已有表），而 record_download
            # 的 ON CONFLICT upsert 依赖它，缺失会导致每条记录都报
            # "ON CONFLICT clause does not match any PRIMARY KEY or UNIQUE
            # constraint"。先清掉重复行（保留最新一条）再补唯一索引
            try:
                c.execute(
                    "DELETE FROM download_history WHERE id NOT IN "
                    "(SELECT MAX(id) FROM download_history "
                    "GROUP BY chat_id, message_id)"
                )
                c.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_msg "
                    "ON download_history(chat_id, message_id)"
                )
            except Exception as idx_err:
                logger.warning(f"补建唯一索引失败(不影响下载): {idx_err}")
            c.execute("CREATE INDEX IF NOT EXISTS idx_ts ON download_history(download_timestamp DESC)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_chat ON download_history(chat_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_pt ON download_history(publish_time)")
            # 一次性迁移：修复 hide_file_name 开启时写入的掩码文件名（****.ext）。
            # DB 必须存真实名（去重/补录按 file_name 匹配），掩码仅用于展示；
            # 真实名可从一直存对的 file_path 取 basename 恢复
            try:
                rows = c.execute(
                    "SELECT id, file_path FROM download_history "
                    "WHERE file_name LIKE '****%' AND chat_id != '-' "
                    "AND file_path IS NOT NULL AND file_path != ''"
                ).fetchall()
                fixed = 0
                for rid, fpath in rows:
                    base = os.path.basename(str(fpath).replace("\\", "/"))
                    if base and not base.startswith("****"):
                        c.execute(
                            "UPDATE download_history SET file_name = ? WHERE id = ?",
                            (base, rid),
                        )
                        fixed += 1
                if fixed:
                    logger.info(f"已修复 {fixed} 条掩码文件名的历史记录")
            except Exception as migration_err:
                logger.warning(f"掩码文件名迁移失败(不影响使用): {migration_err}")
        _db_ok = True
    except Exception as e:
        logger.error(f"数据库初始化失败: {e}")


def record_download(chat_id, message_id, file_name, file_size, file_path="", media_type="", status="success", publish_time=None):
    """记录一次下载（status: success / failed / skip）

    同一 (chat_id, message_id) 覆盖旧记录（重试成功后更新状态/时间，避免历史保留旧 failed）。
    """
    if not _db_ok:
        return
    try:
        with _get_lock():
            with _conn() as c:
                c.execute(
                    "INSERT INTO download_history (chat_id,message_id,file_name,file_size,file_path,media_type,download_timestamp,status,publish_time) "
                    "VALUES (?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(chat_id,message_id) DO UPDATE SET "
                    "file_name=excluded.file_name, file_size=excluded.file_size, "
                    "file_path=excluded.file_path, media_type=excluded.media_type, "
                    "download_timestamp=excluded.download_timestamp, status=excluded.status, "
                    "publish_time=excluded.publish_time",
                    (str(chat_id), message_id, file_name, file_size, file_path, media_type, time.time(), status, publish_time),
                )
    except Exception as e:
        # pylint: disable=W0603
        global _db_write_failures
        _db_write_failures += 1
        logger.error(f"记录下载失败 {file_name}: {e}")


def record_upload_time(chat_id, message_id, ts=None):
    """记录转发到 Telegram 频道的时间（按 chat_id + message_id 定位）"""
    if not _db_ok:
        return
    try:
        with _get_lock():
            with _conn() as c:
                c.execute(
                    "UPDATE download_history SET upload_telegram_time = ? "
                    "WHERE chat_id = ? AND message_id = ?",
                    (ts if ts is not None else time.time(), str(chat_id), message_id),
                )
    except Exception as e:
        # pylint: disable=W0603
        global _db_write_failures
        _db_write_failures += 1
        logger.error(f"记录转发时间失败: {e}")


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
        "publish_time": "publish_time",
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
        with _get_lock():
            with _conn() as c:
                c.execute("DELETE FROM download_history")
    except Exception as e:
        # pylint: disable=W0603
        global _db_write_failures
        _db_write_failures += 1
        logger.error(f"清空历史失败: {e}")


def get_total_count() -> int:
    """历史累计总数（全量，不受搜索/筛选影响）"""
    if not _db_ok:
        return 0
    try:
        with _conn() as c:
            return c.execute("SELECT COUNT(*) FROM download_history").fetchone()[0]
    except Exception as e:
        logger.error(f"查询历史总数失败: {e}")
        return 0


def backfill_from_dir(base_dir, media_type="document"):
    """启动时扫描下载目录，把数据库中不存在的旧文件补录进历史。

    用于兼容数据库功能上线前已下载的文件。文件名+大小作为去重键。
    """
    if not _db_ok or not base_dir or not os.path.isdir(base_dir):
        return 0
    added = 0
    try:
        # 先清理历史补录残留：删除 chat_id="-" 且与真实记录(file_name+file_size)重复的补录行
        _dedup_backfill_duplicates()
        with _get_lock():
            with _conn() as c:
                # 去重键：真实下载记录优先，补录记录不再重复插入同名同大小文件
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
                        # 用负数自增 message_id 作唯一键，避免 UNIQUE(chat_id,message_id) 冲突
                        # （chat_id 统一为 "-"，message_id 全 0 会导致 INSERT 只生效第一条）
                        added += 1
                        c.execute(
                            "INSERT OR IGNORE INTO download_history (chat_id,message_id,file_name,file_size,file_path,media_type,download_timestamp,status) VALUES (?,?,?,?,?,?,?,?)",
                            ("-", -added, name, size, rel, media_type, mtime, "success"),
                        )
                        existing.add((name, size))
    except Exception as e:
        logger.error(f"补录历史失败: {e}")
    if added:
        logger.info(f"从目录补录 {added} 个历史文件")
    return added


def _dedup_backfill_duplicates():
    """删除历史补录残留：chat_id="-" 且与真实下载记录同 file_name 的行"""
    if not _db_ok:
        return 0
    try:
        with _get_lock():
            with _conn() as c:
                # 找到真实下载记录(chat_id != '-')的 file_name 集合
                # 以 file_name 为主键（忽略 file_size，因为 backfill 补录的 size
                # 可能与 record_download 的 media_size 有差异，导致 (name,size) 匹配不上）
                real_names = {
                    r[0]
                    for r in c.execute(
                        "SELECT file_name FROM download_history WHERE chat_id != '-'"
                    ).fetchall()
                }
                if not real_names:
                    return 0
                # 删除补录行(chat_id='-')中与真实记录 file_name 相同的行
                cur = c.execute(
                    "SELECT id, file_name FROM download_history WHERE chat_id = '-'"
                ).fetchall()
                del_ids = [r[0] for r in cur if r[1] in real_names]
                for did in del_ids:
                    c.execute("DELETE FROM download_history WHERE id = ?", (did,))
                if del_ids:
                    logger.info(f"清理 {len(del_ids)} 条重复补录记录")
                return len(del_ids)
    except Exception as e:
        logger.error(f"清理重复补录失败: {e}")
        return 0
