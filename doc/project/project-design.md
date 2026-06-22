# Computer Use Agent 项目设计文档

## 1. 文档目的

本文档用于明确本课设项目的目标、范围、系统结构、关键模块、实现路线与测试方案，作为后续实现和文档同步的统一依据。

## 2. 项目概述

### 2.1 项目主题

实现一个面向 Windows 环境的简化版 **computer use agent**。系统接收自然语言任务后，由主 agent 自主决定下一步动作；当主 agent 认为任务已完成时，不直接退出，而是提交给 examiner subagent 依据运行轨迹与截图证据进行验收。若 examiner 认为任务未完成或证据不足，则返回返工建议，主 agent 继续执行，直到任务被接受或流程终止。

### 2.2 设计目标

1. 构建一个结构清晰、可演示的 computer use agent 原型。
2. 支持终端执行与 GUI 操作两类基本能力。
3. 支持截图采集、历史截图回看与轨迹记录，形成可回溯证据链。
4. 使用 examiner 机制实现“执行—验收—返工”的闭环。
5. 以少量固定任务为目标，完成一个可展示 demo，而不是追求通用商业级能力。

### 2.3 项目边界

本项目的边界如下：

- **优先支持 Windows**，不承诺跨平台一致性。
- **优先支持受限任务集**，不追求开放环境下的通用任务求解。
- **先保证流程可跑通**，再逐步提高 GUI 定位与执行稳定性。
- **验收以轨迹和截图为主**，不追求完全自动、无误判的结束判定。

### 2.4 非目标

以下内容不作为本课设的首要目标：

- 不追求覆盖完整 OSWorld 基准。
- 不追求复杂网页自动化、多窗口并发或长程自主规划。
- 不追求高安全隔离、沙箱化和生产级权限控制。
- 不以训练新模型为目标，优先使用现成模型与工具框架。

## 3. 总体设计思路

系统采用“**主执行 agent + 验收 examiner**”的双角色结构：

- **主 agent**：根据用户任务、历史动作、命令输出和截图，决定下一步动作。
- **examiner subagent**：只在主 agent 申请结束时介入，读取当前 run 的轨迹与证据，给出 accept 或 reject。
- **工具层**：向主 agent / examiner 提供终端命令、截图、GUI 操作、历史截图查询、元素定位等能力。
- **轨迹与证据存储层**：记录动作、观察、截图和阶段结论，支持回溯与验收。

核心设计原则：

1. **动作要少而清晰**：每一步尽量只执行一个明确动作。
2. **证据要可追溯**：重要动作前后尽量保留截图和输出。
3. **状态要结构化**：便于主循环和 examiner 读取。
4. **先跑通，再增强**：先完成 terminal-only MVP，再逐步加入 GUI 和定位。

## 4. 系统架构

### 4.1 模块划分

1. **Task Interface**
   - 接收自然语言任务。
   - 初始化 run_id、状态对象与输出目录。

2. **Main Agent**
   - 读取任务目标、历史轨迹、最近观察结果。
   - 决定执行 `run_command`、`take_screenshot`、`click`、`type_text`、`hotkey`、`drag`、`locate_element` 或 `finish_request`。

3. **Examiner Subagent**
   - 在主 agent 发起结束申请后运行。
   - 读取任务说明、完整轨迹、关键命令输出、截图引用及主 agent 的完成说明。
   - 输出 `accept` 或 `reject`，并给出理由与返工建议。

4. **Tool Layer**
   - 终端工具：运行命令、返回 stdout / stderr / exit code。
   - GUI 工具：截图、点击、输入文本、快捷键、拖拽。
   - 定位工具：根据截图与元素描述返回目标 bbox。
   - 证据工具：列出历史截图、查看指定截图元信息。

5. **Run Store**
   - 保存轨迹、截图、命令输出、摘要和阶段状态。
   - 为 examiner 和后续调试提供依据。

### 4.2 主流程

```text
用户任务
  -> 初始化运行状态
  -> 主 agent 决策
      -> 终端动作 / GUI 动作 / 观察动作
      -> 记录结果与证据
      -> 回到主 agent
  -> 主 agent 提交 finish_request
  -> examiner 验收
      -> accept: 结束
      -> reject: 返回建议给主 agent
  -> 若未超出上限，则继续执行
```

### 4.3 状态机建议

适合使用 LangGraph 建模为显式状态图：

```text
START
  -> main_agent_decide
  -> observation_record
  -> action_execute
  -> main_agent_decide
  -> finish_request
  -> examiner_review
      -> accept -> END(success)
      -> reject -> main_agent_decide
      -> abort  -> END(failed/aborted)
```

