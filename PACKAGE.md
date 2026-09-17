# Packaging razerdash for distribution

How to get razerdash from this working directory onto other machines — from
the two-laptop home fleet up to public release. One constraint shapes every
option here: **`python3-openrazer` is not on PyPI** — it exists only as a
distro package — so no pure-Python packaging route can express the hardest
dependency. That is why pipx installs need `--system-site-packages`, and why a
Debian package is the most natural final form for this project.

## Step 0 (all routes): the git repository

The source of truth is <https://github.com/jamesawebb/razerdash>. Its
`.gitignore` keeps out build output, the pandoc-generated `README.html` /
`INSTALL.html` (regenerate them at release time), the per-machine
`.claude/settings.local.json`, and the root-level `config.yaml` /
`gmail-exporter.yaml`, which are personal snapshots of the live
`~/.config/razerdash/` files -- the shipped examples live under `config/`
and `contrib/`.

`pyproject.toml` carries `authors`, `project.urls` and the licence:
**GPL-3.0-or-later**. The openrazer Python client is GPL-2+ and razerdash
imports it as a library, so a GPL-compatible licence is the safe choice.

## Own fleet: install from git (lightest good option)

Push to GitHub (or a bare repo on cobra) and install straight from git — this
is also the one-command migration for a machine still running an old install:

```sh
pipx install --system-site-packages git+https://github.com/jamesawebb/razerdash
pipx upgrade razerdash        # subsequent updates
```

## Debian machines generally: a .deb (best fit)

A proper Debian package solves exactly what PyPI cannot:

- `Depends: python3-openrazer, python3-yaml, python3-jinja2` — apt pulls the
  hard dependency.
- Installs into the system Python's `dist-packages`, killing the
  `--system-site-packages` footgun and the silent-mock-fallback failure mode
  entirely (see INSTALL.md for what that failure looks like).
- Ships the systemd user unit into `/usr/lib/systemd/user/` and the example
  config under `/usr/share/doc/razerdash/examples/`.

The standard route is a `debian/` directory using `dh-python`/`pybuild`,
built with `dpkg-buildpackage -us -uc`. For an all-Debian household, host the
result in a small apt repo (aptly or reprepro, e.g. on cobra) and every
machine just runs `apt install razerdash` — code arrives via apt like
everything else, while config and keymap stay per-user in
`~/.config/razerdash/`.

## Public release

GitHub with tagged releases is the realistic channel. PyPI works today —
`python3 -m build` then `twine upload dist/*` (the pyproject is ready) — but
its UX is compromised: every user must apt-install openrazer themselves and
know the `--system-site-packages` incantation, which README.md documents.
Publish to PyPI if you want `pipx install razerdash` to work; treat the deb
and the git URL as the first-class paths.

Release checklist either way: bump `version` in `pyproject.toml`, tag the
commit, regenerate the HTML docs if they are shipped.

## Recommended path

1. Git repo + install-from-git now (ten minutes; immediately fixes any
   machine still on an old install).
2. The `.deb` + local apt repo as the polished end state for the fleet.
3. PyPI only if/when razerdash goes public, with the caveats above.
