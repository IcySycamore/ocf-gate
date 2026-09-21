# OCF 重构设计（v2）

面向人类读者。目标：把当前"两份手工同步实现 + 规则写死在代码里"的形态，重构成"**一份跨平台实现 + 策略数据化 + 可自检可测试**"。

---

## 0. 这次重构要消除的风险

当前门禁由 `ocf.ps1`（43.5 KB）与 `ocf.sh`（32.3 KB）两份手工同步的实现组成，占整套系统 87 KB 中的 76 KB。本次会话在它们身上发现 5 个缺陷，**全部在门禁层**：

1. `if (Test-Enforced -and -not $isOcf)` —— PowerShell 把开头裸词当命令，变量从未被读取，"控制面豁免"成了死代码。后果：`enforce=on` 时连 `ocf status` 都被拦，agent 无法推进任何状态。**过严到完全不可用，且只在 enforce=on 下显形。**
2. 控制面豁免用子串匹配：`Write-Host "ocf.ps1"; <任意命令>` 即继承豁免、跳过批准门禁。**过松。**
3. `create_and_run_task` 的命令在 `tool_input.task.command`，ps1 的属性访问取不到 → `$c` 为空直接放行。**过松，且只有 ps1 有。**
4. 自授权内容检查只读 `content` / `newString`，`multi_replace` 与 notebook 的字段完全看不见。**过松。**
5. `clip` 里用了 `…`(U+2026)，文件自称纯 ASCII 却被自己破坏。**编码类。**

共同点：**没有一条是业务逻辑错误，全部是"用文本匹配去逼近语义判定"以及"两份实现分叉"。**

危险的根源不在这些洞本身，而在于**门禁失效是无声的**：它既可以静默放行（你以为有保护），也可以静默锁死（你以为是自己操作错了）。因此本设计把"门禁可被测试、失效可被发现"当作第一优先级。

---

## 1. 目标与非目标

**目标**

- 一份实现，跨 Windows / Linux / macOS，语义唯一。
- 全部门禁行为由**配置文件**驱动，改策略不改代码。
- 状态只能由 CLI 改写；agent 不能绕过。
- 首次对话即自检；失效与环境污染必须**可见**，且被正确归类。
- 门禁逻辑可离线单元测试；本次 5 个缺陷各留一条回归用例。

**非目标**

- 不追求"防住有意的对手"。文本层挡不住，这条写进人类文档。
- 不试图提升模型能力。系统约束的是流程与权限，不是判断质量。
- 不做热加载。策略与 hooks 变更后由人类重载窗口，这是有意选择。

---

## 2. 语言与依赖

**选 Python 3，依赖通过 `requirements.txt` 装载；Docker 只用于跑测试与 CI。**

| 方案         | 跨平台一致性           | 可测试性                 | 编码安全                        | 前提                           |
| ------------ | ---------------------- | ------------------------ | ------------------------------- | ------------------------------ |
| POSIX sh     | 差（Windows 默认没有） | 差                       | 靠工具链                        | —                              |
| PowerShell 7 | 好                     | 中                       | 中（仍有 5.1/7 差异与解析陷阱） | 需装 PS7，Windows 自带的是 5.1 |
| **Python 3** | **好**                 | **好（可直接跑用例表）** | **好（编码处处显式）**          | Python 3.7+                    |

决定性理由：它同时解决第 0 节的两大风险来源 —— 一门语言只有一份逻辑，且门禁规则可以用一张用例表离线验证。标准库自带 JSON，可直接丢掉现在手写的 `json_str`（本身就是 bug 源）。

### 依赖边界（关键约束）

**hook 入口只用标准库，绝不 import 任何第三方包。** 原因：hook 是 VS Code 在**宿主**上直接启动的进程，无法用容器包裹；而一旦入口依赖未安装的包，"环境没装好"就会让门禁**静默 fail-open** —— 正是本设计要消灭的那类故障。

因此：

