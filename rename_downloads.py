#!/usr/bin/env python3
"""批量重命名下载文件为「发布时间-文件名」格式，并同步 SQLite 历史记录。

用法（在 NAS 上运行，推荐先 dry-run 预览）:
    python3 rename_downloads.py                       # 预览（不实际改动）
    python3 rename_downloads.py --apply               # 真正执行

参数:
    --root <下载根目录>   默认 /volume1/dockerdn/tg
    --db   <sqlite 路径>  默认 <root>/downloads.sqlite3
    --apply               实际重命名并更新数据库（不加则只预览）
    --verbose             打印每条处理详情

说明:
- 旧文件名形如 "1303 - photo.jpg"（message_id - 原名），会剥离开头的 message_id
- 新文件名 = "发布时间 - 原名"，发布时间来源：
    1) 优先 SQLite 的 publish_time（精确到分）
    2) 无 publish_time 时，从目录 YYYY_MM 提取年月（近似，如 2020-08-01_00-00）
- 同名冲突自动加 (1)/(2) 后缀
- 只重命名 download_history 里能定位到磁盘文件的记录；找不到文件的会跳过并报告
"""

import argparse
import os
import re
import sqlite3
import sys
import time

_MID_PREFIX = re.compile(r"^\d+\s*-\s*(.+)$")
_DATE_DIR = re.compile(r"(?P<y>\d{4})[_-](?P<m>\d{1,2})$")


def parse_args():
    p = argparse.ArgumentParser(description="批量重命名下载文件为「发布时间-文件名」")
    p.add_argument("--root", default="/volume1/dockerdn/tg", help="下载根目录")
    p.add_argument("--db", default=None, help="sqlite 路径（默认 <root>/downloads.sqlite3）")
    p.add_argument("--apply", action="store_true", help="真正执行（默认仅预览）")
    p.add_argument("--verbose", action="store_true", help="打印每条详情")
    return p.parse_args()


def strip_mid(name: str) -> str:
    """剥离开头的 message_id - 前缀，返回原名"""
    m = _MID_PREFIX.match(name)
    if m and m.group(1):
        return m.group(1)
    return name


def dir_publish_str(file_path: str) -> str:
    """从路径中的 YYYY_MM 目录提取近似发布时间字符串"""
    parts = file_path.replace("\\", "/").split("/")
    for part in parts:
        m = _DATE_DIR.match(part)
        if m:
            return f"{m.group('y')}-{int(m.group('m')):02d}-01_00-00"
    return None


def _map_to_host(old_path: str, root: str) -> str:
    """把记录里的路径映射到宿主机磁盘路径。

    兼容两种存储形式：
    1. 容器内绝对路径（如 /app/downloads/频道A/2020_08/x.jpg）→ 替换前缀为 root
    2. 相对路径（如 频道A/2020_08/x.jpg）→ 拼到 root 下
    """
    p = old_path.replace("\\", "/")
    # 容器内路径映射（docker-compose 里 /app/downloads -> 宿主下载目录）
    for prefix in ("/app/downloads/", "/app/downloads"):
        if p.startswith(prefix):
            rest = p[len(prefix):].lstrip("/")
            return os.path.join(root, rest)
    if p.startswith("/"):
        return p  # 其它绝对路径原样使用
    return os.path.join(root, p)


def _to_container(new_path: str, root: str) -> str:
    """把宿主重命名后的路径转回容器内路径（用于更新数据库）"""
    n = os.path.normpath(new_path).replace("\\", "/")
    r = os.path.normpath(root).replace("\\", "/")
    if n.startswith(r + "/"):
        rel = n[len(r) + 1:]
        return "/app/downloads/" + rel
    return n


def main():
    args = parse_args()
    db_path = args.db or os.path.join(args.root, "downloads.sqlite3")
    root = args.root

    if not os.path.isfile(db_path):
        print(f"[错误] 找不到数据库: {db_path}")
        sys.exit(1)
    if not os.path.isdir(root):
        print(f"[警告] 下载根目录不存在: {root}（路径写错？）")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, chat_id, message_id, file_name, file_path, publish_time "
        "FROM download_history ORDER BY id"
    ).fetchall()
    print(f"共 {len(rows)} 条历史记录，数据库: {db_path}")
    print(f"下载根目录: {root}\n")

    # 预统计
    stats = {"rename": 0, "skip_notfound": 0, "skip_notime": 0, "skip_noneed": 0, "conflict": 0}
    planned = []  # (rec_id, old_path, new_path, new_name)

    for r in rows:
        rec_id = r["id"]
        old_name = r["file_name"] or ""
        old_path = r["file_path"] or ""
        publish_time = r["publish_time"]

        # 跳过数据库临时文件被误补录的记录
        if old_name.endswith((".sqlite3", "-shm", "-wal")) or old_path.endswith((".sqlite3", "-shm", "-wal")):
            continue

        # 定位磁盘文件：把容器内路径 /app/downloads 映射到宿主 root
        disk_path = _map_to_host(old_path, root)
        if not os.path.isfile(disk_path):
            stats["skip_notfound"] += 1
            if args.verbose:
                print(f"[跳过·找不到文件] {disk_path}")
            continue

        # 计算新名称（file_name 字段可能是完整路径，先取纯文件名）
        old_base = os.path.basename(old_name.replace("\\", "/"))
        orig = strip_mid(old_base)
        if publish_time:
            ts = time.strftime("%Y-%m-%d_%H-%M", time.localtime(publish_time))
        else:
            ts = dir_publish_str(old_path)
            if not ts:
                stats["skip_notime"] += 1
                print(f"[跳过·无法确定发布时间] {disk_path}")
                continue
        new_name = f"{ts} - {orig}"
        if new_name == old_base:
            stats["skip_noneed"] += 1
            continue

        new_path = os.path.join(os.path.dirname(disk_path), new_name)

        # 同名冲突处理（预览时也模拟）
        counter = 1
        final_path = new_path
        final_name = new_name
        while os.path.exists(final_path) and os.path.abspath(final_path) != os.path.abspath(disk_path):
            base, ext = os.path.splitext(new_name)
            final_name = f"{base} ({counter}){ext}"
            final_path = os.path.join(os.path.dirname(disk_path), final_name)
            counter += 1
        if final_name != new_name:
            stats["conflict"] += 1

        planned.append((rec_id, disk_path, final_path, final_name, ts))
        stats["rename"] += 1
        if args.verbose:
            print(f"  {os.path.basename(disk_path)}")
            print(f"    -> {final_name}")

    print(f"\n=== 统计 ===")
    for k, v in stats.items():
        print(f"  {k}: {v}")

    if not args.apply:
        print("\n[预览模式] 以上为将要执行的改动，加 --apply 才会真正重命名并更新数据库")
        conn.close()
        return

    # 执行
    print("\n[执行中...]")
    updated = 0
    for rec_id, disk_path, final_path, final_name, _ts in planned:
        try:
            os.rename(disk_path, final_path)
            # 更新数据库：file_name 与 file_path（存容器内路径，与下载器一致）
            new_fp = _to_container(final_path, root)
            conn.execute(
                "UPDATE download_history SET file_name=?, file_path=? WHERE id=?",
                (final_name, new_fp, rec_id),
            )
            updated += 1
            print(f"  [重命名] {os.path.basename(disk_path)} -> {final_name}")
        except OSError as e:
            print(f"  [失败] {disk_path}: {e}")

    conn.commit()
    conn.close()
    print(f"\n完成：重命名 {updated} 个文件并更新数据库")


if __name__ == "__main__":
    main()
