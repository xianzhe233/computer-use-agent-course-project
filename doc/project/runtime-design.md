# Computer Use Agent 运行时专项设计文档

## 1. 文档目的

本文档专门补齐本项目在 **运行时层（runtime layer）** 的设计缺口，重点回答以下问题：

1. LangGraph 状态机中的 `state` 到底长什么样；
2. 主 agent、examiner、工具层三者之间用什么结构化协议交互；
3. 每一步动作、截图、命令输出和验收结论如何落盘，形成可追溯证据链；
4. `locate_element` 这类不稳定能力在运行时如何调度、回退和校验；
5. 高风险动作、工具失败和返工循环在运行时如何受控。

本文件与其他设计文档的关系如下：

- `doc/project/project-design.md`：负责总体目标、系统结构、MVP 路线；
- `doc/project/tooling-design.md`：负责工具清单、最佳参考来源和工具接口建议；
- **本文档**：负责把这些设计真正落到“系统跑起来时内部怎么流转”。

## 2. 运行时设计目标

运行时层必须满足以下目标：

1. **可执行**：设计应足以直接指导后续编码，而不是只停留在概念层；
2. **可追踪**：所有关键状态变化都能在 run 目录中回溯；
3. **可验收**：examiner 读取同一份结构化状态和证据，不依赖隐式上下文；
4. **可失败**：允许工具失败、定位失败、返工失败，并给出清晰终止状态；
5. **可裁剪**：MVP 可以先用最小字段子集运行，后续逐步增强而不推翻结构。

## 3. 运行时总体结构

### 3.1 运行时参与者

运行时共有四类参与者：

1. **User Task Input**
   - 提供自然语言任务；
   - 仅在 run 初始化阶段直接写入状态。

2. **Main Agent**
   - 读取当前任务、历史轨迹、最近观察、可用工具；
   - 每轮只输出一个结构化动作决策。

3. **Tool Runtime**
   - 接收结构化工具调用；
   - 执行动作并返回结构化结果；
   - 负责将产物落盘。

4. **Examiner**
   - 仅在 `finish_request` 阶段介入；
   - 基于运行轨迹、截图与结果摘要做 accept / reject / abort。

### 3.2 单步循环

推荐的单步运行顺序如下：

```text
init_run
  -> main_agent_decide
  -> validate_action
  -> execute_tool
  -> record_artifacts
  -> update_state
  -> next_decision
  -> finish_request
  -> examiner_review
  -> accept / reject / abort
```

设计原则：

- 每轮只允许主 agent 发出一个顶层动作；
- 所有动作必须经过运行时校验；
- 工具不能直接修改核心状态，只能返回结果，由 runtime 统一更新 state；
- examiner 不直接执行工具。

## 4. LangGraph State Schema

### 4.1 顶层状态结构

建议把 LangGraph state 统一定义为下列顶层字段：

```text
RuntimeState
- run
- task
- control
- observation
- memory
- last_action
- pending_finish
- examiner
- metrics
- errors
```

### 4.2 `run` 字段

用于标识当前运行实例的基本元信息。

建议字段：

- `run_id`
- `created_at`
- `root_dir`
- `platform`：固定为 `windows`
- `status`：`running` / `success` / `failed` / `aborted`
- `current_step`
- `current_phase`：`main_loop` / `examiner_review` / `terminated`

### 4.3 `task` 字段

用于保存任务目标与完成定义。

建议字段：

- `user_request`
- `task_type`：`terminal` / `gui` / `hybrid`
- `goal_summary`
- `completion_hints`
- `constraints`
- `sensitive_data_present`

其中：

- `goal_summary` 由初始化阶段生成，用于压缩任务目标；
- `completion_hints` 可来自人工预设任务模板；
- `constraints` 用于描述“不能联网”“不能删除文件”“仅操作记事本”等限制。

### 4.4 `control` 字段

用于控制流程上限、返工轮数、超时与允许工具集。

建议字段：

- `max_steps`
- `max_rework_rounds`
- `max_consecutive_failures`
- `max_runtime_seconds`
- `step_timeout_seconds`
- `allowed_tools`
- `confirmation_required`
- `terminated_reason`

### 4.5 `observation` 字段

用于保存当前主 agent 可以看到的最新环境观察。

建议字段：

- `latest_screenshot_id`
- `latest_screenshot_path`
- `latest_command_result_id`
- `active_window_title`
- `desktop_resolution`
- `last_observation_summary`
- `ui_context`

说明：

- `ui_context` 是可选增强字段，可放最近一次 UIA 查询结果摘要；
- `last_observation_summary` 不是自由发挥的长文本，应控制在简短摘要内。

