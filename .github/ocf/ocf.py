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

import collections
import hashlib
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
# including the fragment the usage text is rendered from. The hook, advance, approve, reject
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
    # Grouped by mechanism, one tuple per line: the gates a human opens, the protected list, and the
    # installation itself. `approve` covers both gated transitions, because the state already says
    # which one is in front of the machine - and printing `approve | reject | confirm` over
    # `allow | deny` put three unrelated verbs in a row where two of them read as synonyms of the third.
    "commands": {
        "agent": (("status", "set", "gate", "journal", "fail", "ok"),
                  ("advance", "init", "selftest", "verify")),
        "human": (("approve", "reject"),
                  ("protect", "unprotect"),
                  ("reload", "install", "package")),
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
        "verify": "verify",
        "reload": "reload",
        "install": "install <target-dir>",
        "package": "package <output-dir>",
        "approve": 'approve "<reason>"',
        "reject": 'reject ["<reason>"]',
        "protect": "protect <path>",
        "unprotect": "unprotect <path>",
    },
}

# Views, not copies: one thing under the names the call sites already use.
AGENT_TARGETS = TRANSITIONS["agent_targets"]
ACTING_STATES = TRANSITIONS["acting_states"]
ENTER_TRANSITION_GATES = TRANSITIONS["gates"]

# The transition each state has a gate on, which is what `approve` opens. Derived from the gates table
# rather than written again, and that derivation is what made the merge possible: with it, "approve is
# the human opening the gate in front of the machine" and "a gate is on this transition" are one fact
# instead of two lists that have to keep agreeing.
APPROVE_TARGETS = {source: target for (source, target) in ENTER_TRANSITION_GATES}

POLICY_REL = ".github/ocf/policy.toml"
HOOKS_REL = ".github/hooks/orchestrator.json"
PLAN_REL = ".orchestrator/plan.md"
# The release is three things: the payload under .github, the bootstrap that deploys it, and the
# version that names both. `release/payload/` is what ships; `release/build/` holds the builder and the
# Inno Setup script, which ship in nothing.
VERSION_REL = "VERSION"
RELEASE_SOURCE = "release/payload"

DEFAULT_STATE_DIR = ".orchestrator"

PLAN_HEADINGS = ("type", "summary", "steps", "tools", "files", "scope", "deliverables", "self-review")
# What a WRITTEN plan must carry, which is only what a gate actually reads. The eight-section shape is
# the agent's own working structure and lives in the template; demanding a file repeat it made the
# human pay for a document nobody read.
PLAN_REQUIRED = ("steps", "files")

# What a tool is, in the order the classes are consulted. The order is strictest first and the first
# class that lists the tool wins, so a tool accidentally listed in two classes is judged by the more
# restrictive one: `visual` is first, so a tool that merely reads an image stays refused even if it
# also appears as a reader.
#
# This table is the only home for the class names and for what each one means. There used to be three:
# the tuple below, the keys of DEFAULT_POLICY's class lists, and a comment block describing them - so a
# class added to one and not the others was a class the policy could name and nothing could classify.
# There was also an `action` class, a leftover bin for "acts, but is neither an edit nor a command",
# and an `always_allow` class, a verdict wearing a category's name. Both are gone: a class says what a
# tool is, and the verdict follows from that.
#
# A tool no class lists is `unknown` - the fallback, deliberately not a member of this table - and is
# judged by what it carries and, failing that, by whether its name looks like an action.
CLASSES = (
    ("visual", "read an image or drive a browser. Refused unconditionally: the machine never looks."),
    ("env", "set up or change the toolchain and the environment: install, configure, debug, scaffold."),
    ("exec", "carries a command string, which is then inspected."),
    ("write", "changes text: files, notebooks, remote files."),
    ("read", "only reads. Never gated, so a broken policy cannot blind whoever has to repair it."),
    ("session", "shapes the conversation and cannot touch the repository: todos, questions, "
                "subagents, memory. Never gated. A subagent's own tool calls are judged separately, "
                "so dispatching one is not a way round the gate."),
)
CLASS_ORDER = tuple(name for name, _ in CLASSES)

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
CONTENT_KEYS = ("content", "newString", "new_string", "newCode", "new_str", "body")

WRITE_RE = re.compile(
    r"(?i)(>>?(?!&)|Set-Content|Out-File|Add-Content|New-Item|Copy-Item|Move-Item|Remove-Item|"
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
    # The class lists are named by the class table above, so there is no second list of class names;
    # `read` and `session` are filled in below, because the strict fallback policy is built from this
    # block and needs them.
    "tools": dict([(name, []) for name, _ in CLASSES]
                  + [("field", {"default": ["command", "code"]})]),
    "selftest": {"canary": []},
    "rule": [],
}

# Two classes carry real defaults, because the strict fallback policy is built from this block.
# Denying every change is the point of a fail-safe; denying reading as well protects nothing and turns
# a one-character policy typo into a total lockout that the agent cannot even help diagnose.
DEFAULT_POLICY["tools"]["session"] = ["runSubagent", "manage_todo_list", "vscode_askQuestions",
                                      "memory"]
DEFAULT_POLICY["tools"]["read"] = [
    "read_file",
    "grep_search",
    "file_search",
    "list_dir",
    "get_errors",
    "copilot_getNotebookSummary",
    "read_notebook_cell_output",
    "vscode_listCodeUsages",
]

# The strictest thing that still lets work continue. Used when the policy cannot be read at all, so
# that "policy is broken" can never mean "gate is open". It denies every change while leaving reading
# and the read and session tools open: the gate must be able to refuse work, not to blind everyone.
STRICT_POLICY = json.loads(json.dumps(DEFAULT_POLICY))
STRICT_POLICY["rule"] = [
    {
        "id": "strict-fallback",
        "enabled": True,
        "result": "deny",
        "if": {"class": "any"},
        "message": "No usable policy file was found, so the strict fallback policy is in force. "
                   "Reading and the session tools still work, so the reason can be diagnosed. "
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
            ("policy", "%s failed to parse: %s. The strict fallback policy is in force, which denies "
                       "every change and allows only reading and the session tools."
                       % (POLICY_REL, exc))])
    return (deep_merge(DEFAULT_POLICY, data), [])


def policy_value(policy, dotted):
    """Read a value the policy is guaranteed to carry, naming the key when it is not there.

    Every read used to carry its own default - a second answer to a question the merge had already
    answered. Those defaults were unreachable, and two of them (`fail_budget`, `min_reason_len`)
    disagreed with the table they shadowed, with nothing pinning the pair together. So the defaults
    live in DEFAULT_POLICY only. A miss here means DEFAULT_POLICY lost a key this program reads: an
    error in this file, not in the human's, and it says which key rather than raising a bare KeyError
    that the hook would turn into an unexplained denial.
    """
    cursor = policy
    walked = []
    for key in dotted.split("."):
        if not isinstance(cursor, dict) or key not in cursor:
            raise OcfError("system", "policy has no %s: DEFAULT_POLICY does not supply it (reached "
                                     "%s)" % (dotted, ".".join(walked) or "the policy root"))
        walked.append(key)
        cursor = cursor[key]
    return cursor


def policy_paths(policy):
    return policy_value(policy, "paths")


def state_dir(root, policy):
    return os.path.join(root, policy_paths(policy)["state_dir"])


def limits(policy):
    return policy_value(policy, "limits")


def strict_bool(value):
    """A switch that only accepts true or false; anything else stays ON and is reported.

    On is the strict direction: an unreadable switch must not switch a guard off, and it must not do
    so quietly either. `policy_findings` names the value, and that finding reaches the model on every
    turn and makes `reload` refuse to write.
    """
    return value if isinstance(value, bool) else True


def enabled(policy):
    return strict_bool(policy_value(policy, "system.enabled"))


def rule_switch(rule, policy):
    """Whether a rule is switched on: the one place a rule's own `enabled` line is read.

    Three spellings, one answer each:

        "switch" or absent  on when the master switch is on. The default, and what every rule in this
                            project says, so the master switch stops the whole gate rather than a list
                            of special cases somebody has to keep in their head.
        true                on whatever the master switch says. Only a human editing the file can
                            suspend such a rule.
        false               off, and the master switch does not bring it back.

    There used to be three functions here asking this one question - one returning how a rule related
    to the switch, one returning the boolean, one splitting the rules into two lists - so a reader
    could not tell which answer the gate obeyed, and the reference document rendered "suspended"
    while the gate went on refusing.
    """
    state = rule.get("enabled", "switch")
    if isinstance(state, bool):
        return state
    if str(state).strip().lower() == "switch":
        return enabled(policy)
    # Out of vocabulary. Hold regardless of the master switch - the strict direction - and let
    # policy_findings report the value, so a typo cannot quietly suspend a rule.
    return True


def window_text(policy):
    """One sentence saying what an open maintenance window left enforcing.

    Shared by status and selftest so the two cannot disagree about the same window. The per-turn prompt
    is deliberately NOT one of its users: that line is injected on every turn, and a list of names that
    does not change between turns is not worth its bytes there. The fact is still one fact - the window
    is open or it is not, and both read it from enabled() - so what differs is the detail, not the
    answer.

    The second half is dropped when nothing holds: "still holding: " with an empty list reads as a
    class of rule that was meant to be there and got lost, which is the opposite of what it means when
    every rule follows the switch.
    """
    pairs = [(rule.get("id", "unnamed"), rule_switch(rule, policy))
             for rule in policy_value(policy, "rule")]
    off = [name for name, on in pairs if not on]
    on = [name for name, on in pairs if on]
    text = "the maintenance window is open. Switched off: %s" % ", ".join(off)
    if on:
        text += ". Still on because they say enabled = true: %s" % ", ".join(on)
    return text + "."


# ---------------------------------------------------------------------------
# Policy vocabulary
# ---------------------------------------------------------------------------

# Every identifier the interpreter is willing to act on. Kept next to the engine rather than in the
# policy file, because the point is to check the file against the engine, not against itself.
SECTION_KEYS = ("system", "paths", "limits", "approval", "hooks", "unknown_tool",
                "tools", "selftest", "rule")
# `allow | ask | deny | require_approval` are defined with their meanings and their editor decisions
# lower down, in ACTIONS: one table rather than a value tuple here and a help list there, which is two
# lists of the same four words that had no reason to agree.
# `enforced` used to be here: it was how a rule asked whether the maintenance window was closed, and
# the three self-protection rules were its only users. The master switch reaches every rule directly
# now, so a rule says `enabled = true` instead and there is no flag left to compute.
COMPUTED_FLAGS = ("control_plane", "invokes_entry", "human_only_call")

