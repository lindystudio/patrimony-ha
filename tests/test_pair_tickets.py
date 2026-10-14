"""0.4.95: /p/{code} hardening: entropy, single-use, persistence, hash-only, 429."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from custom_components.patrimony_collection import pair_tickets as pt
from custom_components.patrimony_collection.pair import get_ticket, pairing_uri, put_ticket

TOKEN = "llat-SECRET-pairing-token-0495"
URL = "https://ha.example.com"


class _Hass:
    def __init__(self, root: Path):
        self.data = {}
        self.config = type("c", (), {"path": lambda _s, p, root=root: str(root / p),
                                     "external_url": URL, "internal_url": None})()

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


def test_code_entropy_and_urlsafe(tmp_path):
    hass = _Hass(tmp_path)
    codes = {put_ticket(hass, URL, TOKEN) for _ in range(50)}
    assert len(codes) == 50
    for c in codes:
        assert len(c) >= 22  # >=128 bits base64url
        assert set(c) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert pt.PAIR_CODE_BYTES * 8 >= 128


def test_single_use(tmp_path):
    hass = _Hass(tmp_path)
    code = put_ticket(hass, URL, TOKEN)
    assert get_ticket(hass, code) == {"url": URL, "token": TOKEN}
    assert get_ticket(hass, code) is None
    assert pt.claim_ticket(hass, code) is None


def test_survives_restart(tmp_path):
    code = put_ticket(_Hass(tmp_path), URL, TOKEN)
    fresh = _Hass(tmp_path)  # new process: no in-memory state
    assert pt.claim_ticket(fresh, code) == {"url": URL, "token": TOKEN}
    assert pt.claim_ticket(_Hass(tmp_path), code) is None


def test_expired_rejected_and_pruned(tmp_path, monkeypatch):
    hass = _Hass(tmp_path)
    code = put_ticket(hass, URL, TOKEN)
    real = time.time
    monkeypatch.setattr(pt.time, "time", lambda: real() + pt.PAIR_TICKET_TTL_SECONDS + 5)
    assert pt.claim_ticket(hass, code) is None
    assert json.loads(pt.ticket_path(hass).read_text())["tickets"] == {}


def test_hash_only_on_disk(tmp_path):
    hass = _Hass(tmp_path)
    code = put_ticket(hass, URL, TOKEN)
    raw = pt.ticket_path(hass).read_bytes()
    assert code.encode() not in raw
    assert TOKEN.encode() not in raw
    doc = json.loads(raw)
    (tid, row), = doc["tickets"].items()
    assert len(tid) == 64 and tid != code
    assert set(row) == {"box", "createdAt", "exp", "usedAt"}
    assert abs(row["exp"] - row["createdAt"] - 15 * 60) < 1


def test_wrong_code_does_not_burn_ticket(tmp_path):
    hass = _Hass(tmp_path)
    code = put_ticket(hass, URL, TOKEN)
    assert pt.claim_ticket(hass, code[:-1] + ("A" if code[-1] != "A" else "B")) is None
    assert pt.claim_ticket(hass, "") is None
    assert pt.claim_ticket(hass, "x" * 500) is None
    assert pt.claim_ticket(hass, code)["token"] == TOKEN


def test_unreadable_store_not_overwritten(tmp_path):
    hass = _Hass(tmp_path)
    path = pt.ticket_path(hass)
    path.parent.mkdir(parents=True)
    path.write_text("{torn")
    try:
        put_ticket(hass, URL, TOKEN)
        raise AssertionError("expected TicketStoreUnreadable")
    except pt.TicketStoreUnreadable:
        pass
    assert pt.claim_ticket(hass, "anything") is None
    assert path.read_text() == "{torn"


def test_rate_limiter_per_ip_and_global():
    lim = pt.ClaimRateLimiter(per_ip=10, global_limit=60)
    assert all(lim.allow("1.1.1.1", now=0) for _ in range(10))
    assert lim.allow("1.1.1.1", now=1) is False
    assert lim.allow("1.1.1.1", now=61) is True  # window slid
    lim = pt.ClaimRateLimiter(per_ip=10, global_limit=60)
    for i in range(60):
        assert lim.allow(f"10.0.0.{i}", now=0)
    assert lim.allow("9.9.9.9", now=0) is False


class _Resp:
    def __init__(self, text="", status=200, content_type=None, charset=None, headers=None):
        self.text, self.status, self.headers = text, status, headers or {}


def _fake_web(monkeypatch):
    import sys
    import types

    web = types.SimpleNamespace(
        Response=_Resp,
        json_response=lambda data, status=200, headers=None: _Resp(json.dumps(data), status, headers=headers),
    )
    mod = types.ModuleType("aiohttp")
    mod.web = web
    monkeypatch.setitem(sys.modules, "aiohttp", mod)
    monkeypatch.setitem(sys.modules, "aiohttp.web", web)


def _view(tmp_path):
    from custom_components.patrimony_collection.http import PatrimonyPairClaimView
    hass = _Hass(tmp_path)
    return hass, PatrimonyPairClaimView(hass)


class _Req:
    def __init__(self, remote="1.2.3.4", accept="application/json"):
        self.remote = remote
        self.headers = {"Accept": accept}


def test_claim_view_single_use_and_429(tmp_path, monkeypatch):
    _fake_web(monkeypatch)
    hass, view = _view(tmp_path)
    code = put_ticket(hass, URL, TOKEN)
    r1 = asyncio.run(view.get(_Req(), code))
    assert r1.status == 200
    assert json.loads(r1.text)["token"] == TOKEN
    r2 = asyncio.run(view.get(_Req(remote="5.5.5.5"), code))  # other IP: no replay grace
    assert r2.status == 410 and TOKEN not in r2.text
    statuses = [asyncio.run(view.get(_Req(), "nope")).status for _ in range(12)]
    assert 429 in statuses and statuses[-1] == 429


def test_claim_html_deep_link_format_unchanged(tmp_path, monkeypatch):
    _fake_web(monkeypatch)
    hass, view = _view(tmp_path)
    code = put_ticket(hass, URL, TOKEN)
    r = asyncio.run(view.get(_Req(accept="text/html"), code))
    assert r.status == 200
    assert "patrimony://pair?url=" in r.text
    q = parse_qs(urlparse(pairing_uri(URL, TOKEN)).query)
    assert q["url"] == [URL] and q["token"] == [TOKEN] and q["public_url"] == [URL]


def test_box_is_authenticated(tmp_path):
    import base64

    hass = _Hass(tmp_path)
    code = put_ticket(hass, URL, TOKEN)
    doc = json.loads(pt.ticket_path(hass).read_text())
    (tid, row), = doc["tickets"].items()
    assert pt._open(code, tid, row["box"])["token"] == TOKEN
    raw = bytearray(base64.urlsafe_b64decode(row["box"]))
    raw[-1] ^= 1
    assert pt._open(code, tid, base64.urlsafe_b64encode(bytes(raw)).decode()) is None
    assert pt._open(code, "0" * 64, row["box"]) is None


def test_replay_grace_same_ip_within_10s(tmp_path):
    hass = _Hass(tmp_path)
    code = put_ticket(hass, URL, TOKEN)
    first = pt.claim_with_grace(hass, code, "1.2.3.4", now=100.0)
    assert first == {"url": URL, "token": TOKEN}
    assert pt.claim_with_grace(hass, code, "1.2.3.4", now=109.9) == first
    raw = pt.ticket_path(hass).read_bytes()  # grace is memory only
    assert TOKEN.encode() not in raw and code.encode() not in raw


def test_replay_grace_expires_after_10s(tmp_path):
    hass = _Hass(tmp_path)
    code = put_ticket(hass, URL, TOKEN)
    assert pt.claim_with_grace(hass, code, "1.2.3.4", now=100.0)
    assert pt.claim_with_grace(hass, code, "1.2.3.4", now=110.1) is None
    assert pt.claim_with_grace(hass, code, "1.2.3.4", now=111.0) is None


def test_replay_grace_other_ip_gets_nothing(tmp_path):
    hass = _Hass(tmp_path)
    code = put_ticket(hass, URL, TOKEN)
    assert pt.claim_with_grace(hass, code, "1.2.3.4", now=100.0)
    assert pt.claim_with_grace(hass, code, "5.6.7.8", now=101.0) is None
    assert pt.claim_with_grace(hass, code, None, now=101.0) is None
    assert pt.claim_with_grace(_Hass(tmp_path), code, "1.2.3.4", now=101.0) is None  # restart: no grace


def test_claim_view_replay_grace(tmp_path, monkeypatch):
    _fake_web(monkeypatch)
    hass, view = _view(tmp_path)
    code = put_ticket(hass, URL, TOKEN)
    assert asyncio.run(view.get(_Req(remote="1.2.3.4"), code)).status == 200
    again = asyncio.run(view.get(_Req(remote="1.2.3.4"), code))
    assert again.status == 200 and json.loads(again.text)["token"] == TOKEN
    other = asyncio.run(view.get(_Req(remote="9.9.9.9"), code))
    assert other.status == 410 and TOKEN not in other.text
