"""Browser-based keymap editor: a grid UI over keymap.yaml, with live key preview."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yaml

from .backend import MockDevice, get_backend
from .daemon import PAUSE_FILE
from .keygroup import save_keymap

WHITE = (255, 255, 255)
FALLBACK_DIMS = (8, 23)

log = logging.getLogger("razerdash")


class _PauseGuard:
    """Hold the daemon pause marker for the server's lifetime.

    Unlike calibrate we must not steal the marker: if another live process
    (e.g. an active `razerdash calibrate`) already owns it, leave it alone and
    never remove it on exit.
    """

    def __init__(self):
        self.owned = False

    def acquire(self):
        if self.owned:
            return
        try:
            with open(PAUSE_FILE, encoding="utf-8") as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            return  # a live owner holds it -- don't touch
        except (OSError, ValueError):
            pass  # absent, stale or unreadable: take it
        try:
            os.makedirs(os.path.dirname(PAUSE_FILE), exist_ok=True)
            with open(PAUSE_FILE, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
            self.owned = True
            time.sleep(1.0)  # give the daemon a moment to notice
        except OSError:
            pass

    def release(self):
        if not self.owned:
            return
        try:
            os.remove(PAUSE_FILE)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# keymap file IO (same shape calibrate writes: {"cells": [...], "names": {...}})
# --------------------------------------------------------------------------- #
def _load_keymap(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {"cells": [], "names": {}}, None
    except yaml.YAMLError as e:
        raise ValueError(f"invalid YAML in {path}: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    try:
        cells = [[int(r), int(c)] for r, c in (data.get("cells") or [])]
        names = {str(k): [int(v[0]), int(v[1])]
                 for k, v in (data.get("names") or {}).items()}
    except (TypeError, ValueError, IndexError, KeyError) as e:
        raise ValueError(f"{path}: malformed cells/names: {e}") from e
    return {"cells": cells, "names": names}, os.stat(path).st_mtime


def _save_keymap(path, cells, names):
    save_keymap(path, cells, names)
    return os.stat(path).st_mtime


# --------------------------------------------------------------------------- #
# request handling
# --------------------------------------------------------------------------- #
def _device(ctx):
    """Return the device, re-probing if it was absent (KVM switch-in)."""
    with ctx["lock"]:
        if ctx["dev"] is None:
            ctx["dev"] = ctx["backend"].find_device(ctx["match"])
            if ctx["dev"] is not None and not isinstance(ctx["dev"], MockDevice):
                ctx["guard"].acquire()
        return ctx["dev"]


def _state(ctx):
    km, mtime = _load_keymap(ctx["path"])
    dev = ctx["dev"]
    if dev is not None:
        rows, cols, device = dev.rows, dev.cols, dev.name
    else:
        rows, cols = FALLBACK_DIMS
        for r, c in km["cells"] + list(km["names"].values()):
            rows, cols = max(rows, r + 1), max(cols, c + 1)
        device = None
    return {"rows": rows, "cols": cols, "device": device,
            "path": ctx["path"], "mtime": mtime, "keymap": km}


def _save(ctx, body):
    try:
        cells = sorted({(int(r), int(c)) for r, c in body.get("cells") or []})
        names = {}
        for k, v in (body.get("names") or {}).items():
            k = str(k).strip()
            if k:
                names[k] = [int(v[0]), int(v[1])]
    except (TypeError, ValueError, IndexError):
        return 400, {"error": "malformed keymap"}
    path = ctx["path"]
    # The conflict check and the write must be one atomic step, or two
    # concurrent saves could both pass the check and silently lose one edit.
    with ctx["lock"]:
        disk_mtime = os.stat(path).st_mtime if os.path.exists(path) else None
        if not body.get("force") and disk_mtime != body.get("mtime"):
            return 409, {"error": "keymap changed on disk since you loaded it"}
        mtime = _save_keymap(path, [list(c) for c in cells], names)
    log.info("webedit: saved %d keys (%d named) to %s", len(cells), len(names), path)
    return 200, {"ok": True, "mtime": mtime, "cells": len(cells), "names": len(names)}


def _light(ctx, body):
    dev = _device(ctx)
    if dev is None:
        return 503, {"error": f"no device matching {ctx['match']!r}"}
    with ctx["lock"]:
        try:
            if body.get("off"):
                dev.clear()
            else:
                dev.draw({(int(body["row"]), int(body["col"])): WHITE}, (0, 0, 0))
        except (KeyError, TypeError, ValueError):
            return 400, {"error": "row/col required"}
        except Exception as e:  # DBus hiccup, device unplugged mid-session...
            ctx["dev"] = None
            return 503, {"error": str(e)}
    return 200, {"ok": True}


class _Handler(BaseHTTPRequestHandler):
    ctx: dict  # injected by run()

    def log_message(self, fmt, *a):
        log.debug("webedit: " + fmt, *a)

    def _send(self, code, body, ctype):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json; charset=utf-8")

    def do_GET(self):
        try:
            if self.path in ("/", "/index.html"):
                self._send(200, _PAGE, "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self._json(200, _state(self.ctx))
            else:
                self._json(404, {"error": "not found"})
        except Exception as e:  # e.g. a malformed keymap.yaml on disk
            log.warning("webedit: GET %s failed: %s", self.path, e)
            self._json(500, {"error": str(e)})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": "bad JSON"})
        try:
            if self.path == "/api/save":
                self._json(*_save(self.ctx, body))
            elif self.path == "/api/light":
                self._json(*_light(self.ctx, body))
            else:
                self._json(404, {"error": "not found"})
        except Exception as e:
            log.warning("webedit: POST %s failed: %s", self.path, e)
            self._json(500, {"error": str(e)})


def run(args) -> int:
    backend = get_backend(args.backend, log)
    dev = backend.find_device(args.match)
    guard = _PauseGuard()
    if dev is not None and not isinstance(dev, MockDevice):
        guard.acquire()
    ctx = {"backend": backend, "match": args.match, "dev": dev,
           "lock": threading.Lock(), "path": args.output, "guard": guard}

    handler = type("Handler", (_Handler,), {"ctx": ctx})
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    except OSError as e:
        print(f"cannot bind 127.0.0.1:{args.port}: {e}")
        guard.release()
        return 2

    print(f"keymap: {args.output}")
    print(f"device: {dev.name if dev else 'none found (light preview off until it appears)'}")
    if guard.owned:
        print("live rendering is paused while the editor runs")
    print(f"open http://127.0.0.1:{args.port}/ in a browser  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        with ctx["lock"]:
            if ctx["dev"] is not None and not isinstance(ctx["dev"], MockDevice):
                try:
                    ctx["dev"].clear()
                except Exception:
                    pass
        guard.release()
        print("\nstopped.")
    return 0


# --------------------------------------------------------------------------- #
# the page (self-contained; talks to /api/state, /api/save, /api/light)
# --------------------------------------------------------------------------- #
_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>razerdash keymap editor</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; background: #121212; color: #ddd;
         font: 14px/1.4 system-ui, sans-serif; }
  header { position: sticky; top: 0; z-index: 1; background: #1a1a1af0;
           padding: 10px 16px; border-bottom: 1px solid #2a2a2a;
           display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  header h1 { font-size: 15px; margin: 0 8px 0 0; font-weight: 600; }
  .sel { font-family: monospace; min-width: 64px; color: #9ecbff; }
  input[type=text] { background: #222; color: #eee; border: 1px solid #444;
                     border-radius: 4px; padding: 5px 8px; width: 120px; }
  button { background: #2b2b2b; color: #ddd; border: 1px solid #4a4a4a;
           border-radius: 4px; padding: 5px 10px; cursor: pointer; }
  button:hover { background: #383838; }
  button:disabled { opacity: .4; cursor: default; }
  button.primary { background: #2456a5; border-color: #3a6fc4; color: #fff; }
  button.primary.dirty { background: #a56a24; border-color: #c48a3a; }
  label.chk { display: flex; gap: 4px; align-items: center;
              font-size: 12px; color: #aaa; }
  #status { font-size: 12px; color: #8fc98f; margin-left: auto; }
  #status.err { color: #f08080; }
  #gridwrap { overflow-x: auto; }
  #grid { display: grid; gap: 3px; padding: 16px; width: max-content; }
  .hdr { color: #666; font: 10px monospace; display: flex;
         align-items: center; justify-content: center; }
  .cell { width: 42px; height: 42px; border-radius: 5px; cursor: pointer;
          background: #1b1b1b; border: 1px solid #2e2e2e;
          display: flex; align-items: center; justify-content: center;
          font: 10px monospace; color: #9db8d4; overflow: hidden;
          user-select: none; }
  .cell:hover { border-color: #777; }
  .cell.key { background: #24303f; border-color: #3b566e; }
  .cell.selected { outline: 2px solid #7ab8ff; outline-offset: 1px; }
  footer { padding: 0 16px 20px; color: #777; font-size: 12px; }
  footer #info { margin-bottom: 4px; color: #999; }
  kbd { background: #2a2a2a; border: 1px solid #444; border-radius: 3px;
        padding: 0 4px; font-size: 11px; }
</style>
</head>
<body>
<header>
  <h1>razerdash keymap</h1>
  <span class="sel" id="selLabel">&ndash;</span>
  <input type="text" id="nameInput" placeholder="key name" disabled
         autocomplete="off" spellcheck="false">
  <button id="setBtn" disabled>Set</button>
  <button id="keyBtn" disabled title="mark as a key without a name">Key only</button>
  <button id="clearBtn" disabled>Clear</button>
  <label class="chk"><input type="checkbox" id="lightChk" checked>
    light selected key</label>
  <button id="saveBtn" class="primary">Save</button>
  <button id="reloadBtn">Reload</button>
  <span id="status"></span>
</header>
<div id="gridwrap"><div id="grid"></div></div>
<footer>
  <div id="info"></div>
  click a cell to select (it lights on the keyboard) &middot;
  <kbd>&larr;&uarr;&darr;&rarr;</kbd> move &middot; type to name &middot;
  <kbd>Enter</kbd> apply + advance &middot; <kbd>Esc</kbd> back to grid &middot;
  <kbd>Del</kbd> clear cell &middot; dim cells = no LED / unmapped
</footer>
<script>
"use strict";
const $ = id => document.getElementById(id);
let S = null;           // server state: rows, cols, device, path, mtime
let cells = new Set();  // "r,c" of cells that are real keys
let names = {};         // name -> [r, c]
let cellEls = {};       // "r,c" -> element
let sel = null;         // [r, c]
let dirty = false;

const key = (r, c) => r + "," + c;

function cellName(r, c) {
  for (const [n, rc] of Object.entries(names))
    if (rc[0] === r && rc[1] === c) return n;
  return null;
}

async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} :
    { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "HTTP " + res.status);
  return data;
}

function setStatus(msg, err) {
  $("status").textContent = msg;
  $("status").className = err ? "err" : "";
}

function hdr(text) {
  const el = document.createElement("div");
  el.className = "hdr";
  el.textContent = text;
  return el;
}

function buildGrid() {
  const grid = $("grid");
  grid.innerHTML = "";
  cellEls = {};
  grid.style.gridTemplateColumns = "26px repeat(" + S.cols + ", 42px)";
  grid.appendChild(hdr(""));
  for (let c = 0; c < S.cols; c++) grid.appendChild(hdr(c));
  for (let r = 0; r < S.rows; r++) {
    grid.appendChild(hdr(r));
    for (let c = 0; c < S.cols; c++) {
      const el = document.createElement("div");
      el.className = "cell";
      el.onclick = () => select(r, c);
      grid.appendChild(el);
      cellEls[key(r, c)] = el;
    }
  }
}

function renderCell(r, c) {
  const el = cellEls[key(r, c)];
  const n = cellName(r, c);
  el.classList.toggle("key", cells.has(key(r, c)));
  el.classList.toggle("selected", !!sel && sel[0] === r && sel[1] === c);
  el.textContent = n || "";
  el.title = "(" + r + ", " + c + ")" + (n ? " = " + n : "");
}

function renderAll() {
  for (let r = 0; r < S.rows; r++)
    for (let c = 0; c < S.cols; c++) renderCell(r, c);
}

function updateInfo() {
  $("info").textContent =
    cells.size + " keys, " + Object.keys(names).length + " named" +
    (dirty ? " (unsaved changes)" : "") + "  \\u2014  " + S.path +
    "  \\u2014  device: " + (S.device || "none");
}

function markDirty() {
  dirty = true;
  $("saveBtn").classList.add("dirty");
  $("saveBtn").textContent = "Save*";
  updateInfo();
}

function select(r, c) {
  const prev = sel;
  sel = [r, c];
  if (prev) renderCell(prev[0], prev[1]);
  renderCell(r, c);
  $("selLabel").textContent = "(" + r + ", " + c + ")";
  $("nameInput").value = cellName(r, c) || "";
  for (const id of ["nameInput", "setBtn", "keyBtn", "clearBtn"])
    $(id).disabled = false;
  cellEls[key(r, c)].scrollIntoView({ block: "nearest", inline: "nearest" });
  if ($("lightChk").checked)
    api("/api/light", { row: r, col: c })
      .catch(e => setStatus("light: " + e.message, true));
}

function advanceSel() {
  let [r, c] = sel;
  if (++c >= S.cols) { c = 0; r++; }
  if (r < S.rows) select(r, c);
}

function applyName(advance) {
  if (!sel) return;
  const [r, c] = sel;
  const v = $("nameInput").value.trim();
  const old = cellName(r, c);
  if (!v && !old) { if (advance) advanceSel(); return; }
  if (old && old !== v) delete names[old];
  if (v) {
    const prev = names[v];
    names[v] = [r, c];
    cells.add(key(r, c));
    if (prev && (prev[0] !== r || prev[1] !== c)) {
      renderCell(prev[0], prev[1]);
      setStatus("moved '" + v + "' here from (" + prev[0] + ", " + prev[1] + ")");
    } else setStatus("named (" + r + ", " + c + ") '" + v + "'");
  } else setStatus("removed name '" + old + "' (still a key)");
  renderCell(r, c);
  markDirty();
  if (advance) advanceSel();
}

function markKey() {
  if (!sel) return;
  cells.add(key(sel[0], sel[1]));
  renderCell(sel[0], sel[1]);
  markDirty();
  setStatus("marked (" + sel[0] + ", " + sel[1] + ") as an unnamed key");
}

function clearCell() {
  if (!sel) return;
  const [r, c] = sel;
  const old = cellName(r, c);
  if (old) delete names[old];
  cells.delete(key(r, c));
  $("nameInput").value = "";
  renderCell(r, c);
  markDirty();
  setStatus("cleared (" + r + ", " + c + ")");
}

async function save(force) {
  const body = {
    cells: [...cells].map(s => s.split(",").map(Number)),
    names: names, mtime: S.mtime, force: !!force,
  };
  try {
    const res = await api("/api/save", body);
    S.mtime = res.mtime;
    dirty = false;
    $("saveBtn").classList.remove("dirty");
    $("saveBtn").textContent = "Save";
    setStatus("saved " + res.cells + " keys (" + res.names + " named)");
    updateInfo();
  } catch (e) {
    if (!force && /changed on disk/.test(e.message) &&
        confirm("The keymap changed on disk (a calibrate run?).\\n" +
                "Overwrite it with this editor's version?"))
      return save(true);
    setStatus("save failed: " + e.message, true);
  }
}

async function load() {
  const st = await api("/api/state");
  S = st;
  cells = new Set(st.keymap.cells.map(rc => key(rc[0], rc[1])));
  names = st.keymap.names;
  dirty = false;
  sel = null;
  $("saveBtn").classList.remove("dirty");
  $("saveBtn").textContent = "Save";
  $("selLabel").textContent = "\\u2013";
  buildGrid();
  renderAll();
  updateInfo();
  setStatus(st.mtime ? "loaded keymap from disk" : "no keymap file yet (blank slate)");
}

$("setBtn").onclick = () => applyName(false);
$("keyBtn").onclick = markKey;
$("clearBtn").onclick = clearCell;
$("saveBtn").onclick = () => save(false);
$("reloadBtn").onclick = () => {
  if (dirty && !confirm("Discard unsaved changes and reload from disk?")) return;
  load().catch(e => setStatus(e.message, true));
};

document.addEventListener("keydown", e => {
  const inp = $("nameInput");
  if (e.target === inp) {
    if (e.key === "Enter") { e.preventDefault(); applyName(true); }
    else if (e.key === "Escape") inp.blur();
    return;
  }
  if (!S) return;
  const moves = { ArrowUp: [-1, 0], ArrowDown: [1, 0],
                  ArrowLeft: [0, -1], ArrowRight: [0, 1] };
  if (e.key in moves) {
    e.preventDefault();
    if (!sel) return select(0, 0);
    const r = Math.min(Math.max(sel[0] + moves[e.key][0], 0), S.rows - 1);
    const c = Math.min(Math.max(sel[1] + moves[e.key][1], 0), S.cols - 1);
    select(r, c);
  } else if (e.key === "Enter" && sel) {
    e.preventDefault();
    inp.focus();
  } else if ((e.key === "Delete" || e.key === "Backspace") && sel) {
    e.preventDefault();
    clearCell();
  } else if (sel && e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
    e.preventDefault();
    inp.focus();
    inp.value = e.key;
  }
});

window.addEventListener("beforeunload", e => { if (dirty) e.preventDefault(); });

load().catch(e => setStatus(e.message, true));
</script>
</body>
</html>
"""
