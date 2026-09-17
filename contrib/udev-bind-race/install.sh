#!/bin/bash
# Install the openrazer bind-race udev rule and prove it actually fires.
#   sudo bash contrib/udev-bind-race/install.sh
set -u

RULE_SRC="$(dirname "$(realpath "$0")")/99-razer-bind-fix.rules"
RULE_DST=/etc/udev/rules.d/99-razer-bind-fix.rules

if [ "$(id -u)" -ne 0 ]; then echo "must run as root" >&2; exit 1; fi
[ -f "$RULE_SRC" ] || { echo "missing $RULE_SRC" >&2; exit 1; }

echo "==> installing $RULE_DST"
install -m 0644 "$RULE_SRC" "$RULE_DST"
udevadm control --reload
echo "    installed, rules reloaded"

# Find a razer hid node that actually exposes device_type (the control interface).
NODE=""
for d in /sys/bus/hid/drivers/razer*/*:*; do
    [ -e "$d/device_type" ] && { NODE="$d"; break; }
done

if [ -z "$NODE" ]; then
    echo "!! no razer control node attached -- skipping live test."
    echo "   Rule is installed and applies on next attach."
    exit 0
fi

SYSNAME="$(basename "$NODE")"
ATTR="$NODE/device_type"
echo "==> testing against $SYSNAME"
echo "    before: $(stat -c '%U:%G' "$ATTR")"

echo "==> breaking group (reproducing the lost race)"
chgrp -R root "$NODE"
echo "    now:    $(stat -c '%U:%G' "$ATTR")"

echo "==> firing a real 'bind' uevent"
udevadm trigger --action=bind --sysname-match="$SYSNAME"
udevadm settle
sleep 0.3

AFTER="$(stat -c '%U:%G' "$ATTR")"
echo "    after:  $AFTER"
echo

if [ "$AFTER" = "root:plugdev" ]; then
    echo "PASS -- udev repaired the group on bind. The race is covered."
else
    echo "FAIL -- still $AFTER. Restoring by hand so you are not left broken."
    chgrp -R plugdev "$NODE"
    echo "        restored to $(stat -c '%U:%G' "$ATTR")"
    echo "        Rule left installed. Check: journalctl -b -t systemd-udevd"
    exit 1
fi
