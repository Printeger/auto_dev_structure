# Auto Dev

# Q1: 使用agent进行项目开发，有什么好的方案？怎样能够实现高自动化，不跑偏，高效率？现在我使用双agent方案，还需要我手动来回下达命令同时上下文长度限制导致有跑偏情况。

## 结论

你现在的“双 Agent 两个长期窗口”思路方向没错，但实现方式应该升级。

**我最推荐的架构不是两个长期 Agent 对话，而是：**

```Plain Text
你
                         │
                只处理真正的 BLOCKED
                         │
                         ▼
              ┌─────────────────────┐
              │ Persistent Commander │
              │ Sol / High           │
              │ 项目状态、方向、验收   │
              └──────────┬──────────┘
                         │
              读取结构化项目状态
                         │
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
     Explorer         Builder        Reviewer
     临时创建          临时创建        临时创建
     Read-only        Write          Read-only
     Medium           High           High
          │              │              │
          └──────────────┼──────────────┘
                         ▼
              ┌─────────────────────┐
              │   STATE MACHINE     │
              │ PLAN→BUILD→TEST     │
              │ →REVIEW→PASS/REWORK │
              └──────────┬──────────┘
                         │
                  PASS → 自动下一项
                  FAIL → 自动重做
                  BLOCKED → 找你
```

核心原则只有一句：

> **聊天上下文不能作为项目记忆；Git \+ 状态文件 \+ acceptance criteria 才是项目记忆。**
> 
> 

你现在“上下文越来越长→压缩→忘记早期约束→跑偏”，本质上就是因为两个 Agent 都把 conversation 当成数据库。

官方 Codex 文档也直接指出长期主线程会出现 context pollution / context rot，并建议把探索、测试、日志分析等噪声工作交给 subagents，只将浓缩结果返回主线程。

---

# 一、三个方案里，我建议你选第二个

你现在**没必要直接造 LangGraph/AutoGen/CrewAI 那种完整 orchestration framework**。

当前 Codex 已经原生提供：

- subagents； 

- 自定义 worker/reviewer/explorer； 

- 不同 Agent 使用不同 model/reasoning； 

- `AGENTS.md`； 

- `SubagentStop` / `Stop` hooks； 

- approval auto\-review； 

- non\-interactive `codex exec`； 

- `codex review`； 

- Git worktree 并行开发。 

当前版本 Codex 的 subagent workflow 默认已经启用，而且 custom agents 可以直接放在 `.codex/agents/`。

---

# 二、最重要的改变：Builder 不要长期存在

你现在大概是：

```Plain Text
Commander context
█████████████████████████████████████

Builder context
█████████████████████████████████████
```

两边都越来越肥。

这是最容易跑偏的模式。

应该变成：

```Plain Text
Commander
████████████
长期存在，但只保存项目级信息

Task 001 Builder
████
完成 → 销毁

Task 002 Builder
████
完成 → 销毁

Task 003 Builder
████
完成 → 销毁
```

例如 Commander 下达：

```Plain Text
TASK-037

Implement IMU timestamp synchronization validation.

Read:
- TASKS/TASK-037.md
- src/time_sync/
- tests/time_sync/

Do not modify:
- estimator/
- calibration/
- dataset format

Acceptance criteria:
AC1 ...
AC2 ...
AC3 ...

Return:
STATUS
FILES_CHANGED
TEST_RESULTS
RISKS
```

然后 spawn 一个**全新的 Builder**。

这个 Builder 不需要知道你半个月前和另一个 Builder 讨论了什么。

它只需要知道：

```Plain Text
当前任务是什么？
项目硬约束是什么？
哪些文件能改？
怎样算完成？
```

所以它天然不容易跑偏。

---

# 三、你的项目需要的不是一个大 DEV\_LOG，而是 5 层状态

我建议项目根目录变成：

```Plain Text
project/
│
├── AGENTS.md
│
├── PROJECT.md
│
├── .agent/
│   ├── STATE.json
│   ├── ROADMAP.md
│   ├── DECISIONS.md
│   ├── DEV_LOG.md
│   └── tasks/
│       ├── TASK-001.md
│       ├── TASK-002.md
│       └── TASK-003.md
│
├── .codex/
│   ├── config.toml
│   └── agents/
│       ├── builder.toml
│       ├── reviewer.toml
│       └── explorer.toml
│
└── src/
```

它们不是重复文件，而是不同层级。

### `AGENTS.md` —— 宪法

只写永远不能违反的规则：

```Plain Text
Architecture authority
Coding rules
Safety boundaries
Testing policy
Agent delegation policy
Human escalation policy
```

**千万不要往 AGENTS\.md 塞开发历史。**

Codex 会按照目录层级加载 `AGENTS.md` / `AGENTS.override.md`；默认合并 instruction 大小还有 32 KiB 限制，所以应该保持精炼，需要局部规则就在子目录放 nested `AGENTS.md`。

---

# 四、`STATE.json` 才是解决上下文问题的核心

例如：

```Plain Text
{
  "project_status": "IN_PROGRESS",
  "milestone": "M3",
  "current_task": "TASK-037",
  "phase": "VERIFY",
  "last_good_commit": "91ab231",
  "retry_count": 1,
  "last_result": "TEST_FAILED",
  "blocking_reason": null,
  "next_action": "builder_rework"
}
```

Commander 每次不需要重新理解：

```Plain Text
过去 400 条聊天
+
20000 行 DEV_LOG
+
几十个旧 prompt
```

只需要：

```Plain Text
AGENTS.md
PROJECT.md
STATE.json
ROADMAP.md relevant section
TASK-037.md
git diff
```

上下文直接少一个数量级。

---

# 五、Task 必须变成“合同”，而不是普通 Prompt

这是防跑偏第二重要的东西。

每个 task 建议固定成：

```Plain Text
TASK_ID: TASK-037

OBJECTIVE
实现 XXX。

RATIONALE
为什么需要这一项。

INPUTS
需要读取哪些文件。

IN_SCOPE
允许修改什么。

OUT_OF_SCOPE
明确禁止修改什么。

ACCEPTANCE_CRITERIA
AC1:
AC2:
AC3:

MANDATORY_TESTS
command 1
command 2

DO_NOT
- 不改变 API
- 不修改 dataset
- 不降低测试标准
- 不修改已有 acceptance criteria

OUTPUT_CONTRACT
STATUS:
SUMMARY:
FILES_CHANGED:
TESTS:
RISKS:
BLOCKERS:
```

尤其重要的是：

```Plain Text
OUT_OF_SCOPE
ACCEPTANCE_CRITERIA
DO_NOT
```

很多 Agent 跑偏并不是能力问题。

而是：

> “它发现另一个方案似乎更漂亮，于是顺手重构了。”
> 
> 

Task contract 直接把这种行为堵住。

---

# 六、Commander 不应该写代码

Commander 的工作必须严格限定成：

```Plain Text
OBSERVE
   ↓
PLAN
   ↓
DELEGATE
   ↓
WAIT
   ↓
VERIFY
   ↓
DECIDE
```

Builder：

```Plain Text
READ TASK
   ↓
PLAN IMPLEMENTATION
   ↓
CODE
   ↓
TEST
   ↓
SELF CHECK
   ↓
REPORT
```

Reviewer：

```Plain Text
READ TASK
+
READ DIFF
+
READ TEST RESULT
   ↓
CHECK ACCEPTANCE CRITERIA
   ↓
PASS / FAIL
```

Reviewer最好**不要看到 Builder 的完整推理过程**。

否则容易产生 confirmation bias：

> Builder：“这是正确的，因为……”
> 
> Reviewer：“嗯听起来有道理。”
> 
> 

Reviewer应该独立看到：

```Plain Text
需求
+
代码
+
测试
```

然后自己判断。

Codex 官方现在本身就提供 `worker` 和 `explorer` built\-in agent，并建议 custom agent 要 narrow and opinionated；官方示例甚至专门配置独立 reviewer。

---

# 七、你现在最缺的是 State Machine