### 4.6 `memory` 字段

用于保存主 agent 运行时需要复用、但不应反复从全量 trace 回放的信息。

建议字段：

- `completed_subgoals`
- `important_findings`
- `known_failures`
- `last_successful_strategy`
- `element_hints`

这里的 `memory` 只做运行时压缩，不承担长期记忆系统功能。

### 4.7 `last_action` 字段

用于保存上一步动作及其结果摘要。

建议字段：

- `action_id`
- `actor`：`main_agent` / `tool_runtime` / `examiner`
- `action_type`
- `action_args`
- `result_status`
- `result_summary`
- `artifact_refs`

### 4.8 `pending_finish` 字段

当主 agent 认为任务完成时，不直接结束，而是先写入 finish 请求。

建议字段：

- `requested`
- `request_step`
- `completion_claim`
- `supporting_evidence`
- `open_questions`

### 4.9 `examiner` 字段

用于保存 examiner 当前轮的输入与输出。

建议字段：

- `review_count`
- `last_decision`
- `last_reason`
- `missing_evidence`
- `suggested_next_steps`
- `review_trace_refs`

### 4.10 `metrics` 字段

建议字段：

- `step_count`
- `tool_call_count`
- `screenshot_count`
- `command_count`
- `rework_count`
- `consecutive_failures`
- `runtime_seconds`

### 4.11 `errors` 字段

用于保存最近错误与阻塞信息。

建议字段：

- `last_error_code`
- `last_error_message`
- `last_failed_tool`
- `blocked`
- `block_reason`

## 5. 状态更新规则

### 5.1 只允许 runtime 更新核心状态

为了避免工具层直接改乱状态，定义如下约束：

- agent 只输出结构化动作请求；
- tool 只返回结构化结果；
- **只有 runtime reducer / state updater 可以改 `RuntimeState`**。

### 5.2 每轮最少更新项

每次成功执行一轮动作后，至少更新：

- `run.current_step`
- `last_action`
- `metrics.step_count`
- 对应工具相关的 observation / artifact 引用

### 5.3 失败更新规则

工具失败时必须更新：

- `metrics.consecutive_failures += 1`
- `errors.last_error_code`
- `errors.last_error_message`
- `errors.last_failed_tool`
- `last_action.result_status = failed`

若后续某轮成功，则：

- `metrics.consecutive_failures = 0`

## 6. Main Agent 输出协议

### 6.1 输出原则

主 agent 每轮只允许输出以下两类结构之一：

1. **工具动作请求**
2. **结束申请 `finish_request`**

不允许：

- 一轮同时请求多个工具；
- 一轮直接输出自然语言长计划替代动作；
- 跳过工具层直接宣告完成。

### 6.2 工具动作请求 schema

建议统一格式：

```json
{
  "kind": "tool_call",
  "thought_summary": "简短说明为什么做这一步",
  "tool_name": "click",
  "tool_args": {
    "x": 512,
    "y": 384,
    "button": "left"
  },
  "expected_observation": "点击后应出现保存对话框"
}
```

字段说明：

- `kind`：固定为 `tool_call`
- `thought_summary`：保留简短理由，供 trace 与 examiner 理解
- `tool_name`：必须来自允许工具集
- `tool_args`：必须可校验
- `expected_observation`：为下一轮判断是否成功提供依据

### 6.3 `finish_request` schema

建议统一格式：

```json
{
  "kind": "finish_request",
  "completion_claim": "任务已完成，文件已保存到桌面",
  "supporting_evidence": [
    "screenshot:ss_0008",
    "command:cmd_0003"
  ],
  "remaining_uncertainty": "未再次打开文件确认内容"
}
```

说明：

- 主 agent 不能只说“我觉得完成了”；
- 必须至少附上证据引用；
- `remaining_uncertainty` 允许显式承认不足，供 examiner 判断是否 reject。

## 7. Tool Runtime 协议

### 7.1 工具输入 schema

tool runtime 接收的统一对象建议如下：

```json
{
  "action_id": "act_0012",
  "step_id": 12,
  "tool_name": "type_text",
  "tool_args": {
    "text": "hello"
  },
  "requested_by": "main_agent"
}
```

### 7.2 工具输出 schema

建议统一格式：

```json
{
  "action_id": "act_0012",
  "tool_name": "type_text",
  "success": true,
  "duration_ms": 840,
  "result": {
    "typed_length": 5
  },
  "artifacts": [
    "screenshot:ss_0009"
  ],
  "error": null
}
```

失败时：

