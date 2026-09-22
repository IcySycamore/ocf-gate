---
description: "Locate a defect: decide the reproduction method first, then converge with the diagnose skill"
agent: "Work Orchestrator"
argument-hint: "Symptom: what you expect, what actually happens, how to reproduce it"
---

Locate first; do not change code first. This flow only runs inside executing. Commands below are
shorthand for `python .github/ocf/ocf.py <cmd>`.

1. Dispatch criterion-picker to decide the reproduction method. Its criteria table lives in that agent
   file. It must return the chosen method, why the other two were rejected, the next step, and what it
   needs from the human.
2. Run the chosen method. Chain-of-thought simulation reasons statically with no edits and no
   commands. A unit or integration run follows the diagnose skill, ruling out one block at a time,
   with commands that obey section 7. For a state or visual snapshot you export and analyse the state
   yourself, and the visual part must come from a human-taken screenshot.
3. When the root cause is found: if the fix is inside the approved plan, stay in executing; if it is
   outside, advance blocked and re-plan through review and approval.

After 2 consecutive failures of command execution or character parsing, record them with
`ocf.py fail "<reason>"`, including the first one. Reaching the budget sets must_consult and locks
every exec-class tool. Stop and tell the human the symptom with the exact error and command, what you
already tried with the result of each, and what you need from them. Do not keep retrying. Note what the
lock actually is: any message from the human clears it, and the gate cannot tell a considered reply
from an injected one or a perfunctory one. So it stops you from looping; it is not evidence that the
human has understood anything.

Do not change code before a reproduction loop exists, do not rule out several blocks at once, and do
not run visual or screenshot tests or work around that ban.