不要让 Commander自由发挥：

> “接下来我觉得可以……”
> 
> 

而是强制：

```Plain Text
READY
  │
  ▼
PLAN
  │
  ▼
IMPLEMENT
  │
  ▼
TEST
  │
  ├── FAIL ──────────────┐
  │                       │
  ▼                       │
REVIEW                     │
  │                       │
  ├── FAIL → REWORK ─────┘
  │
  ▼
PASS
  │
  ▼
COMMIT
  │
  ▼
UPDATE STATE
  │
  ▼
NEXT TASK
```

只有：

```Plain Text
REWORK #1
↓
REWORK #2
↓
还是失败
↓
BLOCKED
↓
找你
```

这样 Commander 就不需要你一直输入：

```Plain Text
继续
review
好的继续
让builder修改
review一下
继续下一步
```

---

# 八、现在 Codex 的 Hooks 可以直接解决这个问题

这是当前 Codex 对你最有价值的新功能之一。

现在已经有：

```Plain Text
SubagentStart
SubagentStop
Stop
PreCompact
PostCompact
PreToolUse
PostToolUse
...
```

尤其：

### `SubagentStop`

Builder 说：

```Plain Text
Done.
```

hook 可以判断：

```Plain Text
STATE = TEST_FAILED
```

然后直接告诉它：

```Plain Text
继续修复 failing tests。
```

不需要你。

### `Stop`

Commander说：

```Plain Text
TASK-037 complete.
```

Stop hook 检查：

```Plain Text
project_status != BLOCKED
project_status != COMPLETE
```

就自动返回：

```Plain Text
{
  "decision": "block",
  "reason": "Continue the autonomous development loop. Read STATE.json and execute next_action."
}
```

这里 Codex 的 `"decision": "block"` 在 `Stop` hook 语义里实际上是**阻止当前 Agent 停止，然后自动生成一个 continuation prompt**。这是官方明确支持的行为。

这就能把：

```Plain Text
Agent结束
↓
等你说“继续”
```

变成：

```Plain Text
Agent结束
↓
Hook检查状态
↓
不是BLOCKED
↓
自动继续
```

这和你的目标几乎完全一致。

---

# 九、但一定要设置死循环保护

否则容易出现：

```Plain Text
test fail
↓
修
↓
fail
↓
修
↓
fail
↓
修
↓
……
```

建议：

```Plain Text
retry_count <= 2
```

以及：

```Plain Text
task_agent_runs <= 4
task_wallclock / token budget
```

超过以后：

```Plain Text
STATE = BLOCKED
```

让 Commander 汇报：

```Plain Text
BLOCKED

TASK:
TASK-037

FAILURE:
...

ATTEMPTS:
1. ...
2. ...

EVIDENCE:
...

ROOT CAUSE HYPOTHESES:
A ...
B ...

DECISION REQUIRED:
...
```

然后才找你。

这是很重要的：**高度自动化不等于无限自主。**

---

# 十、真正的 Human\-on\-BLOCKED

我建议只有以下情况允许叫你：

7338. 两种架构都合理，而且项目文档无法判断； 

7339. 需要改变明确的 research/product requirement； 

7340. 需要删除数据、force push、修改远程资源等高风险操作； 

7341. 缺少账号、API key、硬件或外部数据； 

7342. requirement 自相矛盾； 

7343. 同一 acceptance criterion 连续修复两次仍失败； 

7344. 必须降低测试标准才能继续。 

除此之外：

```Plain Text
不要找你。
```

---

# 十一、权限确认也不应该一直打断你

Codex 当前还有 `auto-review`：

```Plain Text
approval_policy = "on-request"
approvals_reviewer = "auto_review"
```

跨 sandbox 边界时，approval 可以先交给一个独立 reviewer，而不是立即停下来找人。

所以整个系统可以变成：

```Plain Text
普通文件修改
→ 自动

普通测试
→ 自动

安全 command
→ 自动

稍敏感 command
→ auto-review

危险 / 不可逆
→ 你
```

这比 `--yolo` 合理得多。

---

# 十二、并行的正确姿势

不要：

```Plain Text
Builder A ─┐
Builder B ─┼→ 同时修改 src/
Builder C ─┘
```

很容易乱。

官方也明确提醒 parallel subagents 更适合 read\-heavy work，write\-heavy parallelism 会增加冲突和 coordination overhead。

我建议：

```Plain Text
Commander
                     │
          ┌──────────┼──────────┐
          ▼          ▼          ▼
      Explorer    Test Agent   Log Agent
          │          │          │
          └──────────┴──────────┘
                     │
              汇总给 Commander
                     │
                     ▼
               一个 Builder
                     │
                     ▼
                 Reviewer
```

也就是说：

> **读并行，写串行。**
> 
> 

这是效率和可靠性最好的平衡。

如果两个 feature 真正互不相关，可以：

```Plain Text
Builder A → Worktree A
Builder B → Worktree B
```

Git worktree 正是用于让多个独立工作副本并行而不互相干扰。

---

# 十三、我建议你的模型配置

结合这种架构，我会这样分：

这其实比：

```Plain Text
Commander XHigh
Builder High
两个都永远跑
```

更省。

因为真正减少 token 的关键不是：

```Plain Text
High → Medium
```

而是：

```Plain Text
400k 长上下文
        ↓
10k task-scoped context
```

官方目前也建议 demanding agent 用 GPT\-5\.6，`medium` 作为多数 agent 的平衡档，而复杂逻辑、edge cases、review 使用 `high`。

---

# 十四、最终我建议你建设成这个系统

```Plain Text
╔══════════════════════════════════════╗
║              HUMAN                   ║
║         only BLOCKED / DONE          ║
╚══════════════════╤═══════════════════╝
                   │
                   ▼
╔══════════════════════════════════════╗
║ COMMANDER — Persistent               ║
║ Sol High                             ║
║                                      ║
║ Reads:                               ║
║ AGENTS.md                            ║
║ PROJECT.md                           ║
║ STATE.json                           ║
║ ROADMAP.md                           ║
║ current TASK                         ║
╚══════════════════╤═══════════════════╝
                   │
       ┌───────────┼───────────┐
       │           │           │
       ▼           ▼           ▼
   Explorer     Builder     Reviewer
   ephemeral    ephemeral   ephemeral
   read-only    write       read-only
       │           │           │
       └───────────┼───────────┘
                   │
                   ▼
            MACHINE GATES
        ┌──────────────────┐
        │ lint             │
        │ unit tests       │
        │ integration      │
        │ acceptance       │
        │ codex review     │
        └────────┬─────────┘
                 │
           PASS  │ FAIL
             ┌───┴────┐
             ▼        ▼
          NEXT      REWORK
                    ≤ 2
                      │
                      ▼
                   BLOCKED
```

而且 `codex review` 现在本身就是 stable 的非交互 review command，可以进一步作为 Reviewer gate 使用。

---

## 你当前最值得做的升级顺序

**第一阶段不用写任何复杂 orchestrator。**

先把你的现有双 Agent 改成：

```Plain Text
Persistent Commander
+
ephemeral Builder
+
ephemeral Reviewer
+
STATE.json
+
TASK contract
```

接着加入：

```Plain Text
SubagentStop Hook
Stop Hook
Auto-review
```

这样大概就能消灭你现在 **80–90% 的人工“继续 / review / 修改 / 下一步”操作**。

等这一层跑稳定之后，如果你还想做到：

```Plain Text
晚上启动
↓
自动跑几个小时
↓
早上只看到

MILESTONE COMPLETE
或者
BLOCKED: xxx
```

再加一个非常薄的 Python orchestrator，通过 `codex exec --json` 读取机器可读事件即可；Codex 的 non\-interactive mode 本身已经支持 JSONL event stream，因此完全没必要自己重写 agent runtime。

**我认为这才是你这套开发流程应该最终演化的方向：不是“双 Agent”，而是一个小型的 autonomous software engineering control loop。**