## 5. Agent 职责设计

### 5.1 主 agent

主 agent 的职责：

- 理解当前任务目标。
- 基于已有证据决定下一步动作。
- 在终端分支与 GUI 分支之间进行选择。
- 必要时主动请求截图，或查看历史截图辅助判断。
- 当认为任务已完成时提交结束申请，而不是直接结束整个流程。

主 agent 每一步只允许选择以下几类动作之一：
> 动作待定，下面是示例
- `run_command`
- `take_screenshot`
- `view_screenshot`
- `click`
- `type_text`
- `hotkey`
- `drag`
- `locate_element`
- `finish_request`

### 5.2 Examiner Subagent

examiner 的职责：

- 不负责具体执行，只负责验收。
- 审核当前证据是否足以支持“任务已经完成”。
- 若证据不足或结果明显不对，给出可操作的返工建议。
- 控制系统是否真正进入终止状态。

examiner 的设计目标不是绝对正确，而是：

1. 减少主 agent 过早结束；
2. 提供结构化返工意见；
3. 让系统具有“自我复查”的展示效果。

## 6. 动作与工具设计

### 6.1 终端工具
> 详细工具设计已在 `doc/project/tooling-design.md` 展开；本节只保留总览。
#### `run_command(command)`

功能：在终端执行命令并返回结构化结果。

建议输出字段：

- `command`
- `exit_code`
- `stdout`
- `stderr`
- `duration_ms`
- `success`

设计要求：

- 支持超时控制。
- 保留完整命令输出到 run 目录。
- 在高风险命令上可额外增加确认或限制。

### 6.2 GUI 基础工具（示例）

#### `take_screenshot()`
- 截取当前屏幕或活动窗口。
- 保存图片到指定目录。
- 返回路径、时间戳、分辨率等元信息。

#### `view_screenshot(screenshot_id | path)`
- 读取指定历史截图供 agent 继续分析。
- 主要用于对比、回看、补证据。

#### `click(x, y, button="left")`
- 在坐标位置执行点击。

#### `type_text(text)`
- 输入文本。

#### `hotkey(keys)`
- 执行组合键，如 `Ctrl+S`、`Alt+Tab`。

#### `drag(x1, y1, x2, y2)`
- 执行拖拽操作。

### 6.3 元素定位工具

#### `locate_element(query)`

输入：
- 当前或指定截图
- 元素描述，如“保存按钮”“搜索框”“地址栏”

输出：
- `bbox = [x1, y1, x2, y2]`
- `confidence`
- `reason` 或简短定位说明

定位实现思路：

1. 主体方案：调用专门的视觉定位模型，根据屏幕截图和文字描述返回坐标框。
2. 备选增强：结合 OCR、模板规则或 Windows UI Automation 做辅助验证。
3. 执行策略：`locate_element` 只负责定位，不直接点击；点击由主 agent 再做决策，便于轨迹清晰。

### 6.4 工具层抽象要求

所有 GUI / terminal 能力都应通过统一接口暴露，底层实现可替换。这样后续可以在不改变 agent 逻辑的前提下，替换：

- 命令执行方案
- 截图方案
- 鼠标键盘控制方案
- 元素定位模型
- OCR / UIA / 规则辅助模块

## 7. 轨迹、截图与证据链设计

### 7.1 运行目录建议

每次任务运行生成独立目录，例如：

```text
runs/<run_id>/
  ├── trace.jsonl
  ├── screenshots/
  ├── command_logs/
  ├── artifacts/
  └── summary.json
```

### 7.2 动作记录格式

建议每一步记录为一条结构化事件，核心字段包括：

- `step_id`
- `timestamp`
- `actor`：`main_agent` / `examiner` / `tool`
- `action_type`
- `action_args`
- `result`
- `related_screenshot`
- `status`
- `note`

### 7.3 截图记录要求

截图不只是调试素材，也属于验收证据。建议记录：

- `screenshot_id`
- 文件路径
- 截取时间
- 屏幕分辨率
- 来源步骤
- 简短描述（如“点击保存前”“examiner 验收时查看”）

### 7.4 证据链目标

证据链需要支持以下问题的回溯：

- 主 agent 为什么执行这个动作？
- 动作执行后的直接结果是什么？
- 是否存在支持“任务完成”的截图或输出？
- examiner 为什么 accept 或 reject？

## 8. 主循环与结束判定

### 8.1 主循环策略

主循环采用“决策—执行—记录—再决策”的单步迭代模式。该模式优点是：

- 轨迹清晰
- 容易插入截图
- 容易调试错误动作
- examiner 更容易回溯

