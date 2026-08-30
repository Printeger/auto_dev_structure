# AutoDev 4.0 alpha

AutoDev 是一个面向 Codex 的本地自主开发控制平面。它把用户确认过的开发目标转换为可验证、可暂停、可恢复的 Campaign，并在隔离工作区中完成开发。

正常使用只有一条路径：

```text
$autodev -> local stdio MCP -> AutoDev Core -> current Codex Commander -> fresh subagents
```

当前 Python 包版本是 `4.0.0a1`，Codex 插件版本是 `4.0.0-alpha.1`。这是 alpha 版本，建议先在可恢复的 Git 项目中试用。

## 30 秒快速开始

在 AutoDev 仓库根目录安装 Python 包和 Codex 插件：

```bash
python3 -m pip install .
autodev version
autodev-mcp --version

codex plugin marketplace add "$(pwd)"
codex plugin add autodev@personal
```

安装后必须打开一个新的 Codex thread。旧 thread 不会自动加载刚安装的 Skill。

进入要开发的 Git 项目，确认项目已有 commit 且工作区干净：

```bash
git rev-parse --verify HEAD
git status --short
```

然后在新 Codex thread 中显式输入 `$autodev` 和开发目标：

```text
$autodev 为这个项目实现本地全文搜索。开始前先和我明确需求、权限范围和验收标准。
```

正常情况下，AutoDev 会先提问并展示 Proposal 与 Authority Envelope。只有你明确确认后，它才会开始执行开发 Action。

## 使用前准备

### 环境要求

- Python 3.11 或更高版本。
- Git。
- 支持本地 Plugin、Skill 和 stdio MCP 的 Codex。
- 目标项目至少有一个 Git commit。
- 目标项目的源代码工作区保持干净。

### 为什么至少需要一个 commit

AutoDev 从当前 `HEAD` 创建 Campaign 私有 ref 和隔离 worktree。这个 commit 是计算 diff、检测并发修改、保存 checkpoint、崩溃恢复和安全写回的稳定基线。

如果项目还没有 commit，可以先保存当前文件：

```bash
git add .
git commit -m "Initial project baseline"
```

空项目也可以建立空基线：

```bash
git commit --allow-empty -m "Initial project baseline"
```

如果 `git status --short` 有输出，先检查并保存这些改动。不要让 AutoDev 在一组来源不明的未提交修改上建立 Campaign。

## 安装并验证

从 AutoDev 仓库根目录执行：

```bash
python3 -m pip install .
autodev version
autodev-mcp --version
```

两个版本命令都应输出 `4.0.0a1`。

添加仓库内的 `personal` marketplace，并安装插件：

```bash
codex plugin marketplace add "$(pwd)"
codex plugin add autodev@personal
codex plugin list --json
```

列表中应出现 `autodev@personal`，并且 `installed`、`enabled` 都是 `true`。插件通过本地 stdio 启动 `autodev-mcp --stdio`，不包含 hooks 或 UI。

完成安装后，新开一个 Codex thread。若新 thread 仍看不到 `$autodev`，重启 Codex 后再检查插件列表。

## 第一次运行

### 1. 提出目标

在目标项目的新 Codex thread 中输入：

```text
$autodev <你想实现的开发目标>
```

`$autodev` 是显式触发词。Skill 不会根据普通开发请求隐式启动。

### 2. 回答关键问题

对于新 Campaign，Commander 应当澄清仍会改变结果的决策，例如：

- 要解决的问题和明确不做的范围。
- development strategy 与成熟度目标。
- 功能 Requirement 和验收标准。
- 允许修改和禁止修改的路径。
- 必须执行的验证命令。
- 网络、凭据、提交和远程操作等 Authority Envelope。

问题数量不是固定的。Prompt 已足够具体时可以少问，但不能跳过 Proposal 确认。

### 3. 确认 Proposal

Commander 会展示一份结构化 Proposal 和 Authority Envelope。它们应包含目标、范围、Task、风险、路径限制、验证方式和禁止操作。

你需要明确回复是否批准。该确认同时覆盖 Proposal 和 Authority Envelope，并且只确认一次。

在确认之前，AutoDev 不应启动 Worker 或修改产品代码。如果 Codex 没有展示 Proposal 就开始写代码，请立即停止并查看“故障排查”。

### 4. 执行 Campaign

确认后，Core 持久化 Campaign 并返回下一项 Action。Commander 会为 Planner、Worker、Reviewer 或 Diagnostic Action 启动 fresh subagent。

同时最多有一个写入型 Worker。Worker 只能修改 Action 指定的隔离 workspace，Reviewer 和 Diagnostic 必须只读。

