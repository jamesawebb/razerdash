---
title: Privacy Policy - razerdash gmail-exporter
permalink: /privacy/
---

# Privacy Policy

**gmail-exporter**, part of [razerdash](https://github.com/jamesawebb/razerdash)<br>
Effective 2 November 2026

gmail-exporter is free, open-source software that runs on your own computer.
It counts the Gmail messages that match search queries you choose and
publishes those counts as Prometheus metrics, for example to light keys on a
Razer keyboard. In short: it reads only counts, keeps them on your own
machines, and sends nothing to the author or anyone else.

This policy is published by James Webb, the author of razerdash, who operates
the Google OAuth client named on the consent screen.

## What it accesses

When you authorize gmail-exporter, it asks Google for the `gmail.readonly`
permission. That is the narrowest Gmail permission that allows searching: the
smaller `gmail.metadata` permission does not allow search queries.

With that permission, gmail-exporter makes one kind of request. For each
search query you configure, it lists the matching messages and asks Google to
return only each message's ID. It never requests or reads message bodies,
subjects, senders, recipients, attachments or labels. It counts the IDs in
memory and discards them.

It cannot send, delete or change email.

## What it stores

Everything gmail-exporter stores stays on the computer where you run it:

| Data | Where | Kept for |
|---|---|---|
| Google sign-in tokens (access and refresh token) | a file in your home directory, readable only by your user account | until you revoke access or delete the file |
| Your search queries and the names you give them | your configuration file | until you edit or delete it |
| Each named query's message count, and when it last succeeded | a metrics file on the same computer | overwritten on every run |
| Error messages, such as a failed sign-in | the computer's system log, when run as a background service | your system's log retention |

If you set up Prometheus to collect the metrics, the counts and query names
are also held on your Prometheus server for as long as you configure it to
keep them.

## Who it is shared with

No one. gmail-exporter contacts only Google's sign-in and Gmail API servers,
over HTTPS. It sends nothing to the author or any other party, and contains
no analytics, advertising or tracking. Your Google data is not sold, used for
advertising, read by people, or used to train AI or machine-learning models.

The counts go only where you yourself configure your own monitoring to send
them.

## Google API Services User Data Policy

gmail-exporter's use and transfer to any other app of information received
from Google APIs will adhere to the
[Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
including the Limited Use requirements.

## Removing access and data

- Revoke gmail-exporter's access at any time at
  <https://myaccount.google.com/permissions>.
- Delete the locally stored data by removing the token file (by default
  `~/.config/razerdash/gmail-token.json`) and the metrics file (by default
  `/var/lib/prometheus/node-exporter/gmail.prom`).

## Security

The token file is created readable only by your user account. Sign-in uses
Google's OAuth flow for desktop apps, with PKCE and a redirect that only your
own computer can receive. Every request to Google uses HTTPS.

## Changes

If this policy changes, the new version will be posted at this address with a
new effective date. Its full history is in the
[razerdash repository](https://github.com/jamesawebb/razerdash/commits/main/docs/privacy.md).

## Contact

Open an issue at <https://github.com/jamesawebb/razerdash/issues>.
