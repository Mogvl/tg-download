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

# 运行时依赖：TLS 根证书（Telegram/HTTPS 必需）+ 健康检查工具
RUN apk add --no-cache ca-certificates wget && update-ca-certificates

# Copy installed deps from build stage
COPY --from=build /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages

# Copy rclone to the path expected by the app (matches code default: ./rclone/rclone)
COPY --from=build /usr/bin/rclone /app/rclone/rclone

# Copy app source code
COPY . /app

# 非 root 运行，降低安全风险
RUN addgroup -S app && adduser -S app -G app \
    && mkdir -p /app/downloads /app/sessions /app/log /app/temp \
    && chown -R app:app /app

USER app

CMD ["python", "media_downloader.py"]