Core 自行计算 diff、changed paths 和验证结果。Agent 关于“改了哪些文件”或“测试已通过”的声明不被直接视为证据。

### 5. 达到目标

达到目标后，Core 会尝试一次安全 materialization，把 Campaign 私有 checkpoint 的累计增量写回用户工作区。

AutoDev 不会自动提交产品改动，也不会自行 push、publish 或 deploy。你需要检查最终 diff 和测试结果，再决定是否提交。

## 如何写有效的 Prompt

一个实用 Prompt 最好说明目标、范围、成熟度和验收方式：

```text
$autodev <目标>：

- 必须实现：<关键行为>
- 不包含：<明确排除项>
- 允许修改：<路径>
- 验收标准：<可观察结果>
- 验证命令：<测试或检查命令>
- 成熟度目标：<working MVP / integrated system / release candidate>
```

例如：

```text
$autodev 为本项目实现本地全文搜索的 working MVP：

- 索引 docs/ 下的 Markdown 文件
- 支持关键词查询，返回文件路径、标题和匹配片段
- 重启后复用索引，索引损坏时可以重建
- 提供 search "关键词" CLI 命令
- 添加索引、查询和重建测试
- 只允许修改 src/search、tests/search 和 CLI 注册代码
```

“本地全文搜索”只是示例功能，不是 AutoDev 自带或固定实现的功能。AutoDev 可以处理其他明确、可验证且符合 Authority Envelope 的开发目标。

## Development strategy 与成熟度

`CHANGE`、`STAGED`、`CRITICAL` 是开发策略，不是执行 backend。

| Strategy | 适用场景 | 行为 |
| --- | --- | --- |
| `CHANGE` | 范围明确的小改动 | 只运行 `IMPLEMENT`，目标为 `CHANGE_COMPLETE` |
| `STAGED` | 普通的多阶段功能开发 | 按固定阶段推进，到选定成熟度停止 |
| `CRITICAL` | 高风险或需要额外人工控制的开发 | 使用完整阶段，并在关键阶段和最终写回前增加人工 gate |

V3 canonical state 中可能仍用兼容字段名 `mode` 保存 strategy，但它不表示运行方式选择。AutoDev 没有 Managed/Native 或 `execution_backend` 选项。

成熟度目标从低到高为：

| 目标 | 含义 |
| --- | --- |
| `ARCHITECTURE_BASELINE` | 架构与基础骨架成立 |
| `WORKING_MVP` | 核心流程可运行，并通过组件级验证 |
| `INTEGRATED_SYSTEM` | 组件之间及外部边界完成集成验证 |
| `RELEASE_CANDIDATE` | 完成 hardening，达到候选发布质量 |

`WORKING_MVP` 不是通用的自动验收标准。Proposal 仍需把“可工作”翻译为可观察行为、测试和验证命令。

## 执行期间

### 查看状态

可以在对话中询问 `$autodev`，也可以使用只读 CLI：

```bash
autodev campaign status CAMP-001
autodev report phase --campaign CAMP-001
autodev report requirements --campaign CAMP-001
autodev report release --campaign CAMP-001
```

不要手工编辑 `.autodev/`。它是 Core 管理的 canonical state。

### 暂停

请求在当前 Action 安全完成后暂停：

```text
$autodev 在当前 Action 安全完成后暂停 CAMP-001。
```

如果已有 pending Action，Core 会记录 pause request，等该 Action 完成确定性处理后进入 `PAUSED`。没有 pending Action 时可以立即暂停。

### 恢复

在同一项目的任意新 Codex thread 中输入：

```text
$autodev 继续 CAMP-001。
```

pending Action 保存在 `.autodev/actions/`。恢复依赖 canonical state，不依赖旧对话或 specialist transcript。

### BLOCKED 和 ASK_HUMAN

当 Core 返回 `ASK_HUMAN` 或 `BLOCKED` 时，直接回答它显示的问题。Commander 会通过 `answer_blocker` 提交答案。

不要绕过 blocker，也不要把凭据或 secret 写进 `.autodev/`。需要超出 Authority Envelope 的操作时，AutoDev 必须重新请求授权。

## 完成后

达到 `TARGET_REACHED` 后，先检查写回结果：

```bash
git status --short
git diff
```

运行项目自己的测试和验收命令。确认结果正确后，再由你自行提交：

```bash
git add <确认过的文件>
git commit -m "Implement <feature>"
```

AutoDev 不会替你创建产品 commit，也不会自动推送远端。

### Materialization 冲突

写回前，Core 会核对 source fingerprint 并预检 patch。如果用户并发修改或 patch 冲突，它不会覆盖现有文件，而会进入可恢复的 `BLOCKED`。

