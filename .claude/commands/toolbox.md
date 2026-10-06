---
description: List the commands and skills available from BOTH the user-global ~/.claude/ toolkit AND the current workspace's .claude/ overrides (fast, hardcoded — refresh with /toolbox-update).
---

Output exactly the following text verbatim, with no preamble, commentary, or trailing summary. The two sections below correspond to the two directories Claude actually has access to: the user-global `~/.claude/` toolkit and the current workspace's `.claude/` overrides. If a name appears in both, the workspace copy wins for the running Claude (it's loaded later); both are listed here so you can see what's available at each scope.

**User-global commands (`~/.claude/commands/`)**
- `/toolbox` — List the user-scoped commands and skills in ~/.claude.
- `/toolbox-update` — Rescan ~/.claude and the workspace .claude and refresh the hardcoded list inside /toolbox.

**User-global skills (`~/.claude/skills/`)**
- `/grill-me` — Interview me relentlessly to stress-test a plan or design.

**Workspace commands (`./.claude/commands/`)**
- `/verify-sandbox` — Run the 22-check sandbox PASS/FAIL battery (+ adversarial probes) against the live process.

**Workspace skills (`./.claude/skills/`)**
- `/claude-sandbox` — Architecture invariants, refuse-lists and walked-back paths for the bwrap sandbox core.
- `/claude-sandbox-container` — Design decisions for the published container image and its host launcher.
- `/claude-sandbox-networking` — Egress jail, firewall and lateral-movement design.
- `/grill-me` — Interview me relentlessly to stress-test a plan or design.
- `/pr-review-sweep` — Work through a pull request's review comments systematically.
- `/triage` — Triage issues through a state machine driven by triage roles.
