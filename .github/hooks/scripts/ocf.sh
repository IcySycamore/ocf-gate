#!/bin/sh
# ocf.sh - Work Control Flow hard orchestrator (state machine + gates) / POSIX sh.
# No third-party dependencies; semantics matched by ocf.ps1.
# Exit codes: 0 pass / 2 hard block (CLI mode); in hook mode use stdout permissionDecision instead.
#
# State machine: ready -> asking -> planning -> executing -> reporting -> ready    bypass: blocked
# Authorization does NOT use language recognition: the human runs approve / reject / confirm in their
# own terminal.
#
# INTENTIONALLY PURE ASCII: every string in this file is ASCII-only, for the same reason ocf.ps1 is.
# Do not reintroduce non-ASCII here.

set -u

SELF_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH= cd -- "$SELF_DIR/../../.." && pwd)

ORC="$ROOT/.orchestrator"
STATE_FILE="$ORC/state"
FACTS_FILE="$ORC/facts"
CONFIG_FILE="$ORC/config"
PLAN_FILE="$ORC/plan.md"
JOURNAL="$ORC/journal.log"
HUMAN_CODE="$ORC/human-code.txt"
ALLOWED_EDITS="$ORC/allowed-edits.txt"
EXEC_LOG="$ORC/exec.log"

STATES="ready asking planning executing reporting blocked"
AGENT_ADVANCE_TARGETS="asking planning reporting ready blocked"

# Default limits; all four can be overridden from .orchestrator/config (see get_limit below).
DEFAULT_MAX_CMD_LEN=400
DEFAULT_MAX_CMD_STMTS=3
DEFAULT_MAX_CMD_REPEAT=3
DEFAULT_FAIL_BUDGET=2

# Human-only subcommands. Writing one of these into an executable file is self-authorization.
HUMAN_ONLY_SUBCMD='approve|reject|confirm|allow|deny|human-code'

BOM=$(printf '\357\273\277')

HOOK_MODE=""

# JSON-safe: escape backslashes and double quotes, then drop EVERY control character (including tab),
# because a raw tab inside a JSON string produces invalid JSON and the hook output would be discarded,
# which would silently let the tool through.
json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr -d '\000-\037'; }

block() {
  if [ "$HOOK_MODE" = "PreToolUse" ]; then
    printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"%s"}}\n' "$(json_escape "$*")"
    exit 0
  fi
  printf 'OCF-BLOCK: %s\n' "$*" >&2
  exit 2
}
note() { printf 'OCF: %s\n' "$*" >&2; }
clip() { printf '%s' "$1" | cut -b "1-${2:-80}" | LC_ALL=C sed 's/[\200-\277]*$//'; }

# Strip a UTF-8 BOM that a human editor may have added, so the '^key=' anchors still match.
strip_bom() { LC_ALL=C sed "1s/^$BOM//"; }

cfg() {
  [ -f "$CONFIG_FILE" ] || return 0
  strip_bom < "$CONFIG_FILE" | sed -n "s/^$1=//p" | head -n1
}

# enforce=off = the human handed control back: skip "no action before approval" and let the machine
# edit gate code. Note the fail-safe direction: an unreadable config keeps enforcement ON.
enforced() { [ "$(cfg enforce)" = "off" ] && return 1; return 0; }

get_limit() {
  _v=$(cfg "$1")
  case "$_v" in ''|*[!0-9]*) _v=0 ;; esac
  if [ "$_v" -gt 0 ]; then printf '%s' "$_v"; else printf '%s' "$2"; fi
}

# Effective limits: config overrides the defaults. Read on every invocation, so changing them takes
# effect immediately without reloading the VS Code window.
MAX_CMD_LEN=$(get_limit max_cmd_len "$DEFAULT_MAX_CMD_LEN")
MAX_CMD_STMTS=$(get_limit max_cmd_stmts "$DEFAULT_MAX_CMD_STMTS")
MAX_CMD_REPEAT=$(get_limit max_cmd_repeat "$DEFAULT_MAX_CMD_REPEAT")
FAIL_BUDGET=$(get_limit fail_budget "$DEFAULT_FAIL_BUDGET")

