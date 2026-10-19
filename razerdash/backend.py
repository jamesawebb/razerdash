"""Device backends: real OpenRazer hardware, and a hardware-free mock."""
from __future__ import annotations

import glob
import os
import sys
import time

RAZERKBD_SYSFS = "/sys/bus/hid/drivers/razerkbd"
HID_SYSFS = "/sys/bus/hid/devices"

# Write pacing per model, keyed by a substring of the OpenRazer device name:
# (gap, min_interval) in seconds. The Firefly V2 is still busy applying a
# frame when the next command arrives straight after it; razeraccessory waits
# only ~1 ms for a reply, gets the stale one, and logs "Response doesn't match
# request" (class 0f, id 03, status 01 busy). The gap is the settle time we
# leave it before each command; min_interval caps how often it gets a new
# frame (0 = uncapped, so it keeps up with an animated background). Both can
# be overridden per device in the config; min_interval is a daemon policy,
# applied through pace(), so the interactive tools (calibrate, webedit) stay
# responsive.
#
# Measured at 10 fps against the keyboard's writes (warnings per 1800 Firefly
# frames): 0 ms -> 18, 10 ms -> 5, 20 ms -> 0, 50 ms -> 0. 30 ms leaves a
# margin, and the daemon's animation tick absorbs it (Daemon._animate).
PACING = {"Firefly V2": (0.03, 0.0)}


class DeviceGone(Exception):
    """The device left the bus (KVM switch, unplug) or openrazer lost it."""


def _pacing(name):
    for key, pacing in PACING.items():
        if key.lower() in name.lower():
            return pacing
    return 0.0, 0.0


class DeviceHandle:
    name = "unknown"
    rows = 0
    cols = 0
    gap = 0.0           # settle time before each command sent to the device
    min_interval = 0.0  # minimum time between frames written
    waited = 0.0        # total settle time slept so far
    _model_gap = 0.0
    _model_interval = 0.0
    _last = None        # payload last written, for skipping identical frames
    _sent_at = None     # monotonic time of that write

    def draw(self, frame, background=(0, 0, 0)):
        """Show `frame` over `background`. Returns True if a frame was
        written, False if it was skipped: identical to the one the device
        already shows, or inside min_interval (then the next draw after the
        interval writes it)."""
        payload = self._payload(frame, background)
        if payload == self._last:
            return False
        if (self._sent_at is not None
                and time.monotonic() - self._sent_at < self.min_interval):
            return False
        self._write(payload)
        self._last, self._sent_at = payload, time.monotonic()
        return True

    def clear(self):
        return self.draw({}, (0, 0, 0))

    def forget(self):
        """Something else may have drawn since our last write (calibrate,
        a driver-mode fix): write the next frame even if it is identical."""
        self._last = self._sent_at = None

    def pace(self, gap=None, min_interval=None):
        """Daemon-side pacing from the device's config; None keeps the
        model default from PACING."""
        self.gap = self._model_gap if gap is None else gap
        self.min_interval = (self._model_interval if min_interval is None
                             else min_interval)

    def _payload(self, frame, background):
        """The frame as OpenRazer's matrix_custom_frame rows: per row
        ROW_ID, START_COL, STOP_COL, then RGB for every column."""
        out = []
        for r in range(self.rows):
            row = bytearray((r, 0, self.cols - 1))
            for c in range(self.cols):
                row += bytes(int(v) for v in frame.get((r, c), background))
            out.append(bytes(row))
        return tuple(out)

    def _write(self, payload):
        raise NotImplementedError

    def restore(self):
        pass

    def ensure_driver_mode(self):
        return None

    def kbd_sysfs_node(self):
        """This device's razerkbd sysfs directory, or None when it has none
        (not a keyboard, or no hardware at all). `razerdash fix-blackout`
        needs it to find the keyboard's USB port (blackout.py)."""
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
    # Monotonic time of the last command razerdash sent to any device: a
    # paced device's gap also runs from the previous device's write.
    _last_io = 0.0

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
        self._model_gap, self._model_interval = _pacing(self.name)
        self.gap = self._model_gap
        self.gone = False
        self._chroma = None  # DBus interfaces, opened at first draw
        self._nodes = None   # HID sysfs nodes seen at first draw

    def attached(self):
        """False once a write failed or the device's HID interfaces changed:
        the handle is spent, and the backend opens a fresh one when
        openrazer-daemon reports the device again."""
        if not self.gone and not self._present():
            self.gone = True
        return not self.gone

    def _present(self):
        # The device's HID interfaces start vanishing from sysfs as soon as
        # the kernel tears it down, before openrazer-daemon notices; writes
        # in that window "succeed" while the kernel logs a failed transfer
        # per row. A re-enumeration (new node names) also counts: the device
        # came back blank, and the fresh handle will redraw it.
        if self._nodes is None:
            return True
        return self._hid_nodes(*self._nodes[0]) == self._nodes[1]

    @staticmethod
    def _hid_nodes(vid, pid):
        return frozenset(glob.glob(os.path.join(
            HID_SYSFS, f"*:{vid:04X}:{pid:04X}.*")))

    def _open(self):
        import dbus
        obj = dbus.SessionBus().get_object(
            "org.razer", f"/org/razer/device/{self._serial}")
        self._chroma = dbus.Interface(obj, "razer.device.lighting.chroma")
        try:
            vid, pid = dbus.Interface(obj, "razer.device.misc").getVidPid()
            ids = (int(vid), int(pid))
            nodes = self._hid_nodes(*ids)
            if nodes:  # no sysfs nodes to watch => skip the presence check
                self._nodes = (ids, nodes)
        except Exception:
            pass

    def _send(self, method, *args):
        wait = OpenRazerDevice._last_io + self.gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
            self.waited += wait
        try:
            method(*args)
        finally:
            OpenRazerDevice._last_io = time.monotonic()

    def _write(self, payload):
        if not self.attached():
            raise DeviceGone(f"{self.name} is no longer on the bus")
        try:
            if self._chroma is None:
                self._open()
            if self.gap:
                # One row per setKeyRow, so the gap separates every USB
                # command: each row, then the apply.
                for row in payload:
                    self._send(self._chroma.setKeyRow, row)
            else:
                self._send(self._chroma.setKeyRow, b"".join(payload))
            self._send(self._chroma.setCustom)
        except Exception:
            self.gone = True  # DBus/openrazer error: stop writing to it
            raise

    def ensure_driver_mode(self):
        """KVM guard: on re-attach, openrazer's switch of the keyboard into
        driver mode sometimes doesn't stick, leaving it in device mode where
        custom frames are accepted-but-ignored -- every draw succeeds, LEDs
        stay dark. Returns None when there is nothing to do (already in
        driver mode, or not a razerkbd device), "fixed" after re-asserting
        driver mode, "stuck" if the re-assert didn't take (the keyboard needs
        its physical reset). Raises OSError on sysfs access problems."""
        node = self.kbd_sysfs_node()
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

    def kbd_sysfs_node(self):
        """This keyboard's razerkbd sysfs directory (matched by serial), or
        None. Only keyboards get the KVM babysitting (driver mode, blackout
        recovery): openrazer never puts other device types (e.g. the Firefly)
        into driver mode, and they render custom frames fine without it."""
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

    def restore(self):
        # Hand the keyboard back to a normal effect on exit.
        try:
            self._dev.fx.spectrum()
        except Exception:
            pass


