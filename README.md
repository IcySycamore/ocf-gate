# Work Control Flow

English | [中文](README_CH.md)

---

## Project introduction

The goals of this project are,

1. as agent-assisted development increasingly becomes part of a computer-industry practitioner's work, to explore the human & agent collaboration workflow and the agent's **permission boundary**;
2. to provide a system. Through a whole built from well-designed prompts, a state machine and so on, it effectively raises the human-agent collaboration experience, distils the human collaboration steps that are necessary, balances working efficiency, quality and accuracy, and solves the problems that come from the agent and the human disagreeing: the agent overstepping, behaving unpredictably, answering badly.

The glossary is in the appendix.

### Have you hit these problems?

The most common accident with an AI agent is not that it cannot do the work, but that it does not understand what the human meant.

- Starts work before the requirement is clear, and what it delivers does not match what was expected
- Edits files, sets up environments, creates directories, runs commands silently or retries forever, causes huge unrecoverable damage after an error, or overwrites comments and code the human wrote by hand - doing work outside the boundary it was given
- Answers are poor: full of syntax, logic, factual and comprehension errors; hallucination, not answering the question, repetition, meaninglessness, over-explaining (raising the cost of understanding, slowing the reader down) or too shallow (burning too many tokens)
- Output in the wrong format, the wrong language or the wrong style; work with no order, no plan, no report

### Advantages

| Feature                                                             | What it gives you                                                                                                                                                |
| ------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **A hard gate plus soft guidance**                                  | Hooks are designed to build the workflow and constrain the model's behaviour, and light guidance words focus the model's attention on what matters               |
| **A light core**                                                    | A single .py file at the core, standard library only, cross-platform                                                                                             |
| **Approval does not rest on understanding, and cannot be bypassed** | Only a human running the`approve` command in their own terminal can approve                                                                                      |
| **The protected list**                                              | A protected directory limits the model's access to files, and self-protects the system                                                                           |
| **A rules and configuration system**                                | 23 hard rules and 8 soft rules distilled from real project experience, managed conveniently as key-value toml, able to switch the whole system on or off at once |
| **The terminal stays in the human's hands**                         | It blocks execution before approval, long commands, multi-statement chains, silenced output, interactive blocking, repeated execution and consecutive failures   |
| **Auditable end to end**                                            | State transitions, authorizations and failures are all visible in`.orchestrator/journal.log`                                                                     |
| **Classic tests built in**                                          | Self-checks at deployment, at the start of a conversation and on every rule reload, so the system never fails silently                                           |
| **Easy to deploy, package and migrate**                             | Convenient to deploy; one command migrates the system and its state                                                                                              |

Other features are for you to find!

### Composition

| Part                   | Where                             | What it does                                                                                                                                                                                                                                      |
| ---------------------- | --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **State machine**      | `TRANSITIONS` in `ocf.py`         | `ready → asking → planning → executing → reporting → ready`, by pass: `blocked`                                                                                                                                                                   |
| **Command units**      | the command table in`ocf.py`      | The agent may use`status` / `set` / `gate` / `journal` / `fail` / `ok` / `advance` / `init` / `selftest` / `verify`; the human may use `approve` / `reject` / `protect` / `unprotect` / `reload` / `install` / `package`                          |
| **Persistence**        | `.orchestrator/`                  | `state` the machine state, `facts` the distilled working elements, `glossary.md` the terms, `journal.log` the audit, `exec.log` the command-repeat record, `prompt-log` the intake record, `plan.md` the plan as a file, only when the human asks |
| **Configurable rules** | `.github/ocf/policy.toml`         | 23 hard rules + 8 soft rules                                                                                                                                                                                                                      |
| **VS Code hook**       | `.github/hooks/orchestrator.json` | Four events at`ocf.py hook`: SessionStart / UserPromptSubmit / PreToolUse / PostToolUse                                                                                                                                                           |

---

## Environment dependencies

