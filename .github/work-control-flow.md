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
  gate that quietly stopped denying fails a test instead of looking healthy. It also resolves every
  agent name referenced by a custom agent or a prompt, every tool token against the seven documented
  aliases, every relative markdown link under `.github`, and that the self-protection pattern still
  covers every orchestrator file. Frontmatter and path references fail silently otherwise.
- `.orchestrator/` holds the runtime: state, facts, plan.md, journal.log, exec.log. Never hand-edit
  them. The protected list is NOT here - it is `.github/protected.txt`, because a list that is not
  versioned is absent on a fresh clone, and every rule still reads as if it were there.

## 2. Commands

Agent: status, set, gate, journal, fail, ok, init, selftest. advance targets are limited to asking,
planning, reporting, ready, blocked. set refuses approved_by and must_consult, and grill_rounds is
counted by the hook so it refuses that too.

Human terminal only: approve, reject, confirm, allow, deny, human-code, reload. `reload` re-reads the
configuration and regenerates the two files built from it - the standing contract and the hook wiring -
so an agent able to run it could edit the rules it is being asked to follow.

The hook entry is `python .github/ocf/ocf.py hook` (or `python3` off Windows), which reads the event
JSON on stdin. Run `selftest` after any change to the policy or the hooks wiring: it reports findings
classified as environment, policy or system, and only system findings mean this code is at fault.

## 3. States

    ready -> asking -> planning -> executing -> reporting -> ready       bypass: blocked

ready to asking is the hook, on the human's first message. asking to planning is the agent, or the
human running confirm, behind the gates context, docs-decision and grill-valid. planning to executing
is the human running approve only, behind plan-schema, zero-p0, protected-list-clear and stack-env.
planning to asking is the human running reject. executing to reporting, and reporting to ready, are the
agent. Any state goes to blocked and back on the human's word. Staying put is legal and means keep
going.

Only the human running approve enters executing. Only executing and reporting allow file edits and
commands. Skipping a state is illegal. When a gate fails or information is missing, go to blocked and
wait rather than forcing it.

## 4. Gates

State gates:

- context: goal, tools, references, deliverables, code_style, each at least 4 characters. The agent
  derives them from context and the human's own statement; grilling fills only what is left, one
  question per turn. Never fill them in yourself.
- docs-decision: docs_decision is create or skip.
- grill-valid: grill_rounds at least 1, consensus at least 10 characters, grill_used with-docs or me.
- plan-schema: plan.md carries all 8 sections from section 6.
- zero-p0: p0_count is 0. Revise the plan, do not just change the number.
- protected-file: plan.md touches no path in `.github/protected.txt`. There is no exemption to grant;
  a human edits that list by hand.
- stack-env: stack_env is declared. Ask for the existing environment, never install or probe.

Runtime gates run on PreToolUse. They block by writing `permissionDecision: deny` to stdout and
exiting 0, except going in circles which answers ask. Never rely on the exit code and never write to
stderr, and a broken guard fails safe by denying.

PostToolUse is a reporter rather than a gate. When a terminal command returns with no progress - a
shell sitting at a continuation prompt, or a command moved to the background - it injects a warning
saying the command never reported a result, and journals it. Text rules cannot predict that an unclosed
quote will hang a shell, because the gate reads the command instead of parsing the shell; what it can
do is notice the aftermath, so a silent hang becomes a record instead of a mystery.

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
   plus `.github/work-control-flow.md`, `.github/copilot-instructions.md`, `.github/protected.txt` and
   the four `.orchestrator` runtime files. It is tied to `system.enabled`, not to approval, so it still
   holds during executing. The human edits by hand, or sets `system.enabled = false`.
3. protected list: the target matches a line in `.github/protected.txt`. Judged per path, so one
   qualifying path does not decide the fate of the other paths in the same call, and judged wherever
   the path came from - the same rule covers the editing tools and the terminal. There is no exemption
   file: to let something through, a human edits that list by hand.
4. approval: the state is not executing or reporting.

Exec-class, in order:

- visual ban: headless, --screenshot, screenshot, playwright, puppeteer, or capture image or screen.
- failure budget: must_consult is yes. Stop and report the symptom, what you tried, what you need.
- self-authorization: a human-only subcommand in a command that invokes the control script, or an
  advance to a target outside the agent list.
