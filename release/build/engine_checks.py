#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Checks on the engine itself: not shipped, because only the person editing ocf.py can be bitten.

Every check in `.github/ocf/tests/run.py` reaches the person who deploys this gate, because that file
is part of the payload - which means it should only contain things THEY can break: their policy, their
tools, their rules, their protected list, their documentation. A check on the state machine's internals
is not one of those. Nobody who deploys a gate ever edits TRANSITIONS, GATES or evaluate_rule_v2, so a
failure there tells them nothing they can act on, and the message says so in a vocabulary they have no
reason to know.

They are not deleted, because they do catch real defects - the CLI vocabulary drifting from the
dispatcher, a gated pair renamed into advancing in silence, a soft rule read as a verdict by the
fail-safe. They live here instead, on the side of the line that does not ship: `release/build/` is in no
archive. `build.ps1` runs both suites before it packages anything, so a broken engine cannot reach a
release - which is the arrangement that was missing, since the build used to run no checks at all.

They import their helpers from the shipped suite rather than growing a second copy of `build_root`,
`load_ocf_module` and the temp-repo plumbing.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
TESTS = os.path.join(REPO, ".github", "ocf", "tests")
sys.path.insert(0, TESTS)

from run import ENTRY, POLICY, build_root, install_into, load_ocf_module, run_cli  # noqa: E402

# Both moved here with the checks that use them; they have no other reader.
GATED_PAIRS = (("asking", "planning"), ("planning", "executing"))

PROBE_POLICY = """\
[[rule]]
id = "probe-long-command"
enabled = true
result = "deny"
message = "too long for the probe"

[rule.if]
class = "exec"
command_length_over = 10

[[rule]]
id = "probe-default"
enabled = true
result = "allow"
message = "short enough for the probe"

[rule.if]
class = "exec"
"""


def check_transition_table():
    """The one table must agree with the states, and the CLI vocabulary must agree with the dispatcher.

    `STATES` stays the literal and the table is built from it, so this compares the two rather than
    deriving one from the other: a derived value would satisfy the comparison by construction, which is
    indistinguishable from having no check at all. The vocabulary half matters for the same reason: a
    command in the table but not in the dispatcher is unreachable, and one in the dispatcher but not in
    the table is a name the usage text never mentions.
    """
    ocf = load_ocf_module()
    table = ocf.TRANSITIONS
    problems = []
    if set(table["states"]) != set(ocf.STATES):
        problems.append("the table's states %s differ from STATES %s"
                        % (sorted(set(table["states"])), sorted(set(ocf.STATES))))
    for target in table["agent_targets"]:
        if target not in ocf.STATES:
            problems.append("the agent target %r is not a state" % target)
    mentioned = set(table["agent_targets"])
    for pair in table["gates"]:
        for name in pair:
            if name not in ocf.STATES:
                problems.append("a gate key names %r, which is not a state" % name)
        mentioned.update(pair)
    unmentioned = sorted(set(ocf.STATES) - mentioned)
    if unmentioned:
        problems.append("no part of the table mentions the state(s) %s, so nothing governs them"
                        % ", ".join(unmentioned))
    # The happy path is drawn from the table now, so it is checked like the rest of it. A state renamed
    # in STATES used to leave the diagram in the contract behind, and the diagram is what gets read.
    for name in table["flow"]:
        if name not in ocf.STATES:
            problems.append("the flow names %r, which is not a state" % name)
    if table["flow"][0] != table["flow"][-1]:
        problems.append("the flow starts at %r and ends at %r, so it does not describe a cycle"
                        % (table["flow"][0], table["flow"][-1]))
    if table["bypass"] not in ocf.STATES:
        problems.append("the bypass names %r, which is not a state" % table["bypass"])
    if table["bypass"] in table["flow"]:
        problems.append("the bypass %r is on the happy path, so it bypasses nothing" % table["bypass"])
    for name in ocf.SOFT_OCCASIONS:
        if not name.isascii() or name != name.strip().lower():
            problems.append("the occasion %r is not a plain lowercase token; it is matched verbatim "
                            "against the policy, so a stray space makes a rule silently inert" % name)
    declared = set()
    for group in table["commands"].values():
        for line in group:
            declared.update(line)
    handlers = set(ocf.COMMAND_HANDLERS)
    if handlers != declared:
        problems.append("the CLI vocabulary differs: only in the dispatcher %s; only in the table %s"
                        % (sorted(handlers - declared), sorted(declared - handlers)))
    missing_usage = sorted(name for name in declared if name not in table["usage"])
    if missing_usage:
        problems.append("the usage text has no fragment for %s" % ", ".join(missing_usage))
    assert not problems, "; ".join(problems)


