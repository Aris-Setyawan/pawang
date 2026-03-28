#!/bin/bash
# Generate foto + kirim ke Telegram
# Usage: generate-image.sh "<prompt>" "[caption]" [chat_id]
#
# Models (auto-fallback chain):
#   openai_gpt → gemini_native → gemini_imagen → kieai_flux → kieai_gpt4o
#
# API keys dibaca dari /root/pawang/.env

PROMPT="$1"
CAPTION="${2:-$1}"
CHAT_ID="${3:-613802669}"
ENV_FILE="/root/pawang/.env"
TG_SEND="/root/pawang/scripts/telegram-send.sh"

if [ -z "$PROMPT" ]; then
  echo "Usage: generate-image.sh <prompt> [caption] [chat_id]" >&2
  exit 1
fi

# Load API keys dari .env
GEMINI_KEY=$(grep '^GEMINI_API_KEY=' "$ENV_FILE" | cut -d= -f2-)
OPENAI_KEY=$(grep '^OPENAI_API_KEY=' "$ENV_FILE" | cut -d= -f2-)
KIEAI_KEY=$(grep '^KIEAI_API_KEY=' "$ENV_FILE" | cut -d= -f2-)
BOT_TOKEN=$(grep '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2-)

tg_msg() {
  curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    -H "Content-Type: application/json" \
    -d "{\"chat_id\":\"$CHAT_ID\",\"text\":\"$1\"}" > /dev/null
}

# Escape prompt for JSON
PROMPT_ESCAPED=$(python3 -c "import json; print(json.dumps('$PROMPT')[1:-1])" 2>/dev/null || echo "$PROMPT")

# ── Model 1: OpenAI GPT Image ────────────────────────────────────────────

gen_openai() {
  echo "[gen] OpenAI gpt-image-1..." >&2
  local OUT=/tmp/img-openai-$(date +%s).png
  [ -z "$OPENAI_KEY" ] && return 1
  local RESP=$(curl -s --max-time 90 -X POST "https://api.openai.com/v1/images/generations" \
    -H "Authorization: Bearer $OPENAI_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"gpt-image-1\",\"prompt\":\"${PROMPT_ESCAPED}\",\"size\":\"1024x1024\",\"quality\":\"low\",\"n\":1}")
  echo "$RESP" | python3 -c "
import json,sys,base64
d=json.load(sys.stdin)
items=d.get('data',[])
if items:
    b64=items[0].get('b64_json','')
    if b64:
        open('$OUT','wb').write(base64.b64decode(b64))
" 2>/dev/null
  [ -f "$OUT" ] && [ -s "$OUT" ] && echo "$OUT"
}

# ── Model 2: Gemini Native Image (gemini-2.5-flash-image) ────────────────

gen_gemini_native() {
  echo "[gen] Gemini 2.5 Flash Image (native)..." >&2
  local OUT=/tmp/img-gemini-$(date +%s).png
  [ -z "$GEMINI_KEY" ] && return 1
  local RESP=$(curl -s --max-time 90 -X POST \
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-image:generateContent?key=${GEMINI_KEY}" \
    -H "Content-Type: application/json" \
    -d "{\"contents\":[{\"parts\":[{\"text\":\"Generate an image: ${PROMPT_ESCAPED}\"}]}],\"generationConfig\":{\"responseModalities\":[\"TEXT\",\"IMAGE\"]}}")
  echo "$RESP" | python3 -c "
import json,sys,base64
d=json.load(sys.stdin)
candidates=d.get('candidates',[])
if candidates:
    parts=candidates[0].get('content',{}).get('parts',[])
    for p in parts:
        if 'inlineData' in p:
            data=p['inlineData'].get('data','')
            if data:
                open('$OUT','wb').write(base64.b64decode(data))
                break
" 2>/dev/null
  [ -f "$OUT" ] && [ -s "$OUT" ] && echo "$OUT"
}

# ── Model 3: Google Imagen 4.0 (predict API) ─────────────────────────────

gen_imagen4() {
  echo "[gen] Google Imagen 4.0..." >&2
  local OUT=/tmp/img-imagen4-$(date +%s).png
  [ -z "$GEMINI_KEY" ] && return 1
  local RESP=$(curl -s --max-time 90 -X POST \
    "https://generativelanguage.googleapis.com/v1beta/models/imagen-4.0-generate-001:predict?key=${GEMINI_KEY}" \
    -H "Content-Type: application/json" \
    -d "{\"instances\":[{\"prompt\":\"${PROMPT_ESCAPED}\"}],\"parameters\":{\"sampleCount\":1}}")
  echo "$RESP" | python3 -c "
import json,sys,base64
d=json.load(sys.stdin)
preds=d.get('predictions',[])
if preds:
    data=preds[0].get('bytesBase64Encoded','')
    if data:
        open('$OUT','wb').write(base64.b64decode(data))
" 2>/dev/null
  [ -f "$OUT" ] && [ -s "$OUT" ] && echo "$OUT"
}

# ── Model 4: kie.ai Flux Kontext ─────────────────────────────────────────

gen_flux() {
  echo "[gen] Flux Kontext (kie.ai)..." >&2
  local OUT=/tmp/img-flux-$(date +%s).jpg
  [ -z "$KIEAI_KEY" ] && return 1
  local RESP=$(curl -s --max-time 15 -X POST "https://api.kie.ai/api/v1/flux/kontext/generate" \
    -H "Authorization: Bearer $KIEAI_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"prompt\":\"${PROMPT_ESCAPED}\",\"aspectRatio\":\"1:1\",\"model\":\"flux-kontext-pro\"}")
  local TASK=$(echo "$RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('data',{}).get('taskId',''))" 2>/dev/null)
  [ -z "$TASK" ] && return 1
  for i in $(seq 1 15); do
    sleep 8
    local POLL=$(curl -s "https://api.kie.ai/api/v1/flux/kontext/record-info?taskId=$TASK" -H "Authorization: Bearer $KIEAI_KEY")
    local URL=$(echo "$POLL" | python3 -c "
import json,sys
d=json.load(sys.stdin)
r=(d.get('data') or {}).get('response') or {}
print(r.get('resultImageUrl','') or (r.get('imageList') or [''])[0])
" 2>/dev/null)
    if [ -n "$URL" ]; then
      curl -s -L "$URL" -o "$OUT"
      [ -f "$OUT" ] && [ -s "$OUT" ] && echo "$OUT" && return 0
    fi
  done
  return 1
}

# ── Model 5: kie.ai GPT-4o Image ─────────────────────────────────────────

gen_kieai_gpt4o() {
  echo "[gen] GPT-4o Image (kie.ai)..." >&2
  local OUT=/tmp/img-gpt4o-$(date +%s).png
  [ -z "$KIEAI_KEY" ] && return 1
  local RESP=$(curl -s --max-time 15 -X POST "https://api.kie.ai/api/v1/gpt4o-image/generate" \
    -H "Authorization: Bearer $KIEAI_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"prompt\":\"${PROMPT_ESCAPED}\",\"size\":\"1:1\"}")
  local TASK=$(echo "$RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('data',{}).get('taskId',''))" 2>/dev/null)
  [ -z "$TASK" ] && return 1
  for i in $(seq 1 15); do
    sleep 8
    local POLL=$(curl -s "https://api.kie.ai/api/v1/gpt4o-image/record-info?taskId=$TASK" -H "Authorization: Bearer $KIEAI_KEY")
    local URLS=$(echo "$POLL" | python3 -c "
import json,sys
d=json.load(sys.stdin)
r=(d.get('data') or {}).get('response') or {}
urls=r.get('resultUrls',[])
print(urls[0] if urls else '')
" 2>/dev/null)
    if [ -n "$URLS" ]; then
      curl -s -L "$URLS" -o "$OUT"
      [ -f "$OUT" ] && [ -s "$OUT" ] && echo "$OUT" && return 0
    fi
  done
  return 1
}

# ── Auto-fallback chain ──────────────────────────────────────────────────

echo "[img] Starting image generation..." >&2

for MODEL_FN in gen_openai gen_gemini_native gen_imagen4 gen_flux gen_kieai_gpt4o; do
  RESULT=$($MODEL_FN)
  if [ -n "$RESULT" ] && [ -f "$RESULT" ] && [ -s "$RESULT" ]; then
    bash "$TG_SEND" "$RESULT" "$CAPTION" "$CHAT_ID"
    echo "Image generated and sent to Telegram. File: $RESULT"
    exit 0
  fi
  echo "[img] $MODEL_FN failed, trying next..." >&2
done

tg_msg "Semua model gagal generate gambar. Silakan coba lagi nanti."
echo "ERROR: Semua model gagal" >&2
exit 1
