"""Command-line entry point."""
from __future__ import annotations

import argparse
import logging
import os
import sys

from . import calibrate as calibrate_mod
from .backend import get_backend
from .config import ConfigError, ConfigWatcher, load_config
from .context import metric_context
from .daemon import Daemon
from .explain import describe_config
from .keygroup import KeyMap, KeyMapError, resolve
from .lighting import lit_count, normalize
from .prometheus import Prometheus
from .render import binding_value

DEFAULT_CONFIG = os.path.expanduser("~/.config/razerdash/config.yaml")
DEFAULT_KEYMAP = os.path.expanduser("~/.config/razerdash/keymap.yaml")


def _setup_logging(verbose: bool):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_run(args) -> int:
    watcher = ConfigWatcher(args.config)
    try:
        watcher.load()
    except (ConfigError, OSError) as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    backend = get_backend(args.backend, logging.getLogger("razerdash"))
    Daemon(watcher, backend, once=args.once).run()
    return 0


def _device_views(cfg, backend, fallback_dims=None):
    """Per-device (rows, cols) and loaded keymap: probed from the attached
    hardware when possible, else the mock backend's known profile for that
    match (e.g. Firefly = 1x19), else `fallback_dims`."""
    from .backend import MockBackend
    dims, keymaps = {}, {}
    for name, d in cfg.devices.items():
        dev = backend.find_device(d.match) if backend else None
        if dev is None and fallback_dims is None:
            dev = MockBackend().find_device(d.match)
        dims[name] = (dev.rows, dev.cols) if dev else fallback_dims
        keymaps[name] = None
        if d.keymap:
            try:
                keymaps[name] = KeyMap.load(d.keymap)
            except (OSError, KeyMapError) as e:
                print(f"(keymap {d.keymap} not loaded: {e})", file=sys.stderr)
    return dims, keymaps


def cmd_query(args) -> int:
    try:
        cfg = load_config(args.config)
    except (ConfigError, OSError) as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    prom = Prometheus(cfg.prometheus.url, cfg.prometheus.timeout)
    backend = get_backend(args.backend)
    # Explicit --rows/--cols override the probe; the 8x23 defaults mean "ask
    # the hardware, or the mock profile for that device when detached".
    flags = (args.rows, args.cols)
    dims, keymaps = _device_views(cfg, backend,
                                  None if flags == (8, 23) else flags)
    ctx = metric_context()
    multi = len(cfg.devices) > 1
    sessions = {}  # fresh trackers; bootstrap replay finds the live anchor
    for b in cfg.bindings:
        value, status = binding_value(prom, b, ctx, sessions)
        if value is None:
            print(f"{b.name}: {status}")
            continue
        rows, cols = dims[b.device]
        try:
            keys = resolve(b.key_group, rows, cols, keymaps[b.device])
        except (KeyError, ValueError, IndexError, TypeError) as e:
            print(f"{b.name}: key group unresolved ({e})")
            continue
        n = len(keys)
        grp = f"{b.key_group.type}:{b.key_group.index}"
        if multi:
            grp += f"@{b.device}"
        note = " (no active session)" if status == "no-session" else ""
        print(f"{b.name}: value={value:.2f} norm={normalize(value, b.range) * 100:.0f}% "
              f"group={grp} keys={n} lit={lit_count(b, value, n)} "
              f"method={b.lighting.method}{note}")
    return 0


def cmd_explain(args) -> int:
    try:
        cfg = load_config(args.config)
    except (ConfigError, OSError) as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    backend = get_backend(args.backend)
    dims, keymaps = _device_views(cfg, backend)
    values = None
    if args.values:
        prom = Prometheus(cfg.prometheus.url, cfg.prometheus.timeout)
        ctx = metric_context()
        values = {}
        sessions = {}
        for b in cfg.bindings:
            value, status = binding_value(prom, b, ctx, sessions)
            values[b.name] = value if status in ("ok", "no-session") else \
                (None if status == "empty" else f"ERROR: {status}")
    print(describe_config(cfg, keymaps=keymaps, dims=dims, values=values), end="")
    return 0


def cmd_list_devices(args) -> int:
    backend = get_backend(args.backend)
    if args.match:
        dev = backend.find_device(args.match)
        if dev:
            print(f"matched: {dev.name}  matrix={dev.rows}x{dev.cols}")
            return 0
        print(f"no device matching {args.match!r}")
        return 1
    # No --match: report every device the config names.
    try:
        cfg = load_config(args.config)
        matches = {name: d.match for name, d in cfg.devices.items()}
    except (ConfigError, OSError):
        matches = {"keyboard": "BlackWidow V4"}
    rc = 0
    for name, m in matches.items():
        dev = backend.find_device(m)
        if dev:
            print(f"{name}: matched {dev.name}  matrix={dev.rows}x{dev.cols}")
        else:
            print(f"{name}: no device matching {m!r}")
            rc = 1
    return rc