| Item                 | Requirement                                                                                                            |
| -------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| **Runtime**          | Python 3,`tomllib` or `tomli`, no build.                                                                               |
| **Host**             | VS Code GitHub Copilot Chat                                                                                            |
| **hooks**            | An organization policy may disable hooks                                                                               |
| **Interpreter name** | `python` on Windows, `python3` elsewhere                                                                               |
| **Window reload**    | `json` is not hot-reloaded, so the window must be reloaded; (optional) confirm with `Developer: Show Agent Debug Logs` |

---

## Installation and deployment

### Where the resources are

| Key                                 | Value                                                           |
| ----------------------------------- | --------------------------------------------------------------- |
| `.github/`                          | The delivery unit. Plain text; copy it into any repository root |
| `release/payload/`                  | The publishing half                                             |
| `release/build/`                    | The build and packaging half                                    |
| `dist/ocf-gate-<version>-setup.exe` | The distribution package                                        |
| `dist/ocf-gate-<version>/`          | The staged tree                                                 |

### One-click deployment

Double-click `dist/ocf-gate-<version>-setup.exe` and pick the **target repository** on the first page

### Manual deployment

```text
# 1) Copy .github/ into the target repository root
# 2) Initialise the runtime
python  .github\ocf\ocf.py init      # Windows
python3 .github/ocf/ocf.py init      # other platforms
# 3) Register the paths this repository must protect
python .github\ocf\ocf.py protect "src/**"
# 4) Self-check
python .github\ocf\ocf.py selftest
# 5) Generate the configuration into the artifacts
python .github\ocf\ocf.py reload
# 6) Reload the VS Code window so the hooks take effect
```

Out of the box the master switch `system.enabled` in `policy.toml` is `false`; after deploying, set it to `true` first, then run `reload`.

### Confirm the deployment

```text
python .github\ocf\ocf.py verify      # deployment usability check
python .github\ocf\ocf.py selftest    # gate status check
```

---

## Getting started

Just tell the agent your task.

The human-facing skills this system provides: `/work-intake`, `/work-plan`, `/bug-route`.

### Session start

- **User**: say something to the agent.
- **System**:
  - the `SessionStart` hook fires: the self-check runs, and its findings are classified as environment / policy / system / ok and injected;
  - the `UserPromptSubmit` hook fires: `ready` to `asking`, recording `first_prompt` and the transition
- **Model**: reads the self-check findings, reads what the human said.

### Intake

- **User**: answer the consensus gaps from the model's reply - goal, tools, references, deliverables, code style, plus the docs decision and the consensus. The recommended three-part structure is shown in the `argument-hint` of `.github/agents/orchestrator.agent.md`: Goal / Requirements / Deliverables
- **System**:
  - every time the human replies: `grill_rounds` +1
  - write `prompt-log`
  - when the model asks for a state transition: verify that `context`, `docs-decision`, `grill-valid` and the rest are complete, and record the transition
  - on the transition 'asking' to 'planning', clear the intake record and counters
- **Model**:
  - asks the human and fills the working elements into `facts`;
  - when it believes the questions are done: asks the system for the state transition `advance planning`

### Plan

- **User**:
  - review the plan
  - (optional) explicitly ask for a plan file
- **System**: checks that the plan elements `plan-schema`, `zero-p0`, `protected-list-clear`, `stack-env` are complete. The plan is given in the conversation by default
- **Model**:
  - writes the plan according to the template
  - has a subagent audit it independently
  - repeats the above until the P0 count reaches zero
  - delivers the action report and the risk design report
  - asks the human to approve

### Execution

- **User**:
  - run `python ocf approve "<a reason of at least 8 characters>"` in the terminal, then wake the model
  - (optional) explicitly ask for tests, a build, visual verification or a file report
- **System**:
  - allows the agent to make command-class tool calls (undeclared tools fall back to heuristic matching)
  - checks whether the commands the agent runs are repeated, silent, or need no waiting
- **Model**:
  - carries out the plan
  - when it has to deviate from the plan: `advance blocked` and re-plan.
  - writes the report according to the template

---

## Appendix

### Release

Produce every artifact:

```text
.\release\build\build.ps1
```