def check_gated_transitions():
    """Every transition the machine gates must really run gates, and must name states that exist.

    The expectation is written out above rather than taken from the table, because a check that iterates
    the table to build its own expectation stays green when a key is renamed or emptied - and that is
    exactly the omission it exists to catch. `transition_gate_names` falls back to an empty tuple for an
    unknown pair, so a renamed key would advance the machine with no gate running and nothing printed.

    Blind spot, stated rather than implied: it does not notice a gated pair deleted outright, only a pair
    renamed or emptied. It guards the keys, not the set of transitions.
    """
    ocf = load_ocf_module()
    problems = []
    for pair in GATED_PAIRS:
        for name in pair:
            if name not in ocf.STATES:
                problems.append("%s names %r, which is not a state in STATES" % (pair, name))
        names = ocf.transition_gate_names(*pair)
        if not names:
            problems.append("the transition %s -> %s runs no gate at all, so it would advance in "
                            "silence" % pair)
        for name in names:
            if name not in ocf.GATES:
                problems.append("the transition %s -> %s names a gate the engine does not have: %s"
                                % (pair[0], pair[1], name))
    for pair in ocf.TRANSITIONS["gates"]:
        for name in pair:
            if name not in ocf.STATES:
                problems.append("a key of the transition table names %r, which is not a state" % name)
    assert not problems, "; ".join(problems)


