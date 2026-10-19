"""KVM-blackout recovery: USB port lookup, power cycle, `fix-blackout`, and
the daemon leaving the port alone (contrib/kvm-blackout/ has the hardware
data)."""
import argparse
import contextlib
import io
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import yaml
from razerdash import blackout, cli
from razerdash.backend import MockDevice
from razerdash.config import ConfigWatcher
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

# --- 3. power_cycle: disable 1 -> 0, and a denied write raises ----------------
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
print("3. power_cycle writes 1 then 0, raises when denied: OK")


# --- 4. the daemon never power-cycles on its own ------------------------------
class _Handle(MockDevice):
    def __init__(self, name, rows, cols, node=None):
        super().__init__(name, rows, cols)
        self.node = node

    def kbd_sysfs_node(self):
        return self.node


class _Backend:
    def __init__(self, node):
        self.node = node

    def find_device(self, match):
        return (_Handle("pad", 1, 19) if "firefly" in match.lower()
                else _Handle("kb", 8, 23, self.node))


cycled = []
real_cycle = blackout.power_cycle
blackout.power_cycle = lambda p, off_seconds=None: cycled.append(p)

CFG = {"prometheus": {"url": "http://127.0.0.1:1", "timeout": "100ms"},
       "idle_interval": "50ms", "background": "#101010",
       "devices": {"keyboard": "BlackWidow V4", "mousepad": "Firefly V2"},
       "bindings": [BINDING, {**BINDING, "name": "y", "device": "mousepad"}]}
cfg_path = os.path.join(TMP, "config.yaml")
with open(cfg_path, "w", encoding="utf-8") as f:
    yaml.safe_dump(CFG, f)
w = ConfigWatcher(cfg_path)
cfg = w.load()

try:
    d = Daemon(w, _Backend(link), once=True)
    handles = d._find_devices(cfg)
    assert set(handles) == {"keyboard", "mousepad"}, handles
    assert cycled == [], "the daemon power-cycled a port by itself"
finally:
    blackout.power_cycle = real_cycle
print("4. daemon attaches a keyboard without touching its port: OK")

# --- 5. a config from before the detector came out still loads ---------------
old = {**CFG, "devices": {"keyboard": {"match": "BlackWidow V4",
                                       "recover_blackout": False}},
       "bindings": [BINDING]}
old_path = os.path.join(TMP, "old.yaml")
with open(old_path, "w", encoding="utf-8") as f:
    yaml.safe_dump(old, f)
assert ConfigWatcher(old_path).load().devices["keyboard"].match == "BlackWidow V4"
print("5. a leftover recover_blackout key is ignored, not an error: OK")


# --- 6. fix-blackout --dry-run reports the port and touches nothing ----------
def _fix(dry_run):
    real_backend = cli.get_backend
    cli.get_backend = lambda kind: _Backend(link)
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            rc = cli.cmd_fix_blackout(argparse.Namespace(
                backend="mock", match="BlackWidow V4", dry_run=dry_run))
    finally:
        cli.get_backend = real_backend
    return rc, out.getvalue()


blackout.power_cycle = lambda p, off_seconds=None: cycled.append(p)
try:
    rc, out = _fix(dry_run=True)
    assert rc == 0 and port in out and "allowed" in out, (rc, out)
    assert cycled == [], "--dry-run cut the power"
    rc, out = _fix(dry_run=False)
    assert rc == 0 and cycled == [port], (rc, cycled, out)
finally:
    blackout.power_cycle = real_cycle
print("6. fix-blackout --dry-run reports the port; without it, cycles it: OK")

print("\nall blackout tests passed")
