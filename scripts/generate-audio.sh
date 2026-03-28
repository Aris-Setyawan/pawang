#!/bin/bash
# Generate audio/suara via TTS + kirim ke Telegram
# Usage: generate-audio.sh "<teks>" "[caption]" "[voice]" "[provider]"
#
# Provider:
#   google  — Gemini TTS (default, narasi)
#   openai  — OpenAI TTS (fallback narasi)
#   kieai   — Kie.ai Suno (music generation dari prompt)
#
# Voice Google: Aoede, Charon, Fenrir, Kore, Puck
# Voice OpenAI: alloy, echo, fable, nova, onyx, shimmer

TEXT="$1"
CAPTION="${2:-Pesan suara}"
VOICE="${3:-Aoede}"
PROVIDER="${4:-google}"
ENV_FILE="/root/pawang/.env"
TG_SEND="/root/pawang/scripts/telegram-send.sh"

if [ -z "$TEXT" ]; then
  echo "Usage: generate-audio.sh <teks> [caption] [voice] [provider: google|openai|kieai]" >&2
  exit 1
fi

GEMINI_KEY=$(grep '^GEMINI_API_KEY=' "$ENV_FILE" | cut -d= -f2-)
OPENAI_KEY=$(grep '^OPENAI_API_KEY=' "$ENV_FILE" | cut -d= -f2-)
KIEAI_KEY=$(grep '^KIEAI_API_KEY=' "$ENV_FILE" | cut -d= -f2-)
OUT=/tmp/audio-$(date +%s).mp3

# ── Google Gemini TTS ─────────────────────────────────────────────────────
google_tts() {
  [ -z "$GEMINI_KEY" ] && return 1
  echo "[tts] Google Gemini TTS, voice=$VOICE..." >&2

  RESPONSE=$(curl -s -X POST \
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-tts:generateContent?key=${GEMINI_KEY}" \
    -H "Content-Type: application/json" \
    -d "{
      \"contents\": [{\"parts\": [{\"text\": \"$TEXT\"}]}],
      \"generationConfig\": {
        \"responseModalities\": [\"AUDIO\"],
        \"speechConfig\": {\"voiceConfig\": {\"prebuiltVoiceConfig\": {\"voiceName\": \"$VOICE\"}}}
      }
    }")

  ERROR=$(echo "$RESPONSE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('error',{}).get('message',''))" 2>/dev/null)
  if [ -n "$ERROR" ]; then
    echo "[tts] Google error: $ERROR" >&2
    return 1
  fi

  echo "$RESPONSE" | python3 -c "
import json, sys, base64, wave
d = json.load(sys.stdin)
parts = d.get('candidates',[{}])[0].get('content',{}).get('parts',[])
for p in parts:
    if 'inlineData' in p:
        raw = base64.b64decode(p['inlineData']['data'])
        with wave.open('${OUT%.mp3}.wav', 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(24000)
            wf.writeframes(raw)
        print('wav_saved')
        break
" 2>/dev/null

  WAV="${OUT%.mp3}.wav"
  if [ -f "$WAV" ]; then
    if command -v ffmpeg &>/dev/null; then
      ffmpeg -y -i "$WAV" -q:a 4 "$OUT" -loglevel quiet
      rm -f "$WAV"
    else
      mv "$WAV" "$OUT"
    fi
    return 0
  fi
  return 1
}

# ── Kie.ai Suno: Music Generation ────────────────────────────────────────
kieai_suno() {
  [ -z "$KIEAI_KEY" ] && { echo "[suno] KIEAI_API_KEY not set" >&2; return 1; }

  SUNO_MODEL="${VOICE:-V4_5}"
  case "$SUNO_MODEL" in
    Aoede|Charon|Fenrir|Kore|Puck|nova|onyx|fable|alloy|echo|shimmer) SUNO_MODEL="V4_5" ;;
  esac

  echo "[suno] kie.ai Suno $SUNO_MODEL — generating music..." >&2

  SUNO_RESP=$(curl -s -X POST "https://api.kie.ai/api/v1/generate" \
    -H "Authorization: Bearer $KIEAI_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"prompt\":\"$TEXT\",\"model\":\"$SUNO_MODEL\",\"customMode\":false,\"instrumental\":true,\"callBackUrl\":\"https://example.com/cb\"}")

  TASK_ID=$(echo "$SUNO_RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('data',{}).get('taskId',''))" 2>/dev/null)
  [ -z "$TASK_ID" ] && { echo "[suno] Failed to create task" >&2; return 1; }
  echo "[suno] taskId: $TASK_ID" >&2

  for i in $(seq 1 36); do
    sleep 5
    POLL=$(curl -s "https://api.kie.ai/api/v1/generate/record-info?taskId=$TASK_ID" \
      -H "Authorization: Bearer $KIEAI_KEY")
    STATUS=$(echo "$POLL" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('data',{}).get('status',''))" 2>/dev/null)
    echo "[suno] status: $STATUS" >&2

    if [[ "$STATUS" == "SUCCESS" || "$STATUS" == "FIRST_SUCCESS" ]]; then
      AUDIO_URL=$(echo "$POLL" | python3 -c "
import json,sys
d=json.load(sys.stdin)
suno = d.get('data',{}).get('response',{}).get('sunoData',[])
print(suno[0]['audioUrl'] if suno else '')
" 2>/dev/null)
      if [ -n "$AUDIO_URL" ]; then
        curl -s -L "$AUDIO_URL" -o "$OUT"
        [ -f "$OUT" ] && [ -s "$OUT" ] && return 0
      fi
    fi

    if [[ "$STATUS" == *FAILED* || "$STATUS" == "SENSITIVE_WORD_ERROR" ]]; then
      echo "[suno] Failed: $STATUS" >&2
      return 1
    fi
  done
  echo "[suno] Timeout" >&2
  return 1
}

# ── OpenAI TTS (fallback) ────────────────────────────────────────────────
openai_tts() {
  [ -z "$OPENAI_KEY" ] && return 1
  OAI_VOICE="${VOICE:-nova}"
  case "$OAI_VOICE" in
    Aoede|Kore) OAI_VOICE="nova" ;;
    Charon|Fenrir) OAI_VOICE="onyx" ;;
    Puck) OAI_VOICE="fable" ;;
  esac

  echo "[tts] OpenAI TTS, voice=$OAI_VOICE..." >&2
  curl -s -X POST https://api.openai.com/v1/audio/speech \
    -H "Authorization: Bearer $OPENAI_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"tts-1\",\"input\":\"$TEXT\",\"voice\":\"$OAI_VOICE\"}" \
    -o "$OUT"
  [ -f "$OUT" ] && [ -s "$OUT" ] && file "$OUT" | grep -q "MPEG\|audio"
}

# ── Run ───────────────────────────────────────────────────────────────────
if [ "$PROVIDER" = "kieai" ]; then
  kieai_suno || { echo "ERROR: kie.ai Suno gagal" >&2; exit 1; }
elif [ "$PROVIDER" = "openai" ]; then
  openai_tts || { echo "ERROR: OpenAI TTS gagal" >&2; exit 1; }
else
  google_tts || openai_tts || { echo "ERROR: Semua TTS gagal" >&2; exit 1; }
fi

if [ -f "$OUT" ] && [ -s "$OUT" ]; then
  SIZE=$(ls -lh "$OUT" | awk '{print $5}')
  echo "[tts] Audio saved: $OUT ($SIZE)" >&2
  bash "$TG_SEND" "$OUT" "$CAPTION"
  echo "AUDIO_SENT_OK"
  exit 0
fi

echo "ERROR: File audio tidak terbuat" >&2
exit 1