先在用户工作区解决已记录的冲突并保留需要的改动，然后请求：

```text
$autodev 重试 CAMP-001 的 materialization。
```

显式 `materialize_campaign` 只用于冲突解决后的重试。成功写回只发生一次。

### 提高成熟度目标

只有已达到目标的 Campaign 才能 retarget 到更高成熟度：

```text
$autodev 把 CAMP-001 的目标提高到 release candidate 并继续。
```

新增 Requirement 或扩大产品 scope 时，应从已有 checkpoint 提出新 Campaign，而不是伪装成 retarget。

## 故障排查

### 输入 `$autodev` 后直接开始修改代码

新 Campaign 必须先展示 Proposal 和 Authority Envelope，并获得一次明确确认。没有确认就修改产品代码，不是正常 AutoDev 流程。

先停止当前任务，然后检查：

```bash
command -v autodev
command -v autodev-mcp
codex plugin list --json
```

确认 `autodev@personal` 已安装且启用。安装插件之前已经打开的 thread 不会自动加载 Skill，请新开 thread；必要时重启 Codex。

如果项目中也没有 `.autodev/state.json`，通常说明 MCP/Core 根本没有初始化，Codex 很可能把 Prompt 当成了普通开发指令。

### MCP 命令找不到

运行：

```bash
autodev version
autodev-mcp --version
```

如果命令不存在，重新安装 Python 包，并确保安装目录位于启动 Codex 时可见的 `PATH` 中。随后重启 Codex 并新开 thread。

### 项目基线不满足要求

运行：

```bash
git rev-parse --verify HEAD
git status --short
```

第一条失败表示项目没有 commit。第二条有输出表示存在未提交的源代码改动。检查并保存这些改动后再启动 Campaign。

### 恢复时找不到 Campaign

确认新 thread 打开的是原来的项目根目录，并检查 `.autodev/state.json` 是否存在。Campaign 状态属于项目，不属于某个 Codex 对话。

## 从 V3 迁移到 V4

先在 V3 项目根目录做只读检查：

```bash
autodev migrate v3 --check
```

确认检查结果后执行原子迁移：

```bash
autodev migrate v3 --apply
```

迁移保留 Campaign private refs、Tasks、Evidence、checkpoint 与用户工作区内容，并返回 `migration_id`。

如果尚未创建任何 V4 Action，可以回滚：

```bash
autodev migrate v3 --rollback MIGRATION-ID
```

首次创建 V4 Action 后，回滚会被永久拒绝。需要回滚时必须在首次 `$autodev` Action 之前完成；`--check` 本身不会推进状态。

## 质量与隔离参考

| 条件 | Core 的质量路由 |
| --- | --- |
| 普通 LOW/MEDIUM implementation、test、docs | `NONE`：Worker 自审 + Core 确定性验证 |
| HIGH、安全、迁移、权限扩大、公共 API、远程副作用 | `IMMEDIATE`：fresh read-only Reviewer |
| architecture、internal interface、shared internal data、integration wiring | `PHASE`：累计阶段 diff Review |
| 同一语义验证失败指纹连续两次 | `DIAGNOSTIC`：fresh read-only 根因诊断 |

Reviewer 与 Diagnostic 必须只读。强制质量预算耗尽时 Campaign 会进入 `BLOCKED`，不会跳过检查。

公开 Action 只有 `PLAN_PHASE`、`EXECUTE_TASK`、`RUN_IMMEDIATE_REVIEW`、`RUN_DIAGNOSTIC`、`RUN_PHASE_REVIEW`、`ASK_HUMAN`、`PAUSED` 和 `TARGET_REACHED`。

验证、路径检查、checkpoint、phase advancement 和 materialization 判断由 Core 内部完成。

## Headless 与兼容 CLI

`autodev start`、`autodev campaign start`、`autodev campaign plan/approve`、`autodev resume --campaign`、Codex exec/App Server engines 与 Fake engines 继续保留。

这些接口用于 headless、CI、测试、debug 和恢复基础设施，不是普通用户的交互入口，也不是另一种产品工作流。

需要真实模型的 legacy/headless 命令仍要求显式设置 `AUTODEV_LIVE_CODEX=1`。正常 `$autodev` MCP 路径不会启动 `codex exec`、App Server 或第二个 Commander。

## 开发者验证

```bash
python3 -m unittest discover -s tests -v
python3 scripts/autodev.py validate
python3 -m build
git diff --check
```

普通测试使用 Fake Engine、Fake Planner 和本地假 MCP/App Server，不调用真实模型。真实模型 Campaign 必须另行得到明确授权。
