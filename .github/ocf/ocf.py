#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OCF - orchestrator control flow: the state machine, the gates, and the hook entry point.

One implementation, no build step, stdlib only. The rules it interprets live in policy.toml next to
this file, and the human-readable system guide is .github/work-control-flow.md.

Design rules that this file obeys and that must not be relaxed:

1. The hook entry imports NOTHING outside the standard library. VS Code starts the hook process on the
   host, so a container cannot wrap it; a missing third party import would make the gate silently
   fail open, which is exactly the failure mode this project exists to remove. TOML comes from
   tomllib (3.11+) with tomli as a fallback, and if neither is available the built in strict policy
   takes over instead of degrading to "no policy".
2. Gating rules are data, in policy.toml. This file is a generic interpreter for them. Adding or
   changing a rule must not require touching this code.
3. Failing safe means refusing, not allowing. Any internal error during a PreToolUse check denies.
4. All text I/O is explicit UTF-8 and tolerates a BOM, because the previous implementation was bitten
   by an implicit locale decode that silently mangled a state file.
"""

import json
import os
import re
import sys
import time

try:
    import tomllib
except ImportError:  # Python < 3.11
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STATES = ("ready", "asking", "planning", "executing", "reporting", "blocked")
AGENT_TARGETS = ("asking", "planning", "reporting", "ready", "blocked")
ACTING_STATES = ("executing", "reporting")

AGENT_COMMANDS = ("status", "set", "gate", "journal", "fail", "ok", "advance", "init", "selftest", "hook")
HUMAN_COMMANDS = ("approve", "reject", "confirm", "allow", "deny", "human-code")

POLICY_REL = ".github/ocf/policy.toml"
HOOKS_REL = ".github/hooks/orchestrator.json"
PLAN_REL = ".orchestrator/plan.md"

DEFAULT_STATE_DIR = ".orchestrator"

PLAN_HEADINGS = ("type", "summary", "steps", "tools", "files", "scope", "deliverables", "self-review")

# Entry script recognised only by its full relative path. Merely mentioning the name elsewhere must
# not inherit the control plane exemption, which was a real bypass in the previous implementation.
ENTRY_PAT = r"\.github[\\/](?:ocf[\\/]ocf\.py|hooks[\\/]scripts[\\/]ocf\.(?:py|ps1|sh))"
ENTRY_RE = re.compile(r"(?i)" + ENTRY_PAT)
CP_STMT_RE = re.compile(r"(?i)^\s*(?:&\s*)?(?:(?:python3?(?:\.exe)?|py(?:\.exe)?)\s+)?[\"']?" + ENTRY_PAT + r"[\"']?(?:\s|$)")

PATH_KEYS = ("filePath", "file_path", "path", "newPath", "uri", "dirPath")

# Conditions that must be judged against one candidate value rather than against the whole call.
VALUE_SCOPED_CONDITIONS = ("listed_in",)
CONTENT_KEYS = ("content", "newString", "new_string", "newCode", "new_str", "body")

WRITE_RE = re.compile(
    r"(?i)(>>?|Set-Content|Out-File|Add-Content|New-Item|Copy-Item|Move-Item|Remove-Item|"
    r"Rename-Item|Set-ItemProperty|\bdel\b|\berase\b|\brm\b|\bmv\b|\bcp\b|\btouch\b|\bmkdir\b|"
    r"\btee\b|\btruncate\b|git\s+apply)"
)
TOKEN_RE = re.compile(r"[A-Za-z0-9_@%$+.\-]*[\\/][A-Za-z0-9_@%$+.\-\\/]*|[\w\-]+\.[A-Za-z0-9]{1,8}")
UNKNOWN_ACTION_RE = re.compile(
    r"(?i)(create|write|edit|delete|remove|run|exec|install|deploy|upload|push|patch|update|set|"
    r"add|insert|replace|apply|start|stop|kill|move|rename|format|build|compile|generate|send|"
    r"post|submit|merge|revert|reset|drop|truncate|commit|launch|open|config|configure|install)"
)


# ---------------------------------------------------------------------------
# Errors, classified
# ---------------------------------------------------------------------------

class OcfError(Exception):
    """kind is environment | policy | system. Only system is this code's own fault."""

    def __init__(self, kind, message):
        Exception.__init__(self, message)
        self.kind = kind
        self.message = message

    def tagged(self):
        return "[%s] %s" % (self.kind, self.message)


# ---------------------------------------------------------------------------
# Text I/O - explicit UTF-8, BOM tolerant
# ---------------------------------------------------------------------------

