<h1 align="center">Telegram Media Downloader - 绿联 Docker 版</h1>

<p align="center">
基于 <a href="https://github.com/tangyoha/telegram_media_downloader">telegram_media_downloader</a> 的绿联 NAS Docker 一键部署版本
（镜像：<code>ghcr.io/mogvl/tg-download:latest</code>，push 自动构建）
</p>

<p align="center">
<a href="https://github.com/Mogvl/tg-download"><img alt="GitHub" src="https://img.shields.io/badge/GitHub-Mogvl%2Ftg--download-blue"></a>
<a href="https://github.com/tangyoha/telegram_media_downloader/blob/master/LICENSE"><img alt="License: MIT" src="https://black.readthedocs.io/en/stable/_static/license.svg"></a>
</p>

---

## 功能

- 📥 下载 Telegram 频道/群组/私聊中的媒体文件（音频、视频、图片、文档等）
- 🌐 Web UI 管理界面（端口 `14087`）
- 🤖 支持 Telegram Bot 机器人交互
- 📁 自动按频道/日期分类存储
- ☁️ 支持 Rclone 上传到云盘

---

## 绿联 Docker 部署指南

### 方式一：SSH 命令行部署（推荐）

#### 1. SSH 登录绿联 NAS

通过 SSH 登录你的绿联 NAS 终端。

#### 2. 创建项目目录

```bash
mkdir -p /volume1/docker/tg-download
cd /volume1/docker/tg-download
```

#### 3. 下载项目文件

```bash
git clone https://github.com/Mogvl/tg-download.git .
```

或者手动创建以下文件：

- `docker-compose.yml`（仓库自带）
- `config.yaml`（由 `config.example.yaml` 复制而来）
- `data.yaml`（由 `data.example.yaml` 复制而来）

`docker-compose.yml` 内容：

```yaml
version: "3.8"

services:
  tg-download:
    image: ghcr.io/mogvl/tg-download:latest
    container_name: tg-download
    ports:
      - "14087:5000"
    environment:
      - TZ=Asia/Shanghai
    volumes:
      - /volume1/docker/tg-download/config.yaml:/app/config.yaml
      - /volume1/docker/tg-download/data.yaml:/app/data.yaml
      - /volume1/dockerdn/tg:/app/downloads
      - ./sessions:/app/sessions
      - ./log:/app/log
      - ./temp:/app/temp
    restart: unless-stopped
```

#### 4. 修改配置文件

```bash
cp config.example.yaml config.yaml
cp data.example.yaml data.yaml
```

编辑 `config.yaml`，填入你的 Telegram API 信息：

```yaml
api_hash: 你的api_hash
api_id: 你的api_id
chat:
- chat_id: 你的聊天ID
  last_read_message_id: 0
```

#### 5. 创建必要目录

```bash
mkdir -p /volume1/dockerdn/tg /volume1/docker/tg-download/sessions /volume1/docker/tg-download/log /volume1/docker/tg-download/temp
```

#### 6. 首次启动（前台，用于登录 Telegram 账号）

```bash
docker compose run --rm telegram_media_downloader
```

按提示输入你的手机号和验证码，登录成功后按 `Ctrl+C` 退出。

#### 7. 后台启动服务

```bash
docker compose up -d
```

#### 8. 访问 Web UI

打开浏览器访问：`http://你的NAS IP:14087`

---

### 方式二：绿联 Docker 图形界面部署

#### 1. 下载项目文件

将以下文件下载到 NAS 的一个目录中（如 `/volume1/docker/tg-download/`）：

| 文件 | 说明 |
|------|------|
| `docker-compose.yml` | Docker Compose 配置 |
| `config.example.yaml` | 配置模板（复制为 `config.yaml` 后填写） |
| `data.example.yaml` | 数据文件模板（复制为 `data.yaml`） |

#### 2. 准备配置文件

```bash
cp config.example.yaml config.yaml
cp data.example.yaml data.yaml
```

然后编辑 `config.yaml`，填入你的 Telegram API 信息（获取方式见下方）。

#### 3. 在绿联 Docker 中导入

1. 打开绿联 Docker 管理界面
2. 选择「Compose」或「导入」
3. 选择你的项目目录
4. 点击部署

#### 4. 首次登录 Telegram

在绿联 SSH 终端进入项目目录，前台启动一次完成账号登录：

```bash
docker compose run --rm tg-download
```

按提示输入手机号和验证码，登录成功后 `Ctrl+C` 退出（会话保存在 `sessions/`），再执行 `docker compose up -d` 后台运行。

> Web UI 仅用于查看下载进度与暂停/继续，不支持在网页里配置 api_id 或登录 Telegram。

---

## 获取 Telegram API 密钥

1. 访问 [https://my.telegram.org/apps](https://my.telegram.org/apps)
2. 使用你的 Telegram 账号登录
3. 创建应用，获取 `api_id` 和 `api_hash`

## 获取聊天 ID

**方法一：使用 Web Telegram**

1. 打开 [https://web.telegram.org](https://web.telegram.org)
2. 进入目标聊天/频道，URL 中的数字即为 chat_id
   - 私聊：`https://web.telegram.org/#/im?p=u123456789` → chat_id 为 `123456789`
   - 频道：`https://web.telegram.org/#/im?p=@channelname` → chat_id 为 `@channelname`
   - 超级群组：在 chat_id 前加 `-100`

**方法二：使用 Bot**

使用 [@username_to_id_bot](https://t.me/username_to_id_bot) 获取 chat_id。

---

## 目录结构

```
tg-download/
├── docker-compose.yml      # Docker Compose 配置
├── config.example.yaml     # 配置模板
├── data.example.yaml       # 数据文件模板
├── config.yaml             # 应用配置（自行复制创建）
├── data.yaml               # 数据文件（自行复制创建）
├── downloads/              # 下载文件存储
├── log/                    # 日志文件
├── sessions/               # Telegram 会话文件
├── temp/                   # 临时文件
└── rclone/                 # Rclone 配置（可选）
```

---

## 配置说明

| 配置项 | 说明 |
|--------|------|
| `api_hash` | Telegram API Hash |
| `api_id` | Telegram API ID |
| `chat[].chat_id` | 要下载的聊天/频道 ID |
| `web_host` | Web UI 监听地址（默认 `0.0.0.0`） |
| `web_port` | Web UI 端口（默认 `5000`） |
| `web_login_secret` | Web UI 登录密码 |
| `language` | 语言（`EN`/`ZH`） |
| `max_download_task` | 最大并发下载数 |

---

## 代理设置

如果需要代理，在 `config.yaml` 中添加：

```yaml
proxy:
  scheme: socks5
  hostname: 你的代理地址
  port: 端口号
  username: 用户名（无则删除）
  password: 密码（无则删除）
```

---

## 更新

```bash
cd /volume1/docker/tg-download
git pull
docker compose down
docker compose up -d
```

---

## 致谢

- [telegram_media_downloader](https://github.com/tangyoha/telegram_media_downloader) - 原项目

---

## License

MIT
