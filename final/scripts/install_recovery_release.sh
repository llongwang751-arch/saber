#!/usr/bin/env bash
# Run as root after uploading the reviewed code-only overlay.
set -euo pipefail
umask 077
root=/opt/agi-saber
release=$root/releases/recovery-20260919-01
test ! -e "$release"
test ! -e /etc/systemd/system/agi-saber.service.d/recovery.conf
cp -a "$root/releases/lightweight-20260919" "$release"
tar -xzf /home/ubuntu/agi-saber-transfer/recovery-code.tar.gz -C "$release" --no-same-owner
chown -R root:root "$release"
chmod -R a+rX "$release"
install -d -m 700 "$root/ops" "$root/backups"
install -m 600 "$release/scripts/backup_server.py" "$root/ops/backup_server.py"
install -m 600 "$release/scripts/run_server_backup.py" "$root/ops/run_server_backup.py"
python3 "$root/ops/backup_server.py" --keep 7
printf 'FRONTEND_DIR=%s/web/dist\n' "$release" > "$root/recovery.env"
cat > /etc/systemd/system/agi-saber.service.d/recovery.conf <<'UNIT'
[Service]
WorkingDirectory=/opt/agi-saber/releases/recovery-20260919-01
EnvironmentFile=/opt/agi-saber/recovery.env
ExecStart=
ExecStart=/opt/agi-saber/venv-lightweight/bin/python /opt/agi-saber/releases/recovery-20260919-01/serve_private.py
UNIT
systemctl daemon-reload
systemctl restart agi-saber
healthy=0
for i in $(seq 1 40); do
    if curl -fsS --max-time 2 http://127.0.0.1:8090/healthz > /dev/null; then
        healthy=1
        break
    fi
    sleep 1
done
if test "$healthy" != 1; then
    unlink /etc/systemd/system/agi-saber.service.d/recovery.conf
    systemctl daemon-reload
    systemctl restart agi-saber
    echo RECOVERY_ACTIVATION_FAILED_ROLLED_BACK
    exit 1
fi
cat > /etc/systemd/system/agi-saber-backup.service <<'UNIT'
[Unit]
Description=AGI Saber consistent database backup and verified offsite copy
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/agi-saber/ops/run_server_backup.py
User=root
UMask=0077
Nice=10
IOSchedulingClass=idle
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/opt/agi-saber/backups
MemoryMax=256M
TimeoutStartSec=15min
UNIT
cat > /etc/systemd/system/agi-saber-backup.timer <<'UNIT'
[Unit]
Description=Daily AGI Saber backups (Asia/Shanghai)
[Timer]
OnCalendar=*-*-* 03:15:00 Asia/Shanghai
RandomizedDelaySec=5min
Persistent=true
Unit=agi-saber-backup.service
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now agi-saber-backup.timer
echo RECOVERY_RELEASE_ACTIVE