def check_new_rule_shape():
    """The newer rule shape (one condition block, one result) must decide on its own.

    Nothing in the shipped policy uses it yet, so without this the code would stay unverified until the
    policy is converted - and "unverified until later" is how a silent failure gets in. The probe drives
    the real hook entry point, so it also proves the dispatch from evaluate_rule actually happens.
    """
    root = tempfile.mkdtemp(prefix="ocf-newshape-")
    try:
        os.makedirs(os.path.join(root, ".github", "ocf"))
        os.makedirs(os.path.join(root, ".orchestrator"))
        with open(os.path.join(root, ".github", "ocf", "policy.toml"), "w",
                  encoding="utf-8", newline="\n") as handle:
            handle.write(PROBE_POLICY)
        with open(os.path.join(root, ".orchestrator", "state"), "w", encoding="utf-8") as handle:
            handle.write("asking\n")
        env = dict(os.environ)
        env["OCF_ROOT"] = root

        def verdict(command):
            payload = {"hook_event_name": "PreToolUse", "session_id": "abcdef1234567890",
                       "tool_name": "run_in_terminal", "tool_input": {"command": command}}
            proc = subprocess.run([sys.executable, ENTRY, "hook"],
                                  input=json.dumps(payload).encode("utf-8"),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=root)
            out = json.loads(proc.stdout.decode("utf-8", "replace"))
            return out["hookSpecificOutput"]["permissionDecision"], \
                out["hookSpecificOutput"]["permissionDecisionReason"]

        decision, reason = verdict("echo one two three")
        assert decision == "deny", "the new shape did not deny: %r" % decision
        assert "too long for the probe" in reason, (
            "the denial did not come from the new-shaped rule, so it may have been the fail-safe; "
            "reason: %r" % reason[:200])
        decision, reason = verdict("echo")
        assert decision == "allow", "the new shape did not fall through to its own default: %r" % decision
        assert "short enough for the probe" in reason, "wrong rule answered: %r" % reason[:200]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_soft_rules_never_gate():
    """A soft rule must be invisible to the gate, proven by a rule that would deny everything.

    A soft rule's logic is a sentence, not a verdict. If the gate ever reads one as a verdict, the
    action becomes that sentence, DECISION_MAP does not recognise it, and the fail-safe turns every
    tool call into a denial - a total lockout caused by adding a sentence to a config file. So the
    probe here is a soft rule with the broadest possible occasion: if the skip were removed, this
    check would deny rather than pass.
    """
    ocf = load_ocf_module()
    root = build_root({"state": "executing"})
    try:
        policy, warnings = ocf.load_policy(root)
        assert not warnings, "the probe policy did not load cleanly: %r" % (warnings,)
        soft = [rule for rule in policy["rule"] if rule.get("kind") == "soft"]
        assert soft, ("the shipped policy has no soft rule, so this check proves nothing: the "
                      "generated instructions would be written from an empty set")
        # A soft rule's sentence has to arrive in the contract, and the generated-region comparison
        # cannot notice it going missing: it compares the render against the file `reload` wrote, and
        # both sides read the same key, so a wrong key empties both and the comparison still passes.
        # This is the one assertion that fails when the reader and the validator disagree about the
        # name of the key the sentence lives under.
        assert any(ocf.soft_text(rule, policy) for rule in soft), (
            "no shipped soft rule produced a sentence, so the standing contract is being written "
            "without them. Compare the key `soft_text` reads against SOFT_SENTENCE_KEY")
        payload = {"tool_name": "run_in_terminal",
                   "tool_input": {"command": "python .github/ocf/ocf.py status"}}
        context = ocf.Context(root, policy, payload)
        probe = {"id": "probe-soft", "kind": "soft", "enabled": True,
                 "message": "deny", "if": {"occasion": "act"}}
        for rule in list(soft) + [probe]:
            verdict = ocf.evaluate_rule(context, rule)
            assert verdict is None, (
                "the soft rule %r produced the verdict %r. A sentence read as a verdict denies "
                "everything the fail-safe cannot map." % (rule.get("id"), verdict))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_line_endings_are_normalised():
    """A file written with CRLF must still compare equal to what this program would write.

    The failure this guards is quiet and platform-specific: git's autocrlf hands out CRLF on Windows,
    the program writes LF, and the comparison that decides "is this generated file already current"
    then never matches. The visible symptom is a reload that always says it rewrote something, which
    makes a real change indistinguishable from the checkout's line endings.
    """
    ocf = load_ocf_module()
    root = build_root({"state": "executing"})
    try:
        policy, _ = ocf.load_policy(root)
        target = os.path.join(root, ocf.HOOKS_REL)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        rendered = ocf.render_hooks_json(policy)
        with open(target, "w", encoding="utf-8", newline="") as handle:
            handle.write(rendered.replace("\n", "\r\n"))
        current = ocf.write_generated(root, policy, ocf.generated(ocf.HOOKS_REL))
        assert current.changed is False, (
            "a CRLF copy of the current wiring was treated as out of date, so the check that a "
            "generated file is current cannot pass on Windows: %r" % (current.message,))
        # The other direction, so the flag cannot pass by always answering "unchanged": a file
        # with stale content must report a change, or nothing decides whether to warn about a reload.
        with open(target, "a", encoding="utf-8", newline="") as handle:
            handle.write("\nstale\n")
        stale = ocf.write_generated(root, policy, ocf.generated(ocf.HOOKS_REL))
        assert stale.changed is True, (
            "a wiring file with stale content was reported as already current: %r" % (stale.message,))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_prompt_instrumentation():
    """A UserPromptSubmit must leave a record, because that record is what settles who spoke.

    The gate has no reliable signal for "a human really replied". Until the instrumentation answers
    that, no rule may depend on one - a rule built on an unverified signal fails by locking the agent
    out, which is the other accident this project exists to prevent.
    """
    root = tempfile.mkdtemp(prefix="ocf-prompt-")
    try:
        os.makedirs(os.path.join(root, ".github", "ocf"))
        os.makedirs(os.path.join(root, ".orchestrator"))
        shutil.copyfile(POLICY, os.path.join(root, ".github", "ocf", "policy.toml"))
        with open(os.path.join(root, ".orchestrator", "state"), "w", encoding="utf-8") as handle:
            handle.write("asking\n")
        env = dict(os.environ)
        env["OCF_ROOT"] = root
        payload = {"hook_event_name": "UserPromptSubmit", "session_id": "abcdef1234567890",
                   "prompt": "a human sentence"}
        proc = subprocess.run([sys.executable, ENTRY, "hook"], input=json.dumps(payload).encode("utf-8"),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=root)
        path = os.path.join(root, ".orchestrator", "prompt-log")
        assert os.path.exists(path), "a UserPromptSubmit wrote no prompt-log, so nothing can be settled"
        with open(path, "r", encoding="utf-8") as handle:
            line = handle.read().strip()
        assert "abcdef12" in line, "prompt-log did not record the session: %r" % line
        assert "a human sentence" in line, "prompt-log did not record the prompt: %r" % line
        # The injected line must report what the agent could not derive, not instruct it to ask for the
        # keys one at a time: the second shape is what this state used to mean, and it is what made the
        # agent interrogate the human for seven answers instead of deriving them.
        emitted = proc.stdout.decode("utf-8", "replace")
        assert "not derivable from context" in emitted, (
            "the asking line no longer reports what could not be derived; emitted: %r" % emitted[:300])
        assert "Ask one at a time" not in emitted, (
            "the asking line still orders the agent to interrogate the human; emitted: %r" % emitted[:300])
        # The record is bounded: it must not outlive the intake it is evidence for.
        ocf = load_ocf_module()
        policy, _ = ocf.load_policy(root)
        assert ocf.clear_prompt_log(root, policy) is True, (
            "the intake transcript was not cleared, so it grows without a bound")
        assert not os.path.exists(path), "the transcript survived its own clear"
        assert ocf.clear_prompt_log(root, policy) is False, (
            "clearing an absent transcript reported that it cleared one")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_rule_text_is_flattened():
    """A soft rule's text reaches the model as one line, with the spaces the human meant and no others.

    This was wrong three times in a row, each time invisibly: joining every wrap on a space put one
    where the break was not a word boundary, joining none of them ran words into each other, and
    dropping the whitespace after a break lost the space in front of an indented path. The terminal
    could not be used to check any of it - its own line wrapping inserts spaces - so the rule is pinned
    here as comparisons rather than looked at. One case per branch: a bare break with a non-word
    character on one side closes up, a break before an indent becomes one space, a bare break between
    two words becomes one space, a leading indent is dropped, and text with no break is untouched.

    It tests `one_line`, which is the engine's, not the human's: the human writes the rule and this
    decides what reaches the model, so a human cannot go wrong here except by editing ocf.py.
    """
    ocf = load_ocf_module()
    cases = (
        ("clause,\nclause", "clause,clause"),
        ("see\n  CONTEXT.md\n  and\n  docs/adr/\n: done", "see CONTEXT.md and docs/adr/: done"),
        ("run\nthe command", "run the command"),
        ("  add\n  docs/notes.md", "add docs/notes.md"),
        ("plain - text", "plain - text"),
    )
    for source, expected in cases:
        got = ocf.one_line(source)
        assert got == expected, ("one_line(%r) gave %r, expected %r. The text lands in front of the "
                                 "model verbatim, so a wrong space here is a wrong instruction."
                                 % (source, got, expected))


