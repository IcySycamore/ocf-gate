# Work Control Flow

Full rules: [work-control-flow.md](./work-control-flow.md). Read it before first acting and whenever your attention begins to drift.

## Entry point

    Windows: python .github\ocf\ocf.py <cmd>
    Other:   python3 .github/ocf/ocf.py <cmd>

Agent may run: status, set, gate, journal, fail, ok, init, selftest. advance targets are limited to
asking, planning, reporting, ready, blocked. Human terminal only, calling these gets you blocked:
approve, reject, confirm, allow, deny, human-code.
Never hand-edit state, facts, journal.log, human-code.txt or allowed-edits.txt under .orchestrator/.
The gate's own configuration is `.github/ocf/policy.toml`, which the human edits by hand.

## State machine

    ready -> asking -> planning -> executing -> reporting -> ready      bypass: blocked

Only the human running `python .github/ocf/ocf.py approve "<reason>"` in their own terminal enters
executing. Never run it for them or set approved_by.

## Three behaviours no code can enforce

1. asking: derive the eight plan sections from context and the human's three-section statement, then
   grill only what is left, one question per turn. Never guess or fill in for the human. A perfunctory
   or automatic reply is not an answer, so re-ask.
2. planning: dispatch the plan-auditor subagent for an independent P0 review, never audit your own
   plan. At P0 zero, give the human the action report and the risk design report, hand them
   `python .github/ocf/ocf.py approve "<reason>"` verbatim, then stop.
3. To see a screen, state exactly what to capture and how many shots, then ask the human to attach the
   screenshot. Attachments are readable, screenshot tools are permanently blocked.

## When a gate blocks you

Fix the precondition it names: supply the missing fact, split the command, stop silencing output, go
ask the human. Never rewrite your way around it. Circumventing a gate is a serious violation.