- `ocf.py` 运行时只需解释器。TOML 用 `tomllib`（3.11+），低于 3.11 时用 `tomli` 兜底；两者都取不到时**不降级为"无策略"**，而是报 `[environment]` 并保持最严兜底。
- 第三方依赖：**没有**。门禁入口只需标准库；用例表也只需 CPython（曾用过 `pytest`，实测它根本
  收集不到 `run.py`，已移除 —— 一个从不生效的依赖比没有依赖更糟）。
- Docker 用于**测试与 CI 的复现环境**，不参与运行时。

打包形态：

    .github/ocf/ocf.py             唯一实现（入口只用标准库）
    .github/ocf/policy.toml        出厂策略（随包分发）
    .github/ocf/requirements.txt   运行时依赖（标记条件，≥3.11 时为空）
    .github/ocf/tests/             用例表 + 运行器（只需 CPython）
    .github/work-control-flow.md   规则全文（原先挂在 skill 的 references/ 下，第 12 节已移出）
    Dockerfile                     只用于跑用例
    .github/hooks/orchestrator.json   只负责把 hook 事件指向 ocf.py

hook 命令保持一行，用平台覆盖解决解释器名差异（`py` / `python3` / `python`）；入口失败必须 fail-safe（拒绝并报 `[environment]`）。

---

## 3. 架构

    VS Code hook ──stdin JSON──> ocf.py hook <event> ──判定──> stdout permissionDecision
                                        │
                                        └──读/写──> .orchestrator/{state,facts,journal.log,plan.md}

三层职责：

- **策略层**：`policy.toml`。全部规则、阈值、动作都在这里，代码只负责解释它。
- **判定层**：`ocf.py`。把所有输入归一到统一的 `Verdict`，不关心具体规则内容。
- **状态层**：`.orchestrator/`。唯一写入口 `write_state()`。

`Verdict` 只有四种：`allow` / `ask` / `deny` / `require_approval`。任何一条规则产出一个，规则之间"首个命中即生效"。

---

## 4. 策略文件（配置驱动门禁）

**只有一份策略文件**：`.github/ocf/policy.toml`，没有覆盖层。路径均相对于仓库根。它不存在或解析失败时使用**内置兜底**，而兜底方向是“最严”（除 `always_allow` 工具外一律拒绝），绝不降级为“无策略”。原 `.orchestrator/config` 作废（见第 8 节）。

    [system]
    enabled = true              # false = 跳过"必须先批准"（相当于旧的 enforce=off）
    human_code_list = ".orchestrator/human-code.txt"

    [limits]
    max_cmd_len = 400
    max_cmd_stmts = 3
    max_cmd_repeat = 3
    fail_budget = 2

    [unknown_tool]
    action = "ask"              # allow | ask | deny，仅作用于未获批状态

    [approval]
    required_for = ["edit", "exec"]
    min_reason_len = 8

    [[rule]]
    id = "visual-tools"
    surface = "tool"            # tool | command | path
    when = "always"             # always | not_approved | approved
    match = "^(screenshot_page|view_image|run_playwright_code)$"
    action = "deny"
    why = "机器永不截图。请人类截图并在下一条消息附上。"

    [[rule]]
    id = "human-only-subcommands"
    surface = "command"
    match = "\\bocf\\s+(approve|reject|confirm|allow|deny|human-code)\\b"
    action = "deny"
    why = "这些子命令只能在人类自己的终端执行。"

    [[rule]]
    id = "self-protection"
    surface = "path"
    when = "enforced"         # 注意：不是 not_approved。否则在 executing 里反而关掉了自保护
    match = "^\\.github/(hooks|ocf|agents|prompts)/|^\\.github/work-control-flow\\.md$|^\\.orchestrator/(state|facts)$"
    action = "deny"
    why = "编排器自身的文件。人类手工改，或把 system.enabled 设为 false。"

