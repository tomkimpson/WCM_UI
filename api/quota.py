"""Hard cost ceilings, enforced in the API.

The service is public and has no login. The design doc is explicit that this is
"acceptable only because hard quotas, per-IP rate limits, wall-clock caps, and a
budget kill-switch cap the worst-case cost", so this module is the thing that
makes that sentence true.

Two corrections to the doc's numbers, both from re-deriving the cost model
against Cloud Run Jobs rather than the AWS Batch spot instances it assumed:

  - **Concurrency is not a cost control.** At 4 vCPU + 16 GiB, a job-hour is
    $0.3744, and the one measured run took ~26 minutes ($0.162). A concurrency
    limit of 4 therefore permits ~221 runs/day — about $1,093/month. Bounding
    monthly spend needs a *global daily cap*, which the doc has none of, so
    ``max_runs_per_day`` is added here and is the ceiling that actually matters.
  - **The per-IP cap is fairness, not safety.** IPs are free: a subscriber owns
    at least an IPv6 /64, and cloud VMs and Tor are a card payment away. It
    stops the enthusiastic user from eating the global cap alone, and nothing
    more. Do not let it carry weight it can't bear.

Worst case with the defaults here is roughly $150/month against an expected ~$5.
That ratio is the actual justification for having no login.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
from dataclasses import dataclass
from datetime import datetime, timedelta as _timedelta, timezone as _timezone
from functools import lru_cache
from typing import Optional

from google.cloud import firestore

from worker import db

#: vCPU provisioned on the worker Cloud Run Job (infra/scripts/up.sh). Kept
#: here so the parca_cpus check and the job shape cannot drift silently.
JOB_VCPUS = 4

#: Beyond this, a run is certain to hit the wall-clock cap. Rejecting early
#: turns 45 minutes of billed compute into a 5 ms error.
OBVIOUS_WASTE_LENGTH_SEC = 7200

_COLLECTION_RUNS = "runs"
_COLLECTION_QUOTA = "quota"
_COLLECTION_QUOTA_DAYS = "quota_days"
_CONFIG_DOC = ("config", "global")

#: Only used when neither IP_HASH_SALT nor GCP_PROJECT is set — i.e. a laptop.
#: A deployment without a real salt raises instead; see ip_hash_salt.
_DEV_SALT = b"wcm-ui-local-development-salt-not-for-deployment"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class QuotaError(Exception):
    """Base for every refusal this module can issue."""

    http_status = 429
    code = "quota_exceeded"
    retry_after_sec: Optional[int] = None


class ParamsExceedBudget(QuotaError):
    """The parameters themselves are out of bounds.

    400, not 429: waiting changes nothing, so telling the caller to retry
    would be a lie.
    """

    http_status = 400
    code = "params_exceed_budget"


# ---------------------------------------------------------------------------
# Client IP
# ---------------------------------------------------------------------------

def client_ip(xff_header: Optional[str], *, trusted_hops: int = 0) -> Optional[str]:
    """The client address, read from X-Forwarded-For right to left.

    Cloud Run *appends* the peer address it observed to whatever the client
    sent, so the rightmost entry is the only trustworthy one. ``xff[0]`` is
    entirely attacker-supplied — a per-IP quota keyed on it can be reset on
    every request by sending a fresh fake address, which makes the ceiling
    worthless. ``request.client.host`` is not the client address either.

    ``trusted_hops`` counts proxies that appended their own entry *after* Cloud
    Run: 0 for direct ``*.run.app``, 1 behind a Google external ALB (whose
    chain is ``<supplied>, <client>, <lb>``). Returns None rather than falling
    back to a spoofable entry when the chain is shorter than expected.
    """
    if not xff_header:
        return None
    parts = [p.strip() for p in xff_header.split(",") if p.strip()]
    index = -(1 + trusted_hops)
    if not parts or len(parts) < (1 + trusted_hops):
        return None
    candidate = parts[index]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def normalise_ip(ip: str, *, ipv6_prefix_bits: int = 64) -> str:
    """Reduce an address to the unit we count against.

    IPv4 stays whole. IPv6 is truncated to a /64, because a single subscriber
    is allocated at least that much — counting per address would let anyone
    evade the cap by incrementing the last hextet.

    Residual risk, stated plainly: a /48 holder still commands 65,536 /64s. The
    global daily cap is what protects the bill; this only makes the per-IP cap
    non-trivial.
    """
    addr = ipaddress.ip_address(ip)
    if addr.version == 4:
        return f"{addr}/32"
    network = ipaddress.ip_network(f"{addr}/{ipv6_prefix_bits}", strict=False)
    return str(network)


@lru_cache(maxsize=1)
def ip_hash_salt() -> bytes:
    """The HMAC key, from Secret Manager via the environment.

    Refuses to start a *deployment* without one. Hashing every user under a
    known constant would be pseudonymisation in name only, and a silent
    fallback is exactly the kind of failure nobody notices. GCP_PROJECT is set
    on the Cloud Run service, so its presence distinguishes a deployment from a
    laptop.
    """
    salt = os.environ.get("IP_HASH_SALT")
    if salt:
        return salt.encode("utf-8")
    if os.environ.get("GCP_PROJECT"):
        raise RuntimeError(
            "IP_HASH_SALT is not set. Per-IP quota hashing needs a real salt "
            "in any deployment — mount the wcm-ui-ip-hash-salt secret."
        )
    return _DEV_SALT


def ip_hash(ip: str, *, day_key: str, salt: Optional[bytes] = None,
            ipv6_prefix_bits: int = 64) -> str:
    """A day-scoped pseudonym for an address.

    HMAC rather than a bare digest: IPv4 is only 2^32 addresses, so an unsalted
    hash is reversible by exhaustive search in seconds and is not
    pseudonymisation at all.

    ``day_key`` is part of the message, so a pseudonym is linkable only within
    the UTC day whose counter it serves. That is cross-day unlinkability for
    free, and it means a salt leak permits only same-day correlation.
    """
    key = salt if salt is not None else ip_hash_salt()
    message = f"{day_key}|{normalise_ip(ip, ipv6_prefix_bits=ipv6_prefix_bits)}"
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Ceilings
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        # A typo'd ceiling must not silently fall back to the default — that
        # would look like it worked while leaving the real limit in place.
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Ceilings:
    """The numbers. See the module docstring for how they were derived."""

    #: The one ceiling that bounds monthly spend. 8 x $0.281 = $2.25/day.
    max_runs_per_day: int = 8
    #: Not a cost control. Chosen for latency, and because the lease TTL has to
    #: bound something.
    max_concurrent_runs: int = 2
    #: Fairness only — trivially evaded, free to enforce.
    max_runs_per_ip_per_day: int = 3
    #: 1.7x the single measured run (~26 min), capping per-run cost at $0.281
    #: instead of the doc's 2 hours ($0.749). Enforced via Overrides.timeout.
    wall_clock_cap_sec: int = 2700
    #: Headroom before a lease is considered provably dead.
    lease_grace_sec: int = 900
    #: The doc prices no egress at all, and one tarball download can cost most
    #: of what the run did.
    max_egress_gib_per_day: int = 5
    max_egress_gib_per_ip_per_day: int = 2
    #: 0 = direct *.run.app; 1 = behind a Google external ALB.
    trusted_proxy_hops: int = 0
    ipv6_prefix_bits: int = 64
    #: Staleness is priced, not assumed: 30 s of overshoot is bounded by
    #: max_concurrent_runs, so worst case is 2 runs = $0.56.
    flag_cache_ttl_sec: int = 30

    @property
    def lease_ttl_sec(self) -> int:
        """How long before an unreleased lease may be evicted.

        Sound only because ``Overrides.timeout`` makes the platform kill the
        task at ``wall_clock_cap_sec`` — otherwise this would be a guess and
        the sweep could evict a live run.
        """
        return self.wall_clock_cap_sec + self.lease_grace_sec

    @classmethod
    def from_env(cls) -> "Ceilings":
        return cls(
            max_runs_per_day=_env_int("WCM_MAX_RUNS_PER_DAY", 8),
            max_concurrent_runs=_env_int("WCM_MAX_CONCURRENT_RUNS", 2),
            max_runs_per_ip_per_day=_env_int("WCM_MAX_RUNS_PER_IP_PER_DAY", 3),
            wall_clock_cap_sec=_env_int("WCM_WALL_CLOCK_CAP_SEC", 2700),
            lease_grace_sec=_env_int("WCM_LEASE_GRACE_SEC", 900),
            max_egress_gib_per_day=_env_int("WCM_MAX_EGRESS_GIB_PER_DAY", 5),
            max_egress_gib_per_ip_per_day=_env_int(
                "WCM_MAX_EGRESS_GIB_PER_IP_PER_DAY", 2),
            trusted_proxy_hops=_env_int("WCM_TRUSTED_PROXY_HOPS", 0),
            ipv6_prefix_bits=_env_int("WCM_IPV6_PREFIX_BITS", 64),
            flag_cache_ttl_sec=_env_int("WCM_FLAG_CACHE_TTL_SEC", 30),
        )


def clamp_wall_clock(requested_sec: Optional[int], ceilings: Ceilings) -> int:
    """Bound a user-supplied wall-clock request. Only ever reduces.

    The design doc lists the wall-clock cap as both a user-settable knob and a
    hard ceiling, so it has to be clamped rather than trusted.
    """
    if requested_sec is None:
        return ceilings.wall_clock_cap_sec
    return max(1, min(int(requested_sec), ceilings.wall_clock_cap_sec))


def check_compute_budget(resolved_params: dict, ceilings: Ceilings) -> None:
    """Reject combinations the schema validates but we can't serve.

    JSON Schema checks each knob independently, so the individual maxima
    multiply into runs nobody wants to pay for. Pure and I/O-free by design:
    this runs *before* any reservation, so a doomed request cannot consume a
    quota slot.
    """
    sim = resolved_params.get("simulation", {}) or {}

    generations = int(sim.get("generations", 1))
    init_sims = int(sim.get("init_sims", 1))
    if generations > 1 or init_sims > 1:
        # Grounded, not speculative: postprocess.extract_timeseries raises
        # unless exactly one simOut directory exists, so such a run always
        # fails after paying for the full simulation.
        raise ParamsExceedBudget(
            "multi-generation runs are not supported yet: generations and "
            "init_sims must both be 1 until the timeseries carries a "
            "generation column"
        )

    parca_cpus = int(sim.get("parca_cpus", 1))
    if parca_cpus > JOB_VCPUS:
        raise ParamsExceedBudget(
            f"parca_cpus must be at most {JOB_VCPUS}, the vCPU count "
            f"provisioned for the worker; above that parca oversubscribes and "
            f"runs slower, not faster"
        )

    length_sec = int(sim.get("length_sec", 60))
    if length_sec > OBVIOUS_WASTE_LENGTH_SEC:
        raise ParamsExceedBudget(
            f"length_sec must be at most {OBVIOUS_WASTE_LENGTH_SEC}: a longer "
            f"run is certain to hit the "
            f"{ceilings.wall_clock_cap_sec}s wall-clock cap and be killed"
        )


def _client() -> firestore.Client:
    """Firestore client for the quota documents.

    Delegates to worker.db so the API and the worker cannot end up pointed at
    different databases — the submitter used to build a bare
    ``firestore.Client()`` while the worker honoured GCP_PROJECT and
    FIRESTORE_DATABASE, which is exactly that bug waiting to happen.
    """
    return db._client()


# ---------------------------------------------------------------------------
# Admission control
# ---------------------------------------------------------------------------
#
# The concurrency ceiling is a MAP OF LEASES rather than an integer, and a
# document rather than a count() query. Both choices are deliberate.
#
# Why a document, not count(): a single-document transaction takes an exclusive
# lock on that document, which is exactly the serialization point a
# mutual-exclusion decision needs. Firestore's contention documentation never
# promises range locks, so phantom inserts between a count() and its commit are
# not provably excluded — and aggregation queries are served from index entries,
# which is the wrong primitive for deciding whether to spend money. A useful
# side effect: the quota path needs no composite index at all.
#
# Why a map, not a counter: releasing is `del leases[run_id]`, so it is
# idempotent and a double release is harmless. A MISSED release is visible —
# you can see which run is stuck and for how long — rather than silently
# corrupting an integer nobody can audit. And reconciliation becomes free:
# every reserve() first evicts leases past their expiry, so there is no cron,
# no query and no index.
#
# That eviction is only sound because Overrides.timeout makes the platform kill
# the task at wall_clock_cap_sec. A lease older than cap + grace is therefore
# provably dead, not probably dead. If the cap ever becomes advisory, this
# sweep becomes a guess that can evict a live run.

class AtConcurrencyLimit(QuotaError):
    code = "at_concurrency_limit"

    def __init__(self, message: str, retry_after_sec: Optional[int] = None):
        super().__init__(message)
        self.retry_after_sec = retry_after_sec


class DailyGlobalLimitReached(QuotaError):
    code = "daily_limit_reached"

    def __init__(self, message: str, retry_after_sec: Optional[int] = None):
        super().__init__(message)
        self.retry_after_sec = retry_after_sec


class DailyIpLimitReached(QuotaError):
    code = "daily_ip_limit_reached"

    def __init__(self, message: str, retry_after_sec: Optional[int] = None):
        super().__init__(message)
        self.retry_after_sec = retry_after_sec


class NotAcceptingRuns(QuotaError):
    """The kill switch is on, or we cannot tell whether it is."""

    http_status = 503
    code = "not_accepting_runs"


@dataclass(frozen=True)
class Lease:
    """A reserved concurrency slot. Hold it until the run reaches a terminal
    state, then call finish(); on a failed launch, call release()."""

    run_id: str
    ip_hash: Optional[str]
    day_key: str
    expires_at: "datetime"


_INFLIGHT_DOC = "inflight"
#: Days of retention for the per-day counters. Firestore's TTL deletion lags
#: expiry by up to 24h, so the honest claim is "<= 8 days", not "exactly 7".
_QUOTA_DAY_RETENTION_DAYS = 7

#: The IP bucket used when X-Forwarded-For was absent or unparseable. Grouping
#: them together is deliberate: an unreadable header must not become a way past
#: the per-IP ceiling.
_UNKNOWN_IP = "unknown"


def day_key_for(now: "datetime") -> str:
    """The UTC day a run counts against.

    UTC rather than local time so the boundary is fixed and the cap cannot
    shift under a server timezone change.
    """
    return now.astimezone(_timezone.utc).strftime("%Y-%m-%d")


def _seconds_until_utc_midnight(now: "datetime") -> int:
    utc = now.astimezone(_timezone.utc)
    tomorrow = (utc + _timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((tomorrow - utc).total_seconds()))


def _live_leases(raw: dict, now: "datetime") -> dict:
    """Drop every lease whose expiry has passed. See the note above on why
    this is provably safe rather than merely likely."""
    live = {}
    for run_id, lease in (raw or {}).items():
        expires_at = lease.get("expires_at") if isinstance(lease, dict) else None
        if expires_at is not None and expires_at <= now:
            continue
        live[run_id] = lease
    return live


def reserve(run_id: str, *, ip_hash: Optional[str], now: "datetime",
            ceilings: Ceilings) -> Lease:
    """Claim a slot against all three ceilings, atomically.

    One transaction over two documents, so two simultaneous submits from the
    same address contend on the same exclusive lock and exactly one wins.

    Call this BEFORE launching, and release() if the launch fails. Raises a
    QuotaError subclass; the caller maps `.http_status`, `.code` and
    `.retry_after_sec` onto the response.
    """
    client = _client()
    day = day_key_for(now)
    ip_key = ip_hash or _UNKNOWN_IP

    inflight_ref = client.collection(_COLLECTION_QUOTA).document(_INFLIGHT_DOC)
    day_ref = client.collection(_COLLECTION_QUOTA_DAYS).document(day)
    expires_at = now + _timedelta(seconds=ceilings.lease_ttl_sec)

    def txn(transaction) -> Lease:
        inflight = transaction.get(inflight_ref).to_dict() or {}
        day_doc = transaction.get(day_ref).to_dict() or {}

        leases = _live_leases(inflight.get("leases"), now)

        if len(leases) >= ceilings.max_concurrent_runs:
            soonest = min(
                (l["expires_at"] for l in leases.values()
                 if isinstance(l, dict) and l.get("expires_at")),
                default=expires_at,
            )
            raise AtConcurrencyLimit(
                f"{len(leases)} runs already in flight (limit "
                f"{ceilings.max_concurrent_runs})",
                retry_after_sec=max(1, int((soonest - now).total_seconds())),
            )

        runs_started = int(day_doc.get("runs_started", 0))
        if runs_started >= ceilings.max_runs_per_day:
            raise DailyGlobalLimitReached(
                f"the service has run its {ceilings.max_runs_per_day} "
                f"simulations for today",
                retry_after_sec=_seconds_until_utc_midnight(now),
            )

        by_ip = dict(day_doc.get("by_ip", {}) or {})
        if int(by_ip.get(ip_key, 0)) >= ceilings.max_runs_per_ip_per_day:
            raise DailyIpLimitReached(
                f"you have run your {ceilings.max_runs_per_ip_per_day} "
                f"simulations for today",
                retry_after_sec=_seconds_until_utc_midnight(now),
            )

        leases[run_id] = {"reserved_at": now, "expires_at": expires_at}
        by_ip[ip_key] = int(by_ip.get(ip_key, 0)) + 1

        # Write the whole document back. Both are small, and we read them in
        # this transaction, so a full overwrite cannot lose a concurrent edit.
        transaction.set(inflight_ref, {"leases": leases})
        day_doc.update({
            "runs_started": runs_started + 1,
            "by_ip": by_ip,
            "expire_at": now + _timedelta(days=_QUOTA_DAY_RETENTION_DAYS),
        })
        transaction.set(day_ref, day_doc)

        return Lease(run_id=run_id, ip_hash=ip_hash, day_key=day,
                     expires_at=expires_at)

    return db._atomic(client, txn)


def release(lease: Lease, *, refund_daily: bool, now: "datetime") -> None:
    """Give the slot back after a launch that never became a run.

    ``refund_daily=True`` only when the launch PROVABLY did not start —
    InvalidArgument, PermissionDenied, NotFound, ResourceExhausted. On a
    DeadlineExceeded the server may well have started the job, so keep the
    charge: over-charging annoys one user, under-charging is an unbounded bill.
    """
    client = _client()
    inflight_ref = client.collection(_COLLECTION_QUOTA).document(_INFLIGHT_DOC)
    day_ref = client.collection(_COLLECTION_QUOTA_DAYS).document(lease.day_key)
    ip_key = lease.ip_hash or _UNKNOWN_IP

    def txn(transaction) -> None:
        inflight = transaction.get(inflight_ref).to_dict() or {}
        leases = dict(inflight.get("leases", {}) or {})
        leases.pop(lease.run_id, None)
        transaction.set(inflight_ref, {"leases": leases})

        if not refund_daily:
            return

        day_doc = transaction.get(day_ref).to_dict() or {}
        by_ip = dict(day_doc.get("by_ip", {}) or {})
        # max(0, …) so a double refund cannot manufacture free capacity.
        day_doc["runs_started"] = max(0, int(day_doc.get("runs_started", 0)) - 1)
        by_ip[ip_key] = max(0, int(by_ip.get(ip_key, 0)) - 1)
        day_doc["by_ip"] = by_ip
        transaction.set(day_ref, day_doc)

    db._atomic(client, txn)


def finish(run_id: str, *, now: "datetime") -> None:
    """Free the slot for a run that reached a terminal state.

    Idempotent — it is called from the status read path, which fires on every
    poll. The daily charge stands: the compute was spent.
    """
    client = _client()
    inflight_ref = client.collection(_COLLECTION_QUOTA).document(_INFLIGHT_DOC)

    def txn(transaction) -> None:
        inflight = transaction.get(inflight_ref).to_dict() or {}
        leases = dict(inflight.get("leases", {}) or {})
        if run_id not in leases:
            return
        del leases[run_id]
        transaction.set(inflight_ref, {"leases": leases})

    db._atomic(client, txn)


# ---------------------------------------------------------------------------
# The kill switch
# ---------------------------------------------------------------------------

_flag_cache: dict = {}


def reset_flag_cache() -> None:
    """Drop the cached kill-switch value. For tests and for a manual override."""
    _flag_cache.clear()


def accepting_runs(*, now: "datetime", ceilings: Ceilings) -> tuple[bool, str]:
    """Whether to accept submissions, plus a message for the banner.

    Cached per instance for ``flag_cache_ttl_sec``. The staleness is priced
    rather than hand-waved: 30 s of overshoot is bounded by
    ``max_concurrent_runs``, so the worst case is 2 extra runs — about $0.56.

    Failure handling is deliberately asymmetric:

      - A **missing** document means accepting. It is a provisioning gap, not a
        signal, and failing closed would make a freshly provisioned project
        refuse every run for no visible reason.
      - A **read error with no cached value** means refuse, with 503. If
        Firestore is unreachable then reserve() cannot check the ceilings
        either, so refusing is both correct and more honest than a 500.
      - A read error WITH a cached value serves the stale one. This flag changes
        about once a month; availability beats freshness.
    """
    cached = _flag_cache.get("value")
    cached_at = _flag_cache.get("at")
    if (cached is not None and cached_at is not None
            and (now - cached_at).total_seconds() < ceilings.flag_cache_ttl_sec):
        return cached

    try:
        client = _client()
        snapshot = (client.collection(_CONFIG_DOC[0])
                    .document(_CONFIG_DOC[1]).get())
        data = snapshot.to_dict() if snapshot.exists else None
        if data is None:
            value = (True, "")
        else:
            value = (bool(data.get("accepting_runs", True)),
                     str(data.get("message", "") or ""))
    except Exception:
        if cached is not None:
            return cached
        raise NotAcceptingRuns(
            "cannot determine whether the service is accepting runs"
        )

    _flag_cache["value"] = value
    _flag_cache["at"] = now
    return value
