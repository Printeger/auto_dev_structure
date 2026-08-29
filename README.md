# AutoDev 2.0

AutoDev 是一个面向 Codex CLI 的本地、政策优先开发控制平面。它把需求、Task、状态、权限、验证、审查、证据、恢复和完成条件保存在项目中；Codex 只负责一次有边界的实现或审查。

## 当前状态

AutoDev 2.0 的核心工作流和 release smoke 验收已经完成。当前版本仍为 `2.0.0a1`，表示功能已实现并通过验收，但尚未承诺稳定 API、长期兼容或 PyPI 正式发布。

验收证据：

- 普通测试 `85/85` 通过，全部使用 `FakeCodexRunner` 或本地假进程，不调用模型。
- wheel 构建、隔离安装、`init` 和 `validate` 通过。
- 真实 BUILD + LOW smoke 启动一个 Builder、零个 Reviewer。
- smoke 只修改 `greeting.py`，验证通过，Task 为 `ACCEPTED`，Project 为 `COMPLETE`。
- completion gate 没有创建额外 Codex attempt。

这些结果表示“工作流开发与当前验收完成”。它不表示已经发布，也不授权 AutoDev 自动 commit、push、publish、deploy 或修改远程系统。

## 适用场景

AutoDev 适合单仓库、串行写入、要求结果可恢复且可审计的开发任务。每个 Task 都有冻结合同、允许路径、验收条件和确定性验证命令。

当前核心不包含多仓库调度、并行写入、Dashboard、通知、自动发布、原生 Windows 进程语义或长期模型会话。

## 前置条件

- Python 3.11+
- Git，且目标仓库已有至少一个 commit
- Codex CLI 已安装并登录
- Linux 上 Codex sandbox 可正常启动

先验证 Codex 登录与 sandbox：

```bash
codex login status
codex sandbox -- /bin/true
```

第二条命令必须返回退出码 `0`。它不调用模型。Ubuntu 上若出现 `RTM_NEWADDR`，通常应检查 AppArmor 与 bubblewrap，而不是启用 legacy Landlock 或降低 BUILD 权限。

## 安装

从源码安装：

```bash
cd /path/to/auto_dev/auto_dev_structure
python3 -m pip install .
autodev version
```

当前唯一直接运行依赖为 `jsonschema>=4.26,<5`。

## 快速开始

### 1. 初始化目标项目

```bash
autodev init /path/to/project --name my-project
cd /path/to/project
```

`init` 安装 `.autodev/`、需求模板和 Builder 定义。默认遇到路径冲突时不写入；`--merge` 只补缺失文件，不覆盖已有文件。

初始化不会复制 AutoDev 源码或框架测试，也不会改写用户已有的 `.codex/config.toml`。

首次运行前，目标仓库自己的源码、`docs/` 和 `.codex/` 必须形成 Git commit。AutoDev 不替用户提交；未跟踪或已修改的非 `.autodev/**` 文件都会触发干净基线拒绝。

### 2. 编写需求并激活

编辑 `docs/REQUIREMENTS.md`，为每条需求提供稳定 ID、优先级、可观察验收信号和状态。

```markdown
| ID | Priority | Requirement | Acceptance signal | Status |
| --- | --- | --- | --- | --- |
| REQ-001 | MUST | Build greeting API. | Unit tests pass. | ACCEPTED |
```

然后验证并激活项目：

```bash
autodev validate
autodev activate
```

### 3. 创建 Task

```bash
autodev task create \
  --id TASK-001 \
  --title "Implement greeting" \
  --risk LOW \
  --quality-mode BUILD \
  --requirements REQ-001
```

命令创建 `.autodev/tasks/TASK-001/contract.json`。在 DRAFT 阶段补全目标、变更类别、允许路径、范围外事项、验收条件、验证命令和禁止动作。

关键字段示例：

```json
{
  "objective": "Implement build_greeting(name).",
  "change_classes": ["implementation"],
  "allowed_paths": ["greeting.py"],
  "out_of_scope": ["Changing tests", "Adding dependencies"],
  "acceptance_criteria": [
    {"id": "AC-001", "description": "Ada returns Hello, Ada!"}
  ],
  "validation_commands": [
    {
      "argv": ["python3", "-m", "unittest", "-v"],
      "cwd": ".",
      "timeout": 60
    }
  ],
  "prohibited_actions": ["Commit", "Push", "Publish"]
}
```