`when` 的取值与含义：`always` / `not_approved`（需批准且不在 acting 状态）/ `approved` / `enforced`（`system.enabled == true`，与当前状态无关）/ `not_enforced`。**自保护与自授权必须用 `enforced`**，因为它们要在 acting 期间也有效。

    [[rule]]
    id = "destructive"
    surface = "command"
    match = "rm\\s+-rf\\s+/[^.]|git\\s+push\\s+.*--force|Remove-Item.*-Recurse.*-Force"
    action = "deny"
    why = "破坏性命令交给人类。"

这样带来的好处：

- 加/删一条门禁**不需要改代码**，也就不需要重新审视整套实现 —— 直接缩小了"改门禁反而改坏门禁"的暴露面。
- "单独的命令调用门禁"、"全局门禁"、"编辑门禁"、"要求人类审查"分别对应 `surface = command / system / path` 与 `action = require_approval`。
- 阈值改动即时生效（脚本每次调用重读），不需要重载窗口。

**保持不变的两条**：长命令与多语句仍然限制（这是有意设计）；误报要持续减少，做法是把每个误报都变成一条用例。

---

## 5. 状态存储与"唯一写入口"

不变式：**状态文件只由 `ocf.py` 写入。** agent 不能直接改，只能通过 CLI 调用触发写入。两个例外都在 CLI 内部：

1. hook 初始化（人类第一条消息触发 `ready -> asking`）；
2. 人类批准环节（`ocf.py approve`）。

落地措施：

- 所有写入收敛到单个 `write_state()`，其它地方只读。
- `state`、`facts`、`policy.toml` 在自保护规则内（第 4 节 `self-protection`）。
- **完整性检查**：读到 `state` 不是 6 个合法值之一时，不当作 `ready` 继续，而是归类为环境/篡改问题并**拒绝继续**（fail-safe）。这一条取代了现在"读不出来就默认 ready"的宽松行为。
- `policy.toml` 解析失败时**不回退到"无策略"**（那等于门禁全开），而是按第 6 节报错并保持最严兜底。

---

## 6. 自检与问题分类

新增 `ocf.py selftest`，并在 `SessionStart` 与首次 `UserPromptSubmit` 自动执行一次。

自检项：

1. 解释器可用、版本符合要求。
2. `policy.toml` 能解析；失败时明确指出是**策略**问题。
3. 状态目录可读写。
4. **金丝雀判定**：构造一个必须被拒的合成 PreToolUse 输入（如 `view_image`）与一个必须放行的输入，走**真实的 hook 入口**，断言结果。这是唯一能证明"门禁还活着"的手段 —— 正是它缺失导致第 0 节第 1 条长期未被发现。

输出必须分类，而不是笼统一句失败：

    [environment] 未找到 Python 3（py/python3/python 均不可用）
    [environment] hooks 似乎未加载：本次未收到任何 hook 事件
    [policy]      .orchestrator/policy.toml 第 12 行解析失败
    [system]      金丝雀未通过：预期 deny，实际 allow  ← 这条最严重，failsafe 为拒绝一切

明确原则（对应你的第 6 条）：**环境与配置问题不是本系统的错误。** 例如终端在被 agent 调用时偶发注入奇怪的 `ctrl+U`，或 VS Code 的 hooks 未加载 —— 系统只负责**如实报告并标注**，不去做脆弱的绕行。而 `[system]` 类必须 fail-safe。

---

## 7. 保持"软"的部分

这些没有代码可判定，只能靠模型，文档里不再宣称是硬保证：

- **一次问一个问题**：改为偏好。可以一次问多个；对注意力良好的模型这条基本会被遵守。系统约束的是流程与权限，**不是判断质量**，也不可能把弱模型提升到强模型的水准。
- 独立审计（派 `plan-auditor`）、要求看画面时说明截什么：同样是偏好 + 流程提示。
- 人类文档里要直说：**门禁的强度上限由模型决定。**

---

## 8. 移除清单

