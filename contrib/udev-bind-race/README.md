# udev bind-race fix

Stops the Blackwidow V4 (or any Razer device) intermittently failing to appear,
leaving `razerdash` with nothing to light.

## Symptom

`razerdash list-devices` reports:

```
keyboard: no device matching 'BlackWidow V4'
```

while `lsusb` clearly shows the keyboard and it is bound to `razerkbd`. The
daemon log fills with draws for the remaining device only:

```
INFO drew Razer Firefly V2: (nothing)
```

The tell is in the openrazer daemon log:

```
journalctl --user -u openrazer-daemon | grep plugdev
CRITICAL | Could not access /sys/.../0003:1532:0287.0006/device_type,
           file is not owned by plugdev
```

It is intermittent — it depends on losing a race at boot or on KVM re-attach.

## Cause

Not a razerdash bug. openrazer's shipped
`/usr/lib/udev/rules.d/99-razer.rules` acts only on `ACTION=="add"`, and the
`razer_mount` helper it invokes guards its `chgrp -R plugdev` behind:

```sh
if [ -d /sys/bus/hid/drivers/"$DRIVER"/"$DEVICE_ID" ] ; then
	chgrp -R plugdev /sys/bus/hid/drivers/"$DRIVER"/"$DEVICE_ID"/
fi
```

For a hid interface the `add` uevent can fire *before* the `razer*` driver has
finished binding and created its sysfs attributes. That test then fails, the
chgrp never runs, and the node is left `root:root`. `openrazer-daemon` refuses
any device whose `device_type` is not group `plugdev`, so it drops it from the
DBus device list — and razerdash, correctly, reports no such device.

Only the control interface matters (the one exposing `device_type`). A boot can
chgrp four of the keyboard's five hid nodes and still lose the one that counts.

## Fix

`99-razer-bind-fix.rules` hooks `ACTION=="bind"`, which fires *after* the driver
has attached and populated sysfs — exactly when the group change needs to
happen. It adds to the shipped rule rather than replacing it; on a boot that
wins the original race the chgrp just runs twice, which is harmless.

```sh
sudo bash contrib/udev-bind-race/install.sh
```

`install.sh` installs the rule, then proves it works without a reboot: it
deliberately sets the group back to `root`, fires a real `bind` uevent with
`udevadm trigger --action=bind`, and checks udev restored `plugdev`. It prints
`PASS` or restores the permission by hand and prints `FAIL`.

It lives in `/etc/udev/rules.d/`, so an openrazer package update will not
clobber it.

## Recovering by hand

If it happens before the rule is in place:

```sh
sudo chgrp -R plugdev /sys/bus/hid/drivers/razerkbd/0003:1532:0287.0006/
systemctl --user restart openrazer-daemon.service
systemctl --user restart razerdash.service
razerdash list-devices
```

## Note

This is a different failure from the KVM blank-keyboard problem in the root
`CLAUDE.md`. There, every draw *succeeds* and the keys stay dark, fixed by
re-asserting `device_mode = 0x03`. Here the device never appears at all. Check
`list-devices` first to tell them apart.
