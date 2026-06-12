#!/system/bin/sh
# Magisk service.d autostart for phone_server.py
# Installed at /data/adb/service.d/phone_server.sh (run as root on boot)

# Ждём полной загрузки (сеть, Termux mount points и т.д.)
sleep 45

SUPERVISOR=/data/data/com.termux/files/home/phone_server_supervisor.sh
LOG=/data/data/com.termux/files/home/phone_server_boot.log

if [ -f "$SUPERVISOR" ]; then
    # запуск в global mount namespace чтобы видеть /data/data/com.*
    nohup sh "$SUPERVISOR" > "$LOG" 2>&1 &
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] phone_server autostart PID=$!" >> "$LOG"
fi