# Q2: 我想要的是一个通用化的开发方式，这种方式下，怎样交代给commander项目背景和目标？需要告诉commander的都是什么？

## 结论

如果你要的是一种**通用化、可迁移到任何软件/科研项目的 Agent 开发方式**，不要每次给 Commander 一大段聊天式背景。

应该给 Commander 一份固定结构的 **Project Contract（项目契约）**。

Commander 真正需要长期知道的只有六类信息：

155. **为什么做**：项目背景和最终目标 

156. **什么算成功**：可验证的验收标准 

157. **不能做什么**：边界、禁止事项、不可破坏内容 

158. **项目现在是什么样**：架构、目录、技术栈、入口 

159. **目前做到哪里**：当前 milestone / 已完成 / 当前问题 

160. **Commander 怎么工作**：自主权限、Worker 使用规则、什么时候才能找你 

其他东西，例如过去几十轮讨论、详细开发日志、某次失败输出，都不应该长期塞进 Commander prompt。

---

# 一、我建议所有项目统一成 4 层信息

以后不管你开发：

- SLAM 

- RL 

- ROS 

- Web 

- Python package 

- CUDA 

- 数据分析 pipeline 

- 论文代码 

- UAV 系统 

都使用同一套结构：

```Plain Text
project/
│
├── AGENTS.md              # Agent 怎么工作
├── PROJECT.md             # 这个项目是什么
│
├── docs/
│   ├── ARCHITECTURE.md    # 系统怎么组成
│   ├── REQUIREMENTS.md    # 最终要求
│   └── DECISIONS.md       # 已经做出的重要决定
│
├── .agent/
│   ├── STATE.yaml         # 现在做到哪里
│   ├── ROADMAP.md         # 后续阶段
│   └── tasks/
│       └── TASK-xxx.md
│
└── src/
```

这几份文件其实对应四种不同时间尺度：

```Plain Text
长期不变
PROJECT.md
REQUIREMENTS.md
AGENTS.md

      ↓

偶尔变化
ARCHITECTURE.md
DECISIONS.md
ROADMAP.md

      ↓

经常变化
STATE.yaml

      ↓

一次性
TASK-xxx.md
```

这套分层很重要。

---

# 二、Commander 第一件需要知道的：PROJECT\.md

这是项目的**身份证**。

不要写得太长。

一个正常的软件项目，我建议控制在 **1–3 页**。

固定回答下面这些问题。

---

## Project Mission

Commander 首先必须知道：

> 我们究竟在造什么？
> 
> 

例如：

```Plain Text
# Mission

Build a real-time LiDAR–IMU–UWB localization system for UAVs
operating in GNSS-denied environments.

The system shall provide:
- continuous 6-DoF localization;
- UWB-assisted drift correction;
- NLOS robustness;
- real-time operation onboard an RK3588-class computer.
```

而不是：

> 我正在研究 UWB，我们之前讨论过 GLIO，然后导师说……
> 
> 

前者是 machine\-readable 的项目目标。

---

# 三、必须告诉它“为什么做”

这部分很多人会漏掉。

Commander 不只是需要知道：

> 做什么。
> 
> 

还需要知道：

> **为什么这样做。**
> 
> 

否则遇到 trade\-off 时它没办法判断。

例如：

```Plain Text
## Motivation

This system is intended for UAV inspection in GNSS-denied industrial
environments.

Therefore the priority order is:

1. localization robustness
2. safety
3. real-time performance
4. accuracy
5. implementation elegance
```

这一段非常有用。

因为以后 Builder 可能面对：

```Plain Text
方案 A：
精度 +5%
CPU +200%

方案 B：
精度稍低
实时性稳定
```

Commander 自己就知道选 B。

---

# 四、告诉 Commander 最终 Deliverables

这是：

> 最终必须交出什么东西。
> 
> 

例如：

```Plain Text
## Deliverables

D1. Working localization pipeline
D2. Reproducible configuration
D3. Automated unit/integration tests
D4. Benchmark scripts
D5. Documentation
D6. Docker environment
D7. Example dataset execution
```

否则 Agent 很容易出现：

> “代码写完了，所以项目完成了。”
> 
> 

但实际上：

```Plain Text
README 没有
test 没有
benchmark 没有
配置不可复现
```

---

# 五、最重要：Success Criteria

这其实比“项目目标”还重要。

你必须明确告诉 Commander：

> **什么时候可以宣布 DONE。**
> 
> 

比如：

```Plain Text
## Success Criteria

The project is complete only when:

SC1. All unit tests pass.
SC2. Integration test completes without crash.
SC3. ATE on dataset X < 0.20 m.
SC4. Runtime >= 20 Hz.
SC5. No ground truth is used by the estimator.
SC6. Configuration is reproducible from a clean environment.
SC7. Documentation describes installation and execution.
```

这就是整个 autonomous loop 的终点条件。

没有这个东西，Agent 最容易无限漂移：

```Plain Text
再优化一下
↓
再重构一下
↓
再增加一个 feature
↓
再研究一种方法
```

---

# 六、告诉它什么是“硬约束”

单独建立：

```Plain Text
## Non-Negotiable Constraints
```

例如：

```Plain Text
- Do not change public APIs without approval.
- Do not use ground truth in estimation.
- Do not modify raw datasets.
- Do not weaken existing tests.
- Do not remove previously validated functionality.
- Do not replace the localization architecture unless explicitly approved.
- Do not silently change evaluation metrics.
```

这部分对于科研代码尤其重要。

Agent 特别喜欢为了让 test PASS：

```Plain Text
修改 test
降低 threshold
删除 edge case
改 dataset
```

因此应该明确：

> **不能通过修改判卷标准来让自己及格。**
> 
> 

---

# 七、再告诉 Commander 优先级

建议明确写：

```Plain Text
## Priority Order

When objectives conflict, use this order:

1. correctness
2. explicit requirements
3. safety / data integrity
4. reproducibility
5. backward compatibility
6. performance
7. code simplicity
8. cosmetic improvements
```

不同项目可以换。

例如研究 prototype 可以：

```Plain Text
correctness
↓
scientific validity
↓
reproducibility
↓
performance
↓
maintainability
```

产品则可能是：

```Plain Text
reliability
↓
backward compatibility
↓
security
↓
performance
```

---

# 八、第二份核心文件：REQUIREMENTS\.md

`PROJECT.md` 告诉它：

> 我们为什么做、最终要什么。
> 
> 

`REQUIREMENTS.md` 告诉它：

> **系统必须具体表现成什么样。**
> 
> 

例如：

```Plain Text
# Functional Requirements

FR-01
The system shall ingest LiDAR data at 10 Hz.

FR-02
The system shall ingest IMU data at >= 100 Hz.

FR-03
The estimator shall support UWB range measurements asynchronously.

FR-04
The system shall continue operating when UWB measurements are unavailable.


# Performance Requirements

PR-01
Mean processing rate >= 20 Hz.

PR-02
ATE < 0.20 m on benchmark dataset X.


# Compatibility Requirements

CR-01
Ubuntu 22.04.

CR-02
ROS2 Humble.
```

最好给每个 requirement ID。

以后 Task 可以直接说：

```Plain Text
TASK-017 implements FR-03 and contributes to PR-02.
```

这样 Commander 很难跑偏。

---

# 九、第三份：ARCHITECTURE\.md

这里不用写历史。

只告诉 Commander：

> **当前系统实际上是怎么工作的。**
> 
> 

例如：

```Plain Text
Sensor input
    │
    ├── LiDAR
    ├── IMU
    └── UWB
    │
    ▼
Preprocessing
    │
    ▼
Time synchronization
    │
    ▼
Factor construction
    │
    ▼
Factor graph optimizer
    │
    ▼
Pose output
```

然后告诉它：

```Plain Text
src/
├── imu/
├── lidar/
├── uwb/
├── factors/
├── optimizer/
└── evaluation/
```

每个 module 一句话职责即可。

不要把整个代码库解释一遍。

---

# 十、第四份很重要：DECISIONS\.md