# When a soft rule applies. A soft rule is the one kind of rule no hook can enforce - nothing can
# check whether a sentence was written - so instead of a verdict it carries the sentence itself, and
# `reload` writes it into the instructions the model reads on every turn. The occasion is therefore a
# moment in the conversation, not a tool call: the hard vocabulary (class, tool, command, path) has no
# meaning when no tool is being called.
SOFT_OCCASIONS = ("session-start", "ask", "plan", "act", "answer")
RULE_KEYS = ("id", "enabled", "kind", "label", "if", "unless", "result", "else", "message")
# The key a soft rule's sentence lives under, named once because two readers have to agree on it:
# `soft_text` reads it into the standing contract, and `policy_findings` insists it is there. Two
# spellings of one name is how a rule ends up on and silent.
SOFT_SENTENCE_KEY = "message"
# The keys of the shape the engine no longer reads. Kept as a named list so a policy still written in
# it is reported as a rule that cannot fire, rather than as a key nobody recognises.
LEGACY_RULE_KEYS = ("on", "surface", "match", "when", "action", "hit", "skip")


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
    ("system", "enabled: the maintenance window. A rule says for itself whether the window reaches "
               "it - `enabled = true` holds whatever the window says, `false` is never evaluated, "
               "`\"switch\"` is suspended by it - and `status` prints the rules the window actually "
               "switched off, rather than a list written down here that nobody recomputes. The switch "
               "is the human's: no rule grants the agent a way to reach it. The window itself takes "
               "only true or false; any other value is read as true and reported"),
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
    ("enabled", "true: not subject to the master switch, so only a human editing this file can suspend "
                "it. \"switch\" or absent: the maintenance window suspends it. false: never evaluated. "
                "Any other value is read as true and reported"),
    ("kind", "hard: the hook reads it and gives a verdict. soft: reload writes it into the contract"),
    ("label", "the short name a soft rule is listed under, in the generated contract and in the "
              "reference region"),
    ("if", "the conditions that must all hold. This is the whole of a rule's logic"),
    ("unless", "the exception. For a hard rule a condition table; for a soft rule a sentence the model applies"),
    ("result", "hard only: allow | ask | deny | require_approval. A soft rule gives no verdict, so "
               "it does not use this key"),
    ("else", "soft only: the sentence to inject while the rule is off. Optional - with no else, an off "
             "rule says nothing, which is what an off rule used to do unconditionally. It is how one "
             "rule carries both halves of a choice, the behaviour the human wants while it is on and "
             "the behaviour they want while it is off, so that neither half has to be copied into an "
             "agent or prompt definition, where no switch reaches it"),
    ("message", "the sentence the agent reads. For a hard rule it is handed back inside the verdict, "
                "so write what to fix rather than what went wrong; for a soft rule it is the "
                "paragraph injected into the standing contract while the rule is on"),
)

COMPUTED_HELP = (
    ("control_plane", "every statement runs the entry script by its full relative path, so reading the "
                      "state is not acting and needs no approval"),
    ("invokes_entry", "at least one statement does"),
    ("human_only_call", "the entry script is invoked with a human-only subcommand as its argument"),
)

# What a rule may answer, and what the editor is told for each answer - one table, because the two are
# one fact: `require_approval` is this gate refusing and telling the agent to have the human run the
# approve command, and the editor's protocol has no word for that, so it arrives as a denial with a
# reason.
#
# The mapping used to be a dict of its own, and the canaries compared their expectation through that
# same dict - so a canary and the gate agreed by construction, and a mapping that stopped refusing was
# invisible to the one check whose whole job is to notice that.
Action = collections.namedtuple("Action", "help editor")
ACTIONS = {
    "allow": Action("let it through", "allow"),
    "ask": Action("the editor asks the human once; a pre-approval may swallow this", "ask"),
    "deny": Action("refuse, and say why. Handled before any approval logic, so nothing auto-approves "
                   "past it", "deny"),
    "require_approval": Action("refuse, and tell the agent to have the human run the approve command",
                               "deny"),
}
ACTION_VALUES = tuple(ACTIONS)
ACTION_HELP = tuple((name, action.help) for name, action in ACTIONS.items())


def editor_decision(action):
    """What the editor is told. An action the table does not have is refused: fail-safe means refusing,
    and a word nobody defined must not arrive at the editor as permission."""
    known = ACTIONS.get(action)
    return known.editor if known else "deny"

OCCASION_HELP = (
    ("session-start", "a session begins"),
    ("ask", "the human's statement is being read and gaps are being grilled"),
    ("plan", "a plan is being written or audited"),
    ("act", "a tool is about to be used"),
    ("answer", "every answer"),
)

HOOK_HELP = (
    ("enabled", "the master switch. false writes a wiring file with no hooks: nothing is installed. "
                "Any other value is read as true and reported"),
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
              "# [rule] fields. This region is generated - edit the rules, not this text."]
    lines += ["#   %-14s %s" % (name, help_text) for name, help_text in RULE_FIELD_HELP]
    lines += ["",
              "# [rule.if] conditions. Each key is one condition; the value is what it compares against,",
              "# which is usually a regex or a number."]
    # The registry, in its own order - which is the order this region has documented conditions in
    # since it was first generated. `CONDITIONS` is defined below, beside the evaluators it names; a
    # function body is the one place where that costs nothing.
    lines += ["#   %-22s %s" % (name, condition.help)
              for name, condition in CONDITIONS.items()]
    lines += ["",
              "# computed flags, named by the `computed` condition:"]
    lines += ["#   %-18s %s" % (name, help_text) for name, help_text in COMPUTED_HELP]
    lines += ["",
              "# `result` accepts:"]
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
    master = policy_value(policy, "system.enabled")
    if not isinstance(master, bool):
        problems.append("[system] enabled must be true or false; got %r. The master switch has no "
                        "third spelling - \"switch\" belongs to a rule's own enabled line - and an "
                        "unrecognised value is read as true, so the gate stays armed" % (master,))
    for key in sorted(policy_value(policy, "limits")):
        if key not in DEFAULT_POLICY["limits"]:
            problems.append("[limits] unknown key %r; the engine knows %s"
                            % (key, ", ".join(sorted(DEFAULT_POLICY["limits"]))))
    for key in sorted(policy_value(policy, "hooks")):
        if key not in DEFAULT_POLICY["hooks"]:
            problems.append("[hooks] unknown key %r; the engine knows %s"
                            % (key, ", ".join(sorted(DEFAULT_POLICY["hooks"]))))
        elif not isinstance(policy_value(policy, "hooks." + key), bool):
            problems.append("[hooks] %s must be true or false; got %r. An unrecognised value is "
                            "read as true, so the event stays installed"
                            % (key, policy_value(policy, "hooks." + key)))
    for key in sorted(policy_value(policy, "tools")):
        if key not in CLASS_ORDER + ("field",):
            # `field` is the payload-path sub-table rather than a class, and the message lists every
            # key the engine will accept in this section: the classes and that one.
            problems.append("[tools] unknown class %r; the engine knows %s"
                            % (key, ", ".join(sorted(CLASS_ORDER + ("field",)))))
    for index, rule in enumerate(policy_value(policy, "rule")):
        where = "rule %s" % (rule.get("id") or "#%d" % index)
        # Read once, at the top: the per-rule checks below branch on it, and defining it half way
        # down meant the branches above it read an unbound name.
        kind = str(rule.get("kind", "hard")).strip().lower()
        for key in sorted(rule):
            if key not in RULE_KEYS:
                problems.append("%s: unknown key %r; the engine reads %s"
                                % (where, key, ", ".join(RULE_KEYS)))
        # A rule written in the retired on/surface/match/action shape does not merely misspell a key:
        # the engine reads [rule.if] and result, so the rule never fires at all. "unknown key 'on'"
        # leaves the human to work that out, and this project has already paid once for a rule that
        # read as on while being off. So the values are named too, not just the keys.
        legacy = [(key, rule[key]) for key in LEGACY_RULE_KEYS if key in rule]
        if legacy:
            problems.append("%s: written in the retired rule shape (%s); the engine reads [rule.if] "
                            "and result now, so this rule can never fire"
                            % (where, ", ".join("%s = %r" % pair for pair in legacy)))
        # The one shape's conditions, checked the same way for `unless` as for `if`: an unknown name
        # here is a condition nobody evaluates, which reads as a rule that is running and is not.
        if kind == "hard":
            for name, value in sorted((rule.get("unless") or {}).items()):
                if name == "computed" and str(value) not in COMPUTED_FLAGS:
                    problems.append("%s: rule.unless names the computed flag %r; the engine computes %s"
                                    % (where, value, ", ".join(COMPUTED_FLAGS)))
                    continue
                if name in CONDITIONS:
                    continue
                problems.append("%s: rule.unless holds the unknown condition %r; the engine reads %s"
                                % (where, name, ", ".join(sorted(CONDITIONS))))
        state = rule.get("enabled", "switch")
        if not isinstance(state, bool) and str(state).strip().lower() != "switch":
            problems.append("%s: enabled must be true, false or \"switch\"; got %r. true means the "
                            "rule holds whatever the master switch says, \"switch\" (the default) "
                            "means the maintenance window suspends it" % (where, state))
        # Two keys, one per kind: a hard rule carries a verdict in `result`, a soft rule carries the
        # sentence to inject under SOFT_SENTENCE_KEY. Folding them into one local made the verdict check
        # below read the soft key, which is None for every hard rule - and a validator that stops
        # validating without a word is the defect this whole function exists to catch.
        result = rule.get("result")
        soft_sentence = rule.get(SOFT_SENTENCE_KEY)
        if kind not in ("hard", "soft"):
            problems.append("%s: unknown kind %r; the engine knows hard, soft" % (where, kind))
        if kind == "hard" and "if" not in rule:
            # evaluate_rule raises on this, because reading an absent table as "no conditions" would
            # make the rule fire on everything. Reporting it here too says so before it fires.
            problems.append("%s: a hard rule needs [rule.if]; without it the engine cannot tell when "
                            "the rule applies" % where)
        if kind == "soft":
            # A soft rule's logic is prose, not a verdict, so the two are checked against different
            # vocabularies. Accepting a verdict here would let a deny rule be silently filed as a
            # sentence nobody reads.
            if not str(soft_sentence or "").strip():
                problems.append("%s: a soft rule needs %s, the sentence to inject"
                                % (where, SOFT_SENTENCE_KEY))
            if not comma_list((rule.get("if") or {}).get("occasion", "")):
                problems.append("%s: a soft rule needs if.occasion; without one it matches no moment "
                                "and reload writes nothing, so the rule is on and does nothing"
                                % where)
        elif result is not None and result not in ACTION_VALUES:
            problems.append("%s: unknown result %r; the engine knows %s"
                            % (where, result, ", ".join(ACTION_VALUES)))
        if kind == "soft" and "else" in rule and not isinstance(rule["else"], str):
            problems.append("%s: a soft rule's else is the sentence for while it is off, so it is a "
                            "string, not a condition table" % where)
        if kind != "soft" and "else" in rule:
            problems.append("%s: else belongs to a soft rule. A hard rule gives a verdict, not a "
                            "sentence for both switch positions" % where)
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
            elif name in CONDITIONS:
                if name == "computed" and str(value) not in COMPUTED_FLAGS:
                    problems.append("%s: rule.if names the computed flag %r; the engine computes %s"
                                    % (where, value, ", ".join(COMPUTED_FLAGS)))
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
    return [rule for rule in policy_value(policy, "rule")
            if str(rule.get("kind", "hard")).strip().lower() == "soft"]


