"""ProofGate checker tests. Runs under pytest or directly: python3 test_checker.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import checker


def ev(tool, args=None, result="", status="ok"):
    return {"tool_name": tool, "args": args or {}, "result": result, "status": status}


def test_file_claim_proven_passes():
    evidence = [ev("write_file", {"path": "/tmp/report.md"}, '{"success": true}')]
    result = checker.check_reply("Done. I created the report at /tmp/report.md", evidence)
    assert result["verdict"] == "PASS", result
    out = checker.apply_verdict("Done. I created the report at /tmp/report.md", result)
    assert "verified against tool evidence" in out


def test_file_claim_without_evidence_blocks():
    result = checker.check_reply("Done. I created the report at /tmp/report.md", [])
    assert result["verdict"] == "BLOCK", result
    out = checker.apply_verdict("Done. I created the report at /tmp/report.md", result)
    assert "withheld" in out


def test_send_claim_contradicted_by_failed_send_blocks():
    evidence = [ev("send_message", {"to": "x"}, "error: network down", status="error")]
    result = checker.check_reply("The message has been sent to the client.", evidence)
    assert result["verdict"] == "BLOCK", result
    assert result["unproven"][0]["contradicted"] is True


def test_mixed_proven_and_unproven_downgrades():
    evidence = [ev("write_file", {"path": "/tmp/report.md"}, '{"success": true}')]
    text = "I created the report at /tmp/report.md and sent it to the client."
    result = checker.check_reply(text, evidence)
    assert result["verdict"] == "DOWNGRADE", result
    out = checker.apply_verdict(text, result)
    assert "could NOT be verified" in out


def test_send_claim_proven_passes():
    evidence = [ev("send_message", {"to": "client"}, '{"message_id": "42"}')]
    result = checker.check_reply("Your message has been sent.", evidence)
    assert result["verdict"] == "PASS", result


def test_number_claim_needs_evidence_figure():
    text = "Done. The total cost is $79 for the plan."
    missing = checker.check_reply(text, [])
    assert any(c["family"] == "number" and not c["proven"] for c in missing["claims"])
    evidence = [ev("web_extract", {}, "Pricing page: plan costs $79 per month")]
    found = checker.check_reply(text, evidence)
    number_claims = [c for c in found["claims"] if c["family"] == "number"]
    assert number_claims and all(c["proven"] for c in number_claims), found


def test_casual_reply_without_claims_passes_untouched():
    text = "Sure, I can help with that. What would you like to do first?"
    result = checker.check_reply(text, [])
    assert result["verdict"] == "PASS" and result["claims"] == []
    assert checker.apply_verdict(text, result) is None


def test_missing_status_is_not_proof():
    evidence = [{"tool_name": "write_file", "args": {"path": "/tmp/report.md"}, "result": "ok"}]
    result = checker.check_reply("Done. I created the report at /tmp/report.md", evidence)
    assert result["verdict"] == "BLOCK", result


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