def setup_io():
    for name in ("stdout", "stderr", "stdin"):
        stream = getattr(sys, name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass


def out(line=""):
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def read_text(path):
    """Return the file's text with any BOM stripped, or None when it does not exist."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as handle:
            data = handle.read()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OcfError("environment", "cannot read %s: %s" % (path, exc))
    if data.startswith("\ufeff"):
        data = data[1:]
    return data


def read_lines(path):
    text = read_text(path)
    if text is None:
        return []
    return text.splitlines()


def write_text(path, text):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def now_stamp():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Repository root
# ---------------------------------------------------------------------------

def find_root():
    override = os.environ.get("OCF_ROOT")
    if override:
        return os.path.abspath(override)
    here = os.path.dirname(os.path.abspath(__file__))
    cursor = here
    for _ in range(8):
        if os.path.isdir(os.path.join(cursor, ".github")):
            return cursor
        parent = os.path.dirname(cursor)
        if parent == cursor:
            break
        cursor = parent
    return os.path.dirname(os.path.dirname(here))


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

DEFAULT_POLICY = {
    "system": {"enabled": True},
    "paths": {
        "state_dir": DEFAULT_STATE_DIR,
        "human_code_list": ".orchestrator/human-code.txt",
        "allowed_edits_list": ".orchestrator/allowed-edits.txt",
    },
    "limits": {"max_cmd_len": 400, "max_cmd_stmts": 3, "max_cmd_repeat": 3, "fail_budget": 2},
    "approval": {"allowed_states": ["executing", "reporting"], "min_reason_len": 8},
    "unknown_tool": {"action": "ask"},
    "self_authorization": {"executable_extensions": [".ps1", ".sh", ".py"]},
    "tools": {
        "edit": [],
        "exec": [],
        "action": [],
        "visual": [],
        "always_allow": [],
        "field": {"default": ["command", "code"]},
    },
    "selftest": {"canary": []},
    "rule": [],
}

# The strictest thing that still lets the human work. Used when the policy cannot be read at all, so
# that "policy is broken" can never mean "gate is open".
STRICT_POLICY = json.loads(json.dumps(DEFAULT_POLICY))
STRICT_POLICY["rule"] = [
    {
        "id": "strict-fallback",
        "on": "any",
        "surface": "any",
        "match": ".*",
        "action": "deny",
        "why": "No usable policy file was found, so the strict fallback policy is in force. "
               "Restore .github/ocf/policy.toml.",
    }
]


def deep_merge(base, over):
    result = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_policy(root):
    """Return (policy, warnings). Warnings are (kind, message) pairs, never fatal."""
    path = os.path.join(root, POLICY_REL)
    if tomllib is None:
        return (dict(STRICT_POLICY), [
            ("environment", "no TOML parser available (needs Python 3.11+ or the tomli package), "
                            "so the strict fallback policy is in force")])
    text = read_text(path)
    if text is None:
        return (dict(STRICT_POLICY), [
            ("policy", "%s is missing, so the strict fallback policy is in force" % POLICY_REL)])
    try:
        data = tomllib.loads(text)
    except Exception as exc:
        return (dict(STRICT_POLICY), [
            ("policy", "%s failed to parse: %s. The strict fallback policy is in force, which "
                       "denies everything except the always-allowed tools." % (POLICY_REL, exc))])
    return (deep_merge(DEFAULT_POLICY, data), [])


def policy_paths(policy):
    return deep_merge(DEFAULT_POLICY["paths"], policy.get("paths", {}))


def state_dir(root, policy):
    return os.path.join(root, policy_paths(policy)["state_dir"])


def limits(policy):
    return deep_merge(DEFAULT_POLICY["limits"], policy.get("limits", {}))


def enabled(policy):
    return bool(policy.get("system", {}).get("enabled", True))


# ---------------------------------------------------------------------------
# Runtime state
# ---------------------------------------------------------------------------

def read_state(root, policy):
    path = os.path.join(state_dir(root, policy), "state")
    lines = read_lines(path)
    if not lines:
        return "ready"
    value = lines[0].strip()
    if value not in STATES:
        raise OcfError("system", "the state file holds an illegal value %r; refusing to continue "
                                 "rather than assuming ready" % value)
    return value


def write_state(root, policy, state):
    if state not in STATES:
        raise OcfError("system", "refusing to write the illegal state %r" % state)
    write_text(os.path.join(state_dir(root, policy), "state"), state + "\n")


def load_facts(root, policy):
    facts = {}
    for line in read_lines(os.path.join(state_dir(root, policy), "facts")):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        facts[key.strip()] = value.strip()
    return facts


def save_facts(root, policy, facts):
    body = "".join("%s=%s\n" % (key, facts[key]) for key in sorted(facts))
    write_text(os.path.join(state_dir(root, policy), "facts"), body)


def set_fact(root, policy, key, value):
    facts = load_facts(root, policy)
    facts[key] = value
    save_facts(root, policy, facts)


def journal(root, policy, state, message):
    safe = message.replace("\r", " ").replace("\n", " ").replace("|", "/")
    path = os.path.join(state_dir(root, policy), "journal.log")
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write("%s | %s | %s\n" % (now_stamp(), state, safe))


def exec_log_path(root, policy):
    return os.path.join(state_dir(root, policy), "exec.log")


def read_exec_log(root, policy):
    return read_lines(exec_log_path(root, policy))


def command_repeat_count(root, policy, command):
    target = command.replace("\t", " ").strip()
    count = 0
    for line in read_exec_log(root, policy):
        if "\t" in line:
            _, recorded = line.split("\t", 1)
        else:
            recorded = line
        if recorded.strip() == target:
            count += 1
    return count


def append_exec_log(root, policy, command):
    path = exec_log_path(root, policy)
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        os.makedirs(parent)
    safe = command.replace("\r", " ").replace("\n", " ").replace("\t", " ").strip()
    lines = read_exec_log(root, policy)
    lines.append("%d\t%s" % (int(time.time()), safe))
    if len(lines) > 500:
        lines = lines[-500:]
    write_text(path, "".join(line + "\n" for line in lines))


# ---------------------------------------------------------------------------
# Lists (human-code, allowed-edits). Entries are paths or globs; '#' starts a comment.
# ---------------------------------------------------------------------------

def load_list(root, relative):
    entries = []
    for line in read_lines(os.path.join(root, relative)):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        entries.append(stripped)
    return entries


def norm_path(root, path):
    if not path:
        return ""
    text = path.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1]
    text = text.replace("\\", "/")
    if text.lower().startswith("file:///"):
        text = text[8:]
    if re.match(r"^/[A-Za-z]:/", text):
        text = text[1:]
    if re.match(r"^[A-Za-z]:/", text) or text.startswith("/"):
        if root:
            try:
                rel = os.path.relpath(text, root).replace("\\", "/")
                if not rel.startswith(".."):
                    text = rel
            except Exception:
                pass
    while text.startswith("./"):
        text = text[2:]
    return text


def glob_regex(pattern):
    sentinel = "\x00"
    body = pattern.replace("**", sentinel)
    body = re.escape(body)
    body = body.replace(sentinel, ".*")
    body = body.replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
    return body


def path_match(pattern, value):
    """Case-insensitive. A pattern with no wildcard is an exact path; otherwise a glob."""
    if not pattern or not value:
        return False
    left = norm_path("", pattern)
    right = norm_path("", value)
    if not left or not right:
        return False
    if not any(char in left for char in "*?"):
        return left.lower() == right.lower()
    try:
        return re.match("^" + glob_regex(left) + "$", right, re.I) is not None
    except re.error:
        return False


def listed_in(root, relative, value):
    for entry in load_list(root, relative):
        if path_match(entry, value):
            return True
    return False


# ---------------------------------------------------------------------------
# Command analysis
# ---------------------------------------------------------------------------

def mask_toplevel(command):
    """Blank quoted spans and @{...} hashtable literals, preserving length.

    Only those two, deliberately. Brackets and parentheses are left alone so that a semicolon inside
    them still counts as a statement separator, and so an unbalanced bracket cannot blank the rest of
    the command and hide what follows.
    """
    chars = list(command)
    length = len(chars)
    index = 0
    while index < length:
        char = chars[index]
        if char in ("'", '"'):
            quote = char
            cursor = index + 1
            while cursor < length:
                if quote == '"' and chars[cursor] == "`":
                    cursor += 2
                    continue
                if chars[cursor] == quote:
                    break
                cursor += 1
            for position in range(index, min(cursor + 1, length)):
                chars[position] = " "
            index = cursor + 1
            continue
        index += 1
    text = "".join(chars)
    result = list(text)
    cursor = 0
    while True:
        start = text.find("@{", cursor)
        if start < 0:
            break
        depth = 0
        end = start + 1
        while end < len(text):
            if text[end] == "{":
                depth += 1
            elif text[end] == "}":
                depth -= 1
                if depth == 0:
                    break
            end += 1
        for position in range(start, min(end + 1, len(text))):
            result[position] = " "
        cursor = end + 1
    return "".join(result)


def statements(command):
    masked = mask_toplevel(command)
    return [part.strip() for part in masked.split(";") if part.strip()]


def computed_flags(command):
    if not command:
        return {"control_plane": False, "invokes_entry": False}
    parts = statements(command)
    control_plane = bool(parts) and all(CP_STMT_RE.match(part) for part in parts)
    return {"control_plane": control_plane, "invokes_entry": ENTRY_RE.search(command) is not None}


def dig(payload, dotted):
    cursor = payload
    for part in dotted.split("."):
        if isinstance(cursor, dict) and part in cursor:
            cursor = cursor[part]
        else:
            return None
    if cursor is None:
        return None
    if isinstance(cursor, str):
        return cursor
    if isinstance(cursor, (int, float, bool)):
        return str(cursor)
    return None


def extract_command(payload, policy, tool):
    """Resolve the tool's payload field path against tool_input, then the top level.

    Getting this wrong is not a cosmetic bug: if the command cannot be read, every command rule
    (the visual ban, human-only subcommands, approval) silently stops applying. The canary
    deny-human-only-subcommand exists to catch exactly that, and it did.
    """
    fields = policy.get("tools", {}).get("field", {})
    names = list(fields.get(tool, [])) + list(fields.get("default", []))
    tool_input = payload.get("tool_input")
    containers = [tool_input] if isinstance(tool_input, dict) else []
    containers.append(payload)
    seen = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        for container in containers:
            value = dig(container, name)
            if value and value.strip():
                return value
    return ""


def collect_paths(node, depth=0):
    found = []
    if depth > 4:
        return found
    if isinstance(node, dict):
        for key, value in node.items():
            if key in PATH_KEYS and isinstance(value, str) and value.strip():
                found.append(value)
            elif isinstance(value, (dict, list)):
                found.extend(collect_paths(value, depth + 1))
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)):
                found.extend(collect_paths(item, depth + 1))
    return found


def collect_content(node, depth=0):
    found = []
    if depth > 4:
        return found
    if isinstance(node, dict):
        for key, value in node.items():
            if key in CONTENT_KEYS and isinstance(value, str) and value:
                found.append(value)
            elif isinstance(value, (dict, list)):
                found.extend(collect_content(value, depth + 1))
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)):
                found.extend(collect_content(item, depth + 1))
    return found


def write_targets(root, command):
    if not command or not WRITE_RE.search(command):
        return []
    found = []
    for token in TOKEN_RE.findall(command):
        if len(token) < 3 or not token.strip("./\\"):
            continue
        found.append(norm_path(root, token))
    return found


# ---------------------------------------------------------------------------
# Tool classification
# ---------------------------------------------------------------------------

CLASS_ORDER = ("visual", "always_allow", "edit", "exec", "action")


def tool_class(policy, tool):
    tools = policy.get("tools", {})
    for name in CLASS_ORDER:
        if tool in tools.get(name, []):
            return name
    return "unknown"


def approval_needed(policy, state):
    """True when acting requires the human's approve."""
    if not enabled(policy):
        return False
    allowed = policy.get("approval", {}).get("allowed_states", list(ACTING_STATES))
    return state not in allowed


# ---------------------------------------------------------------------------
# Context and the rule engine
# ---------------------------------------------------------------------------

class Context(object):
    def __init__(self, root, policy, payload):
        self.root = root
        self.policy = policy
        self.payload = payload
        self.facts = load_facts(root, policy)
        self.state = read_state(root, policy)
        self.enabled = enabled(policy)
        self.needs_approval = approval_needed(policy, self.state)
        self.limits = limits(policy)
        self.tool = payload.get("tool_name") or ""
        self.klass = tool_class(policy, self.tool)
        self.command = extract_command(payload, policy, self.tool)
        self.raw_json = json.dumps(payload, ensure_ascii=True, sort_keys=True)
        self.content = "\n".join(collect_content(payload))
        self.computed = computed_flags(self.command)
        self.paths = [norm_path(root, item) for item in collect_paths(payload.get("tool_input") or {})]
        self.paths = [item for item in self.paths if item]
        self.targets = write_targets(root, self.command)
        self.infos = []
        self.report = []
        if not self.paths and self.targets:
            self.infos.append("paths inferred from the command: %s" % ", ".join(self.targets[:5]))
        # An unclassified tool that carries a command must be judged as if it were an exec tool,
        # otherwise a tool added later could run uninspected. A tool that only carries a path is left
        # alone, because read-only tools use the same field and must not be dragged into edit rules.
        # Action-class tools get the same treatment: create_and_run_task carries its command at
        # task.command, and without this the visual ban and the approval rule would not apply to it
        # once the state is executing.
        self.effective = self.klass
        if self.command and self.klass in ("action", "unknown"):
            self.effective = "exec"

    def state_line(self):
        return "state=%s enabled=%s approval_required=%s" % (
            self.state, "on" if self.enabled else "off", "yes" if self.needs_approval else "no")

    def approval_hint(self):
        return ("The human runs this in their own terminal:\n"
                "  python .github/ocf/ocf.py approve \"<one-sentence reason>\"")

    def surface(self, name):
        if name == "tool":
            return [self.tool] if self.tool else []
        if name == "command":
            return [self.command] if self.command else []
        if name == "path":
            return list(self.paths)
        if name == "write_target":
            return list(self.targets)
        if name == "content":
            return [self.content or self.raw_json]
        if name == "any":
            merged = [self.tool] if self.tool else []
            if self.command:
                merged.append(self.command)
            merged.extend(self.paths)
            merged.extend(self.targets)
            if self.content:
                merged.append(self.content)
            return merged
        return []


def condition_holds(context, condition):
    """Evaluate a context-scoped condition. See condition_holds_value for the value-scoped ones."""
    if "fact" in condition:
        name = condition["fact"]
        if condition.get("is_set"):
            return bool(context.facts.get(name, "").strip())
        return context.facts.get(name, "") == str(condition.get("is", ""))
    if "listed_in" in condition:
        # The union form, kept for context-scoped use. Rules that filter candidates want the
        # per-value form below instead; taking a union here is what once let a single human-code path
        # drag every other path in the same batch into the denial.
        for value in context.surface("path") + context.surface("write_target"):
            if listed_in(context.root, condition["listed_in"], value):
                return True
        return False
    if "content_matches" in condition:
        return re.search(condition["content_matches"], context.raw_json + "\n" + context.content,
                         re.I | re.S) is not None
    if "length_over" in condition:
        return len(context.command) > int(context.limits.get(condition["length_over"], 0))
    if "statements_over" in condition:
        return len(statements(context.command)) > int(context.limits.get(condition["statements_over"], 0))
    if "repeats_at_least" in condition:
        threshold = int(context.limits.get(condition["repeats_at_least"], 0))
        return command_repeat_count(context.root, context.policy, context.command) >= threshold
    if "computed" in condition:
        return bool(context.computed.get(condition["computed"]))
    raise OcfError("policy", "unknown condition key(s): %s" % ", ".join(sorted(condition)))


def when_holds(context, when):
    if when in (None, "always"):
        return True
    if when == "not_approved":
        return context.needs_approval
    if when == "approved":
        return context.enabled and not context.needs_approval
    if when == "enforced":
        return context.enabled
    if when == "not_enforced":
        return not context.enabled
    raise OcfError("policy", "unknown when value %r" % when)


def condition_holds_value(context, name, spec, value):
    """Evaluate a value-scoped condition against one candidate, not against the whole batch."""
    if name == "listed_in":
        return listed_in(context.root, spec, value)
    return condition_holds(context, {name: spec})


def evaluate_rule(context, rule):
    """Return a verdict dict when the rule fires, else None.

    Conditions split in two. Context-scoped ones (fact, computed, length_over, content_matches and
    friends) are true or false for the call as a whole. Value-scoped ones (listed_in) must be judged
    per candidate value, because a rule is asking "is THIS path the one I care about". Judging them as
    a union of the whole batch makes one qualifying path decide the fate of every other path, which
    both over-blocks and reports a target that is not the offender.
    """
    targets = rule.get("on", "any")
    if targets != "any":
        wanted = [item.strip() for item in str(targets).split(",")]
        if context.effective not in wanted:
            return None
    if not when_holds(context, rule.get("when")):
        return None
    values = context.surface(rule.get("surface", "any"))
    if not values:
        return None
    pattern = rule.get("match")
    hits = []
    for value in values:
        if pattern is None or pattern == ".*" or re.search(pattern, value, re.I):
            hits.append(value)
    if not hits:
        return None
    for key in ("only_if", "unless"):
        condition = rule.get(key)
        if not condition:
            continue
        context_part = {k: v for k, v in condition.items() if k not in VALUE_SCOPED_CONDITIONS}
        value_part = {k: v for k, v in condition.items() if k in VALUE_SCOPED_CONDITIONS}
        if context_part:
            holds = condition_holds(context, context_part)
            if (key == "only_if" and not holds) or (key == "unless" and holds):
                return None
        for name, spec in value_part.items():
            if key == "only_if":
                hits = [v for v in hits if condition_holds_value(context, name, spec, v)]
            else:
                hits = [v for v in hits if not condition_holds_value(context, name, spec, v)]
            if not hits:
                return None
    exempt = rule.get("exempt_when_listed")
    if exempt:
        remaining = [value for value in hits if not listed_in(context.root, exempt, value)]
        if not remaining:
            return None
        hits = remaining
    return {"id": rule.get("id", "unnamed"), "action": rule.get("action", "deny"),
            "why": rule.get("why", ""), "hit": hits[0], "rule": rule}


def decide(context):
    """Return (action, rule_id, reason). First matching rule wins.

    Every verdict names the rule that produced it, allow included. Without that, "why was this
    allowed" cannot be answered afterwards, which is the question that matters most in an audit.
    """
    if context.klass == "always_allow":
        return ("allow", "always-allowed",
                "[always-allowed] This tool is always allowed.")
    for rule in context.policy.get("rule", []):
        fired = evaluate_rule(context, rule)
        if fired:
            action = fired["action"]
            reason = "[%s] %s" % (fired["id"], fired["why"])
            if fired.get("hit") and fired["action"] in ("deny", "ask"):
                reason += "\n  target: %s" % fired["hit"]
            if action == "require_approval":
                reason = "[%s] Approval required. %s\n%s" % (
                    fired["id"], fired["why"], context.approval_hint())
                return ("require_approval", fired["id"], reason)
            return (action, fired["id"], reason)
    if context.klass == "exec" and not context.command and context.enabled:
        return ("deny", "unreadable-command",
                "[unreadable-command] This tool runs a command but no command could be read from the "
                "payload, so it cannot be inspected. Refusing rather than allowing it blind. Add the "
                "field path under [tools.field] in policy.toml.")
    if context.effective == "unknown":
        if context.needs_approval and UNKNOWN_ACTION_RE.search(context.tool):
            setting = context.policy.get("unknown_tool", {}).get("action", "ask")
            return (setting, "unknown-tool",
                    "[unknown-tool] This tool is not classified in policy.toml and is only escalated "
                    "while acting is not allowed. Classify it under [tools] if the answer should be "
                    "permanent.")
    return ("allow", "default", "[default] No rule objected.")

# ---------------------------------------------------------------------------
# Hook handling
# ---------------------------------------------------------------------------

DECISION_MAP = {"allow": "allow", "deny": "deny", "ask": "ask", "require_approval": "deny"}


def read_payload():
    raw = b""
    try:
        stream = getattr(sys.stdin, "buffer", None)
        raw = stream.read() if stream is not None else sys.stdin.read().encode("utf-8", "replace")
    except Exception:
        raw = b""
    if not raw or not raw.strip():
        return {}
    for encoding in ("utf-8", "utf-8-sig"):
        try:
            return json.loads(raw.decode(encoding))
        except Exception:
            continue
    return {}


def emit_pretooluse(action, reason):
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": DECISION_MAP.get(action, "deny"),
            "permissionDecisionReason": reason,
        }
    }
    out(json.dumps(payload, ensure_ascii=True))


