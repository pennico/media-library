"""Credentials for reading apps (OPDS + KOReader sync).

Reading apps can't do the console's unlock (passkey, cookie session), so they
get their own login: a user name and a generated password (a file, like the
other secrets), never the console password. KOReader sync sends
md5(password) as ``x-auth-key``, so the password must not be one used
elsewhere. Readest also falls back to HTTP Basic; OPDS uses Basic only.
On top of it, requests must come from the owner's Tailscale login, like
console writes.
"""
import base64
import hashlib
import hmac
import threading
import time

MAX_FAILS = 20
FAIL_WINDOW_S = 600


class Reader:
    def __init__(self, user, password_file):
        self.user = user or ""
        self.password_file = password_file
        self._password = None
        self._lock = threading.Lock()
        self._fails = []

    def password(self):
        """Read on use, so a new password file needs no restart."""
        try:
            with open(self.password_file, encoding="utf-8") as fh:
                pw = fh.read().strip()
        except (OSError, TypeError):
            return None
        return pw or None

    def available(self):
        return bool(self.user) and self.password() is not None

    def locked_out(self):
        now = time.time()
        with self._lock:
            self._fails[:] = [t for t in self._fails if now - t < FAIL_WINDOW_S]
            return len(self._fails) >= MAX_FAILS

    def fail(self):
        with self._lock:
            self._fails.append(time.time())

    def _ok(self, user, secret, md5_form):
        pw = self.password()
        if not pw or not self.user or not user or not secret:
            return False
        want = hashlib.md5(pw.encode()).hexdigest() if md5_form else pw
        given = secret.strip().lower() if md5_form else secret
        return hmac.compare_digest(user.encode(), self.user.encode()) & hmac.compare_digest(given.encode(), want.encode())

    def check_basic(self, header):
        if not (header or "").startswith("Basic "):
            return False
        try:
            user, _, pw = base64.b64decode(header[6:].strip()).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return self._ok(user, pw, md5_form=False)

    def check_kosync(self, headers):
        """x-auth-user + x-auth-key (md5 of the password), or HTTP Basic."""
        if headers.get("x-auth-user") is not None:
            return self._ok(headers.get("x-auth-user"), headers.get("x-auth-key") or "", md5_form=True)
        return self.check_basic(headers.get("Authorization"))

    def check_create(self, username, key):
        """POST /users/create with the configured user and password: say yes,
        so an app that 'registers' on first use gets in. Anything else: no."""
        return self._ok(str(username or ""), str(key or ""), md5_form=True)