init_store() {
  [ -d "$ORC" ] || mkdir -p "$ORC"
  [ -f "$STATE_FILE" ] || printf 'ready\n' > "$STATE_FILE"
  [ -f "$FACTS_FILE" ] || : > "$FACTS_FILE"
  [ -f "$JOURNAL" ] || : > "$JOURNAL"
  [ -f "$ALLOWED_EDITS" ] || : > "$ALLOWED_EDITS"
  [ -f "$EXEC_LOG" ] || : > "$EXEC_LOG"
  if [ ! -f "$CONFIG_FILE" ]; then
    cat > "$CONFIG_FILE" <<'OCF_EOF'
# Human-editable. enforce=on (default) = no file edits and no commands before approval; gate code is machine-protected.
# enforce=off = control handed back to the human: the approval requirement is skipped and the machine may edit gate code. Human-code protection still applies.
# Optional limit overrides (positive integers): max_cmd_len, max_cmd_stmts, max_cmd_repeat, fail_budget.
enforce=on
OCF_EOF
  fi
  if [ ! -f "$HUMAN_CODE" ]; then
    cat > "$HUMAN_CODE" <<'OCF_EOF'
# Protected path list - the machine never modifies these (one glob per line, '#' starts a comment).
CONTEXT.md
CONTEXT-MAP.md
docs/**
.github/copilot-instructions.md
# .github/hooks/**, .github/agents/**, .github/prompts/** and .github/skills/work-control-flow/**
# are covered by the self-protection gate; they are unlocked only while enforce=off.
# Add your own hand-written code here, e.g.:  src/human/**   legacy/**
OCF_EOF
  fi
}

state() {
  if [ -f "$STATE_FILE" ]; then
    _s=$(head -n1 "$STATE_FILE")
    printf '%s' "${_s#"$BOM"}"
  else
    printf 'ready'
  fi
}
set_state() { printf '%s\n' "$1" > "$STATE_FILE"; }

fact() {
  [ -f "$FACTS_FILE" ] || return 0
  strip_bom < "$FACTS_FILE" | sed -n "s/^$1=//p" | head -n1
}
set_fact() {
  init_store
  _k=$1; _v=$(printf '%s' "$2" | tr -d '\r\n')
  _tmp="$FACTS_FILE.tmp$$"
  awk -v k="$_k" -v v="$_v" 'BEGIN{d=0} $0 ~ ("^" k "="){print k "=" v; d=1; next} {print} END{if(!d) print k "=" v}' "$FACTS_FILE" > "$_tmp" && mv "$_tmp" "$FACTS_FILE"
}
log_event() {
  init_store
  _ts=$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date)
  printf '%s | %s | %s\n' "$_ts" "$(state)" "$*" >> "$JOURNAL"
}

relativize() {
  _p=$(printf '%s' "$1" | tr '\\' '/')
  _alt=''
  case "$ROOT" in
    /?/*) _alt="$(printf '%s' "$ROOT" | cut -c2 | tr 'A-Z' 'a-z'):$(printf '%s' "$ROOT" | cut -c3-)" ;;
  esac
  if [ -n "$_alt" ]; then
    case "$_p" in "$_alt"/*) _p="${_p#"$_alt"}" ;; esac
  fi
  case "$_p" in "$ROOT"/*) _p=${_p#"$ROOT"/} ;; esac
  while :; do
    case "$_p" in ./*) _p=${_p#./} ;; /*) _p=${_p#/} ;; *) break ;; esac
  done
  printf '%s' "$_p"
}

matches_list() {
  [ -f "$2" ] || return 1
  _first=1
  while IFS= read -r _pat; do
    if [ "$_first" = 1 ]; then _first=0; _pat=${_pat#"$BOM"}; fi
    case "$_pat" in ''|\#*) continue ;; esac
    case "$1" in $_pat) return 0 ;; esac
  done < "$2"
  return 1
}

# Single source of truth for "the orchestrator's own files". Previously the list was duplicated
# inline in three places and the copies drifted, which left .github/agents, .github/prompts and
# .github/skills/work-control-flow write-protected on one code path but wide open on the other.
is_self_protected() {
  case "$1" in
    .github/hooks/*|.github/skills/work-control-flow/*|.github/agents/*|.github/prompts/*) return 0 ;;
    .orchestrator/state|.orchestrator/facts|.orchestrator/config|.orchestrator/human-code.txt) return 0 ;;
    .orchestrator/allowed-edits.txt|.orchestrator/exec.log|.orchestrator/journal.log) return 0 ;;
  esac
  return 1
}

is_executable_ext() {
  case "$1" in
    *.ps1|*.psm1|*.sh|*.bash|*.bat|*.cmd|*.py|*.js|*.rb|*.pl) return 0 ;;
  esac
  return 1
}

compact_stdin() { tr -d '\r' | tr '\n' ' ' | sed 's/[[:space:]][[:space:]]*/ /g'; }
json_str() {
  sed 's/\\"/@@/g' | sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" | sed 's/@@/"/g' | head -n1
}

# Blank quoted spans and the whole of an @{ ... } hashtable literal, so only text that can carry a
# real statement separator survives. Used only for the statement count: a ';' inside a string, or
# between hashtable entries, is not a separator and counting it was a false positive. Everything
# else stays counted, including the inside of ( ) [ ] { } and of a script block. Security regexes
# still run on the raw command, since masking would hide a real threat inside a payload.
mask_toplevel() {
  awk '{
    out = ""; q = 0; hd = 0
    n = length($0); i = 1
    while (i <= n) {
      c = substr($0, i, 1)
      if (q != 0) { out = out " "; if (c == q) q = 0; i++; continue }
      if (c == "\"" || c == "\047") { q = c; out = out " "; i++; continue }
      if (hd > 0) {
        out = out " "
        if (c == "{") hd++
        else if (c == "}") hd--
        i++; continue
      }
      if (c == "@" && i < n && substr($0, i + 1, 1) == "{") { hd = 1; out = out "  "; i += 2; continue }
      out = out c
      i++
    }
    print out
  }'
}