def emit_context(event, text):
    if not text:
        return
    payload = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    out(json.dumps(payload, ensure_ascii=True))


def check_pretooluse(root, policy, payload):
    context = Context(root, policy, payload)
    action, rule_id, reason = decide(context)
    if action == "allow" and context.klass == "exec" and context.command:
        append_exec_log(root, policy, context.command)
    return action, rule_id, reason, context


def cmd_hook(root, argv):
    try:
        policy, warnings = load_policy(root)
    except Exception as exc:
        policy, warnings = dict(STRICT_POLICY), [("system", str(exc))]
    payload = read_payload()
    event = payload.get("hook_event_name") or (argv[0] if argv else "")
    try:
        if event == "PreToolUse":
            action, rule_id, reason, context = check_pretooluse(root, policy, payload)
            if action != "allow":
                reason = "\n".join([reason] + context.infos)
            emit_pretooluse(action, reason)
            return 0
        if event == "UserPromptSubmit":
            text = handle_prompt(root, policy, payload)
            emit_context("UserPromptSubmit", text)
            return 0
        if event in ("SessionStart", "SessionStartReload"):
            text = handle_session_start(root, policy)
            emit_context("SessionStart", text)
            return 0
        if event in ("Stop", "SubagentStop", "PreCompact", "SubagentStart", "PostToolUse"):
            return 0
        return 0
    except OcfError as exc:
        if event == "PreToolUse":
            emit_pretooluse("deny", "Blocked by the orchestrator: %s" % exc.tagged())
        else:
            out(exc.tagged())
        return 0
    except Exception as exc:  # fail safe: an internal fault denies rather than allows
        if event == "PreToolUse":
            emit_pretooluse("deny", "[system] internal error in the gate, denied to stay safe: %r" % (exc,))
        return 0


