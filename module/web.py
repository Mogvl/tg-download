"""web ui for media download"""

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time

from flask import Flask, jsonify, redirect, render_template, request
from flask_login import LoginManager, UserMixin, login_required, login_user
from ruamel import yaml

import utils
from module.app import Application
from module.download_stat import (
    DownloadState,
    get_download_result,
    get_download_state,
    get_total_download_speed,
    set_download_state,
)
from utils.crypto import AesBase64
from utils.format import format_byte

log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)

_flask_app = Flask(__name__)

# 会话密钥：默认随机生成（重启后需重新登录），
# 若配置了 web_login_secret 则在 init_web 中以其作为密钥，保持登录态稳定
_flask_app.secret_key = secrets.token_urlsafe(32)
_login_manager = LoginManager()
_login_manager.login_view = "login"
_login_manager.init_app(_flask_app)
deAesCrypt = AesBase64("1234123412ABCDEF", "ABCDEF1234123412")

# 全局引用，供新路由读写配置 / 日志 / 历史
_app: Application = None  # type: ignore
_yaml = yaml.YAML()
_yaml.preserve_quotes = True


class User(UserMixin):
    """Web Login User"""

    def __init__(self):
        self.sid = "root"

    @property
    def id(self):
        """ID"""
        return self.sid


@_login_manager.user_loader
def load_user(_):
    """
    Load a user object from the user ID.

    Returns:
        User: The user object.
    """
    return User()


def get_flask_app() -> Flask:
    """get flask app instance"""
    return _flask_app


def run_web_server(app: Application):
    """
    Runs a web server using the Flask framework.
    """

    get_flask_app().run(
        app.web_host, app.web_port, debug=app.debug_web, use_reloader=False
    )


# pylint: disable = W0603
def _login_enabled() -> bool:
    """登录开关是否开启（运行时实时从磁盘 yaml 读取，不依赖内存快照）"""
    cfg = _read_config()
    raw = cfg.get("web_login_enabled", False)
    # 字符串 "false" 不能 bool()（bool("false")=True）
    if isinstance(raw, str):
        return raw.strip().lower() == "true"
    return bool(raw)


def _current_secret() -> str:
    """实时从磁盘读取 web_login_secret，避免依赖启动时的全局快照"""
    cfg = _read_config()
    return str(cfg.get("web_login_secret", "") or "")


# 登录失败限速：5 次失败后锁定 60 秒
_LOGIN_MAX_FAIL = 5
_LOGIN_LOCK_SECONDS = 60
_login_fail_count = 0
_login_fail_time = 0.0


def _check_login_rate() -> bool:
    """登录是否允许尝试（限速）"""
    global _login_fail_count, _login_fail_time
    now = time.time()
    if _login_fail_count >= _LOGIN_MAX_FAIL:
        if now - _login_fail_time < _LOGIN_LOCK_SECONDS:
            return False
        # 锁定时间结束，重置
        _login_fail_count = 0
    return True


def _record_login_fail():
    """记录一次登录失败"""
    global _login_fail_count, _login_fail_time
    _login_fail_count += 1
    _login_fail_time = time.time()


def _reset_login_fail():
    """登录成功后重置失败计数"""
    global _login_fail_count
    _login_fail_count = 0


def _apply_login_state():
    """在每个请求前，根据磁盘上的开关实时决定是否需要登录。

    解决两个问题：
    1. 开关改 true 后仍免登录（旧实现只在启动时判断一次，且 LOGIN_DISABLED 只设不清除）
    2. 运行时改 yaml 重启后立即生效，不残留旧状态
    """
    if _login_enabled():
        # 开关开启：必须输密码
        _flask_app.config.pop("LOGIN_DISABLED", None)
        _login_manager.login_view = "login"
    else:
        # 开关关闭：免登录直进主页
        _flask_app.config["LOGIN_DISABLED"] = True
    _csrf_protect()


def _csrf_protect():
    """简单 CSRF 防护：POST 请求校验 Origin/Referer 与 Host 同源"""
    # 启动时（init_web 调 _apply_login_state）没有请求上下文，request 不可用，直接跳过
    from flask import has_request_context

    if not has_request_context():
        return
    if request.method != "POST":
        return
    # 只对需要登录态的接口生效（避免影响 /login 本身的 POST）
    if request.path in ("/login",):
        return
    origin = request.headers.get("Origin") or request.headers.get("Referer") or ""
    if not origin:
        # 无 Origin/Referer（如 curl）直接拒绝写操作
        from flask import abort

        abort(403)
    from urllib.parse import urlparse

    # 精确比较 hostname（子串匹配可被 tg.example.com.evil.com 绕过）
    origin_host = (urlparse(origin).hostname or "").lower()
    request_host = (request.host.split(":")[0] or "").lower()
    if origin_host != request_host:
        from flask import abort

        abort(403)