need_fact() {
  _v=$(fact "$1")
  if [ -z "$_v" ] || [ "${#_v}" -lt "$3" ]; then
    block "GATE $2: fact '$1' is missing or too short (min $3 chars). $4"
  fi
}

gate() {
  case "$1" in
    context)
      need_fact goal context 4 "Ask the human for the goal. Never infer it."
      need_fact tools context 4 "Ask the human which tools are available (existing commands/scripts/services)."
      need_fact references context 4 "Ask the human for reference designs or workflows."
      need_fact deliverables context 4 "Ask the human for the expected result and deliverables."
      need_fact code_style context 4 "The coding style/conventions must be specified by the human."
      return 0 ;;
    docs-decision)
      case "$(fact docs_decision)" in
        create|skip) return 0 ;;
      esac
      block "GATE docs-decision: you have not asked whether to create CONTEXT.md / docs/adr. Ask first, then set docs_decision=create|skip." ;;
    grill-valid)
      _n=$(fact grill_rounds); _n=${_n:-0}
      case "$_n" in ''|*[!0-9]*) _n=0 ;; esac
      [ "$_n" -ge 1 ] || block "GATE grill-valid: no Q&A round completed yet (grill_rounds=$_n). Interview the human first, one question at a time."
      need_fact consensus grill-valid 10 "Write the agreed consensus into consensus (min 10 chars)."
      case "$(fact grill_used)" in
        with-docs|me) return 0 ;;
      esac
      block "GATE grill-valid: interview mode not declared. Use grill-with-docs when CONTEXT.md / docs/adr exist, otherwise grill-me; set grill_used=with-docs|me." ;;
    plan-schema)
      [ -f "$PLAN_FILE" ] || block "GATE plan-schema: .orchestrator/plan.md is missing."
      for _f in "## Type" "## Summary" "## Steps" "## Tools" "## Files" "## Scope" "## Deliverables" "## Self-review"; do
        grep -qF -- "$_f" "$PLAN_FILE" || block "GATE plan-schema: plan.md is missing the section '$_f'."
      done
      return 0 ;;
    zero-p0)
      [ "$(fact p0_count)" = "0" ] && return 0
      block "GATE zero-p0: P0 count is not zero (p0_count=$(fact p0_count); unset counts as not zero). Revise the plan until there is no P0. Do not just change the number." ;;
    human-code-clear)
      _c=$(count_conflicts)
      [ "$_c" -eq 0 ] && return 0
      block "GATE human-code-clear: plan.md touches $_c human-protected path(s) (list: .orchestrator/human-code.txt). The human must run the allow subcommand in their own terminal." ;;
    stack-env)
      need_fact stack_env stack-env 4 "When the task involves a toolchain the human must first declare the EXISTING environment/packages/runtime/version manager."
      return 0 ;;
    *) block "Unknown gate: $1" ;;
  esac
}

entry_gates() {
  case "$1" in
    planning)  gate context; gate docs-decision; gate grill-valid ;;
    executing) gate plan-schema; gate zero-p0; gate human-code-clear; gate stack-env ;;
    *) : ;;
  esac
}

transition_ok() {
  case "$1:$2" in
    ready:asking|ready:blocked) return 0 ;;
    asking:asking|asking:planning|asking:blocked) return 0 ;;
    planning:planning|planning:executing|planning:asking|planning:blocked) return 0 ;;
    executing:executing|executing:reporting|executing:blocked) return 0 ;;
    reporting:reporting|reporting:ready|reporting:blocked) return 0 ;;
    blocked:ready|blocked:asking|blocked:planning|blocked:executing|blocked:reporting) return 0 ;;
    *) return 1 ;;
  esac
}