```json
{
  "action_id": "act_0012",
  "tool_name": "type_text",
  "success": false,
  "duration_ms": 150,
  "result": null,
  "artifacts": [],
  "error": {
    "code": "FOCUS_LOST",
    "message": "No active editable control"
  }
}
```

### 7.3 工具校验阶段

正式执行前必须有一层 runtime 校验：

- 工具是否在 `allowed_tools` 中；
- 参数是否齐全；
- 坐标是否越界；
- 文本是否触发敏感数据策略；
- 当前是否处于禁止执行状态。

校验失败不进入 tool backend，直接返回结构化错误。

## 8. Examiner 输入输出协议

### 8.1 Examiner 输入包

examiner 不读全量隐式上下文，建议 runtime 组装一个明确的 review payload：

```json
{
  "task": {
    "user_request": "...",
    "goal_summary": "..."
  },
  "finish_request": {
    "completion_claim": "...",
    "supporting_evidence": ["..."]
  },
  "trace_summary": {
    "step_count": 12,
    "key_actions": ["click save", "type filename"]
  },
  "artifacts": {
    "latest_screenshots": ["ss_0007", "ss_0008"],
    "latest_command_results": ["cmd_0003"]
  },
  "constraints": ["..."]
}
```

### 8.2 Examiner 输出 schema

建议固定为：

```json
{
  "decision": "accept",
  "reason": "已有截图显示文件保存成功",
  "missing_evidence": [],
  "suggested_next_steps": []
}
```

或：

```json
{
  "decision": "reject",
  "reason": "只有输入文件名的截图，没有保存成功后的界面",
  "missing_evidence": [
    "缺少保存后的结果截图"
  ],
  "suggested_next_steps": [
    "再次截图确认保存结果"
  ]
}
```

### 8.3 Examiner 判定规则

建议 examiner 至少按以下顺序判断：

1. 是否存在与任务目标直接相关的结果证据；
2. 证据是否来自当前 run，而不是主 agent 的主观陈述；
3. 是否仍存在关键未验证项；
4. 若缺证据，返工建议是否足够具体。

## 9. Run Store 与证据链数据设计

### 9.1 运行目录

建议目录结构细化为：

```text
runs/<run_id>/
  ├── trace.jsonl
  ├── state_snapshots/
  ├── screenshots/
  ├── command_logs/
  ├── examiner/
  ├── artifacts/
  └── summary.json
```

### 9.2 `trace.jsonl` 事件格式

每行一条事件，建议字段：

- `event_id`
- `step_id`
- `timestamp`
- `actor`
- `phase`
- `event_type`
- `payload`
- `status`
- `artifact_refs`

`event_type` 建议枚举：

- `agent_decision`
- `tool_validation`
- `tool_execution`
- `state_update`
- `finish_request`
- `examiner_review`
- `termination`

### 9.3 `state_snapshots/`

建议只在关键节点保存状态快照，而不是每步都全量落盘。

关键节点包括：

- run 初始化后；
- 每次 `finish_request` 前；
- 每次 examiner review 后；
- 运行终止前。

命名示例：

- `state_step_0000.json`
- `state_step_0012_pre_finish.json`
- `state_step_0012_post_examiner.json`

### 9.4 截图索引记录

建议在 `screenshots/index.jsonl` 里额外保存截图索引：

- `screenshot_id`
- `path`
- `step_id`
- `timestamp`
- `source_action_id`
- `description`
- `resolution`

### 9.5 命令日志索引记录

建议在 `command_logs/index.jsonl` 中记录：

- `command_result_id`
- `step_id`
- `command`
- `exit_code`
- `stdout_path`
- `stderr_path`
- `duration_ms`

### 9.6 Examiner 记录

`examiner/` 目录建议至少保存：

- `review_0001_input.json`
- `review_0001_output.json`
- `review_0002_input.json`
- `review_0002_output.json`

### 9.7 `summary.json`

建议最终汇总字段：

- `run_id`
- `task`
- `final_status`
- `final_reason`
- `step_count`
- `tool_call_count`
- `rework_count`
- `key_artifacts`
- `started_at`
- `ended_at`
- `duration_seconds`

## 10. `locate_element` 运行时策略

### 10.1 设计定位

`locate_element` 不是普通 GUI 动作，而是一个 **感知增强工具**。它的任务不是操作，而是给后续操作提供候选目标。

### 10.2 调用前提

只有在以下场景建议调用：

- 当前截图中存在需要基于描述定位的元素；
- 纯坐标点击不稳定；
- 需要给 examiner 提供“为什么点这里”的证据解释。

### 10.3 运行时调度顺序

建议采用三段式：

