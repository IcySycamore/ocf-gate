# OCF 策略配置指南

面向人类读者。讲清 **`.github/ocf/policy.toml` 怎么读、怎么改、改完怎么验**。

**词的权威出处只有两处**，本文件不复制它们：

- [`policy.toml`](.github/ocf/policy.toml) 自己的头部注释 —— 字段与谓词词表。它就在规则旁边，
  而且由 `policy_findings` 对着代码校验，写错了会被报出来。
- [`.github/work-control-flow.md`](.github/work-control-flow.md) —— 行为：每条规则何时命中、
  每个门禁要什么。

本文件曾经复制过这两份词表（字段表、谓词表、段表、22 条规则表）。四张都过期了 —— 表格没人校验，
改代码时不会有人想起它。所以删掉，只留"怎么走、怎么改、怎么验"。

---

## 1. 三条铁律

1. 按顺序解析，**首个命中即生效**；豁免放最前，兜底放最后。
2. 解析失败**不降级为"没有策略"**，而是落到最严兜底：除永远允许与只读工具外一律拒绝。
3. 改配置要人类手工编辑；改完跑 `python .github/ocf/ocf.py reload`
   把配置生成进常驻契约与 hooks 接线。**该命令人类专属** —— 它能改写模型读到的规则。

---

## 2. 判定模型（先看这个，再去看字段）

```text
每个工具调用
  → 分类（visual / always_allow / read_only / edit / exec / action / unknown）
  → 短路①：always_allow  → allow
  → 短路②：read_only     → allow
  → 按顺序逐条 [[rule]]，首个命中即生效
  → 短路③：exec 类但读不到命令 → deny（fail-safe）
  → 短路④：未分类且名字像动作、且在需批准的状态 → [unknown_tool].action
  → 都没有命中 → allow（[default]）
```

**四种判定**：`allow` / `ask`（编辑器弹一次确认）/ `deny` / `require_approval`（等同 deny，
理由里带上"你去终端跑 approve"）。

**几个容易忘的点**

- 未分类的工具**只要带命令**就按 `exec` 判定。
- `action` 类工具（如 `create_and_run_task`）**只要带命令也按 exec 判定**，所以执行类规则对它同样成立。
- `visual` 在分类顺序里最靠前，所以"只能读图片"的 `view_image` 仍然被拒。
- `deny` 在任何审批逻辑**之前**被处理，所以自动批准绕不过它；`ask` 则可能被编辑器的自动批准吞掉。
  凡"必须成立"的事，用 `deny`。

---

## 3. 规则怎么写

每条规则是一句话：**当 `if` 里的每个条件都成立，就执行 `result`**。五个字段，硬软两类共用同一套：

| 字段      | 含义                                                                                                                         |
| --------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `enabled` | 是否启用                                                                                                                     |
| `kind`    | `hard`：由 hook 读，给出裁决。`soft`：无法被代码强制（没人能检查一句话写没写），由 `reload` 写进常驻契约，由模型执行         |
| `if`      | 命中情景。硬规则写工具条件（`class` / `tool` / `command_matches` / `path_matches` / 事实…）；软规则写 `occasion`（对话时机） |
| `unless`  | 绕过情景。硬规则写条件表（代码判）；软规则写一句话（模型判）                                                                 |
| `result`  | 规则逻辑。硬规则是 `allow`/`ask`/`deny`/`require_approval`；软规则是要注入的那段话                                           |
| `why`     | 规则描述：一句话，会直接展示给 agent，所以要写清"该怎么补前置条件"                                                           |

**没有 `surface` 字段了**。一次动作本来就带好几段文字（工具名、命令、每个路径、要写入的内容），
所以由条件自己指明它量的是哪一段：`command_*` 量命令，`path_*` 量路径。谓词分两类，这是最容易错的地方：

- **上下文级**：对整次调用只有一个真假值。
- **值级**（`path_matches` / `touches_protected`）：必须**逐个候选值**判断，而且只看真实路径。
  曾经写成"整批取并集"，于是批量编辑里一条合法路径会把其它路径一起拖下水，报错还指向无关文件。

加一条禁止，照这个形状写：

```toml
[[rule]]
id      = "no-prod-config"
enabled = true
result  = "deny"
why     = "生产配置是人类的，改完交给他。"
unless  = { computed = "control_plane" }      # 可省；这是唯一真需要豁免时的写法

[rule.if]
class        = "edit"
path_matches = '^config/prod/'
```

位置决定语义：要压过批准规则，放在 `approval-required-*` **之前**。要变硬，
把 `result` 从 `ask` 改成 `deny`（见 §2 最后一条）。

**加工具**：加进 `[tools]` 对应列表即可，不必改代码。命令不在 `command`/`code` 字段里时，
往 `[tools.field]` 加一行点号路径。**加只读工具要谨慎**：那是无条件放行，且严格兜底也依赖这份清单。

**加金丝雀**：`[selftest.canary]` 里至少一条必须 deny、一条必须 allow，才能同时证明
"该拦的还拦"和"该放的还放"。金丝雀走真实 hook 入口，是"门禁还活着"的唯一证明。

**改阈值**：直接改规则里那个数字（如 `command_length_over = 400`）。`[limits]` 只剩 `fail_budget`。
配置每次调用重读，改完立即生效；**只有 `.github/hooks/*.json` 需要重载窗口**。

---

## 4. 改完怎么验

```powershell
python .github\ocf\ocf.py selftest      # 分类输出：environment / policy / system / ok，非 ok 即退出码 1
python .github\ocf\tests\run.py         # 38 条用例 + 14 条结构断言
python .github\ocf\ocf.py gate all      # 逐条看状态门禁的通过情况
python .github\ocf\ocf.py reload        # 人类专属：把配置重新生成进常驻契约与 hooks 接线
```

自检的三个分类含义不同，**别混为一谈**：

- `[environment]`：Python、hooks 接线、状态目录 —— 环境问题，不是本系统的错
- `[policy]`：策略文件本身的问题 —— 改这个文件
- `[system]`：金丝雀未通过 —— **门禁不再执行策略**，最严重

`generated-instructions` 与 `hooks-wiring` 两条断言比对"配置渲染出的内容"与"磁盘上的文件"，
所以改了配置忘了 `reload` 会**失败**，而不是静默漂移。

---

## 5. 已知边界

- 规则读的是**文本**，不是语义。它无法预知"引号不闭合会让 shell 挂住"，也无法判断一句话是不是
  "有效答复"。这类事由 `PostToolUse` 事后发现（命令没有进展就注入警告），而不是事前拦截。
- `write_target`、未知工具分类等依赖启发式，方向刻意偏向"多拦一次"。
- 并不能拦下人类和 agent 通过编码恶意绕过门禁的行为。
- 策略文件解析失败会拒绝一切变更。
- 终端与编辑器工具的**折行**不可信：判断长文本里有没有多余空格要用布尔比较或 `repr`，别肉眼核对。