def check_human_cli():
    """The human-only commands must run, because they are the only way a gate ever opens.

    Nothing tested them: the suite drives the hook and the renderers, so `protect` and `unprotect` were
    free to raise NameError on every single run - and they did. Each wrote the list and printed success,
    then died on its journal call with a traceback and exit code 1: a command that had worked reporting
    failure, with no audit line.

    `approve` is run from both gated states, which is the merge: one verb, and the state decides which
    gates open.
    """
    facts = {"goal": "ship the fix", "tools": "python", "references": "the README",
             "deliverables": "a passing suite", "code_style": "short lines",
             "docs_decision": "skip", "grill_rounds": "1", "consensus": "we agreed on the goal",
             "grill_used": "me", "stack_env": "python 3.12", "p0_count": "0"}
    root = build_root({"state": "asking", "facts": facts})
    problems = []

    def read(relative):
        with open(os.path.join(root, relative), "r", encoding="utf-8") as handle:
            return handle.read()

    try:
        code, out, err = run_cli(root, "protect", "src/notes.md")
        if code != 0:
            problems.append("`protect` exited %d: %s" % (code, (err or out).strip()[-200:]))
        if "src/notes.md" not in read(".github/protected.txt"):
            problems.append("`protect` did not add the path: %r" % read(".github/protected.txt"))
        code, out, err = run_cli(root, "unprotect", "src/notes.md")
        if code != 0:
            problems.append("`unprotect` exited %d: %s" % (code, (err or out).strip()[-200:]))
        if "src/notes.md" in read(".github/protected.txt"):
            problems.append("`unprotect` did not remove the path: %r" % read(".github/protected.txt"))

        code, out, err = run_cli(root, "approve", "short")
        if code == 0:
            problems.append("`approve` accepted a reason shorter than min_reason_len")

        code, out, err = run_cli(root, "approve", "the intake is complete")
        if code != 0:
            problems.append("`approve` from asking exited %d: %s" % (code, (err or out).strip()[-200:]))
        if read(".orchestrator/state").strip() != "planning":
            problems.append("`approve` from asking left the state at %r, not planning"
                            % read(".orchestrator/state").strip())

        code, out, err = run_cli(root, "approve", "the plan is approved")
        if code != 0:
            problems.append("`approve` from planning exited %d: %s"
                            % (code, (err or out).strip()[-200:]))
        if read(".orchestrator/state").strip() != "executing":
            problems.append("`approve` from planning left the state at %r, not executing"
                            % read(".orchestrator/state").strip())
        if "approved_by=human" not in read(".orchestrator/facts"):
            problems.append("the plan approval recorded no approved_by: %r"
                            % read(".orchestrator/facts"))
        log = read(".orchestrator/journal.log")
        for expected in ("protect src/notes.md", "unprotect src/notes.md", "approved:"):
            if expected not in log:
                problems.append("the journal has no line for %r" % expected)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    assert not problems, "; ".join(problems)


