# System guide

Makes "no action before human approval" an unbypassable gate. Authorization uses no language
recognition: the human approves by running a command in their own terminal.

## 1. Layout

- `.github/ocf/ocf.py` is the only implementation: the state machine, the gates and the hook entry
  point. Python 3, standard library only, no build step. The entry point deliberately imports nothing
  third party, because VS Code starts it on the host, so a container cannot wrap it, and a missing
  import would make the gate fail open silently.
- `.github/ocf/policy.toml` is the only configuration: every rule, threshold and tool classification.
  There is no override layer, so what that file says is what the gate does. It is self-protected, so
  the human edits it by hand.
- `.github/hooks/orchestrator.json` wires SessionStart, UserPromptSubmit, PreToolUse and PostToolUse to
  `ocf.py hook`.
- `.github/ocf/tests/` holds the offline case table. Each case runs through the real entry point, so a
  gate that quietly stopped denying fails a test instead of looking healthy. The structural checks are
  the tuple `CHECKS` at the bottom of `run.py` - read it for the list rather than a copy of it here,
  which is what this bullet used to be and it had already fallen behind. It covers things that fail
  silently otherwise: markdown links that no longer resolve, agent names no prompt defines, templates
  that do not satisfy the schema they exist to satisfy, a policy using a key the engine does not read,
  and every generated region on disk matching what the code renders.
- `.github/assets/` holds the plan and report templates. `.github/copilot-instructions.md` is the model's
  standing contract and is generated; `.github/protected.txt` is the one protected list.
- `.orchestrator/` holds the runtime: `state` and `facts` (the hook writes both), `journal.log` and
  `exec.log` (the hook appends), `prompt-log` (the hook records every prompt), `plan.md` (written only
  when the human asks for a plan file), and `glossary.md` (written by the model, because the contract's
  soft rules tell it to). Leave the first four alone - they are the gate's own record. The protected
  list is NOT here: it is `.github/protected.txt`, because a list that is not versioned is absent on a
  fresh clone, and every rule still reads as if it were there.

## 2. Commands

Agent: status, set, gate, journal, fail, ok, init, selftest. advance targets are limited to asking,
planning, reporting, ready, blocked. set refuses approved_by and must_consult, and grill_rounds is
counted by the hook so it refuses that too.

Human terminal only: approve, reject, confirm, allow, deny, reload. `deny <path>` adds a path to the
protected list and `allow <path>` takes it off: `deny` protects, `allow` lets the machine touch it.
There is no second list and no exemption file. `reload` re-reads the configuration and regenerates the
three regions and one file built from it - the model's standing contract, the vocabulary region of
`policy.toml`, the reference region of this document, and the hook wiring. An agent able to run it
could edit the rules it is being asked to follow, which is why it is human-only.

The hook entry is `python .github/ocf/ocf.py hook` (or `python3` off Windows), which reads the event
JSON on stdin. Run `selftest` after any change to the policy or the hooks wiring: it reports findings
classified as environment, policy or system, and only system findings mean this code is at fault.

## 3. States

    ready -> asking -> planning -> executing -> reporting -> ready       bypass: blocked

ready to asking is the hook, on the human's first message. asking to planning is the agent, or the
human running confirm, behind the gates context, docs-decision and grill-valid. planning to executing
is the human running approve only, behind the four gates below. planning to asking is the human running
reject. executing to reporting, and reporting to ready, are the agent.

Two things that surprise people, both of them true and both of them once written the other way round:

- **The agent may enter and leave `blocked` by itself**, with no gate and no human, because that is
  what "stop and report" needs to mean. It is a state the agent declares, not a decision the human
  makes for it.
- **Only `executing` cannot be reached by the agent.** `advance` to any other target in the agent list
  succeeds whether or not it skips a state: the gate is on the transition, and only the two transitions
  above have one. Skipping is not blocked by a rule; it simply has nothing to check.