def log_prompt(root, policy, payload, state):
    """Record every UserPromptSubmit, enough to tell who actually spoke.

    Temporary instrumentation, not a rule: nothing reads it back. It exists because the gate has no
    reliable signal for "a human really replied", and the honest way to settle that is to look at what
    the hook actually receives rather than to reason about it. Delete it once the question is answered.
    """
    prompt = " ".join(str(payload.get("prompt") or "").split())[:160]
    session = str(payload.get("session_id") or "")[:8]
    path = os.path.join(state_dir(root, policy), "prompt-log")
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write("%s | %s | %s | %s\n" % (now_stamp(), session, state, prompt))


def handle_prompt(root, policy, payload):
    state = read_state(root, policy)
    facts = load_facts(root, policy)
    prompt = payload.get("prompt") or ""
    log_prompt(root, policy, payload, state)
    lines = []
    if state == "ready":
        write_state(root, policy, "asking")
        set_fact(root, policy, "first_prompt", " ".join(prompt.split())[:200])
        journal(root, policy, "asking", "advance ready -> asking (first human message)")
        lines.append("OCF: the human's first message moved ready -> asking.")
    elif state == "asking":
        rounds = 0
        try:
            rounds = int(facts.get("grill_rounds", "0"))
        except ValueError:
            rounds = 0
        rounds += 1
        set_fact(root, policy, "grill_rounds", str(rounds))
        journal(root, policy, "asking", "human reply #%d" % rounds)
    if facts.get("must_consult") == "yes":
        facts = load_facts(root, policy)
        facts.pop("must_consult", None)
        facts["fail_count"] = "0"
        save_facts(root, policy, facts)
        journal(root, policy, state, "human replied, failure budget cleared")
        lines.append("OCF: the failure budget is cleared, commands may resume.")
    if not enabled(policy):
        lines.append("OCF WARNING: system.enabled is false, so the gate is OFF and nothing is "
                     "required before acting.")
    state = read_state(root, policy)
    lines.append("OCF state: %s (%s)." % (state, ", ".join(STATES)))
    if state == "asking":
        missing = gate_missing(root, policy)
        if missing:
            lines.append("Missing intake items: %s. Ask one at a time, never fill them in."
                         % ", ".join(missing))
    return "\n".join(lines)


