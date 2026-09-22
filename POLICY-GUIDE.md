# OCF 策略配置指南

- 字段与谓词词表 位于[`policy.toml`](.github/ocf/policy.toml) 里 `# OCF:VOCABULARY:BEGIN/END` 之间的区域 。
- 工作流描述 位于[`.github/work-control-flow.md`](.github/work-control-flow.md) 里 `<!-- OCF:REFERENCE:BEGIN/END -->` 之间的区域
- 每次提问时注入的引导词 位于[`.github/copilot-instructions.md`](.github/copilot-instructions.md)由 `reload` 生成。

---

## 1. 三条铁律

1. 按顺序解析，**首个命中即生效**
2. 解析失败**不降级为"没有策略"**，而是落到最严兜底：除永远允许与只读工具外一律拒绝。
3. 改配置要人类手工编辑；改完跑 `python .github/ocf/ocf.py reload`

---

## 2. 判定模型

```text
每个工具调用
  → 分类（visual / env / exec / write / read / session / unknown；最严在前，同时列出时取更严的）
  → 短路A：session  → allow
  → 短路B：read    → allow
  → 按顺序逐条 [[rule]]，首个命中即生效
  → 短路C：exec 类、命令读不出来、且 `system.enabled` 为真 → deny（fail-safe）
  → 短路D：未分类且名字像动作、且在需批准的状态 → [unknown_tool].action
  → 都没有命中 → allow（[default]）
```

**四种判定**：`allow` / `ask` / `deny` / `require_approval`

## 3. 规则

| 字段      | 含义                                                                                                                           |
| --------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `enabled` | 是否启用                                                                                                                       |
| `kind`    | `hard`：由 hook 保障功能。`soft`：无法被代码强制，由 `reload` 写进常驻引导词                                                   |
| `if`      | 命中情景。硬规则写工具条件（`class` / `tool` / `command_matches` / `path_matches` / `fact`…）；软规则写 `occasion`（对话时机） |
| `unless`  | 绕过情景                                                                                                                       |
| `result`  | 规则逻辑。硬规则是`allow`/`ask`/`deny`/`require_approval`；软规则是注入内容                                                    |
| `why`     | 规则描述：硬规则作为 hook 的返回 message 会直接展示给 agent，软规则会展示给人类                                                |

`surface` 仍然被旧形状接受，而现役策略里还有 3 条规则在用（两条自保护、一条自授权）——迁移完成前两者并存。

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

**加工具**：加进 `[tools]` 对应列表即可

**加金丝雀（门禁状态验证）**：`[selftest.canary]` 里至少一条必须 deny、一条必须 allow，才能同时证明
"该拦的还拦"和"该放的还放"。

**改阈值**：改规则中对应键（如 `command_length_over = 400`。
配置每次调用重读，改完立即生效；更改 **`.github/hooks/*.json` 需要重载窗口**。

---

## 4. 改完怎么验

```powershell
python .github\ocf\ocf.py selftest      # 分类输出：environment / policy / system / ok，非 ok 即退出码 1
python .github\ocf\tests\run.py         # 38 条用例 + 18 条结构断言
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
- 终端与编辑器工具的**折行**不可信
