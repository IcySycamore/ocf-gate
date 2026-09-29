<div align="center">

# OCF Gate

### Putting a traffic light on the AI Agent

**Exploring the collaboration and the boundary of human & agent**

<p>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.x-3776AB?logo=python&logoColor=white">
  <img alt="Platform" src="https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-4B5563">
  <img alt="Host" src="https://img.shields.io/badge/Host-VS%20Code%20Copilot%20Chat-007ACC?logo=visualstudiocode&logoColor=white">
  <img alt="Dependencies" src="https://img.shields.io/badge/Dependencies-stdlib%20only-success">
  <a href="https://github.com/IcySycamore/ocf-gate"><img alt="GitHub" src="https://img.shields.io/badge/GitHub-IcySycamore%2Focf--gate-181717?logo=github&logoColor=white"></a>
</p>

[**English**](README.md) · [中文](README_CH.md)

</div>

## Table of contents

- [Project introduction](#project-introduction)
- [Advantages](#advantages)
- [The state flow](#the-state-flow)
- [The pre-configured rules](#the-pre-configured-rules)
- [Composition](#composition)
- [Project structure](#project-structure)
- [Environment dependencies](#environment-dependencies)
- [Installation and deployment](#installation-and-deployment)
- [Getting started](#getting-started)
- [Experimental](#experimental)
- [Future plans](#future-plans)
- [Appendix](#appendix)

---

## Project introduction

As agent-assisted development increasingly becomes part of a computer-industry practitioner's work, this project works to:

1. **Explore** the human & agent collaboration workflow, and the agent's **permission boundary**.
2. **Provide** a system. Through a whole built from well-designed prompts, a state machine and so on, it effectively raises the human-agent collaboration experience, distils the human collaboration steps that are necessary, balances **efficiency, quality and accuracy**, and solves the problems that come from the agent and the human disagreeing: the agent overstepping, behaving unpredictably, answering badly.

> See the glossary in the [appendix](#glossary)

### 😫 Have you hit these problems?

The most common accident with an AI agent is not that it cannot do the work, but that it does not understand human intent — and that is not only about model quality.

| Symptom                        | Typical sign                                                                                                                                                                                                                     |
| :----------------------------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **No shared understanding**    | Starts before the requirement is clear, so the deliverable does not match what was expected, wasting a great deal of time and tokens and making the human see red                                                                |
| **Out of bounds**              | Edits files, sets up environments and creates directories before approval, runs commands silently or retries forever, causing enormous and unrecoverable damage, and overwrites the comments and code the human wrote by hand    |
| **Poor answers**               | Full of syntax, logic, factual and comprehension errors; hallucination, not answering the question, repetition, meaninglessness, over-explaining (raising the barrier to understanding) or too shallow (burning too many tokens) |
| **Bad collaboration**          | Work with no order, no plan, no report; hard to communicate with                                                                                                                                                                 |
| **Chain-of-thought pollution** | Technical detail written into a user-facing UI, the model's reasoning, its mistakes and its trade-offs written into comments, poor writing in the deliverable                                                                    |

> [!TIP]
> Sometimes it is not the agent's fault at all, but the human being too lazy, or not knowing how, to design a good prompt
> and to state the working elements clearly.
>
> Out of a large amount of real project experience, this system standardises and streamlines the human-agent
> workflow, placing a degree of demand and limit on both the human and the agent, so as to reach the best
> collaboration and, from there, a better deliverable.

---

## Advantages

| Feature                                                             | What it gives you                                                                                                                                                 |
| ------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **A hard gate plus soft guidance**                                  | Hooks are designed to build the workflow and constrain the model's behaviour, and light guidance words focus the model's attention on what matters                |
| **A light core**                                                    | A single .py file at the core, standard library only, cross-platform                                                                                              |
| **Approval does not rest on understanding, and cannot be bypassed** | Only a human running the`approve` command in their own terminal can approve                                                                                       |
| **The protected list**                                              | A protected directory limits the model's access to files, and self-protects the system                                                                            |
| **A rules and configuration system**                                | 21 hard rules and 12 soft rules distilled from real project experience, managed conveniently as key-value toml, able to switch the whole system on or off at once |
| **The terminal stays in the human's hands**                         | It blocks execution before approval, long commands, multi-statement chains, silenced output, interactive blocking, repeated execution and consecutive failures    |
| **Auditable end to end**                                            | State transitions, authorizations and failures are all visible in`.orchestrator/journal.log`                                                                      |
| **Classic tests built in**                                          | Self-checks at deployment, at the start of a conversation and on every rule reload, so the system never fails silently                                            |
| **Easy to deploy, package and migrate**                             | Convenient to deploy; one command migrates the system and its state                                                                                               |

> Other features are for you to find!

---

## The state flow

How the transitions work: which part of the system performs them · and when

```mermaid
stateDiagram-v2
ready --> asking: hook, when the human sends a message
asking --> ready: agent, when the human only asks questions
asking --> planning: agent or human, when the agent believes follow-up questions are over and all necessary elements are in place, or when the human directly approves (never recommended)
planning --> executing: human, when the human approves and all planning elements are in place
executing --> reporting: agent, when the agent executes the plan
reporting --> ready: agent, when the agent reports the work
planning --> asking: human, when the human rejects the plan

```

> [!NOTE]
>
> - When the agent believes, or the system in fact hits, a block, it moves from any state to `blocked`; when the agent believes the block is resolved, it leaves.

> [!CAUTION]
> - You may change the state machine to suit your needs.  A more complex state machine and more sub-agent identities may bring too much time and cost overhead, and a distracted model may make the result worse than the design expected.

---

## The pre-configured rules

### Hard rules

Run by the hook outside the conversation, which is where the verdict is given.

| Rule                       | What it does                                                                                          | Default            |
| -------------------------- | ----------------------------------------------------------------------------------------------------- | ------------------ |
| `plan-file-writable`       | Allows each round's plan to be saved as a file                                                        | follows the switch |
| `failure-budget`           | Blocks once consecutive failures reach the limit, and waits for the human                             | follows the switch |
| `visual-tool`              | Disables vision tools                                                                                 | follows the switch |
| `visual-command`           | Disables vision commands                                                                              | follows the switch |
| `human-only-subcommand`    | The agent may not run human commands for the human                                                    | follows the switch |
| `advance-to-executing`     | The agent may not enter the `executing` state                                                         | follows the switch |
| `self-authorization-write` | The agent may not create a human-command script                                                       | follows the switch |
| `unread-write-target`      | Disables modifying commands whose target path cannot be parsed (experimental)                         | follows the switch |
| `touches-protected`        | No editing a file on the protected list (relies on target-path parsing)                               | follows the switch |
| `command-too-long`         | No command over the character limit                                                                   | follows the switch |
| `too-many-statements`      | No command over the statement limit (experimental)                                                    | follows the switch |
| `silenced-output`          | No silenced terminal commands                                                                         | follows the switch |
| `interactive-command`      | No interactive commands                                                                               | follows the switch |
| `test-authorization`       | No unauthorized tests                                                                                 | follows the switch |
| `undeclared-env-command`   | No installing dependencies or probing the toolchain before the human declares the environment         | follows the switch |
| `destructive`              | No destructive commands (experimental)                                                                | follows the switch |
| `undeclared-env-tool`      | No installing packages / extensions / scaffolding before the environment is declared                  | follows the switch |
| `approval-required-write`  | Editing a file outside an acting state needs approval                                                 | follows the switch |
| `approval-required-exec`   | Running a command outside an acting state needs approval (exempt when only reading the control plane) | follows the switch |
| `approval-required-env`    | Changing the environment outside an acting state needs approval                                       | follows the switch |
| `repeated-command`         | Blocks when a repeated command passes the limit, and waits for the human                              | follows the switch |

### Soft rules

Natural-language semantics that no code can check; the standing guidance gets the model to apply them on its own.

| Rule                     | What it does                                                                                                              |      Default       |
| :----------------------- | :------------------------------------------------------------------------------------------------------------------------ | :----------------: |
| `intake-grilling`        | One question per turn during intake, to sharpen the questioning                                                           | follows the switch |
| `independent-audit`      | Audits the plan independently until P0 reaches zero, then asks for the human's approval command                           | follows the switch |
| `no-screenshots`         | No vision tools (when switched off, name the vision practice instead)                                                     | follows the switch |
| `beginner-mode`          | Explain every domain primitive and record it in the glossary; a vague request gets the three-part question                | follows the switch |
| `human-only-commands`    | The agent may not run human commands for the human                                                                        | follows the switch |
| `write-for-the-reader`   | Guides the agent to leave blanks and tune the text in the artifact to its reader (experimental, depends on model quality) | follows the switch |
| `a-way-back`             | Guides the agent to keep an archive or a rollback for medium and high risk                                                | follows the switch |
| `plan-write-file`        | Gives the plan as a file                                                                                                  |      **off**       |
| `plan-template`          | Writes the plan with the recommended template                                                                             |       **on**       |
| `report-template`        | Writes the report with the recommended template                                                                           |       **on**       |
| `grill-with-docs`        | Questions against `CONTEXT.md` and `docs/adr/`                                                                            | follows the switch |
| `question-is-not-a-task` | When the human only asks a question, return to `ready` once it is answered                                                | follows the switch |

---

## Composition

| Part                   | Where                             | What it does                                                                                                                                                                                                             |
| ---------------------- | --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **State machine**      | `TRANSITIONS` in `ocf.py`         | `ready → asking → planning → executing → reporting → ready`, bypass: `blocked`                                                                                                                                           |
| **Command units**      | the command table in`ocf.py`      | The agent may use`status` / `set` / `gate` / `journal` / `fail` / `ok` / `advance` / `init` / `selftest` / `verify`; the human may use `approve` / `reject` / `protect` / `unprotect` / `reload` / `install` / `package` |
| **Persistence**        | `.orchestrator/`                  | `state` the machine state, `facts` the distilled working elements, `glossary.md` the terms, `journal.log` the audit, `exec.log` the command-repeat record, `prompt-log` the intake record, `plan.md` the plan as a file  |
| **Configurable rules** | `.github/ocf/policy.toml`         | 21 hard rules + 12 soft rules                                                                                                                                                                                            |
| **VS Code hook**       | `.github/hooks/orchestrator.json` | Four events at`ocf.py hook`: SessionStart / UserPromptSubmit / PreToolUse / PostToolUse                                                                                                                                  |

---

## Project structure

```text
.
├─ .github/                    交付单元
│  ├─ ocf/
│  │  ├─ ocf.py                状态机、门禁、规则求值、hook 入口
│  │  ├─ policy.toml           规则
│  │  └─ tests/run.py          离线用例 + 结构断言
│  ├─ hooks/orchestrator.json  四个事件的接线
│  ├─ agents/  prompts/        子 agent 与可用 skill
│  ├─ assets/                  计划与报告模板
│  ├─ protected.txt            保护清单
│  ├─ copilot-instructions.md  常驻引导词
│  └─ work-control-flow.md     说明书(for agent)
├─ .orchestrator/              运行时
│  ├─ state  facts             状态机状态 与 工作要素
│  ├─ journal.log  exec.log    审计 与 命令重复记录
│  └─ glossary.md  plan.md     术语 与 本轮计划
├─ release/
│  ├─ payload/                 发布载荷
│  └─ build/                   打包、构建和测试套件
├─ dist/                       产物与中间树
├─ README.md  README_CH.md
├─ POLICY-GUIDE.md  POLICY-GUIDE_CH.md
└─ VERSION
```

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

```bash
# 1) Copy .github/ into the target repository root

# 2) Initialise the runtime
python  .github\ocf\ocf.py init      # Windows
python3 .github/ocf/ocf.py init      # other platforms

# 3) Register the paths this repository must protect
python  .github\ocf\ocf.py protect "src/**"

# 4) Self-check
python  .github\ocf\ocf.py selftest

# 5) Generate the configuration into the artifacts
python  .github\ocf\ocf.py reload

# 6) Reload the VS Code window, so the hooks take effect
```

> [!IMPORTANT]
> Out of the box the master switch `system.enabled` in `policy.toml` is `false`. After deploying, set it to `true` first, then run `reload`.

### Confirm the deployment

```bash
python  .github\ocf\ocf.py verify      # deployment usability check
python  .github\ocf\ocf.py selftest    # gate status check
```

---

## Getting started

Just tell the agent your task.

The human-facing skills this system provides: `/work-intake`, `/work-plan`, `/bug-route`.

### Session start

- **User**: 🦜 says something witty 🦜
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

## Experimental

### Reader judgement test

Before switching `write-for-the-reader` on, you can first test the model's ability.

50 model use-case scenarios are prepared.
The model tells each artifact's target audience (the reader) by `write-for-the-reader`.

**HOW TO**

1. Run `python release\build\reader_eval.py` and take the questions;
2. Send the questions to **the model you want to measure**, and save its answers as JSON in the format the script gives;
3. Run `python release\build\reader_eval.py --answers <file_name>.json` for the accuracy and every MISS.

---

## Future plans

In order...

| Item                                      | Notes                                                                                          |
| ----------------------------------------- | ---------------------------------------------------------------------------------------------- |
| **Tests**                                 | Close the coverage gaps for the hard rules; quantify the soft rules, or mark them unmeasurable |
| **New and improved features** (long term) | Keep distilling rules and gates from the suggestions collected                                 |
| **Adapt to the DeepSeek harness**         | Stop the gate depending on VS Code's hook events                                               |
| **Adapt to Codex**                        | As above                                                                                       |
| **Publish as a VS Code extension**        | —                                                                                              |

---

## 📎 Appendix

### Release

Produce every artifact:

```powershell
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

**Key**

| Command                              | What it does                                                                                                        |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------- |
| `python .github\ocf\ocf.py reload`   | Rebuilds the standing guidance, the vocabulary region of`policy.toml`, and the reference region of the system guide |
| `python .github\ocf\ocf.py selftest` | Checks the environment, the policy, the hooks and the gate status                                                   |
| `python .github\ocf\tests\run.py`    | The offline cases and structural checks in the delivery set                                                         |

**Others**

| Command                                | What it answers                                            |
| -------------------------------------- | ---------------------------------------------------------- |
| `python .github\ocf\ocf.py status`     | State machine state, facts, configuration elements         |
| `python .github\ocf\ocf.py journal 20` | The audit: state transitions, authorizations, failures     |
| `python .github\ocf\ocf.py gate`       | Runs the gates: whether a transition is possible right now |
| `python .github\ocf\ocf.py verify`     | Deployment usability check                                 |

> [!WARNING]
> After editing `.github/ocf/**`, set the entries you want to enable in the configuration to `true` first, then run `reload`.

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

### Common problems and suggestions

| Symptom                                                                       | What to do / Notes                                                                                                                                                                                                                  |
| ----------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The agent can do nothing / I broke the whole system                           | Rename `.github/hooks/orchestrator.json` to `.json.off` and reload the window, then let the agent handle it or reinstall                                                                                                            |
| The hook does not react at all                                                | The configuration was not hot-reloaded. Reload the window; check `Developer: Show Agent Debug Logs`                                                                                                                                 |
| Self-check reports an environment finding: the hooks do not point at `ocf.py` | The agent did not pick Work Orchestrator, the hook is broken. Pick that agent, or fix `orchestrator.json` and reload                                                                                                                |
| Self-check reports a policy finding                                           | **The gate refuses only "changes"; reading and thinking tools still pass**, so the agent can help you locate it. Look at the `policy.toml` diff and restore the last working version; or `selftest` prints the parse failure reason |
| Self-check reports a system finding                                           | A canary failed = the gate no longer enforces the policy. Fix the policy as directed; do not route around it                                                                                                                        |
| The hook errors saying `$f` became empty in a command                         | The hooks command string was interpolated by the outer shell. Remove every `$` from that string                                                                                                                                     |
| The generated-region assertion always misfires                                | An editor or formatter reformatted after reload. Run reload once, or ignore it                                                                                                                                                      |
| Approval is decided at the text layer, not by process identity                | The design does not consider deliberate bypass                                                                                                                                                                                      |
| "What counts as a substantive answer" has no code test                        | Related to model quality; the cost of adding guidance words outweighs the effect                                                                                                                                                    |
| The gate's strength ceiling is set by the model                               | Related to model quality                                                                                                                                                                                                            |
| My agent still seems hopelessly stupid                                        | Confirm `Work Orchestrator` is selected both at the start of the conversation and now                                                                                                                                               |

The remaining limitations are in the _Known limitations_ section of `.github/work-control-flow.md`.

> [!NOTE]
> And...
>
> You must, absolutely must, be patient with the agent. Do not lose your temper at the agent's "stupidity" and so lower the quality of your own prompt.
> While your agent is working, watch what it is doing and how it is progressing - so that you can brake or correct it in time.
> After all, an AI agent is only a tool Orz

### Contact

- If you have opinions and questions, or feedback on the experience or a bug, please write to **liwenhu2y@outlook.com** ;)
- Or open an issue or a discussion at [GitHub](https://github.com/IcySycamore/ocf-gate).

### Glossary

| Term                   | Chinese       | Meaning                                                                                                                       |
| ---------------------- | ------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| **State machine**      | 状态机        | `ready / asking / planning / executing / reporting / blocked` six states, plus the transitions between them                   |
| **Gate**               | 门禁          | **A precondition of a state transition**                                                                                      |
| **Rule**               | 规则          | The verdict for **one tool call**: `allow` / `ask` / `deny` / `require_approval`. First match wins                            |
| **Hard rule**          | 硬规则        | A rule the hook reads and answers with outside the conversation                                                               |
| **Soft rule**          | 软规则        | Natural-language semantics cannot be checked by code, so the model applies it from the standing guidance                      |
| **Configuration**      | 配置          | The configuration file `.github/ocf/policy.toml`                                                                              |
| **Authorization**      | 授权          | The human runs `python ocf.py approve`                                                                                        |
| **Protected list**     | 受保护清单    | `.github/protected.txt`                                                                                                       |
| **Maintenance window** | 维护窗口      | The `system.enabled` item in the configuration                                                                                |
| **Canary**             | 金丝雀        | A hook probe case; at least one deny and one allow                                                                            |
| **Facts**              | 事实          | The working elements the human supplies (goal, deliverables, environment declaration, audit result), in `.orchestrator/facts` |
| **Intake**             | 受理          | The process of drawing the background out while in `asking`                                                                   |
| **Payload**            | 载荷 / 中间树 | The `release/payload/` used for publishing                                                                                    |

---

<div align="center">

**⭐ If this project is helpful, give it a star to show your support PLZ!! ⭐**

<sub>Made with ❤️ for better human–agent collaboration</sub>

</div>