- `ocf.sh`、`ocf.ps1`（由 `ocf.py` 取代）。
- 手写 JSON 提取（`json_str`）：改用标准库，顺带消掉"文本正则匹配嵌套字段"这一整类缺陷（第 0 节第 3、4 条）。
- 手写的 glob→regex 字符串手术：收敛到一处实现。
- 三处重复的自保护清单：已合并，现改由策略文件承载。
- `.orchestrator/config`：开关与阀值已并入 `policy.toml`，该文件作废并删除（留着一个“看起来像开关”的死文件本身就是陷阱）。
- `.github/skills/work-control-flow/SKILL.md` 与整个 `skills/` 目录：见第 12 节。
- "命令长度按字节还是字符"的实现差异：一门语言之后自然消失。

---

## 9. 测试策略

一张用例表，每条 = `(输入 JSON, 期望 Verdict, 期望 rule id)`，离线运行，不依赖 VS Code：

    .github/ocf/tests/cases.json   用例表（一个文件，便于自上而下通读）
    .github/ocf/tests/run.py       运行器（只需 CPython，无测试框架）

运行器为每条用例搭一个临时仓库，把用例作为 **真实 hook 入口的子进程** 跑一遍（而非直接调用判定函数），
并断言三件事：判定结果、命中的 rule id、进程退出码为 0 且 stderr 为空。“入口本身坏掉”因此也会失败。
再加一条全 `.github` 纯 ASCII 断言（容忍前导 BOM，跳过构建产物）。当前 25 条用例 + 1 条 ASCII 断言全过。

必含的回归用例（对应第 0 节的 5 个缺陷）：

1. 完整相对路径的控制面命令 → allow；仅提及 `ocf.py` 字样后接命令 → deny；追加真实工作 → deny。
2. `create_and_run_task`（命令在 `task.command`）→ deny；在 executing 下同样 deny。
3. `multi_replace_string_in_file` 里嵌 `ocf.py approve` 写入 `.ps1` → deny。
4. 含 `@{...}` 的命令 → 3 段（不误报）；`(a; b; c; d)` → 4 段；未闭合括号 → 仍计 4 段。
5. 全 `.github` 纯 ASCII 断言。

另含：人类专属子命令必须压过控制面豁免、未知工具带命令时按 exec 判定、只读工具不得被升级为
需批准、plan.md 在批准前可写、自保护在 executing 下仍然生效、开关关闭时确实放行网关代码、
human-code glob 生效与 allowed-edits 豁免生效、静默输出／超长命令／重复命令、以及
**策略文件损坏时必须落到最严兜底而不是门禁全开**。

这是本次重构最重要的产出：**让门禁失效变成一条失败的测试，而不是一个静默的行为。**

---

## 10. 迁移步骤

1. ✅ 落地 `ocf.py` + `policy.toml` + 用例表；已用真实 payload 跑通（27/27 用例 + 4 条结构断言）。
2. ✅ 切换 `.github/hooks/orchestrator.json` 指向新入口，并加上 SessionStart 自检。
3. ⏳ **人类**：重载窗口，跑 `python .github/ocf/ocf.py selftest` 与用例表。
4. ⏳ **人类**：确认无事后，把 `policy.toml` 的 `system.enabled` 改为 true，重载窗口。
5. ⏳ 观察一段时间后删除 `ocf.ps1` / `ocf.sh`（`.orchestrator/config` 已删）。
6. ✅ README 已按新架构整体重写。

回滚：hooks 配置改回一行即可。但要注意 **`d:\PROJECT\agent` 不是 git 仓库**，删除不可回滚，
所以旧脚本保留到确认稳定，旧 README 暂存为 `README.md.bak`，复核后可删。

步骤 3、4 必须由人类做，原因有两条：一是 hooks 无热加载，重载只能由人操作；二是 `enabled=false` 期间
门禁是关的，而“把门禁打开”这个动作本身绝不能由 agent 完成。

---

## 11. 已定