验证命令必须使用 `argv[]`、项目相对 `cwd` 和 timeout。Task 不能提供 shell 字符串或环境变量。

### 4. 冻结 Task

```bash
autodev task ready TASK-001
autodev task show TASK-001
```

READY 会保存 `contract.json` 的 SHA-256，并生成只读投影 `contract.md`。哈希不一致时 AutoDev 会在 claim 前拒绝运行。

修改冻结合同必须显式 reopen：

```bash
autodev task reopen TASK-001 --reason "acceptance contract changed"
```

### 5. 运行 doctor

```bash
AUTODEV_LIVE_CODEX=1 autodev doctor --json
```

`doctor` 检查 Python、Git、最终 Codex 命令解析、配置加载、登录、sandbox 预检、live 授权、Git HEAD、干净基线和 canonical state。

doctor 的帮助、版本、登录和 sandbox 检查不调用模型。`AUTODEV_LIVE_CODEX=1` 只是让授权项变为 ready，不会单独启动 Codex attempt。

### 6. 运行一个 Task

```bash
AUTODEV_LIVE_CODEX=1 autodev run --task TASK-001
```

默认 `run` 接受一个 Task 后停止。让调度器继续执行 READY Task，需要显式连续模式：

```bash
AUTODEV_LIVE_CODEX=1 \
  autodev run --until complete-or-blocked
```

### 7. 查看状态与证据

```bash
autodev status --json
autodev task show TASK-001
autodev evidence TASK-001
autodev logs --run RUN-ID
```

成功 Task 应为 `ACCEPTED`，并引用 evidence ID。只有满足所有完成条件后，Project 才会被推导为 `COMPLETE`。

### 8. 停止与恢复

```bash
autodev stop
AUTODEV_LIVE_CODEX=1 autodev resume
AUTODEV_LIVE_CODEX=1 autodev resume --recover-stale
```

`stop` 会触发进程组 interrupt、terminate 和最终 kill。普通 resume 使用 fresh attempt，不恢复旧模型对话。

`--recover-stale` 只在同主机 PID 已确认死亡后回收遗留锁或 workspace；活动锁永远不会被抢占。

## 工作原理

AutoDev 把模型输出当作“提案”，而不是事实或状态写入命令。最终状态由确定性控制平面根据合同、diff、验证和审查证据决定。

```text
Requirements
    |
    v
Frozen Task contract
    |
    v
Admission: auth + config/login + sandbox + Git baseline
    |
    v
Claim -> isolated Git worktree -> fresh Builder
    |
    v
Path policy -> deterministic validation -> optional Reviewer
    |
    v
Evidence + binary patch + source fingerprint check
    |
    v
Apply checkpoint -> ACCEPTED -> derived COMPLETE
```

### 运行前准入

Runner 在锁、Task claim 和模型进程之前检查：

1. `AUTODEV_LIVE_CODEX=1` 是否存在。
2. canonical state 和冻结 Task 哈希是否合法。
3. 目标仓库是否有 Git HEAD。
4. 除 `.autodev/**` 外源码基线是否干净。
5. Builder 与 Reviewer permission profile 的 Linux sandbox 是否可启动。

任一检查失败都会 fail-closed。AutoDev 不会领取 Task、创建模型 attempt 或自动改成 `:danger-full-access`。

### Task 选择与 claim

Task 按优先级、依赖是否满足、创建时间和 Task ID 确定性排序。同一时刻只允许一个写入 Task、一个项目锁和一个隔离 worktree。

claim 使用 canonical revision 做乐观并发检查。只有 `ControlPlane` 能写顶层状态；Builder 和 Reviewer 都不能直接修改 `.autodev/`。

### 隔离 Builder

每次 attempt 都是 fresh Codex execution。BUILD 默认使用 `:workspace` permission profile，MCP servers 和 hooks 被清空，审批策略为 `never`。

Builder 只能在隔离 Git worktree 中工作。合同的 `allowed_paths` 和 policy 的 `protected_paths` 会在接受模型结果后再次独立检查。

### 确定性验证

AutoDev 使用 `shell=False` 执行合同中的 `argv[]`。可执行文件和工作目录必须位于 policy allowlist，超时会形成结构化失败记录。

