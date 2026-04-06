#!/bin/bash
# Generate video + kirim ke Telegram
# Usage: generate-video.sh "<prompt>" "[caption]" [chat_id]
#
# Models (auto-fallback): veo3f → runway → kling3 → hailuo → google_veo
# API keys dari /root/pawang/.env

PROMPT="$1"
CAPTION="${2:-Video AI}"
CHAT_ID="${3:-613802669}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/../.env"
TG_SEND="${SCRIPT_DIR}/telegram-send.sh"

if [ -z "$PROMPT" ]; then
  echo "Usage: generate-video.sh <prompt> [caption] [chat_id]" >&2
  exit 1
fi

KIEAI_KEY=$(grep '^KIEAI_API_KEY=' "$ENV_FILE" | cut -d= -f2-)
GEMINI_KEY=$(grep '^GEMINI_API_KEY=' "$ENV_FILE" | cut -d= -f2-)
BOT_TOKEN=$(grep '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2-)
OUT=/tmp/video-$(date +%s).mp4

tg_msg() {
  curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    -H "Content-Type: application/json" \
    -d "{\"chat_id\":\"$CHAT_ID\",\"text\":\"$1\"}" > /dev/null
}

# ── Polling helper (kie.ai /jobs/recordInfo) ─────────────────────────────
poll_jobs() {
  local TASK="$1" MAX="${2:-300}" WAITED=0
  while [ $WAITED -lt $MAX ]; do
    sleep 12; WAITED=$((WAITED + 12))
    local POLL=$(curl -s "https://api.kie.ai/api/v1/jobs/recordInfo?taskId=$TASK" -H "Authorization: Bearer $KIEAI_KEY")
    local STATE=$(echo "$POLL" | python3 -c "import json,sys; d=json.load(sys.stdin); print((d.get('data') or {}).get('state',''))" 2>/dev/null)
    local URL=$(echo "$POLL" | python3 -c "
import json,sys
d=json.load(sys.stdin)
data=d.get('data') or {}
rj=data.get('resultJson','')
if rj:
    import json as j2
    r=j2.loads(rj)
    urls=r.get('resultUrls',[])
    print(urls[0] if urls else '')
else:
    print('')
" 2>/dev/null)
    echo "[poll] ${WAITED}s state=$STATE" >&2
    [ "$STATE" = "fail" ] && return 1
    if [ "$STATE" = "success" ] && [ -n "$URL" ]; then echo "$URL"; return 0; fi
  done
  return 1
}

# ── Model functions ──────────────────────────────────────────────────────

gen_veo3f() {
  echo "[gen] Veo3 Fast..." >&2
  [ -z "$KIEAI_KEY" ] && return 1
  local RESP=$(curl -s -X POST "https://api.kie.ai/api/v1/veo/generate" \
    -H "Authorization: Bearer $KIEAI_KEY" -H "Content-Type: application/json" \
    -d "{\"prompt\":\"$PROMPT\",\"model\":\"veo3_fast\",\"aspect_ratio\":\"16:9\"}")
  local TASK=$(echo "$RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('data',{}).get('taskId',''))" 2>/dev/null)
  [ -z "$TASK" ] && return 1
  local WAITED=0
  while [ $WAITED -lt 300 ]; do
    sleep 15; WAITED=$((WAITED+15))
    local POLL=$(curl -s "https://api.kie.ai/api/v1/veo/record-info?taskId=$TASK" -H "Authorization: Bearer $KIEAI_KEY")
    local URL=$(echo "$POLL" | python3 -c "import json,sys; d=json.load(sys.stdin); print((d.get('data') or {}).get('videoUrl',''))" 2>/dev/null)
    echo "[veo3f] ${WAITED}s" >&2
    if [ -n "$URL" ]; then curl -s -L "$URL" -o "$OUT"; [ -s "$OUT" ] && echo "$OUT" && return 0; fi
  done
  return 1
}

gen_runway() {
  echo "[gen] Runway Gen4 Turbo..." >&2
  [ -z "$KIEAI_KEY" ] && return 1
  local RESP=$(curl -s -X POST "https://api.kie.ai/api/v1/runway/generate" \
    -H "Authorization: Bearer $KIEAI_KEY" -H "Content-Type: application/json" \
    -d "{\"prompt\":\"$PROMPT\",\"duration\":5,\"quality\":\"720p\",\"aspectRatio\":\"16:9\",\"callBackUrl\":\"https://example.com/cb\"}")
  local TASK=$(echo "$RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('data',{}).get('taskId',''))" 2>/dev/null)
  [ -z "$TASK" ] && return 1
  local WAITED=0
  while [ $WAITED -lt 300 ]; do
    sleep 12; WAITED=$((WAITED+12))
    local POLL=$(curl -s "https://api.kie.ai/api/v1/runway/record-detail?taskId=$TASK" -H "Authorization: Bearer $KIEAI_KEY")
    local URL=$(echo "$POLL" | python3 -c "
import json,sys; d=json.load(sys.stdin)
r=(d.get('data') or {}).get('response') or {}
print(r.get('videoUrl','') or r.get('video_url',''))
" 2>/dev/null)
    echo "[runway] ${WAITED}s" >&2
    if [ -n "$URL" ]; then curl -s -L "$URL" -o "$OUT"; [ -s "$OUT" ] && echo "$OUT" && return 0; fi
  done
  return 1
}

gen_kling3() {
  echo "[gen] Kling 3.0..." >&2
  [ -z "$KIEAI_KEY" ] && return 1
  local RESP=$(curl -s -X POST "https://api.kie.ai/api/v1/jobs/createTask" \
    -H "Authorization: Bearer $KIEAI_KEY" -H "Content-Type: application/json" \
    -d "{\"model\":\"kling-3.0/video\",\"input\":{\"prompt\":\"$PROMPT\",\"duration\":\"5\",\"aspect_ratio\":\"16:9\",\"mode\":\"std\"}}")
  local TASK=$(echo "$RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('data',{}).get('taskId',''))" 2>/dev/null)
  [ -z "$TASK" ] && return 1
  local URL=$(poll_jobs "$TASK" 300)
  [ -z "$URL" ] && return 1
  curl -s -L "$URL" -o "$OUT"; [ -s "$OUT" ] && echo "$OUT"
}

gen_hailuo() {
  echo "[gen] Hailuo Standard..." >&2
  [ -z "$KIEAI_KEY" ] && return 1
  local RESP=$(curl -s -X POST "https://api.kie.ai/api/v1/jobs/createTask" \
    -H "Authorization: Bearer $KIEAI_KEY" -H "Content-Type: application/json" \
    -d "{\"model\":\"hailuo/02-text-to-video-standard\",\"input\":{\"prompt\":\"$PROMPT\",\"duration\":\"6\"}}")
  local TASK=$(echo "$RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('data',{}).get('taskId',''))" 2>/dev/null)
  [ -z "$TASK" ] && return 1
  local URL=$(poll_jobs "$TASK" 300)
  [ -z "$URL" ] && return 1
  curl -s -L "$URL" -o "$OUT"; [ -s "$OUT" ] && echo "$OUT"
}

gen_google_veo() {
  echo "[gen] Google Veo (direct)..." >&2
  [ -z "$GEMINI_KEY" ] && return 1
  local BASE="https://generativelanguage.googleapis.com/v1beta"
  local MODEL="veo-3.0-fast-generate-001"
  local RESP=$(curl -s -X POST "${BASE}/models/${MODEL}:predictLongRunning?key=${GEMINI_KEY}" \
    -H "Content-Type: application/json" \
    -d "{\"instances\":[{\"prompt\":\"$PROMPT\"}],\"parameters\":{\"aspectRatio\":\"16:9\",\"durationSeconds\":6}}")
  local OP=$(echo "$RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('name',''))" 2>/dev/null)
  [ -z "$OP" ] && return 1
  local WAITED=0
  while [ $WAITED -lt 300 ]; do
    sleep 12; WAITED=$((WAITED+12))
    local STATUS=$(curl -s "${BASE}/${OP}?key=${GEMINI_KEY}")
    local DONE=$(echo "$STATUS" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('done','false'))" 2>/dev/null)
    echo "[gveo] ${WAITED}s done=$DONE" >&2
    if [ "$DONE" = "True" ] || [ "$DONE" = "true" ]; then
      local URL=$(echo "$STATUS" | python3 -c "
import json,sys; d=json.load(sys.stdin)
s=d.get('response',{}).get('generateVideoResponse',{}).get('generatedSamples',[])
print(s[0].get('video',{}).get('uri','') if s else '')
" 2>/dev/null)
      [ -z "$URL" ] && return 1
      curl -s -L "${URL}&key=${GEMINI_KEY}" -o "$OUT"
      [ -s "$OUT" ] && echo "$OUT" && return 0
    fi
  done
  return 1
}

# ── Auto-fallback chain ──────────────────────────────────────────────────

echo "[vid] Starting video generation..." >&2
tg_msg "Generating video... (ini bisa 1-5 menit)"

for MODEL_FN in gen_veo3f gen_runway gen_kling3 gen_hailuo gen_google_veo; do
  RESULT=$($MODEL_FN)
  if [ -n "$RESULT" ] && [ -f "$RESULT" ] && [ -s "$RESULT" ]; then
    bash "$TG_SEND" "$RESULT" "$CAPTION" "$CHAT_ID"
    echo "VIDEO_SENT_OK"
    exit 0
  fi
  echo "[vid] $MODEL_FN failed, trying next..." >&2
done

tg_msg "Semua model gagal. Generate video dibatalkan."
echo "ERROR: Semua model gagal" >&2
exit 1
