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

# The state set is the literal and the table is built from it. Deriving STATES from the table instead
# would make the structural check that compares the two true by construction, and a check that cannot
# fail is indistinguishable from no check at all.
STATES = ("ready", "asking", "planning", "executing", "reporting", "blocked")

# One table, one owner: the states, the targets the agent may advance to, the states in which acting
# is already approved, the gates each transition must pass, and the CLI vocabulary of both sides,
# including the fragment the usage text is rendered from. The hook, advance, approve, reject, confirm
# and the usage text all read it. A state list with four copies is a rename waiting to be missed, and
# that miss is silent.
TRANSITIONS = {
    "states": STATES,
    "agent_targets": ("asking", "planning", "reporting", "ready", "blocked"),
    "acting_states": ("executing", "reporting"),
    # The happy path and the way out of it. Held here rather than drawn as a diagram in a document,
    # because a diagram is prose: nothing compares it to STATES, so a state renamed in the code leaves
    # the drawing behind and the drawing is what the human reads first.
    "flow": ("ready", "asking", "planning", "executing", "reporting", "ready"),
    "bypass": "blocked",
    "gates": {
        ("asking", "planning"): ("context", "docs-decision", "grill-valid"),
        ("planning", "executing"): ("plan-schema", "zero-p0", "protected-list-clear", "stack-env"),
    },
    # Grouped the way the usage text prints them: one tuple per line.
    "commands": {
        "agent": (("status", "set", "gate", "journal", "fail", "ok"),
                  ("advance", "init", "selftest")),
        "human": (("approve", "reject", "confirm"),
                  ("allow", "deny"),
                  ("reload",)),
    },
    "usage": {
        "status": "status",
        "set": "set key=value",
        "gate": "gate [name|all]",
        "journal": "journal [n]",
        "fail": 'fail "<reason>"',
        "ok": "ok",
        "advance": 'advance <%s> ["<reason>"]',
        "init": "init",
        "selftest": "selftest",
        "reload": "reload",
        "approve": 'approve "<reason>"',
        "reject": 'reject ["<reason>"]',
        "confirm": "confirm",
        "allow": "allow <path>",
        "deny": "deny <path>",
    },
}

# Views, not copies: one thing under the names the call sites already use.
AGENT_TARGETS = TRANSITIONS["agent_targets"]
ACTING_STATES = TRANSITIONS["acting_states"]
ENTER_TRANSITION_GATES = TRANSITIONS["gates"]

POLICY_REL = ".github/ocf/policy.toml"
HOOKS_REL = ".github/hooks/orchestrator.json"
PLAN_REL = ".orchestrator/plan.md"

DEFAULT_STATE_DIR = ".orchestrator"

PLAN_HEADINGS = ("type", "summary", "steps", "tools", "files", "scope", "deliverables", "self-review")
# What a WRITTEN plan must carry, which is only what a gate actually reads. The eight-section shape is
# the agent's own working structure and lives in the template; demanding a file repeat it made the
# human pay for a document nobody read.
PLAN_REQUIRED = ("steps", "files")

# Entry script recognised only by its full relative path. Merely mentioning the name elsewhere must
# not inherit the control plane exemption, which was a real bypass in the previous implementation.
# One branch, not three: the PowerShell and sh implementations are gone, and a pattern that keeps
# naming deleted files would hand the exemption to a path nobody executes.
ENTRY_REL = ".github/ocf/ocf.py"
# The subcommand the hook wiring must pass. Held here and used by both the renderer and the dispatch,
# because getting it wrong is silent in the worst way: `ocf.py` with no arguments prints the usage and
# exits 0, which a hook reads as "no objection". A wiring file missing this word is a gate that looks
# installed and decides nothing.
HOOK_SUBCOMMAND = "hook"
ENTRY_PAT = re.escape(ENTRY_REL).replace("/", r"[\\/]")
ENTRY_RE = re.compile(r"(?i)" + ENTRY_PAT)
CP_STMT_RE = re.compile(r"(?i)^\s*(?:&\s*)?(?:(?:python3?(?:\.exe)?|py(?:\.exe)?)\s+)?[\"']?" + ENTRY_PAT + r"[\"']?(?:\s|$)")

# name in the wiring file -> (switch in [hooks], the event this program reads, timeout seconds).
# One table for the same reason as the command table: the wiring file is generated from it, and a
# second hand-written copy of an event name is a hook that silently stops being installed.
HOOK_EVENTS = (
    ("session_start", "SessionStart", 30),
    ("user_prompt", "UserPromptSubmit", 20),
    ("pre_tool_use", "PreToolUse", 20),
    ("post_tool_use", "PostToolUse", 20),
)

PATH_KEYS = ("filePath", "file_path", "path", "newPath", "uri", "dirPath")