def soft_text(rule, policy):
    """The sentence a soft rule puts in front of the model, or "" when it has nothing to say.

    A soft rule may carry two halves: `message`, which applies while the rule is on, and `else`, which
    applies while it is off. That is how one rule states both sides of a choice - what to do when the
    machine may not do a thing, and how to do it when it may - without either half living in an agent or
    prompt definition, where no switch reaches it. A rule with no `else` says nothing while it is off.
    """
    key = SOFT_SENTENCE_KEY if rule_switch(rule, policy) else "else"
    return str(rule.get(key) or "").strip()


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
    """Render the standing contract: what a turn needs, and nothing a turn can look up instead.

    Pure: same policy in, same text out, so it can be compared.

    Every turn of every conversation is injected with this text, so its size is paid continuously
    rather than once, and that makes it the wrong home for anything that can be read when it is
    needed. What stays is the entry point, the two things it is dangerous to get wrong, and the soft
    rules - the one part with nowhere else to be, because they are guidance for the turn in progress
    rather than a reference. The gate table, the command reference and the reasoning behind them moved
    to `.github/work-control-flow.md`, which is where they already were: sections 2, 3 and 6 cover the
    commands, the states and the approval, so most of what left here was the second copy.

    It also printed each soft rule once per moment it applied to, so a rule carrying two moments was
    written out twice on every turn. Printed once now, with its moments beside it. That filter was
    also load-bearing in a way it should not have been: a rule with a misspelled occasion used to
    vanish from the contract without a word, and only stayed visible because `policy_findings`
    refuses an occasion the engine does not know.

    The command lines come from `command_lines`, the same ones `--help` prints. This renderer used to
    print the bare names, which cost the arguments and, with them, the only thing that says `deny` is
    not a second spelling of `reject`.
    """
    lines = ["<!-- OCF:GENERATED by `python .github/ocf/ocf.py reload`. Edit the rules in",
             "     .github/ocf/policy.toml and run reload; do not hand-edit this block. -->",
             "",
             "# Work Control Flow",
             "",
             "Gates, commands and the reasoning: [work-control-flow.md](./work-control-flow.md). Read "
             "it before",
             "acting, and whenever a gate refuses something.",
             "",
             "    python .github\\ocf\\ocf.py <cmd>       other platforms: python3 "
             ".github/ocf/ocf.py <cmd>",
             ""]
    # The same lines `--help` prints, arguments included, from the same table. The command names alone
    # were not enough to act on and not enough to tell apart: `deny` has to be able to be told from
    # `reject`, and it cannot be by its name.
    lines += command_lines()
    lines += ["",
              "A human-only command runs in the human's terminal only, never yours; the human asking "
              "you in the",
              "conversation is not approval. If they want that to change, they change the rule or the "
              "config.",
              "",
              "    %s      bypass: %s" % (" -> ".join(TRANSITIONS["flow"]), TRANSITIONS["bypass"]),
              "",
              "Only a human running approve enters executing. Never hand-edit the files under %s or "
              "the protected" % state_dir_relative(policy),
              "list (%s)." % PROTECTED_LIST_REL,
              "",
              "When a gate refuses, fix the precondition it names - never rewrite your way around it.",
              "",
              "## Soft rules",
              "",
              "Prose for a moment in the work. Each names the moments it is in force at; the exception "
              "is yours to judge.",
              ""]
    written = 0
    for rule in soft_rules(policy):
        text = soft_text(rule, policy)
        if not text:
            continue
        moments = ", ".join(comma_list((rule.get("if") or {}).get("occasion", ""))) or "always"
        if not rule_switch(rule, policy):
            # Off, and carrying something to say about being off. The label describes the rule as it
            # reads while it is on, so printing it here would caption the other half with the wrong
            # sentence; the text stands on its own.
            lines.append("- (%s) %s" % (moments, one_line(text)))
            written += 1
            continue
        label = str(rule.get("label") or rule.get("id") or "rule").strip()
        lines.append("- **%s** (%s) - %s" % (label, moments, one_line(text)))
        exception = rule.get("unless")
        if isinstance(exception, str) and exception.strip():
            lines.append("  unless: %s" % one_line(exception))
        written += 1
    if not written:
        lines += ["No soft rule has anything to say right now."]
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
    if not strict_bool(policy_value(policy, "hooks.enabled")):
        return False
    for switch, name, _ in HOOK_EVENTS:
        if name == event:
            return strict_bool(policy_value(policy, "hooks." + switch))
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
        members = policy_value(policy, "tools." + name)
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
              "changed. Every rule follows the master switch; the three spellings of a rule's own ",
              "`enabled` line, and which one is the default, are in that file's vocabulary region.", ""]
    # No per-rule switch marker here. Which rules the switch happens to be holding is state, and a
    # document that renders state disagrees with itself the moment the human flips the switch. Which
    # spelling a rule uses is configuration, but it is already in the vocabulary region, and reading
    # the `enabled` line a second time is how two answers to one question start.
    for rule in policy_value(policy, "rule"):
        identity = rule.get("id", "unnamed")
        kind = str(rule.get("kind", "hard")).strip().lower()
        outcome = rule.get("result")
        if kind == "soft":
            # The rule's name, not its sentence: `message` holds the whole paragraph injected into the
            # contract, which is not what an index of rules is for.
            lines.append("- `%s` (soft, occasion `%s`) - %s"
                         % (identity,
                            ", ".join(comma_list((rule.get("if") or {}).get("occasion", ""))) or "none",
                            one_line(rule.get("label") or identity)))
            continue
        conditions = sorted(set(rule.get("if") or {})
                            | ({"unless"} if rule.get("unless") else set()))
        lines.append("- `%s` -> `%s` - %s%s"
                     % (identity,
                        outcome,
                        one_line(rule.get("message", "")),
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
            return SyncResult(False, "%s already matches" % relative)
        write_text(path, rendered)
        return SyncResult(True, "rewrote %s" % relative)
    begin, end = markers
    block = "%s\n%s\n%s" % (begin, rendered, end)
    start = text.find(begin)
    if start < 0:
        if text.find(end) >= 0:
            raise OcfError("policy", "%s has the end marker but not the begin marker; refusing to "
                                     "guess where the generated region starts" % relative)
        separator = "\n\n" if text.strip() else ""
        write_text(path, text.rstrip("\n") + separator + block + "\n")
        return SyncResult(True, "appended the generated region to %s" % relative)
    stop = text.find(end, start)
    if stop < 0:
        raise OcfError("policy", "%s has the begin marker but not the end marker; refusing to guess "
                                 "where the generated region stops" % relative)
    updated = text[:start] + block + text[stop + len(end):]
    if updated == text:
        return SyncResult(False, "%s already matches" % relative)
    write_text(path, updated)
    return SyncResult(True, "rewrote the generated region in %s" % relative)


def hooks_note(root, policy, result):
    """What to add after rewriting the wiring, which no amount of rewriting can make take effect.

    Hooks are read when the editor window starts, so this is the one generated file whose change is not
    felt until then - and the one that can be written with no hooks in it at all.
    """
    if not result.changed:
        return ""
    off = [name for switch, name, _ in HOOK_EVENTS if not hook_enabled(policy, name)]
    if not hook_enabled(policy, "PreToolUse"):
        return (" The gate is not installed now, so nothing is refused. Reload the VS Code window for "
                "it to take effect: hooks are read when the window starts.")
    return (" (%s off). Reload the VS Code window for it to take effect: hooks are read when the "
            "window starts and are not re-read afterwards."
            % (", ".join(off) if off else "no event"))


# One row per generated file, and the only place its path, its region and its renderer are named
# together. There used to be four writers, three marker pairs, four renderer calls in `reload` and a
# path restated in three tests - so a fifth artifact was five edits, and the failure mode of forgetting
# one is a file that looks current and is not.
#
# `markers` of None means this program owns the whole file; a pair means it owns one region of a file
# the human also edits, and the text outside the pair survives. `note` adds what the human has to do
# that writing the file cannot do.
Generated = collections.namedtuple("Generated", "relative render markers note")
# What `sync_generated` did, as fields rather than as a sentence. The sentence used to be the
# interface: `hooks_note` told the human to reload the window only when it found "already matches"
# inside it, and `engine_checks` pinned the same wording. A sentence that load-bearing turns a
# rewording into a silent behaviour change. The message is now for printing only.
SyncResult = collections.namedtuple("SyncResult", "changed message")
GENERATED = (
    Generated(INSTRUCTIONS_REL, render_instructions, (INSTRUCTIONS_BEGIN, INSTRUCTIONS_END), None),
    # The vocabulary is the engine's, not the file's, so its renderer ignores the policy - but every
    # renderer in this table takes one, so that the table has one shape.
    Generated(POLICY_REL, lambda policy: render_vocabulary(), (VOCAB_BEGIN, VOCAB_END), None),
    Generated(REFERENCE_REL, render_reference, (REFERENCE_BEGIN, REFERENCE_END), None),
    Generated(HOOKS_REL, render_hooks_json, None, hooks_note),
)


def generated(relative):
    """The row for one generated file.

    Raises rather than returning None: a caller that silently got nothing back would look exactly like
    a file with nothing to write, which is how a check stops checking anything.
    """
    for artifact in GENERATED:
        if artifact.relative == relative:
            return artifact
    raise OcfError("policy", "%s is not a generated file" % relative)


def write_generated(root, policy, artifact):
    """Write one generated file from the table; report whether it changed, and what to tell the human."""
    result = sync_generated(root, artifact.relative, artifact.render(policy), artifact.markers)
    if artifact.note:
        result = SyncResult(result.changed, result.message + artifact.note(root, policy, result))
    return result


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
    fields = policy_value(policy, "tools.field")
    names = list(fields.get(tool, [])) + list(policy_value(policy, "tools.field.default"))
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


def collect_strings(node, keys, depth=0):
    """Every non-blank string under one of `keys`, anywhere in a nested payload.

    One walker for both key sets. There were two, and they differed in nothing but the tuple they
    matched and a `.strip()`: so the depth limit, the treatment of lists, and any fix to either of them
    applied to half the payload. The `.strip()` is shared now, which narrows content by whitespace-only
    strings - a value that is nothing but spaces cannot carry an instruction either way.
    """
    found = []
    if depth > 4:
        return found
    if isinstance(node, dict):
        for key, value in node.items():
            if key in keys and isinstance(value, str) and value.strip():
                found.append(value)
            elif isinstance(value, (dict, list)):
                found.extend(collect_strings(value, keys, depth + 1))
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)):
                found.extend(collect_strings(item, keys, depth + 1))
    return found


