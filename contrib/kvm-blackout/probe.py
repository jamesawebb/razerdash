#!/usr/bin/env python3
"""Capture the BlackWidow V4 KVM blackout while it is happening, and find the
cheapest recovery that works. Run it when the keyboard is dark, BEFORE
unplugging it:

    python3 contrib/kvm-blackout/probe.py            # snapshot + recovery ladder
    python3 contrib/kvm-blackout/probe.py --snapshot # read-only state dump only

The full run also writes: a brightness write/read-back trace (the candidate
detector razerdash uses) and the effects each recovery step tests. --snapshot
touches nothing.

Use the system python3 (it has python3-openrazer), not the pipx venv. razerdash
is paused for the duration via its calibrate marker, so it can't paint over
the tests. Everything is written to ~/.local/state/razerdash/blackout-*.log.
"""
from __future__ import annotations

import datetime
import glob
import os
import subprocess
import sys
import time

MATCH = "blackwidow v4"
RAZERKBD_SYSFS = "/sys/bus/hid/drivers/razerkbd"
PAUSE_FILE = os.path.expanduser("~/.config/razerdash/.calibrating")
LOG_DIR = os.path.expanduser("~/.local/state/razerdash")
# sysfs attributes that the razerkbd driver answers by querying the keyboard
# itself, so they show the firmware's view, not a cached one.
LIVE_ATTRS = ("device_mode", "matrix_brightness", "firmware_version",
              "device_serial", "poll_rate", "game_led_state",
              "macro_led_state", "kbd_layout")

_log = None


def say(msg=""):
    print(msg)
    _log.write(msg + "\n")
    _log.flush()


def ask(question):
    try:
        ans = input(f"{question} [y/N] ").strip().lower()
    except EOFError:
        ans = ""
    _log.write(f"{question} -> {ans or 'n'}\n")
    return ans.startswith("y")


