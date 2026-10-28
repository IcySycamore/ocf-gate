#!/usr/bin/env python3
"""Is the OCF hook actually running in the session you are in?

A gate that was never installed looks exactly like a gate with nothing to object to: both are silent.
This program exists to tell those apart. Every rule in `policy.toml` is enforced by a hook process, and
a hook process the editor never starts enforces nothing - while `selftest` and `verify` go on reporting
PASS, because they run the engine directly and the engine is healthy either way.

So this probe deliberately does NOT use `ocf.py`. It is the one check that has to work when the hook,
the wiring, or the policy is the thing that is broken.

It reads only, writes nothing, and needs nothing but the standard library.

What it can see, and what it cannot. It can see whether a hook is installed and whether the editor is
even reading hook files. It can see the execution log of the local chat agent, which runs hooks in the
editor extension. It cannot see the execution of hooks that the agent-host runtime runs itself, so in
one of those sessions it reports NO-EVIDENCE rather than inventing a verdict - an earlier revision of
this probe reported "harness unsupported" there, and that was wrong: the host turns file hooks on
(`enableFileHooks`), and both harnesses accept this project's wiring unchanged. The decisive test costs
nothing and needs no file: when the hook runs it returns text that the editor puts into the model's
context, so the model can see whether it was served. That is what the standing contract requires it to
check, and it is the only test that cannot be fooled by a healthy engine behind a dead hook.

Exit status is always 0: the verdict is the output, and a probe whose exit code is its answer must not
feed the failure budget of the thing it is probing.
"""

import glob
import json
import os
import re
import sqlite3
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WIRING = os.path.join(REPO, ".github", "hooks", "orchestrator.json")
HOOK_LOG_REL = os.path.join("exthost", "GitHub.copilot-chat", "GitHub Copilot Chat Hooks.log")

# A hook fires on every human message and every tool call, so it is never minutes out of date in a
# session that is being used. The window is generous because the probe cannot know when the current
# session started - it reports the age as well as the verdict, and the age is the real evidence.
STALE_AFTER_MIN = 15

# Which harness ids are the local chat agent, and which execute hooks somewhere this probe cannot
# read. The session key is the reliable place to read this: a session that belongs to the local chat
# agent is a bare uuid, and one that does not carries its harness as a prefix - 
# `agent-host-copilotcli:/<id>`. The model's own `vendor` moved between two reads of the same
# workspace, so it is a cross-check and not the source.
#
# Both harnesses load this project's wiring. The local chat agent runs hooks in the editor extension,
# which logs to `GitHub Copilot Chat Hooks.log`. The agent-host runtime runs them itself - the host
# passes `enableFileHooks: true` when it creates the session, and it accepts the same event keys
# (PascalCase and camelCase both resolve to the same event) and the same `command`/`windows`/`timeout`
# action shape. So for an agent-host session this probe has no execution log to read, and saying "the
# harness does not support this" would be false. What it can say honestly is that it cannot see.
LOCAL_HARNESS = "local"
UNKNOWN_HARNESS = "unknown"

LOG_ROOTS = (
    os.path.join(os.environ.get("APPDATA", ""), "Code", "logs"),
    os.path.join(os.environ.get("APPDATA", ""), "Code - Insiders", "logs"),
    os.path.join(os.path.expanduser("~"), "Library", "Application Support", "Code", "logs"),
    os.path.join(os.path.expanduser("~"), ".config", "Code", "logs"),
)

SETTINGS_FILES = (
    os.path.join(os.environ.get("APPDATA", ""), "Code", "User", "settings.json"),
    os.path.join(os.environ.get("APPDATA", ""), "Code - Insiders", "User", "settings.json"),
    os.path.join(os.path.expanduser("~"), "Library", "Application Support", "Code", "User",
                 "settings.json"),
    os.path.join(os.path.expanduser("~"), ".config", "Code", "User", "settings.json"),
)


