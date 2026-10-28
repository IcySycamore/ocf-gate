#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Checks on the engine, and on this repository's own conventions. Not shipped: a deployer cannot act
on a failure by either kind, because neither is about their policy, their tools or their documents.

The one test for which side a check belongs on: if it fails, can the person reading the message act on
it? If they can, it belongs in `.github/ocf/tests/run.py`, which travels with the payload. If the
message names something they do not have and should not have, it belongs here, where `build.ps1` runs
it before packaging and it reaches nobody.

They import their helpers from the shipped suite rather than growing a second copy of `build_root`,
`load_ocf_module`, `iter_markdown_files` and the temp-repo plumbing.
"""

import json
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
TESTS = os.path.join(REPO, ".github", "ocf", "tests")
sys.path.insert(0, TESTS)

from run import (ENTRY, POLICY, build_root, install_into, iter_markdown_files,  # noqa: E402
                 load_ocf_module, run_cli, set_section_enabled)

# Both moved here with the checks that use them; they have no other reader.
#
# The gates table is keyed by the state being ENTERED, so the expectation is written as which targets
# must be gated and which must not be. Written out rather than read from the table: a check that reads
# its own expectation out of the thing it checks stays green when that thing is renamed or emptied.
GATED_TARGETS = ("planning", "executing")
# `blocked` is the bypass - the state a machine enters when it cannot go on. It is the one place that
# must ask nothing, and a bypass that asks is not a bypass.
UNGATED_TARGETS = ("blocked",)
# The routes worth walking: every way INTO a gated state that the machine can actually take. The one
# from `blocked` is here on purpose - leaving the bypass used to run no gate at all, and a table keyed
# by origin made that look like a designed exemption instead of the hole it was.
GATED_ROUTES = (("asking", "planning"), ("blocked", "planning"), ("planning", "executing"))

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
    for target in table["gates"]:
        if target not in ocf.STATES:
            problems.append("a gate key names %r, which is not a state" % target)
        mentioned.add(target)
    if table["bypass"] in table["gates"]:
        problems.append("the bypass %r is gated, so entering or leaving it asks something of the "
                        "machine; a bypass that asks is not a bypass" % table["bypass"])
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
    """Arriving somewhere gated runs gates, whoever you are and wherever you came from.

    The expectation is written out above rather than taken from the table, because a check that iterates
    the table to build its own expectation stays green when a key is renamed or emptied - and that is
    exactly the omission it exists to catch. `transition_gate_names` falls back to an empty tuple for an
    unknown destination, so a renamed key would advance the machine with no gate running and nothing
    printed. That is also what `blocked` did: the table had no key for it, so the way out of the bypass
    was free.

    `blocked` is asserted to be ungated as well, from the other side - it is the one destination that
    must ask nothing.
    """
    ocf = load_ocf_module()
    problems = []
    for route in GATED_ROUTES:
        for name in route:
            if name not in ocf.STATES:
                problems.append("%s names %r, which is not a state in STATES" % (route, name))
        names = ocf.transition_gate_names(*route)
        if not names:
            problems.append("the transition %s -> %s runs no gate at all, so it would advance in "
                            "silence" % route)
        for name in names:
            if name not in ocf.GATES:
                problems.append("the transition %s -> %s names a gate the engine does not have: %s"
                                % (route[0], route[1], name))
    for target in UNGATED_TARGETS:
        if ocf.transition_gate_names("asking", target):
            problems.append("entering %s runs gates; it is the bypass and must ask nothing" % target)
    for target in ocf.TRANSITIONS["gates"]:
        if target not in ocf.STATES:
            problems.append("a key of the transition table names %r, which is not a state" % target)
    for target in GATED_TARGETS:
        if target not in ocf.TRANSITIONS["gates"]:
            problems.append("nothing gates arriving at %s, so the machine can walk in ungated"
                            % target)
    assert not problems, "; ".join(problems)


def check_gate_reports_what_is_in_front():
    """`gate` with no argument reports the gates in front of the machine, and only those.

    Running all seven by default meant `grill-valid` printed FAIL in every state but `asking`, because
    it reads intake facts that leaving `asking` clears on purpose - so the ordinary invocation always
    exited 1. A diagnostic that is wrong in the ordinary case is one the human learns to stop reading,
    and this is the only diagnostic there is.

    The other direction is asserted too: `blocked` is not on the flow, so nothing is in front of it.
    Inventing a transition for it would be the same mistake mirrored.

    The expectation is written out per state. Deriving both sides from the flow would make this pass by
    construction, which is indistinguishable from having no check.
    """
    ocf = load_ocf_module()
    expected = {
        "ready": (),
        "asking": ("context", "docs-decision", "grill-valid"),
        "planning": ("plan-schema", "zero-p0", "protected-list-clear", "stack-env"),
        "executing": (),
        "reporting": (),
        "blocked": (),
    }
    problems = []
    for state, names in expected.items():
        actual = tuple(ocf.gate_names_in_front(state))
        if actual != names:
            problems.append("%s: %s is in front of it, expected %s"
                            % (state, actual or "nothing", names or "nothing"))
    assert not problems, "; ".join(problems)


def check_github_folder_is_ascii():
    """Every file under .github is ASCII, and the exemption list is empty on purpose.

    A leading BOM is stripped first: it is an encoding mark rather than content, and the previous
    PowerShell implementation genuinely needed one. Build output is skipped, because a .pyc is not
    source.

    This is a rule about how THIS repository writes. It used to ship, which meant a deployer who wrote
    their own policy messages in their own language met a failure whose message told them to stop - and
    `.github` is theirs, so it was accusing the wrong person. Moving it here keeps the check and points
    it at the only person who agreed to it.
    """
    offenders = []
    for folder, folders, files in os.walk(os.path.join(REPO, ".github")):
        folders[:] = [name for name in folders if name not in ("__pycache__", ".pytest_cache")]
        for name in files:
            path = os.path.join(folder, name)
            with open(path, "rb") as handle:
                data = handle.read()
            if data.startswith(b"\xef\xbb\xbf"):
                data = data[3:]
            if any(byte > 127 for byte in data):
                offenders.append(os.path.relpath(path, REPO).replace("\\", "/"))
    assert not offenders, ("non-ASCII bytes in: %s. Nothing under .github is exempt, because a file the "
                           "toolchain reads is a file the toolchain has to encode consistently"
                           % ", ".join(offenders))


WORKFLOW = os.path.join(REPO, ".github", "work-control-flow.md")
# The sections other files cite by number. A citation is a number, so renumbering the document silently
# re-points every one of them and nothing complains: the agent simply follows the wrong section. The
# titles below are what this check believes that document's numbering is, and they are compared with
# the document on every run - so the list cannot drift, it can only be deliberately updated.
CITED_SECTIONS = {2: "Commands", 5: "Intake", 6: "Planning and approval", 7: "Terminal discipline",
                  8: "Delivery", 9: "Limits and switches", 10: "Known limitations"}
SECTION_HEADING = re.compile(r"(?m)^##\s+(\d+)\.\s+(.+?)\s*$")
CITATION = re.compile(r"section\s+(\d+)|\u7b2c\s*(\d+)\s*\u8282|\u00a7\s*(\d+)")


def check_section_references():
    """Every "see section N" in the markdown still points at the section it was written about.

    Asserted here rather than in the shipped suite because the numbering is this repository's.
    """
    with open(WORKFLOW, "r", encoding="utf-8") as handle:
        headings = {int(number): title for number, title in SECTION_HEADING.findall(handle.read())}
    problems = []
    if sorted(headings) != list(range(1, len(headings) + 1)):
        problems.append("the sections are numbered %s, so no citation can be trusted to land"
                        % ", ".join(str(number) for number in sorted(headings)))
    for number, title in sorted(CITED_SECTIONS.items()):
        if number not in headings:
            problems.append("section %d (%s) is cited from other files and is gone" % (number, title))
        elif headings[number] != title:
            problems.append("section %d is %r, and the files citing it by number expect %r - a "
                            "renumbering re-points every one of them"
                            % (number, headings[number], title))
    for path in iter_markdown_files():
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        for index, line in enumerate(lines, 1):
            for groups in CITATION.findall(line):
                number = int([group for group in groups if group][0])
                if number not in headings:
                    problems.append("%s:%d cites section %d, which does not exist"
                                    % (os.path.relpath(path, REPO), index, number))
    assert not problems, "; ".join(problems)


def check_readme_matches_the_policy():
    """Both READMEs list every rule by name, and both carry the state flow.

    The list is hand-written, so it is a second copy of the policy - the kind that drifts. Asserting it
    is what makes the copy safe: a renamed rule or a new one fails here until the README follows, and a
    renumbered flow fails too. The READMEs are at the repository root and ship in nothing, so this
    belongs on this side of the line.
    """
    ocf = load_ocf_module()
    policy, warnings = ocf.load_policy(REPO)
    for kind, text in warnings:
        raise AssertionError("the policy did not load cleanly: [%s] %s" % (kind, text))
    ids = [rule.get("id") for rule in policy.get("rule", [])]
    flow = " \u2192 ".join(ocf.TRANSITIONS["flow"])
    problems = []
    for name in ("README.md", "README_CH.md"):
        with open(os.path.join(REPO, name), encoding="utf-8") as handle:
            text = handle.read()
        missing = sorted(rule_id for rule_id in ids if "`%s`" % rule_id not in text)
        if missing:
            problems.append("%s does not list %s" % (name, ", ".join(missing)))
        if flow not in text:
            problems.append("%s does not carry the state flow %r" % (name, flow))
        if ocf.TRANSITIONS["bypass"] not in text:
            problems.append("%s does not name the bypass state %r"
                            % (name, ocf.TRANSITIONS["bypass"]))
    assert not problems, "; ".join(problems)


def check_rule_counts_in_docs():
    """Both READMEs state how many rules there are, and nothing was comparing that to the file.

    It is not decoration. The count is the only number in the documentation that describes the
    policy's own size, and it is the number a reader uses to tell whether the rules they are reading
    about are all of them.

    During the batch that folded the two self-protection rules into the protected list, this check
    would have been the only thing to notice that three soft rules had gone missing. The policy still
    parsed, `policy_findings` still reported nothing, and `selftest` still passed - because a soft rule
    with an occasion and a sentence is a valid rule no matter what the sentence says. A count that is
    asserted in two languages is cheap, and it is the difference between a mistake being found by a
    check and being found by somebody counting by hand later.

    Both halves are read out of the policy by kind, and out of both READMEs by a pattern that tolerates
    the two languages' word order. A README that stops mentioning the count at all fails too: a count
    that can be deleted instead of corrected is one that can go stale in silence.
    """
    ocf = load_ocf_module()
    policy, warnings = ocf.load_policy(REPO)
    for kind, text in warnings:
        raise AssertionError("the policy did not load cleanly: [%s] %s" % (kind, text))
    rules = policy.get("rule", [])
    actual = [len([rule for rule in rules if rule.get("kind") != "soft"]),
              len([rule for rule in rules if rule.get("kind") == "soft"])]
    pattern = re.compile(r"(\d+)\s*(?:hard|条硬)[^\d]{0,24}?(\d+)\s*(?:soft|条软)")
    problems = []
    for name in ("README.md", "README_CH.md"):
        with open(os.path.join(REPO, name), encoding="utf-8") as handle:
            text = handle.read()
        found = pattern.findall(text)
        if not found:
            problems.append("%s no longer states the rule counts at all" % name)
            continue
        for hard, soft in found:
            if [int(hard), int(soft)] != actual:
                problems.append("%s says %s hard and %s soft rules; policy.toml holds %d and %d; "
                                "the rules are counted by kind, so update the sentence or the rules"
                                % (name, hard, soft, actual[0], actual[1]))
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
                 "message": "deny", "if": {"occasion": "on-plan"}}
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


def check_prompt_log():
    """A UserPromptSubmit must leave a record, because that record is what settles who spoke.

    The gate has no reliable signal for "a human really replied". Until the instrumentation answers
    that, no rule may depend on one - a rule built on an unverified signal fails by locking the agent
    out, which is the other accident this project exists to prevent.
    """
    root = tempfile.mkdtemp(prefix="ocf-prompt-")
    try:
        os.makedirs(os.path.join(root, ".github", "ocf"))
        os.makedirs(os.path.join(root, ".orchestrator"))
        # Copied, then installed. This drives the real entry point, so the copy has to have the event
        # installed and the master switch on - otherwise the hook answers nothing and the check reports
        # on whatever the live switches happen to be. It did exactly that: with `[hooks] enabled` set to
        # false in the working policy, this check failed with "a UserPromptSubmit wrote no prompt-log",
        # which was true of the switch and false of the log.
        with open(POLICY, "r", encoding="utf-8") as handle:
            installed = handle.read()
        installed = set_section_enabled(set_section_enabled(installed, "hooks", "true"),
                                        "system", "true")
        with open(os.path.join(root, ".github", "ocf", "policy.toml"), "w", encoding="utf-8",
                  newline="\n") as handle:
            handle.write(installed)
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
        # `protect` refuses an entry that would cover nothing, so the fixture has to contain the file
        # it is asked to protect. That refusal is asserted immediately after, on a path that does not
        # exist: writing an entry that protects nothing and echoing it as success is what this list
        # used to do, and it is how a run of stray arguments once put sixteen lines of junk into a
        # human's list while printing `covers 0 file(s)` on every one of them.
        os.makedirs(os.path.join(root, "src"))
        with open(os.path.join(root, "src", "notes.md"), "w", encoding="utf-8") as handle:
            handle.write("notes\n")
        code, out, err = run_cli(root, "protect", "src/notes.md")
        if code != 0:
            problems.append("`protect` exited %d: %s" % (code, (err or out).strip()[-200:]))
        if "src/notes.md" not in read(".github/protected.txt"):
            problems.append("`protect` did not add the path: %r" % read(".github/protected.txt"))
        code, out, err = run_cli(root, "protect", "src/missing.md")
        if code == 0:
            problems.append("`protect` accepted a path that covers no file, so the list can fill up "
                            "with entries that protect nothing")
        if "src/missing.md" in read(".github/protected.txt"):
            problems.append("`protect` wrote an entry it had just refused: %r"
                            % read(".github/protected.txt"))
        if "src/missing.md" not in (err or out):
            problems.append("the refusal did not name the path, so it cannot be acted on: %r"
                            % (err or out).strip()[:200])
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
        for expected in ("protect src/notes.md", "unprotect src/notes.md", "approved by human:"):
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
        # A deployed gate with the maintenance window open is the failure this project exists to
        # remove: the wiring is there, `verify` passes, the canaries still deny, and every rule that
        # follows the switch is suspended. 0.2.0 shipped exactly that, so it is read rather than
        # assumed - `verify` cannot see it.
        with open(os.path.join(target, ".github", "ocf", "policy.toml"), "r", encoding="utf-8") as handle:
            installed = handle.read().partition("[system]")[2].split("[", 1)[0]
        if "enabled = true" not in installed:
            problems.append("install deployed a policy whose maintenance window is open (%r), so the "
                            "installed gate suspends every rule that follows the switch"
                            % " ".join(installed.split())[:80])
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
        # The one file the release does not ship as written: a gate deployed with the maintenance
        # window open is a gate that decides nothing while looking installed, and 0.2.0 shipped that
        # way. Read here rather than trusted, because this is the artifact people install.
        with open(os.path.join(stage, ".github", "ocf", "policy.toml"), "r", encoding="utf-8") as handle:
            shipped = handle.read()
        if "[system]" not in shipped:
            problems.append("the shipped policy has no [system] section, so the window cannot be read")
        else:
            window = shipped.partition("[system]")[2].split("[", 1)[0]
            if "enabled = true" not in window:
                problems.append("the release ships a policy whose maintenance window is open (%r), so "
                                "the deployed gate suspends every rule that follows the switch"
                                % " ".join(window.split())[:80])
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
    return run_hook_capture(root, prompt)[0]


def run_hook_capture(root, prompt):
    """Drive the real hook with a UserPromptSubmit payload. Returns (exit code, stdout)."""
    env = dict(os.environ)
    env["OCF_ROOT"] = root
    env["PYTHONIOENCODING"] = "utf-8"
    payload = {"hook_event_name": "UserPromptSubmit", "prompt": prompt, "session_id": "abcdef01"}
    proc = subprocess.run([sys.executable, ENTRY, "hook"], input=json.dumps(payload).encode("utf-8"),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=root)
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


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
            # Absence is read as empty rather than crashing: the sweep that ends a round removes the
            # facts file outright, which is a stronger statement than "no intake key is left inside
            # it", and a check that raises on a correct outcome is worse than no check at all.
            facts_path = os.path.join(root, ".orchestrator", "facts")
            facts_text = ""
            if os.path.exists(facts_path):
                with open(facts_path, "r", encoding="utf-8") as handle:
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


def seed_round_files(root, files):
    """Put a round's artifacts in place, so "did the boundary take them away" is a real question.

    Without this the assertion is "absent stays absent", which is true of every implementation
    including one with the sweep deleted - the same mistake the intake check above had to seed its
    way out of.
    """
    for name, text in files.items():
        with open(os.path.join(root, ".orchestrator", name), "w",
                  encoding="utf-8", newline="\n") as handle:
            handle.write(text)


def facts_values(root):
    path = os.path.join(root, ".orchestrator", "facts")
    values = {}
    if not os.path.exists(path):
        return values
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle.read().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
    return values


def facts_keys(root):
    return sorted(facts_values(root))


def run_hook_payload(root, payload):
    """Drive the real hook with one exact payload. Returns (exit code, stdout)."""
    env = dict(os.environ)
    env["OCF_ROOT"] = root
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run([sys.executable, ENTRY, "hook"], input=json.dumps(payload).encode("utf-8"),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=root)
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def check_p0_comes_from_the_report():
    """`p0_count` comes off a subagent's report, and does not survive one that carries no count.

    `zero-p0` is the gate that lets work start, so this fact is an authorization. It has no writer on
    the command line - `p0-is-an-audit-result` refuses that, and the shipped suite pins the refusal -
    which leaves the hook reading the auditor's own report, the one artifact in the round that the
    agent asking for the audit did not write.

    The clearing half is the one that is easy to leave out and impossible to notice from the outside:
    a zero left standing from an earlier audit is exactly the value the gate accepts, so a report
    with no count has to remove the number rather than leave it where it was.
    """
    problems = []
    for report, expected, label in (("## P0 (0)\n\nthe plan is clean\n", "0", "a clean report"),
                                    ("## P0 (3)\n\n- a real defect\n", "3", "a report with defects")):
        root = build_root({"state": "planning", "facts": {"p0_count": "9"}})
        try:
            payload = {"hook_event_name": "PostToolUse", "tool_name": "runSubagent",
                       "tool_input": {"agentName": "Plan Auditor", "description": "audit it"},
                       "tool_response": report}
            code, _ = run_hook_payload(root, payload)
            if code != 0:
                problems.append("%s: the hook exited %d" % (label, code))
            got = facts_values(root).get("p0_count")
            if got != expected:
                problems.append("%s: p0_count is %r rather than %r, so the gate is reading something "
                                "other than the report" % (label, got, expected))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    root = build_root({"state": "planning", "facts": {"p0_count": "0"}})
    try:
        payload = {"hook_event_name": "PostToolUse", "tool_name": "runSubagent",
                   "tool_input": {"agentName": "Criterion Picker"},
                   "tool_response": "I picked a way to reproduce it.\n"}
        code, _ = run_hook_payload(root, payload)
        if code != 0:
            problems.append("a report with no count: the hook exited %d" % code)
        if "p0_count" in facts_values(root):
            problems.append("a report carrying no P0 heading left the previous audit's count "
                            "standing, so `zero-p0` can pass on an audit that is not this one")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    assert not problems, "; ".join(problems)


# What a round leaves on disk besides the facts: the plan it was approved to do, the intake that
# produced it, and the two files that have to survive the boundary.
ROUND_ARTIFACTS = {"plan.md": "# Plan\n\n## Steps\n\n1. do it\n\n## Files\n\n`a.py`\n",
                   "prompt-log": "seeded | 00000000 | asking | a human reply\n",
                   "exec.log": "1\tpython x\n",
                   "glossary.md": "term: meaning\n"}
ROUND_SURVIVES = {"exec.log": "1\tpython x\n", "glossary.md": "term: meaning\n"}


def check_round_state_cleared():
    """Entering `ready` ends the round: the facts, the plan and the transcript go, the record stays.

    `zero-p0` is a gate and `p0_count` is the number behind it, so the point of the sweep is that the
    number cannot outlive the round it was given in. A verdict that survives is a verdict the next
    round can be satisfied by without ever being audited - which is the whole failure the gate exists
    to prevent, and it would be invisible: the suite would keep passing while the gate quietly stopped
    asking for an audit.

    Every route into `ready` is walked, not the `reporting -> ready` edge alone. The machine can
    always get back to `ready` by way of `blocked`, so a sweep that watched only the direct edge would
    leave the round standing on the interrupted path.

    The counter-case is what keeps this able to fail in the other direction: `blocked` interrupts the
    round rather than ending it, so entering it must clear nothing at all. A sweep that ran on every
    `advance` would pass every case above and fail this one.
    """
    base = {"goal": "ship the fix", "tools": "python", "references": "the README",
            "deliverables": "a passing suite", "code_style": "short lines",
            "docs_decision": "skip", "stack_env": "python 3.12", "p0_count": "0",
            "grill_rounds": "3", "consensus": "we agreed on the goal", "grill_used": "me"}
    ends = (
        ("asking -> ready", "asking", (("advance", "ready", "the question was answered"),)),
        ("blocked -> ready", "blocked", (("advance", "ready", "resuming after the interruption"),)),
        ("reporting -> blocked -> ready", "reporting",
         (("advance", "blocked", "stuck"), ("advance", "ready", "resuming after the interruption"))),
    )
    problems = []
    for label, start, commands in ends:
        root = build_root({"state": start, "facts": dict(base)})
        try:
            seed_round_files(root, ROUND_ARTIFACTS)
            for command in commands:
                code, out, err = run_cli(root, *command)
                if code != 0:
                    problems.append("%s: `%s` exited %d: %s"
                                    % (label, " ".join(command), code, (err or out).strip()[-160:]))
            left = facts_keys(root)
            if left:
                problems.append("%s: %s survived the round, so the previous audit is still standing "
                                "and `zero-p0` can be satisfied without a new one"
                                % (label, ", ".join(sorted(left))))
            for name in ("plan.md", "prompt-log"):
                if os.path.exists(os.path.join(root, ".orchestrator", name)):
                    problems.append("%s: %s belongs to the round that ended and is still there"
                                    % (label, name))
            for name, text in ROUND_SURVIVES.items():
                path = os.path.join(root, ".orchestrator", name)
                if not os.path.exists(path):
                    problems.append("%s: %s is part of the record and the sweep removed it"
                                    % (label, name))
                    continue
                with open(path, "r", encoding="utf-8") as handle:
                    if handle.read() != text:
                        problems.append("%s: %s was rewritten, and the record is not the sweep's to "
                                        "change" % (label, name))
            if not os.path.exists(os.path.join(root, ".orchestrator", "state")):
                problems.append("%s: the sweep removed the state file it was moving" % label)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    root = build_root({"state": "reporting", "facts": dict(base)})
    try:
        seed_round_files(root, ROUND_ARTIFACTS)
        code, out, err = run_cli(root, "advance", "blocked", "stuck")
        if code != 0:
            problems.append("`advance blocked` exited %d: %s" % (code, (err or out).strip()[-160:]))
        if "p0_count" not in facts_keys(root):
            problems.append("`blocked` cleared the round's facts, but the bypass interrupts a round "
                            "instead of ending it")
        for name in ("plan.md", "prompt-log"):
            if not os.path.exists(os.path.join(root, ".orchestrator", name)):
                problems.append("`blocked` removed %s, but the round is still running" % name)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    assert not problems, "; ".join(problems)


# The moments that no state delivers, because they are not about a state: `session-start` is about the
# conversation beginning and arrives on the session hook, and `on-mistake` is about a command that
# failed and arrives on the post-tool hook, whenever that happens rather than in one state.
OUTSIDE_STATE_MOMENTS = ("session-start", "on-mistake")


def check_state_moments():
    """A soft rule must reach the model in the states its occasion names, and in no others.

    Handing over every soft rule on every turn was the thing this replaced, and the way to get it
    wrong is quiet in both directions: a rule whose occasion is in no state's row stops arriving
    without a word, and a table that is built but never consulted looks identical to one that works -
    the contract the model receives is the only place the difference shows, and nobody reads that
    every turn.

    So both halves are here: the table covers the states and speaks the engine's own vocabulary, and
    the hook actually filters by it. The vocabulary half is written out rather than read from
    `SOFT_OCCASIONS`, because a check that reads its expectation out of the thing it checks stays
    green when that thing is emptied.
    """
    ocf = load_ocf_module()
    moments = ocf.STATE_MOMENTS
    problems = []
    for state in ocf.STATES:
        if state not in moments:
            problems.append("%s has no row, so a soft rule written for its moment never arrives" % state)
    for moment in ("session-start", "ask", "plan", "act", "answer", "on-mistake"):
        if moment not in ocf.SOFT_OCCASIONS:
            problems.append("the engine no longer knows the moment %r, so this check is out of date "
                            "with it" % moment)
    for state, listed in moments.items():
        unknown = [moment for moment in listed if moment not in ocf.SOFT_OCCASIONS]
        if unknown:
            problems.append("%s names %s, which is not a moment the engine knows"
                            % (state, ", ".join(unknown)))
    for moment in ocf.SOFT_OCCASIONS:
        if moment in OUTSIDE_STATE_MOMENTS:
            continue
        if not any(moment in listed for listed in moments.values()):
            problems.append("no state carries %r, so a rule written for it is dead on arrival"
                            % moment)

    probes = """
