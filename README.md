# Work Control Flow

把「人类批准后才动手」从口头约定变成**不可绕过的代码门禁**。给 VS Code Copilot Chat 用。

- 规则全文（agent 读这一份）：[`.github/work-control-flow.md`](.github/work-control-flow.md)
- 策略配置指南（人类读这份改门禁）：[`POLICY-GUIDE.md`](POLICY-GUIDE.md)
- 常驻契约（每个请求都加载）：[`.github/copilot-instructions.md`](.github/copilot-instructions.md)
- 重构设计与取舍记录：[`REFACTOR-DESIGN.md`](REFACTOR-DESIGN.md)

## 解决什么问题

AI agent 最常见的事故不是「不会写」，而是**没被要求就动手**：没问清需求就开工、没等批准就改文件、
静默跑一堆命令、出错后无限重试、改掉人类的代码。

这套东西把这些问题做成**代码判定**：状态机决定「现在能做什么」，门禁判定「这一下放不放行」，
hooks 在工具执行前强制检查。不通过就拦下，并明确告诉你缺哪个前置条件。

## 优势与特性

| 特性                   | 说明                                                                                                  |
| ---------------------- | ----------------------------------------------------------------------------------------------------- |
| **硬门禁，不是提示词** | 门禁是进程 + hooks，拦不拦得住不取决于 agent 是否听话                                                 |
| **一份实现**           | 只有 `.github/ocf/ocf.py`（Python 3，标准库），跨平台语义唯一，不存在两份实现分叉                     |
| **规则是数据**         | 门禁规则全在 [`policy.toml`](.github/ocf/policy.toml)，改规则不改代码，首个命中即生效                 |
| **批准不靠说话**       | 进 `executing` 只能由人类在自己终端执行 `approve`；机器跑它会被拦（残余绕过面见「设计取舍」）         |
| **人类代码受保护**     | `human-code.txt` 清单内的路径机器改不了，只能人类 `allow` 逐路径授权                                  |
| **门禁不可自改**       | 规则文档、策略、hooks、agent、prompt 与运行时状态全部受自保护；改一个文件忘了改保护清单会**测试失败** |
| **失效可被发现**       | 自检含金丝雀，走真实 hook 入口；门禁若静默停止拦截，自检与测试会失败而不是看起来健康                  |
| **阈值免重载可调**     | `[limits]` 每次调用重读，改完立即生效                                                                 |
| **终端可视化**         | 拦长命令、多语句串联、静默输出、交互阻塞；同一命令反复执行转人工确认                                  |
| **失败预算**           | 连续失败 2 次锁死执行类工具，强制 agent 停下问人类；人类回话即解锁                                    |
| **永久禁视觉测试**     | 截图/看图工具无条件拦；需要看画面时由人类截图并在下一条消息附上                                       |
| **全程可审计**         | 状态流转、授权、失败都写进 `.orchestrator/journal.log`；每条判定都带规则 id                           |
| **离线可测**           | 一张用例表走真实 hook 入口的子进程，不依赖 VS Code                                                    |
| **单一可打包单元**     | 整个 `.github/` 复制进任意仓库即可，纯文本、无构建产物                                                |

## 组成

```text
.github/
├── copilot-instructions.md          常驻契约（每个请求都加载，含入口点与状态机）
├── work-control-flow.md             规则全文：状态机、门禁、受理契约、计划模板、终端纪律
├── ocf/
│   ├── ocf.py                       唯一实现：状态机 + 判定引擎 + CLI + 自检 + hook 入口
│   ├── policy.toml                  唯一策略：规则、阈值、工具分类、自检金丝雀
│   ├── requirements.txt             运行时依赖（仅 Python < 3.11 需 tomli，否则为空）
│   └── tests/
│       ├── cases.json               用例表（交给真实 hook 入口判定）
│       └── run.py                   运行器 + 结构断言
├── hooks/
│   └── orchestrator.json            四个事件的挂钩：SessionStart + UserPromptSubmit + PreToolUse + PostToolUse
├── agents/
│   ├── orchestrator.agent.md        主编排器人格（按状态驱动流程，子 agent 白名单已钉死）
│   ├── plan-auditor.agent.md        只读计划审查员（独立找 P0）
│   └── criterion-picker.agent.md    复现手段裁决员（只出一决策，不给修复）
└── prompts/
    ├── work-intake.prompt.md        /work-intake  受理任务
    ├── work-plan.prompt.md          /work-plan    出计划并送独立审查
    └── bug-route.prompt.md          /bug-route    定位错误

.orchestrator/                       运行时（不打包、自动创建、勿手改）
├── state                            当前状态
├── facts                            人类提供的要素（key=value）
├── plan.md                          行动清单（8 个字段）
├── human-code.txt                   人类代码保护清单（支持 glob）
├── allowed-edits.txt                人类逐路径授权
├── journal.log                      审计
└── exec.log                         命令重复执行记录
```