模型声称“测试通过”不构成证据。只有 Runner 实际执行并记录为 `returncode: 0` 的验证才参与验收。

### 质量路由

| 条件 | 质量门 |
| --- | --- |
| BUILD + LOW/MEDIUM，无特殊变更类 | Builder 自审 + 路径检查 + 验证 + evidence；不启动 Reviewer |
| HIGH | fresh `:read-only` Reviewer |
| 架构、公共接口、安全、迁移、共享 schema/data | fresh Reviewer |
| milestone integration 或达到 rework 阈值 | integration checks + Reviewer |
| HARDENING | 完整检查 + Reviewer |

Reviewer 只收到冻结 Task、相关接口、diff 和验证证据，不接收 Builder 的推理历史。

### Evidence 与 checkpoint

Runner 为 contract、proposal、diff、validation、review 和 checkpoint 记录哈希。成功结果以 binary-safe patch 保存，再应用到源工作区。

应用 patch 前会重新计算源目录 fingerprint。如果用户在运行期间修改了源码，AutoDev 进入 `PAUSED` 并保留 checkpoint，不覆盖用户内容。

### 完成推导

Agent 无权宣布项目完成。`COMPLETE` 必须同时满足：

- 所有 blocking Task 已 ACCEPTED。
- 每个 MUST requirement 都有 accepted evidence。
- 没有 blocking debt、当前 Task、run、lock 或 blocker。
- project-level full validation 通过。

## 架构

### 模块边界

```text
autodev CLI
  |
  +-> ControlPlane.execute(Command) -> CommandResult
  |     schema / revision / transition / atomic state / events
  |
  +-> RunController.run(RunRequest) -> RunOutcome
        selection / lock / worktree / validation / review / evidence
          |
          `-> ExecutionEngine
                preflight(...) + execute(AttemptRequest)
                  |- CodexExecEngine
                  `- FakeCodexRunner
```

`ControlPlane` 是 canonical state 的唯一写入者。`RunController` 编排一次或有限循环的 Task 生命周期。`ExecutionEngine` 只跨越一次进程边界。

CLI 只负责解析和渲染，不包含业务状态转移。Codex 是执行依赖，不是生命周期权威。

### 目标项目布局

```text
.autodev/
  manifest.json
  config.json
  policy.json
  state.json
  debt.json
  tasks/<TASK-ID>/
  runs/<RUN-ID>/
  locks/
  workspaces/
  migrations/
.codex/agents/
  autodev-builder.toml
docs/
  REQUIREMENTS.md
```

`.autodev/` 是 V2 唯一运行真相。`.agent/` 只用于 V1 迁移输入或冻结备份，迁移后不再作为 live state 读取。

### 状态机

```text
Project:
BOOTSTRAP -> ACTIVE -> COMPLETE
                |-> PAUSED
                |-> BLOCKED
                |-> STOPPED
                `-> FAILED

Task:
DRAFT -> READY -> CLAIMED -> RUNNING -> VALIDATING
                                      -> REVIEWING -> ACCEPTED