Only the human running approve enters executing. Acting - file edits and commands - is confined to
executing and reporting, with two exceptions: `.orchestrator/plan.md` is writable anywhere so a plan
can be given as a file during planning, and a command that only reads the orchestrator's own state
(`status`, `gate`, `journal`) is not acting and is never held up by approval. When a gate fails or
information is missing, go to blocked and wait rather than forcing it.

## 4. Rules

The gate is an interpreter and `policy.toml` is the program: each rule declares the conditions under
which it applies and what it answers, and the first match wins. That is why exemptions sit at the top
of the file and broad catch-alls sit at the bottom, and why a rule change is a data edit rather than a
code change.

Everything below this paragraph is rendered from the tables and the rules by `reload`. It is here
instead of being described because the description kept being wrong: the list it replaced named a rule
where a gate belonged, and claimed plan-schema wants eight sections when it reads two. If this section
disagrees with your expectation, this section is right.

<!-- OCF:REFERENCE:BEGIN -->
### Gates

A gate is a precondition of a state change, not a rule about tool calls. Failing one refuses the transition and names what is missing.

`asking -> planning`
- `context` - the five intake items are present and each at least 4 characters
- `docs-decision` - docs_decision is create or skip
- `grill-valid` - grill_rounds is at least 1, consensus at least 10 characters, grill_used is with-docs or me

`planning -> executing`
- `plan-schema` - a plan file, if one exists, carries `## Steps` and `## Files`, both non-empty. An absent file passes: the plan is a conversation artefact by default
- `zero-p0` - p0_count is 0
- `protected-list-clear` - the plan's `## Files` section names no path on the protected list
- `stack-env` - stack_env is declared, which means the human said what the environment is rather than the machine probing for it

### Tool classes

The class decides which rules are consulted and how a tool is judged. Membership is here rather than in prose because prose drifts; a tool not listed is `unknown`.

- `visual`: `screenshot_page`, `view_image`, `run_playwright_code`, `mcp_playwright_browser_take_screenshot`, `mcp_playwright_browser_run_code_unsafe`
- `always_allow` (never gated, and the strict fallback keeps both): `runSubagent`, `manage_todo_list`, `vscode_askQuestions`, `memory`
- `read_only` (never gated, and the strict fallback keeps both): `read_file`, `grep_search`, `file_search`, `list_dir`, `get_errors`, `copilot_getNotebookSummary`, `read_notebook_cell_output`, `vscode_listCodeUsages`
- `edit`: `create_file`, `create_directory`, `replace_string_in_file`, `multi_replace_string_in_file`, `edit_notebook_file`, `vscode_renameSymbol`, `mcp_github_mcp_se_create_or_update_file`, `mcp_github_mcp_se_delete_file`, `mcp_github_mcp_se_push_files`, `mcp_github_mcp_se_fork_repository`
- `exec`: `run_in_terminal`, `run_notebook_cell`, `mcp_playwright_browser_evaluate`
- `action`: `create_and_run_task`, `install_python_packages`, `install_extension`, `debug_java_application`, `configure_python_environment`, `create_new_workspace`, `create_new_jupyter_notebook`

`visual` is checked before `always_allow`, so a tool that both reads an image and is listed as always-allowed is still refused.

### Rules

In file order, first match wins. The conditions are named rather than quoted: the values live in `.github/ocf/policy.toml`, which is where they are meant to be read and changed.

