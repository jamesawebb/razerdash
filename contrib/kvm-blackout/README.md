# KVM blackout

The BlackWidow V4 going completely dark after a KVM switch-in: what it is, and
how razerdash now gets out of it. Only a loss of bus power revives the keyboard
— a physical replug, or, as it turns out, a hub port power-off over sysfs.

| file | what it is |
|---|---|
| `probe.py` | run it while the keyboard is dark: state dump + a ladder of recoveries |
| `install.sh` | installs the udev rule + helper below, then proves the rule fires |
| `99-razerdash-port-power.rules` | hands the keyboard's port power switch to `plugdev` |
| `razerdash-port-power-perms` | the rule's helper: sysname → that port's `disable` |

## Result: only a power cut recovers it, and sysfs can do it (2026-10-01)

Probed during the blackout of 2026-10-01 17:58
(`~/.local/state/razerdash/blackout-20261001-175939.log`). The ladder ran to
the end:

| Step | Recovery | Result |
|---|---|---|
| — | press Caps Lock | indicator dark too |
| 1 | hardware static effect | dark |
| 2 | brightness to 100% | dark; `matrix_brightness` read back **0** |
| 3 | `device_mode` 0x00 → 0x03 | dark |
| 4 | USB re-enumerate, power kept (`authorized` 0 → 1) | dark |
| 5 | **hub port power-off for 3 s (`port2/disable`)** | **lit** |

What that settles:

- **It takes a power cut, not a reset.** Step 4 is a complete USB
  re-enumeration — new `devnum`, fresh `razerkbd` bind, fresh openrazer init —
  and the keyboard stayed dark. Only dropping VBUS cleared it. The bad state
  lives in firmware and nothing sent over the USB wire reaches it.
- **No physical unplug is needed.** The KVM's hub (`3-4.1.3`) does support
  per-port power switching, so `echo 1 > …/3-4.1.3-port2/disable`, 3 s,
  `echo 0` *is* the replug. After the probe exited, razerdash resumed drawing
  normally with the keyboard never leaving the KVM. The "physical reset"
  recipe in the main README (unplug, hold Ctrl+CapsLock+Space, replug) is for
  a different failure — stuck *device* mode — and does not apply here.
- **The Caps Lock indicator is dark as well**, so the whole LED controller is
  wedged, not just the matrix effect.
- **No detector yet — the obvious candidate turned out to be an artifact.**
  Step 2's `matrix_brightness 0` read like "the firmware stops taking writes
  while dark", but a healthy keyboard does exactly the same thing. Measured on
  this hardware (v1.3): a read issued *immediately* after a brightness write
  answers **0**, the written value appears from ~10–30 ms on, and the odd junk
  value (`1`, `5`, `8`) turns up in between.

  ```
  wrote 127 -> reads ['0', '0', '127', '127', '127', '127', '127']
  wrote 200 -> reads ['0', '200', '200', '1', '5', '200', '200']
  wrote 255 -> reads ['0', '255', '255', '255', '255', '255', '255']
  ```
  (reads at roughly 0, 10, 40, 100, 220, 470 and 970 ms after the write;
  keyboard lit and working throughout)

  So that one reading says nothing about the blackout, and everything else
  readable — `device_mode`, serial, firmware version, `poll_rate`, `kbd_layout`,
  brightness — is identical dark and healthy, while draws are fire-and-forget.
  **Nothing is known to tell the two states apart in software.** What the next
  blackout should answer: does a write still land when the read is given its
  settle time? `razerdash fix-blackout --test-only` reports exactly that and
  touches nothing.
- Step 5's follow-up custom-frame check was answered **no — it stayed white**.
  The first hardware static effect lit the keys; the red flash that should have
  followed never showed. Either the writes after the first were dropped while
  openrazer was still initialising, or the 0.5 s flashes were too quick to
  catch. razerdash itself drew normally seconds later, so this is the *check*
  being unconvincing rather than a second fault — it now holds each state for
  2 s and paints a pattern no hardware effect can imitate.

## Automatic recovery

razerdash acts on the result above. `razerdash/blackout.py` is the mechanism --
find the keyboard's hub port, cut its power for 3 s -- and there are two ways in.

**By hand**, whenever the keyboard is dark:

```sh
razerdash fix-blackout               # power-cycle the keyboard's USB port
razerdash fix-blackout --test-only   # just report the firmware write test
```

**Automatically**, on attach: the daemon asks the firmware whether it still
takes a write (`Daemon._recover_blackout`) and power-cycles the port if it
doesn't. That test is **unproven** -- see the detector bullet above -- and is
built to fail safe: a wedged keyboard that still accepts brightness writes reads
as healthy, nothing happens, and `fix-blackout` is there to finish the job. If
the next blackout shows the test does fire, this becomes the whole fix; if it
shows the test can't see it, the detection comes out and the command stays.

Either way the power cut needs write access to the port's `disable`:

```sh
sudo bash contrib/kvm-blackout/install.sh
```

installs the udev rule that hands that one attribute to `plugdev` -- the group
openrazer already needs for the razerkbd attributes -- for the one port the
keyboard is on, and then proves the rule fires by resetting the attribute and
replaying a real `add` uevent. Without the rule the daemon only reports the
blackout and asks for a replug; it never tries to become root.

