"""Adversarial tests for the HTTP SSRF policy.

Each case is a URL a model could record as an assumption. The policy must refuse the dangerous
ones and allow the ordinary ones, and a refusal must surface as ERROR (uncertainty), never as
FAIL (a contradiction) — a blocked request is not evidence about the assumption.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ledger.model import ERROR  # noqa: E402
from ledger.url_safety import check_url  # noqa: E402
from ledger.verifiers import CheckContext, run_check  # noqa: E402

PUBLIC_V4 = "93.184.216.34"


def _dns(*addresses):
    """A resolver stub returning *addresses* in getaddrinfo shape."""
    def resolve(hostname):
        out = []
        for addr in addresses:
            family = 10 if ":" in addr else 2
            out.append((family, 1, 6, "", (addr, 0, 0, 0) if family == 10 else (addr, 0)))
        return out
    return resolve


def _fails_dns(hostname):
    import socket
    raise socket.gaierror("name resolution failed")


# --- schemes ------------------------------------------------------------------------------------
@pytest.mark.parametrize("url", [
    "ftp://example.com/file",
    "file:///etc/passwd",
    "gopher://example.com/",
    "data:text/plain;base64,SGk=",
    "javascript:alert(1)",
    "ws://example.com/socket",
    "example.com/no-scheme",
    "",
    "   ",
])
def test_non_http_schemes_refused(url):
    allowed, reason = check_url(url)
    assert not allowed, f"{url!r} should be refused"
    assert reason


# --- loopback -----------------------------------------------------------------------------------
@pytest.mark.parametrize("url", [
    "http://localhost/",
    "http://localhost:8080/admin",
    "http://127.0.0.1/",
    "http://127.0.0.1:5432/",
    "http://127.1.2.3/",
    "http://[::1]/",
    "http://[::1]:9200/_cat/indices",
    "http://0.0.0.0/",
])
def test_loopback_refused(url):
    allowed, _ = check_url(url)
    assert not allowed, f"{url!r} should be refused"


def test_localhost_hostname_resolving_to_loopback_refused():
    allowed, _ = check_url("http://evil.example.com/", resolver=_dns("127.0.0.1"))
    assert not allowed


# --- private / RFC1918 / CGNAT ------------------------------------------------------------------
@pytest.mark.parametrize("url", [
    "http://10.0.0.1/",
    "http://10.255.255.254/",
    "http://172.16.0.1/",
    "http://172.31.255.1/",
    "http://192.168.1.1/",
    "http://192.168.0.254/router",
    "http://100.64.0.1/",          # CGNAT, RFC 6598
    "http://[fc00::1]/",           # unique local
    "http://[fd12:3456::1]/",
])
def test_private_addresses_refused(url):
    allowed, _ = check_url(url)
    assert not allowed, f"{url!r} should be refused"


def test_public_name_resolving_to_private_refused():
    """DNS is not a bypass: a public name answered with a private address is still refused."""
    allowed, reason = check_url("http://internal.example.com/", resolver=_dns("10.1.2.3"))
    assert not allowed
    assert "private" in reason or "internal" in reason


# --- link-local ---------------------------------------------------------------------------------
@pytest.mark.parametrize("url", [
    "http://169.254.1.1/",
    "http://[fe80::1]/",
    "http://[fe80::1%eth0]/",
])
def test_link_local_refused(url):
    allowed, _ = check_url(url)
    assert not allowed, f"{url!r} should be refused"


# --- cloud metadata -----------------------------------------------------------------------------
@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",
    "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
    "http://169.254.170.2/v2/credentials",
    "http://169.254.169.253/",
    "http://100.100.100.200/latest/meta-data/",
    "http://metadata.google.internal/computeMetadata/v1/",
    "http://metadata.goog/",
    "http://[fd00:ec2::254]/",
    "http://[::ffff:169.254.169.254]/",
])
def test_cloud_metadata_refused(url):
    allowed, _ = check_url(url)
    assert not allowed, f"{url!r} should be refused"


def test_metadata_stays_blocked_even_with_private_opt_in():
    """allow_private_urls is 'reach my LAN', not 'read my cloud credentials'."""
    for url in ("http://169.254.169.254/latest/meta-data/",
                "http://metadata.google.internal/",
                "http://100.100.100.200/"):
        allowed, reason = check_url(url, allow_private=True)
        assert not allowed, f"{url!r} must stay blocked under opt-in"
        assert "metadata" in reason


# --- public destinations ------------------------------------------------------------------------
@pytest.mark.parametrize("url", [
    "https://example.com/",
    "http://example.com/health",
    "https://api.github.com/repos/x/y",
    "https://example.com:8443/path?q=1",
])
def test_public_http_allowed(url):
    allowed, reason = check_url(url, resolver=_dns(PUBLIC_V4))
    assert allowed, f"{url!r} should be allowed (reason: {reason})"


def test_public_name_resolving_to_public_allowed():
    allowed, _ = check_url("https://deploy.example.com/", resolver=_dns("1.1.1.1", "8.8.8.8"))
    assert allowed


def test_ipv6_public_allowed():
    allowed, _ = check_url("https://[2606:4700::1111]/", resolver=_dns("2606:4700::1111"))
    assert allowed


# --- ambiguity / parsing ------------------------------------------------------------------------
@pytest.mark.parametrize("url", [
    "http://user:pass@example.com/",        # userinfo
    "http://example.com\\@evil.com/",       # backslash
    "http://exa%6dple.com/",                # percent-encoded host
    "http://exa mple.com/",                 # whitespace in host
    "http://example.com:notaport/",         # invalid port
    "http:///nohost",
])
def test_ambiguous_authority_refused(url):
    allowed, reason = check_url(url, resolver=_dns(PUBLIC_V4))
    assert not allowed, f"{url!r} should be refused"
    assert reason


def test_control_character_refused():
    allowed, _ = check_url("http://exam\nple.com/", resolver=_dns(PUBLIC_V4))
    assert not allowed


# --- DNS behaviour ------------------------------------------------------------------------------
def test_dns_failure_fails_closed():
    allowed, reason = check_url("https://unresolvable.example/", resolver=_fails_dns)
    assert not allowed
    assert "dns" in reason


def test_resolver_exception_fails_closed():
    def boom(hostname):
        raise RuntimeError("resolver exploded")

    allowed, _ = check_url("https://example.com/", resolver=boom)
    assert not allowed


def test_unparseable_resolved_address_fails_closed():
    allowed, _ = check_url("https://example.com/", resolver=lambda h: [(2, 1, 6, "", ("not-an-ip", 0))])
    assert not allowed


def test_empty_dns_answer_fails_closed():
    allowed, _ = check_url("https://example.com/", resolver=lambda h: [])
    assert not allowed


def test_every_resolved_answer_is_checked():
    """A name answering with both a public and a private address must be refused."""
    allowed, _ = check_url("https://dual.example.com/", resolver=_dns(PUBLIC_V4, "10.0.0.5"))
    assert not allowed


# --- integration with the verifier ---------------------------------------------------------------
def _ctx(tmp_path, **kw):
    return CheckContext(root=tmp_path, cwd=tmp_path, **kw)


def test_verifier_refuses_metadata_and_returns_error(tmp_path):
    """A policy refusal is uncertainty (ERROR), never a contradiction."""
    ctx = _ctx(tmp_path, http_get=lambda u, t: {"status": 200, "body": "ok"})
    r = run_check(
        {"kind": "http_status", "url": "http://169.254.169.254/latest/meta-data/",
         "expected_status": 200},
        ctx,
    )
    assert r.verdict == ERROR
    assert "policy" in r.detail


def test_verifier_policy_applies_to_injected_transport(tmp_path):
    """The policy lives in the verifier, so an injected getter cannot bypass it."""
    called = []

    def getter(u, t):
        called.append(u)
        return {"status": 200, "body": "ok"}

    ctx = _ctx(tmp_path, http_get=getter)
    r = run_check({"kind": "http_status", "url": "http://localhost:8080/", "expected_status": 200}, ctx)
    assert r.verdict == ERROR
    assert called == [], "the transport must not be reached for a refused URL"


def test_verifier_allows_public_with_resolver(tmp_path):
    ctx = _ctx(tmp_path, http_get=lambda u, t: {"status": 200, "body": "ok"}, url_resolver=_dns(PUBLIC_V4))
    r = run_check({"kind": "http_status", "url": "https://example.com/", "expected_status": 200}, ctx)
    assert r.verdict == "pass"


def test_verifier_private_opt_in_allows_loopback(tmp_path):
    ctx = _ctx(
        tmp_path,
        http_get=lambda u, t: {"status": 200, "body": "ok"},
        allow_private_urls=True,
        url_resolver=_dns("127.0.0.1"),
    )
    r = run_check({"kind": "http_status", "url": "http://localhost:8080/", "expected_status": 200}, ctx)
    assert r.verdict == "pass"


def test_verifier_private_opt_in_still_refuses_metadata(tmp_path):
    ctx = _ctx(
        tmp_path,
        http_get=lambda u, t: {"status": 200, "body": "ok"},
        allow_private_urls=True,
    )
    r = run_check({"kind": "http_status", "url": "http://169.254.169.254/", "expected_status": 200}, ctx)
    assert r.verdict == ERROR


# --- redirects ----------------------------------------------------------------------------------
def test_redirect_to_restricted_destination_is_refused(tmp_path):
    """A redirect is a URL the model did not name; it must be re-validated."""
    ctx = _ctx(tmp_path, url_resolver=_dns(PUBLIC_V4))
    ctx._http_get = None

    class _Resp:
        status = 302
        headers = {"Location": "http://169.254.169.254/latest/meta-data/"}

        def read(self, n):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import ledger.verifiers as V

    class _Opener:
        def open(self, req, timeout=None):
            return _Resp()

    original = V._OPENER
    V._OPENER = _Opener()
    try:
        r = run_check({"kind": "http_status", "url": "https://example.com/", "expected_status": 200}, ctx)
    finally:
        V._OPENER = original
    assert r.verdict == ERROR
    assert "policy" in r.detail


def test_redirect_to_public_destination_is_followed(tmp_path):
    import ledger.verifiers as V

    seen = []

    class _Redirect:
        status = 301
        headers = {"Location": "https://other.example.com/final"}

        def read(self, n):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Final:
        status = 200
        headers = {}

        def read(self, n):
            return b"ok"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Opener:
        def open(self, req, timeout=None):
            seen.append(req.full_url)
            return _Redirect() if len(seen) == 1 else _Final()

    ctx = _ctx(tmp_path, url_resolver=_dns(PUBLIC_V4))
    original = V._OPENER
    V._OPENER = _Opener()
    try:
        r = run_check({"kind": "http_status", "url": "https://example.com/", "expected_status": 200}, ctx)
    finally:
        V._OPENER = original
    assert r.verdict == "pass", r.detail
    assert seen == ["https://example.com/", "https://other.example.com/final"]


def test_redirect_loop_is_error_not_fail(tmp_path):
    import ledger.verifiers as V

    class _Loop:
        status = 302
        headers = {"Location": "https://example.com/again"}

        def read(self, n):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Opener:
        def open(self, req, timeout=None):
            return _Loop()

    ctx = _ctx(tmp_path, url_resolver=_dns(PUBLIC_V4))
    original = V._OPENER
    V._OPENER = _Opener()
    try:
        r = run_check({"kind": "http_status", "url": "https://example.com/", "expected_status": 200}, ctx)
    finally:
        V._OPENER = original
    assert r.verdict == ERROR