# Conditions that must be judged against one candidate value rather than against the whole call. One
# table, so the name list and the evaluator cannot drift apart: VALUE_SCOPED_CONDITIONS is derived
# from it instead of written a second time. The lambda resolves `listed_in` at call time, which is why
# the table may sit above the function it uses.
VALUE_SCOPED_TESTS = {"listed_in": lambda context, spec, value: listed_in(context.root, spec, value)}
VALUE_SCOPED_CONDITIONS = tuple(VALUE_SCOPED_TESTS)
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
    """Return the file's text with any BOM stripped and line endings normalised, or None if absent.

    Line endings are normalised on the way in. This program writes "\n" - see write_text - and it
    compares what it read against what it would write to decide whether a generated file is already
    current. On Windows, git's autocrlf hands out CRLF, so those comparisons never matched: reload
    reported "rewrote" every single time and the "already matches" branch was unreachable. Harmless
    looking, and it hid a real difference: a human reading "rewrote" cannot tell a real change from the
    checkout's line endings.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as handle:
            data = handle.read()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OcfError("environment", "cannot read %s: %s" % (path, exc))
    data = data.replace("\r\n", "\n").replace("\r", "\n")
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
    },
    "limits": {"fail_budget": 2},
    "approval": {"allowed_states": ["executing", "reporting"], "min_reason_len": 8},
    "hooks": {"enabled": True,
              "session_start": True, "user_prompt": True,
              "pre_tool_use": True, "post_tool_use": True},
    "unknown_tool": {"action": "ask"},
    "tools": {
        "visual": [],
        "env": [],
        "exec": [],
        "write": [],
        # These two carry real defaults, because the strict fallback policy is built from this block.
        # Denying every change is the point of a fail-safe; denying reading as well protects nothing
        # and turns a one-character policy typo into a total lockout that the agent cannot even help
        # diagnose. See REFACTOR-DESIGN.md section 14.
        "session": ["runSubagent", "manage_todo_list", "vscode_askQuestions", "memory"],
        "read": [
            "read_file",
            "grep_search",
            "file_search",
            "list_dir",
            "get_errors",
            "copilot_getNotebookSummary",
            "read_notebook_cell_output",
            "vscode_listCodeUsages",
        ],
        "field": {"default": ["command", "code"]},
    },
    "selftest": {"canary": []},
    "rule": [],
}

# The strictest thing that still lets work continue. Used when the policy cannot be read at all, so
# that "policy is broken" can never mean "gate is open". It denies every change while leaving reading
# and the read and session tools open: the gate must be able to refuse work, not to blind everyone.
STRICT_POLICY = json.loads(json.dumps(DEFAULT_POLICY))
STRICT_POLICY["rule"] = [
    {
        "id": "strict-fallback",
        "on": "any",
        "surface": "any",
        "match": ".*",
        "action": "deny",
        "why": "No usable policy file was found, so the strict fallback policy is in force. Reading "
               "and the read and session tools still work, so the reason can be diagnosed. Restore "
               ".github/ocf/policy.toml.",
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
            ("policy", "%s failed to parse: %s. The strict fallback policy is in force, which denies "
                       "every change and allows only reading and the session tools."
                       % (POLICY_REL, exc))])
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
# Policy vocabulary
# ---------------------------------------------------------------------------

# Every identifier the interpreter is willing to act on. Kept next to the engine rather than in the
# policy file, because the point is to check the file against the engine, not against itself.
SECTION_KEYS = ("system", "paths", "limits", "approval", "hooks", "unknown_tool",
                "tools", "selftest", "rule")
# `is_set` and `is` sit beside `fact`, not inside it: condition_holds reads them as siblings.
# The three command-limit conditions are gone: each rule states its own number, so a rule and the
# limit it reads can no longer be separated into two files where only one of them is edited.
CONDITION_KEYS = ("fact", "is_set", "is", "listed_in", "content_matches", "computed")
WHEN_VALUES = ("always", "enforced")
SURFACE_NAMES = ("tool", "command", "path", "write_target", "content", "any")
ACTION_VALUES = ("allow", "deny", "ask", "require_approval")
COMPUTED_FLAGS = ("control_plane", "invokes_entry", "human_only_call", "enforced")

# When a soft rule applies. A soft rule is the one kind of rule no hook can enforce - nothing can
# check whether a sentence was written - so instead of a verdict it carries the sentence itself, and
# `reload` writes it into the instructions the model reads on every turn. The occasion is therefore a
# moment in the conversation, not a tool call: the hard vocabulary (class, tool, command, path) has no
# meaning when no tool is being called.
SOFT_OCCASIONS = ("session-start", "ask", "plan", "act", "answer")
RULE_KEYS = ("id", "on", "surface", "match", "when", "action", "why", "only_if", "unless",
             # The newer shape. Both are accepted while the policy is converted rule by rule.
             "enabled", "kind", "label", "result", "if")


# ---------------------------------------------------------------------------
# The vocabulary, described once
# ---------------------------------------------------------------------------
#
# A policy rule may name a section, a key, a condition or a value - and nothing else. That list is the
# policy file's interface, so it has to be documented; it used to be documented a second time as prose
# in the policy file's own header, and the two drifted. The prose went on offering five `when` values
# after the engine accepted two, and following it raised inside the evaluator, which the hook turns into
# a denial of everything - a documented instruction that locks the gate. So the descriptions live here,
# `reload` renders them into the marked region of policy.toml, and a structural check compares the two
# sets in both directions: a key with no description fails, and a description of a key that no longer
# exists fails.

SECTION_HELP = (
    ("system", "enabled: the maintenance switch. False stops the three `when = enforced` rules (the "
               "orchestrator's self-protection and the self-authorization rule) and the three approval "
               "rules, and nothing else - the protected list is judged by a rule with no `when`, so it "
               "still applies"),
    ("paths", "state_dir: where the runtime lives. Everything else is a fixed filename, not a setting"),
    ("limits", "fail_budget: consecutive failures before exec-class tools are locked"),
    ("approval", "allowed_states: the states acting is allowed in. min_reason_len: shortest approve reason"),
    ("hooks", "which events the orchestrator is installed on; reload rewrites the wiring from these"),
    ("unknown_tool", "action: verdict for an unrecognised tool that looks like it acts"),
    ("tools", "the tool classes, and the payload field each tool keeps its command in"),
    ("selftest", "canaries: one must deny and one must allow, or the gate is not proven alive"),
    ("rule", "the rules themselves, evaluated in file order, first match wins"),
)

RULE_FIELD_HELP = (
    ("id", "the name every verdict carries, so a decision can be traced back to its rule. The tests "
           "assert these, so renaming one fails instead of drifting"),
    ("enabled", "true or false. A switched-off rule is not evaluated at all"),
    ("kind", "hard: the hook reads it and gives a verdict. soft: reload writes it into the contract"),
    ("label", "the name a soft rule is listed under in the generated contract"),
    ("if", "the conditions that must all hold. This is the whole of a rule's logic"),
    ("unless", "the exception. For a hard rule a condition table; for a soft rule a sentence the model applies"),
    ("result", "hard: allow | ask | deny | require_approval. soft: the sentence to inject"),
    ("why", "shown to the agent in the verdict, so write what to fix, not what went wrong"),
)

# The older shape. Three rules are still written this way: the two that protect the orchestrator's own
# files and the one against writing a human-only subcommand into a script. They are migrated last, on
# purpose, because the migration is what removes the machine's ability to edit them.
RULE_FIELD_HELP_OLDER = (
    ("on", "tool class this rule applies to: visual | env | exec | write | read | session | unknown | any"),
    ("surface", "which text to match: tool | command | path | write_target | content | any"),
    ("match", "regex tried against every candidate value of that surface"),
    ("when", "always | enforced (system.enabled is true, regardless of state)"),
    ("action", "allow | ask | deny | require_approval"),
    ("only_if", "an extra condition table that must hold"),
)

CONDITION_HELP = (
    ("class", "the tool's class, or the class a tool carrying a command is judged as: visual, env, "
              "exec, write, read, session, unknown"),
    ("state", "the current state: ready, asking, planning, executing, reporting, blocked"),
    ("tool", "the tool name as the editor reports it"),
    ("command_matches", "regex against the command string"),
    ("command_length_over", "the command is longer than this many characters"),
    ("command_statements_over", "the command has more statements than this, counted with quotes blanked"),
    ("command_repeats_at_least", "this exact command has already run at least this many times"),
    ("content_matches", "regex against what would be written, plus the raw tool input"),
    ("environment_declared", "the human has declared the existing environment (the stack_env fact)"),
    ("approval", "true when a human approve is still outstanding, i.e. the state is not an acting state"),
    ("computed", "a flag computed from the command. See the list below"),
    ("fact", "a fact recorded by the human or the hook, compared with is / is_not / is_set"),
    ("is_set", "with fact: the fact is non-empty (true) or empty (false)"),
    ("is", "with fact: the fact equals this value"),
    ("is_not", "with fact: the fact does not equal this value"),
    ("path_matches", "regex against each path the action touches. Judged per path, never as a batch"),
    ("touches_protected", "each path the action touches is on the protected list, judged per path"),
    ("listed_in", "older shape: each candidate value appears in that list file"),
)

COMPUTED_HELP = (
    ("control_plane", "every statement runs the entry script by its full relative path, so reading the "
                      "state is not acting and needs no approval"),
    ("invokes_entry", "at least one statement does"),
    ("human_only_call", "the entry script is invoked with a human-only subcommand as its argument"),
    ("enforced", "system.enabled is true: the maintenance window is closed. The three rules that "
                  "protect the orchestrator's own files are the only users, and removing this word "
                  "from them is what makes that protection unconditional"),
)

WHEN_HELP = (
    ("always", "always"),
    ("enforced", "system.enabled is true, regardless of the current state"),
)

ACTION_HELP = (
    ("allow", "let it through"),
    ("ask", "the editor asks the human once; a pre-approval may swallow this"),
    ("deny", "refuse, and say why. Handled before any approval logic, so nothing auto-approves past it"),
    ("require_approval", "refuse, and tell the agent to have the human run the approve command"),
)

OCCASION_HELP = (
    ("session-start", "a session begins"),
    ("ask", "the human's statement is being read and gaps are being grilled"),
    ("plan", "a plan is being written or audited"),
    ("act", "a tool is about to be used"),
    ("answer", "every answer"),
)

HOOK_HELP = (
    ("enabled", "the master switch. false writes a wiring file with no hooks: nothing is installed"),
    ("session_start", "run the self-check when a session begins"),
    ("user_prompt", "advance the state machine and report it on each human message"),
    ("pre_tool_use", "the gate. Switching it off stops everything being refused"),
    ("post_tool_use", "warn when a command produced no progress"),
)

# What each gate demands, next to the transition that runs it. This was prose in the rules document and
# it drifted twice over: the gate list there named a rule (`protected-file`) instead of a gate, and
# claimed plan-schema wants the eight plan sections when it reads two of them.
GATE_HELP = {
    "context": "the five intake items are present and each at least 4 characters",
    "docs-decision": "docs_decision is create or skip",
    "grill-valid": "grill_rounds is at least 1, consensus at least 10 characters, grill_used is "
                   "with-docs or me",
    "plan-schema": "a plan file, if one exists, carries `## Steps` and `## Files`, both non-empty. An "
                   "absent file passes: the plan is a conversation artefact by default",
    "zero-p0": "p0_count is 0",
    "protected-list-clear": "the plan's `## Files` section names no path on the protected list",
    "stack-env": "stack_env is declared, which means the human said what the environment is rather "
                 "than the machine probing for it",
}


def render_vocabulary():
    """Render the policy file's interface as comments. Pure, so the file can be compared to it."""
    lines = ["# Every identifier this file may use, and what it means.",
             "# Rendered by `python .github/ocf/ocf.py reload` from the tables in ocf.py, so it cannot",
             "# describe a key the engine does not read, or miss one it does.",
             "",
             "# [sections]"]
    lines += ["#   %-14s %s" % (name, help_text) for name, help_text in SECTION_HELP]
    lines += ["",
              "# [rule] fields. Two shapes are accepted while the older one is converted away, and",
              "# this region is generated - edit the rules, not this text."]
    lines += ["#   %-14s %s" % (name, help_text) for name, help_text in RULE_FIELD_HELP]
    lines += ["",
              "#   the older shape, still used by the rules that protect the orchestrator itself:"]
    lines += ["#   %-14s %s" % (name, help_text) for name, help_text in RULE_FIELD_HELP_OLDER]
    lines += ["",
              "# [rule.if] conditions. Each key is one condition; the value is what it compares against,",
              "# which is usually a regex or a number."]
    lines += ["#   %-22s %s" % (name, help_text) for name, help_text in CONDITION_HELP]
    lines += ["",
              "# computed flags, named by the `computed` condition:"]
    lines += ["#   %-18s %s" % (name, help_text) for name, help_text in COMPUTED_HELP]
    lines += ["",
              "# `when` (older shape) accepts:"]
    lines += ["#   %-18s %s" % (name, help_text) for name, help_text in WHEN_HELP]
    lines += ["",
              "# `result` and `action` accept:"]
    lines += ["#   %-18s %s" % (name, help_text) for name, help_text in ACTION_HELP]
    lines += ["",
              "# a soft rule's occasion, i.e. when its sentence is in force:"]
    lines += ["#   %-18s %s" % (name, help_text) for name, help_text in OCCASION_HELP]
    lines += ["",
              "# [hooks] switches:"]
    lines += ["#   %-18s %s" % (name, help_text) for name, help_text in HOOK_HELP]
    return "\n".join(lines)


