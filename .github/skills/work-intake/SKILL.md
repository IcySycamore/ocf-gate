---
name: work-intake
description: Collect only the missing facts for an OCF-governed task and move the OCF workflow into planning. Use when starting or resuming task intake.
argument-hint: "Goal, requirements, deliverables, or the task as currently understood"
user-invocable: true
disable-model-invocation: true
---

# Work Intake

Use this skill only with the Work Orchestrator agent. Agent Skills do not inherit the prompt file's
agent binding. If another agent is active, stop and ask the human to switch to Work Orchestrator.
Do not continue this workflow under a different agent.

Read section 5 of `.github/work-control-flow.md` and follow the live OCF state. First run `status`.
Use the existing conversation and repository context before asking anything. Ask only for facts the
context cannot establish, and ask one focused question at a time.

Record confirmed facts with the OCF `set` command, using the human's wording where practical. Never
guess the environment, tools, references, deliverables, code style, documentation decision, or
consensus. Do not set `p0_count`; the hook records that value from the auditor's report.

When the intake gates pass, request the allowed transition to `planning`. If a gate fails, report
the exact missing item and keep the state unchanged. A short task can use the three-part shape
`Goal / Requirements / Deliverables`; do not force the human to reformat a request that is already
clear.
