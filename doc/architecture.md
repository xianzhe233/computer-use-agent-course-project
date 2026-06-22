# 项目结构

当前仓库已建立基础 AI 协作文档骨架。

## 当前目录

```text
.
├── AGENTS.md
├── config/
│   ├── README.md
│   ├── models.example.json
│   └── models.local.json
├── doc/
│   ├── architecture.md
│   ├── progress.md
│   ├── task-workflow.md
│   ├── commit-conventions.md
│   ├── work-template.md
│   ├── log.md
│   ├── project/
│   │   ├── project-design.md
│   │   ├── runtime-design.md
│   │   └── tooling-design.md
│   ├── references/
│   │   ├── README.md
│   │   ├── openai-computer-use.md
│   │   ├── anthropic-computer-use.md
│   │   ├── windows-use.md
│   │   ├── ui-tars-desktop.md
│   │   └── osworld.md
│   ├── current_tasks/
│   └── agent_log/
└── tasks/
    ├── prd-mvp-1-terminal-agent.md
    ├── prd-mvp-2-gui-basic-actions.md
    ├── prd-mvp-3-element-location.md
    ├── prd-mvp-4-examiner-rework-loop.md
    └── prd-mvp-5-demo-packaging.md
    ├── architecture.md
    ├── progress.md
    ├── task-workflow.md
    ├── commit-conventions.md
    ├── work-template.md
    ├── log.md
    ├── project/
    │   ├── project-design.md
    │   ├── runtime-design.md
    │   └── tooling-design.md
    ├── references/
    │   ├── README.md
    │   ├── openai-computer-use.md
    │   ├── anthropic-computer-use.md
    │   ├── windows-use.md
    │   ├── ui-tars-desktop.md
    │   └── osworld.md
    ├── current_tasks/
    └── agent_log/
```

## 目录说明

- `AGENTS.md`：项目入口地图，告诉协作者先看什么、做完后更新什么。
- `config/`：项目本地模型配置目录，集中存放后续 agent / examiner / GUI grounding 读取的 provider、role 与模板配置。
- `config/README.md`：说明本地模型配置文件的用途、角色分配和使用约定。
- `config/models.local.json`：本机实际使用的模型配置，包含敏感 key，仅供本地运行使用。
- `config/models.example.json`：不含密钥的模型配置模板，用于迁移、共享结构与后续接线参考。
- `doc/architecture.md`：记录仓库结构和目录职责。
- `doc/project/`：项目信息专用目录，集中存放项目设计、需求、方案等项目相关文档。
- `doc/project/project-design.md`：项目设计文档，统一记录课设目标、系统方案、MVP 路线、测试方案与风险约束。
- `doc/project/runtime-design.md`：运行时专项设计文档，集中记录状态对象、agent/examiner 协议、run store、风险控制与失败恢复。
- `doc/project/tooling-design.md`：工具专项设计文档，集中记录 GUI/terminal 工具清单、优先级、最佳参考来源与具体借鉴策略。
- `doc/references/`：外部参考目录，集中存放可借鉴的外部项目、文档和 benchmark 整理笔记。
- `doc/references/README.md`：外部参考总览，汇总来源、结构、可借鉴点和优先参考顺序。
- `doc/progress.md`：记录阶段进展、阻塞与阶段状态。
- `doc/task-workflow.md`：记录任务从创建、执行到归档的详细流程要求。
- `doc/commit-conventions.md`：记录 git / commit / push 规则。
- `doc/work-template.md`：单任务工作模板。
- `doc/log.md`：记录规则、模板、工作流的变更。
- `doc/current_tasks/`：正在进行中的任务笔记。
- `doc/agent_log/`：已完成任务的归档目录。
- `tasks/`：功能级阶段 PRD 目录，用于拆分 MVP 范围、约束验收标准，并在后续开发时作为当前阶段的控制输入。
## 当前约定

1. 所有任务先建笔记，再开始执行。
2. 开工前先读 `doc/task-workflow.md`，不要只复制模板不看流程。
3. 涉及 `git / commit / push` 的任务，先读 `doc/commit-conventions.md`。
4. 所有规则类文档统一收敛在 `doc/`。
5. 项目信息类文档统一放在 `doc/project/`，不要散落在 `doc/` 根目录。
6. 外部参考资料统一放在 `doc/references/`，不要混进 `doc/project/`。
7. `AGENTS.md` 与 `doc/` 当前作为本地协作文件存在，不纳入版本控制。
8. 根目录暂不随意堆文件；新增稳定目录后，先在本文件补充说明。
9. 本地敏感配置统一收敛到 `config/`，并通过忽略规则避免误提交。

## 后续扩展方式

当项目进入实际开发阶段时，建议按真实需要补充，例如：

- `src/`：主要源码目录
- `apps/`：多应用工作区
- `packages/`：共享包或模块
- `scripts/`：自动化脚本
- `tests/`：测试代码与测试资源
- `resources/`：外部素材、样例或原始数据
- `output/`：导出结果或构建产物

新增以上目录时，请同步补三件事：

1. 在本文件登记目录职责
2. 在 `doc/progress.md` 记录阶段变化
3. 若工作方式改变，在 `doc/log.md` 追加说明

## 维护原则

- 本文件记录“现在真实存在的结构”与“已经确定的扩展约定”。
- 不要把还没创建的复杂结构写得像已经完成。
- 目录一旦重命名、拆分、合并，应第一时间更新本文件。