def policy_findings(policy):
    """Report identifiers the engine does not know, as (kind, message) pairs.

    An unknown `on` class, a misspelled limit key, a condition the engine never reads: none of them
    raises. The rule simply stops matching, so the gate keeps running while enforcing less than the
    file says. That is the defect class this project keeps meeting, so it is checked rather than
    hoped for.

    These findings deliberately do NOT travel through load_policy's warnings. Every guard in
    tests/run.py treats a warning as "the policy did not load cleanly", and a policy holding one
    unknown key has loaded perfectly well; mixing the two would make those guards fail for the wrong
    reason, which is its own silent defect.
    """
    problems = []
    for key in sorted(policy):
        if key not in SECTION_KEYS:
            problems.append("unknown policy section [%s]" % key)
    for key in sorted(policy.get("limits", {})):
        if key not in DEFAULT_POLICY["limits"]:
            problems.append("[limits] unknown key %r; the engine knows %s"
                            % (key, ", ".join(sorted(DEFAULT_POLICY["limits"]))))
    for key in sorted(policy.get("hooks", {})):
        if key not in DEFAULT_POLICY["hooks"]:
            problems.append("[hooks] unknown key %r; the engine knows %s"
                            % (key, ", ".join(sorted(DEFAULT_POLICY["hooks"]))))
    for key in sorted(policy.get("tools", {})):
        if key not in DEFAULT_POLICY["tools"]:
            problems.append("[tools] unknown class %r; the engine knows %s"
                            % (key, ", ".join(sorted(DEFAULT_POLICY["tools"]))))
    for index, rule in enumerate(policy.get("rule", [])):
        where = "rule %s" % (rule.get("id") or "#%d" % index)
        # Read once, at the top: the per-rule checks below branch on it, and defining it half way
        # down meant the branches above it read an unbound name.
        kind = str(rule.get("kind", "hard")).strip().lower()
        for key in sorted(rule):
            if key not in RULE_KEYS:
                problems.append("%s: unknown key %r; the engine reads %s"
                                % (where, key, ", ".join(RULE_KEYS)))
        for name in [item.strip() for item in str(rule.get("on", "any")).split(",")]:
            if name and name not in CLASS_ORDER + ("any", "unknown"):
                problems.append("%s: unknown on class %r; the engine knows %s"
                                % (where, name, ", ".join(CLASS_ORDER + ("any", "unknown"))))
        surface = rule.get("surface", "any")
        if surface not in SURFACE_NAMES:
            problems.append("%s: unknown surface %r; the engine knows %s"
                            % (where, surface, ", ".join(SURFACE_NAMES)))
        when = rule.get("when")
        if when is not None and when not in WHEN_VALUES:
            problems.append("%s: unknown when %r; the engine knows %s"
                            % (where, when, ", ".join(WHEN_VALUES)))
        action = rule.get("action")
        if action is not None and action not in ACTION_VALUES:
            problems.append("%s: unknown action %r; the engine knows %s"
                            % (where, action, ", ".join(ACTION_VALUES)))
        for key in ("only_if", "unless"):
            # A soft rule's unless is prose for the model, checked in its own branch below. Walking
            # the string here read it one character at a time and called each character an unknown
            # condition, which is how a well-formed rule came back as two pages of findings.
            if kind == "soft":
                continue
            for name in sorted(rule.get(key) or {}):
                if name == "computed" and rule[key][name] not in COMPUTED_FLAGS:
                    problems.append("%s: %s names the computed flag %r; the engine computes %s"
                                    % (where, key, rule[key][name], ", ".join(COMPUTED_FLAGS)))
                elif name not in CONDITION_KEYS:
                    problems.append("%s: %s holds the unknown condition key %r; the engine reads %s"
                                    % (where, key, name, ", ".join(CONDITION_KEYS)))
        result = rule.get("result")
        if kind not in ("hard", "soft"):
            problems.append("%s: unknown kind %r; the engine knows hard, soft" % (where, kind))
        if kind == "soft":
            # A soft rule's logic is prose, not a verdict, so the two are checked against different
            # vocabularies. Accepting a verdict here would let a deny rule be silently filed as a
            # sentence nobody reads.
            if not str(result or "").strip():
                problems.append("%s: a soft rule needs result, the sentence to inject" % where)
            if not comma_list((rule.get("if") or {}).get("occasion", "")):
                problems.append("%s: a soft rule needs if.occasion; without one it matches no moment "
                                "and reload writes nothing, so the rule is on and does nothing"
                                % where)
        elif result is not None and result not in ACTION_VALUES:
            problems.append("%s: unknown result %r; the engine knows %s"
                            % (where, result, ", ".join(ACTION_VALUES)))
        for name, value in sorted((rule.get("if") or {}).items()):
            if kind == "soft":
                if name == "occasion":
                    for moment in comma_list(value):
                        if moment not in SOFT_OCCASIONS:
                            problems.append("%s: unknown occasion %r; the engine knows %s"
                                            % (where, moment, ", ".join(SOFT_OCCASIONS)))
                    continue
                problems.append("%s: a soft rule is keyed on occasion, not on %r; a soft rule fires "
                                "at a moment in the conversation, where no tool is being called"
                                % (where, name))
            elif name in FLAT_CONDITIONS or name in FLAT_VALUE_CONDITIONS or name in FLAT_FACT_KEYS:
                continue
            else:
                problems.append("%s: rule.if holds the unknown condition %r" % (where, name))
        if kind == "soft" and "unless" in rule and not isinstance(rule["unless"], str):
            problems.append("%s: a soft rule's unless is the exception in the human's own words, so "
                            "it is a sentence, not a condition table - no code can evaluate it for "
                            "the model" % where)
    return [("policy", text) for text in problems]


def policy_notices(policy, warnings):
    """The (kind, text) pairs a hook turn should carry, so a policy problem is never invisible."""
    return list(warnings) + policy_findings(policy)


def notices_text(policy, warnings):
    notices = policy_notices(policy, warnings)
    if not notices:
        return ""
    return "\n" + "\n".join("OCF [%s] %s" % (kind, text) for kind, text in notices)


# ---------------------------------------------------------------------------
# Generated instructions
# ---------------------------------------------------------------------------
#
# The other half of the rule set. A hard rule is read by this program, outside the conversation, and
# its answer is a verdict. A soft rule cannot be enforced that way - nothing can check whether a
# sentence was written - so it is written INTO the file the model reads on every turn, in the part of
# it marked off below. That is the whole mechanism: the human edits a rule in policy.toml, runs
# `reload`, and the text in front of the model changes. The file is protected from the machine, so the
# model cannot edit the rules it is being asked to follow.

