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


FRONTMATTER_REQUIRED = {"agent": ("name", "description"), "prompt": ("description",)}


def frontmatter_problems(path, kind):
    """Frontmatter that does not parse is worse than none: the file loads and every field reads absent.

    `read_frontmatter` returns nothing at all for a file whose first line is not `---`, which is exactly
    what one stray blank line produces - and the cross-reference check then reports nothing, because it
    treats an absent key as "not declared". That is how a prompt bound to no agent stayed green.
    """
    name = os.path.basename(path)
    with open(path, "r", encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    if not lines or lines[0].strip() != "---":
        return ["%s has no frontmatter: the file has to start with --- on its own first line" % name]
    closed = False
    for line in lines[1:]:
        if line.strip() == "---":
            closed = True
            break
        if line.strip() and not line.startswith((" ", "\t")) and ":" not in line:
            return ["%s has frontmatter that does not parse: %r is not a key: value line"
                    % (name, line[:60])]
    if not closed:
        return ["%s has no closing --- in its frontmatter, so the line that should close it is part "
                "of the value above" % name]
    fields = read_frontmatter(path) or {}
    return ["%s declares no %s" % (name, key)
            for key in FRONTMATTER_REQUIRED[kind] if not fields.get(key)]


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
    agents = os.path.join(REPO, ".github", "agents")
    for name in sorted(os.listdir(agents)):
        if name.endswith(".agent.md"):
            problems.extend(frontmatter_problems(os.path.join(agents, name), "agent"))
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
        problems.extend(frontmatter_problems(os.path.join(prompts, name), "prompt"))
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


MUST_STAY_PROTECTED = (
    ".github/ocf/ocf.py",
    ".github/ocf/policy.toml",
    ".github/work-control-flow.md",
    ".github/copilot-instructions.md",
    ".github/hooks/orchestrator.json",
    ".github/agents/orchestrator.agent.md",
    ".github/agents/plan-auditor.agent.md",
    ".github/prompts/work-plan.prompt.md",
    # The two templates. They were covered by neither mechanism before: the self-protection regex
    # listed hooks/ocf/agents/prompts and stopped at the directory above these, and the list did not
    # mention them either. A template the machine can rewrite is a shape imposed on it, not on the
    # plan.
    ".github/assets/plan-template.md",
    ".github/assets/report-template.md",
    # The list guards itself: without this entry the machine could empty it, and an empty list
    # protects nothing while every rule still reads as if it did.
    ".github/protected.txt",
)


def check_gate_files_protected():
    """A rules document the machine may rewrite is not a rule.

    Moving a file is easy and forgetting to protect it is also easy, and the failure mode is silent:
    the gate keeps working while quietly becoming editable. So this pins BOTH ends.

    One end is the list: every file that must stay protected has to be covered by an entry. The other
    is the rule that reads the list. Checking only the list is the weaker test - a list that says the
    right thing while the rule no longer consults it protects exactly nothing, and the check would
    have gone on passing. So the rule is asserted to exist, to carry both classes, to be keyed on
    `touches_protected`, and then `listed_in` is called for every path, which is the same judgement
    the gate itself makes.

    The policy is also asserted to have loaded cleanly, because the fallback policy would make this
    check pass for the wrong reason.
    """
    ocf = load_ocf_module()
    policy, warnings = ocf.load_policy(REPO)
    for kind, text in warnings:
        raise AssertionError("the policy did not load cleanly: [%s] %s" % (kind, text))
    problems = []
    rules = [rule for rule in policy.get("rule", []) if rule.get("id") == "touches-protected"]
    if not rules:
        problems.append("policy.toml has no touches-protected rule, so nothing reads the list")
    else:
        condition = rules[0].get("if") or {}
        if not condition.get("touches_protected"):
            problems.append("touches-protected does not use the touches_protected condition")
        classes = ocf.comma_list(condition.get("class", ""))
        for wanted in ("write", "exec"):
            if wanted not in classes:
                problems.append("touches-protected does not cover class %s" % wanted)
    for path in MUST_STAY_PROTECTED:
        if not ocf.listed_in(REPO, ocf.PROTECTED_LIST_REL, path):
            problems.append("%s is covered by no entry in %s" % (path, ocf.PROTECTED_LIST_REL))
    assert not problems, "%s. The machine could edit the orchestrator's own files unopposed." % \
                         "; ".join(problems)


def load_ocf_module():
    spec = importlib.util.spec_from_file_location("ocf_under_test", ENTRY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_markdown_links():
    check_markdown_links()


# The section-reference and ASCII checks are in release/build/engine_checks.py: they assert this
# repository's own conventions, and this file ships to people who never agreed to them.


def test_gate_files_protected():
    check_gate_files_protected()


def check_plan_template_matches_schema():
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


def test_plan_template_matches_schema():
    check_plan_template_matches_schema()


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
        {"id": "probe-unknown-condition", "enabled": True, "result": "deny",
         "if": {"not_a_condition": 1, "computed": "not_a_flag"}},
        {"id": "probe-unknown-exception", "enabled": True, "result": "deny",
         "if": {"class": "exec"}, "unless": {"not_an_unless_condition": 1}},
        {"id": "probe-hard-without-if", "enabled": True, "result": "deny"},
        {"id": "probe-soft-without-occasion", "kind": "soft", "message": "a sentence", "if": {}},
        {"id": "probe-soft-without-a-sentence", "kind": "soft", "if": {"occasion": "answer"}},
        {"id": "probe-soft-on-a-tool-condition", "kind": "soft", "message": "a sentence",
         "if": {"occasion": "answer", "class": "exec"}},
    ]
    found = "; ".join(text for _, text in ocf.policy_findings(probe))
    for needle in ("not_a_condition", "not_a_flag", "not_an_unless_condition",
                   "probe-hard-without-if", "probe-soft-without-occasion",
                   "probe-soft-without-a-sentence", "probe-soft-on-a-tool-condition"):
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


def check_generated_region(ocf, policy, artifact, must_contain):
    """One generated region of one file: idempotent, exactly replaced, half-marked refused, and current.

    Shared by every generated region rather than written once per file. The four failures it looks for
    are silent in the same way and for the same reasons, and the second copy of a check is how the first
    copy stops being run - the vocabulary region and the contract region fail identically, so they are
    checked by one piece of code.

    The file, its markers and its renderer all come from the generated table, so this cannot be checking
    a path that is not the one `reload` writes.
    """
    relative = artifact.relative
    rendered = artifact.render(policy)
    begin, end = artifact.markers
    block = "%s\n%s\n%s" % (begin, rendered, end)
    prose = "hand-written text\n\n"
    probe = build_root({"state": "executing"})
    try:
        target = os.path.join(probe, relative)
        os.makedirs(os.path.dirname(target), exist_ok=True)

        def seed(text):
            with open(target, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)

        def current():
            with open(target, "r", encoding="utf-8") as handle:
                return handle.read()

        seed(prose + block + "\n")
        ocf.write_generated(probe, policy, artifact)
        assert current() == prose + block + "\n", (
            "reload changed %s even though it already matched, so it is not idempotent and every run "
            "rewrites the human's file: %r" % (relative, current()[:120]))

        seed(prose + begin + "\nstale\n" + end + "\ntail\n")
        ocf.write_generated(probe, policy, artifact)
        assert current() == prose + block + "\ntail\n", (
            "reload did not replace exactly the marked region of %s, so it either lost the text "
            "around it or left the stale content in place: %r" % (relative, current()[:200]))

        seed(prose + end + "\ntail\n")
        try:
            ocf.write_generated(probe, policy, artifact)
        except ocf.OcfError:
            pass
        else:
            raise AssertionError("a %s with the end marker but no begin marker was rewritten anyway; "
                                 "that reload would have eaten the text around it" % relative)
    finally:
        shutil.rmtree(probe, ignore_errors=True)

    with open(os.path.join(REPO, relative), "r", encoding="utf-8") as handle:
        text = handle.read()
    assert begin in text, (
        "%s has no generated region. Run `python .github/ocf/ocf.py reload` in your own terminal: "
        "%s" % (relative, must_contain))
    start = text.index(begin)
    stop = text.index(end, start) + len(end)
    assert text[start:stop] == block, (
        "the generated region of %s no longer matches what %s renders, so it is describing "
        "something other than what the engine does. Run `python .github/ocf/ocf.py reload` in your "
        "own terminal. First difference: %r"
        % (relative, rendered.name if hasattr(rendered, "name") else "the code",
           _first_difference(text[start:stop], block)))


def check_generated_contract():
    """The written contract must equal what the policy renders, and half a region must be refused."""
    ocf = load_ocf_module()
    policy, _ = ocf.load_policy(REPO)
    check_generated_region(ocf, policy, ocf.generated(ocf.INSTRUCTIONS_REL),
                           "the soft rules reach the model only through that region")


def check_generated_vocabulary():
    """The policy file must document exactly the identifiers the engine reads - both directions.

    One half is the region on disk matching the renderer. The other is the vocabulary itself: a key the
    engine reads with no description is a key nobody can use correctly, and a description of a key the
    engine no longer reads is an instruction to write a policy that fails. The prose version of this
    list did exactly that, which is why it is generated now.

    The conditions used to need a third half - the same comparison against three sets of callables and
    a separate table of descriptions, held together by an assertion much like the one below. They are
    one registry now, so there is nothing left to compare: it cannot describe a condition it does not
    read, or read one it does not describe.
    """
    ocf = load_ocf_module()
    policy, _ = ocf.load_policy(REPO)
    for label, help_table, values in (
            ("occasion", ocf.OCCASION_HELP, ocf.SOFT_OCCASIONS),
            ("[hooks]", ocf.HOOK_HELP, tuple(ocf.DEFAULT_POLICY["hooks"])),
            ("[section]", ocf.SECTION_HELP,
             tuple(name for name in ocf.SECTION_KEYS)),
            ("[rule] field", ocf.RULE_FIELD_HELP, ocf.RULE_KEYS),
            # The computed flags belong here too. They are the one help table that is neither
            # derived from its source nor covered by the reference check, so without this row a
            # flag added to COMPUTED_FLAGS leaves the contract a line short and nothing fails.
            ("computed", ocf.COMPUTED_HELP, ocf.COMPUTED_FLAGS),
    ):
        documented = {name for name, _ in help_table}
        assert documented == set(values), (
            "the documentation of %s and the engine disagree: documented but unread %s; read but "
            "undocumented %s"
            % (label, sorted(documented - set(values)), sorted(set(values) - documented)))
    check_generated_region(ocf, policy, ocf.generated(ocf.POLICY_REL),
                           "the vocabulary region is what documents the policy's interface")


def check_generated_reference():
    """The generated half of the rules document must exist and cover every gate.

    A separate function from the vocabulary check on purpose: both end in a call that raises when the
    file needs reloading, and an assertion placed after one of those never runs at all. A check that
    silently does not execute is the defect this whole exercise is about, and it would have been left
    here by an earlier version of this very change.
    """
    ocf = load_ocf_module()
    policy, _ = ocf.load_policy(REPO)
    gate_names = {name for names in ocf.TRANSITIONS["gates"].values() for name in names}
    assert gate_names == set(ocf.GATE_HELP), (
        "the gate descriptions and the transitions disagree: gates with no description %s; "
        "descriptions of gates that are not run %s. A reference that does not cover every gate is the "
        "prose version again." % (sorted(gate_names - set(ocf.GATE_HELP)),
                                  sorted(set(ocf.GATE_HELP) - gate_names)))
    rendered = ocf.render_reference(policy)
    assert "MISSING DESCRIPTION" not in rendered, (
        "the reference renders a gate with no description, so it would publish a placeholder")
    check_generated_region(ocf, policy, ocf.generated(ocf.REFERENCE_REL),
                           "the reference region is what makes the rules document true")


def test_generated_contract():
    check_generated_contract()


def test_generated_vocabulary():
    check_generated_vocabulary()


def test_generated_reference():
    check_generated_reference()


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


def check_class_names_are_known():
    """Every class a rule names must be one the engine knows, and an unknown one must raise.

    This is how `edit` became `write`: the engine's list changed, two rules kept the old name, and both
    of them quietly stopped applying - `touches-protected` went from refusing to allowing. The validator
    reported it, but the new-shape evaluator did not raise, so nothing forced the issue; only reading the
    output did. Two halves here: the shipped policy must contain no unknown class name, and the
    evaluator must refuse one rather than compare it unequal and return.
    """
    ocf = load_ocf_module()
    known = set(ocf.CLASS_ORDER) | {"unknown", "any"}
    policy, _ = ocf.load_policy(REPO)
    problems = []
    for rule in policy.get("rule", []):
        where = rule.get("id", "unnamed")
        spelled = [name.strip() for name
                   in str((rule.get("if") or {}).get("class", "")).split(",") if name.strip()]
        for name in spelled:
            if name not in known:
                problems.append("rule %s names the class %r" % (where, name))
    assert not problems, ("a rule names a class the engine does not know, so it can never match and the "
                          "rule is off while reading as on: %s" % "; ".join(problems))

    class Stub(object):
        effective = "write"
        tool = "probe"

        def candidates(self):
            return []

    try:
        ocf.evaluate_conditions(Stub(), {"id": "probe"}, {"class": "not_a_class"})
    except ocf.OcfError:
        pass
    else:
        raise AssertionError("an unknown class compared unequal and returned, so a rule misspelling its "
                             "own class silently stops applying instead of failing loudly")
    assert ocf.evaluate_conditions(Stub(), {"id": "probe"}, {"class": "write"}) == ["probe"], (
        "the class condition no longer matches its own class name, so every class-scoped rule is off")


def test_class_names_are_known():
    check_class_names_are_known()


def run_cli(root, *args):
    """Run the real CLI against a throwaway root and return (exit code, stdout, stderr)."""
    env = dict(os.environ)
    env["OCF_ROOT"] = root
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run([sys.executable, ENTRY] + list(args), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=env, cwd=root)
    return (proc.returncode, proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"))


def check_human_only_vocabulary():
    """Every human-only command name must appear in the rule that bans writing one into a script.

    `human_only_call` builds its words from the command table, so it cannot drift. The
    `self-authorization-write` rule holds the same vocabulary a second time, as a regex in the policy
    file, and nothing compared them: renaming the protected-list commands would have left that rule
    refusing `ocf.py deny x` in a script while allowing `ocf.py protect x`, with every test green.
    """
    ocf = load_ocf_module()
    policy, _ = ocf.load_policy(REPO)
    names = sorted(set(word for group in ocf.TRANSITIONS["commands"]["human"] for word in group))
    pattern = ""
    for rule in policy.get("rule", []):
        if rule.get("id") == "self-authorization-write":
            pattern = str((rule.get("if") or {}).get("content_matches", ""))
    assert pattern, ("the self-authorization-write rule is gone, so nothing bans writing a human-only "
                     "command into an executable file")
    missing = [name for name in names if name not in pattern]
    assert not missing, ("the self-authorization-write pattern does not name %s, so writing that "
                         "command into a script is not treated as self-authorization. Pattern: %r"
                         % (", ".join(missing), pattern))


def test_human_only_vocabulary():
    check_human_only_vocabulary()


def install_into(target):
    """Run the real install from this repository into a target directory. Returns (code, output)."""
    code, out_text, err_text = run_cli(REPO, "install", target)
    return code, (err_text or out_text)


def check_install_keeps_the_humans_files():
    """A target that already has a policy or a protected list keeps it, and gets the shipped one beside it.

    `policy.toml` is the human's program and `protected.txt` is the list that makes the install worth
    doing. An installer that silently replaced either would be destroying the configuration it exists to
    serve, and nothing would report it - the gate would simply start refusing different things.
    """
    target = tempfile.mkdtemp(prefix="ocf-keep-")
    note = "# the target's own note, added by hand\n"
    problems = []
    try:
        os.makedirs(os.path.join(target, ".github", "ocf"))
        with open(os.path.join(REPO, ".github", "ocf", "policy.toml"), "r", encoding="utf-8") as handle:
            policy_text = handle.read()
        with open(os.path.join(target, ".github", "ocf", "policy.toml"), "w", encoding="utf-8",
                  newline="\n") as handle:
            handle.write(policy_text + "\n" + note)
        with open(os.path.join(target, ".github", "protected.txt"), "w", encoding="utf-8",
                  newline="\n") as handle:
            handle.write("# the target's own list\nsrc/**\n")
        code, message = install_into(target)
        if code != 0:
            problems.append("install exited %d: %s" % (code, message.strip()[-300:]))
        with open(os.path.join(target, ".github", "ocf", "policy.toml"), "r",
                  encoding="utf-8") as handle:
            installed = handle.read()
        if note not in installed:
            problems.append("install replaced the target's policy, which is the human's program")
        if not os.path.exists(os.path.join(target, ".github", "ocf", "policy.toml.dist")):
            problems.append("the target's policy differs but no .dist was left to compare against")
        with open(os.path.join(target, ".github", "protected.txt"), "r", encoding="utf-8") as handle:
            protected = handle.read()
        if "src/**" not in protected:
            problems.append("install overwrote the target's protected list: %r" % protected)
    finally:
        shutil.rmtree(target, ignore_errors=True)
    assert not problems, "; ".join(problems)


def test_install_keeps_the_humans_files():
    check_install_keeps_the_humans_files()


def check_verify_catches_a_broken_install():
    """`verify` has to fail when the installation is broken, or it is a green light nobody can trust.

    Four ways this deployment breaks without anything looking wrong, each one checked here: the wiring
    file is gone, it holds no events, it is not what the switches describe, and the contract the model
    reads has drifted from the rules the gate enforces.
    """
    target = tempfile.mkdtemp(prefix="ocf-broken-")
    hooks = os.path.join(target, ".github", "hooks", "orchestrator.json")
    contract = os.path.join(target, ".github", "copilot-instructions.md")
    problems = []
    try:
        code, message = install_into(target)
        if code != 0:
            problems.append("install exited %d: %s" % (code, message.strip()[-300:]))
        with open(hooks, "r", encoding="utf-8") as handle:
            good = handle.read()
        with open(contract, "r", encoding="utf-8") as handle:
            good_contract = handle.read()

        def verify():
            return run_cli(target, "verify")[0]

        os.remove(hooks)
        if verify() == 0:
            problems.append("verify passed with no wiring file at all")
        with open(hooks, "w", encoding="utf-8", newline="\n") as handle:
            handle.write('{"hooks": {}}\n')
        if verify() == 0:
            problems.append("verify passed with a wiring file holding no events")
        with open(hooks, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(good)
        with open(contract, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(good_contract.replace("# Work Control Flow", "# Something else entirely"))
        if verify() == 0:
            problems.append("verify passed while the contract the model reads had drifted from the "
                            "rules the code renders")
        with open(contract, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(good_contract)
        if verify() != 0:
            problems.append("verify failed on a target that installs cleanly, so it cannot be used as "
                            "a green light")
    finally:
        shutil.rmtree(target, ignore_errors=True)
    assert not problems, "; ".join(problems)


def test_verify_catches_a_broken_install():
    check_verify_catches_a_broken_install()


# What a person who DEPLOYS this gate can break: their policy, their tool list, their rules, their
# protected list, their templates, the prose they edit. Every check here answers a question they can act
# on, in words they have a reason to know.
#
# Checks that are not about what the gate does for its users - the engine's own tables, and this
# repository's own writing rules - live in release/build/engine_checks.py, which is not shipped.
CHECKS = (
    ("plan-template-matches-schema", test_plan_template_matches_schema),
    ("policy-vocabulary", test_policy_vocabulary),
    ("hooks-wiring", test_hooks_wiring),
    ("class-names-known", test_class_names_are_known),
    ("human-only-vocabulary", test_human_only_vocabulary),
    ("install-keeps-the-humans-files", test_install_keeps_the_humans_files),
    ("verify-catches-a-broken-install", test_verify_catches_a_broken_install),
    ("generated-contract", test_generated_contract),
    ("generated-vocabulary", test_generated_vocabulary),
    ("generated-reference", test_generated_reference),
    ("markdown-links", test_markdown_links),
    ("agent-cross-references", test_agent_cross_references),
    ("gate-files-protected", test_gate_files_protected),
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