注意：**没有** `.orchestrator/config`。开关与阈值都并入 `policy.toml` 了 —— 留一个「看起来像开关」的
死文件本身就是陷阱。

## 依赖与运行前提

- **运行时零第三方依赖。** 入口只用标准库，TOML 用 `tomllib`（3.11+）或 `tomli` 兜底。
  这是硬约束：hooks 由 VS Code 在**宿主**上启动，无法用容器包裹，一旦入口缺依赖，
  门禁会**静默 fail-open** —— 正是这套系统要消灭的故障。
- **Docker 不参与运行时，只在测试时有用。** 把门禁跑进容器不可能（VS Code 在**宿主**上启动它），
  而跑测试也只要 CPython。它真正证明过的价值是：**把同一套用例放到 Linux 与其它 Python 版本上跑** ——
  路径/分隔符逻辑的另一半、以及 <3.11 的 tomli 分支，只有在那里才走得到。
  实测价值：它当场抛出 "no tests ran"，因为 Dockerfile 自己用 `python -m pytest` 而 pytest 默认
  只收集 `test_*.py`，我们的运行器叫 `run.py`。宿主上直接跑 `run.py` 永远发现不了这个。
- Windows 上解释器名是 `python`；其它平台是 `python3`（见 `orchestrator.json` 的平台覆盖）。

| 条件               | 说明                                                                                                                                                                                                                                                                                                                                         |
| ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **编辑器不得代答** | VS Code 在 agent 提问时，若聊天权限档为 **Autopilot (Preview)**（即 `chat.permissions.autopilot`）或设置 `chat.autoReply` 为 true，**会自己注入一条答复**。门禁**分不出**它与人类消息 —— 两者都走 `UserPromptSubmit`。所以这是部署前提：档位留在 **Default permissions**（或 **Allow all**），并在**用户设置**里加 `"chat.autoReply": false` |
| 必须重载窗口       | 新增/修改 `.github/hooks/*.json` **不会热加载**。用 `Developer: Show Agent Debug Logs` 确认                                                                                                                                                                                                                                                  |
| hooks 未被策略禁用 | 组织策略可能禁用 hooks                                                                                                                                                                                                                                                                                                                       |
| 工作目录           | hooks 的 cwd = 工作区根，所以配置里用仓库根相对路径                                                                                                                                                                                                                                                                                          |
| `policy.toml` 可读 | 读不到或解析失败时**不降级为「无策略」**，而是落到「除永远允许的工具外一律拒绝」的最严兜底，并由自检报 `[policy]`                                                                                                                                                                                                                            |

## 部署

```text
# 1) 把 .github/ 复制进目标仓库根
# 2) 初始化运行时（幂等）
python  .github\ocf\ocf.py init      # Windows
python3 .github/ocf/ocf.py init      # 其他平台
# 3) 登记本仓库的人类代码保护路径（重要，否则出厂只保护文档与编排器自身）
python .github\ocf\ocf.py human-code "src/**"
# 4) 自检
python .github\ocf\ocf.py selftest
# 5) 重载 VS Code 窗口，让 hooks 生效
```

出厂时 `policy.toml` 的 `system.enabled` 是 `false`（门禁关闭，方便先验证）。
确认无误后由人类手工改成 `true`，再重载窗口。

## 开始使用

直接对 agent 说你的任务即可。`ready` 下第一条人类消息会自动推进到 `asking`，
agent 会**逐项**问你要：目标 / 可用工具 / 参考设计 / 交付物 / 代码规范，
外加是否创建 `CONTEXT.md`、`docs/adr`。

```text
asking（一次只问一个问题）→ planning（写出 8 字段计划、派独立审查、P0 归零）
      → 你在自己终端执行 .github\ocf\ocf.py approve "<理由>" → executing → reporting → ready
```

人类可用的一键入口：

| 场景   | 入口           |
| ------ | -------------- |
| 交任务 | `/work-intake` |
| 要计划 | `/work-plan`   |
| 报 bug | `/bug-route`   |