# What a command's target is: one question with three answers. A list cannot hold the difference
# between "this writes nowhere" and "this writes somewhere I could not read", and collapsing them is
# not a cosmetic loss. Both arrived as an empty list, so `touches_protected` had no candidate to judge
# and simply did not apply - and the pair was asymmetric in the worst way: `Set-Content -Path
# CONTEXT.md -Value x` was refused, while the same write through a variable was allowed, in the same
# state, by the same rule set. The unreadable spelling is the easier one to reach for, so the weaker
# answer was the one a machine would find.
#
# A writing TOOL is not part of this: it declares its target in the payload, so no target read from a
# payload means the tool does not touch a path. A command's target has to be READ OUT OF FREE TEXT, so
# failing to read one is a fact about the gate, not about the command.
Targets = collections.namedtuple("Targets", "certainty paths")
TARGET_NONE = "none"        # it does not write, so no path condition is about it
TARGET_THESE = "these"      # it writes here, and these are the paths
TARGET_UNREAD = "unknown"   # it writes, and where could not be read


def write_targets(root, command):
    if not command or not WRITE_RE.search(command):
        return Targets(TARGET_NONE, [])
    found = []
    for token in TOKEN_RE.findall(command):
        if len(token) < 3 or not token.strip("./\\"):
            continue
        found.append(norm_path(root, token))
    return Targets(TARGET_THESE if found else TARGET_UNREAD, found)


# What one tool call carries, read from the payload. A value rather than six lines of a constructor,
# because "what does the payload say" is the gate's input surface and it used to be decided in a
# seventeen-line `__init__` that also classified the call - so the order the reading had to happen in
# lived in a comment, and the evaluator's dependency on one of those attributes was a lockout when it
# was called too early.
Call = collections.namedtuple("Call", "tool command content paths targets raw_json")


def read_call(root, policy, payload):
    """Everything one tool call carries, read once and in one place.

    Two asymmetries are deliberate and load-bearing. Paths are read from `tool_input` alone: a path
    mentioned somewhere else in the envelope is not a path this call touches. Content is read from the
    whole payload, because a write tool puts it wherever it likes - and the nested-replacement bug that
    read only `content`/`newString` is why that walk exists at all.
    """
    tool = payload.get("tool_name") or ""
    command = extract_command(payload, policy, tool)
    return Call(tool=tool,
                command=command,
                content="\n".join(collect_strings(payload, CONTENT_KEYS)),
                paths=[item for item in
                       (norm_path(root, value) for value in
                        collect_strings(payload.get("tool_input") or {}, PATH_KEYS))
                       if item],
                targets=write_targets(root, command),
                raw_json=json.dumps(payload, ensure_ascii=True, sort_keys=True))


# ---------------------------------------------------------------------------
# Tool classification
# ---------------------------------------------------------------------------

def tool_class(policy, tool):
    tools = policy_value(policy, "tools")
    for name in CLASS_ORDER:
        if tool in tools.get(name, []):
            return name
    return "unknown"


