# System guide

Makes "no action before human approval" an unbypassable gate. Authorization uses no language
recognition: the human approves by running a command in their own terminal.

## 1. Layout

- `.github/ocf/ocf.py` is the only implementation: the state machine, the gates and the hook entry
  point.
- `.github/ocf/policy.toml` is the only configuration: every rule, threshold and tool classification.
- `.github/hooks/orchestrator.json` wires SessionStart, UserPromptSubmit, PreToolUse and PostToolUse to `ocf.py hook`.
- `.github/ocf/tests/` holds the offline case table. Each case runs through the real entry point, so a gate that quietly stopped denying fails a test instead of looking healthy. The structural checks are
  the tuple `CHECKS` at the bottom of `run.py` - read it for the list rather than a copy of it here,
  because the list changes and a copy does not: that is how the bullet this replaced ended up claiming
  the wrong number of them. It travels inside the payload, so it holds only what a person who DEPLOYS
  this gate can break: their policy, their tool list, their rules, their protected list, their documents.
  Checks on the engine itself exist too, but they live beside the build and are not shipped; the build
  runs both before it packages anything. Between them they cover what fails silently otherwise: markdown
  links that no longer resolve, agent names no prompt defines, section numbers a renumbering left
  pointing at the wrong section, frontmatter that does not parse, templates that do not satisfy the
  schema they exist to satisfy, a policy using a key the engine does not read, and every generated
  region on disk matching what the code renders.
- `.github/assets/` holds the plan and report templates. `.github/copilot-instructions.md` is the model's
  standing contract and is generated; `.github/protected.txt` is the one protected list.
- `.orchestrator/` holds the runtime: `state` and `facts` (the hook writes both), `journal.log` and
  `exec.log` (the hook appends), `prompt-log` (the hook records prompts while the intake is open; the
  transcript, and the three facts the intake is judged by, are cleared when the machine leaves
  `asking` - entering `asking` opens a fresh intake, and `blocked` is an interruption rather than an
  end), `plan.md` (written only
  when the human asks for a plan file), and `glossary.md` (written by the model, because the contract's
  soft rules tell it to). Leave the first four alone - they are the gate's own record. The protected
  list is NOT here: it is `.github/protected.txt`, because a list that is not versioned is absent on a
  fresh clone, and every rule still reads as if it were there.

## 2. Commands

Agent: status, set, gate, journal, fail, ok, advance, init, selftest, verify. `verify` asks whether this deployment is usable - installed, wired, current, and answering - where `selftest` asks whether the gate is armed and alive right now. The two differ in exactly one place, an open maintenance window, and
`verify` reads it as the state a fresh deployment is checked in rather than as a fault. advance targets
are limited to asking, planning, reporting, ready, blocked. set refuses approved_by and must_consult,
and grill_rounds is counted by the hook so it refuses that too.

Human terminal only: approve, reject, protect, unprotect, reload, install, package.
`approve "<reason>"` opens the gate
in front of the machine: in asking it runs the three intake gates and moves to planning, in planning the
four below and moves to executing. Either way out of `asking` closes the intake: its transcript, and
the three facts it was judged by, are cleared. One verb for both, because the state already says which gate that is,
and because a row of three near-synonyms read as one thing with three names. `reject` goes back to
asking. `protect <path ...>` adds entries to the protected list and `unprotect <path ...>` takes them off:
`protect` protects, `unprotect` lets the machine touch it. A pattern that matches a **directory** covers
everything beneath it, and every entry is reported back with the number of files it actually covers -
an entry that covers nothing is a request that was not granted. There is no second list and no
exemption file.
`reload` re-reads the configuration and regenerates the four things built from it: the model's standing
contract, the vocabulary region of `policy.toml`, the reference region of this document, and the hook
wiring. It is human-only, for the reason given in section 9 (limits and switches).
`install <target-dir>` copies this whole gate into another directory and is human-only for the same
reason: it writes files where no rule can see the write, so nothing would stop `ocf.py install .` from
replacing the gate's own source with a copy of itself. The target's own `policy.toml` and
`protected.txt` are never replaced - the shipped version is written beside them as `.dist`.
`package <output-dir>` writes the release tree - the payload, the bootstrap from `release/payload/`, and
`VERSION` - for the builder to archive and compile into an installer. It is a maintainer's command and
only works in a source checkout, which is why it is the one human command a deployed copy cannot run.
JSON on stdin. Run `selftest` after any change to the policy or the hooks wiring: it reports findings
classified as environment, policy or system, and only system findings mean this code is at fault.