## 日常操作

```text
# 看状态、事实、开关、阈值
python .github\ocf\ocf.py status
# 看审计
python .github\ocf\ocf.py journal 20
# 跑自检（输出分类为 environment / policy / system / ok，非 ok 即退出码 1）
python .github\ocf\ocf.py selftest
# 跑完整用例表 + 结构断言
python .github\ocf\tests\run.py
# 授权机器改某个受保护路径
python .github\ocf\ocf.py allow "docs/adr/0007-*.md"
# 撤销授权
python .github\ocf\ocf.py deny "docs/**"
```

## 修改

| 想改什么          | 怎么改                                                                                                        | 要重载吗 |
| ----------------- | ------------------------------------------------------------------------------------------------------------- | -------- |
| **门禁规则**      | 改 [`policy.toml`](.github/ocf/policy.toml) 的 `[[rule]]`。规则是数据，首个命中即生效，豁免放最前、兜底放最后 | 不用     |
| **阈值**          | 改 `[limits]`（`max_cmd_len` `max_cmd_stmts` `max_cmd_repeat` `fail_budget`）                                 | 不用     |
| **工具分类**      | 改 `[tools]` 各列表与 `[tools.field]` 的字段路径。新增工具**不必改代码**                                      | 不用     |
| **门禁开关**      | 改 `[system] enabled`。`false` = 人类交回控制权（跳过批准，允许机器改门禁代码；人类代码保护仍生效）           | 不用     |
| **自检金丝雀**    | 改 `[selftest.canary]`。金丝雀必须走真实 hook 入口，否则证明不了门禁还活着                                    | 不用     |
| **状态机**        | 改 `ocf.py` 的 `STATES` / `transition_gate_names`，并同步 `work-control-flow.md` 第 3 节                      | 不用     |
| **挂钩事件**      | 改 `.github/hooks/orchestrator.json`。命令串**只用 ASCII 且不含 `$`** —— 外层 shell 会插值                    | **要**   |
| **文案/规则说明** | 改 `work-control-flow.md`、`copilot-instructions.md`、各 `*.prompt.md` / `*.agent.md`                         | 不用     |

改完跑 `python .github\ocf\tests\run.py`。它会替你检查四件容易漏的事：
全 `.github` 纯 ASCII、markdown 相对链接可解析、agent 名字引用可解析、自保护 pattern 仍覆盖每个编排器文件。

⚠️ **改 `.github/**`前先把`system.enabled`设为`false`**：这些文件受自保护，而且自保护挂在
`enabled`上而不是「是否已批准」，所以`executing` 期间同样有效。人类手工编辑则无此限制。

✅ **`.github/` 下刻意保持纯 ASCII**（文案与注释全英文）。这是为了从根上消掉一整类编码 bug
（BOM/GBK/JSON 转义/控制台代码页），不是为了好看 —— 不要往里加中文。
代价是 `/work-intake` 这类菜单项的描述也是英文；想改中文只改 `description` / `argument-hint` 两行。

## 修复

### 自救路径（agent 被门禁锁死时）

**按顺序试，第 1 条解决不了再上第 2 条。**

| 症状                                       | 处理                                                                                                                                                                                                                                                                           |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 报 `[strict-fallback]`（策略文件解析失败） | **门禁只拒绝「变更」，读与思考类工具仍放行**，所以 agent 能帮你定位。先 `git diff .github/ocf/policy.toml` 看改动，再 `git checkout -- .github/ocf/policy.toml` 恢复到最后可用版本；或跑 `python .github\ocf\ocf.py selftest`，它会打印 `[policy] ... failed to parse: <原因>` |
| agent 什么都干不了（任何原因）             | 把 `.github/hooks/orchestrator.json` 改名为 `.json.off` 并重载窗口 → hooks 全部停用，一切恢复；修好后改回原名再重载                                                                                                                                                            |
| 想放行机器改门禁代码                       | 把 `policy.toml` 的 `system.enabled` 设为 `false`。⚠️ **前提是文件本身可解析**，文件坏了就必须先用上面两条                                                                                                                                                                     |
| 想彻底停用                                 | 见「卸载」                                                                                                                                                                                                                                                                     |

> 这里曾踩过一个坑：文档原先只写了「把 `system.enabled` 设为 `false`」，而那条路径**恰好在最需要它的时候不可用**（策略文件本身坏了就改不动它）；而且当时严格兜底把只读工具也一并拒绝，agent 连文件都读不到。两处都已修。

