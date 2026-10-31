#!/usr/bin/env bash
# Codespaces helper. Runs automatically every time the Codespace starts (postStartCommand).
#   bash scripts/codespace.sh            start (or restart) the bot, make it public, print connect info
#   bash scripts/codespace.sh connect    print the URLs for the terminal, Claude and Grok again
#   bash scripts/codespace.sh stop       stop the bot
#   bash scripts/codespace.sh log        follow the live log
set -u
cd "$(dirname "$0")/.."
mkdir -p data
PID_FILE=data/bot.pid
CONNECT_FILE=data/CONNECT.txt
PORT=8000

stop_bot() {
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    kill "$(cat "$PID_FILE")" && sleep 2
  fi
  rm -f "$PID_FILE"
}

load_token() {
  [ -f .env ] || cp .env.example .env
  # 1) Codespaces secret  2) token saved in .env  3) generate one and save it (stable across restarts)
  if [ -z "${DASHBOARD_TOKEN:-}" ]; then
    DASHBOARD_TOKEN=$(grep -E '^DASHBOARD_TOKEN=[^ #]+' .env | head -1 | cut -d= -f2- | cut -d' ' -f1)
  fi
  if [ -z "${DASHBOARD_TOKEN:-}" ]; then
    DASHBOARD_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(24))")
    if grep -q '^DASHBOARD_TOKEN=' .env; then
      sed -i "s|^DASHBOARD_TOKEN=.*|DASHBOARD_TOKEN=$DASHBOARD_TOKEN|" .env
    else
      echo "DASHBOARD_TOKEN=$DASHBOARD_TOKEN" >> .env
    fi
  fi
  export DASHBOARD_TOKEN
}

public_url() {
  if [ -n "${CODESPACE_NAME:-}" ] && [ -n "${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-}" ]; then
    echo "https://${CODESPACE_NAME}-${PORT}.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}"
  else
    echo "http://localhost:${PORT}"
  fi
}

print_connect() {
  load_token
  local url; url=$(public_url)
  local repo; repo=$(git config --get remote.origin.url | sed -E 's#.*github.com[:/]([^/]+)/([^/.]+)(\.git)?#\1 \2#')
  local owner=${repo% *} name=${repo#* }
  cat > "$CONNECT_FILE" <<TXT
=====================================================================
 TRADING BOT IS RUNNING        (keep these private — they control the bot)
=====================================================================
 Terminal (open on phone/PC):
   $url/?token=$DASHBOARD_TOKEN

 Terminal via GitHub Pages:
   https://$(echo "$owner" | tr 'A-Z' 'a-z').github.io/$name/?server=$url&token=$DASHBOARD_TOKEN

 Claude  -> Customize > Connectors > Add custom connector
   URL:     $url/mcp
   Auth:    No sign-in, request header  Authorization: Bearer $DASHBOARD_TOKEN

 Grok    -> grok.com > Connectors > New Connector > Custom
   URL:     $url/mcp?token=$DASHBOARD_TOKEN

 This address stays the same every time you restart THIS Codespace.
 Codespaces stop after max 12 h: just reopen it and the bot restarts
 by itself, picking up where it left off.
=====================================================================
TXT
  cat "$CONNECT_FILE"
}

start_bot() {
  stop_bot
  load_token
  export HOST=0.0.0.0 PORT
  echo "[$(date -u +%FT%TZ)] starting bot" >> data/bot.log
  nohup setsid python -m bot >> data/bot.log 2>&1 &
  echo $! > "$PID_FILE"
  for _ in $(seq 1 60); do
    curl -fs "http://localhost:$PORT/healthz" >/dev/null 2>&1 && break
    sleep 1
  done
  if ! curl -fs "http://localhost:$PORT/healthz" >/dev/null 2>&1; then
    echo "!! bot did not start — last log lines:"; tail -n 30 data/bot.log; return 1
  fi
  # Make the port reachable by Claude / Grok / your phone (it is still protected by the token).
  if [ -n "${CODESPACE_NAME:-}" ] && command -v gh >/dev/null 2>&1; then
    gh codespace ports visibility "$PORT:public" -c "$CODESPACE_NAME" >/dev/null 2>&1 \
      || echo "!! Could not make port $PORT public automatically: open the PORTS tab, right-click $PORT > Port Visibility > Public"
  fi
  print_connect
}

case "${1:-start}" in
  start|restart) start_bot ;;
  stop) stop_bot; echo "bot stopped" ;;
  connect) print_connect ;;
  log) tail -n 100 -F data/bot.log ;;
  *) echo "usage: $0 [start|stop|connect|log]"; exit 1 ;;
esac
