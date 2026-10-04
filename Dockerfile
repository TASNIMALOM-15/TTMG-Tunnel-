FROM python:3.11-slim-bookworm

ARG TARGETARCH
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# System tools: FFmpeg (thumbnails/probe), megatools (MEGA link download), rclone (official binary)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg megatools ca-certificates curl unzip \
 && curl -fsSL "https://downloads.rclone.org/rclone-current-linux-${TARGETARCH:-amd64}.zip" -o /tmp/rclone.zip \
 && unzip -j /tmp/rclone.zip "*/rclone" -d /usr/local/bin \
 && chmod +x /usr/local/bin/rclone \
 && rm -rf /tmp/rclone.zip /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && pip install -r requirements.txt \
 && apt-get purge -y --auto-remove build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY . .

EXPOSE 8080
CMD ["sh", "/app/start.sh"]