### 8.2 结束判定流程

1. 主 agent 判断任务大概率完成。
2. 主 agent 输出 `finish_request`，并附上简短完成说明。
3. examiner 读取任务与证据并做出判断：
   - `accept`
   - `reject`
   - `abort`（异常、超限或无法继续）
4. 若 `reject`，则返回返工建议，主 agent 继续执行。

### 8.3 Examiner 输出协议

建议 examiner 输出采用固定结构：

- `decision`: `accept` / `reject` / `abort`
- `reason`: 判断理由
- `missing_evidence`: 缺失证据或可疑点
- `suggested_next_steps`: 下一步建议

### 8.4 防失控机制

为避免无限循环，需要设置：

- 最大总步数
- 最大返工轮数
- 单步工具超时
- 最大总运行时长
- 明确的失败终止状态

最终终止状态建议至少包括：

- `success`
- `failed`
- `aborted`

## 9. 技术选型

### 9.1 运行与工程管理

- **uv**：用于 Python 虚拟环境与依赖管理。
- **git**：用于版本管理与阶段迭代。

### 9.2 LLM 交互层

- **LangChain**：用于较底层的模型调用、消息组织与工具调用封装。
- **LangGraph**：用于构建主循环、分支逻辑、状态机与 examiner 验收闭环。

### 9.3 选型理由

1. LangGraph 适合显式表示 agent 状态机。
2. LangChain 可作为模型接入层，而不必承担全部流程控制。
3. 这种组合既保留框架支持，又不会把逻辑完全埋进黑箱 agent abstraction 中。

### 9.4 外部实现借鉴策略

对于 GUI 工具、命令执行方式和提示词设计，可参考成熟、支持 Windows 的 computer use 产品或开源实现，但本项目仍应保持：

- 自己定义统一动作协议；
- 自己维护主循环与 examiner 逻辑；
- 对借鉴内容做适配，而非原样堆叠。

当前外部参考资料统一整理在 `doc/references/`，后续实现时优先从该目录查找可借鉴对象与对应模块。

工具层的详细拆解、必抄/强烈建议工具清单以及每个工具的最佳参考来源，统一记录在 `doc/project/tooling-design.md`。

运行时层的状态对象、agent/examiner 协议、run store、风险控制与失败恢复，统一记录在 `doc/project/runtime-design.md`。

## 10. 分阶段实现路线

### MVP 1：Terminal Agent

目标：只做终端分支，打通最小可运行闭环。

流程：
- 自然语言任务
- 主 agent 输出 `run_command`
- 执行命令
- 记录结果
- 主 agent 申请结束
- 暂时不做 examiner

### MVP 2：加入截图与 GUI 基础动作

新增能力：
>示例动作
- `take_screenshot`
- `click`
- `type_text`
- `hotkey`
- `drag`

目标：主 agent 能在简单 GUI 环境中执行基础交互。

### MVP 3：加入元素定位

新增能力：
- `locate_element("保存按钮") -> bbox`
- `click(center_of_bbox)`

目标：从纯坐标操作升级为“描述驱动”的 GUI 操作。

### MVP 4：加入 examiner 返工循环

新增能力：
- 主 agent finish request
- examiner accept / reject
- reject 后输出返工建议
- 主 agent 根据建议继续执行

目标：形成完整的执行—验收—返工闭环。

### MVP 5：整理为可展示 Demo

目标：
- 固定 5–10 个任务
- 保存完整轨迹和截图
- 形成可复现实验与展示材料

## 11. 测试与评估方案

### 11.1 测试来源

参考 OSWorld 的任务风格，但不直接追求完整复现实验环境。项目将自选 5–10 个适合本地 Windows 环境的固定任务作为 demo 测试集。

### 11.2 测试任务类型建议

- 终端文件操作
- Python 脚本执行与结果检查
- 记事本等简单桌面应用操作
- 基础输入、保存、打开等 GUI 任务
- 少量需要截图或定位验证的任务

### 11.3 评估维度

1. **任务完成率**：任务是否达到预期目标。
2. **轨迹完整性**：是否保留可回溯动作与输出。
3. **证据充分性**：是否有足够截图和输出支持验收结论。
4. **返工有效性**：examiner reject 后是否能推动修正。
5. **演示稳定性**：固定任务在多次运行中是否基本可复现。

### 11.4 验证方式

以人工验证结果为主，examiner 的结论作为系统内自动判定依据。最终展示时应同时提供：

- 任务输入
- 关键动作轨迹
- 截图证据
- 最终结果
- examiner 结论
