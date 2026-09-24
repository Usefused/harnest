#!/bin/sh
set -eu

Xvfb :99 -screen 0 1280x800x24 -nolisten tcp &
xvfb_pid=$!

cleanup() {
  for child in "${api_pid:-}" "${viewer_pid:-}" "${vnc_pid:-}" "${openbox_pid:-}" "${xvfb_pid:-}"; do
    if [ -n "$child" ]; then
      kill "$child" 2>/dev/null || true
    fi
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

until xdpyinfo -display :99 >/dev/null 2>&1; do
  kill -0 "$xvfb_pid" 2>/dev/null || exit 1
  sleep 0.1
done

openbox --sm-disable >/dev/null 2>&1 &
openbox_pid=$!
vnc_pid=
viewer_pid=
if [ "${ENABLE_VIEWER:-0}" = 1 ]; then
  x11vnc -display :99 -localhost -forever -shared -nopw -rfbport 5900 >/dev/null 2>&1 &
  vnc_pid=$!
  websockify --web=/usr/share/novnc 0.0.0.0:6080 127.0.0.1:5900 >/dev/null 2>&1 &
  viewer_pid=$!
fi

uvicorn server:app --host 0.0.0.0 --port 8765 >/dev/null 2>&1 &
api_pid=$!
wait "$api_pid"