- `plan-md-exempt` -> `allow` - The gate itself asks for this artifact. [class, path_matches]
- `failure-budget` -> `deny` - Consecutive failures reached the budget. Stop and report the symptom, what you tried, and what you need from the human. Clears when the human replies. [class, fact, is]
- `visual-tool` -> `deny` - The machine may never take screenshots or view images with a tool. Ask the human to attach one; attachments are readable, tools are not. [class]
- `visual-command` -> `deny` - The machine may never run visual or screenshot tests. [class, command_matches]
- `human-only-subcommand` -> `deny` - Authorization subcommands run only in the human's own terminal, even when the human asks. Point them at the rule or the config to change instead. [class, computed]
- `advance-target` -> `deny` - The agent may not advance to that state. Entering executing is a human act. [class, command_matches]
- `self-authorization-write` -> `deny` - Writing a human-only subcommand into an executable file is self-authorization. [content_matches, when=enforced]
- `self-protection-path` -> `deny` - The orchestrator's own files. The human edits them by hand, or sets system.enabled = false. [when=enforced]
- `self-protection-write` -> `deny` - A command may not write to the orchestrator's own files. [when=enforced]
- `protected-file` -> `deny` - That path is on the protected list. Only the human may change it. [class, touches_protected]
- `command-too-long` -> `deny` - Command too long. Split it into short single-purpose commands. [class, command_length_over]
- `too-many-statements` -> `deny` - Too many statements chained into one command. Split them and run one at a time. [class, command_statements_over]
- `silenced-output` -> `deny` - The command silences its output. Everything must stay visible to the human. [class, command_matches]
- `interactive` -> `deny` - The command may block on input or raise a dialog. Rewrite it non-interactively; anything needing elevation or a click is the human's job. [class, command_matches]
- `test-authorization` -> `deny` - Do not run tests on your own. Only after the human asks in this conversation, set test_authorized yes. [class, command_matches, fact, is_not]
- `toolchain` -> `deny` - The human has not declared the existing environment. Do not install or probe on your own. [class, command_matches, fact, is_set]
- `destructive` -> `deny` - Destructive command. Hand it to the human. [class, command_matches]
- `approval-required-edit` -> `require_approval` - File edits need approval. The human runs the approve subcommand in their own terminal. [approval, class]
- `approval-required-exec` -> `require_approval` - Commands need approval. The human runs the approve subcommand in their own terminal. [approval, class, unless]
- `approval-required-action` -> `require_approval` - This tool performs an action and needs approval. [approval, class]
- `repeat` -> `ask` - The same command again, which suggests you are stuck in a loop. The human decides whether to continue. [class, command_repeats_at_least]
- `intake-grilling` (soft, occasion `ask`) - 从人类回复和上下文推出规定的8项背景信息
- `independent-audit` (soft, occasion `plan`) - 审计交给子代理，且 P0 为零时只交报告、不推进状态。
- `no-screenshots` (soft, occasion `act`) - 永久禁用视觉类工具
- `beginner-mode` (soft, occasion `ask, answer`) - 讲清原语并沉淀术语表，也在人类说不清时帮他把话理顺
- `human-only-commands` (soft, occasion `act`) - 说清人类专属命令的立场，因为人类在对话里提出要求时，模型的默认倾向是照办。
- `plan-file` (soft, occasion `plan`) - 默认对话不落盘。
- `grill-with-docs` (soft, occasion `plan`) - 质询必须对准项目已有的语言和已定决策
<!-- OCF:REFERENCE:END -->

Some behaviour that is not a rule and so is not in the list above:

- Runtime gates run on PreToolUse. They block by writing `permissionDecision: deny` to stdout and
  exiting 0, except going in circles which answers ask. Never rely on the exit code and never write to
  stderr, and a broken guard fails safe by denying.
- PostToolUse is a reporter rather than a gate. When a terminal command returns with no progress - a
  shell sitting at a continuation prompt, or a command moved to the background - it injects a warning
  saying the command never reported a result, and journals it. Text rules cannot predict that an
  unclosed quote will hang a shell, because the gate reads the command instead of parsing the shell;
  what it can do is notice the aftermath, so a silent hang becomes a record instead of a mystery.
- A tool the policy does not classify is judged by what it carries: if it carries a command it is
  treated as an exec tool, so a tool added later cannot create an uninspected path. If it only carries
  a path it is left to the name heuristic, so read tools are not dragged into the write rules. An
  exec tool whose command cannot be read at all is denied rather than allowed blind.
- Control-plane is recognised only when every statement invokes the control script by its full relative
  path, so merely mentioning the name does not inherit the exemption.
