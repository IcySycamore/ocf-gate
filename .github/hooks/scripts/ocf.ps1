# ocf.ps1 - Work Control Flow hard orchestrator (state machine + gates) / Windows side.
# Constraints: no third-party dependencies; Windows PowerShell 5.1 compatible; semantics matched by ocf.sh.
# Exit codes: 0 pass / 2 hard block (CLI mode); in hook mode use stdout permissionDecision instead.
#
# State machine: ready -> asking -> planning -> executing -> reporting -> ready    bypass: blocked
# Authorization does NOT use language recognition: the human runs approve / reject / confirm
# in their own terminal.
#
# INTENTIONALLY PURE ASCII: every string in this file is ASCII-only. This removes the whole class
# of encoding bugs (BOM-less UTF-8 read as ANSI/cp936 under PowerShell 5.1, newline-swallowing by
# trailing multi-byte characters, JSON escaping failures). Do not reintroduce non-ASCII here.

[CmdletBinding()]
param(
  [Parameter(Position = 0)][string]$Command = 'status',
  [Parameter(Position = 1, ValueFromRemainingArguments = $true)][string[]]$Rest
)

$ErrorActionPreference = 'Continue'

$SelfDir = $PSScriptRoot
$Root = (Resolve-Path -LiteralPath (Join-Path $SelfDir '..\..\..')).Path

$Orc = Join-Path $Root '.orchestrator'
$StateFile = Join-Path $Orc 'state'
$FactsFile = Join-Path $Orc 'facts'
$ConfigFile = Join-Path $Orc 'config'
$PlanFile = Join-Path $Orc 'plan.md'
$Journal = Join-Path $Orc 'journal.log'
$HumanCode = Join-Path $Orc 'human-code.txt'
$AllowedEdits = Join-Path $Orc 'allowed-edits.txt'
$ExecLog = Join-Path $Orc 'exec.log'

$States = @('ready', 'asking', 'planning', 'executing', 'reporting', 'blocked')
# Targets the agent may advance to on its own; the rest (especially executing) are human-only.
$AgentAdvanceTargets = @('asking', 'planning', 'reporting', 'ready', 'blocked')

# Default limits. Every one of these can be overridden from .orchestrator/config
# (keys: max_cmd_len / max_cmd_stmts / max_cmd_repeat / fail_budget) WITHOUT reloading the window,
# because this script is re-read on every invocation.
$DefaultMaxCmdLen = 400
$DefaultMaxCmdStmts = 3
$DefaultMaxCmdRepeat = 3
$DefaultFailBudget = 2

# Single source of truth for "the orchestrator's own files". Both Guard-Edit and Guard-Exec consult
# this - previously the list was duplicated inline in three places and the copies drifted apart,
# which left .github/agents, .github/prompts and .github/skills/work-control-flow write-protected
# on one code path but wide open on the other.
$SelfProtGlobs = @(
  '.github/hooks/*',
  '.github/skills/work-control-flow/*',
  '.github/agents/*',
  '.github/prompts/*'
)
$SelfProtExact = @(
  '.orchestrator/state', '.orchestrator/facts', '.orchestrator/config',
  '.orchestrator/human-code.txt', '.orchestrator/allowed-edits.txt',
  '.orchestrator/exec.log', '.orchestrator/journal.log'
)

# Extension of files that can actually be executed. Used to catch an agent trying to smuggle
# a human-only command into a script it writes and then runs.
$ExecutableExtensions = @('.ps1', '.psm1', '.sh', '.bash', '.bat', '.cmd', '.py', '.js', '.rb', '.pl')
# Human-only subcommands. Writing one of these into an executable file is self-authorization.
$HumanOnlySubcommands = 'approve|reject|confirm|allow|deny|human-code'

$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
try { [Console]::OutputEncoding = $Utf8NoBom } catch { }
try { [Console]::InputEncoding = $Utf8NoBom } catch { }

$script:HookMode = ''

function Write-StdoutUtf8([string]$Text) { [Console]::Out.WriteLine($Text); [Console]::Out.Flush() }
function Write-StderrUtf8([string]$Text) { [Console]::Error.WriteLine($Text); [Console]::Error.Flush() }

function Block([string]$Message) {
  # In hook mode the blocking decision MUST go to stdout as JSON: stderr plus a non-2 exit code only
  # shows up as a non-blocking warning and the tool still runs.
  if ($script:HookMode -eq 'PreToolUse') {
    $p = @{ hookSpecificOutput = @{ hookEventName = 'PreToolUse'; permissionDecision = 'deny'; permissionDecisionReason = $Message } }
    Write-StdoutUtf8 ($p | ConvertTo-Json -Compress -Depth 5)
    [Environment]::Exit(0)
  }
  Write-StderrUtf8 "OCF-BLOCK: $Message"
  [Console]::Out.Flush()
  [Console]::Error.Flush()
  [Environment]::Exit(2)
}

function Note([string]$Message) { Write-StderrUtf8 "OCF: $Message" }

function Clip([string]$Text, [int]$Max = 80) {
  if ([string]::IsNullOrEmpty($Text)) { return '' }
  if ($Text.Length -le $Max) { return $Text }
  return $Text.Substring(0, $Max) + '...'
}

function Write-TextFile([string]$Path, [string]$Text) { [System.IO.File]::WriteAllText($Path, $Text, $Utf8NoBom) }

# ---------------------------------------------------------------------------
# Encodingsafe file readers
# ---------------------------------------------------------------------------
# Every orchestrator-owned file is written as UTF-8 WITHOUT a BOM. PowerShell 5.1 (the interpreter
# the hooks actually run) decodes BOM-less files as ANSI/cp936 when no encoding is given, which
# mangles non-ASCII text, can swallow a newline when a multi-byte character sits at end-of-line,
# and silently breaks "replace the existing key" logic. Always read through these two helpers.
# They also tolerate a BOM, because humans edit config / human-code.txt in editors that add one -
# a BOM used to make '^enforce=' fail to match, which silently ignored the human's enforce=off.
function Read-AllLines([string]$Path) {
  if (-not (Test-Path -LiteralPath $Path)) { return @() }
  try {
    $lines = [System.IO.File]::ReadAllLines($Path, $Utf8NoBom)
  }
  catch { return @() }
  if ($lines.Count -gt 0) { $lines[0] = "$($lines[0])".TrimStart([char]0xFEFF) }
  return $lines
}