def init_web(app: Application):
    """
    Set the value of the users variable.

    Args:
        users: The list of users to set.

    Returns:
        None.
    """
    global _app
    _app = app
    # 用 web_login_secret 派生会话密钥（哈希到 32 字节），保持登录态跨重启稳定；
    # 未配置时用随机密钥（重启后需重新登录）
    if app.web_login_secret:
        _flask_app.secret_key = hashlib.sha256(
            app.web_login_secret.encode("utf-8")
        ).digest()
    # 启动时先按当前开关设置一次（避免首个请求前空白期）
    _apply_login_state()
    # 之后每个请求前都重新判定，保证开关实时、正确生效
    _flask_app.before_request(_apply_login_state)
    if app.debug_web:
        threading.Thread(target=run_web_server, args=(app,)).start()
    else:
        threading.Thread(
            target=get_flask_app().run, daemon=True, args=(app.web_host, app.web_port)
        ).start()


@_flask_app.route("/login", methods=["GET", "POST"])
def login():
    """
    Function to handle the login route.

    Parameters:
    - No parameters

    Returns:
    - If the request method is "POST" and the username and
      password match the ones in the web_login_users dictionary,
      it returns a JSON response with a code of "1".
    - Otherwise, it returns a JSON response with a code of "0".
    - If the request method is not "POST", it returns the rendered "login.html" template.
    """
    if request.method == "POST":
        username = "root"
        web_login_form = {}
        for key, value in request.form.items():
            if value:
                value = deAesCrypt.decrypt(value)
            web_login_form[key] = value

        if not web_login_form.get("password"):
            return jsonify({"code": "0"})

        password = web_login_form["password"]
        # 开关未开启时不应出现登录页；兜底拒绝
        if not _login_enabled():
            return jsonify({"code": "0"})
        secret = _current_secret()
        # 开了开关却没设密码：拒绝任何密码，避免裸奔
        if not secret:
            return jsonify({"code": "0"})
        if _check_login_rate():
            # 恒定时间比较，避免时序侧信道
            if hmac.compare_digest(password, secret):
                user = User()
                login_user(user)
                _reset_login_fail()  # 成功登录后重置失败计数
                return jsonify({"code": "1"})
            _record_login_fail()
            return jsonify({"code": "0"})
        # 失败次数过多，锁定一段时间
        return jsonify({"code": "0", "msg": "尝试次数过多，请稍后再试"})

    # GET：开关关闭时不暴露登录页
    if not _login_enabled():
        return redirect("/")
    return render_template("login.html")


@_flask_app.route("/")
@login_required
def index():
    """Index html"""
    return render_template(
        "index.html",
        download_state=(
            "pause" if get_download_state() is DownloadState.Downloading else "continue"
        ),
    )


@_flask_app.route("/get_download_status")
@login_required
def get_download_speed():
    """Get download speed"""
    return jsonify({
        "download_speed": format_byte(get_total_download_speed()) + "/s",
        "upload_speed": "0.00 B/s",
    })


@_flask_app.route("/set_download_state", methods=["POST"])
@login_required
def web_set_download_state():
    """Set download state"""
    state = request.args.get("state")

    if state == "continue" and get_download_state() is DownloadState.StopDownload:
        set_download_state(DownloadState.Downloading)
        return "pause"

    if state == "pause" and get_download_state() is DownloadState.Downloading:
        set_download_state(DownloadState.StopDownload)
        return "continue"

    return state


@_flask_app.route("/get_app_version")
@login_required  # 未认证时不泄露版本号（仅 index.html 登录后调用）
def get_app_version():
    """Get telegram_media_downloader version"""
    return utils.__version__


@_flask_app.route("/healthz")
def healthz():
    """健康检查端点（公开、无版本号等敏感信息，供 Docker healthcheck 使用）"""
    return "ok"


