# razerdash

Drive the RGB LEDs on **Razer** devices — the per-key matrix of a **Blackwidow
V4** keyboard, the 19 zones of a **Firefly V2** mousepad — from **Prometheus**
metrics. Each row, column, or zone strip becomes a bar ("key group") bound to a
metric and a lighting method, all defined in one config file.

Because the KVM wires the devices to only one laptop at a time, razerdash runs
locally on each laptop (Quark and the work machine). It idles while nothing is
attached and starts rendering the moment the KVM switches a device in — the
OpenRazer device simply appears in the device list. Each configured device is
probed independently, so the keyboard and mousepad can come and go separately.

A KVM re-attach can also leave the keyboard **blank while every draw
succeeds**: openrazer's switch of the keyboard into *driver mode* sometimes
doesn't stick, and in device mode custom frames are accepted but ignored. The
daemon guards against this by reading `device_mode` from the razerkbd sysfs
node each probe and re-asserting driver mode (`0x03`) whenever it reads back
wrong (needs membership of the `plugdev` group, which openrazer sets up). If
even that write won't stick the daemon logs that the keyboard needs its
physical reset: unplug, hold Ctrl+CapsLock+Space, plug back in while holding.

```
 Prometheus (cobra:9090)  ──HTTP──▶  razerdash  ──OpenRazer matrix──▶  Blackwidow V4
                                               └─OpenRazer matrix──▶  Firefly V2
```

## How it works

Every `refresh_interval` the daemon, for each binding:

1. runs the binding's PromQL against Prometheus and reduces it to one number,
2. normalises that number to 0..1 using `range`,
3. resolves the `key_group` to matrix coordinates,
4. applies the `lighting.method` to produce per-key colours,