INSTRUCTIONS_REL = ".github/copilot-instructions.md"
INSTRUCTIONS_BEGIN = "<!-- OCF:GENERATED -->"
INSTRUCTIONS_END = "<!-- OCF:END -->"

# The policy file is hand-written except for the region holding the vocabulary, which is rendered from
# the tables below. Its markers are TOML comments rather than HTML ones, which is the only way in which
# the two generated regions differ.
VOCAB_BEGIN = "# OCF:VOCABULARY:BEGIN"
VOCAB_END = "# OCF:VOCABULARY:END"

# The rules document is hand-written prose with one generated region in the middle: the part of it that
# is computable from the tables. Markdown markers, because that is what the file is.
REFERENCE_REL = ".github/work-control-flow.md"
REFERENCE_BEGIN = "<!-- OCF:REFERENCE:BEGIN -->"
REFERENCE_END = "<!-- OCF:REFERENCE:END -->"


def soft_rules(policy):
    return [rule for rule in policy.get("rule", [])
            if str(rule.get("kind", "hard")).strip().lower() == "soft"]


def one_line(text):
    """Collapse a rule's text onto one line, telling a wrap apart from a space the human wrote.

    Two different things look alike in a multi-line TOML string. A bare line break is where the human
    wrapped the source, so joining on a space put one inside a clause that has no spaces in it at all -
    which is every clause in Chinese. Whitespace the human added beyond the break is deliberate, and
    dropping it ran Latin words into the characters around them. So: a bare line break joins, unless
    both sides are ASCII word characters, and any indent or trailing space is kept as one space - which
    is how a path on its own line ends up separated from the word before it.
    """
    parts = [part for part in re.split(r"(\s+)", str(text)) if part]
    out = ""
    for index, part in enumerate(parts):
        if part.isspace():
            leftover = part.replace("\r", "").replace("\n", "")
            if "\n" not in part and "\r" not in part:
                out += " "
                continue
            if leftover:
                out += " "
                continue
            following = parts[index + 1] if index + 1 < len(parts) else ""
            if out and out[-1].isascii() and out[-1].isalnum() \
                    and following[:1].isascii() and following[:1].isalnum():
                out += " "
            continue
        out += part
    return out.strip()


def render_instructions(policy):
    """Render the whole standing contract. Pure: same policy in, same text out, so it can be compared.

    The whole document, not a fragment spliced into prose. It was prose with a generated paragraph in
    the middle, and the prose went stale within one stage: it still listed the human-only commands
    without `reload`, still told the machine not to hand-edit two files that had been deleted, and
    still described three behaviours that are now rules. None of that was checkable, because a
    sentence is not compared to anything. Everything derivable is rendered from the table it derives
    from, and everything that is genuinely a rule is a rule.
    """
    lines = ["<!-- Written by `python .github/ocf/ocf.py reload` from the tables in this program and",
             "     the rules in .github/ocf/policy.toml. Do not edit by hand - the next reload",
             "     overwrites everything above the OCF:END marker. Edit the rules in that file and",
             "     run reload. Anything you add below the marker is yours and survives. -->",
             "",
             "# Work Control Flow",
             "",
             "Full rules: [work-control-flow.md](./work-control-flow.md). Read it before first acting "
             "and whenever your attention begins to drift.",
             "",
             "## Entry point",
             "",
             "    Windows: python .github\\ocf\\ocf.py <cmd>",
             "    Other:   python3 .github/ocf/ocf.py <cmd>",
             "",
             "## Commands",
             ""]
    indent = " " * len("human: ")
    for side, groups in (("agent", TRANSITIONS["commands"]["agent"]),
                         ("human", TRANSITIONS["commands"]["human"])):
        for index, group in enumerate(groups):
            lines.append("%s%s" % (side + ": " if index == 0 else indent, " | ".join(group)))
    lines += ["",
              "A human-only command is refused even if the human asks you in the conversation to run "
              "it. \"The human already said yes\" is not approval; approval is the human running the "
              "command in their own terminal. If they want it to stop, they turn the rule off or edit "
              "the config - they do not authorise it by asking.",
              "",
              "Never hand-edit the files under %s. The protected list is %s; only the human changes "
              "either." % (state_dir_relative(policy), PROTECTED_LIST_REL),
              "",
              "## State machine",
              "",
              "    %s      bypass: %s" % (" -> ".join(TRANSITIONS["flow"]), TRANSITIONS["bypass"]),
              "",
              "Entering a state through a gate means the gate passed. What each one requires:",
              ""]
    for (source, target), names in sorted(TRANSITIONS["gates"].items()):
        lines.append("- %s -> %s: %s" % (source, target, ", ".join(names)))
    lines += ["",
              "Only the human, running `python .github/ocf/ocf.py approve \"<reason>\"` in their own "
              "terminal, enters executing. Never run it for them and never set approved_by yourself.",
              ""]
    rules = soft_rules(policy)
    lines += ["## Soft rules",
              "",
              "No hook can enforce these: nothing can check whether a sentence was written. They are "
              "put in front of you instead. Each one names the moment it applies to.",
              ""]
    written = 0
    for occasion in SOFT_OCCASIONS:
        group = [rule for rule in rules
                 if as_bool(rule.get("enabled", True))
                 and occasion in comma_list((rule.get("if") or {}).get("occasion", ""))]
        if not group:
            continue
        lines.append("### %s" % occasion)
        lines.append("")
        for rule in group:
            label = str(rule.get("label") or rule.get("id") or "rule").strip()
            lines.append("- **%s** - %s" % (label, one_line(rule.get("result", ""))))
            exception = rule.get("unless")
            if isinstance(exception, str) and exception.strip():
                lines.append("  - Exception, judged by you: %s" % one_line(exception))
        lines.append("")
        written += len(group)
    if not written:
        lines += ["No soft rule is switched on, so there is nothing to say here.", ""]
    lines += ["## When a gate blocks you",
              "",
              "Fix the precondition it names: supply the missing fact, split the command, stop "
              "silencing output, go ask the human. Never rewrite your way around it. Circumventing a "
              "gate is a serious violation."]
    return "\n".join(lines)


def state_dir_relative(policy):
    return policy_paths(policy)["state_dir"].replace("\\", "/") + "/"


def hook_enabled(policy, event):
    """Whether this event should do anything at all.

    Two switches, both the human's: a master one, and one per event. The master is what "uninstall"
    means - reload then writes a wiring file with no hooks in it, so VS Code stops starting this
    program rather than merely ignoring its answers, and there is nothing left behind to switch off
    later. The check lives here as well as in the wiring file because the wiring file is only re-read
    when the window reloads.
    """
    settings = policy.get("hooks", {})
    if not as_bool(settings.get("enabled", True)):
        return False
    for switch, name, _ in HOOK_EVENTS:
        if name == event:
            return as_bool(settings.get(switch, True))
    return False


def render_hooks_json(policy):
    """Render the wiring file from the switches. Pure, so the file on disk can be compared to it."""
    body = {}
    for switch, event, timeout in HOOK_EVENTS:
        if not hook_enabled(policy, event):
            continue
        body[event] = [{"type": "command",
                        "command": "python3 %s %s" % (ENTRY_REL, HOOK_SUBCOMMAND),
                        "windows": "python %s %s" % (ENTRY_REL, HOOK_SUBCOMMAND),
                        "timeout": timeout}]
    return json.dumps({"hooks": body}, indent=2) + "\n"


