# 项目进度

## 进度记录

| 时间 | 状态 | 内容 |
|---|---|---|
| 2026-06-22 09:20 | 已完成 | 完成 computer use agent 课设方案初步分析，明确主 agent + examiner 验收闭环、终端/GUI 双执行分支、截图证据链，并识别 GUI 定位与结束判定为主要风险。 |
| 2026-06-22 09:53 | 已完成 | 在初步分析基础上继续细化方案：确认以 LangGraph 状态机承载主循环，建议把动作协议、运行轨迹、截图证据链、examiner 验收规则写入设计文档，并强调 GUI grounding、Windows 环境稳定性与返工循环上限为后续实现重点。 |
| 2026-06-22 10:01 | 已完成 | 新增项目设计文档初稿（初始创建于 `doc/project-design.md`，现位于 `doc/project/project-design.md`），系统化整理了项目目标、边界、双 agent 架构、工具协议、轨迹证据链、MVP 路线、测试方案与主要风险，后续修改将以该文档为统一口径来源。 |
| 2026-06-22 10:14 | 已完成 | 新增 `doc/project/` 作为项目信息专用子目录，并将项目设计文档迁移到 `doc/project/project-design.md`；同步更新项目地图、结构说明与目录约定，后续项目相关文档统一收敛到该目录。 |
| 2026-06-22 10:24 | 已完成 | 新增 `doc/references/` 外部参考目录，并整理 OpenAI Computer Use、Anthropic Computer Use、Windows-Use、OmniParser、UI-TARS Desktop、OSWorld 的来源、结构与借鉴价值；同步把参考目录纳入项目地图、结构说明和设计文档。 |
| 2026-06-22 10:41 | 已完成 | 基于实用性判断，将 OmniParser 从当前项目口径中移除：删除 `doc/references/omniparser.md`，更新参考总览与结构说明；复核后确认主体设计文档中无对 OmniParser 的显式依赖，因此保留现有元素定位设计。 |
| 2026-06-22 11:13 | 已完成 | 聚焦 GUI 工具层完成外部参考梳理：确认 `Windows-Use` 适合直接借鉴 Windows/UIA/PowerShell 工具封装，`OpenAI CUA sample` 适合借鉴 screenshot→action→observation 循环与原子动作协议，`UI-TARS SDK` 适合借鉴 operator 抽象与 action space 定义，`OSWorld` 主要用于动作空间与证据记录参考，`Anthropic` 主要用于安全约束与工具组合边界。 |
| 2026-06-22 11:15 | 已完成 | 新增 `doc/project/tooling-design.md` 作为工具专项设计文档，系统化写清必抄与强烈建议工具、每项工具的最佳参考来源、借鉴理由与建议实现方式；同步在总设计文档与结构说明中增加该文档入口，后续工具实现以该文档为主口径。 |
| 2026-06-22 11:31 | 已完成 | 新增 `doc/project/runtime-design.md` 作为运行时专项设计文档，补齐 LangGraph 状态对象、主 agent / examiner / 工具层协议、run store 与证据链格式、`locate_element` 运行时调度、风险拦截和失败恢复策略；同步在总设计文档与结构说明中增加文档入口，后续编码时以该文档作为 runtime 实现主口径。 |
| 2026-06-22 11:36 | 已完成 | 新增 `config/` 本地模型配置目录，落入 `uitars` 的 OpenRouter key 与 `mimo` 的 Token Plan CN 配置；同时补充 `models.example.json`、`config/README.md` 与根目录 `.gitignore`，为后续主 agent / examiner / GUI grounding 接线提供统一配置入口。 |
| 2026-06-22 13:50 | 已完成 | 评估过 `ralphi` 作为阶段推进方案，但后续确认其交互形态不适配当前实际工作方式，因此不再作为本项目的主开发控制手段。 |
| 2026-06-22 13:59 | 已完成 | 已将 MVP 1~5 分别整理为 `tasks/prd-mvp-*.md` 五份独立 PRD 文档，后续按阶段推进开发时，统一以这些 PRD 作为范围控制、验收约束与任务拆解输入。 |
| 2026-06-22 16:26 | 已完成 | 已移除项目中的 `ralphi` / skill 相关安装产物、runtime 配置与工作流文档，仅保留 `tasks/` 下的阶段 PRD；当前项目回到“设计文档 + 阶段 PRD”驱动的开发方式。 |

## 使用约定

- 有“完成了一步”的事实时再追加，不写空话。
- 每条记录尽量回答两件事：做了什么、现在怎样；若有阻塞可直接写在内容里。
- 如果出现阻塞，直接写明阻塞点，不要隐藏。