If the rule ever stops firing, parse it first: `udevadm verify
99-razerdash-port-power.rules` (no root needed). udev **drops a line it cannot
parse** and says so only in `journalctl -b -t systemd-udevd`, so a bad match key
looks exactly like a rule that ran and did nothing — `DEVTYPE=="usb_device"`
instead of `ENV{DEVTYPE}` cost one round of that, which is why install.sh now
verifies before it installs.

What the automatic path looks like in the journal:

```
WARNING device 'keyboard' attached dark (LED engine wedged, the KVM blackout);
        cutting power at 3-4.1.3-port2 for 3.0s -- the keyboard stops
        responding until it comes back
INFO    device 'keyboard' detached
INFO    device 'keyboard' attached: Razer BlackWidow V4 (8x23)
INFO    device 'keyboard' is lit again after the power cycle
```

The guard rails, because a false positive cuts power to a keyboard someone may
be typing on:

- **Attach only.** The test writes to the firmware, so it runs on the probe
  where the device first appears, never every tick, and never while
  `calibrate`/`webedit`/this probe hold the pause marker.
- **Confirmed three times**, each write given its settle window, and the
  original brightness always put back.
- **Two cycles per blackout.** The device re-attaching re-runs the test: a cycle
  that worked clears the count, one that didn't counts towards it, and after the
  second the daemon asks for a physical replug and goes quiet.
- **Opt out** per device with `recover_blackout: false`; and it is a no-op
  wherever the mechanism cannot apply -- a non-keyboard (no razerkbd node), a hub
  with no per-port power switch, a `disable` that isn't writable.

## What is known

- Since 2026-09-19 it has happened on every switch-in to quark (five so far);
  the seven switch-ins from Sep 14–17 were fine. Each blackout shows up in the
  kernel log as a keyboard-only `USB disconnect` 11–63 s after the switch-in:
  that is the manual replug (confirmed for 2026-09-28 19:37).
- Both KVM hosts run openrazer, and apparently razerdash under sway too. The
  keyboard stays powered across a switch, so it arrives having just been
  driven by the *other* host. openrazer's `devices_off_on_screensaver` is
  probably not the trigger: under sway nothing emits the ScreenSaver signal
  it listens for, and quark's `razer.log` has never logged `Suspending`.
- It happens on the **first** enumeration after a switch-in. The keyboard's
  USB link, `razerkbd` binding, openrazer init and razerdash's draws all look
  normal in the logs.
- `device_mode` reads back `03 00` (driver mode) while dark. razerdash's
  driver-mode guard reads it live from the firmware every probe and has never
  logged a `re-asserted driver mode` or `stuck in device mode` line during a
  blackout, so this is **not** the "driver mode didn't stick" failure the guard
  was written for.
- For this model the kernel driver sends custom frames and the custom effect
  fire-and-forget (`want_response = false` in `razerkbd_driver.c`), so a draw
  "succeeds" no matter what the firmware does with it.
- A replug fixes it, but a KVM switch-in, which re-enumerates the keyboard
  without cutting its power, does not. So the bad state lives in firmware and
  survives a USB reset. Confirmed deliberately at step 4 above.
- Upstream: openrazer
  [#2291](https://github.com/openrazer/openrazer/issues/2291) (same PID
  `1532:0287`, dark until replugged, only with openrazer-daemon running) and
  [#2376](https://github.com/openrazer/openrazer/issues/2376). Both were closed
  wontfix with no root cause.

## What the probe finds out

Run it while the keyboard is dark, **before** replugging:

```sh
python3 contrib/kvm-blackout/probe.py
```

It pauses razerdash, logs the firmware's live state (mode, brightness, poll
rate, etc.) plus the recent kernel, openrazer and razerdash logs, and then
works through the recoveries in order of how disruptive they are, asking after
each one whether the keys lit up:

1. hardware static effect (is the LED engine alive at all?)
2. brightness to 100%
3. `device_mode` 0x00 → 0x03 toggle
4. USB re-enumerate with power kept (`authorized` 0 → 1, sudo)
5. hub port power-off for 3 s (`port*/disable`, sudo). This only cuts VBUS if
   the KVM's hub supports per-port power switching — on this KVM it does, and
   this is the step that works.

After any step that lights the keyboard, it checks that custom frames (what
razerdash sends) also work, by painting alternating columns no hardware effect
can produce and holding them while it asks. The full run also traces a
brightness write's read-back — the candidate detector, and the one thing the
next blackout still has to answer. The log goes to
`~/.local/state/razerdash/blackout-*.log`.

`--snapshot` does the read-only state dump only, and is safe to run at any
time.

## Checking the other host

openrazer keeps its own log file, independent of how the journal is set up.
On the host you switched *away from*:

```sh
grep -hE "Suspending|Resuming|Removing|Found valid" ~/.local/share/openrazer/logs/razer.log*
```

Each `Removing 0003:1532:0287...` is the keyboard leaving that host. Compare
with quark's dark switch-ins (2026-09-19 08:15, 09-21 12:24, 09-22 18:28,
09-27 07:59, 09-28 19:36) and good ones (09-14 to 09-17). Also worth knowing:
what changed on that host around 2026-09-17–19, e.g. razerdash installed or
upgraded there.
