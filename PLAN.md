# Action plan: make the gate provably in force on every harness, and adopt the runtime's full hook surface

> Durable development plan, kept in the repository root on purpose: the round-scoped slot
> `.orchestrator/plan.md` is swept when the machine enters `ready`, and a plan for a development
> phase has to outlive the round that produced it. Headings stay in English; every other line is in
> the language the human is using.

## Type

- [x] feature
- [x] enhancement
- [ ] refactor
- [x] fix
- [x] docs
- [ ] chore
- [x] test

## Summary

现状是：接线、引擎、事件名在两个 harness 上都兼容，但**仓库级 hook 是延迟加载的**，而 VS Code
侧的 agent-host 通道没有任何代码去解除这个延迟，所以门在 agent-host 会话里事实上没有在跑——文件
全对、`verify` 全绿、`selftest` 全绿，却什么也拦不住。这一阶段的目标有两个：把"门到底在不在岗"
变成一条命令、一次观察就能回答的事实；再把运行时已经提供、我们还没用的一半 hook 面（失败事件、
权限裁决、停止拦截）接进来，让状态机的裁决点从"工具调用之后"前移到"授权发生的当下"。

## Steps

1. **人工确认当前修复**（human-only，AI 不可代跑）：执行 `python .github\ocf\ocf.py reload`，
   随后 Reload Window。验收：`verify` 由 FAIL 转为 PASS。
2. **人工确认运行期判据**：新建一个 **本地 chat** 会话，观察该轮上下文是否出现 `OCF state:` 标记。
   验收：标记出现 ⇒「标记判据」成立，可写进文档。
3. **判定延迟加载假设**：新建一个 **agent-host-copilotcli** 会话，观察同一标记。
   验收：无标记 ⇒ 假设成立（仓库 hook 未被解除延迟）；有标记 ⇒ 假设被推翻，回退到日志/能力层再查。
4. **落地 T1-a 规避**：`reload` 额外生成一份**用户级**接线（`~/.copilot/hooks/`），它属于
   `HookOrigin: user`，随会话创建加载、不延迟。验收：第 3 步的会话由此出现标记。
5. **落地 T1-b 备选**：把同一份接线生成为 **agent plugin**（`plugin.json` +
   `com.github.copilot/hooks/hooks.json`），供 `chat.pluginLocations` 选择；插件 hook 可用
   `sessions.reloadPluginHooks` 免重载窗口生效。验收：装插件后不改窗口设置即出现标记。
6. **T2 文档定稿**：`session-start-briefing` 保持"仅 SessionStart 投递"（人类已确认），由常驻契约
   的 Two-channels 段承接存活性要求。验收：`generated-contract` 结构断言通过。
7. **~~接入 `postToolUseFailure`~~ — 已证伪，改为更正。** 该事件**不可由文件配置**：宿主的键映射只认
   十个事件（`SessionStart` / `SubagentStart` / `UserPromptSubmit` / `PreToolUse` / `PostToolUse` /
   `SubagentStop` / `Stop` / `PreCompact` / `SessionEnd` / `ErrorOccurred`），SDK 注释也明说
   `HookType` 里"Discovery emits the file-configurable subset"。`postToolUseFailure` 只在 SDK 回调
   里存在。失败的那一半**已有**来源：`PostToolUse` 的 `handle_post_tool` 解析 `exited with code N`。
   验收：本条不再需要实现，`work-control-flow.md` 已记录该边界。
8. **~~接入 `permissionRequest`~~ — 同上，已证伪。** 它是回调专属事件，写进接线文件只会装上一个
   永不触发 hook。以 hook 直接裁决权限需要扩展或插件的 SDK 回调，属扩展路线，不在本阶段。
9. **接入 `Stop`（运行时名为 `agentStop`）— 已完成。** `reporting` 未收尾时拒绝停止，原因直接给出
   `advance ready`；`executing`/`ready` 保持沉默，避免变成陷阱。验收：3 条用例（阻断 / executing
   沉默 / ready 沉默）全绿。
10. **补齐其余事件 — 已完成（缩小范围）。** `SubagentStart`、`SubagentStop`、`PreCompact`、
    `SessionEnd`、`ErrorOccurred` 已接线，**只写 journal、不产生裁决**，并把 payload 形状记录成证据
    （这些形状没有可读文档，只能看真实报文）。`notification` / `postResult` 属回调专属，不接线。
    验收：3 条静默用例 + `verify` 对 13 个 payload / 10 个事件的形状断言全绿。