这是解决“上下文压缩以后又重新讨论老问题”的关键。

例如你已经决定：

```Plain Text
ADR-004

Decision:
Use constant UWB clock offset rather than drift state.

Reason:
Experimental duration is short and measured drift is negligible.

Rejected alternatives:
- clock drift state
- online affine clock model

Status:
FINAL

Revisit only if:
measured timing residual shows systematic drift.
```

以后 Commander 不允许再次突然说：

> “我建议加 clock drift state。”
> 
> 

除非触发：

```Plain Text
Revisit condition
```

---

# 十一、DECISIONS 比 DEV\_LOG 重要得多

很多人只记录：

```Plain Text
2026-08-21
改了A

2026-08-22
测试B失败

2026-08-23
修复C
```

半年以后没人知道：

> 为什么当时选择 A 而不是 B？
> 
> 

因此：

```Plain Text
DEV_LOG = 发生了什么

DECISIONS = 为什么最终这么设计
```

对于 Agent 防跑偏，第二个更重要。

---

# 十二、STATE\.yaml：Commander 每轮真正应该先读的文件

这份文件一定非常短。

例如：

```Plain Text
project_status: in_progress

current_milestone:
  id: M3
  name: UWB integration

current_task:
  id: TASK-037
  status: review

last_completed_task: TASK-036

last_good_commit: 9f14a31

tests:
  unit: pass
  integration: fail

current_blockers: []

next_action:
  type: review
  task: TASK-037

retry_count: 0
```

Commander 启动以后：

```Plain Text
先读 STATE
```

而不是：

```Plain Text
先读整个 DEV_LOG
```

这能大幅降低 context 消耗。

---

# 十三、ROADMAP 只保存 milestone

比如：

```Plain Text
M1 Environment
[x]

M2 Core LiDAR–IMU estimator
[x]

M3 UWB integration
[IN PROGRESS]

M4 NLOS robustness
[ ]

M5 Benchmark
[ ]

M6 Documentation and release
[ ]
```

Commander 可以根据：

```Plain Text
STATE
+
ROADMAP
```

判断下一步。

不需要你告诉它：

> “好，现在做下一步。”
> 
> 

---

# 十四、AGENTS\.md 不是项目介绍，而是 Commander 的操作规程

这是非常容易混淆的地方。

`PROJECT.md`：

> 项目是什么。
> 
> 

`AGENTS.md`：

> **你应该怎样工作。**
> 
> 

例如：

```Plain Text
# Commander Operating Policy

You are the Commander.

Your responsibility is to drive the project toward the requirements
defined in PROJECT.md and REQUIREMENTS.md.

You own:
- task decomposition
- architecture consistency
- sequencing
- acceptance criteria
- code review
- risk management

You do not perform routine implementation.

Implementation must be delegated to a Builder.

Before every task:
1. read STATE.yaml
2. identify current milestone
3. read relevant requirements
4. inspect relevant architecture/decisions
5. construct a bounded task contract

After every Builder task:
1. inspect diff
2. inspect tests
3. validate acceptance criteria
4. decide PASS / REWORK / BLOCKED

Never:
- redefine project objectives;
- silently modify requirements;
- weaken tests;
- reopen FINAL decisions without new evidence;
- expand task scope unnecessarily.

Human escalation is allowed only for BLOCKED conditions.
```

---

# 十五、你第一次怎么把一个新项目交给 Commander？

以后我建议固定使用一个 **Bootstrap Prompt**。

实际上不需要写很多。

类似：

```Plain Text
You are the Commander for this repository.

Treat the repository documentation as the authoritative project memory.

Read, in order:

1. AGENTS.md
2. PROJECT.md
3. docs/REQUIREMENTS.md
4. docs/ARCHITECTURE.md
5. docs/DECISIONS.md
6. .agent/ROADMAP.md
7. .agent/STATE.yaml

Then inspect the repository only as necessary to verify that the
documentation matches the implementation.

Your job is to autonomously drive the project toward its defined success
criteria.

Use temporary Builder/Reviewer agents for implementation and verification.

Do not ask me for routine engineering decisions.

Escalate only when the BLOCKED criteria in AGENTS.md are met.

Start by determining the current project state and the next bounded task.
```

以后新 session，永远就是这一段。

甚至可以不用每次打。

直接写进 Commander configuration。

---

# 十六、如果项目一开始什么都没有怎么办？

这是另一个很常见的场景。

你只要告诉 Commander **8 个东西**。

我建议以后新项目你就回答这个表。

---

## Universal Project Intake

### Project

```Plain Text
项目是什么？
```

一句话。

---

### Problem

```Plain Text
现在解决什么问题？
为什么现有方案不够？
```

---

### Final Goal

```Plain Text
最终系统应该做到什么？
```

---

### Deliverables

```Plain Text
最终必须交付哪些东西？
```

---

### Success Metrics

```Plain Text
怎样客观判断成功？
```

---

### Constraints

```Plain Text
技术栈？
硬件？
数据？
兼容性？
时间？
哪些东西绝对不能改？
```

---

### Existing Assets

```Plain Text
已有代码？
已有论文？
已有 dataset？
已有 baseline？
已有环境？
```

---

### Priority

```Plain Text
速度 / 稳定 / 精度 / 科学严谨 / 可维护性
哪个优先？
```

就这八项。

Commander 第一项任务应该不是 coding。

而是自动生成：

```Plain Text
PROJECT.md
REQUIREMENTS.md
ARCHITECTURE.md
ROADMAP.md
STATE.yaml
```

然后让你**只审核一次项目定义**。

审核通过：

```Plain Text
PROJECT CONTRACT = FROZEN
```

之后你就退出日常流程。

---

# 十七、哪些东西绝对不要放进 Commander 长期背景

这也很关键。

不要把这些一直喂给它：

### ❌ 整个 DEV\_LOG

太长。

需要时搜索。

---

### ❌ 所有历史聊天

这是最差的 project memory。

---

### ❌ 所有测试输出

保留最新 failure summary 即可。

原始日志放：

```Plain Text
artifacts/logs/
```

---

### ❌ 所有代码内容

需要时 Agent 自己读。

---

### ❌ 所有旧任务

完成任务归档。

Commander只需要：

```Plain Text
当前 task
+
相关 dependencies
```

---

# 十八、还有一个重要机制：Information Budget

我甚至建议给 Commander 制定一个规则：

> **不要无差别读取整个项目。**
> 
> 

例如：

```Plain Text
Context loading order:

Tier 0 — always
AGENTS.md
PROJECT.md
STATE.yaml

Tier 1 — when relevant
REQUIREMENTS.md relevant IDs
ARCHITECTURE relevant section
DECISIONS relevant ADRs

Tier 2 — task specific
source files
tests
logs

Tier 3 — only when debugging requires
historical logs
archived tasks
git history
```

这样可以防止：

```Plain Text
Commander 一启动
↓
cat 所有文档
↓
读整个 src
↓
读所有 logs
↓
context 40%
```

---

# 十九、最理想的信息流其实是这样

```Plain Text
HUMAN
                       │
             初次定义 PROJECT CONTRACT
                       │
                       ▼
              ┌──────────────────┐
              │    PROJECT.md    │
              │ REQUIREMENTS.md  │
              │   CONSTRAINTS    │
              └────────┬─────────┘
                       │
                    FROZEN
                       │
                       ▼
                 COMMANDER
                    │
              reads STATE
                    │
                    ▼
                 ROADMAP
                    │
                    ▼
                TASK CONTRACT
                    │
                    ▼
                  BUILDER
                    │
                    ▼
                TEST / DIFF
                    │
                    ▼
                 REVIEWER
                    │
             ┌──────┴───────┐
             │              │
            PASS          REWORK
             │              │
             ▼              └────┐
        UPDATE STATE             │
             │                   │
             ▼                   │
         NEXT TASK ◀─────────────┘

                BLOCKED
                   │
                   ▼
                  HUMAN
```

这才是通用模式。

---

# 二十、最终压缩成一句话

