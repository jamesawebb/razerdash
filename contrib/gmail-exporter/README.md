# gmail-exporter

Counts Gmail messages matching saved search filters and writes the counts as
Prometheus metrics via node_exporter's textfile collector. razerdash itself
needs **no changes**: the counts flow through Prometheus like any other metric,
so a binding can light a key group from an inbox count.

Uses the Gmail API with OAuth (`gmail.readonly` scope) rather than IMAP + app
password, because app passwords can be blocked by Workspace policy. Note the
narrower `gmail.metadata` scope will not work — it forbids the `q` search
parameter. Counts are exact: the script pages through message ids instead of
trusting `resultSizeEstimate`, which is only an estimate. Stdlib-only apart
from PyYAML (already a razerdash dependency).

## One-time Google Cloud setup

1. Go to <https://console.cloud.google.com/> and create a project (any
   Google account can own it — it does not have to be the mailbox's account).
2. **APIs & Services → Library** → enable **Gmail API**.
3. **APIs & Services → OAuth consent screen**: choose **External**, fill in
   the app name and your email. Then **publish to production** — an app left
   in *Testing* mode expires its refresh tokens after **7 days**, which shows
   up later as `invalid_grant`. Personal use of an "unverified" app is fine;
   the consent page just shows a warning you click through (*Advanced →
   continue*).
4. **APIs & Services → Credentials → Create credentials → OAuth client ID** →
   application type **Desktop app**. Download the JSON to
   `~/.config/razerdash/gmail-client.json`.

### Workspace (corp) accounts

Consent is granted *by the mailbox account*, so a corp mailbox is subject to
corp policy regardless of who owns the Cloud project or which machine runs
the exporter. Admins can block unverified third-party apps from restricted
scopes like `gmail.readonly`; if so, the consent page fails with
`access_denied` / `admin_policy_enforced` and there is no client-side
workaround — ask the admin to allowlist your OAuth client ID.

## Install and authorize

```sh
cd contrib/gmail-exporter/
install -m755 gmail_count.py ~/.local/bin/
cp gmail-exporter.example.yaml ~/.config/razerdash/gmail-exporter.yaml
$EDITOR ~/.config/razerdash/gmail-exporter.yaml   # set your filters

gmail_count.py auth      # opens the consent page; token saved 0600
gmail_count.py print     # sanity check: prints each filter's count
```

`auth` on a remote/headless machine: pass a fixed port and forward it, e.g.
`gmail_count.py auth --port 8377` plus
`ssh -L 8377:127.0.0.1:8377 <host>`, then open the printed URL locally.

## Wiring into Prometheus

The Debian `prometheus-node-exporter` package already scans
`/var/lib/prometheus/node-exporter/*.prom`. That directory is root-owned, so
let your user write to it (survives package upgrades):

```sh
sudo dpkg-statoverride --update --add root "$USER" 2775 /var/lib/prometheus/node-exporter
```

Then run it every minute as a user timer:

```sh
cp gmail-exporter.service gmail-exporter.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now gmail-exporter.timer
```

Quota is a non-issue: `messages.list` costs 5 units per page against a
15,000-unit/user/minute limit — a per-minute timer with a handful of filters
uses well under 1% of it.

## Metrics

| metric | meaning |
|---|---|
| `gmail_messages{filter=NAME}` | exact match count (absent while the filter's query fails — honest, not stale) |
| `gmail_messages_capped{filter=NAME}` | 1 if counting stopped at `max_messages` |
| `gmail_filter_up{filter=NAME}` | 1 if this filter's query succeeded this run |
| `gmail_up` | 1 if authentication itself worked |
| `gmail_last_success_timestamp_seconds` | last fully successful run |

## razerdash binding example

```yaml
- name: gmail-github
  label: unread GitHub mail
  metric: 'gmail_messages{filter="github-unread"}'
  range: { min: 0, max: 10 }
  key_group: { type: keys, keys: [m1, m2, m3, m4, m5] }
  lighting: { method: fill_fixed, color: "#00b0ff" }
```

## Troubleshooting

- **`invalid_grant` on refresh** — token revoked, or the consent screen was
  left in *Testing* mode (7-day expiry). Publish to production, re-run `auth`.
- **No `refresh_token` returned** — Google only re-issues one on fresh
  consent; revoke the app at <https://myaccount.google.com/permissions> and
  re-run `auth`.
- **Consent page refuses with an admin-policy error** — Workspace has blocked
  the app for that mailbox; see the Workspace note above.
- **Counts look low** — check `gmail_messages_capped`; raise `max_messages`.
