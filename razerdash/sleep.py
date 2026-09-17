"""Night mode: decide when the keyboard LEDs should go dark.

Two independent triggers, combined by SleepController (either one darkens the
board):

- follow_sway: dark while every sway output is powered off. This inherits
  whatever idle policy already drives the monitors (swayidle timeouts, manual
  `output * power off`, ...), so there is no second timeout to keep in sync,
  and the LEDs come back the moment sway wakes the displays.
- idle_timeout: dark after N seconds without keyboard/mouse input, read from
  /dev/input/event*. The fallback for machines without sway; needs read access
  to the event devices (the 'input' group on Debian).
"""
from __future__ import annotations

import glob
import json
import logging
import os
import select
import socket
import struct
import time
from typing import Optional

log = logging.getLogger("razerdash")

_IPC_MAGIC = b"i3-ipc"
_GET_OUTPUTS = 3


def _fmt_duration(seconds: float) -> str:
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{int(seconds // 3600)}h"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{int(seconds // 60)}m"
    return f"{seconds:g}s"


class SwayWatcher:
    """Answers 'is any monitor powered on?' over sway's IPC socket.

    The daemon runs under systemd --user with no SWAYSOCK, so the socket is
    discovered by globbing /run/user/<uid>/sway-ipc.*.sock (sway's default
    location; the name embeds sway's PID, so it changes when sway restarts --
    a failed socket is dropped and re-discovered on the next call).
    """

    def __init__(self):
        self._path: Optional[str] = None
        self._reported = None  # last logged reachability, to log transitions once

    def _candidates(self):
        env = os.environ.get("SWAYSOCK")
        found = [env] if env else []
        found += sorted(glob.glob(f"/run/user/{os.getuid()}/sway-ipc.*.sock"))
        seen = set()
        return [p for p in found if not (p in seen or seen.add(p))]

    @staticmethod
    def _recv_exact(sock, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise OSError("sway IPC connection closed mid-message")
            buf += chunk
        return buf

    def _get_outputs(self, path: str):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            s.connect(path)
            s.sendall(_IPC_MAGIC + struct.pack("<II", 0, _GET_OUTPUTS))
            header = self._recv_exact(s, 14)
            if header[:6] != _IPC_MAGIC:
                raise OSError("bad sway IPC magic in reply")
            length, _ = struct.unpack("<II", header[6:])
            return json.loads(self._recv_exact(s, length))

    def outputs_on(self) -> Optional[bool]:
        """True if any output is powered, False if all are off, None if sway
        is unreachable (not running, or not sway at all)."""
        paths = [self._path] if self._path else self._candidates()
        for path in paths:
            try:
                outputs = self._get_outputs(path)
            except (OSError, ValueError):
                self._path = None
                continue
            if not isinstance(outputs, list) or not outputs:
                continue
            self._path = path
            if self._reported is not True:
                self._reported = True
                log.info("follow_sway: watching monitor power via %s", path)
            # sway >= 1.8 reports "power"; older only "dpms". Missing both
            # (i3?) counts as on -- never darken on unknown data.
            return any(o.get("power", o.get("dpms", True)) for o in outputs)
        if self._reported is not False:
            self._reported = False
            log.info("follow_sway: sway IPC not reachable; monitor sync inactive")
        return None


class InputIdleWatcher:
    """Tracks seconds since the last keyboard/mouse event via /dev/input.

    Every event device is opened non-blocking and drained on each poll; any
    bytes read mean activity. Devices come and go (USB replug, the KVM), so
    the directory is re-scanned every RESCAN seconds and dead fds dropped.
    Reading an evdev node does not consume events from other readers.
    """

    RESCAN = 30.0

    def __init__(self):
        self._fds: dict[str, int] = {}
        self._last = time.monotonic()
        self._next_scan = 0.0
        self._warned = False

    def _scan(self):
        denied = 0
        for path in sorted(glob.glob("/dev/input/event*")):
            if path in self._fds:
                continue
            try:
                self._fds[path] = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except PermissionError:
                denied += 1
            except OSError:
                pass
        if not self._fds and denied and not self._warned:
            self._warned = True
            log.warning(
                "sleep.idle_timeout is set but /dev/input/event* is not readable; "
                "add your user to the 'input' group (sudo usermod -aG input $USER, "
                "then re-login). Idle detection disabled until then.")

    def _drop(self, path: str):
        fd = self._fds.pop(path, None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def seconds_idle(self) -> Optional[float]:
        """Seconds since the last input event, or None while no event device
        is readable (no permission / none present)."""
        now = time.monotonic()
        if now >= self._next_scan:
            self._next_scan = now + self.RESCAN
            self._scan()
        if not self._fds:
            return None
        try:
            readable, _, _ = select.select(list(self._fds.values()), [], [], 0)
        except OSError:
            readable = list(self._fds.values())  # let per-fd reads sort it out
        activity = False
        for path, fd in list(self._fds.items()):
            if fd not in readable:
                continue
            try:
                while True:
                    data = os.read(fd, 4096)
                    if not data:  # EOF: device gone
                        self._drop(path)
                        break
                    activity = True
            except BlockingIOError:
                pass
            except OSError:  # ENODEV after unplug/KVM switch
                self._drop(path)
        if activity:
            self._last = now
        return now - self._last

    def close(self):
        for path in list(self._fds):
            self._drop(path)


class SleepController:
    """Combines the configured triggers into one 'why are we dark?' answer.

    status() returns None while the board should render, or a short human
    reason string while it should be dark. Results are cached for CACHE
    seconds so the animation loop can call this every frame cheaply.
    """

    CACHE = 1.0

    def __init__(self, cfg):
        self.cfg = cfg
        self._sway = SwayWatcher() if cfg.follow_sway else None
        self._idle = InputIdleWatcher() if cfg.idle_timeout > 0 else None
        self._cached: Optional[str] = None
        self._next_check = 0.0

    def status(self) -> Optional[str]:
        now = time.monotonic()
        if now < self._next_check:
            return self._cached
        self._next_check = now + self.CACHE
        reason = None
        if self._sway is not None and self._sway.outputs_on() is False:
            reason = "all monitors off"
        if reason is None and self._idle is not None:
            idle = self._idle.seconds_idle()
            if idle is not None and idle >= self.cfg.idle_timeout:
                reason = f"no input for {_fmt_duration(self.cfg.idle_timeout)}"
        self._cached = reason
        return reason

    def close(self):
        if self._idle is not None:
            self._idle.close()
