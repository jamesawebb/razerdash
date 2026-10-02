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

This module is the mechanism: find the port, decide whether the keyboard is
wedged, cut the power. The policy -- test only on attach, how many attempts
before giving up -- lives in `Daemon._recover_blackout`.

The probe this came from, with the full ladder of recoveries that *don't* work,
is in contrib/kvm-blackout/.
"""
from __future__ import annotations

import os
import time

USB_DEVICES = "/sys/bus/usb/devices"

OFF_SECONDS = 3.0      # power off for this long; 3s is what the probe proved
SETTLE_SECONDS = 8.0   # then let openrazer re-init before drawing again
MAX_ATTEMPTS = 2       # power cycles per blackout before asking for a replug
PROBES = 3             # rejected writes before believing the engine is wedged
PROBE_GAP = 0.3
SETTLE_READS = 6       # reads per probe while waiting for a write to land
SETTLE_GAP = 0.04      # a healthy keyboard answers from ~10-30ms on

FULL = 255


def _read_int(path) -> int:
    with open(path) as f:
        return int(f.read().strip() or "0")


def _write(path, value) -> None:
    with open(path, "w") as f:
        f.write(str(value))


def _stable_read(path, tries=5, gap=SETTLE_GAP) -> int:
    """A brightness value worth believing: the first one read twice in a row,
    else the last. Single reads cannot be trusted -- see `_landed`."""
    last = None
    for i in range(tries):
        if i:
            time.sleep(gap)
        v = _read_int(path)
        if v == last:
            return v
        last = v
    return last


def _landed(path, target, reads=SETTLE_READS, gap=SETTLE_GAP) -> bool:
    """Does `target` turn up in the read-back within reads*gap?

    Measured on a healthy Blackwidow V4 (v1.3): a read issued immediately after
    a brightness write answers **0**, the written value appears from ~10-30ms
    on, and the odd junk value ('1', '5', '8') shows up in between. So a single
    read right after a write proves nothing -- which is exactly the trap the
    first probe run fell into, reading 0 and calling it a wedged engine.
    """
    for _ in range(reads):
        time.sleep(gap)
        if _read_int(path) == target:
            return True
    return False


def wedged(node, probes=PROBES, gap=PROBE_GAP) -> bool:
    """Is this keyboard's LED engine wedged? `node` is its razerkbd sysfs
    directory.

    This is the one candidate signal there is: whether the firmware still takes
    a write. Everything readable -- `device_mode`, `poll_rate`, serial, firmware
    version, layout, brightness -- is identical dark and healthy, and draws are
    fire-and-forget, so nothing else tells the two states apart.

    It is **unproven**: the blackout has not yet been caught with this test (the
    one dark reading of `matrix_brightness 0` turned out to be the read-after-
    write artifact above, which healthy keyboards show too). It is written to
    fail safe -- a wedged keyboard that still accepts brightness writes reads as
    healthy, and the recovery simply doesn't fire, leaving `razerdash
    fix-blackout` to do it by hand.

    A rejected write is confirmed `probes` times before saying yes, because the
    answer powers the keyboard off. The original brightness is always put back,
    so on a healthy keyboard this is a no-op (at worst a brief flicker).

    Raises OSError if the attribute cannot be read or written -- not in the
    plugdev group, or the device went away mid-test.
    """
    path = os.path.join(node, "matrix_brightness")
    original = _stable_read(path)
    # Always write a *different* value than the one already there: writing the
    # value a device already holds goes nowhere near the firmware.
    target = FULL - 1 if original >= FULL else FULL
    try:
        for i in range(probes):
            if i:
                time.sleep(gap)
            _write(path, target)
            if _landed(path, target):
                return False  # the firmware took a write: not wedged
        return True
    finally:
        try:
            _write(path, original)
        except OSError:
            pass  # wedged keyboards ignore this too; nothing left to restore


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