def handle_session_start(root, policy):
    lines = ["OCF self-check (classified):"]
    for kind, text in run_selftest(root, policy):
        lines.append("  [%s] %s" % (kind, text))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

def gate_context(root, policy):
    facts = load_facts(root, policy)
    missing = []
    for key in ("goal", "tools", "references", "deliverables", "code_style"):
        if len(facts.get(key, "").strip()) < 4:
            missing.append(key)
    return (not missing), "missing or too short: %s" % ", ".join(missing) if missing else "all present"


def gate_docs_decision(root, policy):
    value = load_facts(root, policy).get("docs_decision", "")
    return (value in ("create", "skip")), "docs_decision=%r, must be create or skip" % value


def gate_grill_valid(root, policy):
    facts = load_facts(root, policy)
    problems = []
    try:
        if int(facts.get("grill_rounds", "0")) < 1:
            problems.append("grill_rounds must be at least 1")
    except ValueError:
        problems.append("grill_rounds is not a number")
    if len(facts.get("consensus", "").strip()) < 10:
        problems.append("consensus must be at least 10 characters")
    if facts.get("grill_used", "") not in ("with-docs", "me"):
        problems.append("grill_used must be with-docs or me")
    return (not problems), "; ".join(problems) if problems else "ok"


def plan_sections(text):
    sections = {}
    current = None
    for line in text.splitlines():
        match = re.match(r"^\s*#{1,6}\s*(.+?)\s*$", line)
        if match:
            current = match.group(1).strip().lower()
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return sections


