#!/usr/bin/env python3
"""Count Gmail messages matching saved filters; write Prometheus textfile metrics.

Companion exporter for razerdash: the daemon needs no changes -- counts arrive
via node_exporter -> Prometheus like any other metric, so a binding can light
a key group from `gmail_messages{filter="..."}`.

Subcommands:
    auth    one-time interactive OAuth consent (opens a browser; see --port)
    run     query every configured filter and (re)write the .prom file
    print   query every configured filter and print the counts to stdout

Uses the Gmail API with the gmail.readonly scope (the narrower gmail.metadata
scope does not allow the `q` search parameter). Counts are exact: the script
pages through message ids rather than trusting `resultSizeEstimate`, which is
documented -- and observed -- to be approximate. Stdlib-only apart from PyYAML,
which razerdash already depends on.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

import yaml

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
API = "https://gmail.googleapis.com/gmail/v1"
PAGE_SIZE = 500  # messages.list hard maximum
DEFAULT_CONFIG = "~/.config/razerdash/gmail-exporter.yaml"


class ExporterError(Exception):
    pass


# ---------------------------------------------------------------- config ----

def load_config(path: str) -> dict:
    path = os.path.expanduser(path)
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except FileNotFoundError:
        raise ExporterError(
            f"config {path} not found -- copy gmail-exporter.example.yaml "
            "there and edit it") from None
    if not isinstance(raw, dict):
        raise ExporterError("config must be a YAML mapping")
    filters = raw.get("filters")
    if not isinstance(filters, dict) or not filters:
        raise ExporterError("config needs a non-empty 'filters:' mapping "
                            "(name -> Gmail search query)")
    for name in filters:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", str(name)):
            raise ExporterError(
                f"filter name {name!r} must use only letters, digits, _ or - "
                "(it becomes a Prometheus label value)")

    def p(key, default):
        return os.path.expanduser(str(raw.get(key, default)))

    return {
        "client_secret": p("client_secret",
                           "~/.config/razerdash/gmail-client.json"),
        "token": p("token", "~/.config/razerdash/gmail-token.json"),
        "output": p("output", "/var/lib/prometheus/node-exporter/gmail.prom"),
        "max_messages": int(raw.get("max_messages", 2000)),
        "filters": {str(k): str(v) for k, v in filters.items()},
    }


# ----------------------------------------------------------------- oauth ----

def load_client(path: str) -> tuple[str, str, str]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise ExporterError(
            f"client secret {path} not found -- create a Desktop-app OAuth "
            "client in the Google Cloud console and download its JSON there "
            "(see README)") from None
    node = data.get("installed") or data.get("web") or {}
    cid = node.get("client_id")
    secret = node.get("client_secret")
    if not cid or not secret:
        raise ExporterError(f"{path} has no installed-app client_id/secret; "
                            "download the JSON for a 'Desktop app' client")
    return cid, secret, node.get("token_uri", "https://oauth2.googleapis.com/token")


def load_token(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise ExporterError(
            f"token {path} not found -- run `gmail_count.py auth` once "
            "to grant access") from None


def save_token(path: str, tok: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(tok, f)


def _post_form(url: str, fields: dict) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise ExporterError(f"{url}: HTTP {e.code}: {body}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ExporterError(f"{url}: {e}") from e


class _CodeHandler(http.server.BaseHTTPRequestHandler):
    """Catches the loopback OAuth redirect; result lands in class attributes."""
    code = None
    error = None

    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        cls = type(self)
        if "code" in params:
            cls.code = params["code"][0]
        elif "error" in params:
            cls.error = params["error"][0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><body>razerdash gmail-exporter: "
                         b"you can close this tab.</body></html>")

    def log_message(self, *args):
        pass


def cmd_auth(cfg: dict, port: int) -> None:
    cid, secret, token_uri = load_client(cfg["client_secret"])
    # PKCE: recommended for installed apps, whose client_secret is not secret.
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    server = http.server.HTTPServer(("127.0.0.1", port), _CodeHandler)
    redirect = f"http://127.0.0.1:{server.server_port}/"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": cid,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",   # else no refresh_token
        "prompt": "consent",        # re-consent also re-issues a refresh_token
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    print("Open this URL in a browser. If this machine is remote, forward the"
          " redirect port first:\n"
          f"  ssh -L {server.server_port}:127.0.0.1:{server.server_port} <this-host>\n\n"
          f"{url}\n")
    webbrowser.open(url)
    while _CodeHandler.code is None and _CodeHandler.error is None:
        server.handle_request()  # one request per call; loops past favicon etc.
    server.server_close()
    if _CodeHandler.error:
        raise ExporterError(
            f"consent failed: {_CodeHandler.error} -- if this is a Workspace "
            "account, an admin policy may block third-party Gmail access")
    tok = _post_form(token_uri, {
        "code": _CodeHandler.code,
        "client_id": cid,
        "client_secret": secret,
        "redirect_uri": redirect,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    })
    if "refresh_token" not in tok:
        raise ExporterError(
            "Google returned no refresh_token; revoke the app at "
            "https://myaccount.google.com/permissions and re-run auth")
    save_token(cfg["token"], {
        "refresh_token": tok["refresh_token"],
        "access_token": tok.get("access_token", ""),
        "expiry": time.time() + float(tok.get("expires_in", 0)),
    })
    print(f"Token saved to {cfg['token']}. Try: gmail_count.py print")


def access_token(cfg: dict, force_refresh: bool = False) -> str:
    tok = load_token(cfg["token"])
    if force_refresh or tok.get("expiry", 0) - 60 < time.time():
        cid, secret, token_uri = load_client(cfg["client_secret"])
        try:
            new = _post_form(token_uri, {
                "client_id": cid,
                "client_secret": secret,
                "refresh_token": tok["refresh_token"],
                "grant_type": "refresh_token",
            })
        except ExporterError as e:
            if "invalid_grant" in str(e):
                raise ExporterError(
                    "refresh token rejected (revoked, or expired -- consent "
                    "screens left in Testing mode expire tokens after 7 days; "
                    "publish the app to production). Re-run `auth`.") from e
            raise
        tok["access_token"] = new["access_token"]
        tok["expiry"] = time.time() + float(new.get("expires_in", 3600))
        save_token(cfg["token"], tok)
    return tok["access_token"]


# ---------------------------------------------------------------- counts ----

def _api_get(path: str, params: dict, bearer: str) -> dict:
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {bearer}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def count_messages(bearer: str, query: str, max_messages: int) -> tuple[int, bool]:
    """Exact count of messages matching `query`, capped at max_messages.
    Returns (count, capped)."""
    count = 0
    page_token = None
    while True:
        params = {
            "q": query,
            "maxResults": min(PAGE_SIZE, max(1, max_messages - count)),
            "fields": "nextPageToken,messages/id",
        }
        if page_token:
            params["pageToken"] = page_token
        data = _api_get("users/me/messages", params, bearer)
        count += len(data.get("messages") or [])
        page_token = data.get("nextPageToken")
        if not page_token:
            return count, False
        if count >= max_messages:
            return count, True


def gather(cfg: dict) -> dict:
    """name -> (count, capped) on success, or Exception on per-filter failure."""
    bearer = access_token(cfg)
    results: dict = {}
    for name, query in cfg["filters"].items():
        try:
            try:
                results[name] = count_messages(bearer, query,
                                               cfg["max_messages"])
            except urllib.error.HTTPError as e:
                if e.code != 401:
                    raise
                bearer = access_token(cfg, force_refresh=True)
                results[name] = count_messages(bearer, query,
                                               cfg["max_messages"])
        except (urllib.error.URLError, OSError, ValueError, ExporterError) as e:
            results[name] = e
    return results


# ---------------------------------------------------------------- output ----

def _lab(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"')


def render_prom(results: dict, authed: bool) -> str:
    """Render the textfile. Failed filters get gmail_filter_up 0 and NO count
    sample -- an absent series is honest; a stale or invented number is not."""
    lines = [
        "# HELP gmail_up 1 if the exporter could authenticate to the Gmail API.",
        "# TYPE gmail_up gauge",
        f"gmail_up {1 if authed else 0}",
        "# HELP gmail_filter_up 1 if this filter's query succeeded this run.",
        "# TYPE gmail_filter_up gauge",
    ]
    for name, res in results.items():
        ok = not isinstance(res, Exception)
        lines.append(f'gmail_filter_up{{filter="{_lab(name)}"}} {1 if ok else 0}')
    lines += [
        "# HELP gmail_messages Messages matching the filter (exact, by paging).",
        "# TYPE gmail_messages gauge",
    ]
    for name, res in results.items():
        if not isinstance(res, Exception):
            lines.append(f'gmail_messages{{filter="{_lab(name)}"}} {res[0]}')
    lines += [
        "# HELP gmail_messages_capped 1 if the count stopped at max_messages.",
        "# TYPE gmail_messages_capped gauge",
    ]
    for name, res in results.items():
        if not isinstance(res, Exception):
            lines.append(
                f'gmail_messages_capped{{filter="{_lab(name)}"}} {1 if res[1] else 0}')
    if authed and all(not isinstance(r, Exception) for r in results.values()):
        lines += [
            "# HELP gmail_last_success_timestamp_seconds Unix time of the last "
            "fully successful run.",
            "# TYPE gmail_last_success_timestamp_seconds gauge",
            f"gmail_last_success_timestamp_seconds {time.time():.0f}",
        ]
    return "\n".join(lines) + "\n"


def write_atomic(path: str, text: str) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"  # same dir: os.replace stays atomic
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def cmd_run(cfg: dict) -> int:
    try:
        results = gather(cfg)
        authed = True
    except ExporterError as e:
        print(f"auth failed: {e}", file=sys.stderr)
        results, authed = {}, False
    write_atomic(cfg["output"], render_prom(results, authed))
    failed = [n for n, r in results.items() if isinstance(r, Exception)]
    for name in failed:
        print(f"[{name}] {results[name]}", file=sys.stderr)
    return 0 if authed and not failed else 1


def cmd_print(cfg: dict) -> int:
    results = gather(cfg)
    status = 0
    for name, res in results.items():
        if isinstance(res, Exception):
            print(f"{name:24s} ERROR: {res}")
            status = 1
        else:
            count, capped = res
            print(f"{name:24s} {count}{' (capped)' if capped else ''}")
    return status


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Export Gmail filter match-counts for Prometheus "
                    "(node_exporter textfile collector).")
    ap.add_argument("--config", default=DEFAULT_CONFIG,
                    help=f"YAML config (default: {DEFAULT_CONFIG})")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_auth = sub.add_parser("auth", help="one-time interactive OAuth consent")
    p_auth.add_argument("--port", type=int, default=0,
                        help="fixed loopback port for the OAuth redirect "
                             "(default: ephemeral; set one to ssh-forward)")
    sub.add_parser("run", help="write counts to the textfile collector")
    sub.add_parser("print", help="print counts to stdout")
    args = ap.parse_args(argv)

    try:
        cfg = load_config(args.config)
        if args.cmd == "auth":
            cmd_auth(cfg, args.port)
            return 0
        if args.cmd == "run":
            return cmd_run(cfg)
        return cmd_print(cfg)
    except ExporterError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
