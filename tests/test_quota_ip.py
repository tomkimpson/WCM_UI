"""Client-IP extraction and pseudonymisation for per-IP quotas.

An unauthenticated per-IP quota keyed on an attacker-controlled value is
decoration, not a quota — so the two tests that matter here are the ones
proving a spoofed leftmost X-Forwarded-For entry is ignored, and that an IPv6
user cannot evade the cap by incrementing the last hextet.
"""
from __future__ import annotations

import pytest

from api import quota

SALT = b"test-salt"


# --- extraction -----------------------------------------------------------

def test_direct_cloud_run_takes_the_rightmost_entry():
    """Cloud Run appends the peer address it observed, so the last hop is ours."""
    assert quota.client_ip("9.9.9.9", trusted_hops=0) == "9.9.9.9"


def test_a_spoofed_leftmost_entry_is_ignored():
    """This is the whole point. xff[0] is whatever the client typed.

    A quota keyed on the first entry can be reset per request by sending a
    fresh fake address, which makes the per-IP ceiling worthless.
    """
    assert quota.client_ip("1.2.3.4, 9.9.9.9", trusted_hops=0) == "9.9.9.9"


def test_many_spoofed_entries_are_still_ignored():
    xff = "1.1.1.1, 2.2.2.2, 3.3.3.3, 9.9.9.9"
    assert quota.client_ip(xff, trusted_hops=0) == "9.9.9.9"


def test_one_trusted_hop_takes_the_second_from_right():
    """Behind a Google ALB the chain is <supplied>, <client>, <lb>."""
    assert quota.client_ip("1.2.3.4, 9.9.9.9, 10.0.0.1", trusted_hops=1) == "9.9.9.9"


def test_whitespace_is_stripped():
    assert quota.client_ip("  1.2.3.4 ,   9.9.9.9  ", trusted_hops=0) == "9.9.9.9"


@pytest.mark.parametrize("header", [None, "", "   ", ",", " , "])
def test_absent_or_empty_headers_yield_none(header):
    assert quota.client_ip(header, trusted_hops=0) is None


def test_more_trusted_hops_than_entries_yields_none():
    """Fail closed rather than silently falling back to a spoofable entry."""
    assert quota.client_ip("9.9.9.9", trusted_hops=3) is None


def test_a_garbage_entry_yields_none():
    assert quota.client_ip("not-an-ip", trusted_hops=0) is None


# --- normalisation --------------------------------------------------------

def test_ipv4_is_not_truncated():
    assert quota.normalise_ip("203.0.113.7") == "203.0.113.7/32"


def test_ipv6_is_truncated_to_a_64_bit_prefix():
    """A subscriber gets at least a /64, often more.

    Counting per address means the cap is evaded by incrementing the last
    hextet — so per-address counting is worth nothing and /64 is the standard
    one-customer granularity for abuse control.
    """
    a = quota.normalise_ip("2001:db8:1:2:aaaa:bbbb:cccc:dddd")
    b = quota.normalise_ip("2001:db8:1:2:eeee:ffff:0:1")
    assert a == b == "2001:db8:1:2::/64"


def test_different_ipv6_64s_stay_distinct():
    a = quota.normalise_ip("2001:db8:1:2::1")
    b = quota.normalise_ip("2001:db8:1:3::1")
    assert a != b


# --- hashing --------------------------------------------------------------

def test_hash_is_short_lowercase_hex():
    h = quota.ip_hash("203.0.113.7", day_key="2026-08-02", salt=SALT)
    assert len(h) == 16
    assert all(c in "0123456789abcdef" for c in h)


def test_the_same_ip_hashes_the_same_within_a_day():
    args = dict(day_key="2026-08-02", salt=SALT)
    assert (quota.ip_hash("203.0.113.7", **args)
            == quota.ip_hash("203.0.113.7", **args))


def test_the_same_ip_hashes_differently_on_a_different_day():
    """Cross-day unlinkability, for free.

    The hash is only useful for the lifetime of the daily counter it serves, so
    binding it to the day means a salt leak permits only same-day correlation.
    """
    assert (quota.ip_hash("203.0.113.7", day_key="2026-08-02", salt=SALT)
            != quota.ip_hash("203.0.113.7", day_key="2026-08-03", salt=SALT))


def test_a_different_salt_changes_the_hash():
    assert (quota.ip_hash("203.0.113.7", day_key="d", salt=b"one")
            != quota.ip_hash("203.0.113.7", day_key="d", salt=b"two"))


def test_ipv6_addresses_in_one_64_share_a_hash():
    args = dict(day_key="2026-08-02", salt=SALT)
    assert (quota.ip_hash("2001:db8:1:2:aaaa::1", **args)
            == quota.ip_hash("2001:db8:1:2:ffff::9", **args))


def test_hashing_uses_hmac_not_a_bare_digest():
    """IPv4 is 2^32 — an unsalted digest is brute-forced in seconds.

    A rainbow table over the whole IPv4 space is trivial, so a bare
    sha256(ip) is not pseudonymisation. Proven by construction: the output
    must not equal the unsalted digest of the normalised form.
    """
    import hashlib
    normalised = quota.normalise_ip("203.0.113.7")
    naive = hashlib.sha256(normalised.encode()).hexdigest()[:16]
    assert quota.ip_hash("203.0.113.7", day_key="d", salt=SALT) != naive


# --- the salt -------------------------------------------------------------

def test_salt_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("IP_HASH_SALT", "from-the-env")
    quota.ip_hash_salt.cache_clear()
    assert quota.ip_hash_salt() == b"from-the-env"


def test_a_local_run_without_a_salt_gets_a_dev_default(monkeypatch):
    monkeypatch.delenv("IP_HASH_SALT", raising=False)
    monkeypatch.delenv("GCP_PROJECT", raising=False)
    quota.ip_hash_salt.cache_clear()
    assert quota.ip_hash_salt()  # non-empty, so hashing still works locally


def test_a_deployed_run_without_a_salt_refuses_to_start(monkeypatch):
    """Failing loudly beats hashing every user under a known constant.

    GCP_PROJECT is set on the Cloud Run service, so its presence is the signal
    that this is a real deployment rather than someone's laptop.
    """
    monkeypatch.delenv("IP_HASH_SALT", raising=False)
    monkeypatch.setenv("GCP_PROJECT", "wcm-ui-dev")
    quota.ip_hash_salt.cache_clear()
    with pytest.raises(RuntimeError, match="IP_HASH_SALT"):
        quota.ip_hash_salt()