def approval_needed(policy, state):
    """True when acting requires the human's approve.

    This asks about the state alone. It used to consult the master switch as well, which meant the
    switch switched rules off by changing the answer to a question rather than by switching anything -
    the approval rules were suspended without saying so anywhere a reader could see. They follow the
    switch through `rule_switch` like every other rule now.
    """
    allowed = policy_value(policy, "approval.allowed_states")
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
        read = read_call(root, policy, payload)
        self.tool = read.tool
        self.command = read.command
        self.content = read.content
        self.paths = read.paths
        self.targets = read.targets
        self.raw_json = read.raw_json
        self.klass = tool_class(policy, self.tool)
        self.computed = computed_flags(self.command)
        self.infos = []
        self.report = []
        if not self.paths and self.targets.paths:
            self.infos.append("paths inferred from the command: %s"
                              % ", ".join(self.targets.paths[:5]))
        if self.targets.certainty == TARGET_UNREAD:
            self.infos.append("this command writes, but where it writes could not be read from it")
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

        An unreadable target is returned as no candidate rather than as a made-up one, and that is the
        whole of this method's third answer: a path condition is a question about a path, so with no
        path there is nothing for it to be right about. Making up a candidate would mean a regex
        matching a string that is not a path, which is how an allow rule grows a hole. The fact that a
        write was seen and its target was not is carried by `write_target_unread` instead, and a rule
        says in its own words what to do about it.
        """
        if self.klass == "write":
            return list(self.paths)
        return list(self.targets.paths)

    def candidates(self):
        """Everything the call carries, in one pool: the tool name, the command, every path it
        mentions, every path it would change, and the text it would write.

        This is what a condition is judged against when it is not scoped to one candidate value. It
        used to be one of six named surfaces behind a `surface(name)` method, and only `"any"` was
        ever asked for: the other five branches were unreachable, and the branch whose whole job was
        to refuse an unknown name referenced a table (`SURFACE_NAMES`) that no longer existed - so it
        raised NameError instead of the refusal it promised. A method with one caller and one live
        branch is not a seam; it is a name for what the caller can say itself.
        """
        merged = [self.tool] if self.tool else []
        if self.command:
            merged.append(self.command)
        merged.extend(self.paths)
        merged.extend(self.targets.paths)
        if self.content:
            merged.append(self.content)
        return merged


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
    unknown = [name for name in wanted if name not in CLASS_ORDER + ("unknown", "any")]
    if unknown:
        raise OcfError("policy", "unknown class %r; the engine knows %s"
                                 % (unknown[0], ", ".join(CLASS_ORDER + ("unknown", "any"))))
    return "any" in wanted or context.effective in wanted


# ---------------------------------------------------------------------------
# The condition vocabulary
# ---------------------------------------------------------------------------
#
# One entry per condition name a rule may use, and the only home for the four facts about it: how it
# is evaluated, where its candidate values come from, what the policy file's own documentation should
# say about it, and which other names it is read together with. The evaluator, the policy validator
# and the vocabulary renderer each read this table - which used to be three sets that had to be kept
# in step by hand, a fourth table that documented them, and a fifth that said where two of them got
# their values.
#
# `scope` is one of four:
#
#   context    judged once for the call as a whole
#   value      judged against each candidate value the action carries, so it needs a candidate source.
#              One qualifying candidate must never decide the fate of every other one in the batch
#   fact       the leader of a group of names read together, because they are one question about the
#              facts file. `applies` receives the whole sub-dict rather than a single value
#   companion  a name that only exists beside a `fact` leader, which names it in `companions`. It has
#              no evaluator of its own - that is what the None means - so it is a vocabulary entry and
#              never a condition in its own right.
#
# Field order is (scope, applies, candidates, companions, help).
Condition = collections.namedtuple("Condition", "scope applies candidates companions help")


def fact_group(context, spec):
    """The one question the `fact` condition asks, spelled with four names in the policy file.

    `is_set` asks about presence rather than value, so it compares against whether the fact is
    non-empty; `is_not` asks the opposite of equality; and an entry with neither compares for equality
    against `is`, which defaults to the empty string so that a lone `fact` means "is it set".
    """
    name = spec["fact"]
    if "is_set" in spec:
        present = bool(context.facts.get(name, "").strip())
        return present == as_bool(spec["is_set"])
    if "is_not" in spec:
        return context.facts.get(name, "") != str(spec["is_not"])
    return context.facts.get(name, "") == str(spec.get("is", ""))


def changed_paths(context):
    """Where a value-scoped condition gets the candidates it judges.

    The paths an action would CHANGE, never the whole text it carries and never every path it
    mentions. Two separate over-blocks came from getting this wrong: reading them off "any" let a path
    that merely appeared in the content decide the verdict, which for an allow rule is a hole rather
    than a nuisance; and reading them off every inferred path meant a command that MENTIONS a protected
    file was refused, so `python .github/ocf/ocf.py status` - which names the entry script on every
    call - would have stopped the gate being read at all. Reading a human's file is not changing it.
    """
    return context.changed_paths()


CONDITIONS = {
    "class": Condition(
        "context", class_condition, None, (),
        "the tool's class, or the class a tool carrying a command is judged as: visual, env, exec, "
        "write, read, session, unknown, or any for every tool"),
    "state": Condition(
        "context", lambda context, value: context.state in comma_list(value), None, (),
        "the current state: ready, asking, planning, executing, reporting, blocked"),
    "tool": Condition(
        "context", lambda context, value: context.tool in comma_list(value), None, (),
        "the tool name as the editor reports it"),
    "command_matches": Condition(
        "context",
        lambda context, value:
            bool(context.command) and re.search(value, context.command, re.I | re.S) is not None,
        None, (),
        "regex against the command string"),
    "command_length_over": Condition(
        "context", lambda context, value: len(context.command) > int(value), None, (),
        "the command is longer than this many characters"),
    "command_statements_over": Condition(
        "context", lambda context, value: len(statements(context.command)) > int(value), None, (),
        "the command has more statements than this, counted with quotes blanked"),
    "command_repeats_at_least": Condition(
        "context",
        lambda context, value:
            command_repeat_count(context.root, context.policy, context.command) >= int(value),
        None, (),
        "this exact command has already run at least this many times"),
    "content_matches": Condition(
        "context",
        lambda context, value:
            re.search(value, context.raw_json + "\n" + context.content, re.I | re.S) is not None,
        None, (),
        "regex against what would be written, plus the raw tool input"),
    "environment_declared": Condition(
        "context",
        lambda context, value: bool(context.facts.get("stack_env", "").strip()) == as_bool(value),
        None, (),
        "the human has declared the existing environment (the stack_env fact)"),
    "approval": Condition(
        "context", lambda context, value: as_bool(value) == context.needs_approval, None, (),
        "true when a human approve is still outstanding, i.e. the state is not an acting state"),
    "computed": Condition(
        "context", lambda context, value: bool(context.computed.get(value)), None, (),
        "a flag computed from the command. See the list below"),
    "fact": Condition(
        "fact", fact_group, None, ("is_set", "is", "is_not"),
        "a fact recorded by the human or the hook, compared with is / is_not / is_set"),
    "is_set": Condition(
        "companion", None, None, (),
        "with fact: the fact is non-empty (true) or empty (false)"),
    "is": Condition(
        "companion", None, None, (),
        "with fact: the fact equals this value"),
    "is_not": Condition(
        "companion", None, None, (),
        "with fact: the fact does not equal this value"),
    "write_target_unread": Condition(
        "context",
        lambda context, value: as_bool(value) == (context.targets.certainty == TARGET_UNREAD),
        None, (),
        "the action writes, but no path could be read out of it, so no path condition can judge it"),
    "path_matches": Condition(
        "value",
        lambda context, value, candidate: re.search(value, candidate, re.I) is not None,
        changed_paths, (),
        "regex against each path the action touches. Judged per path, never as a batch"),
    "touches_protected": Condition(
        "value",
        lambda context, value, candidate:
            as_bool(value) == listed_in(context.root, PROTECTED_LIST_REL, candidate),
        changed_paths, (),
        "each path the action would change is on the protected list, judged per path"),
}

# The condition whose `applies` receives the whole fact sub-dict. Derived rather than written again,
# so the registry stays the only place that says which name leads that group.
FACT_LEADER = next(name for name, condition in CONDITIONS.items() if condition.scope == "fact")


def evaluate_conditions(context, rule, conditions):
    """Return the matching candidates when every condition holds, else None.

    One evaluator for both halves of a rule. `unless` is the same question asked with the opposite
    answer, so a second implementation of "do these conditions hold" is exactly the way the two drift
    apart - and they had: `unless` was read by the older rule shape and ignored by this one, so a rule
    moved to the newer shape silently lost its exception.

    Which lane a condition is judged in is the registry's answer, not this function's: the fact group
    is one question asked with several names, the value lane is judged per candidate, and the rest are
    judged once for the call.

    A name the registry does not have is refused, but only when the walk reaches it. Refusing it up
    front would be tidier and would also change which policy typos lock the gate - the refusal runs
    through the hook's fail-safe, which denies everything - and that is a decision of its own rather
    than a consequence of reading the vocabulary from one table. So the order of the checks below is
    exactly the order it was in before this table existed.
    """
    scopes = {name: CONDITIONS[name].scope for name in conditions if name in CONDITIONS}
    # The companions come along: `fact` is the name that points at the fact, and `is` / `is_not` /
    # `is_set` say what to ask about it, so the leader is handed all of them or none of them is a
    # question. Filtering to `fact` alone made every `fact` rule ask whether a fact that is not set
    # equals the empty string, which is true - and `failure-budget` denied every command in the suite.
    fact_part = {name: spec for name, spec in conditions.items()
                 if scopes.get(name) in ("fact", "companion")}
    if fact_part and not CONDITIONS[FACT_LEADER].applies(context, fact_part):
        return None
    value_conditions = [name for name in conditions if scopes.get(name) == "value"]
    pool = []
    for name in value_conditions:
        source = CONDITIONS[name].candidates
        for value in (source(context) if source else context.candidates()):
            if value not in pool:
                pool.append(value)
    candidates = []
    for value in (pool if value_conditions else context.candidates()):
        if all(CONDITIONS[name].applies(context, conditions[name], value)
               for name in value_conditions):
            candidates.append(value)
    if value_conditions and not candidates:
        return None
    for name, spec in conditions.items():
        if scopes.get(name) in ("fact", "companion", "value"):
            continue
        condition = CONDITIONS.get(name)
        if condition is None:
            raise OcfError("policy", "rule %r: unknown condition %r" % (rule.get("id"), name))
        if not condition.applies(context, spec):
            return None
    return candidates or [context.tool]


def evaluate_rule_v2(context, rule):
    """Evaluate a rule written in the newer shape, and return a verdict dict when it fires.

    Soft rules are never evaluated: they are guidance for the model and live in the instructions the
    hook injects, so the gate has no verdict to give about them.
    """
    if not rule_switch(rule, context.policy):
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
            "message": rule.get("message", ""), "hit": candidates[0], "rule": rule}


def evaluate_rule(context, rule):
    """Return a verdict dict when the rule fires, else None. One shape, one evaluator.

    There used to be two: a flat `on`/`surface`/`match`/`when` chain and the condition table. The cost
    was not the extra code, it was that one of them read `unless` and the other silently did not, so a
    rule moved between the two shapes quietly lost its exception. The self-protection rules were the
    last users of the older shape and are converted, so it is gone.

    A soft rule is not gated at all: it is guidance for the model, and it lives in the contract the
    hook injects, so the gate has no verdict to give about it.
    """
    if str(rule.get("kind", "hard")).strip().lower() == "soft":
        return None
    if "if" not in rule:
        # An absent condition table is not "no conditions, so it always holds". Reading it that way
        # would turn a malformed rule into a rule that fires on everything, and a rule that fires on
        # everything denies every tool call - a typo becomes a lockout.
        raise OcfError("policy", "rule %r has no [rule.if], so the engine cannot tell when it applies"
                                 % rule.get("id"))
    return evaluate_rule_v2(context, rule)


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
    for rule in policy_value(context.policy, "rule"):
        fired = evaluate_rule(context, rule)
        if fired:
            action = fired["action"]
            reason = "[%s] %s" % (fired["id"], fired.get("message", ""))
            if fired.get("hit") and fired["action"] in ("deny", "ask"):
                reason += "\n  target: %s" % fired["hit"]
            if action == "require_approval":
                reason = "[%s] Approval required. %s\n%s" % (
                    fired["id"], fired.get("message", ""), context.approval_hint())
                return ("require_approval", fired["id"], reason)
            return (action, fired["id"], reason)
    if context.klass == "exec" and not context.command and context.enabled:
        return ("deny", "unreadable-command",
                "[unreadable-command] This tool runs a command but no command could be read from the "
                "payload, so it cannot be inspected. Refusing rather than allowing it blind. Add the "
                "field path under [tools.field] in policy.toml.")
    if context.effective == "unknown":
        if context.enabled and context.needs_approval and UNKNOWN_ACTION_RE.search(context.tool):
            setting = policy_value(context.policy, "unknown_tool.action")
            return (setting, "unknown-tool",
                    "[unknown-tool] This tool is not classified in policy.toml and is only escalated "
                    "while acting is not allowed. Classify it under [tools] if the answer should be "
                    "permanent.")
    return ("allow", "default", "[default] No rule objected.")

# ---------------------------------------------------------------------------
# Hook handling
# ---------------------------------------------------------------------------

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
            "permissionDecision": editor_decision(action),
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
        # An event this program does not read never reaches here: hook_enabled turned it away above.
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


INTAKE_STATES = ("ready", "asking")


def log_prompt(root, policy, payload, state):
    """Record a prompt while the intake is open, enough to tell who actually spoke.

    Temporary instrumentation, not a rule: nothing reads it back. It exists because the gate has no
    reliable signal for "a human really replied", and the honest way to settle that is to look at what
    the hook actually receives rather than to reason about it. It is bounded now - written only while
    the intake is open, and cleared when the machine leaves `asking` - so it is evidence for one
    question rather than an append-only log. Delete the mechanism once the question is answered.
    """
    if state not in INTAKE_STATES:
        return
    prompt = " ".join(str(payload.get("prompt") or "").split())[:160]
    session = str(payload.get("session_id") or "")[:8]
    path = os.path.join(state_dir(root, policy), "prompt-log")
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write("%s | %s | %s | %s\n" % (now_stamp(), session, state, prompt))


def clear_prompt_log(root, policy):
    """Drop the intake transcript when the intake closes.

    The instrumentation answers one question - whether a reply the hook receives really came from a
    human - and that question is only open during the intake. Clearing here is what makes the file a
    bounded piece of evidence rather than an append-only log nobody reads.
    """
    path = os.path.join(state_dir(root, policy), "prompt-log")
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


# What the intake accumulates, and therefore what "the intake is over" has to clear. `grill-valid` is
# the gate that reads them and it runs on `asking -> planning`, so they may only exist while the
# machine is in `asking`: entering `asking` opens a fresh intake, leaving it closes this one. `blocked`
# is the exception in both directions - it interrupts an intake rather than ending or starting one, so
# a trip through it does not throw away the rounds the human already spent.
INTAKE_FACTS = ("grill_rounds", "consensus", "grill_used")


def clear_intake_facts(root, policy):
    """Drop the counters the intake is judged by. Returns whether any of them were there."""
    facts = load_facts(root, policy)
    removed = [key for key in INTAKE_FACTS if key in facts]
    for key in removed:
        facts.pop(key, None)
    if removed:
        save_facts(root, policy, facts)
    return bool(removed)


def close_intake(root, policy):
    """End the intake: clear what it was judged by, and drop its transcript.

    `clear_prompt_log` stays a function of its own rather than being folded in here, for two reasons.
    `engine_checks.py` asserts on it directly, so removing the name would break that suite with an
    `AttributeError` rather than an `AssertionError`. And `handle_prompt` needs the counters cleared
    without touching the transcript, because at that moment the transcript is the entry the intake
    that is just starting has written.
    """
    cleared = clear_intake_facts(root, policy)
    dropped = clear_prompt_log(root, policy)
    return cleared or dropped


def handle_prompt(root, policy, payload):
    state = read_state(root, policy)
    facts = load_facts(root, policy)
    prompt = payload.get("prompt") or ""
    log_prompt(root, policy, payload, state)
    lines = []
    if state == "ready":
        write_state(root, policy, "asking")
        # A fresh intake: nothing it will be judged by may be left over from the last one. Only the
        # counters - the transcript line above is this intake's first entry, not the last one's.
        clear_intake_facts(root, policy)
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
        # Named rules are not listed here. This is injected into every turn, and the list is the same
        # 500 bytes every time - the point is that the gate is disarmed, not which 29 rules that is.
        # `status` and `selftest` print the names for the human, who is the one who acts on them.
        lines.append("OCF WARNING: the maintenance window is open, so every rule that follows the "
                     "switch is suspended. `status` lists them.")
    state = read_state(root, policy)
    # The state is reported; the state LIST is not. It is already in the injected contract, in the
    # flow line, and repeating it here was the third copy of the same six words.
    lines.append("OCF state: %s." % state)
    if state == "asking":
        outstanding = context_missing(root, policy)
        if outstanding:
            # The items, not the duty. How they are asked for is the `intake-grilling` soft rule, and
            # that is already in front of the model on every turn. Repeating it here gave one soft rule
            # two homes, and the human can switch off only one of them.
            lines.append("Intake items not derivable from context: %s." % ", ".join(outstanding))
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
        findings.append(("policy", "system.enabled is false: %s" % window_text(policy)))
    else:
        findings.append(("ok", "system.enabled is true, so every rule is in force"))
    try:
        if not os.path.isdir(state_dir(root, policy)):
            os.makedirs(state_dir(root, policy))
        probe = os.path.join(state_dir(root, policy), ".selftest")
        write_text(probe, "ok")
        os.remove(probe)
        findings.append(("ok", "state directory is writable"))
    except Exception as exc:
        findings.append(("system", "state directory is not writable: %r" % (exc,)))
    # The canaries prove the machinery still denies, so they run with the maintenance window shut.
    # Run against the file as written instead, and a human opening the window - which suspends every
    # rule that does not say `enabled = true` - turns them red, and a red canary that means "the gate
    # is doing exactly what the human asked" is a canary nobody reads. The window itself is not lost:
    # it has its own finding above, which is what makes it impossible to miss.
    armed = dict(policy)
    armed["system"] = dict(policy_value(armed, "system"), enabled=True)
    canaries = policy_value(policy, "selftest.canary")
    if not canaries:
        findings.append(("policy", "no canaries are defined in [selftest]; the gate cannot prove "
                                   "that it is still denying"))
    for canary in canaries:
        expect = canary.get("expect", "deny")
        payload = dict(canary.get("payload", {}))
        payload.setdefault("hook_event_name", "PreToolUse")
        try:
            action, rule_id, _, _ = check_pretooluse(root, armed, payload)
        except Exception as exc:
            findings.append(("system", "canary %s raised %r" % (canary.get("id"), exc)))
            continue
        # `expect` is written in the editor's vocabulary rather than the policy's, and the decision is
        # read straight from the action - never through the table the gate itself uses. Comparing both
        # sides through that table made them agree by construction, which left the one check whose job
        # is to notice that the gate stopped refusing unable to notice exactly that.
        got = editor_decision(action)
        if got == expect:
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
# Is this deployment usable?
# ---------------------------------------------------------------------------
#
# `selftest` answers "is the gate armed and alive right now". This answers the other question - "would
# this repository work if someone opened a window on it" - and the two disagree in exactly one place,
# an unarmed gate. `selftest` reports the open window as a finding, because a human reading "every rule
# is in force" needs to know it is not; `verify` reports it as the setup state it is, because a fresh
# deployment is verified BEFORE it is armed and a verify that says FAIL there is a verify nobody reads.
#
# What neither can do is confirm VS Code's side of the contract. The payload shape is read off the
# extension's own consumers - field names, and whether additionalContext is a string or a list - and the
# only way to confirm it is a window reload and one real conversation. What this proves is this side of
# it: that every event this program claims to handle is accepted, answered in that shape, and answered
# the way the policy says. "The file is installed" and "the event arrives and a decision comes back"
# are different claims, and the gap between them is the failure this whole program exists to expose.
Probe = collections.namedtuple("Probe", "event what payload decision container")
PAYLOAD_PROBES = (
    Probe("PreToolUse", "a command that invokes a human-only subcommand",
          {"tool_name": "run_in_terminal",
           "tool_input": {"command": 'python .github/ocf/ocf.py approve "looks fine"'}},
          "deny", None),
    Probe("PreToolUse", "a tool that only reads",
          {"tool_name": "read_file", "tool_input": {"filePath": "README.md"}},
          "allow", None),
    Probe("UserPromptSubmit", "a human message",
          {"session_id": "verify", "prompt": "hello"}, None, "str"),
    Probe("SessionStart", "a session starting", {"session_id": "verify"}, None, "str"),
    Probe("PostToolUse", "a command that reported no progress",
          {"tool_name": "run_in_terminal", "tool_response": "1.1.1\n>>\n"}, None, "list"),
)

# Files installing must not overwrite. `policy.toml` is the rules themselves - the human's program, and
# the one file an install could destroy in a way nothing would notice. `protected.txt` is the
# per-repository list, which is the entire point of installing into another repository. The reference
# document keeps the distribution's prose and the human's own notes in the same file, so it is replaced
# only where the target has none: a document that disagrees with the code is worse than a stale note,
# but neither is worth destroying someone's writing over. When one of these differs, the shipped version
# is written beside it as `.dist` so the human can read the difference instead of losing it.
INSTALL_KEEP_IF_PRESENT = (POLICY_REL, PROTECTED_LIST_REL, REFERENCE_REL)
INSTALL_SKIP_DIRS = ("__pycache__",)


def install_files(root):
    """Every file installing copies, relative to the repository root.

    `.github/` only. The repository's own README is not part of this and must not be: an install into
    another repository that overwrote its README would be the rudest thing this program could do.
    """
    found = []
    for directory, subdirs, names in os.walk(os.path.join(root, ".github")):
        subdirs[:] = [name for name in subdirs if name not in INSTALL_SKIP_DIRS]
        for name in names:
            if name.endswith((".pyc", ".pyo")):
                continue
            relative = os.path.relpath(os.path.join(directory, name), root)
            found.append(relative.replace("\\", "/"))
    return sorted(found)


# What a release can be checked against: one hash per owned file, so "is this still the release we
# built" is a comparison rather than a judgement. Hashes are taken over the NORMALISED text, not the raw
# bytes, because every other comparison in this program already ignores line endings and a byte hash
# would differ between a checkout with autocrlf and one without, for files that are equal in every way
# that matters.
MANIFEST_REL = ".github/ocf/MANIFEST.sha256"


def manifest_scope(root):
    """The files a manifest covers: what installing OWNS, as repository-relative paths.

    Not the three the human owns (`policy.toml`, `protected.txt`, the reference document) and not the
    four generated from their policy. A manifest covering those would go red the moment the human did
    what this system asks them to do, and a check that fires on correct use is a check nobody reads.
    The set is derived from the tables that already say who owns what, not written out again.
    """
    skip = set(INSTALL_KEEP_IF_PRESENT)
    skip.update(artifact.relative for artifact in GENERATED)
    skip.add(MANIFEST_REL)
    return [relative for relative in install_files(root) if relative not in skip]


def manifest_text(root):
    """One line per covered file: sha256, two spaces, path. Pure, so two trees can be compared."""
    lines = []
    for relative in manifest_scope(root):
        text = read_text(os.path.join(root, relative.replace("/", os.sep))) or ""
        lines.append("%s  %s" % (hashlib.sha256(text.encode("utf-8")).hexdigest(), relative))
    return "\n".join(lines) + "\n"


def manifest_findings(root):
    """Whether this copy's own files are the ones the release shipped, by hash.

    It verifies what the manifest LISTS rather than walking the tree: a file nobody listed is not a fact
    about us, and the question here is "is this still the release it claims", not "has anyone put
    anything of their own in .github". A working checkout has no manifest - only a release tree does -
    and saying so is the honest answer rather than a pass.

    What this can and cannot do, stated rather than implied: it catches a payload that was truncated,
    half-copied or altered on the way here. It is not a signature - anyone able to alter the files can
    alter the manifest beside them - and it is not aimed at the human who deploys this and then edits
    their own policy, which is what they are supposed to do.
    """
    declared = read_text(os.path.join(root, MANIFEST_REL))
    if declared is None:
        return [("ok", "no %s here, so this copy is a working checkout rather than an unpacked "
                       "release and there is nothing to compare it to" % MANIFEST_REL)]
    findings = []
    for line in declared.splitlines():
        if not line.strip():
            continue
        digest, _, relative = line.partition("  ")
        relative = relative.strip()
        if not relative:
            findings.append(("system", "the manifest has a line it cannot read: %r" % line[:80]))
            continue
        text = read_text(os.path.join(root, relative.replace("/", os.sep)))
        if text is None:
            findings.append(("system", "%s is in the manifest and is not here" % relative))
            continue
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != digest.strip():
            findings.append(("system", "%s does not hash to what the manifest says, so it is not the "
                                         "file this release shipped" % relative))
    if not findings:
        findings.append(("ok", "every file the manifest lists matches this copy"))
    return findings


def run_hook_payload(root, event, payload):
    """Run the real entry point on one payload, the way the editor does: JSON on stdin, JSON out."""
    import subprocess  # here and not at the top: see rule 1 at the head of this file
    env = dict(os.environ)
    env["OCF_ROOT"] = root
    env["PYTHONIOENCODING"] = "utf-8"
    body = dict(payload, hook_event_name=event)
    proc = subprocess.run([sys.executable, os.path.join(root, ".github", "ocf", "ocf.py"),
                           HOOK_SUBCOMMAND],
                          input=json.dumps(body).encode("utf-8"), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=env, cwd=root)
    return proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")


def payload_findings(root, policy):
    """One realistic payload per event, through the real entry point, against a throwaway copy.

    A copy, because two of these events write: a UserPromptSubmit advances the state and records the
    prompt, and a verification that moved the machine would be its own kind of bug. The window is shut
    in the copy for the same reason the canaries shut it - a suspended rule gives a suspended answer,
    and a check that passes with nothing in force proves nothing.
    """
    import shutil
    import tempfile
    findings = []
    probe = tempfile.mkdtemp(prefix="ocf-verify-")
    try:
        for relative in install_files(root):
            write_text(os.path.join(probe, relative.replace("/", os.sep)),
                       read_text(os.path.join(root, relative)) or "")
        write_text(os.path.join(probe, POLICY_REL),
                   re.sub(r"(?m)^enabled\s*=\s*(true|false)", "enabled = true",
                          read_text(os.path.join(probe, POLICY_REL)) or "", count=1))
        write_text(os.path.join(probe, ".orchestrator", "state"), "asking\n")
        for entry in PAYLOAD_PROBES:
            where = "%s: %s" % (entry.event, entry.what)
            text, errors = run_hook_payload(probe, entry.event, entry.payload)
            lines = [line for line in text.splitlines() if line.startswith("{")]
            if not lines:
                findings.append(("system", "%s produced no decision at all. stderr: %s"
                                           % (where, errors.strip()[-200:] or "empty")))
                continue
            try:
                spec = json.loads(lines[-1]).get("hookSpecificOutput") or {}
            except Exception as exc:
                findings.append(("system", "%s answered with something that is not JSON: %s"
                                           % (where, exc)))
                continue
            if spec.get("hookEventName") != entry.event:
                findings.append(("system", "%s answered as %r, so the editor would route it to the "
                                           "wrong event" % (where, spec.get("hookEventName"))))
            if entry.decision and spec.get("permissionDecision") != entry.decision:
                findings.append(("system", "%s answered %r where the policy says %r"
                                           % (where, spec.get("permissionDecision"), entry.decision)))
            if entry.decision and not str(spec.get("permissionDecisionReason") or "").strip():
                findings.append(("system", "%s gave a decision with no reason, so the human cannot "
                                           "tell why" % where))
            if entry.container:
                value = spec.get("additionalContext")
                wanted = {"str": str, "list": list}[entry.container]
                if not isinstance(value, wanted):
                    findings.append(("system", "%s sent additionalContext as %s where the editor "
                                               "expects %s"
                                               % (where, type(value).__name__, entry.container)))
    finally:
        shutil.rmtree(probe, ignore_errors=True)
    if not findings:
        findings.append(("ok", "all %d hook payloads answered in the shape the editor expects, "
                               "across all four events" % len(PAYLOAD_PROBES)))
    return findings


def wiring_findings(root, policy):
    """Whether the wiring file is the one the switches describe, event by event.

    `selftest` asks only whether the file mentions ocf.py, which a wiring missing three of its four
    events passes. The question here is whether each enabled event has an entry that starts this
    program with the subcommand that turns it into a hook, and whether there is a Windows command -
    the wiring carries two commands per event and only one of them runs on Windows.
    """
    findings = []
    on_disk = read_text(os.path.join(root, HOOKS_REL))
    if on_disk is None:
        return [("environment", "%s is missing, so VS Code starts nothing at all" % HOOKS_REL)]
    if on_disk.strip() != render_hooks_json(policy).strip():
        findings.append(("policy", "%s is not what the [hooks] switches describe, so what is "
                                   "installed is not what the policy says. Run `reload`."
                                   % HOOKS_REL))
    try:
        body = json.loads(on_disk).get("hooks", {})
    except Exception as exc:
        return findings + [("environment", "%s is not valid JSON: %s" % (HOOKS_REL, exc))]
    if not body:
        findings.append(("environment", "no event is installed, so nothing here is refused. Turn "
                                        "[hooks] enabled on and run `reload`, or leave it and "
                                        "accept that this gate is off"))
        return findings
    for switch, event, _ in HOOK_EVENTS:
        if not hook_enabled(policy, event):
            continue
        entries = body.get(event) or []
        if not entries:
            findings.append(("policy", "%s is on but the wiring has no entry for %s" % (switch, event)))
            continue
        if not any("ocf.py" in str(item.get(key, "")) and HOOK_SUBCOMMAND in str(item.get(key, ""))
                   for item in entries for key in ("command", "windows")):
            findings.append(("system", "%s does not run `ocf.py %s`, so the event arrives nowhere"
                                       % (event, HOOK_SUBCOMMAND)))
        if not any("ocf.py" in str(item.get("windows", "")) for item in entries):
            findings.append(("environment", "%s has no Windows command, so on Windows it never fires"
                                            % event))
    return findings


def generated_findings(root, policy):
    """Whether what the model reads and what the gate enforces are the same revision.

    This is the deployment failure that looks most like success. The human edits a rule, forgets
    `reload`, and `.github/copilot-instructions.md` goes on telling the model about the rules from
    before the edit while the gate enforces the new ones. Every file is individually valid and nothing
    else notices - the case table runs against the renderer, not against the copy on disk.
    """
    findings = []
    for artifact in GENERATED:
        on_disk = read_text(os.path.join(root, artifact.relative))
        if on_disk is None:
            findings.append(("system", "%s is missing, so this installation is not finished"
                                       % artifact.relative))
            continue
        rendered = artifact.render(policy)
        if artifact.markers is None:
            current, expected = on_disk, rendered
        else:
            begin, end = artifact.markers
            start = on_disk.find(begin)
            stop = on_disk.find(end, start) if start >= 0 else -1
            if start < 0 or stop < 0:
                findings.append(("system", "%s has lost its generated markers, so reload can neither "
                                           "find the region nor replace it" % artifact.relative))
                continue
            current = on_disk[start:stop + len(end)]
            expected = "%s\n%s\n%s" % (begin, rendered, end)
        if current.strip() != expected.strip():
            findings.append(("policy", "%s no longer matches what the code renders, so the model is "
                                       "reading a different revision from the one the gate enforces. "
                                       "Run `reload`." % artifact.relative))
    return findings


def verify_findings(root, policy):
    """Everything that has to be true for this repository to work when a window is opened on it."""
    findings = []
    for kind, text in run_selftest(root, policy):
        if kind == "policy" and text.startswith("system.enabled is false"):
            findings.append(("ok", "the maintenance window is open, so nothing is refused yet. That is "
                                   "the state a deployment is verified in; arm it in policy.toml and "
                                   "run `reload` when the next steps below are done"))
            continue
        findings.append((kind, text))
    findings.extend(wiring_findings(root, policy))
    findings.extend(generated_findings(root, policy))
    findings.extend(manifest_findings(root))
    findings.extend(payload_findings(root, policy))
    return findings


def cmd_verify(root, policy, args):
    findings = verify_findings(root, policy)
    for kind, text in findings:
        out("[%s] %s" % (kind, text))
    if selftest_failed(findings):
        out("RESULT: FAIL - something above is not usable")
        return 1
    out("RESULT: PASS - installed, wired, and answering")
    return 0


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
    limits_now = limits(policy)
    out("limits=%s" % ",".join("%s:%s" % (key, limits_now[key]) for key in sorted(limits_now)))
    out("facts:")
    for key in sorted(facts):
        out("  %s=%s" % (key, facts[key]))
    if not enabled(policy):
        out("WARNING: %s" % window_text(policy))
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
    budget = int(policy_value(policy, "limits.fail_budget"))
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
    # Entering `asking` opens an intake; leaving it closes one. `blocked` is the exception both ways:
    # it interrupts the intake rather than ending or starting one, so the rounds a human already spent
    # survive a trip through it - which is also why the counters are not cleared on the way in.
    closes = source == "asking" and target != "blocked"
    opens = target == "asking" and source != "blocked"
    if (closes or opens) and close_intake(root, policy):
        journal(root, policy, target, "intake %s" % ("closed" if closes else "opened"))
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
    minimum = int(policy_value(policy, "approval.min_reason_len"))
    if len(reason) < minimum:
        raise OcfError("environment", "%s needs a real one-sentence reason (at least %d characters)"
                                      % (verb, minimum))
    return reason


def cmd_approve(root, policy, args):
    """The human opens the gate that is in front of the machine, wherever it is.

    One verb for both gated transitions, because the state already says which gate that is. `confirm`
    and `approve` were two functions doing the same four things - run the gates, refuse if any failed,
    write the state, journal it - and differing only in the transition they named, so two names for one
    act were also two chances to forget the act: the reason was required for the plan and not for the
    intake, for no better reason than that being how the two functions happened to be written.

    The reason is required either way now, for the same reason `min_reason_len` exists at all - an
    approval is a sentence, not a keystroke.

    `approved_by` is set only for the plan approval: it records who authorised the work, and the two
    steps are not the same authorisation. That branch is about what to record, not about which verb
    ran, which is why it is the only place the two transitions are treated differently.
    """
    reason = require_reason(policy, args, "approve")
    state = read_state(root, policy)
    target = APPROVE_TARGETS.get(state)
    if target is None:
        raise OcfError("environment", "there is no gate to approve from %s; approve applies in %s"
                                      % (state, " and ".join(sorted(APPROVE_TARGETS))))
    results = run_gates(root, policy, ENTER_TRANSITION_GATES[(state, target)])
    for name, ok, detail in results:
        out("%-18s %s  %s" % (name, "PASS" if ok else "FAIL", detail))
    failed = [name for name, ok, _ in results if not ok]
    if failed:
        raise OcfError("system", "refusing to approve %s -> %s, gate(s) failed: %s"
                                 % (state, target, ", ".join(failed)))
    write_state(root, policy, target)
    if state == "asking" and close_intake(root, policy):
        journal(root, policy, target, "intake closed: approved out of asking")
    if target == "executing":
        set_fact(root, policy, "approved_by", os.environ.get("OCF_APPROVED_BY", "human"))
        set_fact(root, policy, "approved_reason", reason)
    journal(root, policy, target, "%s -> %s (approved: %s)" % (state, target, reason))
    out("approved, state %s -> %s" % (state, target))
    return 0


def cmd_reject(root, policy, args):
    reason = " ".join(args).strip() or "rejected"
    state = read_state(root, policy)
    if state != "planning":
        raise OcfError("environment", "reject only applies in planning, current state is %s" % state)
    write_state(root, policy, "asking")
    if close_intake(root, policy):
        journal(root, policy, "asking", "intake reopened: reject sends it back to asking")
    journal(root, policy, "asking", "planning -> asking (rejected: %s)" % reason)
    out("rejected, state planning -> asking")
    return 0


def update_list(root, policy, path, add):
    """Add or remove one entry in the protected list. Reached only from the human's own terminal.

    `policy` is a parameter and not a name this function hopes is in scope. It was not one, so the
    journal call below raised NameError on every run of `protect` and `unprotect` - after the write, so
    the list really changed and the command really reported success on stdout, and then died with a
    traceback and exit code 1. A human-only command that says it failed while having done the thing is
    the worst shape of report there is, and the audit line was never written.

    The two words in the journal line are the command names. They are spelled here rather than carried
    in from the handler because they name the direction the list moved, which is the fact the log is
    for: `protect` protects and `unprotect` lets the machine touch it.
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