count_conflicts() {
  init_store
  _c=0
  [ -f "$PLAN_FILE" ] || { printf '0'; return; }
  while IFS= read -r _pat; do
    case "$_pat" in ''|\#*) continue ;; esac
    _base=${_pat%%\**}
    if [ -z "$_base" ]; then
      # A pattern with no literal prefix, such as *.md, cannot be found by substring. Match it
      # against the path-like tokens in the plan instead of skipping it silently.
      _hit=0
      for _tok in $(grep -oE '`[^`]+`|[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*\.[A-Za-z0-9]{1,10}' "$PLAN_FILE" | tr -d '`'); do
        _norm=$(printf '%s' "$_tok" | tr '\\' '/')
        case "$_norm" in $_pat) _hit=1; break ;; esac
      done
      [ "$_hit" = 1 ] || continue
    else
      grep -qF -- "$_base" "$PLAN_FILE" || continue
    fi
    if [ -f "$ALLOWED_EDITS" ] && { grep -qxF -- "$_base" "$ALLOWED_EDITS" || grep -qxF -- "$_pat" "$ALLOWED_EDITS"; }; then continue; fi
    _c=$((_c+1))
  done < "$HUMAN_CODE"
  printf '%s' "$_c"
}

# ---------------------------------------------------------------------------
# Commands available to the agent (control plane)
# ---------------------------------------------------------------------------

cmd_init() { init_store; printf 'OCF initialized: %s\n' "$ORC"; cmd_status; }

cmd_status() {
  init_store
  printf 'state=%s\n' "$(state)"
  printf 'enforce=%s\n' "$(cfg enforce)"
  printf 'limits=cmd_len:%s,stmts:%s,repeat:%s,fail_budget:%s\n' "$MAX_CMD_LEN" "$MAX_CMD_STMTS" "$MAX_CMD_REPEAT" "$FAIL_BUDGET"
  printf 'human_code_conflict=%s\n' "$(count_conflicts)"
  if [ -s "$FACTS_FILE" ]; then
    printf '%s\n' '--- facts ---'
    grep -v '^$' "$FACTS_FILE"
  else
    printf '%s\n' '(no facts recorded yet)'
  fi
}

cmd_advance() {
  _to=${1:-}
  [ -n "$_to" ] || block "Usage: ocf.sh advance <state>"
  case " $STATES " in *" $_to "*) : ;; *) block "Unknown state: $_to (valid: $STATES)" ;; esac
  _from=$(state)
  [ "$_from" = "$_to" ] && { note "already in $_to"; return 0; }
  transition_ok "$_from" "$_to" || block "Illegal transition: $_from -> $_to. See references/system.md section 3."
  entry_gates "$_to"
  set_state "$_to"
  log_event "advance $_from -> $_to"
  printf 'state=%s\n' "$_to"
}

cmd_gate() { gate "${1:-}"; printf 'gate %s: PASS\n' "${1:-}"; }

cmd_set() {
  _k=${1:-}; _v=${2:-}
  [ -n "$_k" ] || block "Usage: ocf.sh set <key> <value>"
  case "$_k" in
    approved_by) block "OCF privilege gate: approved_by is only written by the human, via the approve subcommand." ;;
    must_consult) block "OCF privilege gate: must_consult only changes via the failure budget or a human reply." ;;
  esac
  case "$_k" in
    grill_rounds|p0_count) case "$_v" in ''|*[!0-9]*) block "$_k must be a non-negative integer" ;; esac ;;
  esac
  set_fact "$_k" "$_v"
  log_event "set $_k='$(clip "$_v" 60)'"
  printf '%s=%s\n' "$_k" "$_v"
}

cmd_journal() { [ -f "$JOURNAL" ] || { printf '(no audit records)\n'; return 0; }; tail -n "${1:-20}" "$JOURNAL"; }

cmd_fail() {
  _reason=${1:-}
  [ -n "$_reason" ] || block "Usage: ocf.sh fail \"<reason>\""
  _s=$(fact fail_streak); _s=${_s:-0}; case "$_s" in ''|*[!0-9]*) _s=0 ;; esac
  _t=$(fact fail_total); _t=${_t:-0}; case "$_t" in ''|*[!0-9]*) _t=0 ;; esac
  _s=$((_s+1)); _t=$((_t+1))
  set_fact fail_streak "$_s"
  set_fact fail_total "$_t"
  set_fact fail_last_reason "$(clip "$_reason" 200)"
  log_event "fail #$_s: $(clip "$_reason" 80)"
  if [ "$_s" -ge "$FAIL_BUDGET" ]; then
    set_fact must_consult yes
    note "OCF-REQUIRED: $_s consecutive failures -> must_consult=yes; execution tools are now hard-blocked."
    note "Stop and tell the human: symptom / what you already tried / what you need from them."
  fi
  printf 'fail_streak=%s\nmust_consult=%s\n' "$_s" "$(fact must_consult)"
}

cmd_ok() { set_fact fail_streak 0; set_fact must_consult no; log_event "ok (fail_streak -> 0)"; printf 'fail_streak=0\n'; }

# ---------------------------------------------------------------------------
# Human-only commands (the agent calling these is caught by guard_exec)
# ---------------------------------------------------------------------------