### 常见故障

| 症状                                            | 根因                                 | 处理                                                                             |
| ----------------------------------------------- | ------------------------------------ | -------------------------------------------------------------------------------- |
| hook 完全没反应                                 | 配置未热加载                         | 重载窗口；查 `Developer: Show Agent Debug Logs`                                  |
| 自检报 `[environment]` 说 hooks 未指向 `ocf.py` | 挂钩没接上或解释器名不对             | 改 `orchestrator.json` 后重载                                                    |
| 自检报 `[policy]`                               | 策略缺失或解析失败                   | 修 `policy.toml`；此时门禁**只放行只读与思考类**，一切变更被拒（见「自救路径」） |
| 自检报 `[system]`                               | 金丝雀未通过 = 门禁不再执行策略      | **最严重**。按提示修策略，不要绕过                                               |
| 工具调用只弹警告但照旧执行                      | 误用了 stderr + 非 0 退出码          | 拦截只能走 stdout `permissionDecision` + `exit 0`                                |
| hook 报错说命令里 `$f` 变空                     | hooks 命令串被外层 shell 插值        | 命令串去掉所有 `$`                                                               |
| 门禁总是拦我改文件                              | 状态还不是 `executing` / `reporting` | 走完 asking→planning，再由你在终端 `approve`                                     |
| 改了策略却不生效                                | 改的是别的文件                       | 唯一策略文件是 `.github/ocf/policy.toml`；`status` 会打印它                      |
| 一条命令里只有一个分号却被拦「语句过多」        | 引号内的 `;` 参与计数                | 已修：计数前先屏蔽引号内容与 `@{...}` 字面量                                     |
| 命令输出被截断 / 丢首行                         | 多行粘贴的输出归属不可靠             | 只发单行短命令                                                                   |

## 更新

1. 从上游拿到新版 `ocf.py` / `policy.toml` / `orchestrator.json` / `work-control-flow.md` / `tests/`
2. 覆盖（旧版 `ocf.sh`、`ocf.ps1` 已被取代，可直接删除）
3. 重载窗口
4. 跑 `selftest` 与 `tests/run.py` 确认；若 `facts` 键有变更，按 `work-control-flow.md` 第 5 节补齐
5. `.orchestrator/` 一般不需要迁移

## 卸载

删掉 `.github/hooks/` 与 `.orchestrator/` 即停止强制。
`.github/{ocf,agents,prompts}` 与两个 `.md` 是纯文本，留着无害。

## 设计取舍与已知限制

| 项                             | 说明                                                                                                                                                                                                                                 |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 失败侦测靠 agent 自觉          | hook 在工具**成功后**触发，无法自动捕获失败；靠 `ocf.py fail` 记账 + 审计留痕，**不是硬保证**                                                                                                                                        |
| `facts` 是纯文本               | 定位是防手滑与流程遗漏，**不防恶意对手**                                                                                                                                                                                             |
| 批准在文本层判定，非进程身份   | 靠「命令串 invoke 了入口脚本 + 出现人类专属子命令」拦截，并额外拦「把这类命令写进可执行文件」。**把命令编码后再写、或复用已存在的脚本仍可能绕过。** TTY 校验无效 —— agent 的终端也是真终端。彻底解决需要进程身份，超出 hook 模型能力 |
| 「一次只问一个问题」无代码判定 | 什么算「具体答复」无法用代码判定；系统**不做关键词识别**，判定交给 agent，只有纪律约束                                                                                                                                               |
| 门禁强度上限由模型决定         | 系统约束的是**流程与权限**，不是判断质量。它拦得住「提前动手」，拦不住「理解错了」，也不可能把弱模型提升到强模型                                                                                                                     |
| 未知工具靠名字启发             | 名字里没有动作动词的工具会走过；但只要它**带命令**就会按执行类工具判定，不会不受检查                                                                                                                                                 |
| 金丝雀要有人跑才有用           | 自检只在 SessionStart 与 `selftest` 时执行；若 hooks 整体不再投递，「自检没输出」就是唯一信号，而「没有信号」很容易被忽略                                                                                                            |
| 内联 agent hooks 未采用        | 它能把强制范围限定到单个 agent，但需要 `chat.useCustomAgentHooks` 设置，一旦关闭门禁会静默失效，所以仍用 workspace 级 hooks                                                                                                          |
