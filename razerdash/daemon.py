"""The main render loop."""
from __future__ import annotations

import logging
import os
import signal
import time

from .config import ConfigWatcher
from .context import metric_context
from .keygroup import KeyMap, KeyMapError
from .prometheus import Prometheus
from .render import build_frame
from .sleep import SleepController

log = logging.getLogger("razerdash")

DEFAULT_KEYMAP = os.path.expanduser("~/.config/razerdash/keymap.yaml")
ERROR_COLOR = (255, 0, 0)
# `razerdash calibrate` drops this marker (holding its PID) to make the daemon
# stop drawing so the two don't fight over the keyboard.
PAUSE_FILE = os.path.expanduser("~/.config/razerdash/.calibrating")


class Daemon:
    def __init__(self, watcher: ConfigWatcher, backend, once=False):
        self.watcher = watcher
        self.backend = backend
        self.once = once
        self._stop = False
        self._paused_state = False
        self._sleepctl = None
        self._night_reason = None
        self._sessions = {}  # binding name -> SessionTracker, kept across polls
        self._attached = set()  # config device names seen last tick, for logging
        self._mode_warned = set()  # devices already warned about driver mode

    def stop(self, *_):
        self._stop = True

    def _keymap(self, path):
        if path:
            try:
                return KeyMap.load(path)
            except (OSError, KeyMapError) as e:
                log.warning("keymap load failed (%s); using full rows/columns", e)
        return None

    def _find_devices(self, config):
        """Probe every configured device; returns name -> handle for those
        attached, logging the set changes (KVM switches, hot-plug)."""
        handles = {}
        for name, dcfg in config.devices.items():
            h = self.backend.find_device(dcfg.match)
            if h is not None:
                handles[name] = h
        attached = set(handles)
        for name in sorted(attached - self._attached):
            h = handles[name]
            log.info("device %r attached: %s (%dx%d)", name, h.name, h.rows, h.cols)
        for name in sorted(self._attached - attached):
            log.info("device %r detached", name)
            self._mode_warned.discard(name)
        self._attached = attached
        for name, h in handles.items():
            self._ensure_driver_mode(name, h)
        return handles

    def _ensure_driver_mode(self, name, handle):
        """Blank-keyboard-after-KVM-switch guard: openrazer's move into driver
        mode sometimes doesn't stick on re-attach, and in device mode the
        keyboard ignores custom frames while reporting success. Checked every
        probe (not just on attach transitions) because openrazer can also
        re-init a device without us ever seeing it detach."""
        try:
            got = handle.ensure_driver_mode()
        except OSError as e:
            if name not in self._mode_warned:
                self._mode_warned.add(name)
                log.warning("cannot check driver mode on %r (%s); a KVM "
                            "switch may leave its LEDs dark", name, e)
            return
        if got == "fixed":
            log.info("device %r was in device mode (draws silently ignored -- "
                     "the KVM-switch blackout); re-asserted driver mode", name)
        elif got == "stuck" and name not in self._mode_warned:
            self._mode_warned.add(name)
            log.warning("device %r is stuck in device mode; it needs its "
                        "physical reset (unplug, hold Ctrl+CapsLock+Space, "
                        "plug back in while holding)", name)

    def run(self):
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)

        config = self.watcher.load()
        prom = Prometheus(config.prometheus.url, config.prometheus.timeout)
        ctx = metric_context()
        log.info("started on host %r; polling %s every %.1fs", ctx["host"],
                 config.prometheus.url, config.refresh_interval)

        while not self._stop:
            config = self.watcher.maybe_reload(log)
            prom.base = config.prometheus.url.rstrip("/")
            prom.timeout = config.prometheus.timeout

            handles = self._find_devices(config)
            if not handles:
                # Nothing attached to this machine (KVM elsewhere).
                log.debug("no configured device attached; idling")
                self._sleep(config.idle_interval)
                if self.once:
                    break
                continue

            paused = self._paused()
            if paused != self._paused_state:
                self._paused_state = paused
                log.info("calibration in progress; pausing rendering" if paused
                         else "calibration finished; resuming rendering")
            if paused:
                self._sleep(config.idle_interval)
                if self.once:
                    break
                continue

            night = self._night(config)
            if night != self._night_reason:
                if night:
                    log.info("going dark (%s)", night)
                else:
                    log.info("waking (%s over); resuming rendering",
                             self._night_reason)
                self._night_reason = night
            if night:
                for device in handles.values():
                    try:
                        device.clear()  # redone each tick: self-heals a KVM switch-in
                    except Exception:
                        pass  # device went away; re-probed next cycle
                # Re-check faster than idle_interval: waking should feel
                # instant when a keypress turns the monitors back on.
                self._sleep(min(config.idle_interval, 2.0))
                if self.once:
                    break
                continue

            frames, summary = {}, []
            for name, device in handles.items():
                keymap = self._keymap(config.devices[name].keymap)
                frames[name], s = build_frame(config, device.rows, device.cols,
                                              prom, keymap, log, ctx,
                                              sessions=self._sessions,
                                              device=name)
                summary += s
            # One background colour per tick, shared by every draw: cells no
            # binding painted (unused keys, unprogrammed mousepad zones) show
            # the same colour on every device, so animated backgrounds stay
            # in lockstep across the keyboard and the mousepad.
            bgc = config.background.color_at(time.monotonic())
            for name in list(handles):
                try:
                    handles[name].draw(frames[name], bgc)
                except Exception as e:
                    # Device vanished between find_device() and here (KVM switch).
                    log.warning("draw failed on %r (%s); re-probing next cycle",
                                name, e)
                    del handles[name]
            if handles:
                names = "+".join(h.name for h in handles.values())
                if self.watcher.error:
                    log.warning("drew %s: %s  [CONFIG ERROR: %s]", names,
                                self._fmt(summary), self.watcher.error)
                else:
                    log.info("drew %s: %s", names, self._fmt(summary))
            if self.once:
                break
            if not handles:
                self._sleep(config.idle_interval)
                continue
            if self.watcher.error:
                # A reload failed and we're on the last-good config; flash one key
                # red so the broken edit is visible without watching the journal.
                self._flash_error(handles, frames, config, config.refresh_interval)
            else:
                self._animate(handles, frames, config, config.refresh_interval)

        # Only hand the keyboard back when actually stopped (SIGINT/SIGTERM);
        # a `--once` render is meant to persist for inspection.
        if self._stop:
            self._cleanup()

    @staticmethod
    def _fmt(summary):
        parts = []
        for name, value, status in summary:
            parts.append(f"{name}={value:.1f}({status})" if value is not None
                         else f"{name}=--({status})")
        return ", ".join(parts) if parts else "(nothing)"

    def _paused(self):
        """True while `razerdash calibrate` holds the keyboard. The marker carries
        the calibrator's PID; a stale marker from a crashed calibrate (PID no
        longer alive) is cleared so rendering can't get stuck off."""
        try:
            with open(PAUSE_FILE) as f:
                pid = int(f.read().strip() or "0")
        except (OSError, ValueError):
            return False
        if pid > 0:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                try:
                    os.remove(PAUSE_FILE)
                except OSError:
                    pass
                return False
            except PermissionError:
                pass  # exists and owned by someone else; treat as alive
        return True

    def _night(self, config):
        """Reason string while the LEDs should be dark (monitors off / input
        idle), else None. The controller holds open sockets/fds, so it is
        rebuilt only when the sleep config actually changes."""
        if self._sleepctl is None or self._sleepctl.cfg != config.sleep:
            if self._sleepctl is not None:
                self._sleepctl.close()
            self._sleepctl = SleepController(config.sleep)
        return self._sleepctl.status()

    def _error_cell(self, config):
        """Resolve config.error_key to a (row, col). A coordinate is used as-is;
        a name (default 'mute') is looked up in the keymap -- config.keymap if
        set, else the default keymap file, so it works even when the (broken)
        config never referenced one."""
        ek = config.error_key
        if isinstance(ek, tuple):
            return ek
        try:
            km = KeyMap.load(config.keymap or DEFAULT_KEYMAP)
            return km.resolve_name(str(ek))
        except (OSError, KeyError, KeyMapError):
            log.warning("error_key %r not resolvable (needs a keymap); "
                        "config error is logged but not flashed", ek)
            return None

    def _animate(self, handles, frames, config, duration):
        """Hold the current metric frames for `duration`. A static background
        just sleeps until the next Prometheus poll; an animated one keeps
        redrawing the same frames with the effect's colour behind them, at
        `background.fps`. Hue is a function of wall time, so the cycle stays
        smooth across polls regardless of frame rate, and every device gets
        the same colour each tick (keyboard and mousepad cycle together)."""
        bg = config.background
        if not bg.animated:
            self._sleep(duration)
            return
        tick = 1.0 / bg.fps
        end = time.monotonic() + duration
        while not self._stop and handles:
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            self._sleep(min(tick, remaining))
            if self._stop:
                break
            if self._paused() or self._night(config):
                # calibrate/webedit took the keyboard, or the monitors went
                # off mid-window; stop drawing NOW, not at the next poll.
                break
            bgc = bg.color_at(time.monotonic())
            for name in list(handles):
                try:
                    handles[name].draw(frames[name], bgc)
                except Exception:
                    del handles[name]  # went away (KVM); re-probed next cycle

    def _flash_error(self, handles, frames, config, duration):
        """Blink the error key on the primary device; other devices keep
        their frames redrawn so an animated background doesn't freeze."""
        cell = self._error_cell(config) if config.primary in handles else None
        if cell is None:
            self._animate(handles, frames, config, duration)
            return
        end = time.monotonic() + duration
        on = True
        while not self._stop and handles and time.monotonic() < end:
            if self._paused() or self._night(config):
                break  # hand the keyboard over / go dark without delay
            bgc = config.background.color_at(time.monotonic())
            for name in list(handles):
                frame = frames[name]
                if name == config.primary:
                    frame = dict(frame)
                    frame[cell] = ERROR_COLOR if on else bgc
                try:
                    handles[name].draw(frame, bgc)
                except Exception:
                    del handles[name]  # went away (KVM); re-probed next cycle
            on = not on
            self._sleep(0.4)

    def _cleanup(self):
        try:
            cfg = self.watcher.config
            if cfg is None:
                return
            for dcfg in cfg.devices.values():
                device = self.backend.find_device(dcfg.match)
                if device is not None:
                    device.clear()
                    device.restore()
        except Exception:
            pass

    def _sleep(self, dur: float):
        end = time.monotonic() + dur
        while not self._stop:
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.25, remaining))
