# PRD: MVP 4 Examiner 返工闭环

## Introduction / Overview

本阶段把系统从“主 agent 自己决定结束”升级为“主 agent 提交结束申请、examiner 审核、必要时返工”的闭环。核心目标是避免过早结束，并让系统具备基于证据进行自我复查的演示效果。

## Goals

- 引入 `finish_request` 与 examiner 审核协议
- 让 examiner 基于结构化证据做 accept / reject / abort
- 让 reject 后的返工有明确下一步建议
- 对返工次数与失败次数设置运行边界

## User Stories

### US-001: 定义 `finish_request` 与 examiner 决策协议
**Description:** As a developer, I want structured finish and review payloads so that task completion can be validated consistently.

**Acceptance Criteria:**
- [ ] 定义主 agent 的 `finish_request` 结构
- [ ] 定义 examiner 输入包与输出协议
- [ ] examiner 输出至少包含 decision、reason、missing_evidence、suggested_next_steps
- [ ] Tests/typecheck/lint passes

### US-002: 实现主 agent -> examiner 的闭环流转
**Description:** As a developer, I want the runtime to route finish requests through examiner review so that completion is no longer a unilateral main-agent decision.

**Acceptance Criteria:**
- [ ] 主 agent 不能直接结束运行，必须先提交 `finish_request`
- [ ] runtime 能触发 examiner review
- [ ] accept / reject / abort 三种结果都能进入统一状态更新流程
- [ ] Tests/typecheck/lint passes

### US-003: 实现 reject 后返工与循环上限控制
**Description:** As a developer, I want structured rework after reject so that the system can improve incomplete runs without entering infinite loops.

**Acceptance Criteria:**
- [ ] reject 时能把返工建议写回主循环
- [ ] 主 agent 可依据建议继续执行
- [ ] 系统有最大返工轮数或等价上限控制
- [ ] 超限时能进入 failed 或 aborted 终止状态
- [ ] Tests/typecheck/lint passes

### US-004: 用证据驱动方式演示一次“执行—验收—返工/通过”流程
**Description:** As a user, I want to observe at least one end-to-end examiner cycle so that the system demonstrates self-review rather than blind completion.

**Acceptance Criteria:**
- [ ] 至少有 1 个任务样例能触发 examiner 审核
- [ ] 样例中可展示 accept 或 reject -> rework -> accept 的流程之一
- [ ] 审核结论能引用运行证据而不是只依赖主 agent 陈述
- [ ] Tests/typecheck/lint passes

## Functional Requirements

- FR-1: 系统必须支持结构化 `finish_request`
- FR-2: 系统必须支持 examiner 审核决策
- FR-3: 系统必须支持 reject 后继续返工
- FR-4: 系统必须对返工轮数和总运行过程设置边界

## Non-Goals

- 不要求 examiner 判定绝对正确
- 不追求复杂多代理协作或外部人类审批系统
- 不实现生产级安全权限体系

## Design Considerations

- examiner 的价值在于减少过早结束和补充返工建议，不是取代主 agent
- 审核输入必须依赖统一 run store 与证据链

## Technical Considerations

- 需与当前 runtime state、trace 与 artifact 结构保持兼容
- examiner 介入点要清晰，避免与工具层职责混淆

## Success Metrics

- 系统能稳定演示至少一轮 examiner 审核
- reject 后能推动后续修正，而不是立即崩溃或空转
- 终止状态与返工上限都可解释、可回溯

## Open Questions

- examiner 使用与 main agent 相同模型还是单独模型
- reject 阈值和 abort 策略的默认值如何设置最合适
