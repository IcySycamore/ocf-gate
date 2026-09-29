---
description: "Produce the action plan and take it through the gates that stand between it and approval"
agent: "Work Orchestrator"
argument-hint: "Optional: extra constraints on the plan"
---

Follow section 6 of [work-control-flow.md](../work-control-flow.md). Commands below are
shorthand for `python .github/ocf/ocf.py <cmd>`.

1. Run status, then run the gate that blocks the transition you want. A gate names what is missing.
2. Write the plan on the template at `.github/assets/plan-template.md` when the human asks for a file.
3. Keep going until every gate on the transition passes, then hand the human the command they need to
   run and stop.

Do not add steps the human never asked for.