def render_reference(policy):
    """Render the derived half of the rules document: what the gate actually does.

    Everything here is computable from the tables and the rules, so none of it should be hand-copied:
    the hand-copied version of exactly this list had the gate named `protected-file` (a rule id) and
    told the reader that plan-schema wants eight sections (it wants two). A reference that is rendered
    cannot be wrong about the thing it is rendered from, and where it disagrees with a human's
    expectation, the rendering is right and the expectation is the thing to fix.
    """
    lines = ["### Gates", "",
             "A gate is a precondition of a state change, not a rule about tool calls. Failing one "
             "refuses the transition and names what is missing.", ""]
    for (source, target), names in sorted(TRANSITIONS["gates"].items()):
        lines.append("`%s -> %s`" % (source, target))
        for name in names:
            lines.append("- `%s` - %s" % (name, GATE_HELP.get(name, "MISSING DESCRIPTION")))
        lines.append("")
    lines += ["### Tool classes", "",
              "The class decides which rules are consulted and how a tool is judged. Membership is "
              "here rather than in prose because prose drifts; a tool not listed is `unknown`.", ""]
    for name in CLASS_ORDER:
        members = policy.get("tools", {}).get(name, [])
        lines.append("- `%s`%s: %s" % (name,
                                       " (never gated: that is what the class means)"
                                       if name in ("read", "session") else "",
                                       ", ".join("`%s`" % item for item in members) or "(none)"))
    lines += ["",
              "Classes are consulted strictest first, so a tool listed in two of them is judged by the "
              "more restrictive one. `visual` is first: a tool that reads an image stays refused even "
              "if it also appears as a reader. A tool no class lists is `unknown`, and is judged by "
              "what it carries - a command makes it an exec tool - and otherwise by whether its name "
              "looks like an action.", "",
              "### Rules", "",
              "In file order, first match wins. The conditions are named rather than quoted: the "
              "values live in `.github/ocf/policy.toml`, which is where they are meant to be read and "
              "changed.", ""]
    for rule in policy.get("rule", []):
        identity = rule.get("id", "unnamed")
        kind = str(rule.get("kind", "hard")).strip().lower()
        outcome = rule.get("result") if kind == "soft" else (rule.get("result") or rule.get("action"))
        if kind == "soft":
            lines.append("- `%s` (soft, occasion `%s`) - %s"
                         % (identity,
                            ", ".join(comma_list((rule.get("if") or {}).get("occasion", ""))) or "none",
                            one_line(rule.get("why", ""))))
            continue
        conditions = sorted(set(rule.get("if") or {}) | set(rule.get("only_if") or {})
                            | ({"when=" + str(rule["when"])} if rule.get("when") else set())
                            | ({"unless"} if rule.get("unless") else set()))
        lines.append("- `%s`%s -> `%s` - %s%s"
                     % (identity,
                        "" if as_bool(rule.get("enabled", True)) else " (switched off)",
                        outcome,
                        one_line(rule.get("why", "")),
                        " [%s]" % ", ".join(conditions) if conditions else ""))
    return "\n".join(lines)


def sync_generated(root, relative, rendered, markers=None):
    """Make a generated file match what the renderer produced, and report what happened.

    One implementation for every generated file, because there were two and they had already diverged:
    one replaced a marked region and preserved everything outside it, the other rewrote the whole file,
    and the whole-file one had lost the subcommand from every hook it wrote - a wiring file that starts
    the program without telling it to be a hook, which the editor reads as "no objection". Everything
    that is easy to get wrong is the same either way: line endings, a half-deleted marker pair, and
    telling "already current" apart from "rewritten".

    `markers` is a (begin, end) pair for a region inside a hand-written file, or None for a file this
    program owns outright. In the first case the text outside the markers is the human's and survives.
    A file missing one of the two markers is refused rather than guessed at: guessing where a
    half-deleted region used to end would eat whatever followed it.
    """
    path = os.path.join(root, relative)
    text = read_text(path)
    if text is None:
        raise OcfError("policy", "%s does not exist" % relative)
    if markers is None:
        if text == rendered:
            return "%s already matches" % relative
        write_text(path, rendered)
        return "rewrote %s" % relative
    begin, end = markers
    block = "%s\n%s\n%s" % (begin, rendered, end)
    start = text.find(begin)
    if start < 0:
        if text.find(end) >= 0:
            raise OcfError("policy", "%s has the end marker but not the begin marker; refusing to "
                                     "guess where the generated region starts" % relative)
        separator = "\n\n" if text.strip() else ""
        write_text(path, text.rstrip("\n") + separator + block + "\n")
        return "appended the generated region to %s" % relative
    stop = text.find(end, start)
    if stop < 0:
        raise OcfError("policy", "%s has the begin marker but not the end marker; refusing to guess "
                                 "where the generated region stops" % relative)
    updated = text[:start] + block + text[stop + len(end):]
    if updated == text:
        return "%s already matches" % relative
    write_text(path, updated)
    return "rewrote the generated region in %s" % relative


def write_hooks(root, policy):
    reported = sync_generated(root, HOOKS_REL, render_hooks_json(policy))
    if "already matches" in reported:
        return reported
    off = [name for switch, name, _ in HOOK_EVENTS if not hook_enabled(policy, name)]
    if not hook_enabled(policy, "PreToolUse"):
        return ("%s. The gate is not installed now, so nothing is refused. Reload the VS Code window "
                "for it to take effect: hooks are read when the window starts." % reported)
    return ("%s (%s off). Reload the VS Code window for it to take effect: hooks are read when the "
            "window starts and are not re-read afterwards."
            % (reported, ", ".join(off) if off else "no event"))


def write_instructions(root, policy):
    return sync_generated(root, INSTRUCTIONS_REL, render_instructions(policy),
                          markers=(INSTRUCTIONS_BEGIN, INSTRUCTIONS_END))


def write_vocabulary(root, policy):
    return sync_generated(root, POLICY_REL, render_vocabulary(),
                          markers=(VOCAB_BEGIN, VOCAB_END))


def write_reference(root, policy):
    return sync_generated(root, REFERENCE_REL, render_reference(policy),
                          markers=(REFERENCE_BEGIN, REFERENCE_END))


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
# The protected list. Entries are paths or globs; '#' starts a comment. Read by listed_in, which is
# the one place a path is judged against it, for both the editing tools and the terminal.
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


def human_only_call(command):
    """True when the command runs the entry script WITH a human-only subcommand.

    The subcommand is the argument that follows the script path - what invoking the command means -
    and not any of those words appearing anywhere in the text. Judging the words anywhere matched
    the FILENAME `.orchestrator/human-code.txt` in an ordinary cleanup and denied it, which is the
    same class of error as the allow-rule hole fixed earlier: a rule reading text the human did not
    aim at it. The vocabulary comes from the human side of the command table, so the rule and the
    CLI cannot come to disagree about which commands are human-only.
    """
    if not command:
        return False
    words = set(word for group in TRANSITIONS["commands"]["human"] for word in group)
    for part in statements(command):
        match = re.search(r"(?i)" + ENTRY_PAT + r"[\"']?\s+([^\s;|&]+)", part)
        if match and match.group(1).strip("\"'").lower() in words:
            return True
    return False


def computed_flags(command):
    if not command:
        return {"control_plane": False, "invokes_entry": False, "human_only_call": False}
    parts = statements(command)
    control_plane = bool(parts) and all(CP_STMT_RE.match(part) for part in parts)
    return {"control_plane": control_plane,
            "invokes_entry": ENTRY_RE.search(command) is not None,
            "human_only_call": human_only_call(command)}


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

