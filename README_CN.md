
<h1 align="center">电报资源下载</h1>

<p align="center">
<a href="https://github.com/tangyoha/telegram_media_downloader/actions"><img alt="Unittest" src="https://github.com/tangyoha/telegram_media_downloader/workflows/Unittest/badge.svg"></a>
<a href="https://codecov.io/gh/tangyoha/telegram_media_downloader"><img alt="Coverage Status" src="https://codecov.io/gh/tangyoha/telegram_media_downloader/branch/master/graph/badge.svg"></a>
<a href="https://github.com/tangyoha/telegram_media_downloader/blob/master/LICENSE"><img alt="License: MIT" src="https://black.readthedocs.io/en/stable/_static/license.svg"></a>
<a href="https://github.com/python/black"><img alt="Code style: black" src="https://img.shields.io/badge/code%20style-black-000000.svg"></a>
<a href="https://github.com/tangyoha/telegram_media_downloader/releases">
<img alt="Code style: black" src="https://img.shields.io/github/v/release/tangyoha/telegram_media_downloader?display_name=tag">
</a>
</p>

<h3 align="center">
  <a href="./README.md">English</a><span> · </span>
  <a href="https://github.com/tangyoha/telegram_media_downloader/discussions/categories/ideas">新功能请求</a>
  <span> · </span>
  <a href="https://github.com/tangyoha/telegram_media_downloader/issues">报告bug</a>
  <span> · </span>
  帮助: <a href="https://github.com/tangyoha/telegram_media_downloader/discussions">讨论</a>
  <span> & </span>
  <a href="https://t.me/TeegramMediaDownload">电报讨论群</a>
</h3>

## 概述

> 支持两种默认运行

* 机器人运行，从机器人下发命令`下载`或者`转发`

* 作为一个一次性的下载工具下载

### 界面

#### 网页

> 运行后打开浏览器访问`localhost:5000`
> 如果是远程机器需要配置web_host: 0.0.0.0


<img alt="Code style: black" style="width:100%; high:60%;" src="./screenshot/web_ui.gif"/>

### 机器人

