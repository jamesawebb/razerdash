# Install razerdash — quick start

Drive a Razer keyboard's LEDs from Prometheus metrics. This is the impatient
path: copy, paste, done. See `README.md` for how it all works.

## Prerequisites

- Debian/Ubuntu with an OpenRazer-supported Razer keyboard plugged in.
- A reachable Prometheus (the example config points at `http://cobra:9090`).
- [`pipx`](https://pipx.pypa.io) installed (`sudo apt install pipx`).

## 1. Install (run from the repo directory)

```sh
sudo apt install python3-openrazer            # OpenRazer client + daemon
sudo gpasswd -a "$USER" plugdev               # OpenRazer needs you in 'plugdev'
                                              # (log out/in once if you were just added)

pipx install --system-site-packages .         # <-- the flag is REQUIRED (see gotcha #1)

mkdir -p ~/.config/razerdash
cp config/config.example.yaml ~/.config/razerdash/config.yaml
```

## 2. Point it at your metrics

Edit `~/.config/razerdash/config.yaml`: set `prometheus.url` and the `instance=`
labels in each binding's metric to match your targets. Saved changes are
**hot-reloaded** — no restart needed. Sanity-check what it will do:

```sh
razerdash explain --values     # prints each row's metric, colour rule, and live reading
```

## 3. Run it at login (systemd user service)

```sh
mkdir -p ~/.config/systemd/user
cp systemd/razerdash.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now razerdash.service
journalctl --user -u razerdash -f              # watch it work; Ctrl-C to stop watching
```

## 4. Verify it's driving real hardware

```sh
razerdash list-devices     # expect: matched: Razer BlackWidow V4  matrix=8x23
```

The journal should show `drew Razer BlackWidow V4: ...`. If it says
`... (mock)`, see gotcha #1.

## 5. (Optional) Calibrate

Only needed to reference keys by name (`m1`, `f1`, `mute`, …) or to skip the
matrix's physical gaps:

```sh
razerdash calibrate        # lights each cell; type its name, 'qqq' to finish
```

Many matrix cells have no LED behind them (the first cell usually included) —
if nothing lights, press Enter to skip. Always name the key that is lit, even
if the coordinates seem off.

Then set `keymap: ~/.config/razerdash/keymap.yaml` in your config.

For touch-ups afterwards (fix one name, mark a missed key) there's a graphical
editor — click a cell in the browser and that key lights up on the keyboard:

```sh
razerdash webedit          # then open http://127.0.0.1:8380/
```

---

## Gotchas (the three things that trip people up)

1. **Keyboard never lights, but the journal shows metrics / `drew ... (mock)`.**
   `python3-openrazer` is a system package; a default pipx venv can't see it, so
   razerdash silently falls back to a terminal mock. Reinstall with the flag:
   `pipx install --system-site-packages --force .`

2. **Nothing lights right after a reboot.** The daemon idles until OpenRazer
   registers the keyboard, then starts on its own — no action needed. (It also
   idles whenever a KVM has the keyboard switched to another machine.)

3. **The `mute` key is flashing red.** Your last config edit was invalid — the
   daemon kept the previous good config. Run `razerdash explain` (or
   `journalctl --user -u razerdash -n 20`) to see the error; fix and save, and the
   flash stops.

## Commands

| command                 | purpose                                        |
|-------------------------|------------------------------------------------|
| `razerdash explain`      | plain-English summary (`--values` adds readings) |
| `razerdash list-devices` | show the matched device + matrix size          |
| `razerdash calibrate`    | build a keymap interactively                   |
| `razerdash webedit`      | edit the keymap graphically in a browser       |
| `razerdash run --once`   | draw a single frame and exit                   |