# What a tool is, in the order the classes are consulted. There used to be an `action` class, which
# was not a category but a leftover bin for "acts, but is neither an edit nor a command", and an
# `always_allow` class, which was a verdict wearing a category's name - a tool parked there said
# nothing about what it was, only about what the gate would answer. Both are gone: every tool now has a
# class that names what it does, and the verdict follows from that.
#
# The order is strictest first, and the first class that lists the tool wins. That is deliberate: a
# tool accidentally listed in two classes is judged by the more restrictive one. `visual` is first so
# that a tool which merely reads an image stays refused even if it also appears as a reader.
#
#   visual   read an image or drive a browser. Refused unconditionally: the machine never looks.
#   env      set up or change the toolchain and the environment: install, configure, debug, scaffold.
#   exec     carries a command string, which is then inspected.
#   write    changes text: files, notebooks, remote files.
#   read     only reads. Never gated, so a broken policy cannot blind whoever has to repair it.
#   session  shapes the conversation and cannot touch the repository: todos, questions, subagents,
#            memory. Never gated. A subagent's own tool calls are judged separately, so dispatching
#            one is not a way round the gate.
# `unknown` is the fallback, not a member of this tuple: a tool no class claims is judged by what it
# carries and, failing that, by whether its name looks like an action.
CLASS_ORDER = ("visual", "env", "exec", "write", "read", "session")


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
        self.tool = payload.get("tool_name") or ""
        self.klass = tool_class(policy, self.tool)
        self.command = extract_command(payload, policy, self.tool)
        self.raw_json = json.dumps(payload, ensure_ascii=True, sort_keys=True)
        self.content = "\n".join(collect_content(payload))
        self.computed = computed_flags(self.command)
        # Gathered here rather than in computed_flags, which sees only the command: this one is about the
        # policy and the state.
        self.computed["enforced"] = self.enabled
        self.paths = [norm_path(root, item) for item in collect_paths(payload.get("tool_input") or {})]
        self.paths = [item for item in self.paths if item]
        self.targets = write_targets(root, self.command)
        self.infos = []
        self.report = []
        if not self.paths and self.targets:
            self.infos.append("paths inferred from the command: %s" % ", ".join(self.targets[:5]))
        # An unclassified tool that carries a command must be judged as if it were an exec tool,
        # otherwise a tool added later could run uninspected. A tool that only carries a path is left
        # alone, because read tools use the same field and must not be dragged into write rules.
        # Action-class tools get the same treatment: create_and_run_task carries its command at
        # task.command, and without this the visual ban and the approval rule would not apply to it
        # once the state is executing.
        self.effective = self.klass
        if self.command and self.klass in ("env", "unknown"):
            self.effective = "exec"

    def state_line(self):
        return "state=%s enabled=%s approval_required=%s" % (
            self.state, "on" if self.enabled else "off", "yes" if self.needs_approval else "no")

    def approval_hint(self):
        return ("The human runs this in their own terminal:\n"
                "  python .github/ocf/ocf.py approve \"<one-sentence reason>\"")

    def changed_paths(self):
        """The paths this action would change, as opposed to the ones it merely mentions.

        An editing tool changes the file it was given. A command changes whatever its write target was
        inferred to be - and only a write-ish command has one, so a command that does not write has
        nothing to judge and no path rule applies to it. This distinction is load-bearing: the rule it
        serves is "nothing may MODIFY these paths", and judging every path the text mentions instead
        meant `python .github/ocf/ocf.py status` was refused for naming the entry script, which is the
        one command that has to keep working. It was added in this order deliberately - the method
        first, the call second - because the other order leaves the gate raising on every tool call,
        which its own fail-safe turns into a denial of everything, including the fix.
        """
        if self.klass == "write":
            return list(self.paths)
        return list(self.targets)

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
        # An unknown surface used to return nothing, which made the rule skip silently - the same
        # fail-open shape as an unknown `on` class. A rule that cannot see what it was told to look at
        # is a rule that is not running.
        raise OcfError("policy", "unknown surface %r; the engine knows %s"
                                 % (name, ", ".join(SURFACE_NAMES)))


def condition_holds(context, condition):
    """Evaluate a context-scoped condition. Value-scoped ones are handled by filter_condition."""
    if "fact" in condition:
        name = condition["fact"]
        if "is_set" in condition:
            present = bool(context.facts.get(name, "").strip())
            return present == as_bool(condition["is_set"])
        if "is_not" in condition:
            return context.facts.get(name, "") != str(condition["is_not"])
        return context.facts.get(name, "") == str(condition.get("is", ""))
    if "content_matches" in condition:
        return re.search(condition["content_matches"], context.raw_json + "\n" + context.content,
                         re.I | re.S) is not None
    if "computed" in condition:
        return bool(context.computed.get(condition["computed"]))
    raise OcfError("policy", "unknown condition key(s): %s" % ", ".join(sorted(condition)))


def when_holds(context, when):
    """Whether the older `when` gate holds. An unknown value raises rather than not firing.

    Not firing is the quiet answer, and for the self-protection rules it is the wrong one: the rule
    would stop governing while the file it guards stays editable. Raising sends the call through the
    hook's fail-safe, which denies and prints the value that was not understood.
    """
    if when in (None, "always"):
        return True
    if when == "enforced":
        return context.enabled
    raise OcfError("policy", "unknown when %r; the engine knows %s. The approval rules are written "
                             "as `approval = true` inside rule.if now."
                             % (when, ", ".join(WHEN_VALUES)))


def filter_condition(context, condition, values, keep):
    """Return the candidate values that survive one condition.

    Conditions split in two. Context-scoped ones are true or false for the call as a whole.
    Value-scoped ones must be judged per candidate, because the rule is asking "is THIS path the one I
    care about": judging them as a union of the batch makes one qualifying path decide the fate of
    every other path, which both over-blocks and names a target that is not the offender.

    `keep` is True for only_if and False for unless, so the two forms share one implementation rather
    than two that must be kept in step by hand.
    """
    context_part = {key: value for key, value in condition.items()
                    if key not in VALUE_SCOPED_CONDITIONS}
    if context_part and keep != bool(condition_holds(context, context_part)):
        return []
    survivors = list(values)
    for name, spec in condition.items():
        test = VALUE_SCOPED_TESTS.get(name)
        if test is None:
            continue
        survivors = [value for value in survivors if keep == bool(test(context, spec, value))]
        if not survivors:
            return []
    return survivors


# ---------------------------------------------------------------------------
# The newer rule shape: one condition block, one result
# ---------------------------------------------------------------------------
#
# A rule reads as one sentence: when every condition in [rule.if] holds, do `result`. There is no
# second layer - no on/when/surface/match chain, no hit/skip split, no unless wrapper - because "when
# it must not fire" is just another condition, named so that it reads that way.
#
# No condition needs a `surface` field either. One action carries several pieces of text (the tool
# name, the command, every path it touches, the content it would write), so each condition names the
# piece it measures: the command_* conditions measure the command, the path_* conditions the paths.

# The protected list is a fixed file, not a per-rule path: a rule that names the list it checks would
# let the list be moved out from under it. It lives under .github/ rather than the gitignored runtime
# directory on purpose - a list that is not versioned is absent on a fresh clone, and an absent list
# protects nothing while every rule still reads as if it did. The list is itself listed in its own
# entries, so the machine cannot empty it.
PROTECTED_LIST_REL = ".github/protected.txt"


def comma_list(value):
    return [item.strip() for item in str(value).split(",") if item.strip()]


def as_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "yes", "1", "on")


def class_condition(context, value):
    """Whether the tool's effective class is one of these. An unknown name raises.

    A class the engine does not know used to compare unequal and return quietly. So a rule whose class
    was misspelled - or renamed in the engine and not in the policy - simply stopped applying, and for
    the rules that protect the orchestrator's own files and the protected list that is a silent hole.
    That is how this was found: `edit` became `write` here, and `protected-file` quietly went from
    refusing to allowing while every other test stayed green.
    """
    wanted = comma_list(value)
    unknown = [name for name in wanted if name not in CLASS_ORDER + ("unknown",)]
    if unknown:
        raise OcfError("policy", "unknown class %r; the engine knows %s"
                                 % (unknown[0], ", ".join(CLASS_ORDER + ("unknown",))))
    return context.effective in wanted


# Judged once for the whole call.
FLAT_CONDITIONS = {
    "class": class_condition,
    "state": lambda context, value: context.state in comma_list(value),
    "tool": lambda context, value: context.tool in comma_list(value),
    "command_matches": lambda context, value:
        bool(context.command) and re.search(value, context.command, re.I | re.S) is not None,
    "command_length_over": lambda context, value: len(context.command) > int(value),
    "command_statements_over": lambda context, value: len(statements(context.command)) > int(value),
    "command_repeats_at_least": lambda context, value:
        command_repeat_count(context.root, context.policy, context.command) >= int(value),
    "content_matches": lambda context, value:
        re.search(value, context.raw_json + "\n" + context.content, re.I | re.S) is not None,
    "environment_declared": lambda context, value:
        bool(context.facts.get("stack_env", "").strip()) == as_bool(value),
    "approval": lambda context, value: as_bool(value) == context.needs_approval,
    "computed": lambda context, value: bool(context.computed.get(value)),
}

# Judged against each candidate value the action carries, so one qualifying path cannot decide the
# fate of every other path in the same batch.
FLAT_VALUE_CONDITIONS = {
    "path_matches": lambda context, value, candidate: re.search(value, candidate, re.I) is not None,
    "touches_protected": lambda context, value, candidate:
        as_bool(value) == listed_in(context.root, PROTECTED_LIST_REL, candidate),
}

# Value-scoped conditions judge the paths an action would CHANGE, never the whole text it carries and
# never every path it mentions. Two separate over-blocks came from getting this wrong: reading them off
# "any" let a path that merely appeared in the content decide the verdict, which for an allow rule is a
# hole rather than a nuisance; and reading them off every inferred path meant a command that MENTIONS a
# protected file was refused, so `python .github/ocf/ocf.py status` - which names the entry script on
# every call - would have stopped the gate being read at all. Reading a human's file is not changing it.
VALUE_CANDIDATE_SOURCE = {
    "path_matches": lambda context: context.changed_paths(),
    "touches_protected": lambda context: context.changed_paths(),
}