def note(level, label, text):
    return (level, label, text)


def state_db(repo):
    """The workspace's own state database, found by matching the folder the storage belongs to.

    Every storage directory names its folder in `workspace.json`, so this is a lookup rather than a
    guess at the hash. The database is opened read-only and may be locked while the editor runs; a
    failure here is reported as "unknown", never as a fault, because the probe still has the hook log.
    """
    roots = [
        os.path.join(os.environ.get("APPDATA", ""), "Code", "User", "workspaceStorage"),
        os.path.join(os.environ.get("APPDATA", ""), "Code - Insiders", "User", "workspaceStorage"),
        os.path.join(os.path.expanduser("~"), "Library", "Application Support", "Code", "User",
                     "workspaceStorage"),
        os.path.join(os.path.expanduser("~"), ".config", "Code", "User", "workspaceStorage"),
    ]
    target = os.path.normcase(os.path.normpath(repo))
    for root in roots:
        if not os.path.isdir(root):
            continue
        for entry in os.scandir(root):
            if not entry.is_dir():
                continue
            manifest = os.path.join(entry.path, "workspace.json")
            if not os.path.isfile(manifest):
                continue
            try:
                with open(manifest, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except Exception:
                continue
            folder = str(data.get("folder") or "")
            if not folder:
                continue
            path = re.sub(r"^file:///", "", folder)
            path = path.replace("/", os.sep)
            try:
                from urllib.parse import unquote
                path = unquote(path)
            except Exception:
                pass
            candidate = os.path.normcase(os.path.normpath(path))
            if os.path.isfile(os.path.join(entry.path, "state.vscdb")) and candidate == target:
                return os.path.join(entry.path, "state.vscdb")
    return None


def read_items(db_path, wanted):
    """Rows of `ItemTable` whose key is in `wanted`, or None when the database cannot be read."""
    for uri in ("file:%s?mode=ro" % db_path.replace("\\", "/"),
                "file:%s?immutable=1" % db_path.replace("\\", "/")):
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=1.0)
            try:
                rows = dict(connection.execute("select key, value from ItemTable"))
            finally:
                connection.close()
            return {key: rows.get(key) for key in wanted}
        except Exception:
            continue
    return None


def current_session(db_path):
    """The newest chat session this workspace knows about, and the viewer's model identifier.

    The session key carries the harness as a prefix on sessions that do not belong to the local chat
    agent - `agent-host-copilotcli:/<id>` - and a local session has no prefix at all. That prefix is
    the whole reason this probe can answer "why is nothing being enforced" instead of only "nothing is".
    """
    items = read_items(db_path, ("chat.ChatSessionStore.index", "chat.untitledInputState"))
    if items is None:
        return None, None, "the state database could not be read"
    session_id = None
    try:
        index = json.loads(items.get("chat.ChatSessionStore.index") or "{}")
        entries = index.get("entries") or {}
        if entries:
            key = max(entries, key=lambda k: entries[k].get("lastMessageDate") or 0)
            session_id = key
    except Exception:
        pass
    # The session key first, because it is the one place the harness is written unambiguously. A bare
    # uuid means the local chat agent; a prefix means a harness that is not it. Defaulting to "local"
    # when nothing was found was the first bug this probe had: it reported the local agent for an
    # `agent-host-copilotcli` session, which is the exact false pass the probe exists to prevent.
    harness = ""
    if session_id:
        harness = session_id.split(":", 1)[0] if ":" in session_id else LOCAL_HARNESS
    if session_id and harness == LOCAL_HARNESS:
        return session_id, harness, None
    reported = ""
    try:
        state = json.loads(items.get("chat.untitledInputState") or "{}")
        model = state.get("selectedModel") or {}
        metadata = model.get("metadata") or {}
        for candidate in (metadata.get("vendor"), metadata.get("targetChatSessionType"),
                          model.get("vendor")):
            text = str(candidate or "").strip()
            if text:
                reported = text
                break
    except Exception:
        pass
    return session_id, harness or reported or UNKNOWN_HARNESS, None


