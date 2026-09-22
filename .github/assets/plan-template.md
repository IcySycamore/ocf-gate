# Action plan: <one-sentence goal>

> Target path: `.orchestrator/plan.md`. Fill every other line in the language the human is using.
> This template is the agent's own working structure. The human never fills it.

## Type

- [ ] feature
- [ ] enhancement
- [ ] refactor
- [ ] fix
- [ ] docs
- [ ] chore
- [ ] test

## Summary

<what and why now, two or three sentences, do not repeat background>

## Steps

1. <one independently verifiable step>
2. <...>
3. <...>

> Every step must be a place where work can stop and be checked. The order must not create an
> unusable intermediate state.

## Tools

<command or service -- what it does -- what effect it produces>

## Files

| Action | Path / Concept |
| ------ | -------------- |
| ADD    | ...            |
| DELETE | ...            |
| MODIFY | ...            |

## Scope

- [ ] Small
- [ ] Middle
- [ ] Big

- ADD **number** files
- DELETE **number** files
- MODIFY **number** files

- Unit testing / Integration testing / E2E testing / no testing
  (not visual testing: the `visual-tool` and `visual-command` rules refuse the machine every way of
  capturing an image, so a step that needs one has to be the human's to run)
- Build / CI / Deployment / no build

## Deliverables

1. ...
2. ...

## Self-review

| ID  | Severity | Problem | Disposition |
| --- | -------- | ------- | ----------- |
| A1  | P1       | ...     | ...         |
