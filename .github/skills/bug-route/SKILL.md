---
name: bug-route
description: Reproduce and isolate an OCF-governed defect before changing code. Use when behavior fails, throws, or regresses.
argument-hint: "Symptom, expected behavior, actual behavior, and reproduction steps"
user-invocable: true
disable-model-invocation: true
---

# Bug Route

Use this skill only with the Work Orchestrator agent. Agent Skills do not inherit the prompt file's
agent binding. If another agent is active, stop and ask the human to switch to Work Orchestrator.
Do not continue this workflow under a different agent.

Read the project's diagnosis guidance and select the cheapest reproduction that can distinguish the
likely failure paths: a focused test or command, a minimized reproduction, or a static trace when
execution would not add useful evidence. Use Criterion Picker to review the method before running
it. Do not edit production code before a reproducible failure or a concrete control-flow defect is
identified.

Reproduce once, record the exact command and result, then reduce the case to the smallest failing
surface. Form one local hypothesis and name a check that could disprove it. Change one cause at a
time, rerun that check, and add a regression check for the observed failure. Ask the human before
installing dependencies or running tests when the active OCF policy requires authorization.

If a gate blocks the work, report its exact finding. If evidence changes the approved scope, stop
and return to planning instead of quietly expanding the fix. Summarize the cause, correction, and
verification evidence; do not claim a fix that was not reproduced.