@_flask_app.route("/get_completion_status")
@login_required
def web_get_completion_status():
    """返回下载完成状态（含失败/进行中统计）"""
    # 注意：主程序以 __main__ 运行，import media_downloader 会得到独立模块实例，
    # 模块级变量不同步。必须通过 _app（共享同一 Application 实例）读取。
    _all_downloads_done = getattr(_app, "_all_downloads_done", False) if _app else False
    from module.app import DownloadStatus

    chat_count = len(_app.chat_download_config) if _app else 0
    total = sum(v.total_task for v in _app.chat_download_config.values()) if _app else 0
    finished = sum(v.finish_task for v in _app.chat_download_config.values()) if _app else 0
    failed = 0
    active = 0
    if _app:
        for v in _app.chat_download_config.values():
            # 快照后迭代：download_status 由事件循环线程并发增删，
            # 直接迭代 values() 偶发 "dictionary changed size during iteration"
            for st in list(v.node.download_status.values()):
                if st is DownloadStatus.FailedDownload:
                    failed += 1
                elif st is DownloadStatus.Downloading:
                    active += 1
    # DB 健康暴露：写失败（磁盘满/卷只读）不再完全静默
    import utils.db as db_mod
    return jsonify({
        "done": bool(_all_downloads_done) and chat_count > 0,
        "total": total,
        "finished": finished,
        "failed": failed,
        "active": active,
        "chat_count": chat_count,
        "db_ok": bool(db_mod._db_ok) and db_mod.get_write_failures() == 0,
    })


@_flask_app.route("/get_download_list")
@login_required
def get_download_list():
    """get download list"""
    if request.args.get("already_down") is None:
        return "[]"

    already_down = request.args.get("already_down") == "true"

    download_result = get_download_result()
    items = []
    # 快照迭代,避免并发修改 RuntimeError
    for chat_id, messages in list(download_result.items()):
        for idx, value in list(messages.items()):
            total_size = value.get("total_size") or 0
            down_byte = value.get("down_byte") or 0
            is_already_down = down_byte == total_size

            if already_down and not is_already_down:
                continue

            progress = round(down_byte / total_size * 100, 1) if total_size else 0.0
            items.append({
                "chat": str(chat_id),
                "id": str(idx),
                "filename": os.path.basename(value.get("file_name") or ""),
                "total_size": format_byte(total_size),
                "download_progress": progress,
                "download_speed": format_byte(value.get("download_speed") or 0) + "/s",
                "down_byte": int(down_byte),   # 原始字节，前端据此计算实时速度
                "save_path": (value.get("file_name") or "").replace("\\", "/"),
                "ftype": _guess_ftype(value.get("file_name") or ""),
            })

    return jsonify(items)


def _guess_ftype(file_name: str) -> str:
    """从文件名猜测媒体类型（用于前端显示类型图标）"""
    ext = os.path.splitext(file_name)[1].lower()
    if ext in (".mp4", ".avi", ".mkv", ".mov", ".flv", ".wmv", ".webm", ".m4v", ".3gp"):
        return "video"
    if ext in (".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma", ".m4a", ".opus"):
        return "audio"
    if ext in (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".heic", ".tiff", ".tif"):
        return "photo"
    if ext in (".zip", ".rar", ".7z", ".tar", ".gz", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".txt", ".epub", ".mobi"):
        return "document"
    if ext in (".ogg", ".oga", ".opus"):
        return "voice"
    if ext in (".gif", ".webm"):
        return "animation"
    return "unknown"


# ────────────────────────────────────────────────────────
#  配置编辑 API
# ────────────────────────────────────────────────────────

def _read_config() -> dict:
    """安全读取 config.yaml，返回可序列化的 dict"""
    if not _app:
        return {}
    cfg_path = os.path.join(os.path.abspath("."), _app.config_file)
    if not os.path.isfile(cfg_path):
        return {}
    try:
        with open(cfg_path, encoding="utf-8") as f:
            return _yaml.load(f.read()) or {}
    except Exception:
        return {}


def _write_config(cfg: dict):
    """用 ruamel.yaml 写回 config.yaml（保留注释）"""
    if not _app:
        return
    cfg_path = os.path.join(os.path.abspath("."), _app.config_file)
    with open(cfg_path, "w", encoding="utf-8") as f:
        _yaml.dump(cfg, f)


