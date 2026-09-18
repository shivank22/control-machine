#!/usr/bin/env bash
set -Eeuo pipefail

export DISPLAY="${DISPLAY:-:99}"
SCREEN_GEOMETRY="${SCREEN_GEOMETRY:-1280x900x24}"
VNC_PORT="${VNC_PORT:-5900}"
NOVNC_PORT="${NOVNC_PORT:-6080}"
CDP_INTERNAL_PORT="${CDP_INTERNAL_PORT:-9222}"
CDP_PROXY_PORT="${CDP_PROXY_PORT:-9223}"

CHROME_BIN="${CHROME_BIN:-}"
if [[ -z "${CHROME_BIN}" ]]; then
  CHROME_BIN="$(compgen -G '/ms-playwright/chromium-*/chrome-linux*/chrome' | sort -V | tail -n 1 || true)"
fi
if [[ -z "${CHROME_BIN}" || ! -x "${CHROME_BIN}" ]]; then
  echo "Unable to locate Playwright Chromium under /ms-playwright." >&2
  exit 1
fi

mkdir -p /profile /tmp/fluxbox
rm -f /tmp/.X99-lock
rm -rf /tmp/.X11-unix/X99

pids=()
cleanup() {
  trap - EXIT INT TERM
  if ((${#pids[@]})); then
    kill "${pids[@]}" 2>/dev/null || true
    wait "${pids[@]}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

Xvfb "${DISPLAY}" -screen 0 "${SCREEN_GEOMETRY}" -ac +extension RANDR &
pids+=("$!")

for _ in {1..50}; do
  [[ -S /tmp/.X11-unix/X99 ]] && break
  sleep 0.1
done
if [[ ! -S /tmp/.X11-unix/X99 ]]; then
  echo "Xvfb did not become ready." >&2
  exit 1
fi

fluxbox -display "${DISPLAY}" >/tmp/fluxbox/fluxbox.log 2>&1 &
pids+=("$!")

"${CHROME_BIN}" \
  --no-sandbox \
  --disable-dev-shm-usage \
  --disable-backgrounding-occluded-windows \
  --disable-background-timer-throttling \
  --disable-renderer-backgrounding \
  --disable-features=Translate,MediaRouter \
  --no-first-run \
  --no-default-browser-check \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port="${CDP_INTERNAL_PORT}" \
  --user-data-dir=/profile \
  --window-position=0,0 \
  --window-size="${SCREEN_GEOMETRY%%x*},$(cut -d x -f 2 <<<"${SCREEN_GEOMETRY}")" \
  about:blank &
pids+=("$!")

socat \
  "TCP-LISTEN:${CDP_PROXY_PORT},fork,reuseaddr,bind=0.0.0.0" \
  "TCP:127.0.0.1:${CDP_INTERNAL_PORT}" &
pids+=("$!")

x11vnc \
  -display "${DISPLAY}" \
  -rfbport "${VNC_PORT}" \
  -forever \
  -shared \
  -nopw \
  -xkb \
  -noxrecord \
  -noxfixes \
  -noxdamage &
pids+=("$!")

websockify \
  --web=/usr/share/novnc \
  "${NOVNC_PORT}" \
  "127.0.0.1:${VNC_PORT}" &
pids+=("$!")

wait -n "${pids[@]}"
echo "A browser service exited unexpectedly." >&2
exit 1
