# AutoDev 3.0 alpha

AutoDev 是一个面向 Codex 的本地自主开发控制平面。V3 用 Campaign 把“从粗到细”的开发阶段、累计私有 Git 基线、自动 Task 准入、分层质量门和人工升级隐藏在一个可恢复生命周期中。

当前版本是 `3.0.0a1`。它不会自动 commit、push、publish 或 deploy；达到目标时只把 Campaign 私有 checkpoint 的增量写回用户工作区。

## 核心模型

- 模式：`CHANGE | STAGED | CRITICAL`。
- 阶段：`SCAFFOLD → IMPLEMENT → COMPONENT_VERIFY → INTEGRATE → HARDEN → TARGET_REACHED`。
- 目标：`ARCHITECTURE_BASELINE | WORKING_MVP | INTEGRATED_SYSTEM | RELEASE_CANDIDATE`；CHANGE 使用隐式 `CHANGE_COMPLETE`。
- 默认自治：`HUMAN_ON_BLOCKED`。在 Authority Envelope 内自动规划、准入和跨阶段推进。
- Requirement Baseline：`.autodev/campaigns/CAMP-NNN/requirements.json` 是唯一真相；Markdown 报告按需派生。

每个 Campaign 拥有 `refs/autodev/campaigns/CAMP-NNN/current`。Task worktree 从该 ref 创建，接受后用 `write-tree`、`commit-tree` 和 compare-and-swap `update-ref` 累积，不移动用户分支、不运行 hooks。目标达成前，用户工作区保持不变。

## 安装与检查

```bash
python3 -m pip install .
autodev version
AUTODEV_LIVE_CODEX=1 autodev doctor --json
```

需要 Python 3.11+、Git、已登录的 Codex CLI 和至少一个 Git commit。`doctor` 会报告 App Server 人工选项为 `native` 或 `fallback`；fallback 可使用 TTY 菜单或持久化问题队列。

## 启动 Campaign

交互式一体化入口：

```bash
AUTODEV_LIVE_CODEX=1 autodev start \
  --idea "Build a local search service" \
  --mode staged \
  --target integrated-system
```

非交互环境先规划，再用 proposal hash 显式批准：

```bash
AUTODEV_LIVE_CODEX=1 autodev campaign plan \
  --idea-file idea.md --mode staged --target working-mvp

autodev campaign approve CAMP-001 --proposal-hash SHA256
AUTODEV_LIVE_CODEX=1 \
  autodev resume --campaign CAMP-001 --until target-or-blocked
```

如果问题被持久化：

```bash
AUTODEV_LIVE_CODEX=1 autodev campaign answer CAMP-001 \
  --request HUMAN-ID --answer scope="Keep the current scope"
```

恢复始终启动 fresh Planner/Builder/Reviewer/Diagnostic，不恢复旧聊天。

## 目标与写回

```bash
autodev campaign status CAMP-001
autodev campaign retarget CAMP-001 --target release-candidate
autodev campaign materialize CAMP-001
autodev campaign archive CAMP-001
```

`retarget` 只能提高同一 Campaign 的成熟度。新增 Requirement 或扩大产品 scope 应从已有 checkpoint 新建 Campaign。materialize 会验证记录的 source fingerprint 和 `git apply --check`；并发编辑或冲突会安全 BLOCKED。

## 质量路由

| 条件 | 路由 |
| --- | --- |
| 普通 LOW/MEDIUM implementation/test/docs | `NONE`：Builder 自审 + 确定性验证 |
| HIGH、安全、迁移、权限扩大、公共 API、远程副作用 | `IMMEDIATE`：fresh read-only Reviewer |
| architecture、internal interface、shared internal data、integration wiring | `PHASE`：累计阶段 diff Review |
| 同一语义失败指纹连续两次 | `DIAGNOSTIC`：fresh read-only 根因诊断 |

强制质量预算耗尽会 BLOCKED，不会跳过检查。style finding 不进入 canonical evidence。

## 派生报告

```bash
autodev report phase --campaign CAMP-001
autodev report requirements --campaign CAMP-001
autodev report release --campaign CAMP-001
```

Task 不同步 `DEV_LOG.md`、`CHANGES.md`、`TRACEABILITY.md`、`HANDOFF.md` 或 README。`.autodev/runs`、events、journal 和 phase summary 默认被目标项目的 `.autodev/.gitignore` 排除。

## 迁移与兼容

```bash
autodev migrate v2 --check
autodev migrate v2 --apply
autodev migrate v2 --apply --adopt-source FINGERPRINT
autodev migrate v2 --rollback MIGRATION-ID
```

dirty V2 项目必须提供 check 输出的精确 fingerprint 才会被采纳为初始 private checkpoint。首次 V3 状态推进前可以回滚。V1 `migrate --check|--apply|--rollback` 和 `run --until complete-or-blocked` 保留一个 alpha 兼容周期。

## 开发验证

```bash
python -m unittest discover -s tests -v
python -m build
```

普通测试使用 Fake Engine/Planner 和本地假 App Server，不调用模型。真实 smoke 仍需要显式 `AUTODEV_LIVE_CODEX=1`。