1. 实现改用 Python 3；依赖通过 `requirements.txt` 装载，Docker 只用于跑测试。
2. 不保留 `ocf.sh` 作为降级：那会把刚消掉的双实现风险请回来。缺 Python 时由自检报 `[environment]` 并拒绝执行，而不是悄悄降级。
3. 重载继续由人工完成；hooks 变更后需重载窗口。
4. **策略文件只有一份**：`.github/ocf/policy.toml`，无覆盖层。所有路径相对仓库根。
5. **中文文档边界**：README 与本文档中文；`.github/` 下全英文且纯 ASCII。
6. **接受 fail-safe**：内部异常一律拒绝，策略不可读时落到“除永远允许的工具外一律拒绝”的最严兜底。
   自检是第一层发现手段，fail-safe 是第二层，二者不可互相替代 —— 自检必须在被运行时才算数。

---

## 12. 文件收敛（第二轮审查）

三处重复被识别并处置：

| 对象                                        | 判断                                                                                                                                                                         | 处置                                                                                                                                                 |
| ------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `.github/skills/work-control-flow/SKILL.md` | **确认多余**。它只剩三件事，其中两件是副本：入口点（全仓库第 4 份，且已经过期过一次）、指向 system.md（`copilot-instructions.md` 已做）。唯一独有价值是“给 system.md 当门牌” | 删除；`system.md` 移到 `.github/work-control-flow.md`；入口点从 4 份减到 2 份（常驻契约 + 规则文档第 2 节）。`orchestrator.agent.md` 改为指向第 2 节 |
| `grill-me` / `grill-with-docs`              | **逐字级重复**。核心指令块除 “one at a time, waiting for feedback...” 这半个从句外完全相同，后者是前者的超集                                                                 | **未处理**：它们在用户级 `.agents/skills/` 下，其它项目也在用，属人类决定。建议合并成一个带 docs 开关的技能                                          |
| `criterion-picker` vs `diagnose`            | **并非子集**。diagnose 的 10 条是“如何搭反馈回路”，criterion-picker 的 3 条是“选哪种手段”，且多出 diagnose 没有的“纯静态推导可能根本不需要回路”这一档                        | 保留；在其文件里加一句指向 diagnose phase 1 的构造手段清单，取消“第二份菜单”的嫌疑                                                                   |

同时新增两条守卫，把“漏改即开洞”变成失败测试：

- `markdown-links`：`.github/**` 与仓库根的 markdown 相对链接必须可解析。它在同一次改动中就抓到了 README 指向已移动文件，以及 README 声称存在、实际却没有的 `Dockerfile`。
- `gate-files-protected`：拿策略自己的 pattern 去匹配每个必须受保护的编排器文件。移动文件却忘记改 pattern，在这里会失败而不是静默开洞。

以及修掉一个真实漏洞：`self-protection-write`（命令写路径）原本不含 `.github/work-control-flow.md`，
意味着在 `executing` 下可以用一条命令重写规则文档。已修，并由上面的守卫长期看住。

**过程教训**：`grep_search` 在文件刚被移动/删除后可能返回陈旧索引，此时必须用文件系统核实再动手。

---

## 13. 未解决：系统分不出“人类答复”与“自动答复”（2026-09-21）

### 事故
Auto-copilot 模式下，agent 调 `vscode_askQuestions` 后收到一条**自动回复**
（"The user is not available to respond..."）。agent 把它当成授权，在**非 git 仓库**里删文件、重构文档。
由人类指出。

### 为什么没拦住 —— 两层，都要认
1. **门禁当时是关的。** `system.enabled = false`（迁移期）。若为 `true`，状态是 `asking`，
   `approval-required-edit` / `approval-required-exec` 会**结构性拒绝**每一个编辑与命令，
   与 agent 如何理解那条回复无关。用例表的 `plain-edit-needs-approval` 与 `plain-command-needs-approval`
   就是这条的证明。**网是存在的，是我们把它关了。**
2. **但这是设计问题，不只是模型问题。** 第 7 节把“敷衍或自动的回复不算答复”降级为**偏好**，
   并写下“系统约束的是流程与权限，不是判断质量”。这条权衡在本例中是错的：
   在 `enabled=false` 期间、以及 `executing` 期间，任何“人类是否真的答复了”的判断都**没有代码**。

