"""Interactive keymap builder: light each key, record its name/coordinate."""
from __future__ import annotations

import os
import sys
import time

from .backend import get_backend
from .daemon import PAUSE_FILE
from .keygroup import KeyMap, KeyMapError, save_keymap

WHITE = (255, 255, 255)


def _existing(path):
    """The output keymap's current content as (cells set, cell -> name), so a
    re-run edits it in place instead of starting over."""
    try:
        km = KeyMap.load(path)
    except FileNotFoundError:
        return set(), {}
    except (OSError, KeyMapError) as e:
        print(f"(ignoring unreadable {path}: {e})", file=sys.stderr)
        return set(), {}
    return set(km.cells), {cell: name for name, cell in km.names.items()}


def _pause_renderer():
    """Ask a running razerdash daemon to stop drawing (marker holds our PID)."""
    try:
        os.makedirs(os.path.dirname(PAUSE_FILE), exist_ok=True)
        with open(PAUSE_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        print("Pausing live rendering during calibration...")
        time.sleep(1.0)  # give the daemon a moment to notice and stop drawing
    except OSError:
        pass


def _resume_renderer():
    try:
        os.remove(PAUSE_FILE)
    except OSError:
        pass


def run(args) -> int:
    backend = get_backend("openrazer")
    dev = backend.find_device(args.match)
    if dev is None:
        print(f"no device matching {args.match!r}", file=sys.stderr)
        return 2

    _pause_renderer()
    try:
        return _calibrate(dev, args)
    finally:
        _resume_renderer()


def _calibrate(dev, args) -> int:
    cells, bycell = _existing(args.output)
    print(f"Calibrating {dev.name} ({dev.rows}x{dev.cols}).")
    print("Each cell lights white in turn. Type the key's name (e.g. 'a', 'esc',")
    print("'f1', 'space'), press Enter alone to skip an empty cell, or 'qqq' to")
    print("finish and save.\n")
    if cells or bycell:
        print(f"Editing {args.output} ({len(cells)} cells, {len(bycell)} named).")
        print("A cell's current name is shown [in brackets]: Enter keeps it, a")
        print("new name replaces it, and '-' clears the cell (nothing there).")
        print("Cells not visited before 'qqq' keep their current entries.\n")
    else:
        print("The matrix has more cells than physical keys, so many cells drive no")
        print("LED -- the very first cell is often such a gap. If NOTHING lights,")
        print("press Enter to skip. Always name the key that is lit, even if the")
        print("coordinates seem off.\n")

    stop = False
    for r in range(dev.rows):
        if stop:
            break
        for c in range(dev.cols):
            dev.draw({(r, c): WHITE}, (0, 0, 0))
            cur = bycell.get((r, c))
            hint = f" [{cur}]" if cur else ""
            try:
                ans = input(f"[row {r:>2}, col {c:>2}] name{hint} > ").strip()
            except EOFError:
                stop = True
                break
            if ans == "qqq":
                stop = True
                break
            if not ans:
                continue  # keep the cell's current entry (or absence)
            if ans == "-":
                cells.discard((r, c))
                bycell.pop((r, c), None)
                continue
            prev = next((cell for cell, n in bycell.items() if n == ans), None)
            if prev is not None and prev != (r, c):
                # A name identifies one cell; retyping it here moves it (and
                # can't silently strand the old cell as named-but-nameless).
                del bycell[prev]
            cells.add((r, c))
            bycell[(r, c)] = ans

    dev.clear()
    out_cells = [list(cell) for cell in sorted(cells)]
    out_names = {name: list(cell) for cell, name in sorted(bycell.items())}
    save_keymap(args.output, out_cells, out_names)
    print(f"\nWrote {len(out_cells)} cells ({len(out_names)} named) to {args.output}")
    return 0
