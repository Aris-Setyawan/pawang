#!/bin/bash
# Kirim file/gambar ke Telegram
# Usage: telegram-send.sh <file_path> [caption] [chat_id]

FILE="$1"
CAPTION="${2:-}"
CHAT_ID="${3:-613802669}"

# Load dari .env Pawang
ENV_FILE="/root/pawang/.env"
BOT_TOKEN=$(grep '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" 2>/dev/null | cut -d= -f2-)

if [ -z "$BOT_TOKEN" ]; then
  echo "Error: TELEGRAM_BOT_TOKEN not found in $ENV_FILE" >&2
  exit 1
fi

if [ ! -f "$FILE" ]; then
  echo "Error: file tidak ditemukan: $FILE" >&2
  exit 1
fi

MIME=$(file --mime-type -b "$FILE")

if [[ "$MIME" == image/* ]]; then
  RESULT=$(curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendPhoto" \
    -F "chat_id=${CHAT_ID}" \
    -F "photo=@${FILE}" \
    -F "caption=${CAPTION}")
elif [[ "$MIME" == video/* ]]; then
  RESULT=$(curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendVideo" \
    -F "chat_id=${CHAT_ID}" \
    -F "video=@${FILE}" \
    -F "caption=${CAPTION}" \
    -F "supports_streaming=true")
elif [[ "$MIME" == audio/* ]] || [[ "$FILE" == *.mp3 ]] || [[ "$FILE" == *.wav ]] || [[ "$FILE" == *.ogg ]]; then
  RESULT=$(curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendAudio" \
    -F "chat_id=${CHAT_ID}" \
    -F "audio=@${FILE}" \
    -F "caption=${CAPTION}")
else
  RESULT=$(curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendDocument" \
    -F "chat_id=${CHAT_ID}" \
    -F "document=@${FILE}" \
    -F "caption=${CAPTION}")
fi

OK=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print('ok' if d.get('ok') else d.get('description','error'))")
echo "$OK"