def check_install_deploys():
    """One command must put a working gate into another directory, without touching what is not its own.

    Installing is the one operation that writes files somewhere this gate does not watch, so nothing
    else in this suite would notice if it wrote the wrong ones - and the two it must never replace are
    the target's policy (the human's program) and the target's protected list (the reason for installing
    into another repository at all). It must also not carry this repository's README across: an install
    that overwrote someone else's readme would be the rudest thing this program could do.
    """
    target = tempfile.mkdtemp(prefix="ocf-install-")
    problems = []
    try:
        code, message = install_into(target)
        if code != 0:
            problems.append("install exited %d: %s" % (code, message.strip()[-300:]))
        for relative in (".github/ocf/ocf.py", ".github/ocf/tests/run.py",
                         ".github/hooks/orchestrator.json", ".github/protected.txt",
                         ".github/copilot-instructions.md", ".orchestrator/state"):
            if not os.path.exists(os.path.join(target, relative.replace("/", os.sep))):
                problems.append("install left no %s in the target" % relative)
        if os.path.exists(os.path.join(target, "README.md")):
            problems.append("install copied this repository's README into the target")
        code, out_text, _ = run_cli(REPO, "install", REPO)
        if code == 0:
            problems.append("install accepted this repository as its own target, so it could replace "
                            "the gate's source with a copy of itself")
        code, out_text, err_text = run_cli(target, "verify")
        if code != 0:
            problems.append("the freshly installed target does not pass its own verify: %s"
                            % (out_text or err_text).strip()[-300:])
    finally:
        shutil.rmtree(target, ignore_errors=True)
    assert not problems, "; ".join(problems)