@_flask_app.route("/get_config")
@login_required
def web_get_config():
    """返回 config.yaml 可编辑字段的 JSON"""
    cfg = _read_config()
    safe = {}
    # 只暴露需要编辑的字段，api_hash 脱敏
    safe["api_id"] = cfg.get("api_id", "")
    raw_hash = cfg.get("api_hash", "")
    safe["api_hash"] = raw_hash[:4] + "****" if len(raw_hash) > 4 else "****"
    safe["api_hash_set"] = bool(raw_hash)
    # bot_token 等同机器人完整控制权，与 api_hash 一致只回传掩码
    raw_token = str(cfg.get("bot_token", "") or "")
    if raw_token:
        safe["bot_token"] = raw_token[:4] + "****" if len(raw_token) > 4 else "****"
    else:
        safe["bot_token"] = ""
    safe["bot_token_set"] = bool(raw_token)
    safe["media_types"] = cfg.get("media_types", [])
    safe["web_host"] = cfg.get("web_host", "0.0.0.0")
    safe["web_port"] = cfg.get("web_port", 5000)
    safe["max_download_task"] = cfg.get("max_download_task", 5)
    safe["language"] = cfg.get("language", "EN")
    safe["web_login_secret"] = ""
    # 不返回明文密码，仅告知是否已设置（前端据此提示）
    safe["web_login_secret_set"] = bool(cfg.get("web_login_secret", ""))
    safe["web_login_enabled"] = bool(cfg.get("web_login_enabled", False))
    safe["hide_file_name"] = cfg.get("hide_file_name", False)
    safe["date_format"] = cfg.get("date_format", "%Y_%m")
    safe["chat"] = cfg.get("chat", [])
    # 文件格式
    ff = cfg.get("file_formats", {})
    safe["file_formats"] = {
        "audio": ", ".join(ff.get("audio", ["all"])),
        "video": ", ".join(ff.get("video", ["all"])),
        "document": ", ".join(ff.get("document", ["all"])),
        "photo": ", ".join(ff.get("photo", ["all"])),
    }
    return jsonify(safe)


@_flask_app.route("/save_config", methods=["POST"])
@login_required
def web_save_config():
    """保存前端提交的配置（仅更新可编辑字段，保留其他注释/字段）"""
    data = request.get_json(silent=True) or {}
    cfg = _read_config()

    # 更新可编辑字段
    if "api_id" in data:
        cfg["api_id"] = data["api_id"]
    # api_hash 只在用户真正修改时更新（跳过掩码值，防止把真实 hash 覆盖成 xxxx****）
    if "api_hash" in data and data["api_hash"] and "*" not in data["api_hash"]:
        cfg["api_hash"] = data["api_hash"]
    # bot_token 与 api_hash 相同：掩码值/空值表示未修改，不回写
    # （如需清空 bot_token，请直接编辑 config.yaml）
    if (
        "bot_token" in data
        and data["bot_token"]
        and "*" not in str(data["bot_token"])
    ):
        cfg["bot_token"] = data["bot_token"]
    if "media_types" in data:
        cfg["media_types"] = data["media_types"]
    if "web_host" in data:
        cfg["web_host"] = data["web_host"]
    if "web_port" in data:
        try:
            port = int(data["web_port"])
        except (TypeError, ValueError):
            port = 5000
        cfg["web_port"] = port if 1 <= port <= 65535 else 5000
    if "max_download_task" in data:
        try:
            mt = int(data["max_download_task"])
        except (TypeError, ValueError):
            mt = 5
        cfg["max_download_task"] = mt if 1 <= mt <= 100 else 5
    if "language" in data:
        cfg["language"] = data["language"]
    if "web_login_secret" in data:
        # 仅当用户显式输入了新密码时才更新（前端不回显明文，空值=未修改）
        new_secret = str(data["web_login_secret"] or "")
        if new_secret:
            cfg["web_login_secret"] = new_secret
    if "web_login_enabled" in data:
        cfg["web_login_enabled"] = bool(data["web_login_enabled"])
    if "hide_file_name" in data:
        cfg["hide_file_name"] = data["hide_file_name"]
    if "date_format" in data:
        cfg["date_format"] = data["date_format"]
    if "chat" in data:
        # 校验 chat 列表结构，防止非法 chat_id（非 hashable）导致启动时崩溃
        valid_chats = []
        if isinstance(data["chat"], list):
            for item in data["chat"]:
                if isinstance(item, dict) and "chat_id" in item:
                    cid = item["chat_id"]
                    if isinstance(cid, (int, str)) and str(cid).strip():
                        item["chat_id"] = cid
                        valid_chats.append(item)
        cfg["chat"] = valid_chats
    if "restart_program" in data:
        cfg["restart_program"] = data["restart_program"]
    if "file_formats" in data:
        ff = data["file_formats"]
        cfg["file_formats"] = {}
        for k in ("audio", "video", "document", "photo"):
            val = ff.get(k, "all")
            cfg["file_formats"][k] = [s.strip() for s in val.split(",") if s.strip()] or ["all"]

    _write_config(cfg)
    # 保存并重启：写盘后设置内存 restart_program 标记，主循环检测到后优雅退出
    # （走 main() 的 finally → app.update_config() 回写 last_read_message_id 游标），
    # 再由 docker（unless-stopped）重新拉起。
    # 注意：不能用 os._exit 直接杀进程——那会绕过 finally，导致游标不回写、
    # 重启后从旧位置重扫已下载文件（表现为大量「已下载,跳过下载」）。
    if data.get("restart_program") and _app:
        _app.restart_program = True
        log.info("收到「保存并重启」，已设置 restart_program，进程将优雅退出以触发容器重启...")
        return jsonify({"ok": True, "msg": "配置已保存，容器即将重启…"})
    return jsonify({"ok": True, "msg": "配置已保存，重启容器后生效"})


