"""ProofGate storage: append-only JSONL evidence and verdict ledgers.

Everything stays on the user's own machine, inside the active Hermes home:
  <HERMES_HOME>/proofgate/evidence.jsonl  - one line per observed tool call
  <HERMES_HOME>/proofgate/ledger.jsonl    - one line per checked reply

Records are append-only. Nothing here talks to the network.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List

_LOCK = threading.Lock()
_MAX_FIELD = 800


def hermes_home() -> Path:
    """Resolve the active Hermes home without hardcoding a profile path."""
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        pass
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    return Path.home() / ".hermes"


def data_dir() -> Path:
    path = hermes_home() / "proofgate"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _truncate(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_FIELD:
        return value[:_MAX_FIELD] + "...[truncated]"
    return value


_SENSITIVE_KEYS = ("key", "token", "secret", "password", "auth", "credential")


def sanitize_args(args: Any) -> Any:
    """Shallow-redact argument values whose key looks sensitive."""
    if not isinstance(args, dict):
        return _truncate(str(args)) if args is not None else {}
    clean: Dict[str, Any] = {}
    for key, value in args.items():
        if any(word in str(key).lower() for word in _SENSITIVE_KEYS):
            clean[key] = "[redacted]"
        elif isinstance(value, str):
            clean[key] = _truncate(value)
        else:
            clean[key] = value
    return clean


def append_record(filename: str, record: Dict[str, Any]) -> None:
    line = json.dumps(record, ensure_ascii=False, default=str)
    with _LOCK:
        with open(data_dir() / filename, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def read_recent(filename: str, limit: int = 20) -> List[Dict[str, Any]]:
    path = data_dir() / filename
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows[-limit:]


def append_evidence(record: Dict[str, Any]) -> None:
    append_record("evidence.jsonl", record)


def append_verdict(record: Dict[str, Any]) -> None:
    append_record("ledger.jsonl", record)