你以后交给 Commander 的，不应该是：

> “我这个项目之前干了什么、我们聊了什么、Builder上次干了什么……”
> 
> 

而应该是：

> **Mission \+ Requirements \+ Success Criteria \+ Constraints \+ Architecture \+ Decisions \+ Current State \+ Operating Policy。**
> 
> 

其中：

```Plain Text
PROJECT.md
= 我们为什么做、最终要什么

REQUIREMENTS.md
= 什么叫做正确

ARCHITECTURE.md
= 现在系统怎么组成

DECISIONS.md
= 哪些问题已经定了，不要重新争论

STATE.yaml
= 现在做到哪里

ROADMAP.md
= 接下来还有什么

AGENTS.md
= Commander应该怎样管理项目
```

**这七份东西就是一个通用 Agent 开发系统真正的“外置大脑”。**

一旦建立好，你就可以随时杀掉 Commander、换模型、context 清零、换电脑，再启动一个新的 Commander；它重新读取这些文件后，应该能够恢复到几乎同一个开发状态。

这才是判断你的 Agent 开发流程是否真正“通用化”的标准。

# Q3: 但是如果我自己都不确定项目的每个模块该怎么设计，或者在开发中发现某个模块的设计需要修改，该怎么办？我是该让commander一次性完成整个系统的开发， 还是每次让他做一个模块？commander的上下文虽然增长的慢，但是总会满的，满了该怎么办？

对。你这里要区分两个层面：

> **对你来说：只下达一次“完成整个项目”的总目标。**
> **对 Commander 来说：绝不能把整个项目当成一个巨大任务一次做完，而必须自己拆成 milestone → module/vertical slice → task，逐步闭环。**
> 
> 

这两者不矛盾。

你担心的三个问题——“一开始不知道怎么设计”“开发中要改架构”“Commander 上下文最终会满”——实际上可以用同一套机制解决：**Rolling Architecture \+ Disposable Commander Session**。

---

# 不确定模块怎么设计，这是正常状态

不要要求你在项目开始时把：

```Plain Text
Module A
Module B
Module C
Module D
```

全部设计清楚。

初始架构只需要确定三类东西：

```Plain Text
① 已确定的硬约束
② 模块边界和接口的大致方向
③ 当前未知的问题
```

所以 `ARCHITECTURE.md` 不应该假装所有东西都已经确定，而应该允许三种状态：

```Plain Text
FROZEN
PROVISIONAL
OPEN
```

例如：

```Plain Text
## Localization Backend
Status: FROZEN

Use factor graph optimization.
External interface is PoseEstimator.


## UWB Outlier Handling
Status: PROVISIONAL

Current approach:
GPD-based residual rejection.

May be revised if:
- convergence unstable
- runtime unacceptable


## Loop Closure
Status: OPEN

Not designed yet.

Need investigation before M5.
```

这样 Commander 不会犯两个极端错误：

```Plain Text
极端1：
第一天把所有架构拍死
→ 后面发现不合理也不敢改

极端2：
什么都没定
→ 每个 Agent 都重新发明架构
```

---

# 推荐使用“滚动式架构设计”

我建议采用这个结构：

```Plain Text
整个系统
████████████████████████████

当前 Milestone
████████████████
设计很清楚

下一个 Milestone
████████
只有大致方向

更远的 Milestone
███
只知道目标
```

这类似工程中的 **rolling\-wave planning**。

比如：

```Plain Text
M1 Environment
→ 完全设计

M2 Core estimator
→ 完全设计

M3 UWB integration
→ 大概设计

M4 NLOS
→ 知道目标，暂不确定方法

M5 Loop closure
→ OPEN
```

当做到 M4：

```Plain Text
M4 原来的“大概设计”
       ↓
Explorer 调研
       ↓
Commander 比较方案
       ↓
确定设计
       ↓
ADR
       ↓
Builder 实现
```

然后 M5 才进一步展开。

---

# 所以什么时候需要 Explorer？

这是你目前双 Agent 架构里很值得增加的一种角色。

不是：

```Plain Text
Commander
↓
不知道怎么设计
↓
自己想半天
↓
直接让 Builder 写
```

而是：

```Plain Text
Commander
↓
发现 DESIGN_UNCERTAINTY
↓
spawn Explorer A
spawn Explorer B（必要时）
↓
调查：
- existing code
- possible designs
- dependencies
- performance implications
- literature / documentation
↓
Commander 综合
↓
Design Decision
↓
Builder
```

例如 Commander 发现：

> UWB 时间同步到底应该在 preprocessing 做，还是 estimator state 里做？
> 
> 

先不写代码。

进入：

```Plain Text
DESIGN SPIKE
```

让 Explorer 输出：

```Plain Text
OPTION A
设计：
优点：
缺点：
影响模块：
测试方法：

OPTION B
...

RECOMMENDATION
...
```

然后 Commander 决定。

---

# 你的 Commander 应该有两种工作模式

### Mode A — Development

设计明确：

```Plain Text
DESIGN
→ TASK
→ BUILD
→ TEST
→ REVIEW
```

### Mode B — Architecture

设计不明确：

```Plain Text
UNCERTAINTY
→ INVESTIGATE
→ OPTIONS
→ IMPACT ANALYSIS
→ DECISION
→ ADR
→ DEVELOPMENT
```

非常重要的一条规则：

> **如果设计本身不确定，不允许 Builder 边写边替 Commander 做架构决策。**
> 
> 

否则 Builder 很容易把临时实现变成永久架构。

---

# 开发中发现原设计错了怎么办？

这也完全正常。

不要规定：

> architecture frozen = 永远不能修改。
> 
> 

而应该规定**不同级别的修改权限**。

我建议三档。

例如：

### Case A

原来：

```Plain Text
KD-tree
```

发现性能不好，模块内部改：

```Plain Text
Voxel hash
```

但是输入输出完全没变化。

Commander 自己决定即可。

---

### Case B

原来：

```Plain Text
UWB Module
    ↓ range
Estimator
```

现在发现应该：

```Plain Text
UWB Module
    ↓ range + uncertainty
Estimator
```

接口变了。

Commander需要先：

```Plain Text
1. 找所有依赖
2. impact analysis
3. 修改 ARCHITECTURE
4. 写 ADR
5. 修改 dependent tasks
6. Builder implementation
7. integration regression
```

但仍然**不用找你**。

---

### Case C

原 requirement：

```Plain Text
Ground truth shall never enter estimator.
```

但 Agent发现：

> 如果把 GT 用来 calibration 会容易很多。
> 
> 

这不是 architecture adjustment。

这是违反 requirement。

所以：

```Plain Text
BLOCKED → 找你
```

---

# 6\. `DECISIONS.md` 也不要把决定写成永恒真理

我前面提到 ADR，这里需要稍微完善一下。

推荐：

```Plain Text
ADR-017
Status: ACCEPTED

Decision:
Use asynchronous UWB factor insertion.

Reason:
...

Assumptions:
A1 ...
A2 ...

Consequences:
...

Revisit triggers:
- latency > 50 ms
- packet disorder exceeds X
- estimator instability observed
```

关键就是：

```Plain Text
Revisit triggers
```

以后如果满足：

```Plain Text
latency > 50 ms
```

Commander可以合法地重新打开 ADR\-017。

于是：

```Plain Text
ACCEPTED
↓
trigger violated
↓
UNDER_REVIEW
↓
new analysis
↓
ADR-024 supersedes ADR-017
```

这样项目既不会僵死，也不会反复横跳。

---

# 那究竟应该“整个系统一次做完”还是“一个模块一个模块做”？

答案是：

> **人只给整个系统目标一次；Commander 内部按 milestone 逐步推进。**
> 
> 

所以你启动时可以只说：

```Plain Text
Drive this project autonomously from its current state to the success
criteria defined in PROJECT.md.

Manage architecture, planning, implementation, verification and
integration.

Proceed milestone by milestone.

Resolve routine implementation and architecture decisions autonomously.

Escalate only when the BLOCKED criteria are met.
```