def read_plan(root):
    text = read_text(os.path.join(root, PLAN_REL))
    return text if text is not None else ""


def gate_plan_schema(root, policy):
    text = read_plan(root)
    if not text.strip():
        return False, "%s is missing or empty" % PLAN_REL
    sections = plan_sections(text)
    missing = [name for name in PLAN_HEADINGS if name not in sections]
    empty = [name for name in PLAN_HEADINGS
             if name in sections and not "".join(sections[name]).strip()]
    problems = []
    if missing:
        problems.append("missing headings: %s" % ", ".join("## " + item for item in missing))
    if empty:
        problems.append("empty headings: %s" % ", ".join("## " + item for item in empty))
    return (not problems), "; ".join(problems) if problems else "all 8 sections present"


def gate_zero_p0(root, policy):
    value = load_facts(root, policy).get("p0_count", "")
    if value == "":
        return False, "p0_count is not set; dispatch plan-auditor and record the result"
    try:
        count = int(value)
    except ValueError:
        return False, "p0_count=%r is not a number" % value
    return (count == 0), "p0_count=%d" % count


def plan_paths(text):
    found = []
    for line in plan_sections(text).get("files", []):
        ticks = re.findall(r"`([^`]+)`", line)
        if ticks:
            found.extend(ticks)
        else:
            found.extend(TOKEN_RE.findall(line))
    return [norm_path("", item) for item in found if item and len(item) >= 3]


def gate_human_code_clear(root, policy):
    text = read_plan(root)
    if not text.strip():
        return False, "%s is missing" % PLAN_REL
    allowed_file = policy_paths(policy)["allowed_edits_list"]
    human_file = policy_paths(policy)["human_code_list"]
    offenders = []
    for path in plan_paths(text):
        if listed_in(root, human_file, path) and not listed_in(root, allowed_file, path):
            offenders.append(path)
    return (not offenders), "touches human code: %s" % ", ".join(offenders) if offenders else "none"


def gate_stack_env(root, policy):
    value = load_facts(root, policy).get("stack_env", "").strip()
    return (len(value) >= 4), "stack_env=%r" % value


GATES = {
    "context": gate_context,
    "docs-decision": gate_docs_decision,
    "grill-valid": gate_grill_valid,
    "plan-schema": gate_plan_schema,
    "zero-p0": gate_zero_p0,
    "human-code-clear": gate_human_code_clear,
    "stack-env": gate_stack_env,
}

ENTER_TRANSITION_GATES = {
    ("asking", "planning"): ("context", "docs-decision", "grill-valid"),
    ("planning", "executing"): ("plan-schema", "zero-p0", "human-code-clear", "stack-env"),
}


def gate_missing(root, policy):
    ok, _ = gate_context(root, policy)
    if ok:
        return []
    facts = load_facts(root, policy)
    return [key for key in ("goal", "tools", "references", "deliverables", "code_style")
            if len(facts.get(key, "").strip()) < 4]


def run_gates(root, policy, names):
    results = []
    for name in names:
        function = GATES.get(name)
        if function is None:
            results.append((name, False, "unknown gate"))
            continue
        ok, detail = function(root, policy)
        results.append((name, ok, detail))
    return results


# ---------------------------------------------------------------------------
# Self test
# ---------------------------------------------------------------------------

def run_selftest(root, policy):
    """Return a list of (kind, message).

    kind is environmental | policy | system | ok. Only the first three are findings that need
    attention; `ok` marks a check that ran and passed. Reporting a passing check as if it were a
    fault would train the human to ignore the tags, which defeats the point of classifying them.
    """
    findings = []
    findings.append(("ok", "interpreter %s (%s.%s.%s)" % (
        sys.executable, sys.version_info[0], sys.version_info[1], sys.version_info[2])))
    if tomllib is None:
        findings.append(("environment", "no TOML parser: install tomli or use Python 3.11+"))
    else:
        findings.append(("ok", "TOML parser available"))
    policy_file = os.path.join(root, POLICY_REL)
    if read_text(policy_file) is None:
        findings.append(("policy", "%s not found, the strict fallback policy is in force" % POLICY_REL))
    else:
        findings.append(("ok", "%s parsed" % POLICY_REL))
    hooks_text = read_text(os.path.join(root, HOOKS_REL))
    if hooks_text is None:
        findings.append(("environment", "%s not found; no hook events will arrive" % HOOKS_REL))
    elif "ocf.py" not in hooks_text:
        findings.append(("environment", "%s does not point at ocf.py, so this gate is not wired in"
                                       % HOOKS_REL))
    else:
        findings.append(("ok", "%s points at ocf.py" % HOOKS_REL))
    if not enabled(policy):
        findings.append(("policy", "system.enabled is false: the gate is OFF. Flip it to true when "
                                   "you want approval to be required."))
    else:
        findings.append(("ok", "system.enabled is true"))
    try:
        if not os.path.isdir(state_dir(root, policy)):
            os.makedirs(state_dir(root, policy))
        probe = os.path.join(state_dir(root, policy), ".selftest")
        write_text(probe, "ok")
        os.remove(probe)
        findings.append(("ok", "state directory is writable"))
    except Exception as exc:
        findings.append(("system", "state directory is not writable: %r" % (exc,)))
    canaries = policy.get("selftest", {}).get("canary", [])
    if not canaries:
        findings.append(("policy", "no canaries are defined in [selftest]; the gate cannot prove "
                                   "that it is still denying"))
    for canary in canaries:
        expect = canary.get("expect", "deny")
        payload = dict(canary.get("payload", {}))
        payload.setdefault("hook_event_name", "PreToolUse")
        try:
            action, rule_id, _, _ = check_pretooluse(root, policy, payload)
        except Exception as exc:
            findings.append(("system", "canary %s raised %r" % (canary.get("id"), exc)))
            continue
        got = DECISION_MAP.get(action, "deny")
        if got == DECISION_MAP.get(expect, "deny"):
            findings.append(("ok", "canary %s (%s, rule %s)" % (canary.get("id"), got, rule_id)))
        else:
            findings.append(("system", "canary %s FAILED: expected %s, got %s (rule %s). The gate no "
                                       "longer enforces what the policy says." % (
                                           canary.get("id"), expect, got, rule_id)))
    return findings


