#!/bin/bash
export AUTH_DIR="${RAILWAY_VOLUME_MOUNT_PATH:-/app}/wa_auth"
PORT=3000 node server.js &
sleep 3
exec python bot.py