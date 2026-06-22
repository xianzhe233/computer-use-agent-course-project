# PRD: MVP 5 Demo 整理与展示

## Introduction / Overview

本阶段把前四个技术阶段整理成可展示、可复现的课设 demo。重点不是再增加底层能力，而是沉淀固定任务集、运行产物结构、展示流程和验证方式，让系统可以稳定演示“执行—证据—验收”的完整链条。

## Goals

- 形成固定 demo 任务集
- 明确演示时需要保留的轨迹、截图与结果
- 提高多次运行下的可复现性
- 为课设答辩或展示准备稳定材料

## User Stories

### US-001: 整理固定 demo 任务集
**Description:** As a developer, I want a curated demo task set so that the system can be evaluated and presented on a stable workload.

**Acceptance Criteria:**
- [ ] 整理 5-10 个适合本地 Windows 环境的固定任务
- [ ] 覆盖 terminal、GUI、定位、examiner 等关键能力
- [ ] 每个任务有简短说明、预期结果和注意事项
- [ ] Tests/typecheck/lint passes

### US-002: 标准化 run 产物与展示材料结构
**Description:** As a developer, I want a predictable artifact layout so that every demo run leaves behind evidence that can be shown and reviewed.

**Acceptance Criteria:**
- [ ] 明确运行目录、trace、screenshots、summary 等产物结构
- [ ] 展示时可以快速定位关键截图和关键步骤
- [ ] 至少有一份总结性结果文件或等价视图说明本次运行结论
- [ ] Tests/typecheck/lint passes

### US-003: 为 demo 任务补齐验证与复查口径
**Description:** As a reviewer, I want a clear verification method for each demo task so that task success is not left to subjective impression.

**Acceptance Criteria:**
- [ ] 每个 demo 任务明确人工或系统内验证方式
- [ ] 对 GUI 任务明确截图或可视证据要求
- [ ] examiner 相关任务明确验收与返工判断口径
- [ ] Tests/typecheck/lint passes

### US-004: 形成完整演示流程说明
**Description:** As a presenter, I want a concise runbook for the demo so that the project can be shown end-to-end without临场混乱.

**Acceptance Criteria:**
- [ ] 提供从任务输入到结果查看的演示顺序说明
- [ ] 明确需要预先准备的环境、配置和应用
- [ ] 明确 demo 中优先展示的成功路径和可接受的失败解释方式
- [ ] Tests/typecheck/lint passes

## Functional Requirements

- FR-1: 系统必须有固定 demo 任务集
- FR-2: 系统必须有可复查的运行产物结构
- FR-3: 系统必须有清晰的展示与验证口径
- FR-4: 系统必须支持复现同类任务的稳定演示

## Non-Goals

- 不再引入新的核心底层能力作为本阶段重点
- 不追求覆盖开放世界或完整 benchmark
- 不追求商业级产品化展示系统

## Design Considerations

- 展示重点是“流程能看懂、证据能回溯、结果能解释”
- 不要求每个任务都完美成功，但失败也必须可解释

## Technical Considerations

- demo 任务应尽量避开依赖不稳定、环境敏感过高的外部因素
- 运行产物结构需与前述 runtime 设计一致

## Success Metrics

- 能稳定演示 5-10 个固定任务中的核心样例
- 观众可以从轨迹、截图与 examiner 结论中理解系统行为
- 项目具备可复现实验与答辩展示材料

## Open Questions

- demo 任务集最终选哪几个作为正式展示主路径
- 是否需要额外整理一页展示脚本或答辩讲解提纲