> 需要配置bot_token,具体参考[文档](https://github.com/tangyoha/telegram_media_downloader/wiki/%E5%A6%82%E4%BD%95%E4%BD%BF%E7%94%A8%E6%9C%BA%E5%99%A8%E4%BA%BA%E4%B8%8B%E8%BD%BD)


<img alt="Code style: black" style="width:60%; high:30%; " src="./screenshot/bot.gif"/>

### 支持

| 类别         | 支持                                     |
| ------------ | ---------------------------------------- |
| 语言         | `Python 3.7` 及以上                      |
| 下载媒体类型 | 音频、文档、照片、视频、video_note、语音 |

### 版本发布计划

* [v2.2.0](https://github.com/tangyoha/telegram_media_downloader/issues/2)

## 安装

对于具有 `make` 可用性的 *nix 操作系统发行版

```sh
git clone https://github.com/tangyoha/telegram_media_downloader.git
cd telegram_media_downloader
make install
```

对于没有内置 `make` 的 Windows

```sh
git clone https://github.com/tangyoha/telegram_media_downloader.git
cd telegram_media_downloader
pip3 install -r requirements.txt
```

## 绿联 NAS (UGREEN) 一键部署

本仓库提供 `docker-compose.yml`，可在绿联 Docker「项目 / Compose」中一键部署。
镜像为本项目自动构建的 `ghcr.io/mogvl/tg-download:latest`（push 到 main 后由 GitHub Actions 自动构建发布）。

### docker-compose.yml 内容

可直接复制以下内容保存为 `docker-compose.yml`：

```yaml
version: "3.8"

services:
  tg-download:
    # 本项目自动构建的镜像（push 到 main 后由 GitHub Actions 发布）
    image: ghcr.io/mogvl/tg-download:latest
    # 如需基于源码本地构建，注释掉上面的 image 行，并取消下面 build 行的注释：
    # build: .
    container_name: tg-download
    ports:
      # Web UI 端口映射（外部端口:容器内 5000）
      - "14087:5000"
    environment:
      - TZ=Asia/Shanghai
    volumes:
      # 首次部署前，必须先执行：
      #   cp config.example.yaml config.yaml  并填入 api_id / api_hash
      #   cp data.example.yaml data.yaml
      # 注意：config.yaml / data.yaml 文件必须存在，否则 Docker 会把它们挂载成目录导致启动失败
      - /volume1/docker/tg-download/config.yaml:/app/config.yaml
      - /volume1/docker/tg-download/data.yaml:/app/data.yaml

      # 下载文件存储目录（持久化）
      - /volume1/dockerdn/tg:/app/downloads

      # Telegram 登录会话（持久化，重启不丢失）
      - ./sessions:/app/sessions

      # 日志与临时文件
      - ./log:/app/log
      - ./temp:/app/temp

      # 如需 rclone 上传云盘，取消下行注释并放入 rclone 配置
      # - ./rclone/:/root/.config/rclone/
    restart: unless-stopped
```

### 部署前准备### 部署前准备（必做）

容器启动时会读取 `config.yaml`，**必须先准备好配置文件**，否则会启动失败：

1. 将项目下载到 NAS 的一个目录中（如 `/volume1/docker/tg-download/`）。
2. 复制并填写配置：

```sh
cp config.example.yaml config.yaml   # 编辑并填入 api_id / api_hash / chat
cp data.example.yaml data.yaml
mkdir -p downloads sessions log temp
```

> 获取 api_id/api_hash：访问 <https://my.telegram.org/apps> 注册应用。
> 注意：`config.yaml`、`data.yaml` 必须以**文件**形式存在；如果缺失，Docker 挂载时会创建同名目录导致启动失败。

### 一键部署步骤

1. 在绿联 Docker 中选择 **项目 / Compose → 创建项目**，导入仓库中的 `docker-compose.yml`。
2. 点击 **部署 / 启动**，容器会自动拉取 `ghcr.io/mogvl/tg-download:latest` 镜像。
3. 首次登录 Telegram 账号：SSH 进入项目目录执行

   ```sh
   docker compose run --rm tg-download
   ```

   按提示输入手机号和验证码，登录成功后 `Ctrl+C` 退出（会话已保存在 `sessions/`）。

4. 后台启动：`docker compose up -d`
5. 浏览器访问 `http://<绿联 IP>:14087` 查看/控制下载进度。
   Web UI 仅用于查看下载进度与暂停/继续，不支持在网页里配置 api_id 或登录 Telegram；
   建议在 `config.yaml` 中设置 `web_login_secret` 作为界面登录密码。

### 更新

```sh
cd /volume1/docker/tg-download
docker compose down
git pull
docker compose up -d
```

### 数据说明

- `downloads/`：下载的媒体文件（持久化）
- `sessions/`：Telegram 登录会话（重启不丢失）
- `config.yaml` / `data.yaml`：配置与应用数据

---
## Docker容器
> 更详细安装教程请查看wiki

确保安装了 **docker** 和 **docker-compose**
```sh
docker pull ghcr.io/mogvl/tg-download:latest
mkdir -p ~/app && mkdir -p ~/app/log/ && cd ~/app
wget https://raw.githubusercontent.com/Mogvl/tg-download/main/docker-compose.yml -O docker-compose.yml
wget https://raw.githubusercontent.com/Mogvl/tg-download/main/config.example.yaml -O config.yaml
wget https://raw.githubusercontent.com/Mogvl/tg-download/main/data.example.yaml -O data.yaml
# vi config.yaml 填入 api_id / api_hash / chat

# 第一次需要前台启动
# 输入你的电话号码和验证码，然后退出(ctrl + c)
docker compose run --rm tg-download

# 执行完以上操作后，后面的所有启动都在后台启动
docker compose up -d

＃ 升级
docker pull ghcr.io/mogvl/tg-download:latest
cd ~/app
docker compose down
docker compose up -d
```

## 升级安装

```sh
cd telegram_media_downloader
pip3 install -r requirements.txt
```

## 配置

所有配置都通过 config.yaml 文件传递​​给 `Telegram Media Downloader`。

**获取您的 API 密钥：**
第一步需要您获得有效的 Telegram API 密钥（API id/hash pair）：

1. 访问 [https://my.telegram.org/apps](https://my.telegram.org/apps) 并使用您的 Telegram 帐户登录。
2. 填写表格以注册新的 Telegram 应用程序。
3. 完成！ API 密钥由两部分组成：**api_id** 和**api_hash**。

**获取聊天ID：**
> 如果你需要下载收藏夹的内容请填`me`

**1。使用网络电报：**

1. 打开 <https://web.telegram.org/?legacy=1#/im>
2. 现在转到聊天/频道，您将看到 URL 类似

- `https://web.telegram.org/?legacy=1#/im?p=u853521067_2449618633394` 这里 `853521067` 是聊天 ID。
- `https://web.telegram.org/?legacy=1#/im?p=@somename` 这里的 `somename` 是聊天 ID。
- `https://web.telegram.org/?legacy=1#/im?p=s1301254321_6925449697188775560` 此处取 `1301254321` 并将 `-100` 添加到 id => `-1001301254321` 的开头。
- `https://web.telegram.org/?legacy=1#/im?p=c1301254321_6925449697188775560` 此处取 `1301254321` 并将 `-100` 添加到 id => `-1001301254321` 的开头。

**2。使用机器人：**
1.使用[@username_to_id_bot](https://t.me/username_to_id_bot)获取chat_id
    - 几乎所有电报用户：将用户名发送给机器人或将他们的消息转发给机器人
    - 任何聊天：发送聊天用户名或复制并发送其加入聊天链接到机器人
    - 公共或私人频道：与聊天相同，只需复制并发送给机器人
    - 任何电报机器人的 ID

### 配置文件

```yaml
api_hash: your_api_hash
api_id: your_api_id
bot_token: your_bot_token
chat:
- chat_id: telegram_chat_id
  last_read_message_id: 0
  download_filter: message_date >= 2022-12-01 00:00:00 and message_date <= 2023-01-17 00:00:00
- chat_id: telegram_chat_id_2
  last_read_message_id: 0
# 我们将ids_to_retry移到data.yaml
ids_to_retry: []
media_types:
- audio
- document
- photo
- video
- voice
- animation #gif
file_formats:
  audio:
  - all
  document:
  - pdf
  - epub
  video:
  - mp4
save_path: D:\telegram_media_downloader
file_path_prefix:
- chat_title
- media_datetime
upload_drive:
  enable_upload_file: true
  remote_dir: drive:/telegram
  before_upload_file_zip: True
  after_upload_file_delete: True
hide_file_name: true
file_name_prefix:
- message_id
- file_name
file_name_prefix_split: ' - '
max_download_task: 5
web_host: 127.0.0.1
web_port: 5000
web_login_secret: 123
allowed_user_ids:
- 'me'
date_format: '%Y_%m'
enable_download_txt: false
```

- **api_hash** - 你从电报应用程序获得的 api_hash
- **api_id** - 您从电报应用程序获得的 api_id
- **bot_token** - 你的机器人凭证
- **chat** -  多频道
  - `chat_id` -  您要下载媒体的聊天/频道的 ID。你从上述步骤中得到的。
  - `download_filter` - 下载过滤器, 查阅 [如何使用过滤器](https://github.com/tangyoha/telegram_media_downloader/wiki/%E5%A6%82%E4%BD%95%E4%BD%BF%E7%94%A8%E8%BF%87%E6%BB%A4%E5%99%A8)
  - `last_read_message_id` -如果这是您第一次阅读频道，请将其设置为“0”，或者如果您已经使用此脚本下载媒体，它将有一些数字，这些数字会在脚本成功执行后自动更新。不要改变它。
- **chat_id** - 您要下载媒体的聊天/频道的 ID。你从上述步骤中得到的。
- **last_read_message_id** - 如果这是您第一次阅读频道，请将其设置为“0”，或者如果您已经使用此脚本下载媒体，它将有一些数字，这些数字会在脚本成功执行后自动更新。不要改变它。
- **ids_to_retry** - `保持原样。`下载器脚本使用它来跟踪所有跳过的下载，以便在下次执行脚本时可以下载它。
- **media_types** - 要下载的媒体类型，您可以更新要下载的媒体类型，它可以是一种或任何可用类型。
- **file_formats** - 为支持的媒体类型（“音频”、“文档”和“视频”）下载的文件类型。默认格式为“all”，下载所有文件。
- **save_path** - 你想存储下载文件的根目录
- **file_path_prefix** - 存储文件子文件夹，列表的顺序不定，可以随机组合
  - `chat_title`      - 聊天频道或者群组标题, 如果找不到标题则为配置文件中的`chat_id`
  - `media_datetime`  - 资源的发布时间
  - `media_type`      - 资源类型，类型查阅 `media_types`
- **upload_drive** - 您可以将文件上传到云盘
  - `enable_upload_file` - [必填]启用上传文件，默认为`false`
  - `remote_dir` - [必填]你上传的地方
  - `upload_adapter` - [必填]上传文件适配器，可以为`rclone`,`aligo`。如果为`rclone`，则支持rclone所有支持上传的服务器，如果为aligo，则支持上传阿里云盘
  - `rclone_path`，如果配置`upload_adapter`为`rclone`则为必填，`rclone`的可执行目录，查阅 [如何使用rclone](https://github.com/tangyoha/telegram_media_downloader/wiki/Rclone)
  - `before_upload_file_zip` - 上传前压缩文件，默认为`false`
  - `after_upload_file_delete` - 上传成功后删除文件，默认为`false`
- **file_name_prefix** - 自定义文件名称,使用和 **file_path_prefix** 一样
  - `message_id` - 消息id
  - `file_name` - 文件名称（可能为空）
  - `caption` - 消息的标题（可能为空）
- **file_name_prefix_split** - 自定义文件名称分割符号，默认为` - `
- **max_download_task** - 最大任务下载任务个数，默认为5个。
- **hide_file_name** - 是否隐藏web界面文件名称，默认`false`
- **web_host** - web界面地址
- **web_port** - web界面端口
- **language** - 应用语言，默认为英文(`EN`),可选`ZH`（中文）,`RU`,`UA`
- **web_login_secret** - 网页登录密码，如果不配置则访问网页不需要登录
- **log_level** - 默认日志等级，请参阅 `logging._nameToLevel`
- **forward_limit** - 限制每分钟转发次数，默认为33，默认请不要修改该参数
- **allowed_user_ids** - 允许哪些人使用机器人，默认登录账号可以使用，带@的名称请加单引号
- **date_format** - 支持自定义配置file_path_prefix中media_datetime的格式，具体格式查看 [python-datetime](https://docs.python.org/zh-cn/3/library/time.html)
- **enable_download_txt** 启用下载txt文件，默认`false`

## 执行

```sh
python3 media_downloader.py
```

所有下载的媒体都将存储在`save_path`根目录下。
具体位置参考如下：

```yaml
file_path_prefix:
  - chat_title
  - media_datetime
  - media_type
```

视频下载完整目录为：`save_path`/`chat_title`/`media_datetime`/`media_type`。
列表的顺序不定，可以随机组合。
如果配置为空，则所有文件保存在`save_path`下。

## 代理

该项目目前支持 socks4、socks5、http 代理。要使用它，请将以下内容添加到`config.yaml`文件的底部

```yaml
proxy:
  scheme: socks5
  hostname: 127.0.0.1
  port: 1234
  username: 你的用户名（无则删除该行）
  password: 你的密码（无则删除该行）
```

如果您的代理不需要授权，您可以省略用户名和密码。然后代理将自动启用。

## 贡献

### 贡献指南

通读我们的[贡献指南](./CONTRIBUTING.md)，了解我们的提交流程、编码规则等。

### 想帮忙？

想要提交错误、贡献一些代码或改进文档？出色的！阅读我们的 [贡献指南](./CONTRIBUTING.md)。

### 行为守则

帮助我们保持 Telegram Media Downloader 的开放性和包容性。请阅读并遵守我们的[行为准则](./CODE_OF_CONDUCT.md)。


### 赞助

<p>
<img alt="Code style: black" style="width:30%" src="./screenshot/alipay.JPG">
<img alt="Code style: black" style="width:30%" src="./screenshot/wechat.JPG">
</p>