def selftest_failed(findings):
    for kind, _ in findings:
        if kind != "ok":
            return True
    return False


# ---------------------------------------------------------------------------
# Agent CLI
# ---------------------------------------------------------------------------

def cmd_status(root, policy):
    state = read_state(root, policy)
    facts = load_facts(root, policy)
    out("state=%s" % state)
    out("enabled=%s" % ("true" if enabled(policy) else "false"))
    out("approval_required=%s" % ("yes" if approval_needed(policy, state) else "no"))
    out("policy=%s" % POLICY_REL)
    out("limits=%s" % ",".join("%s:%s" % (key, limits(policy).get(key)) for key in sorted(limits(policy))))
    out("facts:")
    for key in sorted(facts):
        out("  %s=%s" % (key, facts[key]))
    if not enabled(policy):
        out("WARNING: system.enabled is false, so the gate is OFF.")
    return 0


def parse_assignment(args):
    if not args:
        raise OcfError("environment", "set needs key=value")
    if "=" in args[0]:
        key, value = args[0].split("=", 1)
    elif len(args) >= 2:
        key, value = args[0], " ".join(args[1:])
    else:
        raise OcfError("environment", "set needs key=value")
    return key.strip(), value.strip()


def cmd_set(root, policy, args):
    key, value = parse_assignment(args)
    if key in ("approved_by", "must_consult"):
        raise OcfError("environment", "%s is not settable by the agent" % key)
    if key == "grill_rounds":
        raise OcfError("environment", "grill_rounds is counted by the hook, do not write it")
    if "\n" in value or "\r" in value:
        raise OcfError("environment", "a fact value must be one line")
    set_fact(root, policy, key, value)
    out("set %s=%s" % (key, value))
    return 0


def cmd_gate(root, policy, args):
    name = args[0] if args else "all"
    if name == "all":
        results = run_gates(root, policy, sorted(GATES))
    else:
        if name not in GATES:
            raise OcfError("environment", "unknown gate %r; known: %s" % (name, ", ".join(sorted(GATES))))
        results = run_gates(root, policy, (name,))
    failed = 0
    for gate_name, ok, detail in results:
        out("%-18s %s  %s" % (gate_name, "PASS" if ok else "FAIL", detail))
        if not ok:
            failed += 1
    return 0 if failed == 0 else 1


def cmd_journal(root, policy, args):
    count = 10
    if args:
        try:
            count = int(args[0])
        except ValueError:
            raise OcfError("environment", "journal takes a number")
    lines = read_lines(os.path.join(state_dir(root, policy), "journal.log"))
    for line in lines[-count:]:
        out(line)
    return 0


def cmd_fail(root, policy, args):
    reason = " ".join(args).strip()
    if not reason:
        raise OcfError("environment", "fail needs a one-line reason")
    facts = load_facts(root, policy)
    try:
        count = int(facts.get("fail_count", "0"))
    except ValueError:
        count = 0
    count += 1
    facts["fail_count"] = str(count)
    budget = int(limits(policy).get("fail_budget", 2))
    if count >= budget:
        facts["must_consult"] = "yes"
    save_facts(root, policy, facts)
    journal(root, policy, read_state(root, policy), "fail #%d: %s" % (count, reason))
    out("consecutive failures: %d of %d" % (count, budget))
    if count >= budget:
        out("MUST CONSULT: stop and report the symptom, what you tried, and what you need.")
    return 0


def cmd_ok(root, policy, args):
    facts = load_facts(root, policy)
    facts.pop("must_consult", None)
    facts["fail_count"] = "0"
    save_facts(root, policy, facts)
    journal(root, policy, read_state(root, policy), "ok: failure counter reset")
    out("failure counter reset")
    return 0


def transition_gate_names(source, target):
    if target == "blocked":
        return ()
    if source == "blocked":
        return ()
    return ENTER_TRANSITION_GATES.get((source, target), ())


def cmd_advance(root, policy, args):
    if not args:
        raise OcfError("environment", "advance needs a target")
    target = args[0].strip()
    if target not in STATES:
        raise OcfError("environment", "unknown state %r" % target)
    source = read_state(root, policy)
    if target == "executing":
        raise OcfError("environment", "only the human enters executing, by running approve in "
                                      "their own terminal")
    if target == source:
        journal(root, policy, source, "advance %s (staying put)" % target)
        out("state stays %s" % source)
        return 0
    if target not in AGENT_TARGETS:
        raise OcfError("environment", "the agent may only advance to %s" % ", ".join(AGENT_TARGETS))
    reason = " ".join(args[1:]).strip() or "no reason given"
    names = transition_gate_names(source, target)
    results = run_gates(root, policy, names)
    failed = [(name, detail) for name, ok, detail in results if not ok]
    if failed:
        for name, detail in failed:
            out("GATE FAIL %-18s %s" % (name, detail))
        raise OcfError("system", "refusing %s -> %s: %d gate(s) failed. Advance blocked and ask the "
                                 "human instead of forcing it." % (source, target, len(failed)))
    write_state(root, policy, target)
    journal(root, policy, target, "advance %s -> %s (%s)" % (source, target, reason))
    out("state %s -> %s" % (source, target))
    return 0


