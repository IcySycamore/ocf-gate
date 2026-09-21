---
description: "Take in a new task: ask for goal, available tools, reference design, deliverables and code style one item at a time, and confirm whether to create the ADR and CONTEXT docs"
agent: "Work Orchestrator"
argument-hint: "The task description or request"
---

Follow the intake contract in [work-control-flow.md](../work-control-flow.md) section 5.
Commands below are shorthand for `python .github/ocf/ocf.py <cmd>`.

1. Run status. In ready, this message has already moved the hook to asking.
2. Offer the human three sections to say in their own words: the goal in one sentence, the requirements
   (the reference design and the process steps), and the deliverables. Do not hand them a list of keys.
3. Derive the eight plan sections from that statement plus the context you can observe, and record the
   facts with `set <key> "<the human's own words>"`. Section 5 lists what each key must capture and its
   minimum length. The human corrects what you derived; they do not fill in a form.
4. Grill only what you could not derive, one question per turn, staying in asking. Say which value is
   missing and why it cannot be inferred.
5. Decide docs_decision from the repository itself - does CONTEXT.md or docs/adr already exist? - and let
   the human overrule. create uses `grill-with-docs`, and skip falls back to `grill-me`.
6. stack_env records the human's declaration that the environment may be used. Ask them to declare it;
   never install or probe on your own.

A perfunctory or automatic reply is not an answer. Re-ask and do not advance the state. Once every
gate passes, advance planning, or ask the human to run confirm in their terminal.

During intake do not propose a solution, write code or run commands, and never write a guess into
facts as if the human had confirmed it.
