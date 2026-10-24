"""Unlock: the agent console's password and passkeys, reused read-only (D27).

Ported from agent-console (console/agent-console.py, MIT, same owner): scrypt
password check, WebAuthn ES256 passkey login (origin, single-use challenge,
relying-party hash, user verification, signature, counter), 30-minute
in-memory sessions, five failures lock unlocking for ten minutes.

Differences: the password hash and the passkey list belong to the agent
console and are only read here (adding or removing passkeys happens there);
the signature counters this console has seen are kept in its own state file,
so a counter can never go backwards across both consoles' views.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time

SESSION_S = 1800
FAIL_WINDOW_S = 600
MAX_FAILS = 5


def b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text):
    text = str(text)
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def cbor(data, i=0):
    """Minimal CBOR decoder for WebAuthn: returns (value, next index)."""
    ib = data[i]
    major, info = ib >> 5, ib & 31
    i += 1
    if info < 24:
        n = info
    elif info in (24, 25, 26, 27):
        size = 1 << (info - 24)
        n = int.from_bytes(data[i:i + size], "big")
        i += size
    else:
        raise ValueError("indefinite CBOR not supported")
    if major == 0:
        return n, i
    if major == 1:
        return -1 - n, i
    if major == 2:
        return data[i:i + n], i + n
    if major == 3:
        return data[i:i + n].decode(), i + n
    if major == 4:
        out = []
        for _ in range(n):
            v, i = cbor(data, i)
            out.append(v)
        return out, i
    if major == 5:
        out = {}
        for _ in range(n):
            k, i = cbor(data, i)
            v, i = cbor(data, i)
            out[k] = v
        return out, i
    if major == 7:
        return {20: False, 21: True, 22: None}.get(info), i
    raise ValueError("unsupported CBOR type")


def hash_password(pw):
    """Same format as agent-console's `passwd` (tests and the dev setup)."""
    salt = secrets.token_bytes(16)
    n, r, p = 2 ** 15, 8, 1
    h = hashlib.scrypt(pw.encode(), salt=salt, n=n, r=r, p=p, maxmem=64 * 1024 * 1024, dklen=32)
    return "scrypt${}${}${}${}${}".format(n, r, p, salt.hex(), h.hex())