11. **遵守禁用开关 — 部分完成。** `[hooks] enabled = false` 现在诚实静默（不再答 `allow`）；总开关
    `[system] enabled = false` 改为"不拦截、不 block、但继续报告"，见 F3。`disableAllHooks` /
    `disabledHooks` 属宿主侧配置，仍待在 `liveness.py` 里读并明说。
12. **输出协议适配层**：新增 `hook_adapter(host)`，按宿主选择输出形状（见下节）。
    验收：每个宿主的合法载荷各有一条用例。
13. **Codex / Claude 接线**：从同一张 `HOOK_EVENTS` 表生成第三、第四份接线，分别写入插件格式 1
    （`.claude-plugin/plugin.json` + `hooks/hooks.json`）与 Codex 的 `hooks.json`。验收：生成文件
    与渲染一致的结构断言通过。
14. **回归与文档**：跑离线用例表与结构断言；同步 `README.md` / `README_CH.md`（两份镜像必须一起改）。

> 每一步都是一个可以停下来检查的位置；顺序不会产生不可用的中间态（第 4、5 步互为备选，
> 任一条通过即可推进）。

## Tools

- `python .github\ocf\ocf.py reload` -- 人类在自己终端重生成四份产物 -- 唯一能让契约跟上代码的动作
- `python .github\ocf\liveness.py` -- 只读探针 -- 回答 ALIVE / NOT-INSTALLED / DISABLED / STALE / NO-EVIDENCE
- `python .github\ocf\tests\run.py` -- 离线用例表 + 结构断言 -- 每次改动后的最小回归
- `python .github\skills\quality-self-evaluation\scripts\score.py` -- 策略自评 -- 证据化的交付自检
- `chat.pluginLocations` / `sessions.reloadPluginHooks` -- 宿主侧插件装载与热重载 -- 让 hook 免重载窗口生效
- VS Code 日志 `GitHub Copilot Chat Hooks.log` -- 本地 agent 的 hook 执行记录 -- 判据来源之一

## Files

| Action | Path / Concept |
| ------ | -------------- |
| MODIFY | `.github/ocf/ocf.py` |
| MODIFY | `.github/ocf/policy.toml` |
| MODIFY | `.github/ocf/liveness.py` |
| ADD    | `.github/ocf/tests/cases.json`（用例追加） |
| MODIFY | `.github/ocf/tests/run.py` |
| MODIFY | `.github/work-control-flow.md` |
| MODIFY | `.github/hooks/orchestrator.json`（由 reload 重写） |
| ADD    | `.github/ocf/harness/agent-host/plugin.json` |
| ADD    | `.github/ocf/harness/agent-host/com.github.copilot/hooks/hooks.json` |
| ADD    | `.github/ocf/harness/claude/.claude-plugin/plugin.json` |
| ADD    | `.github/ocf/harness/claude/hooks/hooks.json` |
| ADD    | 用户级接线目标 `~/.copilot/hooks/`（由 reload 写入，不入库） |
| MODIFY | `.github/protected.txt`（新目录纳入保护） |
| MODIFY | `README.md` / `README_CH.md` |
| MODIFY | `.github/ocf/POLICY-GUIDE.md` (moved) / `POLICY-GUIDE_CH.md` |
| MODIFY | `release/build/engine_checks.py` |
| DELETE | 无 |

## Scope

- [ ] Small
- [x] Middle
- [ ] Big

- ADD **6** files
- DELETE **0** files
- MODIFY **10** files

- Unit testing / [x] Integration testing / [x] E2E testing（第 2、3 步是真机 E2E）
- Build / CI / [x] Deployment（`reload` + 窗口重载）/ no build

## Deliverables

1. 一条能回答"门在不在岗"的命令（`liveness.py`）+ 一个不依赖任何文件的判据（常驻契约的 Two-channels 段）。
2. 一份在 agent-host 会话里也能生效的接线（用户级或插件级），并说明它与 `.github/hooks` 的取舍。
3. 三张新的 hook 事件接进状态机：`postToolUseFailure`、`permissionRequest`、`agentStop`。
4. 一份按宿主分派的输出协议适配层，以及 Codex / Claude 两份新接线。
5. `README.md` / `README_CH.md` / 两份 POLICY-GUIDE 与本计划一致。

## Self-review

