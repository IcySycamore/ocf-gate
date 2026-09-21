#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline test suite for the OCF gate.

Every case is fed to the REAL hook entry point as a subprocess with OCF_ROOT pointing at a throwaway
repository. That matters: a case fails when the entry point stops working, not only when a rule's
logic changes. The previous implementation's worst defects were all of that kind - a guard that
silently stopped applying - and none of them would have been caught by unit-testing rule logic alone.

Run it with:

    python .github/ocf/tests/run.py

It needs only CPython: no test framework, no third party package, nothing to install. Each case is a
subprocess through the real hook entry point, and the structural checks live in CHECKS at the bottom.
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
ENTRY = os.path.join(REPO, ".github", "ocf", "ocf.py")
POLICY = os.path.join(REPO, ".github", "ocf", "policy.toml")
CASES = os.path.join(HERE, "cases.json")

# The suite imports the gate to exercise it, which would otherwise drop a __pycache__ into the
# packaged directory.
sys.dont_write_bytecode = True


def load_cases():
    with open(CASES, "r", encoding="utf-8") as handle:
        return json.load(handle)


def policy_text(case):
    if case.get("policy") == "broken":
        return "this is not = valid toml [[[\n"
    with open(POLICY, "r", encoding="utf-8") as handle:
        text = handle.read()
    wanted = "true" if case.get("enabled", True) else "false"
    return re.sub(r"(?m)^enabled\s*=\s*(true|false)", "enabled = " + wanted, text, count=1)


def build_root(case):
    root = tempfile.mkdtemp(prefix="ocf-case-")
    os.makedirs(os.path.join(root, ".github", "ocf"))
    state_dir = os.path.join(root, ".orchestrator")
    os.makedirs(state_dir)

    def write(relative, text):
        with open(os.path.join(root, relative), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)

    write(os.path.join(".github", "ocf", "policy.toml"), policy_text(case))
    write(os.path.join(".orchestrator", "state"), case.get("state", "asking") + "\n")
    facts = case.get("facts") or {}
    write(os.path.join(".orchestrator", "facts"),
          "".join("%s=%s\n" % (key, facts[key]) for key in sorted(facts)))
    # One protected list, and no exemption. The legacy case fields still describe which paths are
    # protected, so the older cases keep meaning the same thing; allowed_edits is deliberately ignored,
    # because the mechanism it stood for is gone.
    protected = list(case.get("protected") or []) or list(case.get("human_code") or [])
    write(os.path.join(".github", "protected.txt"),
          "".join(line + "\n" for line in protected))
    log = case.get("exec_log") or []
    if log:
        write(os.path.join(".orchestrator", "exec.log"),
              "".join("1\t%s\n" % line for line in log))
    return root


def parse_output(text):
    last = ""
    for line in text.splitlines():
        if line.startswith("{"):
            last = line
    if not last:
        return None, "", "", ""
    payload = json.loads(last)
    spec = payload.get("hookSpecificOutput") or {}
    reason = spec.get("permissionDecisionReason") or ""
    match = re.match(r"\[([^\]]+)\]", reason)
    context = spec.get("additionalContext") or ""
    if isinstance(context, list):
        context = "\n".join(str(item) for item in context)
    return spec.get("permissionDecision"), (match.group(1) if match else ""), reason, context


