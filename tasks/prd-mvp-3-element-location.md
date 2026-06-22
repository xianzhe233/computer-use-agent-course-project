# PRD: MVP 3 元素定位

## Introduction / Overview

本阶段把 GUI 交互从纯坐标驱动提升为“描述驱动”的定位+点击模式。核心能力是 `locate_element`：根据截图和元素描述返回候选区域，再由主 agent 决定是否点击。目标是提升 GUI 操作的可解释性和可迁移性，而不是立即实现通用视觉智能。

## Goals

- 支持基于描述的元素定位能力
- 保持定位与点击解耦
- 为 GUI 决策提供更可解释的证据
- 引入定位失败时的回退策略

## User Stories

### US-001: 定义 `locate_element` 统一接口
**Description:** As a developer, I want a structured `locate_element` interface so that visual targeting can be integrated into the runtime consistently.

**Acceptance Criteria:**
- [ ] 定义 `locate_element(query, screenshot)` 输入输出格式
- [ ] 输出至少包含 bbox、confidence、reason
- [ ] 接口与 `doc/project/runtime-design.md` 中的定位工具设想兼容
- [ ] Tests/typecheck/lint passes

### US-002: 实现定位结果与点击解耦流程
**Description:** As a developer, I want element location to be a separate step from clicking so that the trace stays explainable and agent decisions remain explicit.

**Acceptance Criteria:**
- [ ] `locate_element` 本身不直接触发点击
- [ ] 主 agent 可以先定位再决定点击 bbox 中心或调整策略
- [ ] trace 中能单独看到定位和点击两步
- [ ] Tests/typecheck/lint passes

### US-003: 增加定位失败回退策略
**Description:** As a developer, I want fallback behavior when element location fails so that the agent does not blindly continue with unreliable clicks.

**Acceptance Criteria:**
- [ ] 连续定位失败时可选择重新截图、滚动或放弃当前方案
- [ ] 失败会记录结构化错误与建议下一步
- [ ] 不允许定位失败后默认盲点
- [ ] Tests/typecheck/lint passes

### US-004: 用描述驱动方式完成一个 GUI 任务样例
**Description:** As a user, I want the system to locate and operate a GUI target from a textual description so that MVP 3 can demonstrate improved interaction quality.

**Acceptance Criteria:**
- [ ] 至少 1 个 GUI 任务样例通过“描述 -> bbox -> click”流程完成关键一步
- [ ] 任务中保留定位结果与动作证据
- [ ] Verify in browser if relevant; otherwise provide screenshot-based visual verification
- [ ] Tests/typecheck/lint passes

## Functional Requirements

- FR-1: 系统必须支持基于文本描述的元素定位接口
- FR-2: 系统必须将定位与点击动作解耦
- FR-3: 系统必须记录定位成功/失败及其依据
- FR-4: 系统必须提供定位失败后的回退策略

## Non-Goals

- 不追求通用复杂界面的高鲁棒定位
- 不要求所有 GUI 操作都改为定位驱动
- 不引入 examiner 返工闭环

## Design Considerations

- 定位结果本身应成为可供后续审查的证据
- 运行时需避免把定位失败静默吞掉

## Technical Considerations

- 后续可能引入视觉模型、OCR 或 Windows UIA 辅助验证
- 返回结构要为未来 `source = vision / uia / hybrid` 预留空间

## Success Metrics

- 至少 1 个任务样例能演示描述驱动的定位交互
- trace 中能解释“为什么点这里”
- 失败时不会退化为不可解释的盲点行为

## Open Questions

- 首版定位优先依赖视觉模型、OCR 还是 Windows UIA
- 是否在 MVP 3 就纳入 `view_screenshot` 历史回看辅助流程