- approval: a command that is not control-plane while the state is not executing or reporting.
  Skipped when `system.enabled` is false.
- command too long, over the ceiling the rule states (400). Too many statements, over 3, counted after
  blanking
  quoted spans and `@{ }` hashtable literals, because a semicolon inside a string or between hashtable
  entries is not a separator. Everything else, parentheses and script blocks included, still counts.
- silenced output: Out-Null, `$null`, /dev/null, --quiet, -Quiet, -WindowStyle Hidden.
- interactive or elevation: Read-Host, ReadKey, -Verb RunAs, sudo, cmd /c.
- test authorization: a test command while test_authorized is not yes.
- toolchain: an install or probe command while stack_env is empty.
- destructive: rm -rf on a root path, git push --force, drop table, git reset --hard, Remove-Item
  -Recurse -Force.
- write target: a write-ish command touching a self-protected or human-protected path.
- going in circles: the same command about to run for the fourth time, answered ask.

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

**The plan is given in chat by default**: the goal in one sentence, the requirements (the reference design
and the process steps), and the deliverables. A file is written only when the human asks for one. An
absent `.orchestrator/plan.md` is not a failure - plan-schema passes with a note saying where the plan was
given - because the approval is the human's, not the file's. When such a file is written it carries all
eight sections of the template at `.github/assets/plan-template.md`:

    ## Type  ## Summary  ## Steps  ## Tools  ## Files  ## Scope  ## Deliverables  ## Self-review

Of those, only `## Steps` and `## Files` are required, because only they are read: plan-schema checks
that both are present and non-empty, and protected-list-clear compares Files against the protected
list, so that section must name the exact paths to change. Type is refactor, feature, fix, docs, chore
or test. The
report has its own template at `.github/assets/report-template.md`; both template files are in English and
the headings stay in English, while the agent fills every other line in the language the human is using.

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

There are two kinds of rule, and they are enforced in two different places. A **hard** rule is read by
the hook, outside the conversation, and answers with a verdict. A **soft** one cannot be enforced that
way - nothing can check whether a sentence was written - so `reload` writes it into the standing
contract the model reads on every turn, and the model applies it. Both are written the same way in
`.github/ocf/policy.toml`: `enabled`, `kind`, `if` (the occasion it hits), `unless` (the exception),
`result` (the logic) and `why`. For a hard rule `result` is a verdict; for a soft rule it is the
sentence itself.

`python .github/ocf/ocf.py reload` is human-only, because it regenerates the two files that are built
from the configuration: the standing contract and the hook wiring. An agent able to run it could edit
the rules it is being asked to follow. It validates the whole policy first and changes nothing if the
policy is broken, since half-applied configuration is worse than none.

`[hooks]` is the install switch: `enabled = false` makes reload write a wiring file with no hooks in
it, so VS Code stops starting this program at all - uninstalling is not something left half-done. Each
event has its own switch as well, and the entry point reads them too, because the wiring file is only
re-read when the window reloads. `pre_tool_use` is the gate; the other three only add context.

Exactly one threshold is left in `.github/ocf/policy.toml` as a shared key: `[limits] fail_budget = 2`.
Every other ceiling lives inside the rule that uses it. The file is read on every invocation, so a
change to it applies immediately; only `.github/hooks/*.json` needs a window reload.

`system.enabled` is the maintenance switch. It is NOT the whole gate, which is easy to misread: it
gates the three self-protection rules and the three self-authorization rules, and nothing else - the
other rules have no `when` and hold either way. Setting it false hands control back: the orchestrator's
own files stop being protected, so gate code may be edited. The protected list is judged by its own
rule, which has no `when` either, so that protection keeps applying. The policy file is self-protected,
and an unreadable or malformed
policy keeps the gate on by falling back to a policy that denies every change while still allowing
reading and the always-allowed tools. That last part matters: a gate that refuses to let anyone read it
is a gate nobody can repair, and a one-character typo once denied even `read_file`. A missing policy
file also reports itself as a policy finding from `selftest`, never as silence.

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
  every change and still allows reading and the always-allowed tools; the read-only list is what makes
  that true, so removing an entry from it removes a recovery path.
- Inline agent hooks would scope enforcement to one agent, but they need the chat.useCustomAgentHooks
  setting, and if it is off the gate silently stops working, so the workspace hook stays the default.
