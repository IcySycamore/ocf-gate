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
- `.github/hooks/orchestrator.json` wires SessionStart, UserPromptSubmit and PreToolUse to
  `ocf.py hook`.
- `.github/ocf/tests/` holds the offline case table. Each case runs through the real entry point, so a
  gate that quietly stopped denying fails a test instead of looking healthy. It also resolves every
  agent name referenced by a custom agent or a prompt, every tool token against the seven documented
  aliases, every relative markdown link under `.github`, and that the self-protection pattern still
  covers every orchestrator file. Frontmatter and path references fail silently otherwise.
- `.orchestrator/` holds the runtime: state, facts, plan.md, human-code.txt, allowed-edits.txt,
  journal.log, exec.log. Never hand-edit them.

## 2. Commands

Agent: status, set, gate, journal, fail, ok, init, selftest. advance targets are limited to asking,
planning, reporting, ready, blocked. set refuses approved_by and must_consult, and grill_rounds is
counted by the hook so it refuses that too.

Human terminal only: approve, reject, confirm, allow, deny, human-code.

The hook entry is `python .github/ocf/ocf.py hook` (or `python3` off Windows), which reads the event
JSON on stdin. Run `selftest` after any change to the policy or the hooks wiring: it reports findings
classified as environment, policy or system, and only system findings mean this code is at fault.

## 3. States

    ready -> asking -> planning -> executing -> reporting -> ready       bypass: blocked

ready to asking is the hook, on the human's first message. asking to planning is the agent, or the
human running confirm, behind the gates context, docs-decision and grill-valid. planning to executing
is the human running approve only, behind plan-schema, zero-p0, human-code-clear and stack-env.
planning to asking is the human running reject. executing to reporting, and reporting to ready, are the
agent. Any state goes to blocked and back on the human's word. Staying put is legal and means keep
going.

Only the human running approve enters executing. Only executing and reporting allow file edits and
commands. Skipping a state is illegal. When a gate fails or information is missing, go to blocked and
wait rather than forcing it.

## 4. Gates

State gates:

- context: goal, tools, references, deliverables, code_style, each at least 4 characters, asked item by
  item with one missing item per question. Never fill them in yourself.
- docs-decision: docs_decision is create or skip.
- grill-valid: grill_rounds at least 1, consensus at least 10 characters, grill_used with-docs or me.
- plan-schema: plan.md carries all 8 sections from section 6.
- zero-p0: p0_count is 0. Revise the plan, do not just change the number.
- human-code-clear: plan.md touches no path in human-code.txt, unless the human runs allow.
- stack-env: stack_env is declared. Ask for the existing environment, never install or probe.

Runtime gates run on PreToolUse. They block by writing `permissionDecision: deny` to stdout and
exiting 0, except going in circles which answers ask. Never rely on the exit code and never write to
stderr, and a broken guard fails safe by denying.

The gate is an interpreter and `policy.toml` is the program: each rule declares what it applies to,
what it matches, and what it answers, and the first match wins. That is why exemptions sit at the top
of the file and broad catch-alls sit at the bottom, and why a rule change is a data edit rather than
a code change. The rule ids in the lists below are the ids in that file.

A tool the policy does not classify is judged by what it carries: if it carries a command it is
treated as an exec tool, so a tool added later cannot create an uninspected path. If it only carries a
path it is left to the name heuristic, so read-only tools are not dragged into the edit rules. An exec
tool whose command cannot be read at all is denied rather than allowed blind.

Edit-class, in order, for create_file, replace_string_in_file, multi_replace_string_in_file,
edit_notebook_file, vscode_renameSymbol and the GitHub file-write tools:

0. `.orchestrator/plan.md` is exempt, because the gate itself asks for it.
1. self-authorization: an executable target whose content invokes a human-only subcommand.
2. self-protection: anything under `.github/hooks`, `.github/ocf`, `.github/agents` or `.github/prompts`,
   plus `.github/work-control-flow.md` itself and the six `.orchestrator` runtime files. It is tied to
   `system.enabled`, not to approval, so it still holds during executing. The human edits by hand, or
   sets `system.enabled = false`.
3. human code: the target matches human-code.txt and is not in allowed-edits.txt. Both are judged per
   path, so one qualifying path does not decide the fate of the other paths in the same call.
4. approval: the state is not executing or reporting.

Exec-class, in order:

- visual ban: headless, --screenshot, screenshot, playwright, puppeteer, or capture image or screen.
- failure budget: must_consult is yes. Stop and report the symptom, what you tried, what you need.
- self-authorization: a human-only subcommand in a command that invokes the control script, or an
  advance to a target outside the agent list.
- approval: a command that is not control-plane while the state is not executing or reporting.
  Skipped when `system.enabled` is false.
- command too long, over max_cmd_len. Too many statements, over max_cmd_stmts, counted after blanking
  quoted spans and `@{ }` hashtable literals, because a semicolon inside a string or between hashtable
  entries is not a separator. Everything else, parentheses and script blocks included, still counts.
- silenced output: Out-Null, `$null`, /dev/null, --quiet, -Quiet, -WindowStyle Hidden.
- interactive or elevation: Read-Host, ReadKey, -Verb RunAs, sudo, cmd /c.
- test authorization: a test command while test_authorized is not yes.
- toolchain: an install or probe command while stack_env is empty.
- destructive: rm -rf on a root path, git push --force, drop table, git reset --hard, Remove-Item
  -Recurse -Force.
