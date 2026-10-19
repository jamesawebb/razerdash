"""Recovery for the KVM blackout: a keyboard whose LED controller arrives wedged.

After a KVM switch-in the Blackwidow V4 can come back with every LED dark --
the matrix *and* the Caps Lock indicator -- while `device_mode` reads a healthy
0x03 and every draw reports success. The driver-mode guard in backend.py never
fires because driver mode is not the problem: the firmware's LED engine is
stuck, and nothing sent over USB reaches it. A `device_mode` toggle, a full
re-enumeration (`authorized` 0 -> 1), new openrazer init writes: all leave it
dark. Only losing bus power clears it.

That used to mean unplugging the cable, but the kernel can cut the power for
us. Writing 1 to the hub port's `disable` clears PORT_POWER when the hub
supports per-port power switching, so `disable` 1 -> wait -> 0 is a replug
without the cable (and without taking the keyboard off the KVM).

There is deliberately no detector, so nothing calls this automatically:
`razerdash fix-blackout` is the way in. Nothing readable tells a wedged
keyboard from a healthy one -- `device_mode`, `poll_rate`, serial, firmware
version, layout and brightness are identical, and draws are fire-and-forget.
The last candidate, whether a `matrix_brightness` write still lands, was
caught during the blackout of 2026-10-19 and the write landed exactly as on a
healthy keyboard, so it was removed. The data is in contrib/kvm-blackout/.
"""
from __future__ import annotations

import os
import time

USB_DEVICES = "/sys/bus/usb/devices"

OFF_SECONDS = 3.0      # power off for this long; 3s is what the probe proved


def _write(path, value) -> None:
    with open(path, "w") as f:
        f.write(str(value))


def usb_port(node):
    """The hub port directory the keyboard hangs off, e.g.
    `/sys/bus/usb/devices/3-4.1.3:1.0/3-4.1.3-port2`, or None when it can't be
    derived. `node` is the razerkbd sysfs directory, whose realpath is
    `<usb device>/<interface>/<hid node>`."""
    usbdev = os.path.dirname(os.path.dirname(os.path.realpath(node)))
    return port_dir(os.path.basename(usbdev))


def port_dir(sysname, root=None):
    """Hub port directory for a USB device sysname, or None if there is no
    `disable` there to write. Port attributes live under the *hub's* interface
    directory: `3-4.1.3.2` (port 2 of hub `3-4.1.3`) is
    `3-4.1.3:1.0/3-4.1.3-port2`, while a device straight on a root hub spells
    it differently -- `3-4` is `3-0:1.0/usb3-port4`."""
    root = USB_DEVICES if root is None else root
    if "." in sysname:
        hub, port = sysname.rsplit(".", 1)
        rel = f"{hub}:1.0/{hub}-port{port}"
    elif "-" in sysname:
        bus, port = sysname.split("-", 1)
        if not (bus.isdigit() and port.isdigit()):
            return None
        rel = f"{bus}-0:1.0/usb{bus}-port{port}"
    else:
        return None  # a root hub itself ("usb3"); it hangs off no port
    d = os.path.join(root, rel)
    return d if os.path.exists(os.path.join(d, "disable")) else None


def power_cycle(port, off_seconds=OFF_SECONDS) -> None:
    """Cut `port`'s power for `off_seconds`, then restore it.

    Needs write access to the port's `disable`, which is root-owned by default:
    install contrib/kvm-blackout/99-razerdash-port-power.rules to hand it to
    plugdev. Raises OSError when the write is denied -- the caller is expected
    to tell the user to replug instead.
    """
    path = os.path.join(port, "disable")
    _write(path, 1)
    try:
        time.sleep(off_seconds)
    finally:
        _write(path, 0)