**Checks first, then packages**: the case table and structural checks in the delivery set, and the engine self-check.

### Updating

**Run the new exe, and pick the same repository**

Before installing, the payload is checked against the release's hash manifest; if you have done your own development, save your work first.
`policy.toml`, `protected.txt`, `.orchestrator/` and the `facts` keys migrate as they are; where they differ, the new version is written beside them as a `.dist` of the same name for you to compare.

### Uninstalling

Deleting `.github/hooks/` and `.orchestrator/` stops all enforcement.

### Daily operations and checks

Key

| Command                              | What it does                                                                                                        |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------- |
| `python .github\ocf\ocf.py reload`   | Rebuilds the standing guidance, the vocabulary region of`policy.toml`, and the reference region of the system guide |
| `python .github\ocf\ocf.py selftest` | Checks the environment, the policy, the hooks and the gate status                                                   |
| `python .github\ocf\tests\run.py`    | The offline cases and structural checks in the delivery set                                                         |

Others

| Command                                | What it answers                                            |
| -------------------------------------- | ---------------------------------------------------------- |
| `python .github\ocf\ocf.py status`     | State machine state, facts, configuration elements         |
| `python .github\ocf\ocf.py journal 20` | The audit: state transitions, authorizations, failures     |
| `python .github\ocf\ocf.py gate`       | Runs the gates: whether a transition is possible right now |
| `python .github\ocf\ocf.py verify`     | Deployment usability check                                 |

> [!WARNING]
> After editing `.github/ocf/`, set the entries you want to enable in the configuration to true first, then run `reload`.

### Modifying

| What to change            | How                                                                                                                                                                                        | Window reload? |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------- |
| **Gate rules**            | Edit`[[rule]]` in `policy.toml`. Rules are data, first match wins, exemptions first and catch-alls last                                                                                    | no             |
| **Thresholds**            | Edit the number inside the rule                                                                                                                                                            | no             |
| **Soft rules**            | Edit the text of the`kind = "soft"` rule, then `reload`; that paragraph of the standing guidance changes with it                                                                           | no             |
| **Tool classes**          | Edit the lists under`[tools]` and the field paths under `[tools.field]`. Adding a tool **needs no code change**                                                                            | no             |
| **Turning something off** | Edit the`[hooks]` master switch or an event switch, then `reload`                                                                                                                          | **yes**        |
| **The gate switch**       | Edit`[system] enabled`. Only `true` / `false` are accepted; anything else is applied as `true` (strictest) and reported                                                                    | no             |
| **Self-check canaries**   | Edit`[selftest.canary]`. A canary must go through the real hook entry point, or it cannot prove the gate is alive                                                                          | no             |
| **The state machine**     | Edit`TRANSITIONS` in `ocf.py`, and keep `STATES` consistent with it                                                                                                                        | no             |
| **Hook events**           | Edit`[hooks]` in `policy.toml`, then `reload`. **Do not hand-edit** `.github/hooks/orchestrator.json` - it is generated by `reload`, and a hand edit fails an assertion and is overwritten | **yes**        |
| **Generated prose**       | The standing guidance, the configuration's vocabulary region and the guide's reference region are all generated by`reload`; run it after a configuration change                            | no             |
| **Hand-written prose**    | The prose part of the system guide, this README and its Chinese version, the prompt and agent definitions                                                                                  | no             |

### Self-rescue (when the agent is locked out by the gate)

| Symptom                                    | What to do                                                                                                                                                                                                                                     |
| ------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The policy file fails to parse             | **The gate refuses only "changes"; reading and thinking tools still pass**, so the agent can help you locate it. Look at the `policy.toml` diff and restore the last working version; or run `selftest`, which prints the parse failure reason |
| The agent can do nothing at all            | Rename`.github/hooks/orchestrator.json` to `.json.off` and reload the window, then let the agent fix it                                                                                                                                        |
| You want to let the machine edit gate code | Set`system.enabled` to `false` in `policy.toml`. ⚠️ **Only if the file itself parses**; if it is broken, use the first two rows first                                                                                                          |
| You want to stop using it entirely         | See "Uninstalling"                                                                                                                                                                                                                             |

