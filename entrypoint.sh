#!/bin/sh
# CustomProxy entrypoint.
# Runs as root only long enough to make the writable mounts (catalog + logs)
# usable by the unprivileged `app` user, then drops to it and hands off to the
# service launcher (CustomProxy + any enabled key-rotation relays).
# Invoked via `sh`, so a host bind-mount needs no exec bit.
set -e

APP_UID="${APP_UID:-10001}"
APP_GID="${APP_GID:-10001}"

if [ "$(id -u)" = "0" ]; then
  for d in /state /logs; do
    if [ -d "$d" ]; then
      chown -R "$APP_UID:$APP_GID" "$d" 2>/dev/null || true
    fi
  done
  exec setpriv --reuid="$APP_UID" --regid="$APP_GID" --clear-groups -- \
    sh /usr/local/bin/customproxy-services.sh
fi

exec sh /usr/local/bin/customproxy-services.sh