## 3. States

    ready -> asking -> planning -> executing -> reporting -> ready       bypass: blocked

ready to asking is the hook, on the human's first message. asking to planning is the agent, or the
human running approve, behind the gates context, docs-decision and grill-valid. planning to executing
is the human running approve only, behind the four gates below. planning to asking is the human running
reject. asking to ready is the agent, on a turn that only asked a question: it applies on the one turn
the hook reports it moved the machine from ready into asking, so a question asked from planning or
executing is answered without running `advance ready` - pulling the machine back out of work in
progress would discard it with no human act. Entering `asking` opens an intake and leaving it closes
one: `blocked` is the exception both ways, an interruption rather than an end. executing to reporting,
and reporting to ready, are the agent.

Two things that surprise people, both of them true and both of them once written the other way round:

- **The agent may enter and leave `blocked` by itself**, with no gate and no human, because that is
  what "stop and report" needs to mean. It is a state the agent declares, not a decision the human
  makes for it.
- **Only `executing` cannot be reached by the agent.** `advance` to any other target in the agent list
  succeeds whether or not it skips a state: a gate belongs to the state being ENTERED, and only
  `planning` and `executing` carry one. Skipping is not blocked by a rule; the state skipped into simply
  has nothing in front of it. Keying a gate to the destination rather than to the journey is what closed
  a hole that used to be here: leaving `blocked` for `planning` ran no gate at all, because the table
  had no entry for a route that begins in the bypass.

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

Entering `executing`
- `plan-schema` - a plan file, if one exists, carries `## Steps` and `## Files`, both non-empty. An absent file passes: the plan is a conversation artefact by default
- `zero-p0` - p0_count is 0
- `protected-list-clear` - the plan's `## Files` section names no path on the protected list
- `stack-env` - stack_env is declared, which means the human said what the environment is rather than the machine probing for it

Entering `planning`
- `context` - the five intake items are present and each at least 4 characters
- `docs-decision` - docs_decision is create or skip
- `grill-valid` - grill_rounds is at least 1, consensus at least 10 characters, grill_used is with-docs or me

### Tool classes

The class decides which rules are consulted and how a tool is judged. Membership is here rather than in prose because prose drifts; a tool not listed is `unknown`.

- `visual`: `screenshot_page`, `view_image`, `run_playwright_code`, `mcp_playwright_browser_take_screenshot`, `mcp_playwright_browser_run_code_unsafe`, `mcp_playwright_browser_evaluate`
- `env`: `install_python_packages`, `install_extension`, `configure_python_environment`, `debug_java_application`, `create_new_workspace`, `create_new_jupyter_notebook`
- `exec`: `run_in_terminal`, `run_notebook_cell`, `create_and_run_task`
- `write`: `create_file`, `create_directory`, `replace_string_in_file`, `multi_replace_string_in_file`, `edit_notebook_file`, `vscode_renameSymbol`, `mcp_github_mcp_se_create_or_update_file`, `mcp_github_mcp_se_delete_file`, `mcp_github_mcp_se_push_files`, `mcp_github_mcp_se_fork_repository`
- `read` (never gated: that is what the class means): `read_file`, `grep_search`, `file_search`, `list_dir`, `get_errors`, `copilot_getNotebookSummary`, `read_notebook_cell_output`, `vscode_listCodeUsages`
- `session` (never gated: that is what the class means): `runSubagent`, `manage_todo_list`, `vscode_askQuestions`, `memory`

Classes are consulted strictest first, so a tool listed in two of them is judged by the more restrictive one. `visual` is first: a tool that reads an image stays refused even if it also appears as a reader. A tool no class lists is `unknown`, and is judged by what it carries - a command makes it an exec tool - and otherwise by whether its name looks like an action.

### Rules

In file order, first match wins. The conditions are named rather than quoted: the values live in `.github/ocf/policy.toml`, which is where they are meant to be read and changed. Every rule follows the master switch; the three spellings of a rule's own 
`enabled` line, and which one is the default, are in that file's vocabulary region.