```

Task 还可以进入 `DEFERRED`、`BLOCKED` 或 `CANCELLED`。非法转移会在任何写入前被拒绝。

Attempt outcome 为 `PASS | PASS_WITH_DEBT | REWORK | NO_PROGRESS | INFRA_FAILURE | BLOCKED | STOPPED`。

### 信任边界

- 人负责需求、合同、显式 live 授权和产品决策。
- AutoDev 负责状态、准入、路径、验证、审查路由、证据和完成推导。
- Codex 负责单次有边界的实现或审查提案。
- Git 提供基线、隔离 worktree、patch 和并发保护。
- Codex 选择 Linux sandbox 后端；AutoDev 只表达 permission intent。

## Runtime 与权限

初始化后的默认 policy：

```json
{
  "runtime": {
    "mode": "codex-sandbox",
    "build_permission_profile": ":workspace",
    "review_permission_profile": ":read-only"
  }
}
```

AutoDev 不暴露 bubblewrap/Landlock 作为普通策略选项。`features.use_legacy_landlock` 不受支持，也不会被生成、持久化或推荐。

### Linux sandbox 预检

在 Codex 0.144.5 上，预检实际使用：

```bash
codex sandbox -- /bin/true
```

预检不调用模型。失败会被归为 runtime/environment failure，并在 Task claim 与模型启动前停止。

诊断分类包括 legacy/profile 不兼容、bubblewrap bootstrap、namespace/container 限制、Codex 配置错误、一般环境失败和 Agent/Task 失败。

这些类别是第一层路由，不替代系统诊断。例如 `RTM_NEWADDR` 在裸机 Ubuntu 24.04 上也可能由 AppArmor user-namespace 限制造成。

### 显式 external-sandbox

受信任的 Docker/devcontainer 可以把外层容器作为安全边界。该模式必须同时修改项目 policy 并设置第二道确认门：

```json
{
  "runtime": {
    "mode": "external-sandbox",
    "build_permission_profile": ":workspace",
    "review_permission_profile": ":read-only"
  }
}
```

```bash
AUTODEV_EXTERNAL_SANDBOX=1 \
AUTODEV_LIVE_CODEX=1 \
autodev run --task TASK-001
```

该模式仅用于受信任且已自行隔离的环境。它不会因普通 Codex sandbox 失败而被自动选择，授权也不会被缓存。

## 预算与恢复默认值

- 每个 run 最多 30 次迭代或 4 小时。
- 每个 Task 最多 4 次 work attempt、2 次 rework。
- 连续 2 次语义停滞后升级处理。
- Attempt idle timeout 为 600 秒，hard timeout 为 2400 秒。
- 每个 Attempt 默认允许 1 次基础设施重试。

进展指纹基于 Task、阶段、diff、失败检查和 blocking findings。日志变多、总结改写或削弱测试都不算进展。

## V1 迁移

```bash
autodev migrate --check
autodev migrate --apply
autodev migrate --rollback MIGRATION_ID
```

`--check` 只读。apply 在临时目录构造并验证完整 V2 tree，再原子安装并冻结 V1 备份。

checksum manifest 只自动处理未修改的 V1 框架文件。发现用户修改时迁移返回 BLOCKED。任何 V2 run 或有效 revision 推进后都拒绝 rollback。

## 命令与退出码

```text
autodev version
autodev init TARGET --name NAME [--merge]
autodev doctor [--json]
autodev validate [--ready] [--json]
autodev activate
autodev task create|ready|show|reopen|defer|block|unblock
autodev run [--task TASK-ID] [--until complete-or-blocked]
autodev stop
autodev resume [--recover-stale]
autodev status [--json]
autodev logs --run RUN-ID
autodev evidence TASK-ID
autodev checkpoint adopt-existing
autodev complete
autodev migrate --check|--apply|--rollback MIGRATION_ID
```

| 退出码 | 含义 |
| --- | --- |
| `0` | 成功或已验收 |
| `1` | 参数或合同无效 |
| `2` | 未就绪或 PAUSED |
| `3` | BLOCKED |
| `4` | STOPPED |
| `5` | 基础设施失败 |

## 验证与真实 smoke

普通开发验证：

```bash
pytest -q
python3 -m unittest discover -s tests -q
python3 scripts/autodev.py validate --ready
PYTHONPATH=src python3 -m autodev version
python3 -m pip wheel --no-build-isolation --no-deps .
```

真实 smoke 是独立、显式、会消耗 token 的测试：

```bash
PYTHONPATH=src \
  python3 examples/build-low-greeting/run_live_smoke.py
```

最新脱敏结果见 [`smoke-result.json`](examples/build-low-greeting/smoke-result.json)。它证明一个 Builder、零 Reviewer、唯一允许文件变更、验证通过、Task ACCEPTED 和 Project COMPLETE。

失败时脚本保留临时项目并输出日志位置；成功时删除原始 Codex JSONL。仓库不保存模型 transcript 或凭证。

## 详细设计文档

- [Workflow Guide](docs/WORKFLOW.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Requirements](docs/REQUIREMENTS.md)
- [Decision Index](docs/DECISIONS.md)
- [BUILD + LOW smoke](examples/build-low-greeting/README.md)

## 发布边界

当前里程碑的实现与验收已完成，但版本保持 `2.0.0a1`。是否升级版本、发布到 PyPI、承诺兼容性或启用远程副作用，需要单独的发布决策和授权。
