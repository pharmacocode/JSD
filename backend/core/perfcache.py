"""
Request-scoped memoization for the read-heavy cost helpers in `cogs`.

WHY THIS EXISTS (measured against production, October 2026 data):

  GET /deliveries/?month=2026-10   74 s / 262 queries
  GET /dashboard/                  40 s / 144 queries
  GET /clients/                    13 s /  42 queries

The database sits on Supabase, so every individual query costs a network
round trip. The serializers recompute dynamic costs PER ROW, and each
recomputation re-queried the same handful of small tables (SKU requirements,
overhead sums, cases-sold sums, the FIFO queue, print config) — ~13 queries
per delivery row. The fix changes no computation, it just runs each one at
most once per HTTP request.

DESIGN

- The memo lives on a thread-local dict, so one request can never see
  another's values.
- `PerfCacheMiddleware` clears it at the START of every request, so nothing
  is ever served from a previous request (cross-request staleness is
  impossible by construction).
- Within a request, a write must be visible immediately to the response that
  serialises it (e.g. POST /deliveries/ then compute costs for the new row).
  Every model that feeds the memo bumps a GENERATION counter on
  post_save/post_delete (core/signals.py); `cached()` notices the generation
  changed and drops the whole memo. Correctness therefore never depends on
  remembering to invalidate a specific key.

Nothing here mutates data — it only avoids repeating identical queries.
"""

import threading

_local = threading.local()
_lock = threading.Lock()
_generation = 0


def clear_request_cache() -> None:
    """Drop every memoized value for this thread (called per request)."""
    _local.values = {}
    _local.generation = _generation


def invalidate() -> None:
    """
    Bump the generation so ALL threads drop their memos. Wired to
    post_save/post_delete of every model the memo reads from.
    """
    global _generation
    with _lock:
        _generation += 1
        gen = _generation
    _local.values = {}
    _local.generation = gen


def cached(key, compute):
    """
    Return `compute()` memoized under `key` for the rest of this request.

    `key` must be a hashable tuple of primitive ids/values. The caller keys
    anything mutable out of the cached value (return a fresh copy when
    callers may mutate it).
    """
    if getattr(_local, "generation", None) != _generation:
        _local.values = {}
        _local.generation = _generation
    values = _local.values
    if key in values:
        return values[key]
    value = compute()
    values[key] = value
    return value