FLAT_FACT_KEYS = ("fact", "is", "is_not", "is_set")


def evaluate_conditions(context, rule, conditions):
    """Return the matching candidates when every condition holds, else None.

    One evaluator for both halves of a rule. `unless` is the same question asked with the opposite
    answer, so a second implementation of "do these conditions hold" is exactly the way the two drift
    apart - and they had: `unless` was read by the older rule shape and ignored by this one, so a rule
    moved to the newer shape silently lost its exception.
    """
    fact_part = {key: conditions[key] for key in FLAT_FACT_KEYS if key in conditions}
    if fact_part and not condition_holds(context, fact_part):
        return None
    value_conditions = [name for name in conditions if name in FLAT_VALUE_CONDITIONS]
    pool = []
    for name in value_conditions:
        source = VALUE_CANDIDATE_SOURCE.get(name)
        for value in (source(context) if source else context.surface("any")):
            if value not in pool:
                pool.append(value)
    candidates = []
    for value in (pool if value_conditions else context.surface("any")):
        if all(FLAT_VALUE_CONDITIONS[name](context, conditions[name], value)
               for name in value_conditions):
            candidates.append(value)
    if value_conditions and not candidates:
        return None
    for name, spec in conditions.items():
        if name in FLAT_FACT_KEYS or name in FLAT_VALUE_CONDITIONS:
            continue
        test = FLAT_CONDITIONS.get(name)
        if test is None:
            raise OcfError("policy", "rule %r: unknown condition %r" % (rule.get("id"), name))
        if not test(context, spec):
            return None
    return candidates or [context.tool]


def evaluate_rule_v2(context, rule):
    """Evaluate a rule written in the newer shape, and return a verdict dict when it fires.

    Soft rules are never evaluated: they are guidance for the model and live in the instructions the
    hook injects, so the gate has no verdict to give about them.
    """
    if not as_bool(rule.get("enabled", True)):
        return None
    if str(rule.get("kind", "hard")).strip().lower() == "soft":
        return None
    candidates = evaluate_conditions(context, rule, rule.get("if") or {})
    if candidates is None:
        return None
    exception = rule.get("unless")
    if isinstance(exception, dict) and exception:
        if evaluate_conditions(context, rule, exception) is not None:
            return None
    return {"id": rule.get("id", "unnamed"), "action": rule.get("result", "deny"),
            "why": rule.get("why", ""), "hit": candidates[0], "rule": rule}


def evaluate_rule(context, rule):
    """Return a verdict dict when the rule fires, else None.

    Conditions split in two. Context-scoped ones (fact, computed, content_matches and friends) are true
    or false for the call as a whole. Value-scoped ones (listed_in) must be judged
    per candidate value, because a rule is asking "is THIS path the one I care about". Judging them as
    a union of the whole batch makes one qualifying path decide the fate of every other path, which
    both over-blocks and reports a target that is not the offender.

    A rule in the newer shape ([rule.if] plus result/kind) is handed to evaluate_rule_v2. Both shapes
    are accepted on purpose: the engine must understand at least as much vocabulary as the policy file
    uses, because editing the two in sequence - code first, policy second - is exactly how the gate
    locked itself out on itself once already.
    """
    # A soft rule is not gated here at all, in either shape. Checked before the shape dispatch so a
    # soft rule can never be read as a verdict by the older path - one sentence of prose is not an
    # `action`, and defaulting it would turn "explain your terms" into "deny".
    if str(rule.get("kind", "hard")).strip().lower() == "soft":
        return None
    if "if" in rule or "result" in rule:
        return evaluate_rule_v2(context, rule)
    targets = rule.get("on", "any")
    if targets != "any":
        wanted = comma_list(targets)
        unknown = [name for name in wanted if name not in CLASS_ORDER + ("unknown",)]
        if unknown:
            # Not matching is the quiet answer, and for the self-protection rules it is the wrong one:
            # the rule would stop governing while the file it guards stays editable. Raising sends the
            # call through the hook's fail-safe, which denies and prints the name it did not understand.
            raise OcfError("policy", "rule %r applies to the unknown class %r; the engine knows %s"
                                     % (rule.get("id"), unknown[0],
                                        ", ".join(CLASS_ORDER + ("unknown",))))
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
    # only_if and unless are the same value-scoped test asked with opposite polarity, so they become
    # one list of (condition, keep) and run through one evaluator. Two implementations of "is this
    # candidate excused" is exactly how they drift apart.
    conditions = []
    for key, keep in (("only_if", True), ("unless", False)):
        if rule.get(key):
            conditions.append((rule[key], keep))
    for condition, keep in conditions:
        hits = filter_condition(context, condition, hits, keep)
        if not hits:
            return None
    return {"id": rule.get("id", "unnamed"), "action": rule.get("action", "deny"),
            "why": rule.get("why", ""), "hit": hits[0], "rule": rule}


def decide(context):
    """Return (action, rule_id, reason). First matching rule wins.

    Every verdict names the rule that produced it, allow included. Without that, "why was this
    allowed" cannot be answered afterwards, which is the question that matters most in an audit.
    """
    if context.klass == "session":
        return ("allow", "session",
                "[session] This tool shapes the conversation and cannot touch the repository, so it is "
                "never gated. A subagent's own tool calls are judged separately.")
    if context.klass == "read":
        return ("allow", "read",
                "[read] This tool only reads, so no rule applies to it. Reading stays open even "
                "under the strict fallback policy, because a gate that cannot be read is a gate nobody "
                "can repair.")
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


def emit_posttool_context(text):
    """PostToolUse takes additionalContext as a list, unlike SessionStart which takes a string.

    Read off the extension's own consumers: the SessionStart path calls .substring() on the value,
    while the PostToolUse path iterates it. The wrong shape either does nothing or, worse, pushes one
    message per character, so the two emitters deliberately differ.
    """
    payload = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": [text]}}
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
    if not hook_enabled(policy, event):
        # Switched off is silent, not "allowed": no output means no decision to make, and the events
        # this program does not read at all land here too.
        return 0
    try:
        if event == "PreToolUse":
            action, rule_id, reason, context = check_pretooluse(root, policy, payload)
            if action != "allow":
                reason = "\n".join([reason] + context.infos)
            emit_pretooluse(action, reason)
            return 0
        if event == "UserPromptSubmit":
            text = handle_prompt(root, policy, payload) + notices_text(policy, warnings)
            emit_context("UserPromptSubmit", text)
            return 0
        if event in ("SessionStart", "SessionStartReload"):
            text = handle_session_start(root, policy) + notices_text(policy, warnings)
            emit_context("SessionStart", text)
            return 0
        if event == "PostToolUse":
            text = handle_post_tool(root, policy, payload)
            if text:
                emit_posttool_context(text)
            return 0
        if event in ("Stop", "SubagentStop", "PreCompact", "SubagentStart"):
            return 0
        return 0
    except OcfError as exc:
        if event == "PreToolUse":
            emit_pretooluse("deny", "Blocked by the orchestrator: %s" % exc.tagged())
        elif event != "PostToolUse":
            out(exc.tagged())
        return 0
    except Exception as exc:  # fail safe: an internal fault denies rather than allows
        if event == "PreToolUse":
            emit_pretooluse("deny", "[system] internal error in the gate, denied to stay safe: %r" % (exc,))
        return 0


# A command that never reports a result has no place in a workflow whose rule is "visible and
# traceable". These are the shapes of "the shell is still waiting", taken from a command that hung in
# a continuation prompt: quoting that does not close makes a shell wait for ever, and no text rule can
# see that coming, because the gate reads the command and cannot parse the shell's grammar. What it can
# do is notice the aftermath.
NO_PROGRESS_PATTERNS = (
    (re.compile(r"(?m)^\s*>>\s*$"),
     "a shell is sitting at a continuation prompt, so the command never ran to completion"),
    (re.compile(r"(?i)moved to the background"),
     "the command was moved to the background, so it has not reported a result"),
    (re.compile(r"(?i)waiting for (your )?input"),
     "the command is waiting for input"),
)


