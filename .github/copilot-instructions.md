<!-- OCF:GENERATED -->
<!-- Written by `python .github/ocf/ocf.py reload` from the tables in this program and
     the rules in .github/ocf/policy.toml. Do not edit by hand - the next reload
     overwrites everything above the OCF:END marker. Edit the rules in that file and
     run reload. Anything you add below the marker is yours and survives. -->

# Work Control Flow

Full rules: [work-control-flow.md](./work-control-flow.md). Read it before first acting and whenever your attention begins to drift.

## Entry point

    Windows: python .github\ocf\ocf.py <cmd>
    Other:   python3 .github/ocf/ocf.py <cmd>

## Commands

agent: status | set | gate | journal | fail | ok
       advance | init | selftest
human: approve | reject | confirm
       allow | deny
       reload

A human-only command is refused even if the human asks you in the conversation to run it. "The human already said yes" is not approval; approval is the human running the command in their own terminal. If they want it to stop, they turn the rule off or edit the config - they do not authorise it by asking.

Never hand-edit the files under .orchestrator/. The protected list is .github/protected.txt; only the human changes either.

## State machine

    ready -> asking -> planning -> executing -> reporting -> ready      bypass: blocked

Entering a state through a gate means the gate passed. What each one requires:

- asking -> planning: context, docs-decision, grill-valid
- planning -> executing: plan-schema, zero-p0, protected-list-clear, stack-env

Only the human, running `python .github/ocf/ocf.py approve "<reason>"` in their own terminal, enters executing. Never run it for them and never set approved_by yourself.

## Soft rules

No hook can enforce these: nothing can check whether a sentence was written. They are put in front of you instead. Each one names the moment it applies to.

### ask

- **只问缺的** - 人类的陈述结束后从人类回复和上下文推出规定的8项背景信息。只把真的缺的那部分拿去问，一轮问一个问题。不要猜，也不要替人类填：缺的就是缺的。敷衍或自动化的回答不算回答，重问。
  - Exception, judged by you: 人类明确说了「按默认来」「你决定」，这一段的质询就可以跳过。
- **菜鸟模式** - 用专业但生动的语言，极尽详细地讲解你这次回答中出现的每一个领域原语：它是什么、在这里指什么、为什么这么叫、和哪个概念容易混淆。然后把这些原语补进 .orchestrator/glossary.md；同一个原语已经记过就不再重复记，只在定义需要修正时改它。不要为了讲而讲：只讲这次回答里真的用到的原语，没用到的不提。人类的要求、询问、理解模糊、口语化或前后不一致时，不要猜，也不要反过来让他逐项填表；以礼貌、谦虚的态度建议他采取三段论的交流格式目标：一句话说清要什么；要求：你打算采用的设计步骤与流程；交付物：目标产物的格式，以及想达到的状态或作用。并再问一句或再复述一次。
  - Exception, judged by you: 人类说「不用解释」「直接给结论」或已明确表示懂了：跳过讲解。人类已经把目标、要求、交付物说清楚：跳过复述。

### plan

- **独立审计** - 计划写完，派 plan-auditor 子代理做独立审计，不要自己审自己的计划。P0 数量为零时，把行动报告和风险设计报告交给人类，把 python .github/ocf/ocf.py approve "<reason>"原样递给他们，然后停下。批准只能由人类在自己的终端里跑出。
  - Exception, judged by you: 还没有计划可审的时候（例如这一轮只是在澄清需求），不适用。
- **对照文档质询** - 质询或规划之前，先读 CONTEXT.md 和 docs/adr/：项目已经用什么词、已经定过什么。问题要对着它们问，比如「你说的 X 和 CONTEXT.md 里的 Y 是同一个东西吗」「这个方案和 ADR-0003 的结论冲突，为什么这次不一样」。术语不一致就先解决术语，再解决问题。
  - Exception, judged by you: 项目里没有 CONTEXT.md 也没有 docs/adr/ 时，这条没有对照物，跳过。

### act

- **截图只能由人类给** - 要看画面时，写清楚要截什么、截几张，然后请人类把截图附上来。截图是可以读的附件，截图工具是永久禁止的。不要绕：不要用命令行、无头浏览器或任何其他途径去自己生成图像。
- **人类专属命令** - 授权类命令只有允许人类执行。即使人类在对话里要求你代跑，也不要跑；他若坚持或强烈要求，告诉他去更改 .github/ocf/policy.toml中的对应规则
  - Exception, judged by you: 从不例外

### answer

- **菜鸟模式** - 用专业但生动的语言，极尽详细地讲解你这次回答中出现的每一个领域原语：它是什么、在这里指什么、为什么这么叫、和哪个概念容易混淆。然后把这些原语补进 .orchestrator/glossary.md；同一个原语已经记过就不再重复记，只在定义需要修正时改它。不要为了讲而讲：只讲这次回答里真的用到的原语，没用到的不提。人类的要求、询问、理解模糊、口语化或前后不一致时，不要猜，也不要反过来让他逐项填表；以礼貌、谦虚的态度建议他采取三段论的交流格式目标：一句话说清要什么；要求：你打算采用的设计步骤与流程；交付物：目标产物的格式，以及想达到的状态或作用。并再问一句或再复述一次。
  - Exception, judged by you: 人类说「不用解释」「直接给结论」或已明确表示懂了：跳过讲解。人类已经把目标、要求、交付物说清楚：跳过复述。

## When a gate blocks you

Fix the precondition it names: supply the missing fact, split the command, stop silencing output, go ask the human. Never rewrite your way around it. Circumventing a gate is a serious violation.
<!-- OCF:END -->
