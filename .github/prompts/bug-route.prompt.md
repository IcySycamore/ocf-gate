---
description: "Locate a defect: decide the reproduction method first, then converge with the diagnose skill"
agent: "Work Orchestrator"
argument-hint: "Symptom: what you expect, what actually happens, how to reproduce it"
---

Locate first; do not change code first.
Commands below are shorthand for `python .github/ocf/ocf.py <cmd>`.

1. Dispatch criterion-picker to decide the reproduction method. Its criteria table lives in that agent
   file. It must return the chosen method, why the other two were rejected, the next step, and what it
   needs from the human.
2. Run the chosen method. Chain-of-thought simulation reasons statically with no edits and no commands.
   A unit or integration run follows the diagnose skill, ruling out one block at a time, with commands
   that obey section 7.
3. When the root cause is found: if the fix is inside what was approved, stay in executing; if it is
   outside, go blocked.

Record failures with `ocf.py fail "<reason>"` as they happen, including the first one. When a gate stops
you, go to the human with the symptom, the exact error and command, what you tried with the result of
each, and what you need from them. Do not keep retrying.

Do not change code before a reproduction loop exists, and do not rule out several blocks at once.