def sh(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return (r.stdout + r.stderr).rstrip()


def keyboard():
    """(openrazer client device, razerkbd control node) for the keyboard, or
    (None, None). The node is found by serial, as razerdash's guard does."""
    from openrazer.client import DeviceManager
    try:
        dm = DeviceManager()
        dm.sync_effects = False
    except Exception:
        return None, None
    dev = next((d for d in dm.devices if MATCH in d.name.lower()), None)
    if dev is None:
        return None, None
    for p in glob.glob(os.path.join(RAZERKBD_SYSFS, "*", "device_serial")):
        try:
            with open(p) as f:
                if f.read().strip() == dev.serial:
                    return dev, os.path.dirname(p)
        except OSError:
            continue
    return dev, None


def wait_for_keyboard(timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        dev, node = keyboard()
        if dev is not None and node is not None:
            time.sleep(3)  # let openrazer finish its init writes
            return keyboard()
        time.sleep(1)
    return None, None


def read_attr(node, name):
    try:
        with open(os.path.join(node, name), "rb") as f:
            raw = f.read()
    except OSError as e:
        return f"<{e.strerror}>"
    if name == "device_mode":
        return raw.hex(" ")
    return raw.decode(errors="replace").strip()


def usb_paths(node):
    """(usb device dir, hub port dir) for the keyboard, e.g. 3-4.1.3.2 and
    .../3-4.1.3:1.0/3-4.1.3-port2."""
    usbdev = os.path.dirname(os.path.dirname(os.path.realpath(node)))
    name = os.path.basename(usbdev)
    hub, port = name.rsplit(".", 1)
    return usbdev, f"/sys/bus/usb/devices/{hub}:1.0/{hub}-port{port}"


def snapshot(dev, node):
    say("== live state (queried from the keyboard firmware)")
    for a in LIVE_ATTRS:
        say(f"  {a:18} {read_attr(node, a)}")
    say("== openrazer's (cached) view")
    for label, fn in (("brightness", lambda: dev.brightness),
                      ("effect", lambda: dev.fx.effect)):
        try:
            say(f"  {label:18} {fn()}")
        except Exception as e:
            say(f"  {label:18} <{e}>")
    usbdev, port = usb_paths(node)
    say("== usb")
    say(f"  node     {os.path.realpath(node)}")
    say(f"  device   {usbdev} devnum={read_attr(usbdev, 'devnum')} "
        f"runtime={read_attr(usbdev, 'power/runtime_status')}")
    say(f"  port     {port} state={read_attr(port, 'state')} "
        f"over_current={read_attr(port, 'over_current_count')}")
    say("== kernel log (last 40)")
    say(sh("journalctl -k -n 40 --no-pager"))
    say("== openrazer-daemon log (last 15)")
    say(sh("journalctl --user -u openrazer-daemon -n 15 --no-pager"))
    say("== razerdash log (last 15, draws omitted)")
    say(sh("journalctl --user -u razerdash -n 400 --no-pager | grep -v ' drew ' | tail -15"))


def custom_frame(dev, every_other=None):
    """Draw a custom frame the way razerdash does. With `every_other` set, the
    odd columns get that colour: a pattern no whole-board hardware effect can
    imitate, so there is no mistaking which path drew it. (The 2026-10-01 run
    flashed flat colours for 0.5s each and the answer was "stayed white", which
    could mean either "custom frames are ignored" or "I missed it".)"""
    adv = dev.fx.advanced
    adv.matrix.reset()
    for r in range(adv.rows):
        for c in range(adv.cols):
            adv.matrix.set(r, c, every_other if (every_other and c % 2)
                           else (255, 255, 255))
    adv.draw()


def check_lit(dev):
    """After a step: does a hardware effect light it, and do razerdash-style
    custom frames? Returns True once custom frames work (fully recovered).
    Each state is held while the question is on screen, not flashed."""
    dev.fx.static(255, 255, 255)
    if not ask("Keyboard now solid WHITE (hardware static effect)?"):
        return False
    custom_frame(dev, every_other=(255, 0, 0))
    time.sleep(1.0)  # openrazer may still be mid-init on a fresh enumeration
    ok = ask("Now alternating WHITE/RED columns (a custom frame, what "
             "razerdash sends)?")
    custom_frame(dev)
    return ok


def brightness_trace(node):
    """Write a brightness and watch the read-back settle. This is the candidate
    blackout detector: on healthy hardware the written value lands within ~30ms
    (the read issued immediately after a write always answers 0, and junk values
    turn up in between), so if a *dark* keyboard never shows it, razerdash can
    detect the blackout on attach. If it lands exactly like a healthy one, the
    detector is dead and razerdash can only offer `fix-blackout`."""
    path = os.path.join(node, "matrix_brightness")
    with open(path) as f:
        original = f.read().strip()
    target = "254" if original == "255" else "255"

    def read():
        with open(path) as f:
            return f.read().strip()

    reads = []
    t0 = time.monotonic()
    with open(path, "w") as f:
        f.write(target)
    for d in (0, 0.01, 0.02, 0.03, 0.06, 0.13, 0.25, 0.5):
        time.sleep(d)
        reads.append((int((time.monotonic() - t0) * 1000), read()))
    with open(path, "w") as f:
        f.write(original)
    say(f"== brightness write test (was {original}, wrote {target})")
    say("  " + "  ".join(f"{ms}ms:{v}" for ms, v in reads))
    landed = any(v == target for _, v in reads)
    say(f"  -> the write {'LANDED' if landed else 'NEVER LANDED'}: "
        f"{'no use as a detector' if landed else 'this detects the blackout'}")


def step_brightness(dev, node):
    dev.brightness = 100.0
    say(f"  matrix_brightness now {read_attr(node, 'matrix_brightness')}")
    return dev, node


def step_mode_toggle(dev, node):
    for mode in (b"\x00\x00", b"\x03\x00"):
        with open(os.path.join(node, "device_mode"), "wb") as f:
            f.write(mode)
        time.sleep(1)
    say(f"  device_mode now {read_attr(node, 'device_mode')}")
    return dev, node


def step_sudo(cmd_fmt):
    """Run a root-only sysfs poke, then wait for the keyboard to re-appear."""
    def run(dev, node):
        usbdev, port = usb_paths(node)
        cmd = cmd_fmt.format(usbdev=usbdev, port=port)
        say(f"  $ sudo sh -c '{cmd}'")
        rc = subprocess.run(["sudo", "sh", "-c", cmd]).returncode
        say(f"  exit {rc}; waiting for the keyboard to come back...")
        dev, node = wait_for_keyboard()
        if dev is None:
            say("  keyboard did not come back within 30s")
        return dev, node
    return run


STEPS = [
    ("hardware static effect only (no reset)",
     lambda dev, node: (dev, node)),
    ("brightness to 100%",
     step_brightness),
    ("device_mode 0x00 -> 0x03 toggle",
     step_mode_toggle),
    ("USB re-enumerate, power kept (authorized 0 -> 1, needs sudo)",
     step_sudo("echo 0 > {usbdev}/authorized; sleep 2; echo 1 > {usbdev}/authorized")),
    ("hub port power-off for 3s (port disable, needs sudo)",
     step_sudo("echo 1 > {port}/disable; sleep 3; echo 0 > {port}/disable")),
]


def main():
    global _log
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, datetime.datetime.now()
                        .strftime("blackout-%Y%m%d-%H%M%S.log"))
    _log = open(path, "w", encoding="utf-8")
    snap_only = "--snapshot" in sys.argv[1:]

    dev, node = keyboard()
    if dev is None or node is None:
        say(f"no openrazer keyboard matching {MATCH!r} with a razerkbd node; "
            "run `razerdash list-devices` -- this may be the udev bind race "
            "(contrib/udev-bind-race), not the blackout")
        return 1
    say(f"{dev.name} serial={dev.serial} at {datetime.datetime.now():%F %T}")
    snapshot(dev, node)
    if snap_only:
        say(f"\nlog: {path}")
        return 0

    with open(PAUSE_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    try:
        say("\nPaused razerdash; waiting for it to stop drawing...")
        time.sleep(6)
        say("First, with the keyboard still dark:")
        ask("Does the Caps Lock indicator light when you press Caps Lock?")
        try:
            brightness_trace(node)
        except OSError as e:
            say(f"  brightness write test failed: {e}")
        for i, (label, step) in enumerate(STEPS, 1):
            say(f"\n-- step {i}: {label}")
            try:
                dev, node = step(dev, node)
                if dev is None:
                    break
                if check_lit(dev):
                    say(f"RECOVERED at step {i}: {label}")
                    snapshot(dev, node)
                    break
            except Exception as e:
                say(f"  step failed: {e!r}")
        else:
            say("\nNothing short of a physical replug worked.")
    finally:
        try:
            os.remove(PAUSE_FILE)
        except OSError:
            pass
        say(f"\nrazerdash resumed. log: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