def handle_post_tool(root, policy, payload):
    """Return a warning to inject when a command produced no progress, else None."""
    tool = payload.get("tool_name") or ""
    if tool_class(policy, tool) != "exec":
        return None
    response = payload.get("tool_response")
    if not isinstance(response, str):
        response = "" if response is None else str(response)
    for pattern, why in NO_PROGRESS_PATTERNS:
        if pattern.search(response):
            journal(root, policy, read_state(root, policy), "no-progress command: %s" % why)
            return ("OCF: the previous command did not report progress, because %s. Do not treat it as a "
                    "completed success. Split the command, or record it with ocf.py fail \"<reason>\" and "
                    "tell the human what you tried." % why)
    return None


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
        outstanding = context_missing(root, policy)
        if outstanding:
            lines.append("Intake items not derivable from context: %s. Take each one from the human's "
                         "own statement if it already says so, and grill only what remains, one "
                         "question per turn. Never guess a value." % ", ".join(outstanding))
    return "\n".join(lines)


def handle_session_start(root, policy):
    lines = ["OCF self-check (classified):"]
    for kind, text in run_selftest(root, policy):
        lines.append("  [%s] %s" % (kind, text))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

# The intake key list and its threshold live here, once. gate_context and the status line both report
# from this, instead of each holding its own copy of the list to drift away from.
CONTEXT_KEYS = ("goal", "tools", "references", "deliverables", "code_style")
CONTEXT_MIN_LEN = 4


def context_missing(root, policy):
    """Return the intake keys that are missing or too short, in list order.

    This is the structured form: a caller that wants to know *which* items are outstanding does not
    have to recover them from the sentence gate_context prints.
    """
    facts = load_facts(root, policy)
    return [key for key in CONTEXT_KEYS if len(facts.get(key, "").strip()) < CONTEXT_MIN_LEN]


def gate_context(root, policy):
    missing = context_missing(root, policy)
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
    """A plan given in chat is the default, so an absent file is not a failure.

    The plan is a conversation artefact and a file is written only when the human asks for one. What is
    checked here is therefore only what such a file must carry when it exists, and only because a gate
    reads it: Files is compared against the protected list by protected-list-clear.
    """
    text = read_plan(root)
    if not text.strip():
        return True, "no plan file; the plan was given in chat"
    sections = plan_sections(text)
    missing = [name for name in PLAN_REQUIRED if name not in sections]
    empty = [name for name in PLAN_REQUIRED
             if name in sections and not "".join(sections[name]).strip()]
    problems = []
    if missing:
        problems.append("missing headings: %s" % ", ".join("## " + item for item in missing))
    if empty:
        problems.append("empty headings: %s" % ", ".join("## " + item for item in empty))
    return (not problems), "; ".join(problems) if problems else "all required sections present"


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


def gate_protected_list_clear(root, policy):
    """The plan must not touch a protected path. There is no exemption to grant: the list is the list."""
    text = read_plan(root)
    if not text.strip():
        return True, "no plan file; the human approves the work itself"
    offenders = [path for path in plan_paths(text)
                 if listed_in(root, PROTECTED_LIST_REL, path)]
    return (not offenders), "touches protected paths: %s" % ", ".join(offenders) if offenders \
        else "none"


def gate_stack_env(root, policy):
    value = load_facts(root, policy).get("stack_env", "").strip()
    return (len(value) >= 4), "stack_env=%r" % value


GATES = {
    "context": gate_context,
    "docs-decision": gate_docs_decision,
    "grill-valid": gate_grill_valid,
    "plan-schema": gate_plan_schema,
    "zero-p0": gate_zero_p0,
    "protected-list-clear": gate_protected_list_clear,
    "stack-env": gate_stack_env,
}


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
    findings.extend(policy_findings(policy))
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


def update_list(root, path, add):
    """Add or remove one entry in the protected list. Reached only from the human's own terminal.

    Comment lines are preserved: the list carries the notes that say which entries are human-written,
    and a command that silently dropped them would lose the one annotation the list has.
    """
    absolute = os.path.join(root, PROTECTED_LIST_REL)
    entries = load_list(root, PROTECTED_LIST_REL)
    notes = [line for line in (read_lines(absolute) or []) if line.strip().startswith("#")]
    path = (path or "").strip()
    if not path:
        raise OcfError("environment", "that command needs a path argument")
    key = norm_path(root, path)
    if add:
        if key not in entries:
            entries.append(key)
        out("protected: %s" % key)
    else:
        entries = [entry for entry in entries if norm_path("", entry) != key]
        out("no longer protected: %s" % key)
    write_text(absolute, "".join(line + "\n" for line in notes + entries))
    journal(root, policy, read_state(root, policy), "%s %s" % (
        "protect" if add else "unprotect", key))
    return 0


def cmd_reload(root, policy, args):
    """Re-read the configuration and make the parts that are generated match it. Human-only.

    Refusing to write anything when the policy is broken is the point: half-applied configuration is
    worse than none, because the file the model reads would then disagree with the rules the gate
    enforces and neither one would say so. Nothing is touched until the whole file has been checked.
    """
    _, warnings = load_policy(root)
    findings = list(warnings) + policy_findings(policy)
    for kind, text in findings:
        out("[%s] %s" % (kind, text))
    if findings:
        out("refusing to reload: the policy did not load cleanly, so nothing was changed")
        return 1
    out(write_instructions(root, policy))
    out(write_vocabulary(root, policy))
    out(write_reference(root, policy))
    out(write_hooks(root, policy))
    rules = soft_rules(policy)
    on = [rule for rule in rules if as_bool(rule.get("enabled", True))]
    out("soft rules: %d of %d switched on" % (len(on), len(rules)))
    journal(root, policy, read_state(root, policy), "reload: configuration re-read")
    return 0


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

# name -> handler. main() dispatches through this, and the structural check asserts that this map and
# the table's "commands" block declare exactly the same vocabulary. A command that exists in one and
# not the other is either unreachable or a name the usage text lies about.
COMMAND_HANDLERS = {
    "status": lambda root, policy, args: cmd_status(root, policy),
    "set": cmd_set,
    "gate": cmd_gate,
    "journal": cmd_journal,
    "fail": cmd_fail,
    "ok": cmd_ok,
    "advance": cmd_advance,
    "init": lambda root, policy, args: cmd_init(root, policy),
    "selftest": cmd_selftest,
    "approve": cmd_approve,
    "reject": cmd_reject,
    "confirm": cmd_confirm,
    "reload": cmd_reload,
    "allow": lambda root, policy, args: update_list(root, args[0] if args else "", False),
    "deny": lambda root, policy, args: update_list(root, args[0] if args else "", True),
}


def render_usage():
    """Render the usage text from the table, so the command list has one owner.

    Written out instead of typed a second time: a usage line that names a command the dispatcher does
    not have, or an advance target the machine does not accept, is a lie the human reads first.

    One line per group, assembled rather than interpolated into a fixed template: a template with a
    hardcoded number of `%s` breaks the moment a group is added, and it broke here - the whole program
    failed to start, which for a gate means no gate at all rather than a wrong line of help text.
    """
    fragments = dict(TRANSITIONS["usage"])
    fragments["advance"] = fragments["advance"] % "|".join(TRANSITIONS["agent_targets"])
    lines = ["OCF - orchestrator control flow", ""]
    indent = " " * len("human: ")
    for side in ("agent", "human"):
        for index, group in enumerate(TRANSITIONS["commands"][side]):
            lines.append("%s%s%s" % (side + ": " if index == 0 else indent,
                                     "" if index == 0 else "",
                                     " | ".join(fragments[name] for name in group)))
    lines += ["", "The hook entry is: ocf.py hook   (reads the event JSON on stdin)", ""]
    return "\n".join(lines)


USAGE = render_usage()


def main(argv):
    setup_io()
    if not argv or argv[0] in ("-h", "--help", "help"):
        out(USAGE)
        return 0
    command = argv[0]
    args = argv[1:]
    root = find_root()
    if command == HOOK_SUBCOMMAND:
        return cmd_hook(root, args)
    try:
        policy, warnings = load_policy(root)
        for kind, text in warnings:
            out("[%s] %s" % (kind, text))
        handler = COMMAND_HANDLERS.get(command)
        if handler is None:
            out("unknown command %r" % command)
            out(USAGE)
            return 1
        return handler(root, policy, args)
    except OcfError as exc:
        out(exc.tagged())
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
