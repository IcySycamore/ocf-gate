---
description: "Take in a new task: run the intake and record what it yields"
agent: "Work Orchestrator"
argument-hint: "The task description or request"
---

Follow the intake contract in [work-control-flow.md](../work-control-flow.md) section 5.
Commands below are shorthand for `python .github/ocf/ocf.py <cmd>`.

1. Run status. In ready, this message has already moved the hook to asking.
2. Record the facts with `set <key> "<the human's own words>"`.
3. Ask the human to declare whatever you would otherwise have to discover by running or installing
   something.

Once the gates pass, advance planning, or ask the human to run approve in their terminal.