| ID  | Severity | Problem | Disposition |
| --- | -------- | ------- | ----------- |
| A1  | P0       | 上一轮把"探针读不到"写成了"harness 不支持"，并把这条错误结论写进了 §9 文档 | 已在本轮撤回：探针判决改为 NO-EVIDENCE，§9 重写；后续任何"不支持"结论必须以运行期证据为准 |
| A2  | P1       | 第 3 步的延迟加载假设是静态推断（三处 bundle 均无 `loadDeferredRepoHooks`），未经真机确认 | 第 3 步即为其确认点；若被推翻，第 4、5 步的依据随之重估 |
| A3  | P1       | 用户级接线写在用户目录，属"规则看不见的写入"，与 `install` 同类风险 | 只写 hooks 目录下的单一文件，不覆盖同名文件；在 reload 输出中显式报告写入路径 |
| A4  | P1       | `permissionRequest` 接管授权后，`approval-required-*` 规则会与原生权限系统重叠 | 保留两者：原生负责"是否询问/自动批准"，状态机负责"是否已获人类批准"，交集处明写优先级 |
| A5  | P2       | 四份接线意味着四处可能失效，而 `verify` 只检查一份 | 把 `liveness.py` 扩展为逐接线报告，并把结果并进 `verify` |
| A6  | P2       | Codex 的 `hooks.json` 事件名与输出协议未取证 | 第 13 步前先用插件格式 0 做一次金丝雀实验，取证后再落地 |
| A7  | P2       | 软规则投递没有金丝雀，hook 整体消失时静默 | 第 10 步顺带补一条"软规则文本必须到达"的探针 |
| **F1** | **P0** | `hook_enabled()` 在 `try` 之外。一次不完整的重构让入口点崩溃，宿主只看到 `hook errored`，人类看不到任何原因，且**每一次** PreToolUse 都被拒 | 已修：`cmd_hook` 的全部逻辑进入 `try`；崩溃变成可读的拒绝。`Command` 的模拟故障用例证明四条路径各自的行为 |
| **F2** | **P0** | 内部错误时**控制平面也被拒**，于是 `advance blocked` / `status` / `journal` 全部不可达——门坏掉时 agent 无法声明 blocked、无法留证 | 已修：内部错误时放行"每条语句都是 `ocf.py` 全相对路径调用"的命令与 `read`/`session` 工具，其余仍拒绝。验收：模拟故障下控制平面=allow、read=allow、write=deny |
| **F3** | **P0** | 总开关 `[system] enabled` **控制不了整个系统**：它在规则层被读，入口点早已跑完，所以开关关着也照样崩溃、照样 deny | 已修：开关在**读事件表之前**就被判断（字典查找不会失败）；关掉即 `PreToolUse`/`Stop` 完全静默（不答 `allow`——那会压掉编辑器自己的审批提示，比如没有门更宽松），而报告类事件继续跑，并多一句 `OCF: the master switch is off`。验收：用例 `the-master-switch-says-it-is-off`、`switch-off-opens-gate-code`（改为 expect_silent） |
| **F4** | **P1** | hooks 键在**三处**各写一份（事件表、词表、校验用 `DEFAULT_POLICY`），加事件时校验就骂 policy 非法 | 已修：`DEFAULT_POLICY["hooks"]` 由 `HOOK_EVENTS` 生成 |
| **F5** | **P1** | 三处测试夹具**读取现场 policy 的安装状态**，于是主人一关 `[hooks] enabled`，59 个用例全部静默失败、`prompt-log` 与 `policy-vocabulary` 一起误报 | 已修：夹具按 section 强制开关；`parse_output` 支持两种回答形状。用例 59→65 |
| **F6** | **P1** | **注释迁移未完成**：`policy.toml` 仍有注释（68 行手写 + 81 行生成词表区） | **已完成**：手写 68 行迁入配置指南 §5（中英双份），生成词表区迁入指南新的 §6.，`policy.toml` 现在是**零注释**的纯数据（627 -> 540 行，`tomllib` 解析结果逐字节相同）。指南同时从仓库根移到 `.github/ocf/`：根目录不随 `install` 分发，所以 `policy.toml` 原先那句“解释都在 POLICY-GUIDE.md”在部署目标里指向一个不存在的文件。`check_policy_comment_migration` 改为双向硬断言：13 项术语须同时在两份指南出现，且 `policy.toml` 里出现任何注释行即失败 |