- Which tools are never gated, which are gated, and which are refused unconditionally follows from
  their class in the list above. A screenshot the human attaches is readable; the screenshot tool is
  not, in any state.

## 5. Intake

The human is never handed a form. They are offered three sections to say in their own words: the goal in
one sentence, the requirements (the reference design and the process steps), and the deliverables. The
agent derives the eight plan sections from that plus the context it can observe. The facts below are what
the agent records for the human to correct, not what it demands one item at a time; only what cannot be
derived is grilled, one question per turn. Never guess a value and never decide for the human. Facts, with
what each gate needs: goal, tools, references, deliverables and code_style at least 4 characters each,
docs_decision create or skip, stack_env at least 4 characters and declared by the human rather than probed
by the agent, grill_used with-docs or me, consensus at least 10 characters. grill_rounds is counted by the
hook and must not be written.

A perfunctory or automatic reply is not an answer. Re-ask, do not advance, never decide for the human.
You make that judgement yourself; the system does no keyword matching.

## 6. Planning and approval

**The plan is given in chat by default.** A file is written only when the human asks for one. An
absent `.orchestrator/plan.md` is not a failure - plan-schema passes with a note saying where the plan was
given - because the approval is the human's, not the file's. What the human is asked for is the intake
described in section 5; it is not restated here, because a second copy of it is a second thing to keep
in step. When such a file is written it carries all
eight sections of the template at `.github/assets/plan-template.md`:

    ## Type  ## Summary  ## Steps  ## Tools  ## Files  ## Scope  ## Deliverables  ## Self-review

Of those, only `## Steps` and `## Files` are required, because only they are read: plan-schema checks
that both are present and non-empty, and protected-list-clear compares Files against the protected
list, so that section must name the exact paths to change. The list of allowed `Type` values lives in
the template - read it there rather than here, where it had already lost one. The
report has its own template at `.github/assets/report-template.md`; both template files are in English and
the headings stay in English, while the agent fills every other line in the language the human is using.

What counts as P0, P1 and P2 is defined in `.github/agents/plan-auditor.agent.md`, which is the agent
that applies it. It is not repeated here: the copy that used to be here had drifted out of step with
the one the auditor actually reads.

    write the plan -> dispatch plan-auditor -> any P0? yes -> revise and re-audit
                                              no  -> set p0_count 0

Then give the human two reports in chat, unless they want a file: the action report, and the risk
design report covering risks, triggers, mitigations, residual risk and rejected alternatives. Hand
them this verbatim and stop:

    python .github/ocf/ocf.py approve "<one-sentence reason>"

Only the human running it in their own terminal enters executing. Do not run it for them, and do not
set approved_by.

## 7. Terminal discipline

Visible and traceable, never a hung, confused or frantically iterating black box. Keep commands short
and to at most three statements - the ceilings are numbers inside the rules that use them
(`command_length_over = 400`, `command_statements_over = 3`), not keys in a shared table, so a rule and
its limit cannot drift apart. Keep output visible, and send one short single-line command at a time,
because the output ownership of a multi-line paste is unreliable. To filter, limit rows rather than
discarding all output. For a real exit code use a separate child process, since a parent shell keeps a
stale LASTEXITCODE.

fail_budget consecutive failures - two, from `[limits] fail_budget` - sets must_consult and locks every
exec-class tool until the human replies. Record with `ocf.py fail "<reason>"`, reset with `ocf.py ok`.

Pitfalls already stepped on. The first group still governs the workflow; the second is carried over
from the PowerShell and sh implementations that ocf.py replaced. They are kept because the human's
terminal is still PowerShell and because the traps are easy to walk into again, not because the gate
still runs them.

- PreToolUse must block through stdout permissionDecision, because stderr with a non-2 exit code is
  only a non-blocking warning.