def check_package_builds_a_release():
    """The release tree must hold the payload, the release note and the version.

    This is a maintainer's check and only runs in a source checkout: in a copy deployed into another
    repository there is no `release/` and no `VERSION`, and that case asserts the refusal instead of
    skipping. A check that quietly does nothing when it cannot run is the failure this suite is built
    to catch.

    The last assertion is the point of the release: a repository deployed from the built tree has to
    pass its own `verify`. That is "somebody else can install this", checked rather than hoped for.
    """
    ocf = load_ocf_module()
    policy, _ = ocf.load_policy(REPO)
    out_dir = tempfile.mkdtemp(prefix="ocf-package-")
    target = tempfile.mkdtemp(prefix="ocf-release-target-")
    problems = []
    try:
        if not (os.path.isdir(os.path.join(REPO, "release", "payload"))
                and os.path.exists(os.path.join(REPO, "VERSION"))):
            code, out_text, err_text = run_cli(REPO, "package", out_dir)
            text = out_text + err_text
            if code == 0:
                problems.append("package built a release with no release/payload and no VERSION")
            elif "release/payload" not in text and "VERSION" not in text:
                problems.append("package refused without saying what is missing: %r" % text[:200])
            assert not problems, "; ".join(problems)
            return
        code, out_text, err_text = run_cli(REPO, "package", out_dir)
        if code != 0:
            problems.append("package exited %d: %s" % (code, (err_text or out_text).strip()[-300:]))
        with open(os.path.join(REPO, "VERSION"), "r", encoding="utf-8") as handle:
            version = handle.read().strip()
        stage = os.path.join(out_dir, "ocf-gate-%s" % version)
        for relative in (".github/ocf/ocf.py", ".github/ocf/tests/run.py",
                         ".github/hooks/orchestrator.json", ".github/protected.txt",
                         "INSTALL.txt", "VERSION"):
            if not os.path.exists(os.path.join(stage, relative.replace("/", os.sep))):
                problems.append("the release tree holds no %s" % relative)
        code, out_text, err_text = run_cli(stage, "install", target)
        if code != 0:
            problems.append("installing from the release tree exited %d: %s"
                            % (code, (err_text or out_text).strip()[-300:]))
        else:
            code, out_text, err_text = run_cli(target, "verify")
            if code != 0:
                problems.append("a repository deployed from the release tree does not verify: %s"
                                % (out_text or err_text).strip()[-300:])
        # A payload that is not what it says it is has to be refused before anything is written. The
        # stage is corrupted on purpose here: one line appended to the program it ships.
        with open(os.path.join(stage, ".github", "ocf", "ocf.py"), "a",
                  encoding="utf-8", newline="\n") as handle:
            handle.write("# tampered with after packaging\n")
        bystander = tempfile.mkdtemp(prefix="ocf-damaged-target-")
        try:
            code, out_text, err_text = run_cli(stage, "install", bystander)
            if code == 0:
                problems.append("install accepted a payload whose files do not match its manifest")
            elif "MANIFEST" not in (out_text + err_text):
                problems.append("install refused a damaged payload without naming the manifest: %r"
                                % (out_text + err_text)[:200])
            if os.path.exists(os.path.join(bystander, ".github", "ocf", "ocf.py")):
                problems.append("install wrote into the target before refusing the payload")
        finally:
            shutil.rmtree(bystander, ignore_errors=True)
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
        shutil.rmtree(target, ignore_errors=True)
    assert not problems, "; ".join(problems)


INTAKE_FACT_KEYS = ("grill_rounds", "consensus", "grill_used")


def run_hook_submit(root, prompt):
    """Drive the real hook with a UserPromptSubmit payload. Returns its exit code."""
    env = dict(os.environ)
    env["OCF_ROOT"] = root
    env["PYTHONIOENCODING"] = "utf-8"
    payload = {"hook_event_name": "UserPromptSubmit", "prompt": prompt, "session_id": "abcdef01"}
    proc = subprocess.run([sys.executable, ENTRY, "hook"], input=json.dumps(payload).encode("utf-8"),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=root)
    return proc.returncode


