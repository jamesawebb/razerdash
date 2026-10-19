"""Configuration model, parsing, and hot-reload."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

import jinja2
import yaml

from .color import parse_color, spectrum_color
from .context import metric_context


class ConfigError(Exception):
    pass


def parse_duration(v, default=None) -> Optional[float]:
    """Parse "5s", "500ms", "2m", "1h", or a bare number (seconds)."""
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().lower()
    for suf, mult in (("ms", 0.001), ("s", 1.0), ("m", 60.0), ("h", 3600.0)):
        if s.endswith(suf):
            return float(s[: -len(suf)]) * mult
    return float(s)


@dataclass
class PromConfig:
    url: str = "http://localhost:9090"
    timeout: float = 3.0


@dataclass
class DeviceConfig:
    match: str = "BlackWidow V4"
    keymap: Optional[str] = None  # per-device; None => full rows/columns


@dataclass
class Background:
    """Colour of every key not painted by a binding. `static` is a fixed
    colour; `spectrum` cycles those keys around the hue wheel. Effects are
    rendered in software -- Razer's hardware chroma effects are whole-board
    modes that cannot run behind the custom frame the bindings are drawn
    with -- so the daemon redraws at `fps` while one is active."""
    effect: str = "static"    # static | spectrum
    color: tuple = (0, 0, 0)  # the static colour
    fps: float = 10.0         # animation redraws per second
    period: float = 30.0      # seconds for one full hue cycle
    saturation: float = 1.0   # 0..1
    brightness: float = 0.4   # 0..1; dim by default so metric keys stay readable

    @property
    def animated(self) -> bool:
        return self.effect != "static"

    def color_at(self, t: float) -> tuple:
        if self.effect == "spectrum":
            return spectrum_color(t, self.period, self.saturation, self.brightness)
        return self.color


@dataclass
class Sleep:
    """When to turn the LEDs off entirely (night mode). Either trigger
    darkens the board; rendering resumes as soon as neither applies."""
    follow_sway: bool = True   # dark while every sway output is powered off
    idle_timeout: float = 0.0  # dark after N s without input; 0 = disabled


@dataclass
class Range:
    min: float = 0.0
    max: float = 100.0


@dataclass
class KeyGroup:
    type: str  # row | column | keys
    index: Optional[int] = None
    keys: Optional[list] = None
    order: str = "left_to_right"


@dataclass
class Session:
    """Anchored usage window (e.g. Claude's 5-hour session): the binding's
    metric is rendered with `${session}` = time since the window opened, and
    reads zero between windows. `activity` is a PromQL expression that is > 0
    while the account is in use (use a ~[10m] increase of the counter)."""
    activity: str
    length: float = 5 * 3600.0  # window duration
    align: float = 3600.0       # anchor truncation (Claude resets on the hour)


@dataclass
class Lighting:
    method: str  # fill_threshold | fill_fixed | solid_threshold | gradient
    off_color: Optional[tuple] = None  # None => inherit the global background
    color: Optional[tuple] = None
    thresholds: list = field(default_factory=list)  # [(at, colour), ...]
    stops: list = field(default_factory=list)        # [(at, colour), ...]


@dataclass
class Binding:
    name: str
    metric: str
    key_group: KeyGroup
    lighting: Lighting
    reduce: str = "first"
    range: Range = field(default_factory=Range)
    priority: int = 0
    label: Optional[str] = None  # human-readable name for `razerdash explain`
    session: Optional[Session] = None  # anchored usage window (${session})
    device: str = "keyboard"  # which entry in Config.devices this paints onto


@dataclass
class Config:
    prometheus: PromConfig
    devices: dict  # name -> DeviceConfig, in config order; first = primary
    bindings: list
    refresh_interval: float = 5.0
    idle_interval: float = 2.0
    reload: str = "on_change"
    background: Background = field(default_factory=Background)
    sleep: Sleep = field(default_factory=Sleep)
    key_groups: dict = field(default_factory=dict)  # name -> KeyGroup (reusable)
    error_key: object = "mute"  # flashed red on a config error: key name or [row, col]

    @property
    def primary(self) -> str:
        """The first configured device: where the error key flashes, and the
        default target for bindings that don't name a device."""
        return next(iter(self.devices))

    @property
    def device(self) -> DeviceConfig:
        return self.devices[self.primary]

    @property
    def keymap(self) -> Optional[str]:
        return self.device.keymap


def _stops(items):
    out = []
    for it in items or []:
        if "at" not in it or "color" not in it:
            raise ConfigError("each threshold/stop needs 'at' and 'color'")
        out.append((float(it["at"]), parse_color(it["color"])))
    out.sort(key=lambda x: x[0])
    return out


ORDERS = ("left_to_right", "right_to_left", "top_to_bottom", "bottom_to_top")
REDUCERS = ("first", "last", "max", "min", "sum", "avg")  # see prometheus.reduce_values


def _index(v, what="key_group.index") -> int:
    """A matrix row/column index: an int, or something unambiguously one
    (a quoted "3", a 3.0). A str or float that slipped through here used to
    surface as a TypeError inside the render loop and take the daemon down.
    bool is an int subclass but never a sane index."""
    if isinstance(v, bool):
        raise ConfigError(f"{what} must be an integer, got {v!r}")
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ConfigError(f"{what} must be an integer, got {v!r}") from None
    if not f.is_integer():
        raise ConfigError(f"{what} must be an integer, got {v!r}")
    return int(f)


def _keys(items) -> list:
    """Normalise a `keys:` list to key names (str) and (row, col) int tuples."""
    if not isinstance(items, (list, tuple)):
        raise ConfigError("key_group.keys must be a list")
    out = []
    for item in items:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            out.append((_index(item[0], "key_group.keys row"),
                        _index(item[1], "key_group.keys col")))
        else:
            raise ConfigError("key_group.keys entries must be a key name or "
                              f"[row, col], got {item!r}")
    return out


def _key_group(d) -> KeyGroup:
    if not isinstance(d, dict):
        raise ConfigError("key_group must be a mapping")
    t = d.get("type")
    if t not in ("row", "column", "keys"):
        raise ConfigError(f"key_group.type must be row|column|keys, got {t!r}")
    if t == "keys" and not d.get("keys"):
        raise ConfigError("key_group.type=keys requires 'keys'")
    if t in ("row", "column") and d.get("index") is None:
        raise ConfigError(f"key_group.type={t} requires 'index'")
    order = d.get("order", "left_to_right")
    if order not in ORDERS:
        raise ConfigError(f"key_group.order must be one of {ORDERS}, got {order!r}")
    return KeyGroup(type=t,
                    index=_index(d["index"]) if t != "keys" else None,
                    keys=_keys(d["keys"]) if t == "keys" else None,
                    order=order)


def _lighting(d) -> Lighting:
    if not isinstance(d, dict):
        raise ConfigError("lighting must be a mapping")
    m = d.get("method")
    valid = ("fill_threshold", "fill_fixed", "solid_threshold", "gradient")
    if m not in valid:
        raise ConfigError(f"lighting.method must be one of {valid}, got {m!r}")
    off = parse_color(d["off_color"]) if d.get("off_color") else None
    color = parse_color(d["color"]) if d.get("color") else None
    thresholds = _stops(d.get("thresholds"))
    stops = _stops(d.get("stops"))
    if m in ("fill_threshold", "solid_threshold") and not thresholds:
        raise ConfigError(f"lighting.method={m} requires 'thresholds'")
    if m == "gradient" and not stops:
        raise ConfigError("lighting.method=gradient requires 'stops'")
    if m == "fill_fixed" and color is None:
        raise ConfigError("lighting.method=fill_fixed requires 'color'")
    return Lighting(method=m, off_color=off, color=color,
                    thresholds=thresholds, stops=stops)


def _session(d) -> Optional[Session]:
    if d is None:
        return None
    if not isinstance(d, dict) or not d.get("activity"):
        raise ConfigError("session must be a mapping with an 'activity' "
                          "PromQL expression")
    length = parse_duration(d.get("length"), 5 * 3600.0)
    align = parse_duration(d.get("align"), 3600.0)
    if length <= 0:
        raise ConfigError("session.length must be positive")
    if align < 0:
        raise ConfigError("session.align must be >= 0")
    return Session(activity=str(d["activity"]), length=length, align=align)


def _binding(d, named_groups, devices, primary) -> Binding:
    for req in ("name", "metric", "key_group", "lighting"):
        if req not in d:
            raise ConfigError(f"binding missing required field {req!r}")
    kg_raw = d["key_group"]
    if isinstance(kg_raw, str):
        if kg_raw not in named_groups:
            raise ConfigError(
                f"binding {d['name']!r} references unknown key_group {kg_raw!r}")
        key_group = named_groups[kg_raw]
    else:
        key_group = _key_group(kg_raw)
    device = str(d.get("device", primary))
    if device not in devices:
        raise ConfigError(
            f"binding {d['name']!r} targets unknown device {device!r} "
            f"(configured: {', '.join(devices)})")
    reduce = str(d.get("reduce", "first"))
    if reduce not in REDUCERS:
        # reduce_values() silently falls back to "first" for anything unknown.
        raise ConfigError(f"binding {d['name']!r}: reduce must be one of "
                          f"{REDUCERS}, got {reduce!r}")
    rng = d.get("range") or {}
    return Binding(
        name=d["name"],
        metric=d["metric"],
        key_group=key_group,
        lighting=_lighting(d["lighting"]),
        reduce=reduce,
        range=Range(min=float(rng.get("min", 0.0)), max=float(rng.get("max", 100.0))),
        priority=int(d.get("priority", 0)),
        label=d.get("label"),
        session=_session(d.get("session")),
        device=device,
    )


def _check_duplicate_names(bindings):
    """Names key the session trackers and `explain --values`; two bindings
    sharing one would silently share state."""
    seen = set()
    for b in bindings:
        if b.name in seen:
            raise ConfigError(f"duplicate binding name {b.name!r}")
        seen.add(b.name)


def _check_duplicate_indexes(bindings):
    """Two bindings on the same row/column index of the same device almost
    always mean a typo."""
    seen = {}
    for b in bindings:
        kg = b.key_group
        if kg.type in ("row", "column") and kg.index is not None:
            k = (b.device, kg.type, kg.index)
            if k in seen:
                raise ConfigError(
                    f"duplicate {kg.type} index {kg.index} on device "
                    f"{b.device!r}: bindings {seen[k]!r} and {b.name!r} "
                    f"target the same keys")
            seen[k] = b.name


def _devices(raw) -> dict:
    """Named devices, in config order (the first is the primary).

        devices:
          keyboard: { match: "BlackWidow V4", keymap: keymap.yaml }
          mousepad: "Firefly V2"        # shorthand: just the match string

    The historical single-device form (`device: {match: ...}` plus a top-level
    `keymap:`) still works and is equivalent to a lone 'keyboard' entry.
    """
    devs = raw.get("devices")
    if devs is None:
        dev_d = raw.get("device") or {}
        km = raw.get("keymap")
        return {"keyboard": DeviceConfig(
            match=dev_d.get("match", "BlackWidow V4"),
            keymap=os.path.expanduser(km) if km else None)}
    if raw.get("device") or raw.get("keymap"):
        raise ConfigError("use either 'devices:' or the single-device "
                          "'device:'/'keymap:' form, not both")
    if not isinstance(devs, dict) or not devs:
        raise ConfigError("devices must be a non-empty mapping of name -> spec")
    out = {}
    for name, spec in devs.items():
        if isinstance(spec, str):
            out[str(name)] = DeviceConfig(match=spec)
        elif isinstance(spec, dict):
            if not spec.get("match"):
                raise ConfigError(f"device {name!r} needs a 'match'")
            km = spec.get("keymap")
            out[str(name)] = DeviceConfig(
                match=str(spec["match"]),
                keymap=os.path.expanduser(km) if km else None)
        else:
            raise ConfigError(
                f"device {name!r} must be a match string or a mapping")
    return out


def _background(v) -> Background:
    """`background:` is either a plain colour (static, the historical form) or
    a mapping selecting an effect."""
    if v is None:
        return Background()
    if not isinstance(v, dict):
        return Background(color=parse_color(v))
    effect = str(v.get("effect", "static")).lower()
    if effect not in ("static", "spectrum"):
        raise ConfigError(f"background.effect must be static|spectrum, got {effect!r}")
    fps = float(v.get("fps", 10))
    if not 0 < fps <= 60:
        raise ConfigError(f"background.fps must be in (0, 60], got {fps:g}")
    period = parse_duration(v.get("period"), 30.0)
    if period <= 0:
        raise ConfigError("background.period must be positive")
    saturation = float(v.get("saturation", 1.0))
    brightness = float(v.get("brightness", 0.4))
    for name, val in (("saturation", saturation), ("brightness", brightness)):
        if not 0.0 <= val <= 1.0:
            raise ConfigError(f"background.{name} must be between 0 and 1, got {val:g}")
    return Background(effect=effect, color=parse_color(v.get("color", "#000000")),
                      fps=fps, period=period, saturation=saturation,
                      brightness=brightness)


def _sleep(v) -> Sleep:
    if v is None:
        return Sleep()
    if not isinstance(v, dict):
        raise ConfigError("sleep must be a mapping "
                          "(follow_sway / idle_timeout)")
    timeout = parse_duration(v.get("idle_timeout"), 0.0)
    if timeout < 0:
        raise ConfigError(f"sleep.idle_timeout must be >= 0, got {timeout:g}")
    return Sleep(follow_sway=bool(v.get("follow_sway", True)),
                 idle_timeout=timeout)


def _error_key(v):
    if isinstance(v, (list, tuple)):
        if len(v) != 2:
            raise ConfigError("error_key coordinate must be [row, col]")
        return (int(v[0]), int(v[1]))
    return str(v)


def parse_config(raw) -> Config:
    try:
        return _parse_config(raw)
    except ConfigError:
        raise
    except (TypeError, ValueError, AttributeError) as e:
        # float()/int()/parse_color on a malformed value. Surface it as a
        # ConfigError so the hot-reload path keeps the last good config and
        # flashes the error key instead of crashing the daemon.
        raise ConfigError(str(e)) from e


def _parse_config(raw) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("top-level config must be a mapping")
    prom_d = raw.get("prometheus") or {}
    prom = PromConfig(
        url=prom_d.get("url", "http://localhost:9090"),
        timeout=parse_duration(prom_d.get("timeout"), 3.0),
    )
    devices = _devices(raw)
    primary = next(iter(devices))
    named_groups = {name: _key_group(spec)
                    for name, spec in (raw.get("key_groups") or {}).items()}
    bindings_raw = raw.get("bindings") or []
    if not bindings_raw:
        raise ConfigError("config has no bindings")
    bindings = [_binding(b, named_groups, devices, primary) for b in bindings_raw]
    _check_duplicate_names(bindings)
    _check_duplicate_indexes(bindings)
    return Config(
        prometheus=prom,
        devices=devices,
        bindings=bindings,
        refresh_interval=parse_duration(raw.get("refresh_interval"), 5.0),
        idle_interval=parse_duration(raw.get("idle_interval"), 2.0),
        reload=raw.get("reload", "on_change"),
        background=_background(raw.get("background")),
        sleep=_sleep(raw.get("sleep")),
        key_groups=named_groups,
        error_key=_error_key(raw.get("error_key", "mute")),
    )


def render_template(text: str, path: str = "<config>") -> str:
    """Run the config file through Jinja2 before YAML parsing.

    Context: `env` (the process environment -- under systemd that is what
    Environment=/EnvironmentFile= provide, not your shell), plus `host` and
    `fqdn`. StrictUndefined turns a template typo into a ConfigError (-> keep
    last good config, flash the error key) instead of silently rendering
    nothing. This happens at (re)load time; the query-time `${host}`
    substitution in metric strings is separate and unchanged.
    """
    jenv = jinja2.Environment(undefined=jinja2.StrictUndefined,
                              trim_blocks=True, lstrip_blocks=True,
                              keep_trailing_newline=True)
    try:
        return jenv.from_string(text).render(env=os.environ, **metric_context())
    except jinja2.TemplateError as e:
        raise ConfigError(f"template error in {path}: {e}") from e


def load_config(path: str) -> Config:
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    try:
        raw = yaml.safe_load(render_template(text, path))
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {path}: {e}") from e
    cfg = parse_config(raw)
    # A relative `keymap:` is resolved against the config file's own directory,
    # so a bare `keymap: keymap.yaml` finds the file sitting next to config.yaml.
    for dev in cfg.devices.values():
        if dev.keymap and not os.path.isabs(dev.keymap):
            dev.keymap = os.path.join(
                os.path.dirname(os.path.abspath(path)), dev.keymap)
    return cfg


class ConfigWatcher:
    """Loads config and reloads it when the file's mtime changes."""

    def __init__(self, path: str):
        self.path = path
        self._mtime = None
        self._config: Optional[Config] = None
        self.error: Optional[str] = None  # message of the last failed reload, if any

    def load(self) -> Config:
        self._config = load_config(self.path)
        self._mtime = os.path.getmtime(self.path)
        self.error = None
        return self._config

    @property
    def config(self) -> Optional[Config]:
        return self._config

    def maybe_reload(self, logger=None) -> Optional[Config]:
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return self._config
        if self._config is not None and mtime == self._mtime:
            return self._config
        try:
            cfg = load_config(self.path)
        except (ConfigError, yaml.YAMLError, OSError) as e:
            if logger:
                logger.error("config reload failed, keeping previous: %s", e)
            self.error = str(e)
            self._mtime = mtime
            return self._config
        first = self._config is None
        self._config = cfg
        self._mtime = mtime
        self.error = None
        if logger and not first:
            logger.info("config reloaded from %s", self.path)
        return self._config
