#!/bin/sh
set -eu

export DISPLAY=:99
Xvfb "$DISPLAY" -screen 0 1440x900x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
sleep 1
openbox-session >/tmp/openbox.log 2>&1 &
x11vnc -display "$DISPLAY" -forever -shared -localhost -rfbport 5900 -nopw >/tmp/x11vnc.log 2>&1 &
websockify --web=/usr/share/novnc 0.0.0.0:6080 localhost:5900 >/tmp/novnc.log 2>&1 &

exec uvicorn browser_agent.main:app --host 0.0.0.0 --port 8080 --no-access-log
