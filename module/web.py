"""web ui for media download"""

import glob
import logging
import os
import secrets
import threading
import time

from flask import Flask, jsonify, render_template, request
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
web_login_users: dict = {}
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
def init_web(app: Application):
    """
    Set the value of the users variable.

    Args:
        users: The list of users to set.

    Returns:
        None.
    """
    global web_login_users, _app
    _app = app
    if app.web_login_secret:
        _flask_app.secret_key = app.web_login_secret
        web_login_users = {"root": app.web_login_secret}
    else:
        _flask_app.config["LOGIN_DISABLED"] = True
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
        if username in web_login_users and web_login_users[username] == password:
            user = User()
            login_user(user)
            return jsonify({"code": "1"})

        return jsonify({"code": "0"})

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
    return (
        '{ "download_speed" : "'
        + format_byte(get_total_download_speed())
        + '/s" , "upload_speed" : "0.00 B/s" } '
    )


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
def get_app_version():
    """Get telegram_media_downloader version"""
    return utils.__version__


@_flask_app.route("/get_download_list")
@login_required
def get_download_list():
    """get download list"""
    if request.args.get("already_down") is None:
        return "[]"

    already_down = request.args.get("already_down") == "true"

    download_result = get_download_result()
    result = "["
    for chat_id, messages in download_result.items():
        for idx, value in messages.items():
            is_already_down = value["down_byte"] == value["total_size"]

            if already_down and not is_already_down:
                continue

            if result != "[":
                result += ","
            download_speed = format_byte(value["download_speed"]) + "/s"
            result += (
                '{ "chat":"'
                + f"{chat_id}"
                + '", "id":"'
                + f"{idx}"
                + '", "filename":"'
                + os.path.basename(value["file_name"])
                + '", "total_size":"'
                + f'{format_byte(value["total_size"])}'
                + '" ,"download_progress":"'
            )
            result += (
                f'{round(value["down_byte"] / value["total_size"] * 100, 1)}'
                + '" ,"download_speed":"'
                + download_speed
                + '" ,"save_path":"'
                + value["file_name"].replace("\\", "/")
                + '"}'
            )

    result += "]"
    return result


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
    safe["bot_token"] = cfg.get("bot_token", "")
    safe["media_types"] = cfg.get("media_types", [])
    safe["web_host"] = cfg.get("web_host", "0.0.0.0")
    safe["web_port"] = cfg.get("web_port", 5000)
    safe["max_download_task"] = cfg.get("max_download_task", 5)
    safe["language"] = cfg.get("language", "EN")
    safe["web_login_secret"] = cfg.get("web_login_secret", "")
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
    # api_hash 只在用户真正修改时更新（前端传回 masked 值时跳过）
    if "api_hash" in data and data["api_hash"] != "****" and len(data["api_hash"]) > 4:
        cfg["api_hash"] = data["api_hash"]
    if "bot_token" in data:
        cfg["bot_token"] = data["bot_token"]
    if "media_types" in data:
        cfg["media_types"] = data["media_types"]
    if "web_host" in data:
        cfg["web_host"] = data["web_host"]
    if "web_port" in data:
        cfg["web_port"] = int(data["web_port"]) if data["web_port"] else 5000
    if "max_download_task" in data:
        cfg["max_download_task"] = int(data["max_download_task"]) if data["max_download_task"] else 5
    if "language" in data:
        cfg["language"] = data["language"]
    if "web_login_secret" in data:
        cfg["web_login_secret"] = data["web_login_secret"]
    if "hide_file_name" in data:
        cfg["hide_file_name"] = data["hide_file_name"]
    if "date_format" in data:
        cfg["date_format"] = data["date_format"]
    if "chat" in data:
        cfg["chat"] = data["chat"]
    if "file_formats" in data:
        ff = data["file_formats"]
        cfg["file_formats"] = {}
        for k in ("audio", "video", "document", "photo"):
            val = ff.get(k, "all")
            cfg["file_formats"][k] = [s.strip() for s in val.split(",") if s.strip()] or ["all"]

    _write_config(cfg)
    return jsonify({"ok": True, "msg": "配置已保存，重启容器后生效"})


# ────────────────────────────────────────────────────────
#  日志查看 API
# ────────────────────────────────────────────────────────

@_flask_app.route("/get_logs")
@login_required
def web_get_logs():
    """返回最近 N 行日志"""
    lines = int(request.args.get("lines", 120))
    if not _app:
        return jsonify([])
    log_path = os.path.join(_app.log_file_path, "tdl.log")
    if not os.path.isfile(log_path):
        return jsonify([])
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        tail = all_lines[-lines:]
        return jsonify([l.rstrip() for l in tail])
    except Exception:
        return jsonify([])


# ────────────────────────────────────────────────────────
#  下载历史 API
# ────────────────────────────────────────────────────────

@_flask_app.route("/get_history")
@login_required
def web_get_history():
    """扫描 downloads/ 目录，返回文件列表（按修改时间倒序）"""
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 30))
    search = request.args.get("search", "").strip().lower()

    if not _app:
        return jsonify({"files": [], "total": 0})

    base = _app.save_path
    if not os.path.isdir(base):
        return jsonify({"files": [], "total": 0})

    # 收集所有文件（排除 .session 等临时文件）
    files = []
    for fpath in glob.glob(os.path.join(base, "**", "*"), recursive=True):
        if not os.path.isfile(fpath):
            continue
        name = os.path.basename(fpath)
        if name.startswith(".") or name.endswith(".session"):
            continue
        if search and search not in name.lower():
            continue
        stat = os.stat(fpath)
        rel = os.path.relpath(fpath, base)
        files.append({
            "name": name,
            "path": rel,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
        })

    # 按修改时间倒序
    files.sort(key=lambda x: x["mtime"], reverse=True)
    total = len(files)
    start = (page - 1) * per_page
    page_files = files[start : start + per_page]

    # 格式化
    for f in page_files:
        f["size_str"] = format_byte(f["size"])
        f["mtime_str"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(f["mtime"]))

    return jsonify({"files": page_files, "total": total})