然后你就不管了。

Commander 自己：

```Plain Text
PROJECT
  │
  ▼
M1
  │
  ├─ T1
  ├─ T2
  └─ Integration gate
  │
  ▼
M2
  │
  ├─ Design spike
  ├─ T3
  ├─ T4
  └─ Integration gate
  │
  ▼
M3
...
```

**不是你每做一个 module 再回来给一条 prompt。**

---

# 而且我更推荐“Vertical Slice”，不要纯粹按模块开发

例如你系统有：

```Plain Text
Sensor
Preprocessing
Estimator
Output
Visualization
```

一个很危险的方法是：

```Plain Text
先把 Sensor 100% 写完
↓
Preprocessing 100%
↓
Estimator 100%
↓
半年后第一次 integration
```

Agent特别容易这样干。

更好的办法：

```Plain Text
Vertical Slice 1

Sensor
 ↓
basic preprocessing
 ↓
basic estimator
 ↓
basic output
 ↓
最小系统跑通
```

然后：

```Plain Text
Slice 2
增加 IMU

Slice 3
增加 UWB

Slice 4
增加 NLOS

Slice 5
优化 robustness
```

这样每一个 milestone 都有：

```Plain Text
working system
```

而不是一堆孤立模块。

这对于 autonomous agent development 特别重要，因为**integration test 是防跑偏最有效的客观反馈之一。**

---

# 我建议 Commander 使用“双层计划”

Commander永远维护：

## Level 1：Project Roadmap

比较稳定：

```Plain Text
M1 bootstrap
M2 minimal working system
M3 core functionality
M4 advanced functionality
M5 optimization
M6 validation
M7 release
```

## Level 2：Current Milestone Plan

非常详细：

```Plain Text
M3

TASK-031
TASK-032
TASK-033
TASK-034

Integration gate:
IG-M3
```

但是：

```Plain Text
M4
```

只需要：

```Plain Text
goal
dependencies
known risks
```

不要提前拆 30 个 task。

因为做到 M4 时现实已经改变了。

---

# 现在说最关键的：Commander 上下文总会满，怎么办？

答案可能有点反直觉：

> **不要试图让同一个 Commander session 永生。**
> 
> 

你应该让：

> **Commander 这个“角色”永久存在；Commander 的“conversation/session”是可丢弃的。**
> 
> 

这是整个架构成熟与否的重要分界线。

---

# Logical Commander ≠ Chat Session

应该是：

```Plain Text
Commander #1
context ███████████████
完成 M1
↓
CHECKPOINT
↓
销毁


Commander #2
fresh context ██
读取项目状态
↓
继续 M2
↓
CHECKPOINT


Commander #3
fresh context ██
↓
M3
```

但从项目角度：

```Plain Text
Commander
```

一直没有换。

因为真正的 Commander memory 在：

```Plain Text
PROJECT.md
REQUIREMENTS.md
ARCHITECTURE.md
DECISIONS.md
ROADMAP.md
STATE.yaml
Git
```

而不在 conversation。

---

# 什么时候应该换 Commander Session？

我建议不要等：

```Plain Text
context = 99%
```

才处理。

使用三个 trigger：

### Trigger 1：Milestone 完成

这是最好的 checkpoint。

```Plain Text
M3 PASS
↓
更新文档
↓
commit
↓
fresh Commander
```

---

### Trigger 2：发生重大 architecture change

例如：

```Plain Text
ADR-017 被 supersede
```

这时候也很适合：

```Plain Text
重新整理 state
↓
fresh session
```

防止旧架构残留在 conversation 里继续影响模型。

---

### Trigger 3：上下文明显过大

例如可以把：

```Plain Text
50–70%
```

作为软触发区间，而不是死守一个精确百分比。

让 Commander 做：

```Plain Text
CONTEXT CHECKPOINT
```

---

# Checkpoint 到底做什么？

每次 Commander rotation 前必须完成：

```Plain Text
1. tests 到稳定状态
2. git status 清晰
3. 创建 checkpoint commit
4. STATE.yaml 更新
5. ARCHITECTURE.md 更新
6. DECISIONS.md 更新
7. ROADMAP 更新
8. unresolved blockers 更新
9. 当前 next_action 写清楚
```

然后创建一个非常短的：

```Plain Text
HANDOFF.md
```

例如：

```Plain Text
# Commander Handoff

Project:
Localization stack

Completed milestone:
M3 UWB integration

Last good commit:
af91e72

Current system state:
All M3 integration tests pass.

Important recent decision:
ADR-023 changed UWB uncertainty propagation.

Next milestone:
M4 NLOS robustness

Known risks:
- residual distribution may be dataset dependent

Open design questions:
- robust kernel vs explicit NLOS classifier

Next action:
Run architecture exploration for M4.
```

然后杀掉旧 session。

---

# 新 Commander 启动非常简单

只需要：

```Plain Text
You are the new Commander for this project.

The repository is the authoritative project memory.

Read:
1. AGENTS.md
2. PROJECT.md
3. STATE.yaml
4. HANDOFF.md
5. relevant ROADMAP / ARCHITECTURE / DECISIONS

Verify the repository state and continue autonomously from next_action.

Do not reconstruct project history unless needed.
```

新的 Commander 几分钟后就恢复状态。

---

# 自动 Compaction 要不要用？

**用，但不要把它当作项目记忆。**

OpenAI 当前模型/Responses 体系确实支持 long\-running workflow 的 compaction，而且官方建议是在重大 milestone 或 tool\-heavy phase 后进行 compaction，而不是每轮都压缩；压缩时尤其要保留已完成动作、当前假设、工具结果、未解决 blocker 和下一目标。

所以：

```Plain Text
automatic compaction
= 延长当前 session 寿命

external project state
= 保证项目真正连续
```

两者用途完全不同。

我会把 compaction 看成：

> **RAM 压缩。**
> 
> 

而：

```Plain Text
STATE / ADR / Git / Architecture
```

才是：

> **硬盘。**
> 
> 

不要只相信 RAM。

---

# 一个很重要的改进：每次换 Commander 不要继承完整 DEV\_LOG

否则：

```Plain Text
fresh Commander
↓
读 15000 行 DEV_LOG
↓
又满了
```

错误。

新 Commander只读：

```Plain Text
AGENTS
PROJECT
STATE
HANDOFF
current architecture
relevant decisions
```

如果发生问题：

```Plain Text
需要历史
↓
再检索 DEV_LOG / git history
```

也就是 **pull\-based memory**，不是一次全部灌进去。

---

# 这样你甚至可以利用“换 Commander”防止思维惯性

这是一个额外好处。

长期 Commander 有一个风险：

```Plain Text
它自己提出 architecture A
↓
自己实现 architecture A
↓
后来 A 有问题
↓
因为上下文里充满自己支持 A 的理由
↓
继续给 A 打补丁
```

Fresh Commander：

```Plain Text
只看：
requirements
actual architecture
tests
evidence
```

反而可能更客观。

所以 Commander rotation 不一定是缺点。

**它可以是 deliberate context reset。**

---

# 我建议最终状态机增加两个状态

你之前的：

```Plain Text
PLAN
BUILD
TEST
REVIEW
```

还不够。

完善成：

```Plain Text
┌──────────────┐
                 │   DISCOVER   │
                 └──────┬───────┘
                        │
                        ▼
                 ┌──────────────┐
                 │    DESIGN    │
                 └──────┬───────┘
                        │
                        ▼
                    IMPLEMENT
                        │
                        ▼
                      TEST
                        │
                        ▼
                     REVIEW
                    /      \
                  FAIL      PASS
                   │          │
                 REWORK       ▼
                   │      INTEGRATE
                   │          │
                   └──────────┤
                              ▼
                     ARCHITECTURE CHECK
                         /         \
                     stable       change
                       │            │
                       ▼            ▼
                  CHECKPOINT     REDESIGN
                       │            │
                       ▼            └──→ DESIGN
                  NEXT MILESTONE
```

