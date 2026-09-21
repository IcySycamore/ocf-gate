# OCF 策略配置指南

面向人类读者。讲清 **`.github/ocf/policy.toml` 怎么读、怎么改、改完怎么验**。

> **警告：本文件第 4 节起的词表表格已经过期**（2026-09-22 起）。
> 它们描述的是旧规则形状（`on`/`surface`/`match`/`when`/`action`），其中被列出的
> `exempt_when_listed`、`length_over`、`statements_over`、`repeats_at_least` 已从引擎中删除，
> 规则名 `human-code` / `human-code-write` 已合并为 `protected-file`，
> `human-code-clear` 已更名为 `protected-list-clear`，`[limits]` 只剩 `fail_budget`。
>
> **权威出处只有两处**：`policy.toml` 自己的头部注释（词表，且由 `policy_findings` 对着代码校验），
> 以及 `.github/work-control-flow.md`（行为）。本文件里与它们冲突的地方，以那两处为准。
> 重复一份词表就会再过期一次 —— 这段表格建议删掉，等你决定。

---

## 1. 三条铁律

1. 按顺序解析，首个命中即生效
2. 解析失败 将采取默认的最激进配置策略
3. 改 `policy.toml` 需要人类手工编辑；改完跑 `python .github/ocf/ocf.py reload`
   把配置生成进常驻契约与 hooks 接线（该命令人类专属）。

---

## 2. 判定模型（先看这个，再看字段）

```
每个工具调用
  → 分类（visual / always_allow / read_only / edit / exec / action / unknown）
  → 短路①：always_allow  → allow
  → 短路②：read_only     → allow
  → 按顺序逐条 [[rule]]，首个命中即生效
  → 短路③：exec 类但读不到命令 → deny（fail-safe）
  → 短路④：未分类且名字像动作且在需批准的状态 → [unknown_tool].action
  → 都没有命中 → allow（[default]）
```

**四种判定**：`allow` / `ask`（编辑器弹一次确认）/ `deny` / `require_approval`（等同 deny，理由里带上"你去终端跑 approve"）。

**几个容易忘的点**

- 未分类的工具**只要带命令**就按 `exec` 判定
- `action` 类工具（如 `create_and_run_task`）**只要带命令也按 exec 判定**，所以执行类规则对它同样成立。
- `visual` 在分类顺序里最靠前，所以"只能读图片"的 `view_image` 仍然被拒

---

## 3. `[[rule]]` 的字段

| 字段                 | 必填 | 含义                                                                                                                                           |
| -------------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| `id`                 | 是   | 判定理由里会带上它（`[id] ...`），所以每条判定事后都可追溯。**测试也断言它**，改名会失败                                                       |
| `on`                 | 否   | 工具类：`edit` / `exec` / `action` / `visual` / `any`（默认 `any`）                                                                            |
| `surface`            | 否   | 读哪一层：`tool` / `command` / `path` / `write_target` / `content` / `any`（默认 `any`）                                                       |
| `match`              | 否   | 正则，对`surface` 取到的**每个候选值**分别匹配；`.*` 即全部命中                                                                                |
| `when`               | 否   | `always`（默认）/ `not_approved`（需批准且不在 acting 状态）/ `approved` / `enforced`（`system.enabled` 为真，**与状态无关**）/ `not_enforced` |
| `action`             | 否   | `allow` / `ask` / `deny` / `require_approval`（默认 `deny`）                                                                                   |
| `why`                | 建议 | 会直接展示给 agent。**写清"该怎么补前置条件"**，否则它只会重试                                                                                 |
| `only_if`            | 否   | 附加条件，成立才命中                                                                                                                           |
| `unless`             | 否   | 附加条件，成立则跳过本条                                                                                                                       |
| `exempt_when_listed` | 否   | 候选值若出现在该清单文件里，则从候选中剔除                                                                                                     |

**`surface` 对应什么**

- `tool`：工具名
- `command`：按 `[tools.field]` 的字段路径取出的命令串（取不到则本条跳过）
- `path`：编辑目标路径（可多个，逐个匹配）
- `write_target`：从命令里推断出的写入路径
- `content`：本次调用的内容与原始 `tool_input` 的 JSON

**`when = "enforced"` 是最容易被写错的一个**：自保护与自授权必须用它，**不能用 `not_approved`** —— 后者在 `executing` 下不成立，会让保护在干活期间正好失效。

---

## 4. 谓词（`only_if` / `unless` 里写哪个键）

