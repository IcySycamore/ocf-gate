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
    write(os.path.join(".orchestrator", "human-code.txt"),
          "".join(line + "\n" for line in (case.get("human_code") or [])))
    write(os.path.join(".orchestrator", "allowed-edits.txt"),
          "".join(line + "\n" for line in (case.get("allowed_edits") or [])))
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
        return None, "", ""
    payload = json.loads(last)
    spec = payload.get("hookSpecificOutput") or {}
    reason = spec.get("permissionDecisionReason") or ""
    match = re.match(r"\[([^\]]+)\]", reason)
    return spec.get("permissionDecision"), (match.group(1) if match else ""), reason


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
        decision, rule, reason = parse_output(proc.stdout.decode("utf-8", "replace").strip())
        problems = []
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
        return (not problems), decision, rule, reason, "; ".join(problems)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def run_all(verbose=False):
    failures = 0
    cases = load_cases()
    for case in cases:
        ok, decision, rule, reason, problems = run_case(case)
        marker = "PASS" if ok else "FAIL"
        print("%s  %-32s %-6s %s" % (marker, case["id"], decision, rule))
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
    ".github/hooks/orchestrator.json",
    ".github/hooks/scripts/ocf.ps1",
    ".github/agents/orchestrator.agent.md",
    ".github/agents/plan-auditor.agent.md",
    ".github/prompts/work-plan.prompt.md",
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
            with open(path, "rb") as handle:
                data = handle.read()
            if data.startswith(b"\xef\xbb\xbf"):
                data = data[3:]
            if any(byte > 127 for byte in data):
                offenders.append(os.path.relpath(path, REPO))
    return offenders


def test_repo_is_ascii():
    """Every agent-facing file under .github is ASCII, which removes a whole class of encoding bugs."""
    offenders = scan_non_ascii()
    assert not offenders, "non-ASCII bytes in: %s" % ", ".join(offenders)


CHECKS = (
    ("repo-ascii", test_repo_is_ascii),
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