def cmd_init(root, policy):
    directory = state_dir(root, policy)
    if not os.path.isdir(directory):
        os.makedirs(directory)
    path = os.path.join(directory, "state")
    if read_text(path) is None:
        write_state(root, policy, "ready")
        journal(root, policy, "ready", "init")
        out("initialised state=ready")
    else:
        out("already initialised, state=%s" % read_state(root, policy))
    return 0


def cmd_selftest(root, policy, args):
    findings = run_selftest(root, policy)
    for kind, text in findings:
        out("[%s] %s" % (kind, text))
    if selftest_failed(findings):
        out("RESULT: FAIL")
        return 1
    out("RESULT: PASS")
    return 0


# ---------------------------------------------------------------------------
# Human-only CLI
# ---------------------------------------------------------------------------

def require_reason(policy, args, verb):
    reason = " ".join(args).strip()
    minimum = int(policy.get("approval", {}).get("min_reason_len", 8))
    if len(reason) < minimum:
        raise OcfError("environment", "%s needs a real one-sentence reason (at least %d characters)"
                                      % (verb, minimum))
    return reason


def cmd_approve(root, policy, args):
    reason = require_reason(policy, args, "approve")
    state = read_state(root, policy)
    if state != "planning":
        raise OcfError("environment", "approve only applies in planning, current state is %s" % state)
    results = run_gates(root, policy, ENTER_TRANSITION_GATES[("planning", "executing")])
    for name, ok, detail in results:
        out("%-18s %s  %s" % (name, "PASS" if ok else "FAIL", detail))
    failed = [name for name, ok, _ in results if not ok]
    if failed:
        raise OcfError("system", "refusing to approve, gate(s) failed: %s" % ", ".join(failed))
    write_state(root, policy, "executing")
    set_fact(root, policy, "approved_by", os.environ.get("OCF_APPROVED_BY", "human"))
    set_fact(root, policy, "approved_reason", reason)
    journal(root, policy, "executing", "planning -> executing (approved: %s)" % reason)
    out("approved, state planning -> executing")
    return 0


def cmd_reject(root, policy, args):
    reason = " ".join(args).strip() or "rejected"
    state = read_state(root, policy)
    if state != "planning":
        raise OcfError("environment", "reject only applies in planning, current state is %s" % state)
    write_state(root, policy, "asking")
    journal(root, policy, "asking", "planning -> asking (rejected: %s)" % reason)
    out("rejected, state planning -> asking")
    return 0


def cmd_confirm(root, policy, args):
    state = read_state(root, policy)
    if state != "asking":
        raise OcfError("environment", "confirm only applies in asking, current state is %s" % state)
    results = run_gates(root, policy, ENTER_TRANSITION_GATES[("asking", "planning")])
    for name, ok, detail in results:
        out("%-18s %s  %s" % (name, "PASS" if ok else "FAIL", detail))
    failed = [name for name, ok, _ in results if not ok]
    if failed:
        raise OcfError("system", "refusing to confirm, gate(s) failed: %s" % ", ".join(failed))
    write_state(root, policy, "planning")
    journal(root, policy, "planning", "asking -> planning (confirmed by the human)")
    out("confirmed, state asking -> planning")
    return 0


def update_list(root, policy, which, path, add):
    relative = policy_paths(policy)[which]
    absolute = os.path.join(root, relative)
    entries = load_list(root, relative)
    path = (path or "").strip()
    if not path:
        raise OcfError("environment", "that command needs a path argument")
    key = norm_path(root, path)
    if add:
        if key not in entries:
            entries.append(key)
            write_text(absolute, "".join(entry + "\n" for entry in entries))
        out("added %s to %s" % (key, relative))
    else:
        remaining = [entry for entry in entries if norm_path("", entry) != key]
        write_text(absolute, "".join(entry + "\n" for entry in remaining))
        out("removed %s from %s" % (key, relative))
    journal(root, policy, read_state(root, policy), "%s %s %s" % (
        "allow" if add else "deny", which, key))
    return 0


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

USAGE = """OCF - orchestrator control flow

agent:  status | set key=value | gate [name|all] | journal [n] | fail "<reason>" | ok
        advance <asking|planning|reporting|ready|blocked> ["<reason>"] | init | selftest
human:  approve "<reason>" | reject ["<reason>"] | confirm
        allow <path> | deny <path> | human-code <path>

The hook entry is: ocf.py hook   (reads the event JSON on stdin)
"""


def main(argv):
    setup_io()
    if not argv or argv[0] in ("-h", "--help", "help"):
        out(USAGE)
        return 0
    command = argv[0]
    args = argv[1:]
    root = find_root()
    if command == "hook":
        return cmd_hook(root, args)
    try:
        policy, warnings = load_policy(root)
        for kind, text in warnings:
            out("[%s] %s" % (kind, text))
        if command == "status":
            return cmd_status(root, policy)
        if command == "set":
            return cmd_set(root, policy, args)
        if command == "gate":
            return cmd_gate(root, policy, args)
        if command == "journal":
            return cmd_journal(root, policy, args)
        if command == "fail":
            return cmd_fail(root, policy, args)
        if command == "ok":
            return cmd_ok(root, policy, args)
        if command == "advance":
            return cmd_advance(root, policy, args)
        if command == "init":
            return cmd_init(root, policy)
        if command == "selftest":
            return cmd_selftest(root, policy, args)
        if command == "approve":
            return cmd_approve(root, policy, args)
        if command == "reject":
            return cmd_reject(root, policy, args)
        if command == "confirm":
            return cmd_confirm(root, policy, args)
        if command == "allow":
            return update_list(root, policy, "allowed_edits_list", args[0] if args else "", True)
        if command == "deny":
            return update_list(root, policy, "allowed_edits_list", args[0] if args else "", False)
        if command == "human-code":
            return update_list(root, policy, "human_code_list", args[0] if args else "", True)
        out("unknown command %r" % command)
        out(USAGE)
        return 1
    except OcfError as exc:
        out(exc.tagged())
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
