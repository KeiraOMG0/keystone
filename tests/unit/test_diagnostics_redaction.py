"""Hard-line secret handling in the diagnostics dump: these must NEVER appear."""

from __future__ import annotations

import json

import config
from services import diagnostics_service as ds

# FAKE token-shaped value, joined at runtime so secret scanners never see a literal token.
BOT_TOKEN = ".".join(["MTIzNDU2Nzg5MDEyMzQ1Njc4", "GabcDE", "abcdefghijklmnopqrstuvwxyz0123456789"])
CLIENT_SECRET = "clientsecret_VALUE_zzzz1234"
INTERNAL_SECRET = "internalsecret_VALUE_yyyy5678"
SESSION_SECRET = "sessionsecret_VALUE_xxxx9012"
OTHER_KEY = "some_other_api_key_VALUE_9999"
ALL_SECRETS = [BOT_TOKEN, CLIENT_SECRET, INTERNAL_SECRET, SESSION_SECRET, OTHER_KEY]


def _set_secret_env(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("INTERNAL_API_SECRET", INTERNAL_SECRET)
    monkeypatch.setenv("SESSION_COOKIE_SECRET", SESSION_SECRET)
    monkeypatch.setenv("SOME_THIRDPARTY_API_KEY", OTHER_KEY)
    monkeypatch.setattr(config, "TOKEN", BOT_TOKEN, raising=False)


def _no_leak(obj, extra=()):
    blob = json.dumps(obj)
    for s in [*ALL_SECRETS, *extra]:
        assert s not in blob, f"leaked {s[:12]}..."


# --- cmdline -------------------------------------------------------------

def test_cmdline_masks_token_flags_and_env_like_args():
    out = ds.redact_cmdline(
        [
            "node", "server.js", "--token=abc123secret", "--token", "supersecretvalue",
            "--client-secret", "shh", "DISCORD_TOKEN=" + BOT_TOKEN, "API_KEY=k123456", "--port", "4416",
        ]
    )
    joined = " ".join(out)
    for leaked in ("abc123secret", "supersecretvalue", "shh", BOT_TOKEN, "k123456"):
        assert leaked not in joined
    assert "--port" in out and "4416" in out  # non-secret values survive
    assert out[0] == "node"


def test_cmdline_masks_token_shapes_and_opaque_blobs():
    out = ds.redact_cmdline([BOT_TOKEN, "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijk", "a" * 20 + "1" * 20])
    assert all(x == ds.REDACTED for x in out)


def test_cmdline_shows_full_paths_including_cookie_file():
    p = r"C:\Users\Keira\Desktop\python\kick-bot\cookies.txt"
    out = ds.redact_cmdline(["yt-dlp", "--cookies", p, r"C:\node\node.exe"])
    assert p in out and r"C:\node\node.exe" in out  # staff-only page: paths are shown


# --- free text -----------------------------------------------------------

def test_redact_text_variants(monkeypatch):
    _set_secret_env(monkeypatch)
    text = (
        f"login with {BOT_TOKEN} ok; Authorization: Bearer abcdefghijklmnop1234; "
        "Cookie: SID=verysecretcookievalue\n"
        "token=hunter2hunter2; password: 'p@ss word'; "
        f"https://user:pw123@example.com/x; secret {CLIENT_SECRET} {INTERNAL_SECRET} {SESSION_SECRET} {OTHER_KEY}"
    )
    out = ds.redact_text(text)
    for leaked in (*ALL_SECRETS, "abcdefghijklmnop1234", "verysecretcookievalue", "hunter2hunter2", "p@ss word", "pw123"):
        assert leaked not in out
    assert "example.com" in out  # host is kept


# --- config --------------------------------------------------------------

def test_config_report_hides_secrets_but_shows_real_values(monkeypatch):
    _set_secret_env(monkeypatch)
    env = {
        "DISCORD_TOKEN": BOT_TOKEN, "DISCORD_CLIENT_SECRET": CLIENT_SECRET,
        "INTERNAL_API_SECRET": INTERNAL_SECRET, "SESSION_COOKIE_SECRET": SESSION_SECRET,
        "INTERNAL_API_PORT": "8766", "DEBUG": "true", "MUSIC_COOKIES_FILE": r"C:\bot\cookies.txt",
        "LOG_LEVEL": "INFO", "CHECK_INTERVAL_MINUTES": "10", "MUSIC_DEFAULT_SEARCH": "ytsearch7",
        "SOME_THIRDPARTY_API_KEY": OTHER_KEY,
    }
    rep = ds.config_report(env)
    for name in ("DISCORD_TOKEN", "DISCORD_CLIENT_SECRET", "INTERNAL_API_SECRET", "SESSION_COOKIE_SECRET"):
        assert rep[name] == "set"
    assert rep["DISCORD_CLIENT_ID"] == "unset"
    assert rep["INTERNAL_API_PORT"] == 8766 and rep["DEBUG"] is True
    assert rep["CHECK_INTERVAL_MINUTES"] == 10
    assert rep["LOG_LEVEL"] == "INFO" and rep["MUSIC_DEFAULT_SEARCH"] == "ytsearch7"
    assert rep["MUSIC_COOKIES_FILE"] == r"C:\bot\cookies.txt"  # non-secret path shown
    assert "SOME_THIRDPARTY_API_KEY" not in rep  # not in the allowlist -> not emitted
    _no_leak(rep)


def test_scrub_payload_nested(monkeypatch):
    _set_secret_env(monkeypatch)
    payload = {"a": [{"b": f"x {BOT_TOKEN} y"}, INTERNAL_SECRET], "c": {"d": ("t", SESSION_SECRET)}, "n": 5}
    out = ds.scrub_payload(payload)
    _no_leak(out)
    assert out["n"] == 5


# --- cookies -------------------------------------------------------------

COOKIES = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t4102444800\t__Secure-3PSID\tRAWVALUE_sid_abcdefghijklmnopqrstuvwxyz\n"
    "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1000\tLOGIN_INFO\tRAWVALUE_login_0123456789\n"
    ".google.com\tTRUE\t/\tFALSE\t0\tNID\tRAWVALUE_nid\n"
)


def test_cookie_parse_details_but_never_raw_values():
    res = ds.parse_cookie_file(COOKIES, now=2_000_000_000)
    blob = json.dumps(res)
    assert "RAWVALUE" not in blob and "abcdefghijklmnopqrstuvwxyz" not in blob
    assert res["cookie_count"] == 3 and res["domain_count"] == 2
    assert res["per_domain"] == {"youtube.com": 2, "google.com": 1}
    by = {c["name"]: c for c in res["cookies"]}
    sid = by["__Secure-3PSID"]
    assert sid["domain"] == ".youtube.com" and sid["path"] == "/" and sid["secure"] is True
    assert sid["value_len"] == len("RAWVALUE_sid_abcdefghijklmnopqrstuvwxyz")
    assert sid["value_preview"].startswith("RA\u2026(len ") and "RAWVALUE" not in sid["value_preview"]
    assert by["LOGIN_INFO"]["http_only"] is True and by["LOGIN_INFO"]["expired"] is True
    assert by["NID"]["session"] is True and by["NID"]["expired"] is False
    assert res["key_auth_expired"] is True  # LOGIN_INFO expired
    assert res["expired_count"] == 1
    assert res["earliest_expiry_cookie"].startswith("LOGIN_INFO")
    assert res["latest_expiry_cookie"].startswith("__Secure-3PSID")


def test_mask_value_short_values_show_length_only():
    assert ds.mask_value("abcd") == "\u2026(len 4)"
    assert ds.mask_value("abcdef") == "ab\u2026(len 6)"