| 键                 | 写法                                             | 含义                                                                         |
| ------------------ | ------------------------------------------------ | ---------------------------------------------------------------------------- |
| `fact`             | `{ fact = "must_consult", is = "yes" }`          | 事实等于某值                                                                 |
| `fact` + `is_set`  | `{ fact = "stack_env", is_set = true }`          | 事实非空                                                                     |
| `listed_in`        | `{ listed_in = ".orchestrator/human-code.txt" }` | **逐个候选值**判断它是否命中该清单（支持 glob、跳过 `#` 注释、大小写不敏感） |
| `content_matches`  | `{ content_matches = '正则' }`                   | 对内容与原始`tool_input` 的 JSON 匹配                                        |
| `length_over`      | `{ length_over = "max_cmd_len" }`                | 命令长度超过`[limits]` 里那个键                                              |
| `statements_over`  | `{ statements_over = "max_cmd_stmts" }`          | 语句数超过该阈值                                                             |
| `repeats_at_least` | `{ repeats_at_least = "max_cmd_repeat" }`        | 同一命令已出现至少该次数                                                     |
| `computed`         | `{ computed = "control_plane" }`                 | 见下                                                                         |

**谓词分两类，这是本文件最容易出错的地方**

- **上下文级**（`fact` / `content_matches` / `length_over` / `statements_over` / `repeats_at_least` / `computed`）：对整次调用只有一个真假值。
- **值级**（`listed_in`）：必须**逐个候选值**判断。曾经把它写成"整批取并集"，结果一条合法路径会污染同一次批量编辑的其它路径，而且报错指向了无关文件。

**两个 `computed` 标志**

- `control_plane`：**每一条**语句都以完整相对路径调用本编排器入口 → 命令豁免批准（让 agent 能读自己的状态）
- `invokes_entry`：**至少一条**语句如此 → 用于把"人类专属子命令"规则绑定到真实调用，避免提交信息里出现这个词就被拦

---

## 5. 段说明

| 段                                           | 作用                                                                                                                                  |
| -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `[system] enabled`                           | **唯一总开关**。`true` = 门禁全开（批准要求 + 自保护 + 自授权）；`false` = 人类交回控制权（跳过批准与自保护，**人类代码保护仍生效**） |
| `[paths]`                                    | `state_dir` / `human_code_list` / `allowed_edits_list`，均相对仓库根                                                                  |
| `[limits]`                                   | `max_cmd_len`(400) / `max_cmd_stmts`(3) / `max_cmd_repeat`(3) / `fail_budget`(2)。**每次调用重读，改完免重载**                        |
| `[approval]`                                 | `allowed_states`（允许动手的状态，默认 executing+reporting）/ `min_reason_len`（人类批准理由的最短长度）                              |
| `[unknown_tool] action`                      | 未分类工具且名字像动作、且在需批准状态时的判定（`allow`/`ask`/`deny`，默认 `ask`）                                                    |
| `[self_authorization] executable_extensions` | 哪些扩展名算"可执行文件"，用于拦住"把人类专属子命令写进脚本"                                                                          |
| `[selftest]`                                 | `[[selftest.canary]]` 金丝雀：`id` / `expect` / `payload`。**走真实 hook 入口**，是"门禁还活着"的唯一证明                             |
| `[tools]`                                    | 六个类：`edit` / `exec` / `action` / `visual` / `always_allow` / `read_only`                                                          |
| `[tools.field]`                              | 每个工具的命令藏在哪个字段（点号路径可下钻），如`create_and_run_task = ["task.command"]`                                              |

`DEFAULT_POLICY` 里 `always_allow` 与 `read_only` 带**内置默认值**，因为严格兜底策略由它们构成 —— 那两份清单就是"策略坏掉时还能做什么"的边界。

---

## 6. 出厂 22 条规则

