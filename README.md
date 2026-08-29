# AutoDev 4.0 alpha

AutoDev 是一个面向 Codex 的本地自主开发控制平面。正常使用只有一条路径：

```text
$autodev -> local stdio MCP -> AutoDev Core -> current Codex Commander -> fresh subagents
```

当前 Python 包版本是 `4.0.0a1`，Codex 插件版本是
`4.0.0-alpha.1`。Core 是 `.autodev/` canonical state 的唯一写入者；当前
Codex session 是唯一 Commander。AutoDev 不会自行提交产品改动、push、publish 或
deploy。

## 安装

需要 Python 3.11+、Git、Codex，以及至少一个 Git commit。克隆本仓库后，在仓库根目录
安装 Python 包与仓库内插件：

```bash
python3 -m pip install .
autodev version
autodev-mcp --version

codex plugin marketplace add "$(pwd)"
codex plugin add autodev@personal
```

然后打开一个新的 Codex thread，使 Skill 与 MCP 配置生效。插件通过本地 stdio 启动
`autodev-mcp --stdio`，不包含 hooks 或 UI。

## 唯一的正常工作流

在要开发的 Git 项目中，显式输入 `$autodev` 和目标；Skill 不会被隐式调用：

```text
$autodev 为这个项目增加可恢复的本地全文搜索，并做到 working MVP。
```

接下来由当前 Commander 完成整个控制循环：

1. 检查项目并在需要时初始化 `.autodev/`。
2. Grill 仍会实质改变结果的决策，包括目标、development strategy、成熟度目标、
   requirements、allowed paths、验收标准、验证命令和 Authority Envelope。
3. 当前 Commander 生成一份结构化 Proposal；AutoDev 不再启动另一个 Planner。用户只需
   一次明确确认，该确认同时覆盖 Proposal 与 Authority Envelope。
4. Core 返回下一项持久 Action。Commander 为每个 Planner、Worker、Reviewer 或
   Diagnostic Action 启动 fresh subagent；同时最多一个写入型 Worker，且它只能在
   Action 给出的隔离 workspace 中修改。
5. Commander 提交严格结果后继续取下一项 Action，直到 `ASK_HUMAN`、`PAUSED` 或
   `TARGET_REACHED`。

公开 Action 只有 `PLAN_PHASE`、`EXECUTE_TASK`、`RUN_IMMEDIATE_REVIEW`、
`RUN_DIAGNOSTIC`、`RUN_PHASE_REVIEW`、`ASK_HUMAN`、`PAUSED` 和
`TARGET_REACHED`。验证、diff/changed paths 推导、checkpoint、phase advancement 与
materialization 判断都在 Core 内完成；Agent 对改动和测试的声明不是可信证据。

`CHANGE`、`STAGED`、`CRITICAL` 是 development strategy。V3 canonical state 中仍可能
使用兼容字段名 `mode` 保存这一策略，但它不代表运行方式选择。

## 恢复、BLOCKED 与暂停

pending Action 写入 `.autodev/actions/`。Commander 或进程退出后，在同一项目的新
Codex thread 中显式调用：

```text
$autodev 继续 CAMP-001。
```

Core 会返回同一个 pending Action；不需要恢复旧对话或 specialist transcript。相同结果
可安全重试，冲突结果、未知 Action 或 stale revision 会被拒绝且不修改 canonical state。

遇到 `ASK_HUMAN`/`BLOCKED` 时，`$autodev` 只呈现 Core 持久化的问题与恢复选项。直接在
对话中回答即可；Commander 通过 `answer_blocker` 提交答案。不要手工编辑
`.autodev/`，也不要绕过 blocker。

需要停止时，显式要求 graceful pause：

```text
$autodev 在当前 Action 安全完成后暂停 CAMP-001。
```

若已有 Action，Core 先记录 pause request，待该 Action 提交并完成确定性处理后进入
`PAUSED`；没有 pending Action 时立即暂停。之后可在任意新的 Commander session 中用
`$autodev 继续 CAMP-001` 恢复。恢复依赖 canonical state，不依赖模型对话历史。

## Retarget 与目标写回

达到目标时，Core 自动尝试一次安全 materialization，把 Campaign private checkpoint
的累计增量写回用户工作区。它会核对 source fingerprint 并预检 patch；如果用户并发编辑
或 patch 冲突，不会覆盖现有文件，而是进入可恢复的 `BLOCKED`。

发生 materialization conflict 后，先在用户工作区解决已记录的冲突并保留需要的改动，
再显式请求：

```text
$autodev 重试 CAMP-001 的 materialization。
```

显式 `materialize_campaign` 只用于冲突解决后的重试。成功写回只发生一次。

只有已达到目标的 Campaign 才能显式 retarget 到更高成熟度：

```text
$autodev 把 CAMP-001 的目标提高到 release candidate 并继续。
```

新增 Requirement 或扩大产品 scope 时，不要把它伪装成 retarget；应从已有 checkpoint
提出新的 Campaign。

## 质量与隔离

| 条件 | Core 的唯一质量路由 |
| --- | --- |
| 普通 LOW/MEDIUM implementation、test、docs | `NONE`：Worker 自审 + Core 确定性验证 |
| HIGH、安全、迁移、权限扩大、公共 API、远程副作用 | `IMMEDIATE`：fresh read-only Reviewer |
| architecture、internal interface、shared internal data、integration wiring | `PHASE`：累计阶段 diff Review |
| 同一语义验证失败指纹连续两次 | `DIAGNOSTIC`：fresh read-only 根因诊断 |

Reviewer 与 Diagnostic 必须保持只读；任何写入都会被 Core 拒绝。强制质量预算耗尽会
`BLOCKED`，不会跳过检查。

## 从 V3 迁移到 V4

先在 V3 项目根目录做只读检查：

```bash
autodev migrate v3 --check
```

确认检查结果后执行原子迁移：

```bash
autodev migrate v3 --apply
```

迁移保留 Campaign private refs、Tasks、Evidence、checkpoint 与用户工作区内容，并在输出
中返回 `migration_id`。如果尚未创建任何 V4 Action，可以回滚：

```bash
autodev migrate v3 --rollback MIGRATION-ID
```

首次调用 V4 Action Protocol 并创建 Action 后，回滚会被永久拒绝。需要回滚时必须在
首次 `$autodev` Action 之前完成；`--check` 本身不会推进状态。

## Headless 与兼容 CLI

`autodev start`、`autodev campaign start`、`autodev campaign plan/approve`、
`autodev resume --campaign`、Codex exec/App Server engines 与 Fake engines 继续保留，
供 headless、CI、测试、debug 和恢复基础设施使用。它们不是正常交互入口，也不是另一套
产品工作流；需要真实模型的 legacy/headless 命令仍要求显式
`AUTODEV_LIVE_CODEX=1`。

正常的 `$autodev` MCP 路径不会启动 `autodev start`、`codex exec`、App Server、第二个
Commander 或第二个 Proposal Planner。

只读状态和派生报告仍可用于运维：

```bash
autodev campaign status CAMP-001
autodev report phase --campaign CAMP-001
autodev report requirements --campaign CAMP-001
autodev report release --campaign CAMP-001
```

## 开发验证

```bash
python3 -m unittest discover -s tests -v
python3 scripts/autodev.py validate
python3 -m build
git diff --check
```

普通测试使用 Fake Engine/Planner 和本地假 MCP/App Server，不调用模型。真实模型 Campaign
必须另行得到明确授权。
