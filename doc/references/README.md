# 外部参考目录

本目录用于集中存放外部参考项目的整理文档，便于后续实现时快速查找“可以借鉴什么、该去哪里抄结构、哪些点对本项目最有用”。

## 使用方式

1. 先看本文件，快速确定优先参考对象。
2. 需要细看时，再打开对应单独文档。
3. 后续如果新增参考项目，优先补充到本目录，而不是散写在聊天记录里。

## 当前参考对象总览

| 参考对象 | 来源 | 大致结构 | 对我们项目的帮助 |
|---|---|---|---|
| OpenAI Computer Use 文档 | OpenAI 官方文档：`https://developers.openai.com/api/docs/guides/tools-computer-use` | 以 harness 为中心，围绕 screenshot → model action → environment execution → next observation 的循环展开 | 可借鉴主循环、动作协议、截图驱动执行方式 |
| Anthropic Computer Use 文档 | Anthropic 官方文档：`https://docs.anthropic.com/en/docs/agents-and-tools/computer-use`；补充工具文档：`https://docs.claude.com/en/docs/agents-and-tools/tool-use/computer-use-tool` | 以 computer tool + bash + text editor 的组合工具模式展开，并强调安全边界和错误处理 | 可借鉴工具组合、提示词风格、安全限制与错误恢复 |
| Windows-Use | GitHub：`https://github.com/CursorTouch/Windows-Use` | 围绕 Windows UI Automation、PowerShell、文件读写和 GUI 动作封装组织 | 可借鉴 Windows 环境适配、GUI/系统工具封装方式 |
| UI-TARS Desktop | GitHub：`https://github.com/bytedance/ui-tars-desktop` | 更像一个桌面 GUI agent 产品，包含应用形态、运行环境、任务执行与交互界面 | 可借鉴 demo 形态、桌面 agent 的产品组织、交互流程与展示方式 |
| OSWorld | GitHub：`https://github.com/xlang-ai/osworld`；项目页：`https://os-world.github.io/` | 以任务、环境、评测方式为核心，是面向 computer-use agent 的 benchmark | 可借鉴任务设计、评测思路、测试任务组织方式 |

## 当前建议的优先借鉴顺序

1. **OpenAI Computer Use**：先抄主循环和 harness 思想。
2. **Anthropic Computer Use**：再抄工具组合、安全提示和错误处理。
3. **Windows-Use**：重点抄 Windows 下的系统/GUI 工具封装。
4. **UI-TARS Desktop**：重点抄 demo 展示形态和桌面产品组织。
5. **OSWorld**：重点抄任务与评测设计，不直接照搬环境。

## 后续补充要求

新增参考文档时，至少包含：

- 来源
- 大致结构
- 值得借鉴的部分
- 对我们项目的帮助
- 适合在哪个 MVP 阶段参考