def check_intake_closes_on_leaving_asking():
    """Evidence for the intake must not outlive it, and must not die with an interruption.

    `grill-valid` reads three facts and runs on `asking -> planning`, so those facts may only exist
    while the machine is in `asking`. Leaving `asking` has two doors - the agent's `advance` and the
    human's `approve` - and entering it has more than one. A check that watched a single door would
    pass while the others leaked one intake's evidence into the next.

    The `blocked` cases are asserted in the opposite direction on purpose. A trip through `blocked`
    interrupts the intake rather than ending it, so the rounds the human already spent there have to
    survive. That is also what keeps this check able to fail: making `blocked` clear as well, or
    dropping any one of the door-side calls, turns one of these cases red.
    """
    base = {"goal": "ship the fix", "tools": "python", "references": "the README",
            "deliverables": "a passing suite", "code_style": "short lines",
            "docs_decision": "skip", "stack_env": "python 3.12", "p0_count": "0",
            "grill_rounds": "3", "consensus": "we agreed on the goal", "grill_used": "me"}
    # label, the state to start in, the commands to run, and whether the intake's evidence is
    # expected to survive them.
    cases = (
        ("leaving for ready", "asking", (("advance", "ready", "the question was answered"),), False),
        ("leaving for planning", "asking", (("advance", "planning", "the intake is done"),), False),
        ("leaving through approve", "asking", (("approve", "the intake is complete"),), False),
        ("parked in blocked", "asking", (("advance", "blocked", "stuck"),), True),
        ("resumed from blocked", "asking", (("advance", "blocked", "stuck"),
                                           ("advance", "asking", "the human replied")), True),
        ("entering from executing", "executing", (("advance", "asking", "reopen it"),), False),
        ("entering through reject", "planning", (("reject", "start over"),), False),
    )
    problems = []
    for label, start, commands, survives in cases:
        root = build_root({"state": start, "facts": dict(base)})
        try:
            # build_root does not write this file and `advance` does not create it, so without
            # seeding it the assertion below would be "absent stays absent" - always true.
            with open(os.path.join(root, ".orchestrator", "prompt-log"), "w",
                      encoding="utf-8", newline="\n") as handle:
                handle.write("seeded | 00000000 | asking | a human reply\n")
            for command in commands:
                code, out, err = run_cli(root, *command)
                if code != 0:
                    problems.append("%s: `%s` exited %d: %s"
                                    % (label, " ".join(command), code, (err or out).strip()[-160:]))
            with open(os.path.join(root, ".orchestrator", "facts"), "r", encoding="utf-8") as handle:
                facts_text = handle.read()
            left = [key for key in INTAKE_FACT_KEYS if key + "=" in facts_text]
            transcript = os.path.exists(os.path.join(root, ".orchestrator", "prompt-log"))
            if survives and not left:
                problems.append("%s: the intake facts went away, but a trip through `blocked` "
                                "interrupts the intake rather than ending it" % label)
            if survives and not transcript:
                problems.append("%s: the transcript was dropped, but the intake is still open" % label)
            if not survives and left:
                problems.append("%s: the intake facts survived (%s), so `grill-valid` can be "
                                "satisfied by the previous intake's evidence" % (label, ", ".join(left)))
            if not survives and transcript:
                problems.append("%s: the transcript survived the intake closing" % label)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    # The hook's own door into `asking`, which the cases above cannot reach: it must drop the previous
    # intake's counters, and it must KEEP the transcript, because at that instant the transcript is
    # the first entry of the intake that is starting - not the last one of the intake that ended.
    root = build_root({"state": "ready", "facts": dict(base)})
    try:
        code = run_hook_submit(root, "just a question")
        if code != 0:
            problems.append("the hook exited %d on a UserPromptSubmit" % code)
        with open(os.path.join(root, ".orchestrator", "facts"), "r", encoding="utf-8") as handle:
            facts_text = handle.read()
        left = [key for key in INTAKE_FACT_KEYS if key + "=" in facts_text]
        if left:
            problems.append("entering asking through the hook left the previous intake's facts (%s)"
                            % ", ".join(left))
        if not os.path.exists(os.path.join(root, ".orchestrator", "prompt-log")):
            problems.append("the hook dropped the transcript of the intake it had just opened")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    assert not problems, "; ".join(problems)


CHECKS = (
    ("transition-table", check_transition_table),
    ("gated-transitions", check_gated_transitions),
    ("new-rule-shape", check_new_rule_shape),
    ("soft-rules-never-gate", check_soft_rules_never_gate),
    ("line-endings", check_line_endings_are_normalised),
    ("rule-text-flattened", check_rule_text_is_flattened),
    ("human-cli", check_human_cli),
    ("install-deploys", check_install_deploys),
    ("package-builds-a-release", check_package_builds_a_release),
    ("prompt-instrumentation", check_prompt_instrumentation),
    ("intake-closes", check_intake_closes_on_leaving_asking),
)


def main():
    failures = 0
    for label, check in CHECKS:
        try:
            check()
        except AssertionError as exc:
            failures += 1
            print("FAIL  %s" % label)
            for part in str(exc).split("; "):
                print("        %s" % part)
        else:
            print("PASS  %s" % label)
    print("")
    print("%d problem(s)" % failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
