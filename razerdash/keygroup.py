"""Resolve a key_group spec into concrete (row, col) matrix coordinates."""
from __future__ import annotations

import os

import yaml


class KeyMapError(Exception):
    """keymap.yaml exists but its content is unusable (bad YAML / wrong shape)."""


class KeyMap:
    """Optional calibration data: which matrix cells are real keys, plus names."""

    def __init__(self, cells=None, names=None):
        self.cells = set(cells or [])
        self.names = names or {}

    @classmethod
    def load(cls, path) -> "KeyMap":
        with open(path, encoding="utf-8") as f:
            try:
                raw = yaml.safe_load(f) or {}
            except yaml.YAMLError as e:
                raise KeyMapError(f"invalid YAML in {path}: {e}") from e
        if not isinstance(raw, dict):
            raise KeyMapError(f"{path}: top level must be a mapping")
        try:
            cells = {(int(c[0]), int(c[1])) for c in raw.get("cells") or []}
            names = {str(k): (int(v[0]), int(v[1]))
                     for k, v in (raw.get("names") or {}).items()}
        except (TypeError, ValueError, IndexError, KeyError) as e:
            raise KeyMapError(f"{path}: malformed cells/names: {e}") from e
        return cls(cells, names)

    def resolve_name(self, name):
        if name not in self.names:
            raise KeyError(f"key name {name!r} not in keymap")
        return self.names[name]


def save_keymap(path, cells, names):
    """Write a keymap file atomically (temp file + rename), so an interrupted
    save can never leave a truncated file behind for the daemon to choke on."""
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump({"cells": cells, "names": names}, f, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def resolve(key_group, rows: int, cols: int, keymap: "KeyMap | None" = None):
    """Return an ordered list of (row, col) coordinates for a key group.

    Without a keymap, a row/column group spans the full matrix width/height;
    empty cells simply won't light. With a keymap, only real keys are used.
    """
    kg = key_group
    keys: list[tuple[int, int]] = []

    if kg.type == "keys":
        for item in kg.keys:
            if isinstance(item, str):
                if keymap is None:
                    raise ValueError(f"key name {item!r} requires a keymap")
                keys.append(keymap.resolve_name(item))
            else:
                keys.append((int(item[0]), int(item[1])))
    elif kg.type == "row":
        r = kg.index
        if not 0 <= r < rows:
            raise ValueError(f"row index {r} out of range 0..{rows - 1}")
        if keymap and keymap.cells:
            cs = sorted(c for (rr, c) in keymap.cells if rr == r)
            if not cs:
                raise ValueError(f"row {r} has no keys in the keymap")
        else:
            cs = list(range(cols))
        keys = [(r, c) for c in cs]
    elif kg.type == "column":
        c = kg.index
        if not 0 <= c < cols:
            raise ValueError(f"column index {c} out of range 0..{cols - 1}")
        if keymap and keymap.cells:
            rs = sorted(rr for (rr, cc) in keymap.cells if cc == c)
            if not rs:
                raise ValueError(f"column {c} has no keys in the keymap")
        else:
            rs = list(range(rows))
        keys = [(r, c) for r in rs]

    if kg.order in ("right_to_left", "bottom_to_top"):
        keys = list(reversed(keys))
    return keys
