#!/bin/sh
# CustomProxy service launcher: proxy + optional key-rotation relays.
#   PID 1 (this shell)
#     +- python3 /app/customproxy/server.py   -> CustomProxy on :3001
#     +- python3 /app/key_rotate_proxy.py X   -> one relay per KEY_ROTATE_SERVICES
#
# KEY_ROTATE_SERVICES is empty by default: a plain deployment runs only the
# proxy. Set it to the MCP services whose keys you want rotated, e.g.
#   KEY_ROTATE_SERVICES=tavily,firecrawl
#
# The launcher supervises the relays: a relay that exits is restarted in
# place. If CustomProxy (the primary service) exits, the launcher exits so
# Docker's restart policy can recycle the container.
set -e
cd /app

KNOWN_SERVICES="tavily firecrawl gemini tokenharbour context7 zro"

python3 /app/customproxy/server.py &
CP_PID=$!

PROXY_LIST=""
for svc in $(echo "${KEY_ROTATE_SERVICES:-}" | tr ',' ' '); do
  case " $KNOWN_SERVICES " in
    *" $svc "*) ;;
    *) echo "[launcher] unknown relay service: $svc (skipped)" >&2; continue ;;
  esac
  python3 /app/key_rotate_proxy.py "$svc" &
  PROXY_LIST="$PROXY_LIST $svc:$!"
done

cleanup() {
  for item in $PROXY_LIST; do
    kill "${item##*:}" 2>/dev/null || true
  done
  kill "$CP_PID" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

PROBE_FAILS=0
TICKS=0

while kill -0 "$CP_PID" 2>/dev/null; do
  NEW_LIST=""
  for item in $PROXY_LIST; do
    svc=${item%%:*}
    pid=${item##*:}
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "[launcher] relay $svc died - restarting" >&2
      python3 /app/key_rotate_proxy.py "$svc" &
      pid=$!
    fi
    NEW_LIST="$NEW_LIST $svc:$pid"
  done
  PROXY_LIST="$NEW_LIST"

  # Watchdog: after a 30s startup grace, 3 failed /health probes (15s)
  # mean CustomProxy is hung, not just busy -> exit so Docker restarts it.
  TICKS=$((TICKS + 1))
  if [ "$TICKS" -gt 6 ]; then
    if python3 -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3001/health',timeout=4).status==200 else 1)" 2>/dev/null; then
      PROBE_FAILS=0
    else
      PROBE_FAILS=$((PROBE_FAILS + 1))
      echo "[launcher] CustomProxy health probe failed ($PROBE_FAILS/3)" >&2
      if [ "$PROBE_FAILS" -ge 3 ]; then
        echo "[launcher] CustomProxy unresponsive - exiting for container restart" >&2
        kill "$CP_PID" 2>/dev/null || true
        break
      fi
    fi
  fi

  sleep 5
done

wait "$CP_PID"
