#!/system/bin/sh
# Copy to /data/adb/service.d/health_adb_keepalive.sh and chmod 755.
# Replace the placeholder with the public key from ~/.android/adbkey.pub.

PUBKEY='REPLACE_WITH_YOUR_ADB_PUBLIC_KEY'
ADB_KEYS=/data/misc/adb/adb_keys

if [ "$PUBKEY" = 'REPLACE_WITH_YOUR_ADB_PUBLIC_KEY' ]; then
    exit 1
fi

mkdir -p /data/misc/adb
touch "$ADB_KEYS"
grep -Fqx "$PUBKEY" "$ADB_KEYS" || echo "$PUBKEY" >> "$ADB_KEYS"
chmod 640 "$ADB_KEYS"
chown system:shell "$ADB_KEYS"

setprop service.adb.tcp.port 5555
stop adbd
start adbd
