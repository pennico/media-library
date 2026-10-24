# Security policy

## Reporting a vulnerability

Please report security issues **privately** through GitHub's private
vulnerability reporting:
[Security tab → "Report a vulnerability"](https://github.com/pennico/media-library/security/advisories/new).
Only the maintainer can see the report.

Please don't open a public issue with security details. If the form is
unavailable, open an issue that only asks for a private channel. You can
expect an acknowledgement within a week. Fixes go to `main` and into a new
tagged release; there are no maintained release branches.

## Security model

The console holds one person's library: reviews, ratings and reading
history. It is built to be reachable only from your own devices.

**Who can see it.** Anyone on your tailnet who can reach the published port
can read the library and its statistics. Don't publish it with Tailscale
Funnel.

**Who can write.** Every write needs all of these:

- the `Tailscale-User-Login` header that Tailscale Serve adds equal to
  `OWNER_LOGIN`;
- a live unlock session (30 minutes), from the scrypt **password** hash in a
  file or a **passkey** (WebAuthn ES256: origin, single-use challenge,
  relying-party hash, user verification, signature and counter checked);
- the session cookie (`__Host-` prefix, HttpOnly, Secure, SameSite=Strict);
- the `X-Console-Action` header, which another site can't send without a
  CORS preflight that the server never answers;
- a same-origin `Origin`.

Five failed unlocks block unlocking for 10 minutes.

**What the app can do.** The app container has a read-only filesystem, no
capabilities and no Docker socket, and runs as the install user. It connects
as `ml_app`, which can read and write the catalog and library but cannot
change the schema, truncate, or edit the audit log. Statistics use
`ml_readonly`. The database has no host port and sits on an internal network
with no route out; only the app has outbound access (Apple Books and Open
Library lookups, cover downloads).

**In the database.** Writes must name their source and client; a trigger
refuses automated changes to values the owner set; every change lands in an
append-only, hash-chained `audit.change` (`audit.verify()`).

**Uploads.** Covers (JPEG/PNG/WebP, 15 MB) must open and verify in Pillow
as one of those formats; EPUBs (500 MB) are checked to be real EPUB containers and stored by
sha256, never by the uploaded name.

**Secrets** are files passed as Docker secrets, never environment values.
No secret is in this repository; CI scans for private patterns.

## In scope

- writing without the owner login, a session, the header or a same-origin
  `Origin`;
- reaching the database or files outside the app's roles and folders;
- getting around the owner protection or the audit chain;
- script injection through library data the page renders (titles, reviews,
  provider metadata);
- unsafe handling of uploaded covers or EPUBs;
- passkey verification flaws.