def run_case(case):
    root = build_root(case)
    try:
        env = dict(os.environ)
        env["OCF_ROOT"] = root
        env["PYTHONIOENCODING"] = "utf-8"
        payload = json.loads(json.dumps(case["payload"]))
        payload.setdefault("hook_event_name", "PreToolUse")
        padding = case.get("padding")
        if padding:
            tool_input = payload.setdefault("tool_input", {})
            tool_input["command"] = (tool_input.get("command") or "") + ("a" * int(padding))
        proc = subprocess.run(
            [sys.executable, ENTRY, "hook"],
            input=json.dumps(payload).encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=root)
        raw = proc.stdout.decode("utf-8", "replace").strip()
        decision, rule, reason, context = parse_output(raw)
        problems = []
        if case.get("expect_silent"):
            decision = "silent"
            if raw:
                problems.append("the hook must stay silent here, but it wrote: %r" % raw[:200])
        elif case.get("expect_context_contains"):
            decision = "context"
            for needle in case["expect_context_contains"]:
                if needle not in context:
                    problems.append("the injected context must contain %r; got %r"
                                    % (needle, context[:200]))
        else:
            if decision != case["expect"]:
                problems.append("expected %s, got %s" % (case["expect"], decision))
            if case.get("expect_rule") and rule != case["expect_rule"]:
                problems.append("expected rule %s, got %s" % (case["expect_rule"], rule))
            for needle in case.get("expect_reason_contains") or []:
                if needle not in reason:
                    problems.append("the reason must name %r, because a block that names the wrong file "
                                    "cannot be acted on; got: %r" % (needle, reason[:200]))
            if case.get("expect_not_rule") and rule == case["expect_not_rule"]:
                problems.append("rule %s must not fire but did" % rule)
        if proc.returncode != 0:
            problems.append("exit code %d; a hook must always exit 0" % proc.returncode)
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        if stderr:
            problems.append("stderr is not empty: %r" % stderr[:200])
        return (not problems), decision, rule, reason or context, "; ".join(problems)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def run_all(verbose=False):
    failures = 0
    cases = load_cases()
    for case in cases:
        ok, decision, rule, reason, problems = run_case(case)
        marker = "PASS" if ok else "FAIL"
        print("%s  %-32s %-6s %s" % (marker, case["id"], decision or "-", rule or ""))
        if verbose and reason:
            for line in reason.splitlines():
                print("        | %s" % line)
        if not ok:
            failures += 1
            print("        %s" % problems)
            print("        why this case exists: %s" % case.get("why", "(no note)"))
    print("")
    print("%d of %d cases passed" % (len(cases) - failures, len(cases)))
    return failures


def test_cases():
    assert run_all() == 0


AGENT_ALIASES = ("execute", "read", "edit", "search", "agent", "web", "todo")
# Specific tool names rather than aliases. The slash form is what VS Code itself normalises the field
# to: writing `vscode_askQuestions` here was rewritten to `vscode/askQuestions` by the editor, and that
# rewrite is the evidence this entry records. Listed explicitly so a typo cannot pass unnoticed and so
# adding a tool is a deliberate act rather than a silent one.
SPECIFIC_TOOLS = ("vscode/askQuestions",)


def read_frontmatter(path):
    with open(path, "r", encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    fields = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if not line.strip() or line.startswith((" ", "\t")) or ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()
    return fields


def parse_flow_list(value):
    if value is None:
        return None
    text = value.strip()
    if not text.startswith("[") or not text.endswith("]"):
        return None
    inner = text[1:-1].strip()
    if not inner:
        return []
    return [item.strip().strip('"').strip("'") for item in inner.split(",") if item.strip()]


def agent_inventory():
    """Map every declared agent name to its filename and fields."""
    folder = os.path.join(REPO, ".github", "agents")
    inventory = {}
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".agent.md"):
            continue
        fields = read_frontmatter(os.path.join(folder, name)) or {}
        declared = (fields.get("name") or "").strip().strip('"').strip("'")
        inventory[declared or name[: -len(".agent.md")]] = (name, fields)
    return inventory


def check_agent_cross_references():
    """Frontmatter failures are silent, so assert the references resolve instead of hoping.

    This is what makes pinning `agents:` safe. Modelling the orchestrator around "a name might be
    misspelled" avoided a capability rather than making the mistake detectable, which is backwards:
    a wrong name here fails this check instead of quietly removing the ability to dispatch.
    """
    inventory = agent_inventory()
    known = ", ".join(sorted(inventory))
    problems = []
    for display, (filename, fields) in sorted(inventory.items()):
        tools = parse_flow_list(fields.get("tools")) or []
        for token in tools:
            if token in AGENT_ALIASES or token in SPECIFIC_TOOLS or token.endswith("/*"):
                continue
            problems.append("%s lists the tool %r, which is neither one of the seven documented "
                            "aliases, nor a verified specific tool name, nor an MCP server "
                            "wildcard" % (filename, token))
        allowed = parse_flow_list(fields.get("agents"))
        if "agent" in tools and not allowed:
            problems.append("%s can dispatch subagents but names none in agents:, so it may reach "
                            "any agent, including ones unrelated to this flow" % filename)
        for entry in allowed or []:
            if entry not in inventory:
                problems.append("%s lists %r in agents:, but no agent declares that name "
                                "(declared: %s)" % (filename, entry, known))
    prompts = os.path.join(REPO, ".github", "prompts")
    for name in sorted(os.listdir(prompts)):
        if not name.endswith(".prompt.md"):
            continue
        fields = read_frontmatter(os.path.join(prompts, name)) or {}
        target = (fields.get("agent") or "").strip().strip('"').strip("'")
        if target and target not in inventory:
            problems.append("prompts/%s binds to agent %r, which no agent declares (declared: %s)"
                            % (name, target, known))
    assert not problems, "; ".join(problems)


