#!/bin/sh
# Run as root after reviewing the staged files on the target laptop.
set -eu
test "$(id -u)" = 0 || { echo 'Run this installer with sudo.' >&2; exit 1; }
base=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$base"
sha256sum --check SHA256SUMS
backup=/var/lib/lifetech-power/backups/$(date +%Y%m%dT%H%M%S)
install -d -m 755 "$backup" /usr/local/lib/lifetech-power /etc/systemd/logind.conf.d
for path in /etc/systemd/logind.conf.d/90-no-lid-sleep.conf /etc/systemd/logind.conf.d/99-lifetech-power.conf /etc/systemd/system/laptop-power.service /usr/local/lib/lifetech-power/laptop_power.py; do
    if test -f "$path"; then cp -p "$path" "$backup/$(basename "$path")"; fi
done
install -m 644 laptop_power.py /usr/local/lib/lifetech-power/laptop_power.py
install -m 644 laptop-power.service /etc/systemd/system/laptop-power.service
cat > /etc/systemd/logind.conf.d/99-lifetech-power.conf <<'CONFIG'
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
CONFIG
rm -f /etc/systemd/logind.conf.d/90-no-lid-sleep.conf
systemctl daemon-reload
systemctl enable laptop-power.service
systemctl restart laptop-power.service
# GNOME must not race the shared timer with its own battery-idle suspend.
for key in lid-close-ac-action lid-close-battery-action sleep-inactive-ac-type sleep-inactive-battery-type; do
    runuser -u julian -- env DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus gsettings set org.gnome.settings-daemon.plugins.power "$key" nothing
done
for key in sleep-inactive-ac-timeout sleep-inactive-battery-timeout; do
    runuser -u julian -- env DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus gsettings set org.gnome.settings-daemon.plugins.power "$key" 0
done
if test "$(systemctl show systemd-logind -p CanReload --value)" = yes; then
    systemctl reload systemd-logind
else
    # Older installed logind versions cannot reload. Avoid restarting login
    # sessions when the required policy is already active (as on Latitude).
    lid=$(busctl get-property org.freedesktop.login1 /org/freedesktop/login1 org.freedesktop.login1.Manager HandleLidSwitch)
    ac=$(busctl get-property org.freedesktop.login1 /org/freedesktop/login1 org.freedesktop.login1.Manager HandleLidSwitchExternalPower)
    docked=$(busctl get-property org.freedesktop.login1 /org/freedesktop/login1 org.freedesktop.login1.Manager HandleLidSwitchDocked)
    if test "$lid" != 's "ignore"' || test "$docked" != 's "ignore"' || { test "$ac" != 's "ignore"' && test "$ac" != 's ""'; }; then
        echo 'The service is installed, but a reboot is required to load the logind lid policy.' >&2
        exit 1
    fi
fi
systemctl is-active laptop-power.service
echo 'Laptop power policy installed. Status: /run/lifetech-power/status.json'
