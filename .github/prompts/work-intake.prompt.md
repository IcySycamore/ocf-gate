---
description: "Take in a new task: ask for goal, available tools, reference design, deliverables and code style one item at a time, and confirm whether to create the ADR and CONTEXT docs"
agent: "Work Orchestrator"
argument-hint: "The task description or request"
---

Follow the intake contract in [work-control-flow.md](../work-control-flow.md) section 5.
Commands below are shorthand for `python .github/ocf/ocf.py <cmd>`.

1. Run status. In ready, this message has already moved the hook to asking.
2. Ask for goal, tools, references, deliverables and code_style one at a time, one missing item per
   question, and run `set <key> "<the human's own words>"` after each answer. Section 5 lists what each
   key must capture and its minimum length.
3. Ask whether this repository should create CONTEXT.md or docs/adr, then set docs_decision to create
   or skip. create uses `grill-with-docs`, and skip falls back to `grill-me`.
4. When a toolchain is involved, ask the human to declare the existing environment and run
   set stack_env. Never install or probe on your own.

A perfunctory or automatic reply is not an answer. Re-ask and do not advance the state. Once every
gate passes, advance planning, or ask the human to run confirm in their terminal.

During intake do not propose a solution, write code or run commands, and never write a guess into
facts as if the human had confirmed it.