then composites all bindings into one frame per device (a binding's `device:`
field picks which, default the first) and draws each in one pass. Bindings are
drawn in ascending `priority`, so later ones win on overlap. Every device is
drawn with the same background colour each tick, so cells no binding paints —
unused keys, unprogrammed mousepad zones — stay in sync across devices. (With
an animated `background`, the daemon also keeps redrawing those frames between
polls at the effect's frame rate — see
[Background and off colours](#background-and-off-colours).)

### Lighting methods

| `method`           | keys lit          | colour                              |
|--------------------|-------------------|-------------------------------------|
| `fill_threshold`   | ∝ value (bar)     | threshold band chosen by value      |
| `fill_fixed`       | ∝ value (bar)     | one fixed `color`                   |
| `solid_threshold`  | whole group       | threshold band chosen by value      |
| `gradient`         | whole group       | interpolated across `stops` by value|

### Background and off colours

`background` (top level) sets the colour of **every key not lit by a binding** —
and, because a fill bar only paints its lit keys, the **unlit part of a bar** too
("dimmed by a metric"). `"#000000"` leaves those keys dark; any other colour
washes the whole board behind the bars.

A binding may override the background for *its own* group with
`lighting.off_color` (e.g. a dim red behind a red CPU bar). When `off_color` is
unset, that group's unlit keys fall through to the global `background`.

Instead of a fixed colour, the background can be an **animated effect** — a
software rendition of Razer's "spectrum cycling". The hardware chroma effects
are whole-board modes that cannot run behind the custom frame the bindings are
drawn with, so razerdash renders the effect itself: between Prometheus polls the
daemon keeps redrawing the last frame at `fps`, cycling the background hue,
with the metric keys composited on top.

```yaml
background:
  effect: spectrum      # static (default) | spectrum
  color: "#0a0a12"      # static only: the fixed colour (same as the scalar form)
  fps: 10               # spectrum only: frame rate, redraws per second (max 60)
  period: 30s           # spectrum only: one full lap around the hue wheel
  brightness: 0.4       # 0..1 — keep low so the metric bars stay readable
  saturation: 1.0       # 0..1
```

`effect: static` with a `color` is identical to the plain-colour form, so a
config can keep both sets of knobs and switch behaviour by flipping the one
`effect:` word.

Because unlit fill-bar keys inherit the background, they animate too — set
`off_color` on a binding to keep its bar region static. The hue is a function
of wall time, so the cycle stays smooth across polls and even stays in phase
across machines.

### Night mode (LEDs off while you sleep)

A glowing keyboard in a dark bedroom is a problem. The `sleep:` block turns
the LEDs **off entirely** when either trigger applies, and resumes rendering
within a couple of seconds once neither does:

```yaml
sleep:
  follow_sway: true    # dark while ALL sway outputs are powered off
  idle_timeout: 0      # dark after this long without input, e.g. 20m; 0 = off
```

**`follow_sway`** (default on) ties the keyboard to the monitors: when sway
reports every output powered off — typically because your existing `swayidle`
timeout fired — the board goes dark; the moment sway wakes the displays (any
keypress or mouse move), the dashboard comes back. There is no second timeout
to keep in sync: the keyboard simply inherits whatever idle policy already
drives the screens. The daemon finds the sway IPC socket itself
(`/run/user/<uid>/sway-ipc.*.sock`), so no `SWAYSOCK` plumbing is needed in
the systemd unit; without sway the option is inert and costs nothing.

**`idle_timeout`** is the fallback for machines not running sway: the daemon
watches `/dev/input/event*` for keyboard/mouse activity and goes dark after
the given quiet period, waking on the next event. Reading the event devices
needs permission — on Debian, add yourself to the `input` group
(`sudo usermod -aG input $USER`, then log in again); if the devices aren't
readable the daemon logs one warning and leaves the LEDs on. `0` (the
default) disables it.

Both may be enabled at once — either condition darkens the board. Going dark
draws one black frame (repeated at `idle_interval`, which also self-heals a
KVM switch while asleep) and pauses the spectrum animation, so a sleeping
razerdash costs ~nothing.

### Config errors

Config is hot-reloaded on save. If a save is invalid — bad YAML, an invalid
binding, or **duplicate row/column indexes** (two bindings fighting over the same
keys) — the daemon logs the problem, **keeps running the last good config**, and
**flashes one key red** so you notice without watching the journal. The flashed
key is `error_key` (default `mute`, resolved via your keymap; or a `[row, col]`
coordinate). Fix the file and the next save clears the error and stops the flash.

### Devices

`devices:` names every device to drive; each entry is a substring `match` on
the OpenRazer device name, optionally with its own `keymap`. A bare string is
shorthand for `{ match: ... }`:

```yaml
devices:
  keyboard:
    match: "BlackWidow V4"
    keymap: keymap.yaml     # per-device; the pad below needs none
  mousepad: "Firefly V2"    # 19 zones = a 1x19 matrix (row 0, cols 0-18)
```

The **first** entry is the *primary* device: bindings without a `device:` field
paint onto it, and the error key flashes there. A binding targets another
device with `device: mousepad`. Anything no binding paints — unused keys,
unprogrammed zones — shows the shared `background`, drawn with the same colour
on every device each tick, so an animated background cycles in lockstep across
the keyboard and the pad.

The historical single-device form (a top-level `device: {match: ...}` plus
`keymap:`) still parses and is equivalent to a lone `keyboard:` entry.

### Key groups

- `type: row, index: N` — matrix row N, a horizontal bar.
- `type: column, index: N` — matrix column N, a vertical bar.
- `type: keys, keys: [[r,c], ...]` — an arbitrary set (names allowed with a keymap).

`order` (`left_to_right` / `right_to_left` / `bottom_to_top`) sets the fill
direction. Coordinates are per-device: the same group spec means row 0 of
whichever device the binding targets.

### Overlapping groups

All bindings composite onto a single frame, so every key shows at most one
binding. Where two groups share a key — most often a vertical `column` crossing a
horizontal `row` — the binding with the higher `priority` wins that key; on equal
priority the binding **listed later in the config** wins. Non-shared keys are
unaffected. Set `priority:` (default `0`) to control who wins.

The duplicate-index check only rejects two bindings on the *same* row index or
*same* column index *of the same device*; a row crossing a column shares a
single key and is allowed, resolved by priority/order.

### Reusable / named key groups

A top-level `key_groups:` map defines named groups once; a binding's
`key_group:` can then be either an inline spec or the name of one of them:

```yaml
key_groups:
  macro_m1_m5: { type: keys, keys: [m1, m2, m3, m4, m5] }
  fn_f1_f12:   { type: keys, keys: [f1, f2, f3, f4, f5, f6, f7, f8, f9, f10, f11, f12] }

bindings:
  - name: targets-down
    metric: 'sum(up == 0)'
    range: { min: 0, max: 5 }
    key_group: macro_m1_m5      # <-- reference by name
    lighting: { method: fill_fixed, color: "#ff0000" }
```

Groups that reference keys by name (e.g. `m1`, `f1`, `"1"`) need a `keymap`
from `razerdash calibrate`; a binding using one before calibration is skipped
with a warning instead of taking the dashboard down. The example config ships a
palette of these (macro column, number-row digits, F-key clusters) as
placeholders.

Key groups are device-agnostic: the same named group can be referenced by
bindings on different devices, resolving against each device's own
matrix/keymap. Which device gets painted is chosen only by the **binding's**
`device:` field — a `device:` (or any other unrecognised key) inside a key
group spec is silently ignored, and the binding falls back to the primary
device.

### Machine-local metrics

A binding's `metric` may contain `${host}` (this machine's short hostname) or
`${fqdn}` (its fully-qualified name), substituted at query time. That lets one
identical config show each laptop's *own* load — e.g. on Quark

```yaml
metric: '100 - (avg(rate(node_cpu_seconds_total{mode="idle",instance="${host}.lan:9100"}[1m])) * 100)'
```

queries `quark.lan:9100`; on the work laptop it queries that machine instead.
Unknown `$name` tokens are left untouched, so ordinary PromQL is unaffected.

### Session windows (usage quotas that reset)

A sliding `increase(counter[5h])` is the wrong shape for a quota like
Claude's 5-hour session: after the provider resets, the sliding window keeps
counting the old usage for up to five more hours (the bar decays instead of
snapping to zero), and it can over-read a fresh session that follows a heavy
one. A binding's `session:` block gives it real session semantics:

```yaml
- name: claude-session-cost
  metric: '100 * sum(increase(claude_code_cost_usage_USD_total[${session}])) / 17.27'
  session:
    activity: 'sum(increase(claude_code_cost_usage_USD_total[10m]))'
    # length: 5h        # window duration (default)
    # align: 1h         # anchor truncation (default; Claude resets on the hour)
```

The daemon tracks the window the way the provider does: it opens at the
first activity after the previous window expired — anchored to the top of
the hour, which is how Claude's `/usage` reset times behave — lasts
`length`, then usage snaps to zero until new activity opens the next window.
`${session}` in the metric expands to the time since the window opened, so
`increase(counter[${session}])` is exactly "usage this session"; between
windows the binding reads an honest 0 without querying.

`activity` is any PromQL expression that is > 0 while the account is in use;
a `[10m]` increase of the same counter is the right shape (the daemon's
startup replay samples history at 10-minute steps to find a session already
in progress, so the window sizes should match). The journal logs each
window: `new session window opened at 07:00 (resets 12:00)`.

Approximations, both bounded by the hour alignment and self-correcting at
the next real gap: a session whose first message lands in the last minutes
before an hour mark can bootstrap one hour late after a daemon restart, and
continuous usage past an expiry opens the next window immediately rather
than at the next message.

#### Tracking Claude Code usage specifically

Claude Code's OTEL telemetry exports **counters only** — there is no
"percent of session used" metric — so a usage bar has to be built from
`claude_code_token_usage_tokens_total` or `claude_code_cost_usage_USD_total`
and **calibrated against the `/usage` command**, which is the only ground
truth for the session limit:

1. Read the session percentage from `/usage` (say **63%**).
2. At (about) the same moment, query the raw usage inside the current
   session window — the daemon's journal line gives the window, or:
   `sum(increase(claude_code_cost_usage_USD_total{user_email="..."}[<elapsed>]))`.
3. The divisor for a 0–100 metric is `100 × raw / 63`; put it in the
   binding:
   `100 * sum(increase(...[${session}])) / <divisor>`.

Worth knowing:

- **Prefer the cost counter over tokens.** Raw token counts weight a cache
  read the same as an output token, and cache reads are routinely ~95% of
  the total, so the token scale drifts whenever the cache mix changes
  between sessions. `cost_usage_USD` weights token types like the real
  accounting does; its divisor is also meaningful by itself (the session
  budget in API-equivalent dollars). A token bar calibrated on the same day
  can read 15–20 points apart from cost within a week.
- **Recalibrate with one `/usage` reading** whenever the bar and `/usage`
  disagree: repeat the recipe above; only the divisor changes.
- Cost is Claude Code's *estimate*, not Anthropic's ledger — close enough
  for an LED bar, not for billing.
- The session anchor matches `/usage`'s behaviour (resets on the hour); the
  journal logs `new session window opened at 13:00 (resets 18:00)` so the
  bar's reset time can be checked against `/usage`'s directly.

### Templated config (Jinja2)

The whole config file is rendered with **Jinja2 before YAML parsing**, with
`env` (the process environment), `host` and `fqdn` available:

```yaml
prometheus:
  url: {{ env.get('PROM_URL', 'http://cobra:9090') }}

bindings:
  - name: local-cpu        # every machine gets this one
    ...
{% if host == 'quark' %}
  - name: cobra-docker     # only quark shows the Docker bar
    ...
{% endif %}
```

Worth knowing:

- Templates render at **(re)load time**; the `${host}` substitution above
  happens at **query time**. Both work, and a config using neither renders
  unchanged.
- An undefined variable (e.g. `{{ hots }}`) or template syntax error is a
  config error like any other: the daemon keeps the last good config and
  flashes the error key. Use `env.get('NAME', 'default')` for optionals.
- The daemon's environment is what systemd provides — set variables with
  `Environment=`/`EnvironmentFile=` in `razerdash.service`, not your shell.
  Environment changes take effect on the next config reload (touch the file)
  or service restart.
- YAML error line numbers refer to the *rendered* text, which can drift from
  the source once `{% if %}` blocks drop lines.
- `keymap.yaml` is machine-written and is **not** templated.

### Metrics beyond node_exporter (e.g. Gmail counts)

Anything that can write a number into Prometheus can light a key group — the
daemon only ever sees PromQL. The easiest route for a custom value is a small
script writing to node_exporter's *textfile collector*; `contrib/gmail-exporter/`
does exactly that, counting Gmail messages that match saved search filters
(OAuth, exact counts, systemd timer included) so a binding can show unread
mail: `metric: 'gmail_messages{filter="github-unread"}'`. See its README for
the Google Cloud setup.

## Install (on each laptop)

```sh
sudo apt install python3-openrazer          # OpenRazer Python client + daemon
pipx install --system-site-packages .       # or: pip install --user .
mkdir -p ~/.config/razerdash
cp config/config.example.yaml ~/.config/razerdash/config.yaml
$EDITOR ~/.config/razerdash/config.yaml      # set the cobra:9100 instance label
```

**`--system-site-packages` is required.** `python3-openrazer` is an apt package
in the system Python's `dist-packages`; a default pipx/venv is isolated from it,
so `import openrazer` fails and razerdash silently falls back to the **mock**
backend — metrics are fetched and logged but the keyboard never lights up. If
you see `drew Razer BlackWidow V4 (mock)` in the journal (note the `(mock)`),
that is exactly this. Confirm the venv can see it after installing:

```sh
razerdash list-devices
# keyboard: matched Razer BlackWidow V4  matrix=8x23
# mousepad: matched Razer Firefly V2  matrix=1x19
```

(With no args it probes every device in the config; `--match <substring>`
probes one name directly.)

(`pip install --user .` into the system Python avoids the problem too, since
that interpreter already has `openrazer`.)

Run it under your user systemd session so it starts at login:

```sh
mkdir -p ~/.config/systemd/user
cp systemd/razerdash.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now razerdash.service
journalctl --user -u razerdash -f
```

## Try it without hardware

A mock backend renders the matrix to the terminal with truecolor blocks, so you
can see the layout before touching the keyboard:

```sh
python -m razerdash --backend mock run --once        # one frame
python -m razerdash --backend mock run               # live loop
```

(With no reachable Prometheus, bindings are skipped and the grid stays dark;
point `prometheus.url` at a real server to see the bars fill.)

Check what your bindings currently resolve to, without drawing anything:

```sh
python -m razerdash query
# cobra-cpu: value=41.20 norm=41% group=row:3 keys=23 lit=9 method=fill_threshold
```

## Calibrate (recommended, on the real keyboard)

The OpenRazer matrix has gaps (not every cell is a physical key) and the exact
dimensions vary by model. Build a keymap once so row/column groups skip gaps and
you can name keys:

```sh
razerdash calibrate            # lights each cell; type its name, 'qqq' to finish
```

The prompt's `[row, col]` is a **matrix address, not a physical key position** —
the matrix has more cells than the keyboard has keys, and there is no API to ask
which cells are populated. Many cells have no LED behind them (on the BlackWidow
V4, all of column 0, plus gaps at wide keys and between key blocks — the very
first cell is such a gap). **If nothing lights, press Enter to skip that cell.
Always name the key that is lit, even if the coordinates seem off.**

While it runs, calibrate pauses a running razerdash daemon (via a marker file) so
the two don't fight over the LEDs; normal rendering resumes automatically when you
finish, quit with `qqq`, or if calibrate is interrupted.

**Re-running calibrate edits the existing keymap** (when `--output` already
exists) instead of starting over: each cell's current name is shown in the
prompt as `name [m1] >` — press Enter to keep it, type a new name to replace
it, or type `-` to clear the cell entirely. Typing a name that already belongs
to another cell *moves* it. Cells you never reach (finishing early with `qqq`)
keep their current entries, so fixing one key is: run, Enter through to it,
retype it, `qqq`.

This writes `~/.config/razerdash/keymap.yaml`; reference it from `config.yaml`
with `keymap: keymap.yaml` — a relative path is resolved next to `config.yaml`
(`~` and absolute paths also work; `$HOME`-style env vars are not expanded).

## Edit the keymap in a browser

`calibrate` is a linear sweep — good for a first pass, tedious for touch-ups. To
fix a single name or mark a missed key, use the graphical editor instead:

```sh
razerdash webedit              # then open http://127.0.0.1:8380/
```

It serves a local-only page showing the whole matrix as a grid over
`keymap.yaml`. Click a cell (or arrow-key around) and **that key lights up white
on the keyboard**, so you always know which physical key a matrix address is.
Type to name it, <kbd>Enter</kbd> applies and advances, <kbd>Del</kbd> clears;
"Key only" marks a key present but nameless (a `cells` entry). Nothing touches
disk until you press **Save**.

Details worth knowing:

- Like calibrate, it pauses a running daemon while open (and resumes it on
  Ctrl-C) — but it won't steal the pause marker from a live calibrate run.
- Save detects if the file changed on disk since you loaded it (e.g. a
  calibrate run finished meanwhile) and asks before overwriting.
- No keyboard attached? The grid still works for editing names; the live
  light preview just stays off until the device appears (KVM switch-in).
- `--output PATH` edits a different keymap file, `--port N` moves the server.

## Tests

Plain-Python regression suites live in `tests/` (no framework needed — each
prints its checks and exits non-zero on failure). Run them with an
interpreter that can import razerdash's dependencies, e.g. the pipx venv:

```sh
for t in tests/test_*.py; do ~/.local/share/pipx/venvs/razerdash/bin/python "$t" || break; done
```

## Commands

| command                 | purpose                                        |
|-------------------------|------------------------------------------------|
| `razerdash run`          | the daemon (default; `--once` for one frame)   |
| `razerdash explain`      | plain-English summary of the config (`--values` adds live readings) |
| `razerdash query`        | print each binding's value / lit-key count     |
| `razerdash list-devices` | show each configured device's match + matrix size|
| `razerdash calibrate`    | interactively build a keymap                   |
| `razerdash webedit`      | edit the keymap graphically in a browser       |

Global flags: `--config PATH`, `--backend {auto,openrazer,mock}`, `-v`.

## Licence

GPL-3.0-or-later -- see [LICENSE](LICENSE). razerdash drives the hardware through
the [openrazer](https://github.com/openrazer/openrazer) Python client, which is GPL-2+.