cmd_approve() {
  _why=${1:-}
  [ -n "$_why" ] || block "Usage: approve \"<reason>\" (run by the human in their own terminal)"
  [ "${#_why}" -ge 8 ] || block "OCF approval gate: the reason must be a real sentence (min 8 chars) so that an accidental or scripted call cannot approve a plan."
  [ "$(state)" = "planning" ] || block "approve only runs in the planning state (currently $(state))."
  entry_gates executing
  set_fact approved_by "$_why"
  set_fact approved_at "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date)"
  set_state executing
  log_event "APPROVE -> executing | $(clip "$_why" 120)"
  printf 'state=executing\n'
}

cmd_reject() {
  _why=${1:-}
  [ -n "$_why" ] || block "Usage: reject \"<reason>\" (run by the human in their own terminal)"
  [ "$(state)" = "planning" ] || block "reject only runs in the planning state (currently $(state))."
  set_state asking
  log_event "REJECT -> asking | $(clip "$_why" 120)"
  printf 'state=asking\n'
}

cmd_confirm() {
  [ "$(state)" = "asking" ] || block "confirm only runs in the asking state (currently $(state))."
  entry_gates planning
  set_state planning
  log_event "CONFIRM -> planning (human command)"
  printf 'state=planning\n'
}

cmd_human_code() {
  init_store
  _p=$(relativize "${1:-}")
  [ -n "$_p" ] || block "Usage: ocf.sh human-code <path-or-glob>"
  grep -qxF "$_p" "$HUMAN_CODE" 2>/dev/null || printf '%s\n' "$_p" >> "$HUMAN_CODE"
  log_event "human-code += $_p"
  printf 'protected=%s\n' "$_p"
}

cmd_allow() {
  init_store
  _p=$(relativize "${1:-}")
  [ -n "$_p" ] || block "Usage: ocf.sh allow <path-or-glob>"
  grep -qxF "$_p" "$ALLOWED_EDITS" 2>/dev/null || printf '%s\n' "$_p" >> "$ALLOWED_EDITS"
  log_event "allow += $_p"
  printf 'allowed=%s\n' "$_p"
}

cmd_deny() {
  init_store
  _p=$(relativize "${1:-}")
  [ -f "$ALLOWED_EDITS" ] || return 0
  _tmp="$ALLOWED_EDITS.tmp$$"
  grep -vxF "$_p" "$ALLOWED_EDITS" > "$_tmp" 2>/dev/null || : > "$_tmp"
  mv "$_tmp" "$ALLOWED_EDITS"
  log_event "allow -= $_p"
  printf 'revoked=%s\n' "$_p"
}

# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