# ────────────────────────────────────────────────────────
#  日志查看 API
# ────────────────────────────────────────────────────────

@_flask_app.route("/get_logs")
@login_required
def web_get_logs():
    """返回最近 N 行日志"""
    from collections import deque

    try:
        lines = min(int(request.args.get("lines", 120)), 2000)
    except (TypeError, ValueError):
        lines = 120
    if not _app:
        return jsonify([])
    log_path = os.path.join(_app.log_file_path, "tdl.log")
    if not os.path.isfile(log_path):
        return jsonify([])
    try:
        # 流式保留最后 N 行，避免大日志全量 readlines 内存/IO 开销
        tail = deque(maxlen=lines)
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                tail.append(line.rstrip())
        return jsonify(list(tail))
    except Exception:
        return jsonify([])


# ────────────────────────────────────────────────────────
#  下载历史 API
# ────────────────────────────────────────────────────────

@_flask_app.route("/get_history")
@login_required
def web_get_history():
    """从数据库查询下载历史（支持搜索/筛选/排序/分页）"""
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = min(max(int(request.args.get("per_page", 30)), 1), 200)
    except (TypeError, ValueError):
        per_page = 30
    search = request.args.get("search", "").strip()
    media_type = request.args.get("media_type", "All")
    sort_by = request.args.get("sort_by", "download_timestamp")
    sort_desc = request.args.get("sort_desc", "true") == "true"
    status = request.args.get("status", "All")

    import utils.db as db_mod
    records, total = db_mod.get_history(
        page=page, per_page=per_page, search=search,
        media_type=media_type, sort_by=sort_by, sort_desc=sort_desc,
        status=status,
    )
    # 历史累计总数（全量，不受搜索/筛选影响），供历史页「已完成」统计展示
    total_all = db_mod.get_total_count()
    # hide_file_name 开启时 DB 存真实名（供去重/补录/搜索），仅在 API 输出时掩码
    hide_name = bool(_read_config().get("hide_file_name", False))

    files = []
    for r in records:
        pub_ts = r.get("publish_time") or 0
        name = r["file_name"]
        path = r.get("file_path", "")
        if hide_name:
            ext = os.path.splitext(name)[1] if name else ""
            name = f"****{ext}"
            if path:
                p, b = os.path.split(path.replace("\\", "/"))
                path = f"{p}/****{os.path.splitext(b)[1]}" if b else p
        files.append({
            "name": name,
            "path": path,
            "size": r["file_size"],
            "size_str": format_byte(r["file_size"]),
            "mtime": r["download_timestamp"],
            "mtime_str": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["download_timestamp"])),
            "chat_id": r.get("chat_id", ""),
            "media_type": r.get("media_type", ""),
            "status": r.get("status") or "success",
            "publish_time": pub_ts,
            "publish_time_str": time.strftime("%Y-%m-%d %H:%M", time.localtime(pub_ts)) if pub_ts else "",
        })

    return jsonify({"files": files, "total": total, "total_all": total_all})


@_flask_app.route("/clear_history", methods=["POST"])
@login_required
def web_clear_history():
    """清空下载历史"""
    import utils.db as db_mod
    db_mod.clear_history()
    return jsonify({"ok": True})