class OpenRazerBackend:
    def __init__(self):
        # Import lazily so a missing python3-openrazer is caught by get_backend()
        # (which falls back to the mock).
        import dbus
        from openrazer.client.device import RazerDeviceFactory
        self._dbus = dbus
        self._factory = RazerDeviceFactory
        self._devices = None   # openrazer-daemon's razer.devices interface
        self._handles = {}     # serial -> OpenRazerDevice, kept across probes
        self._names = {}       # serial -> device name
        self._no_matrix = set()

    def _serials(self):
        """The serials openrazer-daemon currently reports: one DBus call, no
        USB traffic (a DeviceManager reads every device's firmware version).
        None when the daemon isn't reachable."""
        try:
            if self._devices is None:
                obj = self._dbus.SessionBus().get_object("org.razer", "/org/razer")
                self._devices = self._dbus.Interface(obj, "razer.devices")
                try:
                    self._devices.syncEffects(False)
                except Exception:
                    pass
            return [str(s) for s in self._devices.getDevices()]
        except Exception:
            # openrazer-daemon not up yet / restarted / transient DBus error:
            # behave as if no device is attached so the daemon idles and
            # retries next tick, reconnecting then.
            self._devices = None
            return None

    def _name(self, serial):
        if serial not in self._names:
            try:
                obj = self._dbus.SessionBus().get_object(
                    "org.razer", f"/org/razer/device/{serial}")
                self._names[serial] = str(self._dbus.Interface(
                    obj, "razer.device.misc").getDeviceName())
            except Exception:
                return None
        return self._names[serial]

    def find_device(self, match: str):
        """The attached device whose name contains `match`, or None. The
        handle is reused across probes (it remembers the frame on the
        device); one that has gone away is dropped here, and a fresh one is
        opened once openrazer-daemon reports the device again -- at most
        once per probe, never in a tight loop."""
        serials = self._serials()
        if serials is None:
            return None
        for serial, h in list(self._handles.items()):
            if serial not in serials or not h.attached():
                del self._handles[serial]
        for serial in serials:
            name = self._name(serial)
            if (name is None or match.lower() not in name.lower()
                    or serial in self._no_matrix):
                continue
            h = self._handles.get(serial)
            if h is None:
                try:
                    dev = self._factory.get_device(serial)
                except Exception:
                    continue  # mid-attach or just removed; next probe
                if not _supports_matrix(dev):
                    self._no_matrix.add(serial)
                    continue
                h = self._handles[serial] = OpenRazerDevice(dev)
            return h
        return None


# --------------------------------------------------------------------------- #
# Mock (prints the matrix to the terminal with truecolor blocks)
# --------------------------------------------------------------------------- #
class MockDevice(DeviceHandle):
    def __init__(self, name, rows, cols):
        self.name = name
        self.rows = rows
        self.cols = cols

    def _write(self, payload):
        lines = []
        for r, row in enumerate(payload):
            cells = []
            for c in range(self.cols):
                col = tuple(row[3 + 3 * c:6 + 3 * c])
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
