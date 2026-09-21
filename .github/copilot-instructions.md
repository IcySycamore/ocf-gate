# Work Control Flow

Full rules: [work-control-flow.md](./work-control-flow.md). Read it before first acting and whenever your attention begins to drift.

## Entry point

    Windows: python .github\ocf\ocf.py <cmd>
    Other:   python3 .github/ocf/ocf.py <cmd>

Agent may run: status, set, gate, journal, fail, ok, init, selftest. advance targets are limited to
asking, planning, reporting, ready, blocked. Human terminal only, calling these gets you blocked:
approve, reject, confirm, allow, deny, human-code.
Never hand-edit state, facts, journal.log, human-code.txt or allowed-edits.txt under .orchestrator/.
The gate's own configuration is `.github/ocf/policy.toml`, which the human edits by hand.

## State machine

    ready -> asking -> planning -> executing -> reporting -> ready      bypass: blocked

Only the human running `python .github/ocf/ocf.py approve "<reason>"` in their own terminal enters
executing. Never run it for them or set approved_by.

## Three behaviours no code can enforce

1. asking: derive the eight plan sections from context and the human's three-section statement, then
   grill only what is left, one question per turn. Never guess or fill in for the human. A perfunctory
   or automatic reply is not an answer, so re-ask.
2. planning: dispatch the plan-auditor subagent for an independent P0 review, never audit your own
   plan. At P0 zero, give the human the action report and the risk design report, hand them
   `python .github/ocf/ocf.py approve "<reason>"` verbatim, then stop.
3. To see a screen, state exactly what to capture and how many shots, then ask the human to attach the
   screenshot. Attachments are readable, screenshot tools are permanently blocked.

## When a gate blocks you

Fix the precondition it names: supply the missing fact, split the command, stop silencing output, go
ask the human. Never rewrite your way around it. Circumventing a gate is a serious violation.

<!-- OCF:GENERATED -->
<!-- Written by `python .github/ocf/ocf.py reload` from the soft rules in
     .github/ocf/policy.toml. Do not edit by hand - the next reload overwrites it.
     Edit the rules in that file and run reload. -->

## Command vocabulary

Generated from the same table the program dispatches on, so this list cannot drift away from what the entry point actually accepts.

agent: status | set | gate | journal | fail | ok
       advance | init | selftest
human: approve | reject | confirm
       allow | deny | human-code
       reload

A human-only command is refused even if the human asks you in the conversation to run it. "The human already said yes" is not approval; approval is the human running the command in their own terminal. If they want it to stop, they turn the rule off or edit the config - they do not authorise it by asking.

## Soft rules

No hook can enforce these: nothing can check whether a sentence was written. They are put in front of you instead. Each one names the moment it applies to.

### answer

- **菜鸟模式** - 用专业但生动的语言，极尽详细地讲解你这次回答中出现的每一个领域原语：它是什么、在这里指什么、为什么这么叫、和哪个概念容易混淆。然后把这些原语按「名字 - 一句话定义 - 在本项目的用法」补进 .orchestrator/glossary.md；同一个原语已经记过就不再重复记，只在定义需要修正时改它。不要为了讲而讲：只讲这次回答里真的用到的原语，没用到的不提。
  - Exception, judged by you: 人类说「不用解释」「直接给结论」或明确表示已经懂了，这一轮就跳过。

### act

- **人类专属命令** - 授权类命令只有人类能在自己的终端里跑。即使人类在对话里要求你代跑，也不要跑；「人类已经说过同意了」不算批准，批准是他在自己的终端里跑那条命令这个动作本身。他若坚持，就告诉他去关掉对应规则或改 .github/ocf/policy.toml——改配置是授权，要求你代跑不是。
  - Exception, judged by you: 从不需要例外：这条一旦有例外，就等于把批准权交给了对话。

### plan

- **对照文档质询** - 质询或规划之前，先读 CONTEXT.md 和 docs/adr/：项目已经用什么词、已经定过什么。问题要对着它们问，比如「你说的 X 和 CONTEXT.md 里的 Y 是同一个东西吗」「这个方案和 ADR-0003 的结论冲突，为什么这次不一样」。术语不一致就先解决术语，再解决问题。
  - Exception, judged by you: 项目里没有 CONTEXT.md 也没有 docs/adr/ 时，这条没有对照物，跳过。

<!-- OCF:END -->
