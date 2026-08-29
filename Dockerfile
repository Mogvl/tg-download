FROM python:3.11.9-alpine AS build

WORKDIR /app

# Build deps for pip packages that need compilation
RUN apk add --no-cache --virtual .build-deps gcc musl-dev

# Install python deps
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Install rclone (runtime binary)
RUN apk add --no-cache rclone


FROM python:3.11.9-alpine AS runtime

WORKDIR /app

# 运行时依赖：TLS 根证书 + 健康检查工具 + 时区数据（TZ 生效必需）
RUN apk add --no-cache ca-certificates wget tzdata && update-ca-certificates

# 日志直出 stdout/stderr（docker logs 实时可见，不被 Python 缓冲延迟）
ENV PYTHONUNBUFFERED=1

EXPOSE 5000

# Copy installed deps from build stage
COPY --from=build /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages

# Copy rclone to the path expected by the app (matches code default: ./rclone/rclone)
COPY --from=build /usr/bin/rclone /app/rclone/rclone

# Copy app source code
COPY . /app

# 预建运行时目录（挂载卷会覆盖，这里仅保证镜像内存在）
RUN mkdir -p /app/downloads /app/sessions /app/log /app/temp

# 注意：保持以 root 运行。
# config.yaml / data.yaml / sessions 等通过宿主机卷挂载进入容器，文件属主是宿主用户；
# 若切到非 root 用户（USER app），容器用户对宿主挂载文件没有读写权限，
# 启动即报 PermissionError: '/app/config.yaml'，导致 restart 无限重启循环。
# （已实测验证，见 2026-08-25 19:39 日志）

CMD ["python", "media_downloader.py"]