def test_agent_cross_references():
    check_agent_cross_references()


def iter_markdown_files():
    """Every markdown file an agent or human might follow a link from."""
    for folder, folders, files in os.walk(os.path.join(REPO, ".github")):
        folders[:] = [name for name in folders if name not in ("__pycache__", ".pytest_cache")]
        for name in files:
            if name.endswith(".md"):
                yield os.path.join(folder, name)
    for name in sorted(os.listdir(REPO)):
        path = os.path.join(REPO, name)
        if os.path.isfile(path) and name.endswith(".md"):
            yield path


LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def check_markdown_links():
    """A doc that points at a moved file is a doc that no longer routes anyone anywhere.

    Worth a check because the breakage is invisible: nothing errors, the reader simply never reaches
    the rules.
    """
    problems = []
    for path in iter_markdown_files():
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        for target in LINK_RE.findall(text):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            clean = target.split("#", 1)[0]
            if not clean:
                continue
            resolved = os.path.normpath(os.path.join(os.path.dirname(path), clean))
            if not os.path.exists(resolved):
                problems.append("%s -> %s" % (os.path.relpath(path, REPO), target))
    assert not problems, "broken relative links: %s" % "; ".join(problems)


PROTECTED_BY_POLICY = (
    ".github/ocf/ocf.py",
    ".github/ocf/policy.toml",
    ".github/work-control-flow.md",
    ".github/copilot-instructions.md",
    ".github/hooks/orchestrator.json",
    ".github/agents/orchestrator.agent.md",
    ".github/agents/plan-auditor.agent.md",
    ".github/prompts/work-plan.prompt.md",
    # The list guards itself: without this entry the machine could empty it, and an empty list
    # protects nothing while every rule still reads as if it did.
    ".github/protected.txt",
)


