#!/bin/sh
set -eu

export RCLONE_CONFIG=/tmp/rclone.conf
export RCLONE_TEMP_DIR="${RCLONE_TEMP_DIR:-/tmp/rclone-tmp}"
mkdir -p "$RCLONE_TEMP_DIR"
: > "$RCLONE_CONFIG"

# ---------- Google Drive ----------
if [ -n "${GDRIVE_TOKEN:-}" ] || [ -n "${GDRIVE_SA_B64:-}" ]; then
  {
    echo "[gdrive]"
    echo "type = drive"
    echo "scope = drive"
    if [ -n "${GDRIVE_CLIENT_ID:-}" ]; then echo "client_id = $GDRIVE_CLIENT_ID"; fi
    if [ -n "${GDRIVE_CLIENT_SECRET:-}" ]; then echo "client_secret = $GDRIVE_CLIENT_SECRET"; fi
    if [ -n "${GDRIVE_ROOT_FOLDER_ID:-}" ]; then echo "root_folder_id = $GDRIVE_ROOT_FOLDER_ID"; fi
    if [ -n "${GDRIVE_TEAM_DRIVE_ID:-}" ]; then echo "team_drive = $GDRIVE_TEAM_DRIVE_ID"; fi
    if [ -n "${GDRIVE_TOKEN:-}" ]; then echo "token = $GDRIVE_TOKEN"; fi
    if [ -n "${GDRIVE_SA_B64:-}" ]; then
      echo "$GDRIVE_SA_B64" | base64 -d > /tmp/sa.json
      echo "service_account_file = /tmp/sa.json"
    fi
  } >> "$RCLONE_CONFIG"
  echo "[start.sh] Google Drive remote configured"
else
  echo "[start.sh] WARNING: GDRIVE_TOKEN / GDRIVE_SA_B64 not set - Drive disabled"
fi

# ---------- MEGA ----------
if [ -n "${MEGA_EMAIL:-}" ] && [ -n "${MEGA_PASSWORD:-}" ]; then
  OBSCURED="$(rclone obscure "$MEGA_PASSWORD")"
  {
    echo "[mega]"
    echo "type = mega"
    echo "user = $MEGA_EMAIL"
    echo "pass = $OBSCURED"
  } >> "$RCLONE_CONFIG"
  if [ "${MEGA_LOGIN_FOR_DOWNLOAD:-false}" = "true" ]; then
    {
      echo "[Login]"
      echo "Username = $MEGA_EMAIL"
      echo "Password = $MEGA_PASSWORD"
    } > /tmp/.megarc
    chmod 600 /tmp/.megarc
  fi
  echo "[start.sh] MEGA remote configured"
else
  echo "[start.sh] WARNING: MEGA_EMAIL / MEGA_PASSWORD not set - MEGA upload disabled"
fi

chmod 600 "$RCLONE_CONFIG"
exec python bot.py