- `plan-file-writable` -> `allow` - The gate itself asks for this artifact. [class, path_matches]
- `failure-budget` -> `deny` - Consecutive failures reached the budget. Stop and report the symptom, what you tried, and what you need from the human. Clears when the human replies. [class, fact, is]
- `visual-tool` -> `deny` - The machine may never take screenshots or view images with a tool. Ask the human to attach one; attachments are readable, tools are not. [class]
- `visual-command` -> `deny` - The machine may never run visual or screenshot tests. [class, command_matches]
- `human-only-subcommand` -> `deny` - Authorization subcommands run only in the human's own terminal, even when the human asks. Point them at the rule or the config to change instead. [class, computed]
- `advance-to-executing` -> `deny` - The agent may not advance to that state. Entering executing is a human act. [class, command_matches]
- `self-authorization-write` -> `deny` - Writing a human-only subcommand into an executable file is self-authorization. [class, content_matches, path_matches]
- `unread-write-target` -> `deny` - This command writes, but where it writes could not be read, so the target cannot be checked. Name the path in a form that can be read - a relative path with a separator, or a filename with an extension - or make the change with an editing tool. [class, write_target_unread]
- `touches-protected` -> `deny` - That path is on the protected list. Only the human may change it. [class, touches_protected]
- `command-too-long` -> `deny` - Command too long. Split it into short single-purpose commands. [class, command_length_over]
- `too-many-statements` -> `deny` - Too many statements chained into one command. Split them and run one at a time. [class, command_statements_over]
- `silenced-output` -> `deny` - The command silences its output. Everything must stay visible to the human. [class, command_matches]
- `interactive-command` -> `deny` - The command may block on input or raise a dialog. Rewrite it non-interactively; anything needing elevation or a click is the human's job. [class, command_matches]
- `test-authorization` -> `deny` - Do not run tests on your own. Only after the human asks in this conversation, set test_authorized yes. [class, command_matches, fact, is_not]
- `undeclared-env-command` -> `deny` - The human has not declared the existing environment. Do not install or probe on your own. [class, command_matches, fact, is_set]
- `destructive` -> `deny` - Destructive command. Hand it to the human. [class, command_matches]
- `undeclared-env-tool` -> `deny` - The human has not declared the existing environment. Say what it is before the machine installs, configures or scaffolds anything; do not probe for it. [class, fact, is_set]
- `approval-required-write` -> `require_approval` - File edits need approval. The human runs the approve subcommand in their own terminal. [approval, class]
- `approval-required-exec` -> `require_approval` - Commands need approval. The human runs the approve subcommand in their own terminal. [approval, class, unless]
- `approval-required-env` -> `require_approval` - This tool changes the environment and needs approval. [approval, class]
- `repeated-command` -> `ask` - The same command again, which suggests you are stuck in a loop. The human decides whether to continue. [class, command_repeats_at_least]
- `intake-grilling` (soft, occasion `ask`) - ask only what is missing
- `independent-audit` (soft, occasion `plan`) - independent audit
- `no-screenshots` (soft, occasion `act`) - screenshots come from the human
- `beginner-mode` (soft, occasion `ask, answer`) - beginner mode
- `human-only-commands` (soft, occasion `act`) - human-only commands
- `write-for-the-reader` (soft, occasion `act, answer`) - write for the reader
- `a-way-back` (soft, occasion `plan, act`) - a way back
- `plan-write-file` (soft, occasion `plan`) - the plan is written to a file
- `plan-template` (soft, occasion `plan`) - fill the plan template
- `report-template` (soft, occasion `act`) - fill the report template
- `grill-with-docs` (soft, occasion `plan`) - question against the documents
- `question-is-not-a-task` (soft, occasion `ask`) - a question is not a task
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
  their class in the list above.

## 5. Intake

How the human is asked, how their reply is read, and what counts as an answer are the `intake-grilling`
soft rule, injected on every turn. None of it is restated here: a rule written twice is a rule the human
can switch off in one place and leave running in the other.

What each gate requires is in the generated list under section 4, and what the `context` gate reads is
described next to it in `ocf.py`. Neither is copied here.

## 6. Planning and approval