- write target: a write-ish command touching a self-protected or human-protected path.
- going in circles: the same command about to run for the max_cmd_repeat plus one-th time, answered ask.

Control-plane is recognised only when every statement invokes the control script by its full relative
path, so merely mentioning the name does not inherit the exemption.

Also blocked outside executing and reporting: install_python_packages, install_extension,
debug_java_application, configure_python_environment, create_new_workspace,
create_new_jupyter_notebook. Always allowed: runSubagent, manage_todo_list, vscode_askQuestions,
memory. Blocked unconditionally: screenshot_page, view_image, run_playwright_code,
mcp_playwright_browser_take_screenshot, mcp_playwright_browser_run_code_unsafe. An unknown tool whose
name looks like an action is escalated with ask while the state is not executing or reporting.

A screenshot the human attaches is readable. The screenshot tool is not.

## 5. Intake

One question at a time, one missing item per question, never guessing and never filling in for the
human. Facts, with what each gate needs: goal, tools, references, deliverables and code_style at least
4 characters each, docs_decision create or skip, stack_env at least 4 characters and asking for the
existing environment, grill_used with-docs or me, consensus at least 10 characters. grill_rounds is
counted by the hook and must not be written.

A perfunctory or automatic reply is not an answer. Re-ask, do not advance, never decide for the human.
You make that judgement yourself; the system does no keyword matching.

## 6. Planning and approval

plan.md carries 8 sections, each matched by plan-schema:

    ## Type  ## Summary  ## Steps  ## Tools  ## Files  ## Scope  ## Deliverables  ## Self-review

Type is refactor, feature, fix, docs, chore or test. Files names the exact paths to change, because
human-code-clear compares them against human-code.txt.

P0 causes rework, breaks human code or data, bypasses an approval gate, or misses the goal, and must
reach zero. P1 makes implementation stumble and goes in the risk report. P2 is style.

    write the plan -> dispatch plan-auditor -> any P0? yes -> revise and re-audit
                                              no  -> set p0_count 0

Then give the human two reports in chat, unless they want a file: the action report, and the risk
design report covering risks, triggers, mitigations, residual risk and rejected alternatives. Hand
them this verbatim and stop:

    python .github/ocf/ocf.py approve "<one-sentence reason>"

Only the human running it in their own terminal enters executing. Do not run it for them, and do not
set approved_by.

## 7. Terminal discipline

Visible and traceable, never a hung, confused or frantically iterating black box. Keep commands under
max_cmd_len and to at most max_cmd_stmts statements, keep output visible, and send one short
single-line command at a time, because the output ownership of a multi-line paste is unreliable. To
filter, limit rows rather than discarding all output. For a real exit code use a separate child
process, since a parent shell keeps a stale LASTEXITCODE.

fail_budget consecutive failures sets must_consult and locks every exec-class tool until the human
replies. Record with `ocf.py fail "<reason>"`, reset with `ocf.py ok`.

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
  because `pwsh` 7 hides it. A BOM in config or human-code.txt once broke the `^key=` anchors, so reads
  tolerate one.
- In a `tr` set the `-` may only be first or last.
- After byte truncation strip a partial character with `LC_ALL=C sed 's/[\200-\277]*$//'`.
- `$"..."` is not valid interpolation; write `"...: $($x)"`.
- PowerShell scripts take no pipeline input; pipe into `pwsh -NoProfile -File script.ps1`.
- A non-zero `exit` in an `&`-invoked script is downgraded; use `[Environment]::Exit(2)` and `-File`.

The implementation and every agent-facing document are English and ASCII, which removes that whole
class of bugs at the root. `ocf.py` is checked for it by the test suite.

## 8. Delivery

In executing, follow the approved plan with no unapproved changes; to deviate, advance blocked and
re-plan. Use the diagnose skill when something breaks, with criterion-picker choosing the reproduction
method, and leave the visual part to the human.

In reporting, give the change list, statistics, unfinished items with reasons, and the audit summary
from `ocf.py journal 20`, quoting journal.log and facts rather than memory, then advance ready.

Do not run tests unless the human asks in this conversation, and set test_authorized yes first. Never
run visual tests.

## 9. Limits and switches

The `[limits]` keys in `.github/ocf/policy.toml`, with defaults: max_cmd_len 400, max_cmd_stmts 3,
max_cmd_repeat 3, fail_budget 2. They are read on every invocation, so changes apply immediately; only
`.github/hooks/*.json` needs a window reload.

`system.enabled = true` is the real setting and the only switch: no edits and no commands before
approval, the orchestrator's own files are protected, and writing a human-only subcommand into an
executable file is refused. `system.enabled = false` hands control back: it skips the approval
requirement and lets the machine edit gate code, while human-code protection still applies. The policy
file is self-protected, so the machine cannot turn it off itself, and an unreadable or malformed
policy keeps the gate on by falling back to a policy that denies everything except the always-allowed
tools. A missing policy file also reports itself as a policy finding from `selftest`, never as
silence.

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
- Inline agent hooks would scope enforcement to one agent, but they need the chat.useCustomAgentHooks
  setting, and if it is off the gate silently stops working, so the workspace hook stays the default.