def load_ocf_module():
    spec = importlib.util.spec_from_file_location("ocf_under_test", ENTRY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_gate_files_protected():
    """A rules document the machine may rewrite is not a rule.

    Moving a file is easy and forgetting the protection pattern is also easy, and the failure mode is
    silent: the gate keeps working while quietly becoming editable. So assert the pattern by running it
    against the files that must stay protected, and assert the policy loaded cleanly, because the
    fallback policy would make this check pass for the wrong reason.
    """
    ocf = load_ocf_module()
    policy, warnings = ocf.load_policy(REPO)
    for kind, text in warnings:
        raise AssertionError("the policy did not load cleanly: [%s] %s" % (kind, text))
    problems = []
    for rule_id in ("self-protection-path", "self-protection-write"):
        rules = [rule for rule in policy.get("rule", []) if rule.get("id") == rule_id]
        if not rules:
            problems.append("policy.toml has no %s rule" % rule_id)
            continue
        pattern = rules[0].get("match")
        if not pattern:
            problems.append("%s has no match pattern" % rule_id)
            continue
        for path in PROTECTED_BY_POLICY:
            if not re.search(pattern, path, re.I):
                problems.append("%s no longer covers %s" % (rule_id, path))
    assert not problems, "%s. The machine could edit the orchestrator's own files unopposed." % \
                         "; ".join(problems)


def test_markdown_links():
    check_markdown_links()


def test_gate_files_protected():
    check_gate_files_protected()


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
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_prompt_instrumentation():
    check_prompt_instrumentation()


def scan_non_ascii():
    """Return .github-relative paths that hold non-ASCII bytes.

    A leading BOM is stripped first: it is an encoding mark rather than content, and the previous
    PowerShell implementation genuinely needed one. Build output is skipped, because a .pyc is not
    source.
    """
    offenders = []
    for folder, folders, files in os.walk(os.path.join(REPO, ".github")):
        folders[:] = [name for name in folders if name not in ("__pycache__", ".pytest_cache")]
        for name in files:
            path = os.path.join(folder, name)
            relative = os.path.relpath(path, REPO).replace("\\", "/")
            with open(path, "rb") as handle:
                data = handle.read()
            if data.startswith(b"\xef\xbb\xbf"):
                data = data[3:]
            if any(byte > 127 for byte in data):
                offenders.append(relative)
    return offenders


# The ASCII rule keeps a class of encoding bugs out of code and out of everything the toolchain
# touches. These files are not code that runs in the toolchain: policy.toml is the human's own rules,
# copilot-instructions.md is prose addressed to the human and the model, and run.py has to be able to
# state the real text it asserts on - a check about where spaces land in Chinese cannot be written in
# English and still test Chinese. ocf.py is deliberately NOT on this list, because its output is what
# reaches a console that may not be UTF-8. The exemption is a list of named paths rather than a suffix
# or a folder, because the value of the rule is that it has no convenient way around it: a new file has
# to argue for itself here.
NON_ASCII_ALLOWED = (
    ".github/ocf/policy.toml",
    ".github/copilot-instructions.md",
    ".github/ocf/tests/run.py",
)


def test_repo_is_ascii():
    """Every agent-facing file under .github is ASCII, apart from the two prose files named above."""
    offenders = [path for path in scan_non_ascii() if path not in NON_ASCII_ALLOWED]
    assert not offenders, ("non-ASCII bytes in: %s (allowed: %s)"
                           % (", ".join(offenders), ", ".join(NON_ASCII_ALLOWED)))


def check_plan_template():
    """The template the plan is written on must satisfy the schema that will judge the plan.

    These two drifted apart once already: the template's headings were Chinese while PLAN_HEADINGS
    matched English literals, so the human's own template would have failed plan-schema. Nothing
    noticed, because nothing read the template. This reads it.
    """
    ocf = load_ocf_module()
    path = os.path.join(REPO, ".github", "assets", "plan-template.md")
    assert os.path.exists(path), "the plan template is missing: %s" % path
    with open(path, "r", encoding="utf-8") as handle:
        sections = ocf.plan_sections(handle.read())
    missing = [name for name in ocf.PLAN_HEADINGS if name not in sections]
    empty = [name for name in ocf.PLAN_HEADINGS
             if name in sections and not "".join(sections[name]).strip()]
    problems = []
    if missing:
        problems.append("missing headings: %s" % ", ".join("## " + item for item in missing))
    if empty:
        problems.append("empty headings: %s" % ", ".join("## " + item for item in empty))
    assert not problems, ("a template that fails the schema it exists to satisfy teaches the wrong "
                          "shape: %s" % "; ".join(problems))


def test_plan_template():
    check_plan_template()


def check_policy_vocabulary():
    """An unknown identifier must be reported, and the shipped policy must be free of them.

    The failure this guards is silent by construction: a misspelled `on` class or limit key does not
    raise, it just stops the rule from matching, so the gate keeps running while enforcing less than
    the file says. Proving the validator rejects one matters as much as proving the shipped file is
    clean, because a validator that reports nothing looks exactly like a correct policy.
    """
    ocf = load_ocf_module()
    policy, _ = ocf.load_policy(REPO)
    shipped = ocf.policy_findings(policy)
    assert not shipped, ("the shipped policy carries identifiers the engine does not know: %s"
                         % "; ".join(text for _, text in shipped))
    probe = json.loads(json.dumps(policy))
    probe["rule"] = list(probe.get("rule", [])) + [
        {"id": "probe-unknown-identifiers", "on": "not_a_class", "surface": "not_a_surface",
         "action": "deny", "only_if": {"not_a_condition": 1, "computed": "not_a_flag",
                                        "length_over": "not_a_limit"}},
        {"id": "probe-soft-without-occasion", "kind": "soft", "result": "a sentence", "if": {}},
        {"id": "probe-soft-without-a-sentence", "kind": "soft", "if": {"occasion": "answer"}},
        {"id": "probe-soft-on-a-tool-condition", "kind": "soft", "result": "a sentence",
         "if": {"occasion": "answer", "class": "exec"}},
    ]
    found = "; ".join(text for _, text in ocf.policy_findings(probe))
    # Not the value but the key: `length_over` is no longer a condition the engine knows, since the
    # thresholds moved into the rules themselves, so its value is never reached. A policy edited
    # against the old vocabulary has to be reported, or it would quietly enforce nothing.
    for needle in ("not_a_class", "not_a_surface", "not_a_condition", "not_a_flag", "length_over",
                   "probe-soft-without-occasion", "probe-soft-without-a-sentence",
                   "probe-soft-on-a-tool-condition"):
        assert needle in found, ("the validator did not report %r, and a validator that reports "
                                 "nothing cannot fail. Got: %s" % (needle, found or "(nothing)"))
    # Reporting is not enough on its own: the hook used to receive the policy warnings and never
    # print them, so a broken policy was invisible exactly where it mattered. Drive the real entry
    # point over a policy carrying an unknown identifier and require the name to come back out.
    root = tempfile.mkdtemp(prefix="ocf-vocabulary-")
    try:
        os.makedirs(os.path.join(root, ".github", "ocf"))
        os.makedirs(os.path.join(root, ".orchestrator"))
        with open(POLICY, "r", encoding="utf-8") as handle:
            text = handle.read()
        text += ("\n[[rule]]\nid = \"probe-unknown\"\non = \"not_a_class\"\nsurface = \"command\"\n"
                 "match = \".*\"\naction = \"deny\"\n")
        with open(os.path.join(root, ".github", "ocf", "policy.toml"), "w", encoding="utf-8") as handle:
            handle.write(text)
        env = dict(os.environ)
        env["OCF_ROOT"] = root
        payload = {"hook_event_name": "SessionStart", "session_id": "abcdef1234567890"}
        proc = subprocess.run([sys.executable, ENTRY, "hook"], input=json.dumps(payload).encode("utf-8"),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=root)
        emitted = proc.stdout.decode("utf-8", "replace")
        # The marker, not the bare name: the selftest text also prints [policy], so asserting on
        # "not_a_class" alone would pass even with the notification path removed. "OCF [policy]" is
        # produced only by notices_text, which is the code that used to drop the warning.
        assert "OCF [policy]" in emitted, (
            "the hook dropped the policy finding, so a policy that enforces less than it says is "
            "invisible on the hook path; emitted: %r" % emitted[:300])
        assert "not_a_class" in emitted, (
            "the notification did not name the offending identifier, so it cannot be acted on; "
            "emitted: %r" % emitted[:300])
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_policy_vocabulary():
    check_policy_vocabulary()


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


def test_transition_table():
    check_transition_table()


GATED_PAIRS = (("asking", "planning"), ("planning", "executing"))


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


def test_gated_transitions():
    check_gated_transitions()


PROBE_POLICY = """\
[[rule]]
id = "probe-long-command"
enabled = true
result = "deny"
why = "too long for the probe"

[rule.if]
class = "exec"
command_length_over = 10

[[rule]]
id = "probe-default"
enabled = true
result = "allow"
why = "short enough for the probe"

[rule.if]
class = "exec"
"""


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


def test_new_rule_shape():
    check_new_rule_shape()


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
        payload = {"tool_name": "run_in_terminal",
                   "tool_input": {"command": "python .github/ocf/ocf.py status"}}
        context = ocf.Context(root, policy, payload)
        probe = {"id": "probe-soft", "kind": "soft", "enabled": True,
                 "result": "deny", "if": {"occasion": "act"}}
        for rule in list(soft) + [probe]:
            verdict = ocf.evaluate_rule(context, rule)
            assert verdict is None, (
                "the soft rule %r produced the verdict %r. A sentence read as a verdict denies "
                "everything the fail-safe cannot map." % (rule.get("id"), verdict))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_soft_rules_never_gate():
    check_soft_rules_never_gate()


def _first_difference(written, rendered):
    """Name the first line that differs, so the message points at the change instead of describing it.

    "The block no longer matches" is true and useless: the cause may be a soft rule, the command table
    or the state table, and all three look alike from a byte comparison.
    """
    for index, (left, right) in enumerate(zip(written.splitlines(), rendered.splitlines())):
        if left != right:
            return "line %d: on disk %r, rendered %r" % (index + 1, left[:90], right[:90])
    return "the line counts differ: on disk %d, rendered %d" % (len(written.splitlines()),
                                                               len(rendered.splitlines()))


def check_generated_instructions():
    """The written block must equal what the policy renders, and half a block must be refused.

    Three failures, all silent: a block that drifted from the policy means the model is following
    rules the human has already changed; a reload that eats the prose around the block means the
    hand-written contract slowly disappears; and a half-deleted block means the next reload eats
    whatever followed the marker. All three are probes against a temp root, so the check proves it can
    pass as well as fail - a guard that only ever reports "no block" cannot tell a correct file from a
    broken one.
    """
    ocf = load_ocf_module()
    policy, _ = ocf.load_policy(REPO)
    block = ocf.render_instructions(policy)
    prose = "hand-written contract\n\n"
    probe = build_root({"state": "executing"})
    try:
        target = os.path.join(probe, ocf.INSTRUCTIONS_REL)
        os.makedirs(os.path.dirname(target), exist_ok=True)

        def seed(text):
            with open(target, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)

        def current():
            with open(target, "r", encoding="utf-8") as handle:
                return handle.read()

        seed(prose + block + "\n")
        ocf.write_instructions(probe, policy)
        assert current() == prose + block + "\n", (
            "reload changed a file that already matched the policy, so it is not idempotent and "
            "every run rewrites the human's file: %r" % current()[:120])

        seed(prose + ocf.INSTRUCTIONS_BEGIN + "\nstale\n" + ocf.INSTRUCTIONS_END + "\ntail\n")
        ocf.write_instructions(probe, policy)
        assert current() == prose + block + "\ntail\n", (
            "reload did not replace exactly the marked block, so it either lost the prose around it "
            "or left the stale text in place: %r" % current()[:200])

        seed(prose + ocf.INSTRUCTIONS_END + "\ntail\n")
        try:
            ocf.write_instructions(probe, policy)
        except ocf.OcfError:
            pass
        else:
            raise AssertionError("a file with the end marker but no begin marker was rewritten "
                                 "anyway; that reload would have eaten the prose around it")
    finally:
        shutil.rmtree(probe, ignore_errors=True)

    path = os.path.join(REPO, ocf.INSTRUCTIONS_REL)
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    assert ocf.INSTRUCTIONS_BEGIN in text, (
        "%s has no generated block. Run `python .github/ocf/ocf.py reload` in your own terminal to "
        "write it: the soft rules reach the model only through that block." % ocf.INSTRUCTIONS_REL)
    start = text.index(ocf.INSTRUCTIONS_BEGIN)
    end = text.index(ocf.INSTRUCTIONS_END, start) + len(ocf.INSTRUCTIONS_END)
    assert text[start:end] == block, (
        "the generated block in %s no longer matches what the policy renders, so the model is reading "
        "a contract the human has already changed - the soft rules, or the command table, or the state "
        "table. Run `python .github/ocf/ocf.py reload` in your own terminal. First difference: %r"
        % (ocf.INSTRUCTIONS_REL, _first_difference(text[start:end], block)))


def test_generated_instructions():
    check_generated_instructions()


def check_hooks_wiring():
    """The wiring file must equal what the switches render, and a switched-off event must vanish.

    Two silent failures. A wiring file that drifted from the switches means the human has switched
    something off and is still being gated, or switched it on and is not - and either way the file
    says otherwise. A master switch that only empties the answers rather than the wiring leaves the
    program being started on every tool call, which is the "installed like a plaster" the switches
    exist to avoid. The renderer is exercised in a temp root, so this check can pass as well as fail.
    """
    ocf = load_ocf_module()
    policy, _ = ocf.load_policy(REPO)
    written = ocf.render_hooks_json(policy)
    path = os.path.join(REPO, ocf.HOOKS_REL)
    with open(path, "r", encoding="utf-8") as handle:
        on_disk = handle.read()
    assert on_disk == written, (
        "%s no longer matches the [hooks] switches in policy.toml, so the switches are not in force. "
        "Run `python .github/ocf/ocf.py reload` in your own terminal, then reload the VS Code window."
        % ocf.HOOKS_REL)
    probe = build_root({"state": "executing"})
    try:
        def switches(**over):
            merged = json.loads(json.dumps(policy))
            merged.setdefault("hooks", {}).update(over)
            return merged

        everything = json.loads(ocf.render_hooks_json(switches()))
        assert sorted(everything["hooks"]) == sorted(
            event for _, event, _ in ocf.HOOK_EVENTS), (
            "with everything switched on, the wiring should hold every event: %s"
            % sorted(everything["hooks"]))
        # The command has to carry the subcommand. Without it the program prints its usage and exits
        # 0, which a hook reads as "no objection" - the gate would look installed and decide nothing.
        for event, entries in everything["hooks"].items():
            for entry in entries:
                assert ocf.HOOK_SUBCOMMAND in entry["command"], (
                    "the wiring for %s runs the entry point without %r, so it would answer nothing "
                    "and the gate would be silently off: %r"
                    % (event, ocf.HOOK_SUBCOMMAND, entry["command"]))
        one_off = json.loads(ocf.render_hooks_json(switches(pre_tool_use=False)))
        assert "PreToolUse" not in one_off["hooks"] and len(one_off["hooks"]) == 3, (
            "switching one event off must remove it from the wiring, not merely silence it: %s"
            % sorted(one_off["hooks"]))
        master = json.loads(ocf.render_hooks_json(switches(enabled=False)))
        assert master["hooks"] == {}, (
            "the master switch must leave nothing installed, so the program is never started: %s"
            % master["hooks"])
        assert not ocf.hook_enabled(switches(enabled=False), "PreToolUse"), (
            "the master switch did not reach the entry point; the wiring file is only re-read on a "
            "window reload, so a master switch that works only there is a gate that keeps denying")
        assert not ocf.hook_enabled(switches(user_prompt=False), "UserPromptSubmit"), (
            "a per-event switch did not reach the entry point")
        assert ocf.hook_enabled(switches(), "PreToolUse"), "the gate is off with the switches on"
    finally:
        shutil.rmtree(probe, ignore_errors=True)


def test_hooks_wiring():
    check_hooks_wiring()


def check_rule_text_is_flattened():
    """A soft rule's text reaches the model as one line, with the spaces the human meant and no others.

    This was wrong three times in a row, each time invisibly: joining every wrap on a space put one
    inside a Chinese sentence, joining none of them ran Latin words into the Chinese beside them, and
    dropping the whitespace after a break lost the space in front of an indented path. The terminal
    could not be used to check any of it - its own line wrapping inserts spaces - so the rule is pinned
    here as comparisons rather than looked at.
    """
    ocf = load_ocf_module()
    cases = (
        ("它是什么、在\n这里指什么", "它是什么、在这里指什么"),
        ("先读\n  CONTEXT.md\n  和\n  docs/adr/\n：就这样", "先读 CONTEXT.md 和 docs/adr/：就这样"),
        ("run\nthe command", "run the command"),
        ("  补进\n  .orchestrator/glossary.md；", "补进 .orchestrator/glossary.md；"),
        ("一句话定义 - 用法", "一句话定义 - 用法"),
    )
    for source, expected in cases:
        got = ocf.one_line(source)
        assert got == expected, ("one_line(%r) gave %r, expected %r. The text lands in front of the "
                                 "model verbatim, so a wrong space here is a wrong instruction."
                                 % (source, got, expected))


def test_rule_text_is_flattened():
    check_rule_text_is_flattened()


CHECKS = (
    ("repo-ascii", test_repo_is_ascii),
    ("plan-template", test_plan_template),
    ("policy-vocabulary", test_policy_vocabulary),
    ("transition-table", test_transition_table),
    ("gated-transitions", test_gated_transitions),
    ("new-rule-shape", test_new_rule_shape),
    ("soft-rules-never-gate", test_soft_rules_never_gate),
    ("hooks-wiring", test_hooks_wiring),
    ("rule-text-flattened", test_rule_text_is_flattened),
    ("generated-instructions", test_generated_instructions),
    ("markdown-links", test_markdown_links),
    ("agent-cross-references", test_agent_cross_references),
    ("gate-files-protected", test_gate_files_protected),
    ("prompt-instrumentation", test_prompt_instrumentation),
)


if __name__ == "__main__":
    verbose_flag = "-v" in sys.argv[1:] or "--verbose" in sys.argv[1:]
    failures = run_all(verbose_flag)
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
    sys.exit(1 if failures else 0)