def cmd_fix_blackout(args) -> int:
    """Clear the KVM blackout by hand: cut the keyboard's USB port power, which
    is the only thing that revives a wedged LED engine. The daemon does this on
    attach when it can tell the keyboard is wedged (see blackout.py); this is
    for when it can't -- the firmware test is unproven, so it also prints what
    that test said, which is the data needed to confirm or drop it."""
    from . import blackout
    backend = get_backend(args.backend)
    dev = backend.find_device(args.match)
    if dev is None:
        print(f"no device matching {args.match!r}")
        return 1
    node = dev.kbd_sysfs_node()
    if node is None:
        print(f"{dev.name}: no razerkbd sysfs node (not a keyboard?), "
              "so no USB port to cycle")
        return 1
    try:
        verdict = ("REJECTED -- the engine looks wedged" if blackout.wedged(node)
                   else "accepted -- the firmware looks healthy")
    except OSError as e:
        verdict = f"unavailable ({e})"
    print(f"{dev.name}: firmware write test: {verdict}")
    port = blackout.usb_port(node)
    if port is None:
        print("  its hub exposes no per-port power switch; only a physical "
              "replug will clear a blackout")
        return 1
    if args.test_only:
        print(f"  port {port} (not touched: --test-only)")
        return 0
    print(f"  cutting power at {port} for {blackout.OFF_SECONDS:.0f}s")
    try:
        blackout.power_cycle(port)
    except OSError as e:
        print(f"cannot write {port}/disable ({e}); run as root, or install the "
              "udev rule: sudo bash contrib/kvm-blackout/install.sh",
              file=sys.stderr)
        return 2
    print("  power restored; the keyboard re-enumerates in a few seconds and "
          "the daemon picks it up on its next probe")
    return 0


def cmd_calibrate(args) -> int:
    return calibrate_mod.run(args)


def cmd_webedit(args) -> int:
    from . import webedit
    return webedit.run(args)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="razerdash",
        description="Drive Razer Blackwidow V4 LEDs from Prometheus metrics.",
    )
    p.add_argument("--config", default=DEFAULT_CONFIG)
    p.add_argument("--backend", choices=["auto", "openrazer", "mock"], default="auto")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_run, once=False)

    sub = p.add_subparsers(dest="cmd")

    pr = sub.add_parser("run", help="run the render daemon (default)")
    pr.add_argument("--once", action="store_true",
                    help="render a single frame and exit")
    pr.set_defaults(func=cmd_run)

    pq = sub.add_parser("query", help="query every binding once and print values")
    pq.add_argument("--rows", type=int, default=8)   # BlackWidow V4 matrix
    pq.add_argument("--cols", type=int, default=23)
    pq.set_defaults(func=cmd_query)

    px = sub.add_parser("explain", help="describe the current config in plain English")
    px.add_argument("--values", action="store_true",
                    help="also query Prometheus and show each binding's live reading")
    px.set_defaults(func=cmd_explain)

    pl = sub.add_parser("list-devices",
                        help="show the configured OpenRazer devices")
    pl.add_argument("--match", default=None,
                    help="probe one name substring instead of the config's devices")
    pl.set_defaults(func=cmd_list_devices)

    pf = sub.add_parser("fix-blackout",
                        help="power-cycle a dark keyboard's USB port "
                             "(the KVM blackout, see contrib/kvm-blackout/)")
    pf.add_argument("--match", default="BlackWidow V4")
    pf.add_argument("--test-only", action="store_true",
                    help="report the firmware write test without cutting power")
    pf.set_defaults(func=cmd_fix_blackout)

    pc = sub.add_parser("calibrate", help="interactively build a keymap")
    pc.add_argument("--match", default="BlackWidow V4")
    pc.add_argument("--output", default=DEFAULT_KEYMAP)
    pc.set_defaults(func=cmd_calibrate)

    pw = sub.add_parser("webedit", help="edit the keymap graphically in a browser")
    pw.add_argument("--match", default="BlackWidow V4")
    pw.add_argument("--output", default=DEFAULT_KEYMAP)
    pw.add_argument("--port", type=int, default=8380)
    pw.set_defaults(func=cmd_webedit)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)
