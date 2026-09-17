"""Template variables available inside a binding's `metric` string.

Lets one config run on both laptops and still show each machine's own stats,
e.g. instance="${host}.lan:9100". Substitution uses string.Template, so any
`$name` / `${name}` not in the context is left untouched (PromQL rarely uses $).
"""
from __future__ import annotations

import socket
import string


def metric_context() -> dict:
    return {
        "host": socket.gethostname().split(".")[0],  # e.g. "quark"
        "fqdn": socket.getfqdn(),                     # e.g. "quark.lan"
    }


def expand(template: str, ctx: dict) -> str:
    return string.Template(template).safe_substitute(ctx)