class Auth:
    def __init__(self, owner_login, origin, password_file, passkeys_file, state_dir, session_s=SESSION_S):
        self.owner_login = owner_login or ""
        self.origin = origin or ""
        self.rp_id = self.origin.split("://", 1)[-1].split(":")[0].split("/")[0]
        self.password_file = password_file
        self.passkeys_file = passkeys_file
        self.counters_file = os.path.join(state_dir, "passkey-counters.json") if state_dir else None
        self.session_s = session_s
        self._sessions = {}
        self._challenges = {}
        self._fails = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------ owner

    def is_owner(self, login):
        return bool(self.owner_login) and hmac.compare_digest(login or "", self.owner_login)

    # --------------------------------------------------------- sessions

    def start_session(self):
        now = time.time()
        token = secrets.token_urlsafe(32)
        with self._lock:
            for k in [k for k, exp in self._sessions.items() if exp < now]:
                del self._sessions[k]
            self._sessions[token] = now + self.session_s
        return token, now + self.session_s

    def session_until(self, token):
        if not token:
            return None
        with self._lock:
            exp = self._sessions.get(token)
        return exp if exp and exp > time.time() else None

    def end_session(self, token):
        with self._lock:
            self._sessions.pop(token, None)

    def locked_out(self):
        now = time.time()
        with self._lock:
            self._fails[:] = [t for t in self._fails if now - t < FAIL_WINDOW_S]
            return len(self._fails) >= MAX_FAILS

    def fail(self):
        with self._lock:
            self._fails.append(time.time())

    # --------------------------------------------------------- password

    def password_available(self):
        return bool(self.password_file) and os.path.exists(self.password_file)

    def check_password(self, pw):
        try:
            with open(self.password_file, encoding="utf-8") as fh:
                kind, n, r, p, salt, h = fh.read().strip().split("$")
        except (OSError, ValueError, TypeError):
            return False
        if kind != "scrypt" or not isinstance(pw, str) or len(pw) > 1024:
            return False
        got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                             maxmem=64 * 1024 * 1024, dklen=len(h) // 2)
        return hmac.compare_digest(got.hex(), h)

    # --------------------------------------------------------- passkeys

    def passkeys_available(self):
        try:
            import cryptography.hazmat.primitives.asymmetric.ec  # noqa: F401
        except ImportError:
            return False
        return bool(self.rp_id and self.origin and self.passkeys_file)

    def load_passkeys(self):
        try:
            with open(self.passkeys_file, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, list) else []
        except (OSError, ValueError, TypeError):
            return []

    def _counters(self):
        try:
            with open(self.counters_file, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError, TypeError):
            return {}

    def _save_counter(self, cred_id, count):
        if not self.counters_file:
            return
        data = self._counters()
        data[cred_id] = count
        os.makedirs(os.path.dirname(self.counters_file), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.counters_file))
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, self.counters_file)

    def new_challenge(self, purpose="login"):
        now = time.time()
        c = b64u(secrets.token_bytes(32))
        with self._lock:
            for k in [k for k, (_, exp) in self._challenges.items() if exp < now]:
                del self._challenges[k]
            self._challenges[c] = (purpose, now + 120)
        return c

    def login_options(self):
        return {"publicKey": {"challenge": self.new_challenge("login"), "timeout": 60000, "rpId": self.rp_id,
                              "userVerification": "required",
                              "allowCredentials": [{"type": "public-key", "id": k["id"]} for k in self.load_passkeys()]}}

    def _check_client_data(self, client_data_json, kind, purpose):
        cd = json.loads(client_data_json)
        if cd.get("type") != kind:
            raise ValueError("wrong ceremony type")
        with self._lock:
            got = self._challenges.pop(cd.get("challenge", ""), None)   # one use, even when the rest fails
        if not got or got[0] != purpose or got[1] < time.time():
            raise ValueError("unknown or expired challenge")
        if cd.get("origin") != self.origin or cd.get("crossOrigin"):
            raise ValueError("wrong origin")

    def _parse_auth_data(self, ad):
        if len(ad) < 37:
            raise ValueError("short authenticator data")
        if ad[:32] != hashlib.sha256(self.rp_id.encode()).digest():
            raise ValueError("wrong relying party")
        flags, count = ad[32], int.from_bytes(ad[33:37], "big")
        if not flags & 0x01 or not flags & 0x04:
            raise ValueError("user presence and verification (Touch ID / Face ID) are required")
        return count

    def login_passkey(self, req):
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        k = next((k for k in self.load_passkeys() if k.get("id") == req.get("id")), None)
        if not k:
            raise ValueError("unknown passkey (add it in the agent console)")
        client_data = unb64u(req.get("clientDataJSON", ""))
        self._check_client_data(client_data, "webauthn.get", "login")
        ad = unb64u(req.get("authenticatorData", ""))
        count = self._parse_auth_data(ad)
        pub = ec.EllipticCurvePublicNumbers(int(k["x"], 16), int(k["y"], 16), ec.SECP256R1()).public_key()
        try:
            pub.verify(unb64u(req.get("signature", "")), ad + hashlib.sha256(client_data).digest(),
                       ec.ECDSA(hashes.SHA256()))
        except InvalidSignature:
            raise ValueError("bad signature") from None
        seen = max(int(k.get("sign_count") or 0), int(self._counters().get(k["id"], 0)))
        if count and seen and count <= seen:
            raise ValueError("signature counter went backwards (cloned key?)")
        if count:
            self._save_counter(k["id"], count)
        return k