- The hooks command string must contain no `$`, since the outer shell interpolates it.
- A bare function call at the start of a condition is parsed as a command, so
  `if (Test-Enforced -and -not $isOcf)` degenerates to that function's output and the variable is
  ignored. Write `if ((Test-Enforced) -and (-not $isOcf))`. This trap once made the control-plane
  exemption dead code.
- A function unwraps a one-element array, so `$lines[0]` degrades to a character. Wrap the call site in
  `@(...)`.
- Reading a BOM-less UTF-8 file without -Encoding decodes it as cp936 under PowerShell 5.1, mangling
  text and sometimes swallowing a newline. Use the explicit readers and verify under `powershell` 5.1,
  because `pwsh` 7 hides it. A BOM in config once broke the `^key=` anchors, so reads tolerate one.
- In a `tr` set the `-` may only be first or last.
- After byte truncation strip a partial character with `LC_ALL=C sed 's/[\200-\277]*$//'`.
- `$"..."` is not valid interpolation; write `"...: $($x)"`.
- PowerShell scripts take no pipeline input; pipe into `pwsh -NoProfile -File script.ps1`.
- A non-zero `exit` in an `&`-invoked script is downgraded; use `[Environment]::Exit(2)` and `-File`.

The implementation and every agent-facing document in `.github` are ASCII, with three named exceptions
in `run.py`'s `NON_ASCII_ALLOWED`: `policy.toml` and `copilot-instructions.md`, which are prose addressed
to the human and the model in the language the project is run in, and `run.py` itself, which has to be
able to assert on that text. `ocf.py` is deliberately not exempt, because its output is what reaches a
console that may not be UTF-8. Everything else, a new file included, is checked and would have to argue
for itself there.

## 8. Delivery

In executing, follow the approved plan with no unapproved changes; to deviate, advance blocked and
re-plan. Use the diagnose skill when something breaks, with criterion-picker choosing the reproduction
method, and leave the visual part to the human.

In reporting, give the change list, statistics, unfinished items with reasons, and the audit summary
from `ocf.py journal 20`, quoting journal.log and facts rather than memory, then advance ready.

Do not run tests unless the human asks in this conversation, and set test_authorized yes first. Never
run visual tests.

## 9. Limits and switches

There are two kinds of rule, and they are enforced in two different places. A **hard** rule is read by
the hook, outside the conversation, and answers with a verdict. A **soft** one cannot be enforced that
way - nothing can check whether a sentence was written - so `reload` writes it into the standing
contract the model reads on every turn, and the model applies it. Both are written the same way in
`.github/ocf/policy.toml`: `enabled`, `kind`, `if` (the occasion it hits), `unless` (the exception),
`result` (the logic) and `why`. For a hard rule `result` is a verdict; for a soft rule it is the
sentence itself.

`python .github/ocf/ocf.py reload` is human-only, because it regenerates everything built from the
configuration: the model's standing contract, the vocabulary region of `policy.toml`, the reference
region of this document, and the hook wiring. An agent able to run it could edit the rules it is being
asked to follow. It validates the whole policy first and changes nothing if the policy is broken, since
half-applied configuration is worse than none.

`[hooks]` is the install switch: `enabled = false` makes reload write a wiring file with no hooks in
it, so VS Code stops starting this program at all - uninstalling is not something left half-done. Each
event has its own switch as well, and the entry point reads them too, because the wiring file is only
re-read when the window reloads. `pre_tool_use` is the gate; the other three only add context.

`[limits]` holds one threshold, `fail_budget = 2`. Every other ceiling lives inside the rule that uses
it. Shared keys do remain elsewhere - `[approval] allowed_states` and `min_reason_len`,
`[unknown_tool] action`, the `[hooks]` switches, `[paths] state_dir` - and they are listed with what they
mean in the generated region above. The file is read on every invocation, so a change to it applies
immediately; only `.github/hooks/*.json` needs a window reload.