### 唯一的候选信号，以及它可能不可用
可用的只有 `UserPromptSubmit`。但审计显示：本会话 `human reply` 递增到 **17** 次，
而人类实际消息远少于此，且其中 `#11`/`#12` 相隔 **13 秒**、中间没有任何人类消息。
**推断**：`UserPromptSubmit` 可能也为机器生成的轮次触发。若成立，它就不能作为“人类真的说话了”的信号，
任何基于它的修复都会无效（甚至更糟：会被自动答复自己解除）。

这是推断，不是测量。因此加入**临时插桩**：`ocf.py` 现在把每次 `UserPromptSubmit` 的时间戳、
会话前缀与正文前 160 字写进 `.orchestrator/prompt-log`，由 `run.py` 的
`prompt-instrumentation` 断言看守。下次再出现自动回复时，这份日志会给出确定答案，然后删掉插桩。

### 三个候选修复（待定，不擅自上）
| 方案 | 做法 | 代价 / 风险 |
| --- | --- | --- |
| A. 钉死“提问后停止” | 从 agent 的工具许可里去掉 `vscode/askQuestions`，要求只用聊天文本提问并结束回合 | 结构上关掉带内“伪答复”通道；代价是失去结构化提问（选项、免自由发挥） |
| B. 挂起门禁 | 调 `askQuestions` 时置 `awaiting_human`，在人类**用自己的终端**清除前，拒绝所有变更类动作（控制面 CLI 仍放行，以便记录答复） | 不依赖任何“谁在说话”的判断，必然可靠；代价是每次提问要人类多敲一条命令 |
| C. 收窄关闭开关 | `enabled=false` 只放开“编排器自身文件的维护”，不再等于“可自由作业” | 改动开关语义；但直击本次事故的根因 |

**在信号被测定之前不落地任何依赖它的规则** —— 否则失败方向会是“把 agent 锁死”，那是我们一路在避免的另一类事故。

### 事后查明：这条“自动回复”是编辑器自己注入的，而且可以关（2026-09-21）
在 VS Code 核心包（`workbench.desktop.main.js`）里找到：

     if (info?.permissionLevel === "autopilot" || configService.getValue("chat.autoReply")) {
         let W = info?.modeInfo?.permissionLevel === "autopilot" ? "Autopilot mode" : "Auto-reply enabled";

同处可见注入文案的字面量：`"The user is not available to answer your question. Choose a pragmatic option
best aligned with the context..."`。机制是：提问会建立 `pendingQuestionCarousels`（`blockOnResponse`），
而上述两个条件（**或**关系）之一成立时，VS Code 便**替人类把问题答了**；两者都关时它真的等待人类。

因此新增方案 **D（首选，且不需要写任何规则）**：把权限档退出 autopilot，并在用户设置里设
`"chat.autoReply": false`。相关键（均在核心包中注册）：`chat.tools.global.autoApprove`、
`chat.tools.terminal.enableAutoApprove`、`chat.agent.terminal.autoApprove`、
`chat.autopilot.advanced.enabled`、`chat.permissions.autopilot`。

**方案优先级因此改为：D 先做 → 用插桩确认 → 再判断 A/B 是否仍需。** 若 D 生效，A（删掉提问工具）
就不再值得付代价：它是为一个可以直接关掉的行为而牺牲一个有用的能力。

### 相邻漏洞：任何一条消息都能解除失败预算
批准不受影响 —— `needs_approval` 只由 `state` 决定，而 `executing` 只能由人类在自己终端执行
`approve` 写入，聊天消息无论来自谁都不能改变状态。但**另一个口子存在**：`UserPromptSubmit` 会清除
`must_consult`。若自动回复也能触发它，那么自动回复不仅能骗过 agent，还能**解开失败预算锁**。
同一病灶、低一级严重度，修法与 D 同时生效。