1. **视觉初定位**
   - 输入截图与文本描述；
   - 输出候选 bbox 与置信度。

2. **UIA 辅助验证**
   - 若当前界面可读到控件树，则检查候选区域是否存在匹配控件；
   - 生成 `source = vision / uia / hybrid`。

3. **坐标生成与点击解耦**
   - runtime 只把定位结果写入 observation / artifact；
   - 下一轮是否点击，由主 agent 再决定。

### 10.4 定位失败回退策略

若 `locate_element` 失败：

- 第一次失败：建议先 `take_screenshot` 或 `scroll` 再试；
- 第二次失败：尝试 `move_mouse` / `hover` 触发界面变化；
- 连续失败达到阈值：转为人工可解释失败，不再盲点。

### 10.5 返回格式

建议返回：

- `bbox`
- `confidence`
- `source`
- `matched_text`
- `reason`
- `validation_note`

## 11. 风险控制设计

### 11.1 风险分级

建议将运行时动作分成三级：

1. **低风险**
   - 截图、回看截图、滚动、鼠标移动、等待；

2. **中风险**
   - 点击、双击、右键、快捷键、切换窗口、打开应用；

3. **高风险**
   - 输入敏感信息；
   - 执行危险命令；
   - 可能导致文件删除、外发数据、覆盖保存的动作。

### 11.2 高风险命令拦截

建议建立阻断规则，例如：

- `rm`, `del`, `Remove-Item` 等删除命令；
- 覆盖系统目录或非任务目录文件；
- 涉及网络外发、上传、发送消息、提交表单的动作；
- 在 GUI 中输入口令、验证码、密钥等敏感值。

### 11.3 运行时拦截机制

当命中高风险规则时，runtime 不直接执行，而是返回：

```json
{
  "success": false,
  "error": {
    "code": "RISK_BLOCKED",
    "message": "Action blocked by runtime risk policy"
  }
}
```

同时写入：

- `errors.blocked = true`
- `errors.block_reason`
- trace 事件 `tool_validation`

### 11.4 敏感输入策略

对 `type_text` 应预留三种模式：

- `plain`：普通文本直接输入；
- `masked`：在 trace 中脱敏；
- `blocked`：禁止输入并要求确认。

## 12. 失败恢复与终止策略

### 12.1 工具失败恢复

建议恢复逻辑：

- `FOCUS_LOST`：优先 `focus_window` 或 `click` 激活目标区域；
- `ELEMENT_NOT_FOUND`：优先 `take_screenshot` + `locate_element` 或 `scroll`；
- `APP_NOT_FOUND`：尝试 `open_app` 或转 `run_command`；
- `TIMEOUT`：允许一次 `wait` 后重试。

### 12.2 连续失败阈值

建议默认值：

- 连续工具失败 `>= 3`：进入受限模式；
- 连续工具失败 `>= 5`：直接 `abort`。

### 12.3 返工阈值

建议默认值：

- examiner `reject` 次数 `>= 2`：要求主 agent 必须先补证据，不能再次直接 finish；
- examiner `reject` 次数 `>= 4`：直接 `abort`，避免伪循环。

### 12.4 终止状态定义

统一终止状态：

- `success`
- `failed`
- `aborted`

其中：

- `failed`：任务未完成但流程正常终止；
- `aborted`：超时、超限、风险阻断或无法继续。

## 13. 运行时配置项

建议统一配置文件中包含：

- `model_main`
- `model_examiner`
- `max_steps`
- `max_rework_rounds`
- `max_consecutive_failures`
- `max_runtime_seconds`
- `step_timeout_seconds`
- `screenshot_after_gui_action`
- `risk_policy_mode`
- `trace_level`
- `artifact_root`

## 14. 与现有文档的衔接建议

后续实现时建议遵循以下分工：

- 看总体流程：`doc/project/project-design.md`
- 看工具清单：`doc/project/tooling-design.md`
- 看真正的运行时结构和状态字段：**本文档**

若后续开始编码，建议优先把本文档里的以下内容直接转为代码：

1. `RuntimeState` 数据模型；
2. `MainAgentAction` / `FinishRequest` / `ExaminerDecision` schema；
3. `trace.jsonl` 事件模型；
4. `tool_validation` 风险拦截层。

## 15. 当前仍可后续再细化的点

本文档已经把主要运行时缺口补齐，但仍有三项可以在编码前继续精细化：

1. 每个工具的正式错误码枚举；
2. `locate_element` 的具体模型接入方式；
3. demo 任务集与任务模板库。

这三项目前不阻塞启动实现，但会影响中后期稳定性与可测试性。