def newest_hook_log():
    """The most recently written hook execution log, with its age and its last session id."""
    best = None
    for root in LOG_ROOTS:
        if not root or not os.path.isdir(root):
            continue
        pattern = os.path.join(root, "*", "window*", HOOK_LOG_REL)
        for path in glob.glob(pattern):
            try:
                stat = os.stat(path)
            except OSError:
                continue
            if best is None or stat.st_mtime > best[1]:
                best = (path, stat.st_mtime, stat.st_size)
    if best is None:
        return None
    path, mtime, size = best
    last_session = None
    if size:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                tail = handle.read()[-20000:]
            found = re.findall(r'"session_id":"([0-9a-f-]{8,})"', tail)
            if found:
                last_session = found[-1]
        except Exception:
            pass
    return (path, mtime, size, last_session)


def hook_switches():
    """Which events the wiring file installs. Read as data, so a broken policy cannot hide this."""
    if not os.path.isfile(WIRING):
        return None
    try:
        with open(WIRING, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:
        return {"error": "%r" % (exc,)}
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return {"error": "no `hooks` object"}
    events = [name for name, entries in hooks.items() if entries]
    return {"events": sorted(events), "mentions_ocf": "ocf.py" in json.dumps(data)}


def chat_use_hooks():
    """`chat.useHooks`, or the default. It is the outer switch: off means no hook file is even read."""
    for path in SETTINGS_FILES:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except Exception:
            continue
        try:
            settings = json.loads(re.sub(r"^\s*//.*$", "", text, flags=re.M))
        except Exception:
            continue
        if "chat.useHooks" in settings:
            return settings["chat.useHooks"], path
    return None, None


def main():
    findings = []

    wiring = hook_switches()
    if wiring is None:
        findings.append(note("fail", "wiring", "%s does not exist, so no hook is installed at all"
                             % os.path.relpath(WIRING, REPO)))
        wiring_ok = False
    elif wiring.get("error"):
        findings.append(note("fail", "wiring", "%s could not be read: %s"
                             % (os.path.relpath(WIRING, REPO), wiring["error"])))
        wiring_ok = False
    elif not wiring["events"]:
        findings.append(note("policy", "wiring", "%s installs no event, so nothing is enforced"
                             % os.path.relpath(WIRING, REPO)))
        wiring_ok = False
    else:
        findings.append(note("ok", "wiring", "%s installs %d event(s): %s"
                             % (os.path.relpath(WIRING, REPO), len(wiring["events"]),
                                ", ".join(wiring["events"]))))
        wiring_ok = "PreToolUse" in wiring["events"]
        if not wiring_ok:
            findings.append(note("fail", "wiring", "PreToolUse is not among them, and PreToolUse is "
                                                   "the gate: the other three only add context"))

    value, where = chat_use_hooks()
    hooks_read = value is not False
    if value is None:
        findings.append(note("ok", "chat.useHooks", "not set in any user settings file, so the "
                                                    "default applies (true)"))
    elif value is True:
        findings.append(note("ok", "chat.useHooks", "true (set in %s)" % where))
    else:
        findings.append(note("fail", "chat.useHooks", "%r in %s: hook files are not read at all"
                             % (value, where)))

    db_path = state_db(REPO)
    session_id, harness, read_error = (None, "", "no state database was found for this workspace")
    if db_path:
        session_id, harness, read_error = current_session(db_path)
    harness_known = harness not in (UNKNOWN_HARNESS, "")
    runtime_executes = harness_known and harness != LOCAL_HARNESS
    if read_error:
        findings.append(note("warn", "harness", "%s; the harness is unidentified, so this probe leans "
                                              "on the hook log alone" % read_error))
    elif not harness_known:
        findings.append(note("warn", "harness", "could not be identified from the session store, so "
                                                "this probe leans on the hook log alone"))
    elif runtime_executes:
        findings.append(note("warn", "harness", "%s session (%s) - this harness runs hooks in its own "
                                               "runtime, where this probe cannot read them. The wiring "
                                               "shape is accepted by both harnesses, so absence of "
                                               "evidence here is not evidence of absence."
                             % (harness, session_id)))
    else:
        findings.append(note("ok", "harness", "local chat agent (%s) - hooks run in the editor "
                                             "extension, whose log is what this probe reads"
                             % session_id))

    log = newest_hook_log()
    alive = False
    if log is None:
        findings.append(note("warn" if runtime_executes else "fail", "hook log",
                             "no `GitHub Copilot Chat Hooks.log` under any editor log directory. That "
                             "log belongs to the local chat agent; this harness keeps its own, so this "
                             "is only conclusive in a local session"))
    else:
        path, mtime, size, last_session = log
        age_min = (time.time() - mtime) / 60.0
        detail = "%s, %.1f min old, %d byte(s)" % (path, age_min, size)
        fresh = size > 0 and age_min <= STALE_AFTER_MIN
        if fresh:
            alive = True
        level = "ok" if fresh else ("warn" if runtime_executes else "fail")
        findings.append(note(level, "hook log", "%s; last entry's session_id=%s"
                             % (detail, last_session or "unknown")))
        if runtime_executes and not fresh:
            findings.append(note("warn", "hook log", "this log covers the local chat agent, not an %s "
                                                     "session, so it neither proves nor disproves that "
                                                     "event (hook) ran" % harness))
        elif not fresh:
            findings.append(note("fail", "hook log", "the last entry is older than %d min, so no hook "
                                                     "has run in this session" % STALE_AFTER_MIN))
        if last_session and session_id and last_session not in session_id:
            findings.append(note("warn", "hook log", "the newest hook entry belongs to session %s, "
                                                     "which is not the current session"
                                 % last_session))

    if not wiring_ok:
        verdict, detail = "NOT-INSTALLED", "the wiring is wrong, so nothing can be enforced"
    elif not hooks_read:
        verdict, detail = "DISABLED", "chat.useHooks is off, so no hook file is read at all"
    elif alive:
        verdict, detail = "ALIVE", "hooks are executing and the editor recorded it"
    elif runtime_executes:
        verdict, detail = ("NO-EVIDENCE",
                           "the wiring is right and this harness does run file hooks, but it runs them "
                           "in its own runtime, where this probe has nothing to read. It cannot "
                           "confirm the gate is in force, and it cannot rule it out either")
    else:
        verdict, detail = ("STALE",
                           "the wiring is right, but no hook has run recently enough to account for "
                           "this session")

    width = max(len(label) for _, label, _ in findings)
    print("OCF hook liveness - read-only probe, no part of this reads ocf.py or the policy")
    print("")
    for level, label, text in findings:
        print("[%s] %-*s  %s" % (level, width, label, text))
    print("")
    print("RESULT: %s - %s" % (verdict, detail))
    print()
    if verdict == "ALIVE":
        print("The gate is in force. Its rules can be reported as satisfied.")
    elif verdict == "NO-EVIDENCE":
        print("This probe cannot settle it. The decisive test is not a file: when the hook runs, it "
              "returns text that the editor puts into the model's context, so every turn that the "
              "hook served carries a marker the model can see and this script cannot. Use that: if "
              "the turn carried no marker, the hook did not run, whatever the wiring says - and then "
              "the gate is not in force, its approval requirement is absent rather than met, and "
              "neither the protected list nor any rule should be reported as satisfied.")
    else:
        print("The gate is NOT in force in this session. Do not report its rules as satisfied, do not "
              "treat the approval requirement as met, and say this plainly in the answer rather than "
              "describing the gate as working.")
    print()
    print("Evidence is what this machine recorded, not a claim about intent. A missing signal is "
          "reported as a missing signal, never as a pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