function Read-AllText([string]$Path) {
  if (-not (Test-Path -LiteralPath $Path)) { return '' }
  try { $t = [System.IO.File]::ReadAllText($Path, $Utf8NoBom) } catch { return '' }
  return "$t".TrimStart([char]0xFEFF)
}

function Get-Limit([string]$Key, [int]$Default) {
  $v = 0
  if ([int]::TryParse((Get-Config $Key), [ref]$v) -and $v -gt 0) { return $v }
  return $Default
}

function Test-SelfProtected([string]$RelPath) {
  foreach ($g in $SelfProtGlobs) { if ($RelPath -like $g) { return $true } }
  return ($SelfProtExact -contains $RelPath)
}

# Blanks quoted spans and the whole of an @{ ... } hashtable literal, so only text that can carry a
# real statement separator survives. Used ONLY for the statement-count check: a ';' inside a string,
# or between hashtable entries, is not a statement separator and counting it was a false positive.
# Everything else stays counted, including the inside of ( ) [ ] { } and of a script block, so an
# unbalanced bracket can no longer blank the rest of the command. Security regexes deliberately
# still run on the raw command, since masking would hide a real threat inside a payload.
function Mask-TopLevel([string]$Text) {
  if ([string]::IsNullOrEmpty($Text)) { return '' }
  $sb = New-Object System.Text.StringBuilder
  $q = [char]0
  $hd = 0
  $chars = $Text.ToCharArray()
  for ($i = 0; $i -lt $chars.Length; $i++) {
    $ch = $chars[$i]
    if ($q -ne [char]0) {
      [void]$sb.Append(' ')
      if ($ch -eq $q) { $q = [char]0 }
      continue
    }
    if ($ch -eq "'" -or $ch -eq '"') { $q = $ch; [void]$sb.Append(' '); continue }
    if ($hd -gt 0) {
      [void]$sb.Append(' ')
      if ($ch -eq '{') { $hd++ } elseif ($ch -eq '}') { $hd-- }
      continue
    }
    if ($ch -eq '@' -and ($i + 1) -lt $chars.Length -and $chars[$i + 1] -eq '{') {
      $hd = 1
      [void]$sb.Append('  ')
      $i++
      continue
    }
    [void]$sb.Append($ch)
  }
  return $sb.ToString()
}

# Effective limits are resolved further down, once Get-Config is defined.

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

function Get-Config([string]$Key) {
  $m = [regex]::Matches((Read-AllText $ConfigFile), "(?m)^" + [regex]::Escape($Key) + "=(.*)$")
  if ($m.Count -eq 0) { return '' }
  return "$($m[$m.Count - 1].Groups[1].Value)".Trim()
}

# enforce=off = the human handed control back: skip "no action before approval" and let the machine
# edit gate code. Human-code protection still applies. Note the fail-safe direction: an unreadable
# or missing config keeps enforcement ON.
function Test-Enforced { return ((Get-Config 'enforce') -ne 'off') }

# Effective limits: .orchestrator/config overrides the built-in defaults. Resolved here (not at the
# top of the file) because it depends on Get-Config.
$MaxCmdLen = Get-Limit 'max_cmd_len' $DefaultMaxCmdLen
$MaxCmdStmts = Get-Limit 'max_cmd_stmts' $DefaultMaxCmdStmts
$MaxCmdRepeat = Get-Limit 'max_cmd_repeat' $DefaultMaxCmdRepeat
$FailBudget = Get-Limit 'fail_budget' $DefaultFailBudget

function Initialize-Store {
  if (-not (Test-Path -LiteralPath $Orc)) { New-Item -ItemType Directory -Path $Orc -Force | Out-Null }
  if (-not (Test-Path -LiteralPath $StateFile)) { Write-TextFile $StateFile "ready`n" }
  if (-not (Test-Path -LiteralPath $FactsFile)) { Write-TextFile $FactsFile '' }
  if (-not (Test-Path -LiteralPath $Journal)) { Write-TextFile $Journal '' }
  if (-not (Test-Path -LiteralPath $AllowedEdits)) { Write-TextFile $AllowedEdits '' }
  if (-not (Test-Path -LiteralPath $ExecLog)) { Write-TextFile $ExecLog '' }
  if (-not (Test-Path -LiteralPath $ConfigFile)) {
    Write-TextFile $ConfigFile "# Human-editable. enforce=on (default) = no file edits and no commands before approval; gate code is machine-protected.`n# enforce=off = control handed back to the human: the approval requirement is skipped and the machine may edit gate code. Human-code protection still applies.`n# Optional limit overrides (positive integers): max_cmd_len, max_cmd_stmts, max_cmd_repeat, fail_budget.`nenforce=on`n"
  }
  if (-not (Test-Path -LiteralPath $HumanCode)) {
    $d = @(
      '# Protected path list - the machine never modifies these (one glob per line, # starts a comment).',
      'CONTEXT.md',
      'CONTEXT-MAP.md',
      'docs/**',
      '.github/copilot-instructions.md',
      '# .github/hooks/**, .github/agents/**, .github/prompts/** and .github/skills/work-control-flow/**',
      '# are covered by the self-protection gate; they are unlocked only while enforce=off.',
      '# Add your own hand-written code here, e.g.:  src/human/**   legacy/**'
    ) -join "`n"
    Write-TextFile $HumanCode ($d + "`n")
  }
}

function Get-State {
  Initialize-Store
  $lines = @(Read-AllLines $StateFile)
  if ($lines.Count -eq 0) { return 'ready' }
  $v = "$($lines[0])".Trim()
  if ([string]::IsNullOrEmpty($v)) { return 'ready' }
  return $v
}

function Set-State([string]$Value) { Write-TextFile $StateFile ($Value + "`n") }

