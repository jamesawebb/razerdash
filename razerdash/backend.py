"""Device backends: real OpenRazer hardware, and a hardware-free mock."""
from __future__ import annotations

import glob
import os
import sys

RAZERKBD_SYSFS = "/sys/bus/hid/drivers/razerkbd"


class DeviceHandle:
    name = "unknown"
    rows = 0
    cols = 0

    def draw(self, frame, background=(0, 0, 0)):
        raise NotImplementedError

    def clear(self):
        self.draw({}, (0, 0, 0))

    def restore(self):
        pass

    def ensure_driver_mode(self):
        return None


# --------------------------------------------------------------------------- #
# OpenRazer (real hardware)
# --------------------------------------------------------------------------- #
def _supports_matrix(dev) -> bool:
    try:
        return bool(dev.has("lighting_led_matrix"))
    except Exception:
        return hasattr(dev.fx, "advanced")


class OpenRazerDevice(DeviceHandle):
    def __init__(self, dev):
        self._dev = dev
        self.name = dev.name
        adv = dev.fx.advanced
        self.rows = adv.rows
        self.cols = adv.cols
        try:
            self._serial = dev.serial
        except Exception:
            self._serial = None

    def ensure_driver_mode(self):
        """KVM guard: on re-attach, openrazer's switch of the keyboard into
        driver mode sometimes doesn't stick, leaving it in device mode where
        custom frames are accepted-but-ignored -- every draw succeeds, LEDs
        stay dark. Returns None when there is nothing to do (already in
        driver mode, or not a razerkbd device), "fixed" after re-asserting
        driver mode, "stuck" if the re-assert didn't take (the keyboard needs
        its physical reset). Raises OSError on sysfs access problems."""
        node = self._kbd_sysfs_node()
        if node is None:
            return None
        path = os.path.join(node, "device_mode")
        with open(path, "rb") as f:
            if f.read(1) == b"\x03":
                return None
        with open(path, "wb") as f:
            f.write(b"\x03\x00")
        with open(path, "rb") as f:
            return "fixed" if f.read(1) == b"\x03" else "stuck"

    def _kbd_sysfs_node(self):
        """This keyboard's razerkbd sysfs directory (matched by serial), or
        None. Only keyboards get the driver-mode babysitting: openrazer never
        puts other device types (e.g. the Firefly) into driver mode, and they
        render custom frames fine without it."""
        if not self._serial:
            return None
        for p in glob.glob(os.path.join(RAZERKBD_SYSFS, "*", "device_serial")):
            try:
                with open(p) as f:
                    if f.read().strip() == self._serial:
                        return os.path.dirname(p)
            except OSError:
                continue
        return None

    def draw(self, frame, background=(0, 0, 0)):
        adv = self._dev.fx.advanced
        adv.matrix.reset()
        if background != (0, 0, 0):
            for r in range(self.rows):
                for c in range(self.cols):
                    adv.matrix.set(r, c, background)
        for (r, c), col in frame.items():
            if 0 <= r < self.rows and 0 <= c < self.cols:
                adv.matrix.set(r, c, col)
        adv.draw()

    def restore(self):
        # Hand the keyboard back to a normal effect on exit.
        try:
            self._dev.fx.spectrum()
        except Exception:
            pass


class OpenRazerBackend:
    def __init__(self):
        # Import lazily so a missing python3-openrazer is caught by get_backend()
        # (which falls back to the mock). We only *import* here -- the
        # DeviceManager is (re)built on every find_device() call, because its
        # device list is a snapshot taken at construction and never refreshes on
        # its own. Building it fresh each poll is what lets the keyboard be
        # picked up when it appears after startup (boot race, KVM switch-in) and
        # dropped when it goes away.
        from openrazer.client import DeviceManager  # noqa: F401
        self._DeviceManager = DeviceManager

    def find_device(self, match: str):
        try:
            dm = self._DeviceManager()
        except Exception:
            # openrazer-daemon not up yet / transient DBus error: behave as if
            # no device is attached so the daemon idles and retries next tick.
            return None
        try:
            dm.sync_effects = False
        except Exception:
            pass
        for dev in dm.devices:
            if match.lower() in dev.name.lower() and _supports_matrix(dev):
                return OpenRazerDevice(dev)
        return None


# --------------------------------------------------------------------------- #
# Mock (prints the matrix to the terminal with truecolor blocks)
# --------------------------------------------------------------------------- #
class MockDevice(DeviceHandle):
    def __init__(self, name, rows, cols):
        self.name = name
        self.rows = rows
        self.cols = cols

    def draw(self, frame, background=(0, 0, 0)):
        lines = []
        for r in range(self.rows):
            cells = []
            for c in range(self.cols):
                col = frame.get((r, c), background)
                if col == (0, 0, 0):
                    cells.append("\x1b[2m · \x1b[0m")
                else:
                    cells.append(f"\x1b[48;2;{col[0]};{col[1]};{col[2]}m   \x1b[0m")
            lines.append(f"{r:>2} " + "".join(cells))
        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()


class MockBackend:
    def __init__(self, rows=8, cols=23, name="Razer BlackWidow V4 (mock)"):
        self.rows = rows
        self.cols = cols
        self.name = name

    def find_device(self, match: str):
        # Known non-keyboard profiles, so a multi-device config renders with
        # the right matrix shapes when developing without hardware.
        if "firefly" in match.lower():
            return MockDevice("Razer Firefly V2 (mock)", 1, 19)
        return MockDevice(self.name, self.rows, self.cols)


def get_backend(kind: str, logger=None):
    if kind == "mock":
        return MockBackend()
    if kind == "openrazer":
        return OpenRazerBackend()
    # auto: prefer real hardware, fall back to the mock for dev/testing.
    try:
        return OpenRazerBackend()
    except Exception as e:
        if logger:
            logger.warning("OpenRazer unavailable (%s); using mock backend", e)
        return MockBackend()