### Common failures

| Symptom                                                                          | Root cause                                                                                     | What to do                                                                   |
| -------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| The hook does nothing at all                                                     | Configuration not hot-reloaded                                                                 | Reload the window; check`Developer: Show Agent Debug Logs`                   |
| The self-check reports an environment finding: the hooks do not point at`ocf.py` | The agent did not pick Work Orchestrator, the hook is broken, or the interpreter name is wrong | Fix`orchestrator.json` and reload                                            |
| The self-check reports a policy finding                                          | Policy missing or unparseable                                                                  | See "Self-rescue"                                                            |
| The self-check reports a system finding                                          | A canary failed, so the gate is no longer enforcing the policy                                 | **The most serious one.** Fix the policy as directed; do not route around it |
| The hook errors saying`$f` became empty in a command                             | The hooks command string was interpolated by the outer shell                                   | Remove every `$` from that string                                            |
| The gate keeps blocking the agent's file edits                                   | The state is not`executing` / `reporting`                                                      | Go through intake and planning, then run`approve` in your terminal           |
| A policy change has no effect                                                    | You edited a different file                                                                    | The policy file is`.github/ocf/policy.toml`; `status` prints it              |

### Design trade-offs and common questions

| Item                                                            | Notes                                                                                                                                                                 |
| --------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Failure detection is self-reported                              | The hook fires**after** a tool succeeded, so it cannot catch a failure on its own; it relies on the agent recording it plus the audit trail. **Not a hard guarantee** |
| `facts` is plain text                                           | The design does not consider a user routing around it maliciously                                                                                                     |
| Approval is enforced on the text layer, not by process identity | The design does not consider a user bypassing it maliciously                                                                                                          |
| "What counts as a substantive answer" has no code test          | Related to model quality; this project cannot lift a 3B model to the quality of a 512B one                                                                            |
| The gate's strength ceiling is set by the model                 | Related to model quality; the system constrains workflow and permission, not the quality of judgement                                                                 |

The remaining limitations are in the "Known limitations" section of `.github/work-control-flow.md`.

### Contact

- If you have opinions and questions, or feedback on the experience or a bug, please write to **liwenhu2y@outlook.com**.
- Or open an issue or a discussion at [GitHub]().

### Glossary

| Term                   | Chinese       | Meaning                                                                                                                                           |
| ---------------------- | ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| **State machine**      | 状态机        | What is allowed right now.`ready / asking / planning / executing / reporting / blocked`, six states plus the transitions between them             |
| **Gate**               | 门禁          | **A precondition of a state transition**                                                                                                          |
| **Rule**               | 规则          | The verdict for**one tool call**: `allow` / `ask` / `deny` / `require_approval`. First match wins                                                 |
| **Hard rule**          | 硬规则        | A rule the hook reads and answers with outside the conversation                                                                                   |
| **Soft rule**          | 软规则        | Natural-language semantics cannot be checked by code, so the model applies it from the standing guidance                                          |
| **Configuration**      | 配置          | The single file`.github/ocf/policy.toml`: rules, thresholds and tool classes all live there. Rules are data, so changing behaviour is a data edit |
| **Authorization**      | 授权          | The human runs it in their own terminal:`approve`                                                                                                 |
| **Protected list**     | 受保护清单    | `.github/protected.txt`                                                                                                                           |
| **Maintenance window** | 维护窗口      | `system.enabled`                                                                                                                                  |
| **Canary**             | 金丝雀        | A probe that runs through the real hook entry point; at least one must deny and one must allow, to prove the gate is**effective**                 |
| **Facts**              | 事实          | The elements the human supplies (goal, deliverables, environment declaration, audit result), written in`.orchestrator/facts`                      |
| **Intake**             | 受理          | The process of drawing the background out while in`asking`                                                                                        |
| **Payload**            | 载荷 / 中间树 | `release/payload/` is what gets published                                                                                                         |
