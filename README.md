# ProofGate

**No proof, no "done".**

ProofGate is a Hermes Agent plugin that verifies an agent's completion claims against the tool evidence recorded in the same session, before the final reply reaches you.

Most plugins give agents new powers. ProofGate does the opposite: it holds the agent accountable for the powers it already has.

## Why

The most dangerous failure mode of an autonomous agent is not a wrong answer. It is a false report that work got done: a file "created" that does not exist, a message "sent" that actually failed, a price "checked" that was never looked up. You find out later, usually after acting on the report.

ProofGate was born from a real incident: the author fired a previous AI agent for repeatedly inventing completed work. This plugin turns the rule that replaced it, never claim without receipts, into a mechanism every Hermes user can install.

## How it works

1. **Evidence collector.** A `post_tool_call` hook silently records every tool call in the session: tool name, arguments (sensitive values redacted), result, status, and duration.
2. **Claim checker.** A `transform_llm_output` hook holds the finished reply and extracts factual completion claims in five families: a file was created or updated, a message was sent, a command ran successfully, tests passed, a specific figure is quoted. Each claim is matched against the recorded evidence from the same session. Checking is deterministic, no LLM, no network, milliseconds.
3. **Fail-closed verdicts.**
   * `PASS`: every claim is backed by evidence. The reply ships with a small footer stating how many claims were verified.
   * `DOWNGRADE`: some claims check out, some do not. The unproven claims are listed in the reply as explicitly unverified, never silently deleted.
   * `BLOCK`: no claim is backed by evidence, or a claim is contradicted by a tool attempt that actually failed. The reply is withheld and replaced with an explanation of exactly which proof is missing.
   * If the checker itself errors, the reply is downgraded, never passed. A broken gate fails closed.
4. **Ledger.** Every checked reply is written to an append-only ledger on your own machine at `$HERMES_HOME/proofgate/ledger.jsonl`, with the raw evidence trail in `evidence.jsonl`. Nothing leaves your machine.

## Surfaces

* `/proof` in chat: recent verdicts and counts.
* `proof_status` tool: the agent can inspect its own verification record mid-work and fix itself before the gate has to.
* `hermes proofgate ledger` on the CLI: read the ledger from a terminal.
* A first-turn briefing: when ProofGate is active, the agent is told once per session that its claims will be checked against tool evidence.

## Install

```bash
hermes plugins install <owner>/proofgate --enable
```

Or copy this directory into `~/.hermes/plugins/proofgate/` and run `hermes plugins enable proofgate`, then restart Hermes.

Verify any time with:

```bash
hermes plugins validate ~/.hermes/plugins/proofgate
hermes plugins doctor ~/.hermes/plugins/proofgate
```

Note: `plugins validate` uses a permissive probe. `plugins doctor` exercises the real registration contracts. ProofGate passes both.

## Configuration

In `config.yaml`:

```yaml
plugins:
  entries:
    proofgate:
      settings:
        mode: enforce   # enforce | warn | off
```

* `enforce` (default): replies are footered, flagged, or withheld per verdict.
* `warn`: verdicts are recorded in the ledger, replies are never modified.
* `off`: ProofGate observes nothing and changes nothing.

## Honest limitations

* Five claim families in v0.1. Claims outside those families (plans, opinions, summaries of reasoning) are not checked and pass through untouched.
* `enforce` mode trades streaming for certainty: the finished reply is held until the check completes. Use `warn` mode if streaming matters more to you than gating.
* In v0.1, a DOWNGRADE appends an explicit unverified-claims section to the reply instead of rewriting the claim inline.
* Evidence is matched within the same session. A claim about work done in an earlier session cannot be proven from this session's trail and will be flagged.
* Statuses count as proof only when Hermes reports the tool call as successful. An unknown status is treated as no proof, by design.

## Development

```bash
python3 tests/test_checker.py   # 8 scenario tests, no dependencies
```

The checker (`checker.py`) is pure Python with no Hermes imports, so the verification logic can be tested and audited in isolation.

## License

MIT