`system.enabled` is the maintenance switch, and it is NOT the whole gate - which is easy to misread in
both directions. What it turns off is exactly the rules whose condition mentions it: the three with
`when = "enforced"` (the two that protect the orchestrator's own files and the one against writing a
human-only subcommand into a script) and the three approval rules, because `approval = true` asks
whether a human approve is outstanding, and with the switch off nothing ever is. Every other rule has
no such condition and holds either way. So `false` hands back the ability to edit gate code, and also
quietly stops requiring approval - do not treat it as "only the self-protection is off".

The protected list is judged by a rule with no `when`, so it keeps applying with the switch off; that is
the one protection that cannot be turned off from inside the file it protects.

An unreadable or malformed policy keeps the gate on by falling back to a policy that denies every change
while still allowing reading and the session tools. That last part matters: a gate that refuses
to let anyone read it is a gate nobody can repair, and a one-character typo once denied even `read_file`.
A missing policy file also reports itself as a policy finding from `selftest`, never as silence.

Length is counted in characters, once, on every platform, because there is only one implementation.

The editor must not answer questions on the human's behalf. Verified against the VS Code core bundle: when
the agent asks a question and either the chat permission level is `autopilot` or the setting
`chat.autoReply` is true, VS Code **injects a reply itself**. The injected text is fixed ("The user is
not available to answer your question..."), but the agent cannot rely on recognising it, and neither
can the gate: an injected reply and a human one both arrive as UserPromptSubmit. So this is a
**prerequisite, not a rule**: keep the permission level out of autopilot, and set `chat.autoReply` to
false in user settings. With both off, a question genuinely waits for the human. See section 10.

The selected level lives in `chat.permissions.default`, and the picker labels the ones a build offers as
**Default permissions** (`.default`), **Allow all** (`.autoApprove`) and **Autopilot (Preview)**
(`.autopilot`); only autopilot triggers the injection. Default permissions is described as "Use
configured approval settings", so the granular `chat.tools.*` keys are honoured there - you do not have
to pick Allow all in order to control approvals. So the working configuration is **Default permissions
(or Allow all) plus `"chat.autoReply": false`**: questions then wait for the human, and whether commands
prompt is left to the approval settings you configure. Setting `chat.autoReply` alone is NOT enough
while the level is still autopilot, because the triggering condition is an OR.

## 10. Known limitations

- Failure detection is self-reported: the hook fires after a tool succeeded, so `ocf.py fail` has to
  be called by the agent.
- facts is plain text and guards against slips, not a malicious actor.
- The write-path scan is a write-keyword heuristic, backstopped by the approval gate while
  `system.enabled` is true.
- Approval is enforced on the text layer, not by process identity, so base64-encoding the call or
  reusing a file that already contains it can still get through. A TTY check does not help, because the
  agent's terminal is a real terminal too.
- The unknown-tool policy is name-based, so a tool with no action verb in its name slips through
  unless it carries a command.
- Whether the gate is still alive is proved by canaries, and canaries only run when something runs
  them: SessionStart and `selftest`. If hooks stop being delivered at all, the absence of the
  self-check is the only signal, and the absence of a signal is easy to miss.
- The gate cannot tell a human reply from an injected one. Both arrive as UserPromptSubmit, so
  "the human has replied" is not something any rule can currently establish; it is enforced only by
  turning the injection off in the editor (section 9). Corollary: an injected reply also clears the
  failure budget, because clearing is tied to the same event.
- Auto-approve does not weaken this gate. `deny` is handled before any approval logic, so nothing can
  auto-approve past it; a hook answering `ask` also looks protected, by an explicit branch that
  discards pre-approval when the decision is `ask`. That branch is read from the editor bundle rather
  than verified end to end, so treat `ask` as a hint and put anything that must hold behind `deny`.
- Reading is never gated, which is what keeps a broken gate diagnosable. The strict fallback refuses
  every change and still allows the read and session tools; the read list is what makes
  that true, so removing an entry from it removes a recovery path.
- Inline agent hooks would scope enforcement to one agent, but they need the chat.useCustomAgentHooks
  setting, and if it is off the gate silently stops working, so the workspace hook stays the default.