function Get-Fact([string]$Key) {
  $m = [regex]::Matches((Read-AllText $FactsFile), "(?m)^" + [regex]::Escape($Key) + "=(.*)$")
  if ($m.Count -eq 0) { return '' }
  return "$($m[$m.Count - 1].Groups[1].Value)".Trim()
}

function Set-Fact([string]$Key, [string]$Value) {
  Initialize-Store
  $v = ("$Value" -replace "[\r\n]", '').Trim()
  $out = New-Object System.Collections.Generic.List[string]
  $found = $false
  foreach ($l in @(Read-AllLines $FactsFile)) {
    if ($l -match ("^" + [regex]::Escape($Key) + "=")) { $out.Add("$Key=$v"); $found = $true } else { $out.Add($l) }
  }
  if (-not $found) { $out.Add("$Key=$v") }
  Write-TextFile $FactsFile (($out -join "`n") + "`n")
}

function Add-Journal([string]$Message) {
  Initialize-Store
  $ts = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
  [System.IO.File]::AppendAllText($Journal, "$ts | $(Get-State) | $Message`n", $Utf8NoBom)
}

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

function Get-RelativePath([string]$Path) {
  if ([string]::IsNullOrWhiteSpace($Path)) { return '' }
  $p = $Path.Replace('\', '/')
  $r = $Root.Replace('\', '/')
  if ($p.StartsWith($r + '/', [System.StringComparison]::OrdinalIgnoreCase)) { $p = $p.Substring($r.Length + 1) }
  while ($p.StartsWith('./')) { $p = $p.Substring(2) }
  while ($p.StartsWith('/')) { $p = $p.Substring(1) }
  return $p
}

function Test-InList([string]$RelPath, [string]$ListFile) {
  foreach ($raw in @(Read-AllLines $ListFile)) {
    $pat = "$raw".Trim()
    if ([string]::IsNullOrEmpty($pat) -or $pat.StartsWith('#')) { continue }
    $rx = ConvertTo-PathRegex $pat
    if ($RelPath -match $rx) { return $true }
  }
  return $false
}

# ---------------------------------------------------------------------------
# Hook input
# ---------------------------------------------------------------------------

function Get-Field($Json, [string]$Name) {
  if ($null -eq $Json) { return '' }
  $v = $Json.$Name
  if ($null -eq $v) { return '' }
  return "$v"
}

function Get-ToolField($Json, [string]$Name) {
  if ($null -eq $Json) { return '' }
  if ($null -ne $Json.tool_input) {
    $v = $Json.tool_input.$Name
    if ($null -ne $v) { return "$v" }
  }
  return (Get-Field $Json $Name)
}

function Write-HookMessage([string]$Message) {
  Write-StdoutUtf8 (@{ systemMessage = $Message } | ConvertTo-Json -Compress)
}

function Read-HookInput {
  try {
    $reader = New-Object System.IO.StreamReader([Console]::OpenStandardInput(), $Utf8NoBom, $true)
    $raw = $reader.ReadToEnd()
    $reader.Dispose()
    if ([string]::IsNullOrWhiteSpace($raw)) { return $null }
    return ($raw | ConvertFrom-Json)
  }
  catch { return $null }
}

# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

# The 8 required section headings of .orchestrator/plan.md. Kept as ASCII markdown headings so the
# gate greps something unambiguous - the previous Chinese bare words could match prose by accident.
$PlanFields = @(
  '## Type', '## Summary', '## Steps', '## Tools',
  '## Files', '## Scope', '## Deliverables', '## Self-review'
)

function Assert-Fact([string]$Key, [string]$GateName, [int]$MinLen, [string]$Hint) {
  $v = Get-Fact $Key
  if ([string]::IsNullOrWhiteSpace($v) -or $v.Trim().Length -lt $MinLen) {
    Block "GATE $GateName`: fact '$Key' is missing or too short (min $MinLen chars). $Hint"
  }
}

function Invoke-Gate([string]$Name) {
  switch ($Name) {

    'context' {
      Assert-Fact 'goal' 'context' 4 'Ask the human for the goal. Never infer it.'
      Assert-Fact 'tools' 'context' 4 'Ask the human which tools are available (existing commands/scripts/services).'
      Assert-Fact 'references' 'context' 4 'Ask the human for reference designs or workflows.'
      Assert-Fact 'deliverables' 'context' 4 'Ask the human for the expected result and deliverables.'
      Assert-Fact 'code_style' 'context' 4 'The coding style/conventions must be specified by the human.'
      return
    }

    'docs-decision' {
      $v = Get-Fact 'docs_decision'
      if ($v -eq 'create' -or $v -eq 'skip') { return }
      Block 'GATE docs-decision: you have not asked whether to create CONTEXT.md / docs/adr. Ask first, then set docs_decision=create|skip.'
    }

    'grill-valid' {
      $n = 0; [int]::TryParse((Get-Fact 'grill_rounds'), [ref]$n) | Out-Null
      if ($n -lt 1) { Block "GATE grill-valid: no Q&A round completed yet (grill_rounds=$n). Interview the human first, one question at a time." }
      Assert-Fact 'consensus' 'grill-valid' 10 'Write the agreed consensus into consensus (min 10 chars).'
      $u = Get-Fact 'grill_used'
      if ($u -ne 'with-docs' -and $u -ne 'me') {
        Block 'GATE grill-valid: interview mode not declared. Use grill-with-docs when CONTEXT.md / docs/adr exist, otherwise grill-me; set grill_used=with-docs|me.'
      }
      return
    }

    'plan-schema' {
      if (-not (Test-Path -LiteralPath $PlanFile)) { Block 'GATE plan-schema: .orchestrator/plan.md is missing.' }
      $plan = Read-AllText $PlanFile
      foreach ($f in $PlanFields) {
        if ($plan -notmatch [regex]::Escape($f)) { Block "GATE plan-schema: plan.md is missing the section '$f'." }
      }
      return
    }

    'zero-p0' {
      if ((Get-Fact 'p0_count') -eq '0') { return }
      Block "GATE zero-p0: P0 count is not zero (p0_count=$(Get-Fact 'p0_count'); unset counts as not zero). Revise the plan until there is no P0. Do not just change the number."
    }

    'human-code-clear' {
      $c = Get-ConflictCount
      if ($c -eq 0) { return }
      Block "GATE human-code-clear: plan.md touches $c human-protected path(s) (list: .orchestrator/human-code.txt). The human must run ocf.ps1 allow <path> in their own terminal."
    }

    'stack-env' {
      Assert-Fact 'stack_env' 'stack-env' 4 'When the task involves a toolchain the human must first declare the EXISTING environment/packages/runtime/version manager.'
      return
    }

    default { Block "Unknown gate: $Name" }
  }
}

function Invoke-EntryGates([string]$Target) {
  switch ($Target) {
    'planning' { Invoke-Gate 'context'; Invoke-Gate 'docs-decision'; Invoke-Gate 'grill-valid'; return }
    'executing' { Invoke-Gate 'plan-schema'; Invoke-Gate 'zero-p0'; Invoke-Gate 'human-code-clear'; Invoke-Gate 'stack-env'; return }
    default { return }
  }
}

function Test-Transition([string]$From, [string]$To) {
  $ok = @(
    'ready>asking', 'ready>blocked',
    'asking>asking', 'asking>planning', 'asking>blocked',
    'planning>planning', 'planning>executing', 'planning>asking', 'planning>blocked',
    'executing>executing', 'executing>reporting', 'executing>blocked',
    'reporting>reporting', 'reporting>ready', 'reporting>blocked',
    'blocked>ready', 'blocked>asking', 'blocked>planning', 'blocked>executing', 'blocked>reporting'
  )
  return ($ok -contains "$From>$To")
}

# Glob translation shared by every path check: ** crosses separators, * does not.
function ConvertTo-PathRegex([string]$Pattern) {
  return '^' + [regex]::Escape($Pattern).Replace('\*\*', '.*').Replace('\*', '[^/]*').Replace('\?', '[^/]') + '$'
}

function Get-ConflictCount {
  $c = 0
  $plan = Read-AllText $PlanFile
  if ([string]::IsNullOrEmpty($plan)) { return 0 }
  $allowed = @(Read-AllLines $AllowedEdits)
  $cands = $null
  foreach ($raw in @(Read-AllLines $HumanCode)) {
    $pat = "$raw".Trim()
    if ([string]::IsNullOrEmpty($pat) -or $pat.StartsWith('#')) { continue }
    $base = $pat.Split('*')[0]
    if ([string]::IsNullOrEmpty($base)) {
      # A pattern with no literal prefix, such as *.md, cannot be found by substring. Match it
      # against the path-like tokens in the plan instead of skipping it silently.
      if ($null -eq $cands) {
        $cands = New-Object System.Collections.Generic.List[string]
        foreach ($m in [regex]::Matches($plan, '`([^`]+)`')) { $cands.Add($m.Groups[1].Value) }
        foreach ($m in [regex]::Matches($plan, '[A-Za-z0-9_\-\.]+(/[A-Za-z0-9_\-\.]+)*\.[A-Za-z0-9]{1,10}')) { $cands.Add($m.Value) }
      }
      $rx = ConvertTo-PathRegex $pat
      $hit = $false
      foreach ($cand in $cands) {
        if (($cand.Replace('\', '/').TrimStart('/')) -match $rx) { $hit = $true; break }
      }
      if (-not $hit) { continue }
    }
    elseif (-not $plan.Contains($base)) { continue }
    if (($allowed -contains $base) -or ($allowed -contains $pat)) { continue }
    $c++
  }
  return $c
}

# ---------------------------------------------------------------------------
# Commands available to the agent (control plane)
# ---------------------------------------------------------------------------

function Invoke-Init { Initialize-Store; Write-Output "OCF initialized: $Orc"; Invoke-Status }

function Invoke-Status {
  Initialize-Store
  Write-Output "state=$(Get-State)"
  Write-Output "enforce=$(Get-Config 'enforce')"
  Write-Output "limits=cmd_len:$MaxCmdLen,stmts:$MaxCmdStmts,repeat:$MaxCmdRepeat,fail_budget:$FailBudget"
  Write-Output "human_code_conflict=$(Get-ConflictCount)"
  $facts = @(Read-AllLines $FactsFile | Where-Object { $_ -ne '' })
  if ($facts.Count -gt 0) { Write-Output '--- facts ---'; $facts | ForEach-Object { Write-Output $_ } }
  else { Write-Output '(no facts recorded yet)' }
}

function Invoke-Advance {
  if (-not $Rest -or $Rest.Count -eq 0) { Block 'Usage: ocf.ps1 advance <state>' }
  $to = $Rest[0]
  if ($States -notcontains $to) { Block "Unknown state: $to (valid: $($States -join ', '))" }
  $from = Get-State
  if ($from -eq $to) { Note "already in $to"; return }
  if (-not (Test-Transition $from $to)) { Block "Illegal transition: $from -> $to. See references/system.md section 3." }
  Invoke-EntryGates $to
  Set-State $to
  Add-Journal "advance $from -> $to"
  Write-Output "state=$to"
}

function Invoke-GateCommand {
  if (-not $Rest -or $Rest.Count -eq 0) { Block 'Usage: ocf.ps1 gate <name>' }
  Invoke-Gate $Rest[0]
  Write-Output "gate $($Rest[0]): PASS"
}

function Invoke-Set {
  if (-not $Rest -or $Rest.Count -lt 2) { Block 'Usage: ocf.ps1 set <key> <value>' }
  $k = $Rest[0]
  $v = ($Rest[1..($Rest.Count - 1)] -join ' ')
  if ($k -eq 'approved_by') { Block 'OCF privilege gate: approved_by is only written by the human running ocf.ps1 approve.' }
  if ($k -eq 'must_consult') { Block 'OCF privilege gate: must_consult only changes via the failure budget or a human reply.' }
  if ($k -eq 'grill_rounds' -or $k -eq 'p0_count') {
    $t = 0
    if (-not [int]::TryParse($v, [ref]$t) -or $t -lt 0) { Block "$k must be a non-negative integer" }
  }
  Set-Fact $k $v
  Add-Journal "set $k='$(Clip $v 60)'"
  Write-Output "$k=$v"
}

function Invoke-Journal {
  $n = 20
  if ($Rest -and $Rest.Count -gt 0) { [int]::TryParse($Rest[0], [ref]$n) | Out-Null }
  $lines = @(Read-AllLines $Journal)
  if ($lines.Count -eq 0) { Write-Output '(no audit records)'; return }
  $lines | Select-Object -Last ([Math]::Max(1, $n)) | ForEach-Object { Write-Output $_ }
}

function Invoke-Fail {
  if (-not $Rest -or $Rest.Count -eq 0) { Block 'Usage: ocf.ps1 fail "<reason>"' }
  $reason = ($Rest -join ' ')
  $s = 0; [int]::TryParse((Get-Fact 'fail_streak'), [ref]$s) | Out-Null
  $t = 0; [int]::TryParse((Get-Fact 'fail_total'), [ref]$t) | Out-Null
  $s++; $t++
  Set-Fact 'fail_streak' "$s"
  Set-Fact 'fail_total' "$t"
  Set-Fact 'fail_last_reason' (Clip $reason 200)
  Add-Journal "fail #${s}: $(Clip $reason 80)"
  if ($s -ge $FailBudget) {
    Set-Fact 'must_consult' 'yes'
    Note "OCF-REQUIRED: $s consecutive failures -> must_consult=yes; execution tools are now hard-blocked."
    Note 'Stop and tell the human: symptom / what you already tried / what you need from them.'
  }
  Write-Output "fail_streak=$s"
  Write-Output "must_consult=$(Get-Fact 'must_consult')"
}

function Invoke-Ok {
  Set-Fact 'fail_streak' '0'
  Set-Fact 'must_consult' 'no'
  Add-Journal 'ok (fail_streak -> 0)'
  Write-Output 'fail_streak=0'
}

# ---------------------------------------------------------------------------
# Human-only commands (the agent calling these is caught by Guard-Exec)
# ---------------------------------------------------------------------------

function Invoke-Approve {
  if (-not $Rest -or $Rest.Count -eq 0) { Block 'Usage: ocf.ps1 approve "<reason>" (run by the human in their own terminal)' }
  $why = ($Rest -join ' ')
  if ($why.Trim().Length -lt 8) {
    Block 'OCF approval gate: the reason must be a real sentence (min 8 chars) so that an accidental or scripted call cannot approve a plan.'
  }
  if ((Get-State) -ne 'planning') { Block "ocf approve only runs in the planning state (currently $(Get-State))." }
  Invoke-EntryGates 'executing'
  Set-Fact 'approved_by' $why
  Set-Fact 'approved_at' (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
  Set-State 'executing'
  Add-Journal "APPROVE -> executing | $(Clip $why 120)"
  Write-Output 'state=executing'
}

function Invoke-Reject {
  if (-not $Rest -or $Rest.Count -eq 0) { Block 'Usage: ocf.ps1 reject "<reason>" (run by the human in their own terminal)' }
  $why = ($Rest -join ' ')
  if ((Get-State) -ne 'planning') { Block "ocf reject only runs in the planning state (currently $(Get-State))." }
  Set-State 'asking'
  Add-Journal "REJECT -> asking | $(Clip $why 120)"
  Write-Output 'state=asking'
}

function Invoke-Confirm {
  if ((Get-State) -ne 'asking') { Block "ocf confirm only runs in the asking state (currently $(Get-State))." }
  Invoke-EntryGates 'planning'
  Set-State 'planning'
  Add-Journal 'CONFIRM -> planning (human command)'
  Write-Output 'state=planning'
}

function Invoke-HumanCode {
  Initialize-Store
  if (-not $Rest -or $Rest.Count -eq 0) { Block 'Usage: ocf.ps1 human-code <path-or-glob>' }
  $p = Get-RelativePath $Rest[0]
  $lines = @(Read-AllLines $HumanCode)
  if ($lines -notcontains $p) { [System.IO.File]::AppendAllText($HumanCode, ($p + "`n"), $Utf8NoBom) }
  Add-Journal "human-code += $p"
  Write-Output "protected=$p"
}

function Invoke-Allow {
  Initialize-Store
  if (-not $Rest -or $Rest.Count -eq 0) { Block 'Usage: ocf.ps1 allow <path-or-glob>' }
  $p = Get-RelativePath $Rest[0]
  $lines = @(Read-AllLines $AllowedEdits)
  if ($lines -notcontains $p) { [System.IO.File]::AppendAllText($AllowedEdits, ($p + "`n"), $Utf8NoBom) }
  Add-Journal "allow += $p"
  Write-Output "allowed=$p"
}

function Invoke-Deny {
  Initialize-Store
  if (-not $Rest -or $Rest.Count -eq 0) { Block 'Usage: ocf.ps1 deny <path-or-glob>' }
  $p = Get-RelativePath $Rest[0]
  $lines = @(Read-AllLines $AllowedEdits | Where-Object { $_.Trim() -ne $p })
  Write-TextFile $AllowedEdits (($lines -join "`n") + "`n")
  Add-Journal "allow -= $p"
  Write-Output "revoked=$p"
}

# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

function Invoke-HookUserPrompt($json) {
  Initialize-Store
  if ($null -eq $json) { $json = Read-HookInput }
  $p = Get-Field $json 'prompt'
  $st = Get-State

  # A human reply counts as manual intervention, which clears the failure budget.
  if ((Get-Fact 'must_consult') -eq 'yes') {
    Set-Fact 'must_consult' 'no'
    Set-Fact 'fail_streak' '0'
    Set-Fact 'last_consult_answer' (Clip $p 400)
    Add-Journal 'consult answered (human intervention)'
    Write-HookMessage 'OCF: the human has stepped in, must_consult cleared. First restate your understanding of the instruction, then re-plan.'
    exit 0
  }

  if ($st -eq 'ready') {
    Set-Fact 'first_prompt' (Clip $p 400)
    Set-State 'asking'
    Add-Journal 'advance ready -> asking (first human message)'
    Write-HookMessage 'OCF: entering asking. Interview the human first (one question at a time) and record facts: goal / tools / references / deliverables / code_style / docs_decision / stack_env / grill_used. Once consensus is reached write consensus, then ocf advance planning.'
    exit 0
  }

  if ($st -eq 'asking') {
    $n = 0; [int]::TryParse((Get-Fact 'grill_rounds'), [ref]$n) | Out-Null
    $n++
    Set-Fact 'grill_rounds' "$n"
    Add-Journal "human reply #$n"
    exit 0
  }

  exit 0
}

function Invoke-HookDispatch {
  Initialize-Store
  $raw = ''
  try {
    $reader = New-Object System.IO.StreamReader([Console]::OpenStandardInput(), $Utf8NoBom, $true)
    $raw = $reader.ReadToEnd()
    $reader.Dispose()
  }
  catch { }
  $json = $null
  if (-not [string]::IsNullOrWhiteSpace($raw)) { try { $json = ($raw | ConvertFrom-Json) } catch { } }
  $ev = Get-Field $json 'hook_event_name'
  $script:HookMode = $ev
  try {
    switch ($ev) {
      'UserPromptSubmit' { Invoke-HookUserPrompt $json }
      'PreToolUse' { Invoke-HookPreTool $json }
      default { exit 0 }
    }
  }
  catch {
    Add-Journal ("hook-error: " + $_.Exception.Message + " @" + $_.InvocationInfo.PositionMessage)
    if ($script:HookMode -eq 'PreToolUse') {
      # Fail-safe: a broken guard must REFUSE the call, never let it through. Exiting non-zero from a
      # PreToolUse hook is only surfaced as a non-blocking warning by VS Code.
      $p = @{ hookSpecificOutput = @{ hookEventName = 'PreToolUse'; permissionDecision = 'deny'; permissionDecisionReason = ("OCF hook internal error, refusing this tool call: " + $_.Exception.Message) } }
      Write-StdoutUtf8 ($p | ConvertTo-Json -Compress -Depth 5)
      [Environment]::Exit(0)
    }
    Write-StderrUtf8 "OCF hook exception: $($_.Exception.Message)"
    [Environment]::Exit(9)
  }
}

function Guard-Edit($json) {
  $p = Get-ToolField $json 'filePath'
  if ([string]::IsNullOrWhiteSpace($p)) { $p = Get-ToolField $json 'path' }
  if ([string]::IsNullOrWhiteSpace($p)) { return }
  $rel = Get-RelativePath $p

  # plan.md is the artifact the gate itself demands, so it must be writable before approval.
  if ($rel -eq '.orchestrator/plan.md') { return }

  # Self-authorization guard. Guard-Exec inspects the command STRING, so an agent could otherwise
  # dodge it by writing a small executable file that invokes the control script and then running that
  # file: the executed command line would contain neither the script path nor the subcommand name.
  # Two scope limits, both learned the hard way when this guard blocked a legitimate edit:
  #   - only while enforcement is on: enforce=off is documented as handing full control back to the
  #     human, which has to include editing the gate scripts themselves;
  #   - never for .github/hooks/scripts/**, because that IS the implementation of these subcommands.
  $ext = [System.IO.Path]::GetExtension($rel).ToLowerInvariant()
  if ((Test-Enforced) -and ($ExecutableExtensions -contains $ext) -and ($rel -notlike '.github/hooks/scripts/*')) {
    # Scan the WHOLE tool input, not just 'content'/'newString': multi_replace_string_in_file nests
    # its text under replacements[].newString and edit_notebook_file uses newCode, so a field-by-field
    # read leaves those tools blind.
    $body = ''
    if ($null -ne $json.tool_input) { $body = ($json.tool_input | ConvertTo-Json -Compress -Depth 20) }
    if ($body -match ("(?i)ocf\.(ps1|sh).{0,300}?(" + $HumanOnlySubcommands + ")")) {
      Block "OCF self-authorization gate: writing a human-only subcommand into an executable file is not allowed ($rel). Only the human may approve, from their own terminal."
    }
  }

  if (Test-Enforced) {
    if (Test-SelfProtected $rel) {
      Block "OCF self-protection gate: editing the orchestrator's own state or gate files is not allowed ($rel). Either the human edits it by hand, or the human sets enforce=off in .orchestrator/config."
    }
  }

  if ((Test-InList $rel $HumanCode) -and -not (Test-InList $rel $AllowedEdits)) {
    Block "OCF human-code gate: editing human code or human docs is not allowed ($rel). The human must run ocf.ps1 allow $rel in their own terminal."
  }

  if (Test-Enforced) {
    $st = Get-State
    if ($st -eq 'executing' -or $st -eq 'reporting') { return }
    Block "OCF approval gate: state=$st, not approved, so file edits are blocked ($rel). To approve, the human runs ocf.ps1 approve `"<reason>`" in their own terminal."
  }
}

function Guard-Exec($json) {
  $c = Get-ToolField $json 'command'
  if ([string]::IsNullOrWhiteSpace($c)) { $c = Get-ToolField $json 'code' }
  # create_and_run_task keeps its shell command at tool_input.task.command, and the Playwright
  # evaluate tool keeps its code at tool_input.function. Reading only command/code left both of them
  # ungated, because an empty $c returned early and no gate ever saw the call.
  if ([string]::IsNullOrWhiteSpace($c) -and $null -ne $json.tool_input.task) { $c = "$($json.tool_input.task.command)" }
  if ([string]::IsNullOrWhiteSpace($c)) { $c = Get-ToolField $json 'function' }
  if ([string]::IsNullOrWhiteSpace($c)) { return }

  if ($c -match '(?i)(headless|--screenshot|screenshot|playwright|puppeteer|capture.{0,12}(image|screen))') {
    Block "OCF visual-test gate: the machine may never run visual or screenshot tests. Command: $(Clip $c 120). Take the screenshot yourself and attach it to the conversation."
  }

  if ((Get-Fact 'must_consult') -eq 'yes') {
    Block "OCF failure-budget gate: $(Get-Fact 'fail_streak') consecutive failures (last: $(Get-Fact 'fail_last_reason')). Stop and ask the human: symptom / what you tried / what you need. Do not keep retrying. Clears automatically once the human replies."
  }

  # Orchestrator control plane: subcommands the agent may use. Authorization subcommands and
  # 'advance executing' are human-only.
  # Control plane is recognised only when EVERY statement of the command invokes the control script
  # by its full relative path. Merely mentioning the name is not enough: with a plain substring match
  # `Write-Host "ocf.ps1"; <anything>` inherited the exemption and skipped the approval gate.
  $isOcf = $true
  foreach ($seg in ($c -split ';|&&|\|\|')) {
    if ($seg.Trim() -ne '' -and $seg -notmatch '(?i)\.github[\\/]hooks[\\/]scripts[\\/]ocf\.(ps1|sh)\b') { $isOcf = $false }
  }
  if ($isOcf) {
    if ($c -match ('(?i)\b(' + $HumanOnlySubcommands + ')\b')) {
      Block 'OCF self-authorization gate: ocf approve / reject / confirm / allow / deny / human-code may only be run by the human in their own terminal.'
    }
    $m = [regex]::Match($c, '(?i)\badvance\s+([A-Za-z\-]+)')
    if ($m.Success) {
      $tgt = $m.Groups[1].Value.ToLower()
      if ($AgentAdvanceTargets -notcontains $tgt) {
        Block "OCF self-authorization gate: the agent may not advance to $tgt. Entering executing requires the human to run ocf.ps1 approve in their own terminal."
      }
    }
  }

  # NOTE: the function call must be wrapped in its own parentheses. PowerShell parses a bare word
  # at the start of a condition as a COMMAND, so `if (Test-Enforced -and -not $isOcf)` silently
  # becomes "call Test-Enforced with -and/-not/$isOcf as arguments" and the condition degenerates to
  # Test-Enforced's output. That made the control-plane exemption below dead code: with enforce=on
  # even `ocf status` was blocked, which locked the agent out of the whole flow.
  if ((Test-Enforced) -and (-not $isOcf)) {
    $st = Get-State
    if (-not ($st -eq 'executing' -or $st -eq 'reporting')) {
      Block "OCF approval gate: state=$st, not approved, so this command is blocked: $(Clip $c 120). To approve, the human runs ocf.ps1 approve in their own terminal."
    }
  }

  if ($c.Length -gt $MaxCmdLen) {
    Block "OCF observability gate: command too long ($($c.Length) > $MaxCmdLen chars). Split it into short single-purpose commands and confirm each one."
  }
  # Count statements on the top-level mask: a ';' inside a string, a hashtable or a script block is
  # not a top-level separator, and counting it produced false positives.
  $stmts = 1 + ([regex]::Matches((Mask-TopLevel $c), ';|&&|\|\|')).Count
  if ($stmts -gt $MaxCmdStmts) {
    Block "OCF observability gate: $stmts statements chained into one command (limit $MaxCmdStmts). Split them and run one at a time."
  }
  if ($c -match '(?i)Out-Null|-Quiet|--quiet|>\s*\$null|2>\s*\$null|/dev/null|-WindowStyle\s+Hidden') {
    Block 'OCF observability gate: the command silences its output (Out-Null / $null / /dev/null / --quiet). Everything must stay visible to the human.'
  }
  if ($c -match '(?i)Read-Host|ReadKey|-Verb\s+RunAs|(^|[^a-z])sudo\s|cmd(\.exe)?\s+/c') {
    Block 'OCF observability gate: the command may block on interactive input or raise a dialog. Rewrite it non-interactively; anything needing elevation or a click must be run by the human.'
  }

  if ($c -match '(?i)(^|[^a-z])(npm|pnpm|yarn|bun)\s+(run\s+)?test|(^|[^a-z])(jest|vitest|mocha|pytest|tox|ctest|rspec)([^a-z]|$)|(^|[^a-z])(dotnet|go|cargo|mvn|mvnw|gradle|gradlew|phpunit)\s+\S*test') {
    if ((Get-Fact 'test_authorized') -ne 'yes') {
      Block "OCF test gate: do not run tests on your own. Only after the human asks in this conversation, set test_authorized yes. Command: $(Clip $c 120)"
    }
  }

  if ($c -match '(?i)(^|[^a-z])(npm|pnpm|yarn|bun|pip|pip3|conda|mamba|poetry|uv|gem|composer|cargo|go|dotnet|apt|apt-get|brew|choco|scoop|winget|pacman|npx|dnf|yum)\s+(install|add|get|i|upgrade|update|env|create)|(^|[^a-z])npx\s') {
    if ([string]::IsNullOrWhiteSpace((Get-Fact 'stack_env'))) {
      Block "OCF stack gate: the human has not declared the existing environment/packages/runtime/version manager. Do not install or probe on your own. Command: $(Clip $c 120)"
    }
  }

  if ($c -match '(?i)rm\s+-rf\s+/[^.]|git\s+push\s+.*--force|drop\s+table|git\s+reset\s+--hard|Remove-Item.*-Recurse.*-Force') {
    Block "OCF safety gate: destructive command detected and blocked: $(Clip $c 120)"
  }

  # Write targets are covered by human-code protection too. Only commands that look like writes are
  # path-scanned, so ordinary reads are not caught by accident.
  $writeish = ($c -match '(?i)\b(Set-Content|Add-Content|Out-File|New-Item|Remove-Item|Move-Item|Copy-Item|Rename-Item|tee|sed\s+-i|truncate|dd)\b|>>?\s*\S')
  if ($writeish) {
    foreach ($tok in ([regex]::Matches($c, '[A-Za-z0-9_\-\./\\]+') | ForEach-Object { $_.Value })) {
      if ($tok -notmatch '[/\\]') { continue }
      $rp = Get-RelativePath $tok
      if ([string]::IsNullOrEmpty($rp)) { continue }
      if ((Test-Enforced) -and (Test-SelfProtected $rp)) {
        Block "OCF self-protection gate: a command may not write to the orchestrator's own state or gate files ($rp). The human must edit it by hand."
      }
      if ((Test-InList $rp $HumanCode) -and -not (Test-InList $rp $AllowedEdits)) {
        Block "OCF human-code gate: the command writes to a protected path ($rp). Changing human code or docs is the human's job, or they must run ocf.ps1 allow first."
      }
    }
  }

  # Going in circles: the same command over and over -> hand the decision to the human.
  $norm = ($c -replace '\s', '')
  $cnt = 0
  $cnt = @(Read-AllLines $ExecLog | Where-Object { $_ -eq $norm }).Count
  if ($cnt -ge $MaxCmdRepeat) {
    $reason = "OCF iteration gate: this is about to be execution #$($cnt + 1) of the exact same command, which suggests you are stuck in a loop. The human decides whether to continue or change approach."
    $p = @{ hookSpecificOutput = @{ hookEventName = 'PreToolUse'; permissionDecision = 'ask'; permissionDecisionReason = $reason } }
    Write-StdoutUtf8 ($p | ConvertTo-Json -Compress -Depth 5)
    [Environment]::Exit(0)
  }
  [System.IO.File]::AppendAllText($ExecLog, ($norm + "`n"), $Utf8NoBom)
}

# Unknown tools: fail-safe instead of fail-open. Without this, a newly installed extension or MCP
# server would silently become an unguarded write/exec path. Only names that look like they mutate
# or execute something are escalated, so purely read-oriented tools keep working without friction.
function Guard-UnknownTool([string]$Tool) {
  if ([string]::IsNullOrWhiteSpace($Tool)) { return }
  if (-not (Test-Enforced)) { return }
  if ($Tool -notmatch '(?i)(create|update|write|edit|delete|remove|push|upload|run|exec|install|apply|move|rename|replace|set|launch|start|debug|fork|merge|publish|send|post|commit|patch|deploy)') { return }
  $st = Get-State
  if ($st -eq 'executing' -or $st -eq 'reporting') { return }
  $reason = "OCF unknown-tool gate: '$Tool' looks like it performs an action, but it is not in the known tool lists and state=$st is not approved. Confirm whether it may run."
  $p = @{ hookSpecificOutput = @{ hookEventName = 'PreToolUse'; permissionDecision = 'ask'; permissionDecisionReason = $reason } }
  Write-StdoutUtf8 ($p | ConvertTo-Json -Compress -Depth 5)
  [Environment]::Exit(0)
}

# Tools that perform an action but are not expressed as a shell command, so Guard-Exec cannot see
# them. While no action is authorised they must not run.
function Guard-Action([string]$Tool) {
  if (-not (Test-Enforced)) { return }
  $st = Get-State
  if ($st -eq 'executing' -or $st -eq 'reporting') { return }
  Block "OCF approval gate: '$Tool' performs an action (installs / debugs / launches) while state=$st, which is not approved. The human runs ocf.ps1 approve in their own terminal to approve."
}

function Invoke-HookPreTool($json) {
  Initialize-Store
  if ($null -eq $json) { $json = Read-HookInput }
  $tool = Get-Field $json 'tool_name'
  $handled = $false

  switch -Regex ($tool) {
    '^(replace_string_in_file|multi_replace_string_in_file|edit_notebook_file|create_file|vscode_renameSymbol|mcp_github_mcp_se_create_or_update_file|mcp_github_mcp_se_delete_file|mcp_github_mcp_se_push_files|mcp_github_mcp_se_fork_repository)$' {
      $handled = $true
      Guard-Edit $json
    }
  }

  switch -Regex ($tool) {
    '^(screenshot_page|view_image|run_playwright_code|mcp_playwright_browser_take_screenshot|mcp_playwright_browser_run_code_unsafe)$' {
      $handled = $true
      Block "OCF visual-test gate: the machine may never run visual or screenshot tests (tool: $tool). Take the screenshot yourself and attach it in the next message. Attachments you provide are readable; the tool is not."
    }
  }

  switch -Regex ($tool) {
    '^(run_in_terminal|run_notebook_cell|create_and_run_task|mcp_playwright_browser_run_code_unsafe|mcp_playwright_browser_evaluate)$' {
      $handled = $true
      Guard-Exec $json
    }
  }

  switch -Regex ($tool) {
    '^(install_python_packages|install_extension|debug_java_application|configure_python_environment|create_new_workspace|create_new_jupyter_notebook|create_and_run_task)$' {
      $handled = $true
      Guard-Action $tool
    }
  }

  # Explicitly allowed: read-only or coordination tools whose names would otherwise trip the
  # action-verb heuristic. Dispatching the auditor subagent during planning is mandatory, so it must
  # never be escalated.
  switch -Regex ($tool) {
    '^(runSubagent|manage_todo_list|vscode_askQuestions|memory)$' { $handled = $true }
  }

  if (-not $handled) { Guard-UnknownTool $tool }

  exit 0
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

switch ($Command) {
  'init' { Invoke-Init }
  'status' { Invoke-Status }
  'advance' { Invoke-Advance }
  'gate' { Invoke-GateCommand }
  'set' { Invoke-Set }
  'journal' { Invoke-Journal }
  'fail' { Invoke-Fail }
  'ok' { Invoke-Ok }
  'approve' { Invoke-Approve }
  'reject' { Invoke-Reject }
  'confirm' { Invoke-Confirm }
  'human-code' { Invoke-HumanCode }
  'allow' { Invoke-Allow }
  'deny' { Invoke-Deny }
  'hook-dispatch' { Invoke-HookDispatch }
  default {
    Write-StderrUtf8 'Usage: ocf.ps1 <init|status|advance|gate|set|journal|fail|ok> [args]            # agent'
    Write-StderrUtf8 '       ocf.ps1 <approve|reject|confirm|allow|deny|human-code> [args]      # human only'
    exit 1
  }
}

exit 0
