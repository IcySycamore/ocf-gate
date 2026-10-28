---
name: work-plan
description: Produce an OCF action plan, check the gates, and obtain independent review before execution. Use when a task is ready to plan.
argument-hint: "Optional constraints for the plan"
user-invocable: true
disable-model-invocation: true
---

# Work Plan

Use this skill only with the Work Orchestrator agent. Agent Skills do not inherit the prompt file's
agent binding. If another agent is active, stop and ask the human to switch to Work Orchestrator.
Do not continue this workflow under a different agent.

Read section 6 of `.github/work-control-flow.md`, then run `status` and the gate relevant to the
requested transition. A gate names the missing precondition; resolve that condition rather than
trying to bypass the gate.

Give the plan in the conversation unless the human asks for a file. If a file is requested, use
`.github/assets/plan-template.md` and list exact paths and actions. Dispatch Plan Auditor after the
plan is written. Address findings and repeat the review until the auditor reports zero P0. Then
give the action report and risk design report, provide the human-only approval command, and stop.

Do not enter `executing`, run `approve`, or treat approval expressed in conversation as a terminal
approval. Carry out no implementation work under this planning skill.
