"""Regression tests for Jinja2-templated config loading."""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from razerdash.config import ConfigError, ConfigWatcher, load_config, render_template
from razerdash.context import metric_context

TMP = tempfile.mkdtemp(prefix="razerdash-jinja-")
HOST = metric_context()["host"]

BINDING = """
  - name: {name}
    metric: 'up'
    key_group: {{ type: row, index: {row} }}
    lighting: {{ method: fill_fixed, color: "#00e5ff" }}
"""


def _write(name, text):
    p = os.path.join(TMP, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


def _cfg(body):
    return _write("config.yaml", body)


# 1. the shipped example config (jinja snippets in comments and all) loads
load_config(os.path.join(ROOT, "config", "config.example.yaml"))
print("1. example config loads through the template pass: OK")

# 2. env with default: unset -> default, set -> value
tpl = ("prometheus:\n  url: {{ env.get('RD_TEST_URL', 'http://cobra:9090') }}\n"
       "bindings:" + BINDING.format(name="a", row=0))
os.environ.pop("RD_TEST_URL", None)
assert load_config(_cfg(tpl)).prometheus.url == "http://cobra:9090"
os.environ["RD_TEST_URL"] = "http://other:9999"
assert load_config(_cfg(tpl)).prometheus.url == "http://other:9999"
del os.environ["RD_TEST_URL"]
print("2. env.get with default: OK")

# 3. host conditional includes/excludes a binding
tpl = ("bindings:" + BINDING.format(name="always", row=0) +
       "{% if host == '" + HOST + "' %}" + BINDING.format(name="mine", row=1) +
       "{% endif %}\n"
       "{% if host == 'not-a-real-host' %}" + BINDING.format(name="never", row=2) +
       "{% endif %}\n")
names = [b.name for b in load_config(_cfg(tpl)).bindings]
assert names == ["always", "mine"], names
print("3. host-conditional bindings: OK")

# 4. undefined variable -> ConfigError (StrictUndefined)
try:
    load_config(_cfg("bindings:" + BINDING.format(name="x", row=0) + "# {{ hots }}\n"))
    raise AssertionError("undefined variable accepted")
except ConfigError as e:
    assert "template error" in str(e), e
print("4. undefined variable -> ConfigError: OK")

# 5. template syntax error -> ConfigError
try:
    load_config(_cfg("{% if host %}\nbindings: []\n"))
    raise AssertionError("unclosed block accepted")
except ConfigError as e:
    assert "template error" in str(e), e
print("5. syntax error -> ConfigError: OK")

# 6. hot reload with a broken template keeps the last good config
good = "bindings:" + BINDING.format(name="good", row=0)
p = _cfg(good)
w = ConfigWatcher(p)
w.load()
with open(p, "w") as f:
    f.write(good + "# {{ nope }}\n")
os.utime(p, (0, 0))  # force an mtime change
cfg = w.maybe_reload()
assert w.error and "template error" in w.error, w.error
assert cfg.bindings[0].name == "good"
print("6. broken template on reload -> last good kept, error set: OK")

# 7. query-time ${host} tokens survive rendering untouched
assert render_template("m: 'x{a=\"${host}.lan\"}'") == "m: 'x{a=\"${host}.lan\"}'"
print("7. ${host} passes through: OK")

print("\nall jinja regressions passed")