Whether the plan is a chat message or a file is the `plan-write-file` soft rule, and the shape it takes
is `plan-template`; neither is restated here. What
the gate cares about is that an absent `.orchestrator/plan.md` is not a failure - plan-schema passes with
a note saying where the plan was given - because the approval is the human's, not the file's. When such a
file is written it carries the sections of the template at `.github/assets/plan-template.md`, which is
where the list is kept.

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
exec-class tool until the human replies. The counter has two sources: the model records a failure it saw
with `ocf.py fail "<reason>"`, and the post-tool handler adds one when a command's exit code came back
non-zero - skipping the commands whose exit code IS their answer (`grep`, `diff`, `Test-Path`). Reset with
`ocf.py ok`.

Pitfalls already stepped on, most of them while this was written in PowerShell and sh. They are kept
because the gate's own contract and the human's terminal are both still real - the wiring string, the
blocking channel and reading files under PowerShell 5.1 apply today, and `release/build/build.ps1` is
written in the same language, so its two parsing traps are one keystroke away from being stepped on
again. They are not kept because the gate still runs them.

- PreToolUse must block through stdout permissionDecision, because stderr with a non-2 exit code is
  only a non-blocking warning.
- The hooks command string must contain no `$`, since the outer shell interpolates it.
- A bare function call at the start of a condition is parsed as a command, so
  `if (Test-Enforced -and -not $isOcf)` degenerates to that function's output and the variable is
  ignored. Write `if ((Test-Enforced) -and (-not $isOcf))`.
- A function unwraps a one-element array, so `$lines[0]` degrades to a character. Wrap the call site in
  `@(...)`.
- Reading a BOM-less UTF-8 file without -Encoding decodes it as cp936 under PowerShell 5.1, mangling
  text and sometimes swallowing a newline. Use the explicit readers and verify under `powershell` 5.1,
  because `pwsh` 7 hides it. A BOM in config once broke the `^key=` anchors, so reads tolerate one.
- `$"..."` is not valid interpolation; write `"...: $($x)"`.
- PowerShell scripts take no pipeline input; pipe into `pwsh -NoProfile -File script.ps1`.
- A non-zero `exit` in an `&`-invoked script is downgraded; use `[Environment]::Exit(2)` and `-File`.

The implementation and every agent-facing document in `.github` are ASCII, `run.py` included, and
`run.py`'s `NON_ASCII_ALLOWED` is empty: there is no exemption left to list, so this is a property of
the whole directory rather than of three files. Machine-read text in a language the tooling does not
promise to decode fails with no error message, and `ocf.py` in particular is deliberately inside the
rule because its output reaches a console that may not be UTF-8. The check is `github-folder-ascii` in `release/build/engine_checks.py`:
a new file that wants a non-Latin character has to argue for itself there.

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
`result` (the verdict, hard rules only) and `message` (the sentence the agent reads - handed back
inside a hard rule's verdict, and injected into the contract while a soft rule is on).

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
it (section 7, terminal discipline). Shared keys do remain elsewhere - `[approval] allowed_states` and
`min_reason_len`, `[unknown_tool] action`, the `[hooks]` switches, `[paths] state_dir` - and they are
listed with what they mean in the generated region above. The file is read on every invocation, so a
change to it applies immediately; only `.github/hooks/*.json` needs a window reload.

`system.enabled` is the maintenance switch, and it is not a small one. It takes only `true` or `false`;
any other value is read as `true` and reported, so an unreadable switch leaves the gate armed rather
than open. A rule says for itself how the switch reaches it: `enabled = true` means it holds whatever
the window says, `false` means it is never
evaluated, and `"switch"` (the default) means the window suspends it. Almost every rule in the shipped
policy says `"switch"`, so the window reaches it - the protected list included, and that list is also
what protects this document and the gate's own source. Three say otherwise: `plan-write-file` says
`false`, and `plan-template` and `report-template` say `true`, so the shape they describe holds whether
the window is open or shut. `false` hands back the ability to edit gate code AND quietly stops requiring
approval; do not read it as "only the protected list is off". `status` prints
the rules it actually switched off rather than a list written here, and that is deliberate: a list
written here is a second copy of an answer the program already computes.

What keeps that safe is not that a rule ignores the switch. It is that the switch never leaves the
human's hand: no rule grants the agent a way to reach `[system]`, and nothing it can write re-arms the
gate by itself. Both halves are pinned by the case table rather than by this paragraph -
`switch-off-opens-gate-code` for the open window and `self-protection-holds-while-acting` for the armed
one.

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
