---

description: "Produce the action plan and have it reviewed: 8-section plan, independent P0 review, P0 at zero, then the action report and risk design report ready for human approval"
agent: "Work Orchestrator"
argument-hint: "Optional: extra constraints on the plan"---

Follow section 6 of [work-control-flow.md](../work-control-flow.md). Commands below are
shorthand for `python .github/ocf/ocf.py <cmd>`.

1. Run status. Once `gate grill-valid` passes, advance planning, which runs the entry gates context,
   docs-decision and grill-valid.
2. Write .orchestrator/plan.md with all 8 sections. Files must name the exact paths to change, because
   human-code-clear compares them against human-code.txt; on a collision ask the human to allow the
   path.
3. Dispatch the plan-auditor subagent for an independent review; never audit your own plan. It returns
   the schema result and the P0 list, each item with a verifiable criterion and a repair direction.
4. Any P0 means revise and re-audit until there are none. Only then set p0_count from the auditor's
   actual count. Setting it before the audit, or from optimism rather than from the audit, is
   forbidden.
5. Give the two reports in chat, hand the human this verbatim, and stop:

   python .github/ocf/ocf.py approve "<one-sentence reason>"

Do not run approve for the human and do not set approval yourself. Do not edit files while in
planning, and do not add steps the human never asked for.
