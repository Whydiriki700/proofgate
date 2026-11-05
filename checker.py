"""ProofGate claim checker.

Pure, deterministic verification of completion claims in an agent's final
reply against the tool evidence recorded in the same session. No LLM, no
network, no Hermes imports: this module is unit-testable in isolation.

Claim families (v0.1):
  - file:    a file was created / written / saved / updated at a path
  - send:    a message / email / post was sent, delivered, posted, published
  - run:     a command ran, tests passed, a build succeeded, something installed
  - number:  a specific figure quoted inside a completion claim

Verdicts:
  - PASS:      every extracted claim is backed by evidence (or no claims)
  - DOWNGRADE: some claims proven, some not; unproven ones must be flagged
  - BLOCK:     no claim proven, or a claim contradicts a failed tool attempt
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

# ---------------------------------------------------------------------------
# Evidence helpers
# ---------------------------------------------------------------------------

SEND_TOOL_HINTS = (
    "send", "message", "email", "mail", "post", "publish", "reply", "notify",
)
WRITE_TOOLS = {"write_file", "patch", "create_file", "edit_file"}
RUN_TOOLS = {"terminal", "bash", "shell", "exec", "run_command"}


def _result_text(ev: Dict[str, Any]) -> str:
    result = ev.get("result")
    return result if isinstance(result, str) else ""


def _args_text(ev: Dict[str, Any]) -> str:
    args = ev.get("args")
    return str(args) if args is not None else ""


def is_success(ev: Dict[str, Any]) -> bool:
    """An evidence record counts as proof only when its status is ok.

    The Hermes post_tool_call payload carries an explicit status; a missing
    status is treated as unknown and never as proof (fail closed).
    """
    return str(ev.get("status") or "").lower() in {"ok", "success", "succeeded"}


def is_failure(ev: Dict[str, Any]) -> bool:
    return str(ev.get("status") or "").lower() in {"error", "failed", "failure"}


def written_paths(evidence: Sequence[Dict[str, Any]]) -> List[str]:
    paths: List[str] = []
    for ev in evidence:
        if ev.get("tool_name") in WRITE_TOOLS and is_success(ev):
            path = (ev.get("args") or {}).get("path")
            if isinstance(path, str) and path:
                paths.append(path)
    return paths


def has_successful_send(evidence: Sequence[Dict[str, Any]]) -> bool:
    for ev in evidence:
        name = str(ev.get("tool_name") or "").lower()
        if any(hint in name for hint in SEND_TOOL_HINTS) and is_success(ev):
            return True
    return False


def has_failed_send(evidence: Sequence[Dict[str, Any]]) -> bool:
    for ev in evidence:
        name = str(ev.get("tool_name") or "").lower()
        if any(hint in name for hint in SEND_TOOL_HINTS) and is_failure(ev):
            return True
    return False


def has_successful_run(evidence: Sequence[Dict[str, Any]]) -> bool:
    return any(
        ev.get("tool_name") in RUN_TOOLS and is_success(ev) for ev in evidence
    )


def has_failed_run(evidence: Sequence[Dict[str, Any]]) -> bool:
    return any(
        ev.get("tool_name") in RUN_TOOLS and is_failure(ev) for ev in evidence
    )


def evidence_corpus(evidence: Sequence[Dict[str, Any]]) -> str:
    """All text a quoted figure could legitimately come from."""
    parts: List[str] = []
    for ev in evidence:
        parts.append(_result_text(ev))
        parts.append(_args_text(ev))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Claim extraction
# ---------------------------------------------------------------------------

_PATH_RE = re.compile(
    r"(?:~?/[\w.\-]+(?:/[\w.\-]+)*|[\w.\-]+\.(?:md|txt|py|json|csv|html?|pdf|"
    r"docx|xlsx|pptx|png|jpe?g|webp|yaml|yml|toml|xml|zip))"
)
_NUMBER_RE = re.compile(r"(?<![\w.])\$?\d[\d,]*(?:\.\d+)?%?")

_FILE_VERBS = (
    "created", "wrote", "written", "saved", "generated", "updated", "edited",
    "produced", "dibuat", "disimpan", "tertulis", "diperbarui",
)
_SEND_VERBS = (
    "sent", "delivered", "posted", "published", "emailed", "forwarded",
    "terkirim", "dikirim", "terbit", "diterbitkan",
)
_RUN_PHRASES = (
    "tests passed", "test passed", "all tests pass", "build succeeded",
    "build passed", "installed successfully", "command ran", "ran successfully",
    "executed successfully", "berhasil dijalankan", "tes lulus", "semua tes",
)
_COMPLETION_WORDS = _FILE_VERBS + _SEND_VERBS + (
    "done", "completed", "finished", "selesai", "berhasil",
)


def _sentences(text: str) -> List[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [p.strip() for p in parts if p and p.strip()]


def _contains_any(sentence: str, words: Sequence[str]) -> bool:
    low = sentence.lower()
    return any(w in low for w in words)


def extract_claims(text: str) -> List[Dict[str, Any]]:
    claims: List[Dict[str, Any]] = []
    for sentence in _sentences(text):
        low = sentence.lower()
        paths = _PATH_RE.findall(sentence)
        if paths and _contains_any(low, _FILE_VERBS + ("done", "selesai")):
            for path in paths:
                claims.append(
                    {"family": "file", "text": sentence, "target": path}
                )
        if _contains_any(low, _SEND_VERBS):
            claims.append({"family": "send", "text": sentence, "target": None})
        if _contains_any(low, _RUN_PHRASES):
            claims.append({"family": "run", "text": sentence, "target": None})
        for number in _NUMBER_RE.findall(sentence):
            # Money and percent figures are claims wherever they appear;
            # plain numbers count inside completion claims.
            if (
                number.startswith("$")
                or number.endswith("%")
                or _contains_any(low, _COMPLETION_WORDS)
            ):
                claims.append(
                    {"family": "number", "text": sentence, "target": number}
                )
    return claims


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def _path_matches(claimed: str, written: Sequence[str]) -> Optional[str]:
    claimed_base = claimed.rstrip("/").split("/")[-1]
    for path in written:
        if path == claimed or path.endswith("/" + claimed) or claimed.endswith(path):
            return path
        if path.split("/")[-1] == claimed_base and claimed_base:
            return path
    return None


def verify_claim(
    claim: Dict[str, Any], evidence: Sequence[Dict[str, Any]]
) -> Dict[str, Any]:
    family = claim["family"]
    proven = False
    ref: Optional[str] = None
    contradicted = False
    if family == "file":
        ref = _path_matches(str(claim.get("target") or ""), written_paths(evidence))
        proven = ref is not None
    elif family == "send":
        proven = has_successful_send(evidence)
        ref = "successful send tool call" if proven else None
        contradicted = has_failed_send(evidence) and not proven
    elif family == "run":
        proven = has_successful_run(evidence)
        ref = "successful terminal tool call" if proven else None
        contradicted = has_failed_run(evidence) and not proven
    elif family == "number":
        target = str(claim.get("target") or "")
        proven = bool(target) and target in evidence_corpus(evidence)
        ref = f"figure {target} present in tool evidence" if proven else None
    return {**claim, "proven": proven, "evidence": ref, "contradicted": contradicted}


def check_reply(
    response_text: str, evidence: Sequence[Dict[str, Any]]
) -> Dict[str, Any]:
    """Return the verdict for a final reply against session evidence."""
    claims = [verify_claim(c, evidence) for c in extract_claims(response_text)]
    if not claims:
        return {"verdict": "PASS", "claims": [], "unproven": []}
    unproven = [c for c in claims if not c["proven"]]
    if not unproven:
        return {"verdict": "PASS", "claims": claims, "unproven": []}
    if any(c["contradicted"] for c in unproven):
        verdict = "BLOCK"
    elif len(unproven) == len(claims):
        verdict = "BLOCK"
    else:
        verdict = "DOWNGRADE"
    return {"verdict": verdict, "claims": claims, "unproven": unproven}


# ---------------------------------------------------------------------------
# Rewriting
# ---------------------------------------------------------------------------


def apply_verdict(response_text: str, result: Dict[str, Any]) -> Optional[str]:
    """Return the replacement reply for a verdict, or None to leave as-is."""
    verdict = result["verdict"]
    claims = result["claims"]
    if verdict == "PASS":
        if not claims:
            return None
        footer = (
            f"\n\n[ProofGate: {len(claims)} claim(s) in this reply verified "
            "against tool evidence.]"
        )
        return response_text + footer
    if verdict == "DOWNGRADE":
        lines = [
            "",
            "",
            "[ProofGate: the following claim(s) could NOT be verified against",
            "tool evidence from this session. Treat them as unverified:]",
        ]
        for claim in result["unproven"]:
            lines.append(f"- ({claim['family']}) {claim['text']}")
        return response_text + "\n".join(lines)
    # BLOCK
    lines = [
        "[ProofGate: this reply was withheld. The agent claimed work that is",
        "not backed by tool evidence from this session.]",
        "",
        "Unverified claim(s):",
    ]
    for claim in result["unproven"]:
        note = " (a matching tool attempt actually FAILED)" if claim["contradicted"] else ""
        lines.append(f"- ({claim['family']}) {claim['text']}{note}")
    lines += [
        "",
        "No proof, no \"done\". This incident was written to the ProofGate ledger.",
    ]
    return "\n".join(lines)
