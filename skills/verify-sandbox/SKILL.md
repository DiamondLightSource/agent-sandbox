---
name: verify-sandbox
description: Audit the current claude-sandbox session with the installed 22-check battery, followed by ten adversarial probes. Use when the user requests a full sandbox isolation audit or invokes verify-sandbox; use claude-sandbox verify alone for a quick deterministic check.
---

# Verify the sandbox

Audit the current agent's sandbox in two phases. This skill ships to Claude,
Codex and Pi; no source checkout is needed. The installed shell battery is
the single implementation of the deterministic checks. Do not reconstruct
those checks from prose or run a workspace copy instead.

## Phase 1: installed battery

Run this command and capture its output and exit status:

```bash
bash /usr/libexec/claude-sandbox/verify-sandbox-battery.sh
```

Expect 21 PASS rows and exit status zero. Read informational notes too:
checks 19–20 can pass with network filtering deliberately disabled.

If any check fails, report the failing rows verbatim and stop. Do not attempt
repairs from inside the sandbox or proceed to adversarial probes. A missing
battery is an installation failure; report it and ask the user to update the
sandbox from the outer container before retrying.

The same battery is available without an AI through `claude-sandbox verify`.
Its exit status is suitable for automation. This agent-driven audit produces
a report; do not claim the interactive agent's exit status enforces its result.

## Phase 2: adversarial probes

Only after all 22 checks pass, read [the check rationale](references/checks.md)
and design ten distinct probes for gaps the battery does not exercise.
Try to violate filesystem, credential, process, IPC or network isolation.
Use short shell snippets and record actual evidence for each attempt.

Candidate areas include kernel interfaces, `/proc` traversal, cgroup and
sysfs access, unexpected credential paths, abstract Unix sockets, raw
sockets, setuid or file-capability escalation, and Git configuration redirects.
Do not simply repeat the battery with different syntax.

Use reversible probes. Do not change host data, signal unrelated processes,
print secret values, or send recovered credentials anywhere. Stop on a
demonstrated escape and report the evidence without exploiting it further.

Classify each attempted probe:

- **BLOCKED:** the attempt failed as expected; include the error or observation.
- **ESCAPED:** evidence demonstrates an isolation breach.
- **INCONCLUSIVE:** no breach was demonstrated, but the result does not prove
  blocking. Explain why and suggest a targeted follow-up.

## Report

Include the battery output and numbered probe results under `Adversarial
probes:`, with evidence for each classification and totals for each outcome.
If fewer than ten probes ran, state how many and why; do not claim completion.

- Any battery failure or demonstrated escape: `RESULT: SANDBOX LEAKING`.
- All 22 PASS and ten BLOCKED: `RESULT: SANDBOX OK (22 deterministic + 10 adversarial)`.
- Inconclusive or unrun probes: `RESULT: AUDIT INCOMPLETE`, followed by the
  passed checks, blocked probes and unresolved items.

Scope conclusions to this session and these checks; they are not proof that
every possible escape is prevented. Do not open an issue or publish evidence
unless the user asks.