def release_version(root):
    """The version a release is named after, from the one-line file beside the code.

    A file rather than a constant, because three things have to agree about it and only one of them is
    Python: the archive, the installer's name, and the payload. The builder passes this same value to
    Inno Setup as a define, and a number written in two languages is a number that eventually
    disagrees.
    """
    text = read_text(os.path.join(root, VERSION_REL))
    if text is None or not text.strip():
        raise OcfError("policy", "%s is missing or empty, so a release cannot be named" % VERSION_REL)
    return text.strip().splitlines()[0].strip()


def release_files(root):
    """What a release carries beside the payload, as (path in this repository, name in the archive).

    `release/payload/` is the shipped half and `release/build/` is the half that exists only to build
    it, so what ships is decided by a directory and not by a filter that has to be kept up to date.
    The build script and the Inno Setup script live in the other one and are in no archive.
    """
    base = os.path.join(root, RELEASE_SOURCE)
    if not os.path.isdir(base):
        raise OcfError("policy", "%s is missing, so a release would ship a payload with nothing to "
                                 "deploy it" % RELEASE_SOURCE)
    found = []
    for directory, subdirs, names in os.walk(base):
        subdirs[:] = [name for name in subdirs if name not in INSTALL_SKIP_DIRS]
        for name in names:
            if name.endswith((".pyc", ".pyo")):
                continue
            full = os.path.join(directory, name)
            found.append((os.path.relpath(full, root).replace("\\", "/"),
                          os.path.relpath(full, base).replace("\\", "/")))
    return sorted(found)


