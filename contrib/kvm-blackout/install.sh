#!/bin/bash
# Install the udev rule that lets razerdash power-cycle a Razer device's USB
# port -- the only recovery for the KVM blackout -- and prove it actually fires.
#   sudo bash contrib/kvm-blackout/install.sh
set -u

SRC="$(dirname "$(realpath "$0")")"
RULE_DST=/etc/udev/rules.d/99-razerdash-port-power.rules
HELPER_DST=/usr/local/sbin/razerdash-port-power-perms

if [ "$(id -u)" -ne 0 ]; then echo "must run as root" >&2; exit 1; fi
for f in 99-razerdash-port-power.rules razerdash-port-power-perms; do
    [ -f "$SRC/$f" ] || { echo "missing $SRC/$f" >&2; exit 1; }
done

# Before touching anything: udev drops a rule line it cannot parse and says so
# only in its own journal, so a typo here looks exactly like a rule that ran and
# did nothing. (DEVTYPE=="usb_device" instead of ENV{DEVTYPE} cost one round of
# this.) `udevadm verify` needs no root and exists from systemd 251.
if udevadm verify --help >/dev/null 2>&1; then
    echo "==> checking the rule parses"
    if ! udevadm verify "$SRC/99-razerdash-port-power.rules"; then
        echo "!! the rule file does not parse -- nothing installed" >&2
        exit 1
    fi
fi

echo "==> installing $HELPER_DST"
install -m 0755 "$SRC/razerdash-port-power-perms" "$HELPER_DST"
echo "==> installing $RULE_DST"
install -m 0644 "$SRC/99-razerdash-port-power.rules" "$RULE_DST"
udevadm control --reload
echo "    installed, rules reloaded"

# Prefer a keyboard (what the blackout hits); fall back to any Razer device.
DEV=""
for node in /sys/bus/hid/drivers/razerkbd/*/; do
    [ -e "$node/device_serial" ] || continue
    DEV="$(basename "$(dirname "$(dirname "$(realpath "$node")")")")"
    break
done
if [ -z "$DEV" ]; then
    for v in /sys/bus/usb/devices/*/idVendor; do
        [ -e "$v" ] && [ "$(cat "$v")" = "1532" ] || continue
        DEV="$(basename "$(dirname "$v")")"
        break
    done
fi
if [ -z "$DEV" ]; then
    echo "!! no Razer device attached -- skipping the live test."
    echo "   The rule applies on the next attach (KVM switch-in or replug)."
    exit 0
fi

ATTR="$("$HELPER_DST" -n "$DEV")"
echo "==> testing against $DEV -> $ATTR"
if [ ! -e "$ATTR" ]; then
    echo "!! this hub exposes no per-port power switch ('disable' is missing),"
    echo "   so the blackout can only be cleared by replugging. razerdash will"
    echo "   say so in the journal rather than cutting power."
    exit 1
fi

echo "==> resetting the attribute to root:root 0644 (the default state)"
chgrp root "$ATTR"
chmod 644 "$ATTR"
echo "    before: $(stat -c '%U:%G %a' "$ATTR")"

echo "==> firing a real 'add' uevent for $DEV"
udevadm trigger --action=add --sysname-match="$DEV"
udevadm settle
sleep 0.3

AFTER="$(stat -c '%U:%G %a' "$ATTR")"
echo "    after:  $AFTER"
echo

if [ "$AFTER" = "root:plugdev 664" ]; then
    echo "PASS -- udev handed that port's power switch to plugdev."
    echo "        razerdash can now clear a blackout without a replug."
else
    echo "FAIL -- still $AFTER. Granting it by hand so you are not left broken."
    "$HELPER_DST" "$DEV"
    echo "        now $(stat -c '%U:%G %a' "$ATTR")"
    echo "        (that lasts until the hub re-enumerates -- i.e. the next KVM"
    echo "         switch-in -- so the rule still needs fixing)"
    WHY="$(journalctl -b -t systemd-udevd --no-pager 2>/dev/null \
           | grep -F 99-razerdash-port-power | tail -3)"
    [ -n "$WHY" ] && { echo; echo "udevd said:"; echo "$WHY"; }
    echo
    echo "        Rule left installed. More: journalctl -b -t systemd-udevd"
    exit 1
fi
