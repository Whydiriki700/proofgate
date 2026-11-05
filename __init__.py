"""ProofGate - no proof, no "done".

A Hermes plugin that verifies an agent's completion claims against the tool
evidence recorded in the same session before the user reads the final reply.

How it works:
  1. `post_tool_call` silently records every tool call as evidence.
  2. `transform_llm_output` holds the finished reply, extracts factual
     completion claims (file created, message sent, command ran, figures
     quoted) and matches each claim against the recorded evidence.
  3. Verdicts are fail closed: PASS (all proven), DOWNGRADE (some claims
     flagged unverified in the reply), BLOCK (reply withheld when its claims
     are unproven or contradicted by a failed tool attempt). Every checked
     reply is written to an append-only local ledger.

Modes (config: plugins.entries.proofgate.settings.mode):
  - enforce (default): replies are rewritten or withheld per verdict
  - warn: verdicts are recorded in the ledger, replies are never changed
  - off: plugin observes nothing and changes nothing
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional

try:
    from . import checker, ledger
except ImportError:  # loaded as a top-level module in tests
    import checker  # type: ignore
    import ledger  # type: ignore

logger = logging.getLogger(__name__)

_lock = threading.Lock()
# session_id -> list of evidence records for that session (in memory mirror of
# the evidence ledger; the transform hook reads from here for speed).
_EVIDENCE: Dict[str, List[Dict[str, Any]]] = {}
_MAX_SESSIONS = 64

_CTX: Any = None


def _mode() -> str:
    if _CTX is not None:
        try:
            mode = _CTX.get_config("mode", "enforce")
            if isinstance(mode, str) and mode.lower() in {"enforce", "warn", "off"}:
                return mode.lower()
        except Exception:
            pass
    return "enforce"


def _session_evidence(session_id: str, turn_id: Optional[str]) -> List[Dict[str, Any]]:
    with _lock:
        records = list(_EVIDENCE.get(session_id or "", []))
    if turn_id:
        same_turn = [r for r in records if r.get("turn_id") == turn_id]
        if same_turn:
            return same_turn
    return records


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


def on_post_tool_call(tool_name: str, args: Any = None, result: Any = None, **kwargs: Any) -> None:
    if _mode() == "off":
        return
    session_id = str(kwargs.get("session_id") or "")
    record = {
        "ts": time.time(),
        "session_id": session_id,
        "turn_id": kwargs.get("turn_id"),
        "tool_name": tool_name,
        "args": ledger.sanitize_args(args),
        "result": ledger._truncate(result if isinstance(result, str) else str(result)),
        "status": kwargs.get("status"),
        "duration_ms": kwargs.get("duration_ms"),
    }
    with _lock:
        bucket = _EVIDENCE.setdefault(session_id, [])
        bucket.append(record)
        if len(_EVIDENCE) > _MAX_SESSIONS:
            _EVIDENCE.pop(next(iter(_EVIDENCE)), None)
    try:
        ledger.append_evidence(record)
    except Exception:
        logger.debug("ProofGate: evidence append failed", exc_info=True)


def on_pre_llm_call(is_first_turn: bool = False, **kwargs: Any) -> Optional[Dict[str, str]]:
    if not is_first_turn or _mode() == "off":
        return None
    return {
        "context": (
            "ProofGate is active in this profile. Before you claim any action "
            "is complete (a file created, a message sent, a command run, a "
            "figure quoted), make sure it was actually performed with a tool "
            "in this session. Your final reply will be checked against tool "
            "evidence; unproven claims are flagged or withheld and recorded "
            "in the ProofGate ledger."
        )
    }


def on_transform_llm_output(response_text: str = "", **kwargs: Any) -> Optional[str]:
    mode = _mode()
    if mode == "off" or not response_text:
        return None
    session_id = str(kwargs.get("session_id") or "")
    turn_id = kwargs.get("turn_id")
    evidence = _session_evidence(session_id, turn_id)
    try:
        result = checker.check_reply(response_text, evidence)
    except Exception:
        # Fail closed: a broken checker must never silently PASS a reply.
        logger.warning("ProofGate: checker failed", exc_info=True)
        try:
            ledger.append_verdict(
                {
                    "ts": time.time(),
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "verdict": "ERROR_DOWNGRADE",
                    "claims": [],
                    "unproven": [],
                }
            )
        except Exception:
            pass
        if mode == "warn":
            return None
        return (
            response_text
            + "\n\n[ProofGate: verification failed internally, so treat every "
            "completion claim in this reply as unverified.]"
        )
    try:
        ledger.append_verdict(
            {
                "ts": time.time(),
                "session_id": session_id,
                "turn_id": turn_id,
                "verdict": result["verdict"],
                "claims": result["claims"],
                "unproven": result["unproven"],
            }
        )
    except Exception:
        logger.debug("ProofGate: verdict append failed", exc_info=True)
    if mode == "warn":
        return None
    return checker.apply_verdict(response_text, result)


# ---------------------------------------------------------------------------
# User surfaces: /proof command, proof_status tool, hermes proofgate CLI
# ---------------------------------------------------------------------------


def _format_ledger(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "ProofGate ledger is empty. No reply has been checked yet."
    counts: Dict[str, int] = {}
    for row in rows:
        counts[row.get("verdict", "?")] = counts.get(row.get("verdict", "?"), 0) + 1
    lines = ["ProofGate ledger (most recent last):"]
    lines.append("Counts: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for row in rows[-5:]:
        lines.append(
            f"- {row.get('verdict')} | claims={len(row.get('claims') or [])} "
            f"unproven={len(row.get('unproven') or [])} | session={row.get('session_id') or '-'}"
        )
    return "\n".join(lines)


def proof_command(raw_args: str = "", **kwargs: Any) -> str:
    return _format_ledger(ledger.read_recent("ledger.jsonl", limit=100))


def proof_status_tool(params: Dict[str, Any] | None = None, **kwargs: Any) -> str:
    rows = ledger.read_recent("ledger.jsonl", limit=100)
    evidence_rows = ledger.read_recent("evidence.jsonl", limit=1000)
    return json.dumps(
        {
            "success": True,
            "checked_replies": len(rows),
            "verdicts": rows[-5:],
            "evidence_records_recent": len(evidence_rows),
            "note": "Full history lives in <HERMES_HOME>/proofgate/ledger.jsonl",
        }
    )


def _cli_setup(subparser: Any) -> None:
    sub = subparser.add_subparsers(dest="proofgate_action")
    sub.add_parser("ledger", help="Show recent ProofGate verdicts")
    sub.add_parser("stats", help="Show ProofGate verdict counts")


def _cli_handler(args: Any) -> int:
    rows = ledger.read_recent("ledger.jsonl", limit=100)
    print(_format_ledger(rows))
    return 0


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(ctx: Any) -> None:
    global _CTX
    _CTX = ctx

    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    # hold_stream asks Hermes to hand us the reply before the user sees it.
    # Older Hermes builds accept only (name, callback); fall back cleanly.
    try:
        ctx.register_hook("transform_llm_output", on_transform_llm_output, hold_stream=True)
    except TypeError:
        ctx.register_hook("transform_llm_output", on_transform_llm_output)

    ctx.register_command(
        "proof",
        proof_command,
        description="Show the ProofGate verdict ledger for recent replies",
    )
    ctx.register_tool(
        name="proof_status",
        toolset="proofgate",
        schema={
            "name": "proof_status",
            "description": (
                "Return ProofGate's recent verdicts: which of your replies were "
                "verified against tool evidence, downgraded, or blocked."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        handler=proof_status_tool,
    )
    ctx.register_cli_command(
        "proofgate",
        "Inspect the ProofGate evidence ledger",
        _cli_setup,
        _cli_handler,
        description="Show ProofGate verdicts recorded on this machine",
    )