guard_edit() {
  _rel=$(relativize "$1")
  [ "$_rel" = ".orchestrator/plan.md" ] && return 0

  # Self-authorization guard. guard_exec inspects the command STRING, so an agent could otherwise
  # dodge it by writing a small executable file that invokes the control script and then running that
  # file: the executed command line would contain neither the script path nor the subcommand name.
  # Two scope limits, both learned the hard way when this guard blocked a legitimate edit:
  #   - only while enforcement is on: enforce=off is documented as handing full control back to the
  #     human, which has to include editing the gate scripts themselves;
  #   - never for .github/hooks/scripts/**, because that IS the implementation of these subcommands.
  if enforced && is_executable_ext "$_rel"; then
    case "$_rel" in
      .github/hooks/scripts/*) : ;;
      *)
        # Scan the WHOLE tool input, not just content/newString: multi_replace_string_in_file nests
        # its text under replacements[].newString and edit_notebook_file uses newCode.
        if printf '%s' "${2:-}" | grep -Eqi "ocf\.(ps1|sh).{0,300}?($HUMAN_ONLY_SUBCMD)"; then
          block "OCF self-authorization gate: writing a human-only subcommand into an executable file is not allowed ($_rel). Only the human may approve, from their own terminal."
        fi ;;
    esac
  fi

  if enforced; then
    if is_self_protected "$_rel"; then
      block "OCF self-protection gate: editing the orchestrator's own state or gate files is not allowed ($_rel). Either the human edits it by hand, or the human sets enforce=off in .orchestrator/config."
    fi
  fi

  if matches_list "$_rel" "$HUMAN_CODE" && ! matches_list "$_rel" "$ALLOWED_EDITS"; then
    block "OCF human-code gate: editing human code or human docs is not allowed ($_rel). The human must grant access from their own terminal first."
  fi

  if enforced; then
    case "$(state)" in
      executing|reporting) return 0 ;;
    esac
    block "OCF approval gate: state=$(state), not approved, so file edits are blocked ($_rel). To approve, the human runs the approve subcommand in their own terminal."
  fi
}

guard_exec() {
  _cmd=$1

  if printf '%s' "$_cmd" | grep -Eiq '(headless|--screenshot|screenshot|playwright|puppeteer|capture.*(image|screen))'; then
    block "OCF visual-test gate: the machine may never run visual or screenshot tests. Command: $(clip "$_cmd" 120). Take the screenshot yourself and attach it to the conversation."
  fi

  if [ "$(fact must_consult)" = "yes" ]; then
    block "OCF failure-budget gate: $(fact fail_streak) consecutive failures (last: $(fact fail_last_reason)). Stop and ask the human: symptom / what you tried / what you need. Do not keep retrying. Clears automatically once the human replies."
  fi

  # Orchestrator control plane: subcommands the agent may use. Authorization subcommands and
  # 'advance executing' are human-only.
  # Control plane is recognised only when EVERY statement of the command invokes the control script
  # by its full relative path. Merely mentioning the name is not enough: with a substring match,
  # `Write-Host "ocf.ps1"; <anything>` inherited the exemption and skipped the approval gate.
  _isocf=0
  _bad=$(printf '%s' "$_cmd" | sed 's/&&/;/g; s/||/;/g' | tr ';' '\n' | awk 'NF && $0 !~ /\.github[\\/]hooks[\\/]scripts[\\/]ocf\.(ps1|sh)/' | wc -l | tr -d ' ')
  if [ "$_bad" = "0" ]; then
    _isocf=1
    if printf '%s' "$_cmd" | grep -Eiq "($HUMAN_ONLY_SUBCMD)"; then
      block "OCF self-authorization gate: the authorization subcommands may only be run by the human in their own terminal."
    fi
    _tgt=$(printf '%s' "$_cmd" | grep -oiE 'advance[[:space:]]+[A-Za-z-]+' | head -n1 | sed 's/.*[[:space:]]//' | tr 'A-Z' 'a-z')
    if [ -n "$_tgt" ]; then
      case " $AGENT_ADVANCE_TARGETS " in
        *" $_tgt "*) : ;;
        *) block "OCF self-authorization gate: the agent may not advance to $_tgt. Entering executing requires the human to approve from their own terminal." ;;
      esac
    fi
  fi

  if enforced && [ "$_isocf" = 0 ]; then
    case "$(state)" in
      executing|reporting) : ;;
      *) block "OCF approval gate: state=$(state), not approved, so this command is blocked: $(clip "$_cmd" 120). To approve, the human runs the approve subcommand in their own terminal." ;;
    esac
  fi

  _len=$(printf '%s' "$_cmd" | wc -c | tr -d ' ')
  if [ "$_len" -gt "$MAX_CMD_LEN" ]; then
    block "OCF observability gate: command too long (${_len} > ${MAX_CMD_LEN} bytes; the sh counts bytes, the ps1 counts characters). Split it into short single-purpose commands."
  fi
  # Count statements on the quote-masked copy (see mask_quotes).
  _stmts=$(printf '%s' "$_cmd" | mask_toplevel | sed 's/&&/;/g; s/||/;/g' | tr -cd ';' | wc -c | tr -d ' ')
  _stmts=$((_stmts + 1))
  if [ "$_stmts" -gt "$MAX_CMD_STMTS" ]; then
    block "OCF observability gate: $_stmts statements chained into one command (limit $MAX_CMD_STMTS). Split them and run one at a time."
  fi
  if printf '%s' "$_cmd" | grep -Eiq 'Out-Null|-Quiet|--quiet|>[[:space:]]*\$null|2>[[:space:]]*\$null|/dev/null|-WindowStyle[[:space:]]+Hidden'; then
    block 'OCF observability gate: the command silences its output (Out-Null / $null / /dev/null / --quiet). Everything must stay visible to the human.'
  fi
  if printf '%s' "$_cmd" | grep -Eiq 'Read-Host|ReadKey|-Verb[[:space:]]+RunAs|(^|[^a-z])sudo[[:space:]]|cmd(\.exe)?[[:space:]]+/c'; then
    block 'OCF observability gate: the command may block on interactive input or raise a dialog. Rewrite it non-interactively; anything needing elevation or a click must be run by the human.'
  fi

  if printf '%s' "$_cmd" | grep -Eiq '(^|[^a-z])(npm|pnpm|yarn|bun)[[:space:]]+(run[[:space:]]+)?test|(^|[^a-z])(jest|vitest|mocha|pytest|tox|ctest|rspec)([^a-z]|$)|(^|[^a-z])(dotnet|go|cargo|mvn|mvnw|gradle|gradlew|phpunit)[[:space:]].*test'; then
    [ "$(fact test_authorized)" = "yes" ] || block "OCF test gate: do not run tests on your own. Only after the human asks in this conversation, set test_authorized yes. Command: $(clip "$_cmd" 120)"
  fi

  if printf '%s' "$_cmd" | grep -Eiq '(^|[^a-z])(npm|pnpm|yarn|bun|pip|pip3|conda|mamba|poetry|uv|gem|composer|cargo|go|dotnet|apt|apt-get|brew|choco|scoop|winget|pacman|npx|dnf|yum)[[:space:]]+(install|add|get|i|upgrade|update|env|create)|(^|[^a-z])npx[[:space:]]'; then
    [ -n "$(fact stack_env)" ] || block "OCF stack gate: the human has not declared the existing environment/packages/runtime/version manager. Do not install or probe on your own. Command: $(clip "$_cmd" 120)"
  fi

  if printf '%s' "$_cmd" | grep -Eiq 'rm[[:space:]]+-rf[[:space:]]+/[^.]|git[[:space:]]+push[[:space:]]+.*--force|drop[[:space:]]+table|git[[:space:]]+reset[[:space:]]+--hard|Remove-Item.*-Recurse.*-Force'; then
    block "OCF safety gate: destructive command detected and blocked: $(clip "$_cmd" 120)"
  fi

  # Write targets are covered by human-code protection too. Only commands that look like writes are
  # path-scanned, so ordinary reads are not caught by accident.
  if printf '%s' "$_cmd" | grep -Eiq '(Set-Content|Add-Content|Out-File|New-Item|Remove-Item|Move-Item|Copy-Item|Rename-Item|tee|sed[[:space:]]+-i|truncate|dd)([[:space:]]|$)|>[[:space:]]*[^[:space:]]'; then
    for _tok in $(printf '%s' "$_cmd" | grep -oE '[A-Za-z0-9_./\\-]+' | grep '[\\/]'); do
      _rp=$(relativize "$_tok")
      [ -n "$_rp" ] || continue
      if enforced; then
        if is_self_protected "$_rp"; then
          block "OCF self-protection gate: a command may not write to the orchestrator's own state or gate files ($_rp). The human must edit it by hand."
        fi
      fi
      if matches_list "$_rp" "$HUMAN_CODE" && ! matches_list "$_rp" "$ALLOWED_EDITS"; then
        block "OCF human-code gate: the command writes to a protected path ($_rp). Changing human code or docs is the human's job, or they must grant access first."
      fi
    done
  fi

  # Going in circles: the same command over and over -> hand the decision to the human.
  _norm=$(printf '%s' "$_cmd" | tr -d '[:space:]')
  _cnt=0
  if [ -f "$EXEC_LOG" ]; then
    _cnt=$(grep -cxF -- "$_norm" "$EXEC_LOG" 2>/dev/null || true)
    [ -n "$_cnt" ] || _cnt=0
  fi
  if [ "$_cnt" -ge "$MAX_CMD_REPEAT" ]; then
    printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"ask","permissionDecisionReason":"OCF iteration gate: this is about to be execution #%s of the exact same command, which suggests you are stuck in a loop. The human decides whether to continue or change approach."}}\n' "$((_cnt+1))"
    exit 0
  fi
  printf '%s\n' "$_norm" >> "$EXEC_LOG"
  return 0
}

# Tools that perform an action but are not expressed as a shell command, so guard_exec cannot see
# them. While no action is authorised they must not run.
guard_action() {
  enforced || return 0
  case "$(state)" in
    executing|reporting) return 0 ;;
  esac
  block "OCF approval gate: '$1' performs an action (installs / debugs / launches) while state=$(state), which is not approved. The human approves from their own terminal."
}

# Unknown tools: fail-safe instead of fail-open. Without this, a newly installed extension or MCP
# server would silently become an unguarded write/exec path. Only names that look like they mutate
# or execute something are escalated, so purely read-oriented tools keep working without friction.
guard_unknown_tool() {
  [ -n "${1:-}" ] || return 0
  enforced || return 0
  printf '%s' "$1" | grep -Eqi '(create|update|write|edit|delete|remove|push|upload|run|exec|install|apply|move|rename|replace|set|launch|start|debug|fork|merge|publish|send|post|commit|patch|deploy)' || return 0
  case "$(state)" in
    executing|reporting) return 0 ;;
  esac
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"ask","permissionDecisionReason":"OCF unknown-tool gate: %s looks like it performs an action, but it is not in the known tool lists and the current state is not approved. Confirm whether it may run."}}\n' "$(json_escape "$1")"
  exit 0
}

cmd_hook_user_prompt() {
  init_store
  if [ -n "${1:-}" ]; then _json=$1; else _json=$(compact_stdin); fi
  _p=$(printf '%s' "$_json" | json_str prompt)
  _st=$(state)
  unset _json || :

  # A human reply counts as manual intervention, which clears the failure budget.
  if [ "$(fact must_consult)" = "yes" ]; then
    set_fact must_consult no
    set_fact fail_streak 0
    set_fact last_consult_answer "$(clip "$_p" 400)"
    log_event "consult answered (human intervention)"
    printf '{"systemMessage":"OCF: the human has stepped in, must_consult cleared. First restate your understanding of the instruction, then re-plan."}\n'
    exit 0
  fi

  case "$_st" in
    ready)
      set_fact first_prompt "$(clip "$_p" 400)"
      set_state asking
      log_event "advance ready -> asking (first human message)"
      printf '{"systemMessage":"OCF: entering asking. Interview the human first (one question at a time) and record facts: goal / tools / references / deliverables / code_style / docs_decision / stack_env / grill_used. Once consensus is reached write consensus, then ocf advance planning."}\n'
      exit 0 ;;
    asking)
      _n=$(fact grill_rounds); _n=${_n:-0}; case "$_n" in ''|*[!0-9]*) _n=0 ;; esac
      _n=$((_n+1))
      set_fact grill_rounds "$_n"
      log_event "human reply #$_n"
      exit 0 ;;
  esac
  exit 0
}

cmd_hook_pre_tool() {
  init_store
  if [ -n "${1:-}" ]; then _json=$1; else _json=$(compact_stdin); fi
  _tool=$(printf '%s' "$_json" | json_str tool_name)
  _path=$(printf '%s' "$_json" | json_str filePath)
  [ -n "$_path" ] || _path=$(printf '%s' "$_json" | json_str path)
  _cmd=$(printf '%s' "$_json" | json_str command)
  [ -n "$_cmd" ] || _cmd=$(printf '%s' "$_json" | json_str code)
  [ -n "$_cmd" ] || _cmd=$(printf '%s' "$_json" | json_str function)
  _handled=0

  case "$_tool" in
    screenshot_page|view_image|run_playwright_code|mcp_playwright_browser_take_screenshot|mcp_playwright_browser_run_code_unsafe)
      _handled=1
      block "OCF visual-test gate: the machine may never run visual or screenshot tests (tool: $_tool). Take the screenshot yourself and attach it in the next message. Attachments you provide are readable; the tool is not." ;;
  esac

  case "$_tool" in
    replace_string_in_file|multi_replace_string_in_file|edit_notebook_file|create_file|vscode_renameSymbol|mcp_github_mcp_se_create_or_update_file|mcp_github_mcp_se_delete_file|mcp_github_mcp_se_push_files|mcp_github_mcp_se_fork_repository)
      _handled=1
      [ -n "$_path" ] && guard_edit "$_path" "$_json" ;;
  esac

  case "$_tool" in
    run_in_terminal|run_notebook_cell|create_and_run_task|mcp_playwright_browser_run_code_unsafe|mcp_playwright_browser_evaluate)
      _handled=1
      [ -n "$_cmd" ] && guard_exec "$_cmd" ;;
  esac

  case "$_tool" in
    install_python_packages|install_extension|debug_java_application|configure_python_environment|create_new_workspace|create_new_jupyter_notebook|create_and_run_task)
      _handled=1
      guard_action "$_tool" ;;
  esac

  # Explicitly allowed: read-only or coordination tools whose names would otherwise trip the
  # action-verb heuristic. Dispatching the auditor subagent during planning is mandatory, so it must
  # never be escalated.
  case "$_tool" in
    runSubagent|manage_todo_list|vscode_askQuestions|memory) _handled=1 ;;
  esac

  [ "$_handled" = 1 ] || guard_unknown_tool "$_tool"

  exit 0
}

cmd_hook_dispatch() {
  init_store
  _json=$(compact_stdin)
  _ev=$(printf '%s' "$_json" | json_str hook_event_name)
  [ -n "$_ev" ] || _ev=${1:-}
  HOOK_MODE=$_ev
  case "$_ev" in
    UserPromptSubmit) cmd_hook_user_prompt "$_json" ;;
    PreToolUse)       cmd_hook_pre_tool "$_json" ;;
    *) exit 0 ;;
  esac
}

_cmd=${1:-status}
[ $# -gt 0 ] && shift || :

case "$_cmd" in
  init)          cmd_init "$@" ;;
  status)        cmd_status "$@" ;;
  advance)       cmd_advance "$@" ;;
  gate)          cmd_gate "$@" ;;
  set)           cmd_set "$@" ;;
  journal)       cmd_journal "$@" ;;
  fail)          cmd_fail "$@" ;;
  ok)            cmd_ok "$@" ;;
  approve)       cmd_approve "$@" ;;
  reject)        cmd_reject "$@" ;;
  confirm)       cmd_confirm "$@" ;;
  human-code)    cmd_human_code "$@" ;;
  allow)         cmd_allow "$@" ;;
  deny)          cmd_deny "$@" ;;
  hook-dispatch) cmd_hook_dispatch "$@" ;;
  *)
    printf 'Usage: ocf.sh <init|status|advance|gate|set|journal|fail|ok> [args]         # agent\n' >&2
    printf '       ocf.sh <approve|reject|confirm|allow|deny|human-code> [args]      # human only\n' >&2
    exit 1 ;;
esac
