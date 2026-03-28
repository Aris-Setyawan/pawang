#!/bin/bash
# Check API balances for all Pawang providers
# - Tracks history for trend analysis
# - Output designed for LLM consumption (no ANSI colors)

ENV_FILE="/root/pawang/.env"
HISTORY_FILE="/root/pawang/data/balance-history.json"

get_key() {
  grep "^${1}=" "$ENV_FILE" 2>/dev/null | cut -d= -f2-
}

DEEPSEEK_KEY=$(get_key DEEPSEEK_API_KEY)
OPENROUTER_KEY=$(get_key OPENROUTER_API_KEY)
ANTHROPIC_KEY=$(get_key ANTHROPIC_API_KEY)
OPENAI_KEY=$(get_key OPENAI_API_KEY)
GEMINI_KEY=$(get_key GEMINI_API_KEY)
MODELSTUDIO_KEY=$(get_key MODELSTUDIO_API_KEY)
KIEAI_KEY=$(get_key KIEAI_API_KEY)

NOW_UTC=$(date -u '+%Y-%m-%d %H:%M UTC')
NOW_WIB=$(TZ=Asia/Jakarta date '+%Y-%m-%d %H:%M WIB')
TIMESTAMP=$(date +%s)

# --- Load previous history ---
PREV_DS_BAL=""
PREV_OR_USAGE=""
PREV_TIME=""
if [ -f "$HISTORY_FILE" ]; then
  PREV_DS_BAL=$(python3 -c "
import json
d=json.load(open('$HISTORY_FILE'))
print(d.get('deepseek_balance',''))
" 2>/dev/null)
  PREV_OR_USAGE=$(python3 -c "
import json
d=json.load(open('$HISTORY_FILE'))
print(d.get('openrouter_usage',''))
" 2>/dev/null)
  PREV_TIME=$(python3 -c "
import json
d=json.load(open('$HISTORY_FILE'))
print(d.get('timestamp',''))
" 2>/dev/null)
fi

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "API BALANCE CHECK — $NOW_WIB"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# --- DeepSeek ---
DS_BAL=""
echo "DeepSeek:"
if [ -n "$DEEPSEEK_KEY" ]; then
  DS_RAW=$(curl -s --max-time 10 "https://api.deepseek.com/user/balance" \
    -H "Authorization: Bearer $DEEPSEEK_KEY" 2>/dev/null)
  DS_BAL=$(echo "$DS_RAW" | python3 -c "
import json,sys
d=json.load(sys.stdin)
b=d.get('balance_infos',[{}])[0]
print(b.get('total_balance','')) if b else print('')
" 2>/dev/null)

  if [ -n "$DS_BAL" ] && [ "$DS_BAL" != "None" ]; then
    echo "  Balance: \$$DS_BAL"
    # Trend
    if [ -n "$PREV_DS_BAL" ] && [ -n "$PREV_TIME" ]; then
      TREND=$(python3 -c "
curr=$DS_BAL
prev=$PREV_DS_BAL
diff=curr-prev
prev_ts=$PREV_TIME
now_ts=$TIMESTAMP
hours=(now_ts-prev_ts)/3600
if hours > 0:
    rate=abs(diff)/hours
    if diff < -0.01:
        print(f'  Perubahan: -\${abs(diff):.4f} (turun)')
        print(f'  Rate: ~\${rate:.4f}/jam')
    elif diff > 0.01:
        print(f'  Perubahan: +\${diff:.4f} (naik/top-up)')
    else:
        print(f'  Perubahan: stabil')
    print(f'  Interval: {hours:.1f} jam')
else:
    print('  (baru pertama cek)')
" 2>/dev/null)
      echo "$TREND"
    fi
  else
    echo "  Error: gagal fetch balance"
  fi
else
  echo "  Key not configured"
fi
echo ""

# --- OpenRouter ---
OR_USAGE=""
echo "OpenRouter:"
if [ -n "$OPENROUTER_KEY" ]; then
  OR_RAW=$(curl -s --max-time 10 "https://openrouter.ai/api/v1/auth/key" \
    -H "Authorization: Bearer $OPENROUTER_KEY" 2>/dev/null)
  OR_USAGE=$(echo "$OR_RAW" | python3 -c "
import json,sys
d=json.load(sys.stdin).get('data',{})
print(d.get('usage',''))
" 2>/dev/null)
  OR_LIMIT=$(echo "$OR_RAW" | python3 -c "
import json,sys
d=json.load(sys.stdin).get('data',{})
lim=d.get('limit')
print(lim if lim else 'unlimited')
" 2>/dev/null)

  if [ -n "$OR_USAGE" ] && [ "$OR_USAGE" != "None" ]; then
    echo "  Usage: \$$OR_USAGE"
    echo "  Limit: $OR_LIMIT"
    # Trend
    if [ -n "$PREV_OR_USAGE" ] && [ -n "$PREV_TIME" ]; then
      TREND=$(python3 -c "
curr=$OR_USAGE
prev=$PREV_OR_USAGE
diff=curr-prev
prev_ts=$PREV_TIME
now_ts=$TIMESTAMP
hours=(now_ts-prev_ts)/3600
if hours > 0:
    rate=abs(diff)/hours
    if diff > 0.01:
        print(f'  Perubahan: +\${diff:.4f} (naik)')
        print(f'  Rate: ~\${rate:.4f}/jam')
    elif diff < -0.01:
        print(f'  Perubahan: -\${abs(diff):.4f} (turun/reset)')
    else:
        print(f'  Perubahan: stabil')
else:
    print('  (baru pertama cek)')
" 2>/dev/null)
      echo "$TREND"
    fi
  else
    echo "  Error: gagal fetch usage"
  fi
else
  echo "  Key not configured"
fi
echo ""

# --- Anthropic ---
echo "Anthropic:"
if [ -n "$ANTHROPIC_KEY" ]; then
  echo "  Status: via OpenRouter (no direct key)"
  echo "  Note: Anthropic API route melalui OpenRouter, bukan direct"
else
  echo "  Key not configured (routed via OpenRouter)"
fi
echo ""

# --- OpenAI ---
echo "OpenAI:"
if [ -n "$OPENAI_KEY" ]; then
  echo "  Key: active (${OPENAI_KEY:0:12}...)"
  echo "  Note: Balance check via dashboard, tidak ada API publik"
else
  echo "  Key not configured"
fi
echo ""

# --- Google Gemini ---
echo "Google Gemini:"
if [ -n "$GEMINI_KEY" ]; then
  echo "  Key: active"
  echo "  Tier: API key (generativelanguage.googleapis.com)"
  echo "  Note: Free tier / pay-as-you-go tergantung billing project"
else
  echo "  Key not configured"
fi
echo ""

# --- ModelStudio (Alibaba) ---
echo "ModelStudio (Alibaba):"
if [ -n "$MODELSTUDIO_KEY" ]; then
  echo "  Key: active (${MODELSTUDIO_KEY:0:12}...)"
  echo "  Endpoint: dashscope-intl.aliyuncs.com (International)"
  echo "  Note: Balance cek di DashScope console"
else
  echo "  Key not configured"
fi
echo ""

# --- kie.ai ---
echo "kie.ai:"
if [ -n "$KIEAI_KEY" ]; then
  echo "  Key: active (${KIEAI_KEY:0:12}...)"
  echo "  Note: Dipakai untuk image/video/music generation"
else
  echo "  Key not configured"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Cek terakhir: $NOW_WIB"

# --- Save history for next comparison ---
mkdir -p "$(dirname "$HISTORY_FILE")"
python3 -c "
import json
data = {
    'timestamp': $TIMESTAMP,
    'time_wib': '$NOW_WIB',
    'deepseek_balance': '$DS_BAL' if '$DS_BAL' else None,
    'openrouter_usage': '$OR_USAGE' if '$OR_USAGE' else None,
}
# Keep previous entries for longer history
try:
    old = json.load(open('$HISTORY_FILE'))
    history = old.get('history', [])
except:
    history = []
# Append current to history (keep last 50)
entry = {k:v for k,v in data.items() if k != 'history'}
history.append(entry)
data['history'] = history[-50:]
json.dump(data, open('$HISTORY_FILE','w'), indent=2)
" 2>/dev/null
