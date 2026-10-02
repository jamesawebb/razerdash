"""KVM-blackout recovery: wedge detection, USB port lookup, power cycle, and
the daemon policy around them (contrib/kvm-blackout/ has the hardware data)."""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import yaml
from razerdash import blackout
from razerdash.backend import MockDevice
from razerdash.config import ConfigError, ConfigWatcher, parse_config
from razerdash.daemon import Daemon

TMP = tempfile.mkdtemp(prefix="razerdash-test-")
BINDING = {"name": "x", "metric": "up",
           "key_group": {"type": "row", "index": 0},
           "lighting": {"method": "fill_fixed", "color": "#00e5ff"}}


def _touch(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


# --- 1. port_dir: hub port, root-hub port, and the cases with no port ---------
usb = os.path.join(TMP, "usb-devices")
_touch(os.path.join(usb, "3-4.1.3:1.0", "3-4.1.3-port2", "disable"), "0\n")
_touch(os.path.join(usb, "3-0:1.0", "usb3-port4", "disable"), "0\n")
os.makedirs(os.path.join(usb, "3-4.1.3:1.0", "3-4.1.3-port9"))  # no `disable`

assert blackout.port_dir("3-4.1.3.2", usb) == os.path.join(
    usb, "3-4.1.3:1.0", "3-4.1.3-port2")
assert blackout.port_dir("3-4", usb) == os.path.join(
    usb, "3-0:1.0", "usb3-port4")
assert blackout.port_dir("3-4.1.3.9", usb) is None   # port dir without disable
assert blackout.port_dir("3-4.1.3.7", usb) is None   # no such port dir
assert blackout.port_dir("usb3", usb) is None        # a root hub itself
assert blackout.port_dir("3-4.1", usb) is None       # hub 3-4 not faked here
print("1. port_dir maps a USB sysname to its hub port: OK")

# --- 2. usb_port: razerkbd node -> that port, via the real symlink layout -----
# A razerkbd node is <usb device>/<interface>/<hid node>, reached through
# /sys/bus/hid/drivers/razerkbd/<hid node> as a symlink.
dev_dir = os.path.join(TMP, "devices", "3-4.1.3", "3-4.1.3.2")
hid = os.path.join(dev_dir, "3-4.1.3.2:1.2", "0003:1532:0287.0001")
_touch(os.path.join(hid, "device_serial"), "XY123\n")
drivers = os.path.join(TMP, "drivers", "razerkbd")
os.makedirs(drivers)
link = os.path.join(drivers, "0003:1532:0287.0001")
os.symlink(hid, link)

blackout.USB_DEVICES = usb  # the module default is the real /sys
assert blackout.usb_port(link) == os.path.join(
    usb, "3-4.1.3:1.0", "3-4.1.3-port2")
print("2. usb_port resolves a razerkbd node to its hub port: OK")

# --- 3. wedged(): a firmware that takes writes vs one that ignores them -------
node = os.path.join(TMP, "node")
bright = _touch(os.path.join(node, "matrix_brightness"), "127\n")

assert blackout.wedged(node, gap=0) is False          # plain file: writes stick
with open(bright) as f:
    assert f.read().strip() == "127", "original brightness not restored"

real_write = blackout._write
writes = []


def _ignore_write(path, value):
    """The observed blackout: the write is accepted, the firmware then reports
    0 instead of what was written."""
    writes.append(int(value))
    real_write(path, 0)


blackout._write = _ignore_write
try:
    assert blackout.wedged(node, gap=0) is True
    assert writes.count(255) == blackout.PROBES, writes  # confirmed, not guessed
    # Already at full brightness: probe with a different value, or an echoing
    # firmware would look healthy.
    real_write(bright, 255)
    writes.clear()
    assert blackout.wedged(node, gap=0) is True
    assert set(writes[:blackout.PROBES]) == {254}, writes
finally:
    blackout._write = real_write
print("3. wedged() trusts a read-back write, 3x before saying otherwise: OK")

# --- 4. the read-after-write artifact must not read as a blackout ------------
real_read = blackout._read_int
state = {"value": 127, "fresh": False}


def _fake_write(path, value):
    state["value"] = int(value)
    state["fresh"] = True


def _fake_read(path):
    if state["fresh"]:
        state["fresh"] = False
        return 0  # measured on healthy hardware: the first read answers 0
    return state["value"]


blackout._write, blackout._read_int = _fake_write, _fake_read
try:
    assert blackout.wedged("ignored", gap=0) is False
    assert state["value"] == 127, state  # and the original is put back
finally:
    blackout._write, blackout._read_int = real_write, real_read
print("4. a 0 read straight after a write is not a blackout: OK")

# --- 5. wedged() propagates sysfs errors instead of guessing ------------------
try:
    blackout.wedged(os.path.join(TMP, "nope"), gap=0)
    raise AssertionError("missing attribute accepted")
except OSError:
    pass
print("5. wedged() raises OSError when it cannot read the firmware: OK")

# --- 6. power_cycle: disable 1 -> 0, and a denied write raises ----------------
port = os.path.join(usb, "3-4.1.3:1.0", "3-4.1.3-port2")
blackout.power_cycle(port, off_seconds=0.01)
with open(os.path.join(port, "disable")) as f:
    assert f.read().strip() == "0", "port left disabled"

ro = os.path.join(usb, "ro-port")
ro_attr = _touch(os.path.join(ro, "disable"), "0\n")
os.chmod(ro_attr, 0o444)
try:
    blackout.power_cycle(ro, off_seconds=0.01)
    raise AssertionError("read-only disable accepted")
except OSError:
    pass
print("6. power_cycle writes 1 then 0, raises when denied: OK")


# --- 7. daemon: tests on attach only, cycles once, doesn't draw that tick -----
class _Handle(MockDevice):
    def __init__(self, name, rows, cols, node=None):
        super().__init__(name, rows, cols)
        self.node = node
        self.draws = 0

    def kbd_sysfs_node(self):
        return self.node

    def draw(self, frame, background=(0, 0, 0)):
        self.draws += 1


class _Backend:
    def __init__(self, node):
        self.node = node
        self.handles = []

    def find_device(self, match):
        h = (_Handle("pad", 1, 19) if "firefly" in match.lower()
             else _Handle("kb", 8, 23, self.node))
        self.handles.append(h)
        return h


class _Calls:
    def __init__(self, wedged):
        self.wedged = wedged
        self.tested = 0
        self.cycled = 0

    def __enter__(self):
        self._w, self._p, self._s = (blackout.wedged, blackout.power_cycle,
                                     blackout.SETTLE_SECONDS)
        blackout.wedged = self._test
        blackout.power_cycle = self._cycle
        blackout.SETTLE_SECONDS = 0.0
        return self

    def __exit__(self, *_):
        blackout.wedged, blackout.power_cycle = self._w, self._p
        blackout.SETTLE_SECONDS = self._s

    def _test(self, node, **kw):
        self.tested += 1
        return self.wedged

    def _cycle(self, port, off_seconds=None):
        self.cycled += 1


CFG = {"prometheus": {"url": "http://127.0.0.1:1", "timeout": "100ms"},
       "idle_interval": "50ms", "background": "#101010",
       "devices": {"keyboard": "BlackWidow V4", "mousepad": "Firefly V2"},
       "bindings": [BINDING, {**BINDING, "name": "y", "device": "mousepad"}]}
cfg_path = os.path.join(TMP, "config.yaml")
with open(cfg_path, "w", encoding="utf-8") as f:
    yaml.safe_dump(CFG, f)
w = ConfigWatcher(cfg_path)
cfg = w.load()

with _Calls(wedged=True) as calls:
    backend = _Backend(link)
    d = Daemon(w, backend, once=True)
    handles = d._find_devices(cfg)
    assert calls.tested == 1, calls.tested       # the mousepad has no node
    assert calls.cycled == 1
    assert "keyboard" not in handles, "drew on a handle that is being reset"
    assert "mousepad" in handles, "the mousepad shouldn't be dropped"
    # Still wedged on the way back: one more cycle, then hands over to the user.
    handles = d._find_devices(cfg)
    assert (calls.tested, calls.cycled) == (2, 2), (calls.tested, calls.cycled)
    handles = d._find_devices(cfg)
    assert calls.cycled == 2, "kept power-cycling past MAX_ATTEMPTS"
print("7. daemon power-cycles a wedged keyboard, at most MAX_ATTEMPTS: OK")

# --- 8. a healthy keyboard is tested once per attach, never cycled ------------
with _Calls(wedged=False) as calls:
    backend = _Backend(link)
    d = Daemon(w, backend, once=True)
    handles = d._find_devices(cfg)
    assert (calls.tested, calls.cycled) == (1, 0)
    assert set(handles) == {"keyboard", "mousepad"}
    d._find_devices(cfg)  # still attached: not an attach, so not retested
    assert calls.tested == 1, "tested the firmware on a tick with no attach"
print("8. healthy keyboard: tested on attach only, never cycled: OK")

# --- 9. recover_blackout: false opts out entirely -----------------------------
off = {**CFG, "devices": {"keyboard": {"match": "BlackWidow V4",
                                       "recover_blackout": False}},
       "bindings": [BINDING]}
off_path = os.path.join(TMP, "off.yaml")
with open(off_path, "w", encoding="utf-8") as f:
    yaml.safe_dump(off, f)
w_off = ConfigWatcher(off_path)
cfg_off = w_off.load()
assert cfg_off.devices["keyboard"].recover_blackout is False
assert cfg.devices["keyboard"].recover_blackout is True  # default: on
with _Calls(wedged=True) as calls:
    Daemon(w_off, _Backend(link), once=True)._find_devices(cfg_off)
    assert (calls.tested, calls.cycled) == (0, 0)
try:
    parse_config({**off, "devices": {"keyboard": {"match": "x",
                                                 "recover_blackout": "yes"}}})
    raise AssertionError("accepted a non-boolean recover_blackout")
except ConfigError:
    pass
print("9. recover_blackout: false opts out; non-bool is a ConfigError: OK")

# --- 10. an unreadable firmware or an unknown port never cycles blindly --------
with _Calls(wedged=True) as calls:
    def _raise(node, **kw):
        raise OSError(13, "Permission denied")

    blackout.wedged = _raise
    Daemon(w, _Backend(link), once=True)._find_devices(cfg)
    assert calls.cycled == 0, "power-cycled without a confirmed diagnosis"

with _Calls(wedged=True) as calls:
    port_of = blackout.usb_port
    blackout.usb_port = lambda node: None  # hub without per-port power
    try:
        Daemon(w, _Backend(link), once=True)._find_devices(cfg)
    finally:
        blackout.usb_port = port_of
    assert (calls.tested, calls.cycled) == (1, 0)
print("10. no port or no diagnosis -> no power cycle: OK")

print("\nall blackout tests passed")
