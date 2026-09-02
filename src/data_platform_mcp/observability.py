"""Logging and the tool-call audit trail.

Two separate channels, because they answer different questions:

* **stderr logging** is for a human debugging a session right now. Under stdio
  transport stdout carries the JSON-RPC stream, so a stray ``print`` corrupts
  the protocol and drops the connection -- every log line goes to stderr and
  nothing in this package may write to stdout.
* **the audit log** is a durable JSONL record of every tool call: which tool,
  how long, how much it scanned, and what failed. A server that runs arbitrary
  SQL against company data needs a history of what it was asked; the client
  keeps no per-server log file, so without this there is none.

**SQL is never written to the audit log.** A WHERE clause routinely carries the
user ids, emails or device tokens the query is about, and an audit file that
copies them is a second uncontrolled home for that data. What gets recorded is
a fingerprint -- a truncated SHA-256 and a character count -- which is enough to
recognise the same query recurring, correlate it with a BigQuery job, or spot a
loop, without reproducing its contents.

Set ``BQ_MCP_AUDIT_LOG=off`` to disable the file, or to a path to move it.
``BQ_MCP_LOG_LEVEL`` controls stderr verbosity.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .errors import explain_exception

logger = logging.getLogger("data_platform_mcp")

_DEFAULT_AUDIT = Path.home() / ".local" / "state" / "data-platform-mcp" / "audit.jsonl"

# Arguments whose values are echoed into the audit log verbatim. These name
# structure -- which dataset, how many rows -- and carry no query content.
_SAFE_ARGS = frozenset(
    {
        "dataset_id",
        "table_id",
        "max_rows",
        "confirm_expensive",
        "environment",
    }
)

# Arguments recorded as a fingerprint rather than a value. See the module
# docstring: SQL text is the one argument that reliably contains user data.
_FINGERPRINTED_ARGS = frozenset({"sql"})

# Result fields worth lifting into the audit record, so a run can be costed and
# a truncated response spotted without re-reading the payload.
_AUDIT_RESULT_FIELDS = (
    "count",
    "row_count",
    "truncated",
    "stopped_for_size",
    "status",
    "estimated_bytes",
    "bytes_processed",
    "cache_hit",
)


def configure_logging() -> None:
    """Send package logs to stderr. Never stdout: that is the protocol channel."""
    if logger.handlers:
        return
    level = os.environ.get("BQ_MCP_LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s data-platform-mcp %(message)s")
    )
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, level, logging.INFO))
    logger.propagate = False


def _audit_path() -> Path | None:
    raw = os.environ.get("BQ_MCP_AUDIT_LOG", "").strip()
    if raw.lower() in {"off", "0", "false", "none"}:
        return None
    return Path(raw).expanduser() if raw else _DEFAULT_AUDIT


def _write_audit(record: dict) -> None:
    path = _audit_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError as exc:
        # Never let auditing break a tool call; a warning on stderr is enough.
        logger.warning("could not write audit record: %s", exc)


def fingerprint(text: str) -> dict:
    """Identify a SQL string without reproducing it."""
    normalized = " ".join(str(text).split())
    return {
        "sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12],
        "chars": len(text or ""),
    }


def _safe_arguments(kwargs: dict) -> dict:
    recorded: dict[str, Any] = {}
    for key, value in kwargs.items():
        if value in ("", None):
            continue
        if key in _FINGERPRINTED_ARGS:
            recorded[key] = fingerprint(value)
        elif key in _SAFE_ARGS:
            recorded[key] = value
        else:
            recorded[key] = "<redacted>"
    return recorded


def _response_size(result: Any) -> int:
    if isinstance(result, str):
        return len(result)
    try:
        return len(json.dumps(result, default=str))
    except (TypeError, ValueError):
        return -1


def _result_fields(result: Any) -> dict:
    """Pull a few costing fields out of a result, whatever shape it arrives in.

    Tools currently return JSON text; they return dicts after the response
    contract changes. Handling both keeps the audit trail continuous across
    that change instead of going quiet for a release.
    """
    payload = result
    if isinstance(result, str):
        try:
            payload = json.loads(result)
        except (TypeError, ValueError):
            return {}
    if not isinstance(payload, dict):
        return {}
    return {f: payload[f] for f in _AUDIT_RESULT_FIELDS if f in payload}


def _start_record(fn: Callable, kwargs: dict) -> dict:
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "tool": fn.__name__,
        "arguments": _safe_arguments(kwargs),
    }


def _finish_ok(record: dict, fn: Callable, result: Any, started: float) -> None:
    record["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
    record.update(_result_fields(result))
    record["bytes"] = _response_size(result)
    record["error"] = None
    _write_audit(record)
    logger.info(
        "%s ok %sms %sB", fn.__name__, record["duration_ms"], record["bytes"]
    )


def _finish_error(record: dict, fn: Callable, exc: Exception, started: float) -> Exception:
    record["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
    record["error"] = type(exc).__name__
    record["error_message"] = str(exc)[:500]
    _write_audit(record)
    logger.error("%s failed: %s: %s", fn.__name__, type(exc).__name__, exc)
    # Replace opaque credential failures with something the agent can act on;
    # anything else propagates unchanged.
    return explain_exception(exc)


def instrument(fn: Callable) -> Callable:
    """Time, size and record one tool call, then re-raise anything it threw.

    ``functools.wraps`` keeps ``__doc__``, ``__annotations__`` and the signature
    intact, which is what FastMCP introspects to build the tool schema -- the
    wrapper must stay invisible to the protocol. Async tools are wrapped as
    async, so a coroutine is never recorded as a finished call.
    """
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args, **kwargs):
            started = time.perf_counter()
            record = _start_record(fn, kwargs)
            try:
                result = await fn(*args, **kwargs)
            except Exception as exc:
                raise _finish_error(record, fn, exc, started) from exc
            _finish_ok(record, fn, result, started)
            return result

        return async_wrapper

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        started = time.perf_counter()
        record = _start_record(fn, kwargs)
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            raise _finish_error(record, fn, exc, started) from exc
        _finish_ok(record, fn, result, started)
        return result

    return wrapper