def cmd_package(root, policy, args):
    """Write the release tree: the payload, the bootstrap that deploys it, and the version.

    Human-only for the same reason `install` is - it writes a tree wherever it is told - and it exists
    so that the payload has one owner: this and `install` both read `install_files`, which is what
    stops the archive and a deployed copy from shipping different file sets.

    What is deliberately NOT here is the archive and the installer. Those are formats, the builder makes
    them, and neither one gets to decide what is in them.

    It writes and never deletes, so a stale file from an older build survives in an output tree that is
    reused. The builder owns `dist/` and clears it; this only ever fills in what it was told to.
    """
    if not args:
        raise OcfError("environment", "package needs an output directory: package <output-dir>")
    version = release_version(root)
    stage = os.path.join(os.path.abspath(args[0]), "ocf-gate-%s" % version)
    written = 0
    for relative in install_files(root):
        text = read_text(os.path.join(root, relative.replace("/", os.sep)))
        if text is None:
            continue
        write_text(os.path.join(stage, relative.replace("/", os.sep)), text)
        written += 1
    for source, shipped in release_files(root):
        write_text(os.path.join(stage, shipped.replace("/", os.sep)),
                   read_text(os.path.join(root, source.replace("/", os.sep))) or "")
        written += 1
    write_text(os.path.join(stage, VERSION_REL), version + "\n")
    written += 1
    # Written last and computed from the tree AS WRITTEN, so it describes the release rather than
    # intending to. `install` compares against it before it copies anything.
    write_text(os.path.join(stage, MANIFEST_REL.replace("/", os.sep)), manifest_text(stage))
    written += 1
    out("packaged OCF Gate %s" % version)
    out("  tree: %s" % stage)
    out("  %d files: the .github payload, %s, and %s" % (written, RELEASE_SOURCE, VERSION_REL))
    out("  archive it, or compile release/build/ocf-gate.iss against it - release/build/build.ps1 "
        "does both")
    return 0