这个比简单的 coding loop 更适合你所说的：

> “我自己一开始也不知道每个模块怎么设计。”
> 
> 

---

# 给 Commander 一个“架构变化规则”

我建议最终在 `AGENTS.md` 加这一段：

```Plain Text
ARCHITECTURE POLICY

Do not assume that the initial architecture is final.

Use rolling architecture.

For future milestones, preserve only enough design detail to establish:
- system boundaries
- interfaces
- dependencies
- invariants
- known risks

Defer internal implementation decisions until the corresponding
milestone approaches.

When implementation evidence invalidates an architectural assumption:

1. stop implementation of the affected scope;
2. identify the violated assumption;
3. perform impact analysis;
4. investigate viable alternatives;
5. update or supersede the relevant ADR;
6. update architecture and dependent task plans;
7. run regression/integration validation;
8. continue development.

Do not escalate to the human merely because an architecture change
is required.

Escalate only if the change modifies a project-level requirement,
success criterion, non-negotiable constraint, or presents multiple
material product/research choices that cannot be resolved from
existing evidence.
```

这句话会非常重要：

> **Architecture 可以改；Requirements 不能被 Agent 擅自改。**
> 
> 

---

# 最终你真正需要的开发方式

我把它压缩成整个工作流：

```Plain Text
HUMAN
                           │
                     定义目标/约束
                           │
                           ▼
                   PROJECT CONTRACT
                           │
                           ▼
                 COMMANDER SESSION #1
                           │
                     粗规划全系统
                           │
                           ▼
                 CURRENT MILESTONE
                           │
               ┌───────────┴──────────┐
               │                      │
          design clear?              NO
               │                      │
              YES                 Explorer
               │                      │
               │                 Alternatives
               │                      │
               │                  Commander
               │                      │
               └──────────┬───────────┘
                          ▼
                       DESIGN
                          │
                          ▼
                        TASKS
                          │
                          ▼
                       BUILDER
                          │
                          ▼
                        TEST
                          │
                          ▼
                       REVIEWER
                          │
                          ▼
                     INTEGRATION
                          │
                          ▼
                  Architecture check
                     /          \
                  stable        revise
                    │             │
                    │             └──→ DESIGN
                    ▼
               MILESTONE PASS
                    │
                    ▼
            STATE / ADR / Git update
                    │
                    ▼
                  CHECKPOINT
                    │
                    ▼
          Commander context large?
                /          \
              NO            YES
              │              │
          continue       kill session
                              │
                              ▼
                    COMMANDER SESSION #2
                              │
                        read state
                              │
                              ▼
                         continue
```

---

## 最后给你三个最重要的原则

**第一，项目全局由 Commander 负责，但实现永远分阶段。**

不要：

> “做完整个软件。”
> 
> 

作为内部执行单位。

应该：

> “自动把整个软件做到完成，但一次只推进一个明确 milestone。”
> 
> 

---

**第二，架构不应该全部 upfront design。**

冻结：

```Plain Text
目标
requirements
hard constraints
关键接口/invariants
```

滚动设计：

```Plain Text
模块内部
算法
implementation
尚未进入的 milestone
```

---

**第三，永远不要追求“一个 Commander conversation 从项目第一天活到最后一天”。**

真正合理的是：

> **Persistent Commander identity \+ Disposable Commander sessions\.**
> 
> 

当你做到这一点以后，context window 就不再是项目规模的硬上限。

甚至项目开发一年：

```Plain Text
Commander #1
Commander #2
...
Commander #37
```

都没有关系。

只要任何一个新的 Commander 都能够通过：

```Plain Text
PROJECT
REQUIREMENTS
ARCHITECTURE
DECISIONS
STATE
HANDOFF
Git
```

恢复工作状态，你的 autonomous development system 才算真正搭起来了。

# Q4: 还有一个问题是，使用双agent开发的时候发现，commander review的时候太过复杂，导致耗时很高，并且review的时候太过严格，导致一些细枝末节的问题也要不通过，这可能也是builder的权限太低的原因，我觉得开发过程中，完成第一版本的开发才是最重要的， 这样会导致浪费时间和token,该怎么平衡呢？

对，你现在遇到的是典型的 **“把最终验收标准提前到了开发阶段”**。

最核心的调整是：

> **开发阶段追求“可运行、主链路正确、没有结构性错误”；硬化阶段才追求“严格、完整、边界情况全部收紧”。**
> 
> 

Commander 不应该每次 Builder 提交后都像审论文终稿一样 review。否则双 Agent 会陷入：

```Plain Text
Builder 写 2 小时
→ Commander 找 12 个小问题
→ Builder 改
→ Commander 再找 7 个
→ Builder 再改
→ 功能还没做完
```

这是非常低效的。

我建议你把整个开发流程明确拆成三种质量模式：

其中最重要的是：

> **同一个 Reviewer，在不同阶段必须使用不同的 rejection threshold。**
> 
> 

---

## 第一版开发阶段，Commander 只应该卡 5 类问题

我建议把 Commander 的 first\-pass review 缩减成：

```Plain Text
BLOCK ONLY IF:

1. 功能没有实现主要 objective
2. 代码无法运行 / 编译 / 核心测试失败
3. 明确违反 requirement / architecture invariant
4. 引入明显破坏性 regression
5. 存在会严重阻碍后续开发的结构性设计错误
```

除此之外全部：

```Plain Text
PASS_WITH_DEBT
```

而不是：

```Plain Text
FAIL
```

例如这些问题：

```Plain Text
变量名不好
函数可以再拆
日志格式不统一
注释不足
有一点 duplicate code
某个 edge case 暂时没覆盖
test naming 不够漂亮
documentation 不完整
异常信息不够优雅
```

在 first version：

> **都不应该阻止下一步开发。**
> 
> 

记录下来即可。

---

# 关键是增加一个状态：`PASS_WITH_DEBT`

你现在可能只有：

```Plain Text
PASS
FAIL
BLOCKED
```

这很容易造成过度 review。

应该改成：

```Plain Text
PASS
PASS_WITH_DEBT
REWORK
BLOCKED
```

其中：

### PASS

```Plain Text
核心实现正确
没有值得记录的问题
```

### PASS\_WITH\_DEBT

```Plain Text
核心功能成立
可以继续开发

但存在：
- minor issue
- cleanup
- docs
- optimization
- additional tests
```

这些写入：

```Plain Text
TECH_DEBT.md
```

然后直接继续下一任务。

### REWORK

只用于：

```Plain Text
当前 task 的主要 acceptance criterion 没有满足
```

### BLOCKED

才找你。

这个小变化会大幅减少循环。

---

# 不要让 Commander 每次都 Review 全部代码

这也是你耗时高的主要原因。

Commander 不应该：

```Plain Text
Builder 完成 TASK-032

Commander:
重新读 PROJECT
重新读 architecture
重新扫描几十个文件
重新跑所有 test
review 所有代码风格
检查所有 edge cases
```

应该使用：

> **Incremental Review。**
> 
> 

即：

```Plain Text
TASK contract
+
git diff
+
相关测试结果
+
受影响 interface
```

只 review **delta**。

只有满足以下条件才扩大 review scope：

```Plain Text
接口改变
architecture 改变
共享核心模块改变
integration test 失败
milestone completion
```

正常 feature 不要重新审整个项目。

---

# Builder 权限确实应该提高

你判断得对。

如果 Builder 的职责只是：

> “机械执行 Commander 的详细指令。”
> 
> 

那么 Commander 必然承担：

```Plain Text
设计
task decomposition
implementation decisions
代码检查
debug判断
quality检查
```

Commander自然越来越重。

更合理的是：

```Plain Text
Commander:
WHAT + WHY + BOUNDARY + ACCEPTANCE

Builder:
HOW + implementation details + self-review
```

例如 Commander 不应该下达：

```Plain Text
在 foo.py 第37行增加函数
使用 dict
然后调用 bar()
然后写三个 test
```

应该下达：

