# PRD: MVP 1 Terminal Agent

## Introduction / Overview

本阶段目标是先实现一个仅面向终端任务的最小 computer use agent 闭环。系统接收自然语言任务，由主 agent 输出 `run_command` 动作，执行命令并记录结果，支持最小运行轨迹与结果判断。此阶段不引入 GUI 操作与 examiner 审核，重点是把任务输入、动作执行、状态记录和最小完成流程真正跑通。

## Goals

- 打通 terminal-only 的最小执行闭环
- 形成结构化命令执行结果记录
- 为后续 LangGraph 主循环和 run store 打基础
- 明确任务完成、失败和中断等基础状态

## User Stories

### US-001: 定义 terminal-only runtime state 基础模型
**Description:** As a developer, I want a minimal runtime state for terminal tasks so that the agent loop can track task, steps, and command results.

**Acceptance Criteria:**
- [ ] 定义适用于 MVP 1 的最小 runtime state 字段集
- [ ] 至少包含 task、run、last_action、metrics、errors 等基础字段
- [ ] 字段命名与 `doc/project/runtime-design.md` 保持一致或可平滑扩展
- [ ] 文档或代码中明确 success / failed / aborted 基础终止状态
- [ ] Tests/typecheck/lint passes

### US-002: 实现 `run_command` 工具最小版本
**Description:** As a developer, I want a structured `run_command` tool so that the main agent can execute shell commands and inspect results.

**Acceptance Criteria:**
- [ ] `run_command` 支持传入命令字符串执行
- [ ] 返回结构至少包含 command、stdout、stderr、exit_code、success
- [ ] 支持基本超时控制或至少为超时扩展预留接口
- [ ] 高风险命令暂不做复杂策略，但保留后续拦截扩展点
- [ ] Tests/typecheck/lint passes

### US-003: 记录命令输出与最小 trace
**Description:** As a developer, I want command results persisted to a run directory so that execution can be audited and debugged.

**Acceptance Criteria:**
- [ ] 每次 run 生成独立运行目录
- [ ] 命令输出落盘到结构化日志文件
- [ ] 至少有一个最小 trace 文件记录 step、action、result
- [ ] 能从落盘结果回溯执行过哪些命令及结果如何
- [ ] Tests/typecheck/lint passes

### US-004: 打通 terminal 主循环最小闭环
**Description:** As a user, I want the system to receive a simple terminal task and complete it through the main agent loop so that MVP 1 can be demonstrated end-to-end.

**Acceptance Criteria:**
- [ ] 系统可接收自然语言终端任务并初始化 run
- [ ] 主 agent 能输出一次或多次 `run_command` 动作完成任务
- [ ] 当任务完成时系统能正常终止，不引入 GUI 或 examiner
- [ ] 至少准备 1 个可重复演示的终端任务样例
- [ ] Tests/typecheck/lint passes

## Functional Requirements

- FR-1: 系统必须支持 terminal-only 任务初始化
- FR-2: 系统必须支持结构化 `run_command` 执行
- FR-3: 系统必须记录最小 trace 与命令日志
- FR-4: 系统必须支持 terminal-only 成功、失败和中断状态

## Non-Goals

- 不引入 GUI 截图、点击、输入或拖拽能力
- 不引入 `locate_element`
- 不引入 examiner 审核闭环
- 不追求复杂任务规划或多分支并发执行

## Design Considerations

- 字段和接口应尽量与后续 runtime 总设计兼容
- 尽量避免为 terminal-only 写一套后续无法复用的临时结构

## Technical Considerations

- 后续主循环计划使用 LangGraph，因此状态结构要便于迁移
- 命令工具接口要保留后续风险校验与日志增强空间

## Success Metrics

- 至少 1 个终端任务能稳定跑通
- 运行结果可落盘且可复查
- 为 MVP 2 扩展 GUI 分支时无需推翻 MVP 1 的核心状态结构

## Open Questions

- 终端命令执行层最终统一用什么封装对接 Windows PowerShell
- MVP 1 阶段是否同步提供 2-3 个样例任务而不是仅 1 个