def cmd_install(root, policy, args):
    """Deploy this repository's gate into another directory, then say what is left to do.

    Human-only, and it has to be. It writes files into a directory this gate does not watch, so nothing
    would stop `ocf.py install .` from replacing the gate's own source with a copy of itself - and that
    write happens inside this program, where no rule can see it. What closes it is the command table:
    a word on the human side makes `ocf.py install ...` a human-only subcommand, which is refused.

    The source is this repository. The rules and the protected list are the human's, so a target that
    already has them keeps them and gets the shipped version beside them as `.dist`; everything else is
    this distribution's and is replaced. Then the target's own generated regions are written from the
    target's own policy, so what its model reads and what its gate enforces are the same revision
    before anyone opens a window on it.
    """
    if not args:
        raise OcfError("environment", "install needs a target directory: install <target-dir>")
    target = os.path.realpath(os.path.abspath(args[0]))
    source = os.path.realpath(os.path.abspath(root))
    if os.path.normcase(target) == os.path.normcase(source):
        raise OcfError("environment", "that is this repository. To refresh what it generates run "
                                      "`reload`; to check it run `verify` and tests/run.py")
    findings = policy_findings(policy)
    if findings:
        for kind, text in findings:
            out("[%s] %s" % (kind, text))
        raise OcfError("policy", "refusing to install a copy whose own policy does not load cleanly")
    # Before anything is written: is this the release it claims to be? A payload that was truncated,
    # half-copied or altered on the way here cannot be told from a good one afterwards, because once it
    # is in the target there is nothing left to compare it against.
    findings = manifest_findings(root)
    for kind, text in findings:
        out("[%s] %s" % (kind, text))
    if selftest_failed(findings):
        raise OcfError("system", "refusing to install: this copy does not match its own %s"
                                 % MANIFEST_REL)
    written, replaced, kept = [], [], []
    for relative in install_files(source):
        text = read_text(os.path.join(source, relative.replace("/", os.sep)))
        if text is None:
            continue
        destination = os.path.join(target, relative.replace("/", os.sep))
        existing = read_text(destination)
        if relative in INSTALL_KEEP_IF_PRESENT and existing is not None:
            if existing == text:
                kept.append(relative)
            else:
                write_text(destination + ".dist", text)
                kept.append("%s (yours kept; the shipped one is %s.dist)"
                            % (relative, os.path.basename(relative)))
            continue
        if existing == text:
            kept.append(relative)
            continue
        write_text(destination, text)
        (replaced if existing is not None else written).append(relative)
    out("installed into %s" % target)
    for label, items in (("wrote", written), ("replaced", replaced), ("left alone", kept)):
        if items:
            out("  %s (%d): %s" % (label, len(items),
                                    ", ".join(items[:6]) + (" ..." if len(items) > 6 else "")))
    try:
        target_policy, warnings = load_policy(target)
    except Exception as exc:
        out("[system] the installed policy did not load: %s" % exc)
        out("RESULT: FAIL - copied, but not usable")
        return 1
    for kind, text in warnings:
        out("[%s] %s" % (kind, text))
    if read_text(os.path.join(state_dir(target, target_policy), "state")) is None:
        write_state(target, target_policy, "ready")
        journal(target, target_policy, "ready", "install")
        out("  initialised state=ready")
    for artifact in GENERATED:
        out("  %s" % write_generated(target, target_policy, artifact).message)
    out("")
    out("next, in the target repository:")
    out("  1. record what the machine may not change:")
    out("       python .github/ocf/ocf.py protect \"<path>\"")
    out("  2. arm it: [system] enabled = true in .github/ocf/policy.toml, then `ocf.py reload`")
    out("  3. reload the VS Code window: hooks are read when the window starts")
    out("  4. start a conversation and confirm the gate answers")
    out("")
    findings = verify_findings(target, target_policy)
    for kind, text in findings:
        out("[%s] %s" % (kind, text))
    if selftest_failed(findings):
        out("RESULT: FAIL - copied, but not usable yet; the findings above say why")
        return 1
    out("RESULT: PASS - installed, wired, and answering")
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
    for artifact in GENERATED:
        out(write_generated(root, policy, artifact).message)
    rules = soft_rules(policy)
    on = [rule for rule in rules if rule_switch(rule, policy)]
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
    "verify": cmd_verify,
    "approve": cmd_approve,
    "reject": cmd_reject,
    "reload": cmd_reload,
    "install": cmd_install,
    "package": cmd_package,
    "protect": lambda root, policy, args: update_list(root, policy, args[0] if args else "", True),
    "unprotect": lambda root, policy, args: update_list(root, policy, args[0] if args else "", False),
}


def command_fragments():
    """One exact spelling per command, from the table, with the one placeholder filled in."""
    fragments = dict(TRANSITIONS["usage"])
    fragments["advance"] = fragments["advance"] % "|".join(TRANSITIONS["agent_targets"])
    return fragments


def command_lines():
    """The command lines, one line per group, in the shape both callers print.

    Shared, because there were two renderers and they disagreed about the one thing that matters here:
    the usage text printed the exact spelling - `deny <path>` - and the injected contract printed bare
    names. So the model read `deny` sitting between `reject` and `allow` with nothing to say that it
    edits the protected list while those two decide a state transition, and one verb read as three.
    The arguments are not decoration: `deny` with no path is not a command that can be run.
    """
    fragments = command_fragments()
    indent = " " * len("human: ")
    lines = []
    for side in ("agent", "human"):
        for index, group in enumerate(TRANSITIONS["commands"][side]):
            lines.append("%s%s" % (side + ": " if index == 0 else indent,
                                   " | ".join(fragments[name] for name in group)))
    return lines


def render_usage():
    """Render the usage text from the table, so the command list has one owner.

    Written out instead of typed a second time: a usage line that names a command the dispatcher does
    not have, or an advance target the machine does not accept, is a lie the human reads first.

    One line per group, assembled rather than interpolated into a fixed template: a template with a
    hardcoded number of `%s` breaks the moment a group is added, and it broke here - the whole program
    failed to start, which for a gate means no gate at all rather than a wrong line of help text.
    """
    lines = ["OCF - orchestrator control flow", ""] + command_lines()
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
