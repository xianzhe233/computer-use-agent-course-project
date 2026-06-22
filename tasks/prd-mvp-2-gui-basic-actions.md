# PRD: MVP 2 GUI 基础动作

## Introduction / Overview

本阶段在 MVP 1 terminal 闭环基础上，引入最基础的 GUI 观察与交互能力，包括截图、点击、文本输入、快捷键和拖拽。目标不是立即做到高精度元素理解，而是让主 agent 能在简单桌面环境中完成基础 GUI 操作，并把截图证据纳入运行记录。

## Goals

- 支持基础 GUI 观察与交互动作
- 把截图纳入运行证据链
- 让主 agent 能在简单 GUI 任务中执行基本流程
- 保持工具层接口清晰，便于后续接入定位能力

## User Stories

### US-001: 实现截图工具与截图索引记录
**Description:** As a developer, I want a screenshot tool with persisted metadata so that GUI observations become part of the evidence chain.

**Acceptance Criteria:**
- [ ] 提供 `take_screenshot` 工具
- [ ] 截图保存到运行目录下的统一位置
- [ ] 记录截图元信息（时间、路径、分辨率、来源步骤）
- [ ] 可从运行目录回看截图证据
- [ ] Tests/typecheck/lint passes

### US-002: 实现基础 GUI 动作工具
**Description:** As a developer, I want core GUI action tools so that the main agent can interact with simple desktop interfaces.

**Acceptance Criteria:**
- [ ] 至少实现 `click`、`type_text`、`hotkey`、`drag` 四种工具
- [ ] 每个工具返回统一成功/失败结果结构
- [ ] 工具接口与 `doc/project/tooling-design.md` 总体方向一致
- [ ] 工具失败时有结构化错误返回
- [ ] Tests/typecheck/lint passes

### US-003: 将 GUI 动作纳入 trace 与 artifact 记录
**Description:** As a developer, I want GUI actions and related screenshots persisted in the trace so that later debugging and验收 can rely on them.

**Acceptance Criteria:**
- [ ] GUI 动作执行前后可关联截图或其它 artifact
- [ ] trace 中能看出动作类型、参数、结果与相关截图引用
- [ ] GUI 工具调用不会绕过统一 runtime 记录层
- [ ] Tests/typecheck/lint passes

### US-004: 打通一个简单 GUI 任务样例
**Description:** As a user, I want the main agent to complete a simple GUI task so that MVP 2 can be demonstrated with visible desktop interaction.

**Acceptance Criteria:**
- [ ] 至少 1 个简单 GUI 任务可重复运行（如记事本输入/保存前半流程）
- [ ] 主 agent 能在 terminal 与 GUI 分支中做基本选择
- [ ] 任务过程包含截图证据
- [ ] Verify in browser if relevant; desktop GUI tasks should at least be visually reviewed via screenshots
- [ ] Tests/typecheck/lint passes

## Functional Requirements

- FR-1: 系统必须支持基础截图能力
- FR-2: 系统必须支持基础 GUI 原子动作
- FR-3: 系统必须把 GUI 动作纳入统一 trace 与 artifact 记录
- FR-4: 系统必须支持至少一个 GUI 演示任务

## Non-Goals

- 不要求基于描述自动定位元素
- 不引入 examiner 验收闭环
- 不追求复杂多窗口、多应用混合任务的稳定完成

## Design Considerations

- GUI 动作接口必须保持原子、明确，不把多个动作混成一个工具
- 截图既是调试素材，也是未来 examiner 的证据基础

## Technical Considerations

- 工具实现要与 Windows 环境兼容
- 返回结构要便于未来统一 reducer / state updater 处理

## Success Metrics

- 至少 1 个简单 GUI 任务能稳定演示
- 截图证据与动作 trace 能够对齐回溯
- 为 MVP 3 接入元素定位时不需要重写已实现的 GUI 工具接口

## Open Questions

- GUI 基础动作默认采用哪些底层库实现最稳
- 是否在 MVP 2 同时补充 `wait` / `move_mouse` 等辅助工具
