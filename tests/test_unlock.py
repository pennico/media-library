"""One unlock for the consoles on this host (owner 2026-11-08): a signed cookie that any console reading the same
password file accepts, renewed by use, at most 12 hours. No database needed."""
from medialib.web import auth as auth_mod

OWNER = "owner@example.test"


def _auth(pw_file, tmp_path, session_s=1800):
    return auth_mod.Auth(OWNER, "https://host.test:8459", str(pw_file), str(tmp_path / "pk.json"), str(tmp_path), session_s)


def _pw(tmp_path, text="correct horse battery"):
    p = tmp_path / "pw"
    p.write_text(auth_mod.hash_password(text) + "\n")
    return p


def test_one_unlock_for_every_console_with_the_same_password_file(tmp_path):
    pw = _pw(tmp_path)
    media, finance = _auth(pw, tmp_path), _auth(pw, tmp_path)
    token, until = finance.start_session()
    assert media.session_until(token) == until
    assert media.session_until(token[:-2] + "xx") is None
    first, end, nonce, mac = token.split(".")[1:]
    assert media.session_until("v1.{}.{}.{}.{}".format(first, int(end) + 3600, nonce, mac)) is None
    assert auth_mod.UNLOCK_COOKIE == "__Host-console_unlock"          # the finance console's cookie too


def test_a_new_password_ends_every_unlock(tmp_path):
    pw = _pw(tmp_path)
    a = _auth(pw, tmp_path)
    token, _ = a.start_session()
    _pw(tmp_path, "another one entirely")
    assert a.session_until(token) is None


def test_renewed_by_use_never_past_twelve_hours(tmp_path, monkeypatch):
    a = _auth(_pw(tmp_path), tmp_path, session_s=600)
    t0 = 1_800_000_000.0
    monkeypatch.setattr(auth_mod.time, "time", lambda: t0)
    token, until = a.start_session()
    assert until == t0 + 600 and a.renew(token) is None
    monkeypatch.setattr(auth_mod.time, "time", lambda: t0 + 300)
    token2, until2 = a.renew(token)
    assert until2 == t0 + 900
    monkeypatch.setattr(auth_mod.time, "time", lambda: t0 + auth_mod.MAX_UNLOCK_S - 100)
    capped, cap_until = a.start_session(first=t0)
    assert cap_until == t0 + auth_mod.MAX_UNLOCK_S
    monkeypatch.setattr(auth_mod.time, "time", lambda: t0 + auth_mod.MAX_UNLOCK_S + 1)
    assert a.session_until(capped) is None


def test_lock_refuses_that_unlock_here(tmp_path):
    a = _auth(_pw(tmp_path), tmp_path)
    token, _ = a.start_session()
    a.end_session(token)
    assert a.session_until(token) is None