```Plain Text
Objective:
Support asynchronous UWB measurements.

Constraints:
Do not change estimator public API.
Do not use GT.
Preserve existing LiDAR/IMU behavior.

Acceptance:
Existing tests pass.
New UWB integration test passes.
No crash when UWB is unavailable.
```

然后：

> **具体怎么实现让 Builder 自己决定。**
> 
> 

这样 Builder 才真正承担 engineering work。

---

# Builder 必须自己做第一层 Review

不要：

```Plain Text
Builder
↓
写完
↓
Commander发现低级错误
```

而应该：

```Plain Text
Builder
↓
implementation
↓
self-review
↓
tests
↓
fix obvious problems
↓
Commander
```

Builder completion contract 可以规定：

```Plain Text
Before returning DONE:

- inspect your git diff;
- run relevant tests;
- fix obvious correctness issues;
- check for accidental scope expansion;
- confirm acceptance criteria individually.

Do not report DONE while known fixable defects remain.
```

这样 Commander 就不是 lint machine。

---

# Reviewer最好也不要每个 Task 都上

你之前设计：

```Plain Text
Builder
↓
Reviewer
↓
Commander
```

如果每一个 30 行的小任务都这样，非常浪费。

我建议：

```Plain Text
普通 task

Builder self-review
↓
Commander lightweight gate
↓
继续
```

只有：

```Plain Text
高风险 task
architecture change
核心算法
security
data integrity
milestone integration
release
```

才：

```Plain Text
Builder
↓
Independent Reviewer
↓
Commander
```

也就是说 Reviewer 本身也应该是**按风险调用**的。

---

# 可以给 Task 一个 Risk Level

这个特别适合自动化。

Commander 创建 Task 时自动标：

```Plain Text
RISK = LOW / MEDIUM / HIGH
```

然后：

例如：

```Plain Text
README 修改
LOW

新增普通 parser
MEDIUM

修改 optimizer state representation
HIGH
```

这样不会出现：

> README 改一个参数说明和 factor graph 重构使用同样严格的 review pipeline。
> 
> 

---

# 第一版本应该使用“Definition of Good Enough”

这比 Definition of Done 更适合开发期。

例如：

```Plain Text
FIRST-PASS GOOD ENOUGH

Functional:
core path works.

Correctness:
known normal inputs handled correctly.

Testing:
happy-path + critical regression tests pass.

Architecture:
no obvious dead-end design.

Quality:
readable enough for continued development.

Not required yet:
full edge-case coverage,
performance optimization,
complete documentation,
cosmetic refactoring,
perfect abstraction.
```

这实际上是在告诉 Commander：

> **不要优化一个还没完成的系统。**
> 
> 

---

# Minor 问题怎么办？不要丢，积累成 Debt

建立：

```Plain Text
.agent/DEBT.md
```

例如：

```Plain Text
DEBT-017
Severity: LOW
Module: UWB
Issue:
Duplicated timestamp validation.

Fix before:
M6 Hardening
```

Commander review 时：

```Plain Text
发现 minor issue
↓
记录 debt
↓
PASS_WITH_DEBT
↓
继续
```

等整个系统第一版跑通：

```Plain Text
MVP_COMPLETE
↓
进入 HARDENING
↓
按 severity 清 debt
```

这样不是“不管质量”。

而是：

> **推迟低价值质量工作到正确的时间。**
> 
> 

---

# 更进一步：给 Commander 一个“Review Budget”

这是我非常推荐你加进去的。

例如：

```Plain Text
During development mode:

Review only until sufficient evidence exists to decide
PASS / PASS_WITH_DEBT / REWORK.

Do not continue searching for additional minor defects after
the acceptance criteria have been established as satisfied.

Maximum review priority:
correctness > integration > architectural compatibility > minor quality.
```

最后一句尤其重要：

> **一旦已有足够证据证明 task 可以继续，就停止 review。**
> 
> 

否则大模型天然容易：

```Plain Text
“再看看还有没有问题”
```

然后无限深挖。

---

# Commander 不应该追求“证明代码完美”

Commander真正要回答的问题应该从：

> “这段代码还有没有任何问题？”
> 
> 

改成：

> **“当前证据是否足以允许项目进入下一阶段？”**
> 
> 

这是两个完全不同的 review philosophy。

前者几乎永远可以找到问题。

后者有明确停止条件。

---

# 你的工作流我建议最终变成这样

```Plain Text
COMMANDER
                        │
                  Create task
                        │
                Risk classification
                        │
                        ▼
                     BUILDER
                        │
                 implement + test
                 + self-review
                        │
                        ▼
              acceptance satisfied?
                 /             \
               NO              YES
               │                │
            REWORK              ▼
                        lightweight review
                              │
                    ┌─────────┴──────────┐
                    │                    │
               critical issue?       minor only?
                    │                    │
                   YES                  YES
                    │                    │
                 REWORK          PASS_WITH_DEBT
                                         │
                                         ▼
                                      NEXT TASK
                                         │
                                         ▼
                                FIRST VERSION COMPLETE
                                         │
                                         ▼
                                   HARDENING MODE
                                         │
                              full review / debt cleanup
                                         │
                                         ▼
                                      RELEASE
```

这样第一版开发速度会明显提高。

---

## 我会具体给你的 Commander 定三个 Mode

### `BUILD_MODE`

默认。

目标：

> 最大速度形成 working system。
> 
> 

Commander：

```Plain Text
只检查 major
minor → debt
```

### `INTEGRATION_MODE`

每个 milestone 末尾进入。

目标：

```Plain Text
模块之间真正跑通
```

这时比 BUILD 严格一些。

检查：

```Plain Text
interface
regression
integration
architecture consistency
```

### `HARDENING_MODE`

第一完整版本以后。

这时候才允许 Commander“洁癖”。

包括：

```Plain Text
edge cases
test completeness
refactoring
documentation
performance
error handling
technical debt
code quality
```

---

# 最重要的是 Hardening 不能偷偷提前

你可以直接在 `AGENTS.md` 写：

```Plain Text
DEVELOPMENT PHILOSOPHY

The primary objective during BUILD_MODE is to reach a complete,
working end-to-end implementation quickly.

Do not apply release-level review standards during initial development.

A task must not be rejected solely because of:
- cosmetic code quality issues;
- optional refactoring opportunities;
- incomplete non-critical documentation;
- non-critical edge cases;
- minor duplication;
- naming/style preferences;
- optimization opportunities.

Record such findings as technical debt and continue.

Use REWORK only when:
- an acceptance criterion is not satisfied;
- correctness is materially compromised;
- integration is broken;
- a project invariant is violated;
- the implementation creates a likely architectural dead end.

Once sufficient evidence exists to permit progress, stop reviewing.

Full strict review is reserved for HARDENING_MODE.
```

这基本就是你现在缺的一条“宪法”。

---

# 我对你现在双 Agent 的具体调整

不要：

```Plain Text
Commander = extremely strict reviewer
Builder = low-authority coder
```

改成：

```Plain Text
Commander
= product owner + architect + integration manager

Builder
= senior engineer

Reviewer
= optional QA specialist
```

对应权限：

```Plain Text
Commander:
决定 WHAT / boundaries / acceptance

Builder:
自主决定 HOW

Reviewer:
只在 high-risk / milestone / hardening 出场
```

我认为这样才是效率最高的。

---

## 如果让我给一个默认比例

在**第一版完整系统出来以前**，我会大致追求：

```Plain Text
70%  implement new functionality
20%  integration / debugging
10%  cleanup / review
```

而不是现在这种可能变成：

```Plain Text
30% coding
70% review/rework
```

等第一版 end\-to\-end 跑通以后再反过来增加 review。

所以你现在最需要改变的并不是换更快的模型，而是：

> **把 Commander 从“每个 task 的终审法官”改造成“项目进度和风险管理者”，并引入 BUILD / INTEGRATION / HARDENING 三种质量门槛。**
> 
> 

这会同时降低时间消耗、token 消耗和 Agent 之间无意义的往返，同时仍然保留最终质量。