[[rule]]
id = "probe-plan-moment"
kind = "soft"
label = "probe-plan-moment"
enabled = true
message = "PROBE-PLAN-SENTENCE"

[rule.if]
occasion = "plan"

[[rule]]
id = "probe-ask-moment"
kind = "soft"
label = "probe-ask-moment"
enabled = true
message = "PROBE-ASK-SENTENCE"

[rule.if]
occasion = "ask"
"""
    for state, arrives, withheld in (("planning", "PROBE-PLAN-SENTENCE", "PROBE-ASK-SENTENCE"),
                                     ("asking", "PROBE-ASK-SENTENCE", "PROBE-PLAN-SENTENCE")):
        root = build_root({"state": state})
        try:
            with open(os.path.join(root, ".github", "ocf", "policy.toml"), "a",
                      encoding="utf-8") as handle:
                handle.write(probes)
            code, emitted = run_hook_capture(root, "carry on")
            if code != 0:
                problems.append("the hook exited %d on a UserPromptSubmit in %s" % (code, state))
            if arrives not in emitted:
                problems.append("in %s the contract does not carry the rule for that state's moment, "
                                "so the table is not being consulted: got %r"
                                % (state, emitted[-300:]))
            if withheld in emitted:
                problems.append("in %s the contract carries a rule whose moment is not in view, so "
                                "the model is reading guidance for a moment that is not happening"
                                % state)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    assert not problems, "; ".join(problems)


def load_score_module():
    path = os.path.join(REPO, ".github", "skills", "quality-self-evaluation", "scripts", "score.py")
    spec = importlib.util.spec_from_file_location("ocf_quality_score", path)
    if spec is None or spec.loader is None:
        raise AssertionError("the quality scorer cannot be loaded from its skill directory")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_quality_scoring():
    """The evaluator covers every current rule and keeps confidence separate from quality means.

    The arithmetic can look right while the compliance conclusion is dangerously wrong, so the
    fixtures below combine multiple deliverables, a missing hard-rule observation, and a confirmed
    hard violation. They also pin the true-N/A case separately from missing evidence.
    """
    scorer = load_score_module()
    ocf = load_ocf_module()
    live_ocf, live_policy, live_rules = scorer.load_rules(REPO)
    assert set(live_rules) == {rule.get("id") for rule in live_policy.get("rule", [])}, (
        "the evaluator's source of rule IDs differs from the policy the gate reads")
    policy = {"system": {"enabled": True}}
    rules = {
        "soft-answer": {"id": "soft-answer", "kind": "soft", "enabled": True,
                        "if": {"occasion": "answer"}},
        "soft-plan": {"id": "soft-plan", "kind": "soft", "enabled": True,
                       "if": {"occasion": "plan"}},
        "hard-write": {"id": "hard-write", "enabled": True,
                       "if": {"class": "write"}},
        "hard-exec": {"id": "hard-exec", "enabled": True,
                      "if": {"class": "exec"}},
        "disabled-soft": {"id": "disabled-soft", "kind": "soft", "enabled": False,
                          "if": {"occasion": "answer"}},
    }

    def record(rule_id, status, value=None):
        if status == "score":
            return {"rule_id": rule_id, "status": status, "score": value,
                    "evidence": "fixture evidence"}
        return {"rule_id": rule_id, "status": status, "reason": "fixture reason"}

    def artifact(identifier, kind, occasions, items):
        return {"id": identifier, "kind": kind, "occasions": occasions, "rules": items}

    answer_items = [record("soft-answer", "score", 4),
                    record("soft-plan", "not_applicable"),
                    record("hard-write", "not_applicable"),
                    record("hard-exec", "not_applicable"),
                    record("disabled-soft", "not_applicable")]
    first_doc = [record("soft-answer", "not_applicable"),
                 record("soft-plan", "score", 2),
                 record("hard-write", "unverified"),
                 record("hard-exec", "not_applicable"),
                 record("disabled-soft", "not_applicable")]
    second_doc = [record("soft-answer", "not_applicable"),
                  record("soft-plan", "score", 3),
                  record("hard-write", "score", 1),
                  record("hard-exec", "not_applicable"),
                  record("disabled-soft", "not_applicable")]
    data = {"artifacts": [artifact("answer", "answer", ["answer"], answer_items),
                          artifact("doc-a", "deliverable", ["plan"], first_doc),
                          artifact("doc-b", "deliverable", ["plan"], second_doc)]}
    result = scorer.evaluate(data, ocf, policy, rules)
    assert result["answer_mean"] == "4.0", result
    assert result["deliverables_mean"] == "2.0", result
    assert result["overall"] == "3.0", result
    assert result["quality_status"] == "PARTIAL", result
    assert result["hard_compliance"] == "FAIL", result
    assert result["artifacts"][0]["hard_compliance"] == "NOT_APPLICABLE", result
    assert result["artifacts"][1]["hard_compliance"] == "UNVERIFIED", result

    verified = json.loads(json.dumps(data))
    verified["artifacts"][1]["rules"][2] = record("hard-write", "score", 4)
    verified["artifacts"][2]["rules"][2] = record("hard-write", "score", 5)
    verified_result = scorer.evaluate(verified, ocf, policy, rules)
    assert verified_result["quality_status"] == "COMPLETE", verified_result
    assert verified_result["hard_compliance"] == "PASS", verified_result

    unresolved = json.loads(json.dumps(data))
    unresolved["artifacts"][1]["rules"][2] = record("hard-write", "score", 3)
    unresolved_result = scorer.evaluate(unresolved, ocf, policy, rules)
    assert unresolved_result["artifacts"][1]["hard_compliance"] == "UNVERIFIED", unresolved_result
    assert unresolved_result["hard_compliance"] == "FAIL", unresolved_result

    inactive_rules = {rule_id: dict(rule, enabled=False) for rule_id, rule in rules.items()}
    all_na = {"artifacts": [artifact("answer", "answer", ["answer"],
                                      [record(rule_id, "not_applicable")
                                       for rule_id in inactive_rules]) ]}
    unrated = scorer.evaluate(all_na, ocf, policy, inactive_rules)
    assert unrated["overall"] is None and unrated["quality_status"] == "UNRATED", unrated
    assert unrated["hard_compliance"] == "NOT_APPLICABLE", unrated
    assert scorer.display_number(__import__("decimal").Decimal("3.25")) == "3.3"

    def rejected(label, candidate, candidate_rules=rules):
        try:
            scorer.evaluate(candidate, ocf, policy, candidate_rules)
        except scorer.InputError:
            return
        raise AssertionError("the evaluator accepted %s" % label)

    bad = json.loads(json.dumps(all_na))
    bad["artifacts"][0]["rules"].pop()
    rejected("an artifact that omitted a live policy rule", bad, inactive_rules)

    bad = json.loads(json.dumps(all_na))
    bad["artifacts"][0]["rules"][0]["reason"] = "   "
    rejected("an empty N/A reason", bad, inactive_rules)

    bad = json.loads(json.dumps(all_na))
    bad["artifacts"][0]["rules"].append(dict(bad["artifacts"][0]["rules"][0]))
    rejected("a duplicate rule id", bad, inactive_rules)

    bad = json.loads(json.dumps(all_na))
    bad["artifacts"][0]["rules"][0]["rule_id"] = "not-a-policy-rule"
    rejected("an unknown rule id", bad, inactive_rules)

    bad = json.loads(json.dumps(data))
    bad["artifacts"][0]["rules"][0]["score"] = 6
    rejected("a score outside 1..5", bad)

    bad = json.loads(json.dumps(data))
    bad["artifacts"][0]["rules"][0]["evidence"] = ""
    rejected("a numeric score with empty evidence", bad)

    bad = json.loads(json.dumps(data))
    bad["artifacts"][0]["rules"][0] = record("soft-answer", "not_applicable")
    rejected("N/A for an active soft rule", bad)

    bad = json.loads(json.dumps(all_na))
    bad["artifacts"][0]["rules"][-1] = record("disabled-soft", "score", 5)
    rejected("a score for a disabled rule", bad, inactive_rules)


def check_session_start_briefing():
    """SessionStart must inject actual component results and the current workflow controls.

    The startup hook is the data source for a user's first report. If it omits the state or prints a
    hardcoded command list, the soft rule cannot reliably tell the human what is happening.
    """
    problems = []
    for state, armed, maintenance_status in (("ready", False, "WARN"),
                                            ("planning", False, "WARN"),
                                            ("reporting", True, "PASS")):
        root = build_root({"state": state, "enabled": armed})
        try:
            payload = {"hook_event_name": "SessionStart", "session_id": "briefing-test"}
            code, output = run_hook_payload(root, payload)
            if code != 0:
                problems.append("SessionStart exited %d in %s" % (code, state))
                continue
            lines = [line for line in output.splitlines() if line.startswith("{")]
            if not lines:
                problems.append("SessionStart emitted no JSON in %s" % state)
                continue
            emitted = json.loads(lines[-1])
            context = (emitted.get("hookSpecificOutput") or {}).get("additionalContext", "")
            # Matched against the de-wrapped text: these are multi-word phrases quoted out of a rule
            # whose wording is wrapped by hand, so asserting them against the raw string made an
            # unrelated reflow fail the build - the copy being load-bearing in the one place it should
            # not be. The absence checks below stay on the raw text, because they are about lines.
            flat = " ".join(context.split())
            for expected in ("OCF startup summary:",
                             "Maintenance: %s" % maintenance_status,
                             "report the system at session start",
                             "Goal / Requirements / Deliverables",
                             "report a bug with symptom", "expected result, actual",
                             "result, and reproduction steps"):
                if expected not in flat:
                    problems.append("SessionStart in %s omitted %r: %r"
                                    % (state, expected, context[-500:]))
            # The state, the flow line and the command groups belong to the standing contract, which
            # arrives on every turn. Restating them here is what made the per-turn `OCF state:` marker
            # ambiguous - and that marker is the only test that proves the hook is alive at all, so a
            # second spelling of the same fact has to fail rather than pass unnoticed.
            for absent in ("\n  State: ", "\n  Flow: ", "\n  agent: ", "\n  human: "):
                if absent in context:
                    problems.append("SessionStart in %s restated %r, which the standing contract "
                                    "already carries on every turn" % (state, absent.strip()))
            if "Hook wiring: FAIL" not in context:
                problems.append("the missing hook wiring was not reported as a failed component")
            if "State store: PASS" not in context:
                problems.append("the writable state store was not reported as a passing component")
        finally:
            shutil.rmtree(root, ignore_errors=True)

    assert not problems, "; ".join(problems)


def check_policy_comment_migration():
    """The explanations removed from policy must remain actionable in the language guides.

    Checked in both directions. Forwards: every explanation the file used to carry above a key is still
    reachable, named by an identifier that appears verbatim in both guides - which is why the terms below
    are code literals and not prose, since one guide is Chinese and a phrase would only pin one of them.
    Backwards: the policy carries no comment at all. Not above a key, not as a header, and not the
    generated vocabulary, which moved into the English guide along with the explanations. The header used
    to be pinned as a line count, which left "a file that quietly grew its comments back" as something
    only a reviewer could see; "no line in this file is a comment" is the same rule with nothing to count.
    """
    guides = (os.path.join(REPO, ".github", "ocf", "POLICY-GUIDE.md"),
              os.path.join(REPO, "POLICY-GUIDE_CH.md"))
    required = (
        ("P0 fact source", "p0-is-an-audit-result"),
        ("unread write target", "unread-write-target"),
        ("protected-path coverage", "touches-protected"),
        ("approval condition", "approval = true"),
        ("control-plane exception", "control_plane"),
        ("soft-rule delivery", "occasion"),
        ("tool class semantics", "view_image"),
        ("payload field path", "task.command"),
        ("env gating", "undeclared-env-command"),
        ("approval sentence demand", "min_reason_len"),
        ("allowed acting states", "allowed_states"),
        ("event table", "HOOK_EVENTS"),
        ("wiring file", "orchestrator.json"),
    )
    ocf = load_ocf_module()
    problems = []
    for path in guides:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        for label, term in required:
            if term not in text:
                problems.append("%s has no migrated explanation for %s (%s)"
                                % (os.path.relpath(path, REPO), label, term))

    # The guide owns the whole generated vocabulary now, so the markers reload rewrites through have to
    # be in it - and it is installed, which the root of this repository is not.
    with open(guides[0], "r", encoding="utf-8") as handle:
        guide = handle.read()
    for marker in (ocf.VOCAB_BEGIN, ocf.VOCAB_END):
        if marker not in guide:
            problems.append("%s has lost %s, so reload can neither find nor rewrite the vocabulary "
                            "region" % (os.path.relpath(guides[0], REPO), marker))

    policy = os.path.join(REPO, ".github", "ocf", "policy.toml")
    with open(policy, "r", encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    for index, line in enumerate(lines, 1):
        if line.lstrip().startswith("#"):
            problems.append("policy.toml line %d is a comment (%r); the file is data, and every "
                            "explanation belongs in a guide" % (index, line.strip()[:60]))
    assert not problems, "; ".join(problems)


CHECKS = (
    ("transition-table", check_transition_table),    ("gated-transitions", check_gated_transitions),
    ("gate-in-front", check_gate_reports_what_is_in_front),
    ("github-folder-ascii", check_github_folder_is_ascii),
    ("section-references", check_section_references),
    ("rule-counts-in-docs", check_rule_counts_in_docs),
    ("readme-matches-the-policy", check_readme_matches_the_policy),
    ("new-rule-shape", check_new_rule_shape),
    ("soft-rules-never-gate", check_soft_rules_never_gate),
    ("line-endings", check_line_endings_are_normalised),
    ("rule-text-flattened", check_rule_text_is_flattened),
    ("human-cli", check_human_cli),
    ("install-deploys", check_install_deploys),
    ("package-builds-a-release", check_package_builds_a_release),
    ("prompt-log", check_prompt_log),
    ("intake-closes", check_intake_closes_on_leaving_asking),
    ("round-cleared", check_round_state_cleared),
    ("state-moments", check_state_moments),
    ("p0-from-the-report", check_p0_comes_from_the_report),
    ("quality-scoring", check_quality_scoring),
    ("session-start-briefing", check_session_start_briefing),
    ("policy-comment-migration", check_policy_comment_migration),
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