| #   | id                         | 何时命中                                                                                          | 判定                              |
| --- | -------------------------- | ------------------------------------------------------------------------------------------------- | --------------------------------- |
| 1   | `plan-md-exempt`           | 编辑`.orchestrator/plan.md`                                                                       | allow（门禁自己要求这个产物）     |
| 2   | `failure-budget`           | 命令 且`must_consult=yes`                                                                         | deny（连续失败达预算，停下问人）  |
| 3   | `visual-tool`              | 视觉类工具                                                                                        | deny（机器永不截图/看图）         |
| 4   | `visual-command`           | 命令含`headless`/`screenshot`/`playwright`/`puppeteer`/`capture...image`                          | deny                              |
| 5   | `human-only-subcommand`    | 命令 且`invokes_entry` 且含 `approve`/`reject`/`confirm`/`allow`/`deny`/`human-code`              | deny                              |
| 6   | `advance-target`           | 命令里`advance` 到非允许状态                                                                      | deny（进入 executing 是人类行为） |
| 7   | `self-authorization-write` | `enforced` 下编辑可执行文件，且内容里 300 字符内含入口名与人类专属子命令                          | deny（自授权）                    |
| 8   | `self-protection-path`     | `enforced` 下编辑自保护路径                                                                       | deny                              |
| 9   | `self-protection-write`    | `enforced` 下命令写入自保护路径                                                                   | deny                              |
| 10  | `human-code`               | 编辑目标在`human-code.txt` 且不在 `allowed-edits.txt`                                             | deny（**逐路径**判定）            |
| 11  | `human-code-write`         | 命令写入路径同上                                                                                  | deny                              |
| 12  | `command-too-long`         | 命令长度 >`max_cmd_len`                                                                           | deny                              |
| 13  | `too-many-statements`      | 语句数 >`max_cmd_stmts`（屏蔽引号与 `@{...}` 后计数）                                             | deny                              |
| 14  | `silenced-output`          | 命令含`Out-Null`/`-Quiet`/`--quiet`/`$null` 重定向/`/dev/null`/`-WindowStyle Hidden`              | deny                              |
| 15  | `interactive`              | 命令含`Read-Host`/`ReadKey`/`-Verb RunAs`/`sudo`/`cmd /c`                                         | deny                              |
| 16  | `test-authorization`       | 测试命令 且`test_authorized≠yes`                                                                  | deny                              |
| 17  | `toolchain`                | 安装/探测命令 且`stack_env` 未声明                                                                | deny                              |
| 18  | `destructive`              | 含`rm -rf /`、`git push --force`、`drop table`、`git reset --hard`、`Remove-Item -Recurse -Force` | deny                              |
| 19  | `approval-required-edit`   | 编辑 且`not_approved`                                                                             | require_approval                  |
| 20  | `approval-required-exec`   | 命令 且`not_approved` 且非控制面                                                                  | require_approval                  |
| 21  | `approval-required-action` | action 类 且`not_approved`                                                                        | require_approval                  |
| 22  | `repeat`                   | 同一命令已出现 ≥`max_cmd_repeat` 次                                                               | ask（像是打转，交人判断）         |

批准类排在第 19-21 位、在全部精确规则之后，是刻意的：**理由永远是最具体的那条**，而不是笼统的"需要批准"。

---

## 7. 配方

**放宽一条**：改那条规则的 `match`，或在它前面插一条更窄的规则。别直接删 —— 删掉就失去记录。

**加一条禁止**（例如禁止改生产配置）：

```toml
[[rule]]
id = "no-prod-config"
on = "edit"
surface = "path"
match = '^config/prod/'
action = "deny"
why = "Production config is the human's. Hand it over instead."
```

位置决定语义：要让它压过批准规则，放在 `approval-required-*` **之前**。

**把 `ask` 变硬成 `deny`**：把该规则的 `action` 改成 `deny`。注意：`deny` 在任何审批逻辑之前被处理，**自动批准绕不过它**；`ask` 则可能被编辑器的自动批准吞掉。

**加工具**：加进 `[tools]` 对应列表即可，不必改代码。若它的命令不在 `command`/`code` 字段里，再往 `[tools.field]` 加一行点号路径。

**加只读工具**：加进 `read_only`。**只在它真的只读时加** —— 这会无条件放行，且严格兜底也依赖这份清单。

**加金丝雀**：一条必须 deny、一条必须 allow，才能同时证明"该拦的还拦"和"该放的还放"。

```toml
[[selftest.canary]]
id = "deny-visual-tool"
expect = "deny"
payload = { hook_event_name = "PreToolUse", tool_name = "view_image", tool_input = { filePath = "x.png" } }
```

**改阈值**：改 `[limits]`，改完立即生效，无需重载。

---

## 8. 改完怎么验

```powershell
python .github\ocf\ocf.py selftest      # 分类输出：environment / policy / system / ok，非 ok 即退出码 1
python .github\ocf\tests\run.py         # 33 条用例 + 5 条结构断言
python .github\ocf\ocf.py gate all      # 逐条看状态门禁的通过情况
```

自检的三个分类含义不同，**别混为一谈**：

- `[environment]`：Python、hooks 接线、状态目录 —— 环境问题，不是本系统的错
- `[policy]`：策略文件本身的问题 —— 改这个文件
- `[system]`：金丝雀未通过 —— **门禁不再执行策略**，最严重

---

## 9. 已知边界

- 规则读的是**文本**，不是语义。它无法预知"引号不闭合会让 shell 挂住"，也无法判断一句话是不是"有效答复"。这类事由 `PostToolUse` 事后发现（命令没有进展就注入警告），而不是事前拦截。
- `write_target`、未知工具分类等依赖启发式，方向刻意偏向"多拦一次"。
- 并不能拦下human和agent通过编码恶意绕过门禁的行为
- 策略文件解析失败会拒绝一切变更
