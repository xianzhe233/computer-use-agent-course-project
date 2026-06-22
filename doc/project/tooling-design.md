# Computer Use Agent 工具专项设计文档

## 1. 文档目的

本文档专门聚焦本课设项目的 **工具层设计**，用于回答三个问题：

1. 我们最终要提供哪些工具；
2. 每个工具最适合参考哪个外部项目；
3. 这些外部项目里，哪些实现细节适合直接借鉴，哪些只适合吸收思路。

本文件是 `doc/project/project-design.md` 的工具层展开版。总设计文档负责整体系统与流程，本文件负责把工具层写细，写到足以直接指导后续实现。

## 2. 设计范围

本文件只纳入此前已经确认的两类工具：

- **必抄工具**：MVP 1～3 必须具备，否则 GUI agent 主流程跑不起来；
- **强烈建议工具**：虽然不是第一天就必须写完，但对 Windows 环境稳定性、可演示性、证据链完整性非常重要，建议在 GUI 分支早期就纳入。

本文件暂不展开：

- 浏览器 DOM 专用工具；
- 多桌面、文件批量编辑等实验性工具；
- 高度产品化的远程控制、账号管理、云端会话编排。

## 3. 工具选型原则

### 3.1 选型标准

每个工具的“最佳参考来源”按以下标准选择：

1. **与我们需求的接近程度**：优先选择最像“Windows 本地 computer use agent”的实现；
2. **动作粒度是否清晰**：优先选择原子动作边界清楚、方便接入 LangGraph 的实现；
3. **是否利于证据链记录**：优先选择天然适合 screenshot → action → observation 的实现；
4. **是否能落地**：优先选择工程上已经证明可跑，而不是只有概念；
5. **是否适合课设裁剪**：优先选择可以被我们适配后直接用，而不是只能整套照搬的大系统。

### 3.2 外部来源的职责划分

为避免“什么都想抄，最后反而没有主线”，这里先明确各参考对象最适合承担的职责：

- **`Windows-Use`**：最适合抄 Windows 本地工具封装，包括 `shell_tool`、`click_tool`、`scroll_tool`、`move_tool`、`shortcut_tool`、`app_tool`、`wait_tool` 和 UIA 思路。
- **`OpenAI Computer Use / OpenAI CUA sample`**：最适合抄 screenshot 驱动的 harness、原子动作协议和 action → observation 主循环。
- **`UI-TARS SDK`**：最适合抄 `operator` 抽象，即 `screenshot()` + `execute()` 两个核心方法，以及 action space 的组织方式。
- **`Anthropic Computer Use`**：最适合抄工具说明文字、安全边界、动作校验、失败处理和日志规范。
- **`OSWorld`**：最适合抄截图/动作/视频证据保存与人工复核思路，不适合直接当工具实现模板。

## 4. 工具总览

| 工具 | 优先级 | 最佳参考来源 | 选择理由 |
|---|---|---|---|
| `run_command` | 必抄 | `Windows-Use` | 直接对应 `shell_tool`，最接近 Windows 本地命令执行需求 |
| `take_screenshot` | 必抄 | `UI-TARS SDK` | 明确把 `screenshot()` 作为 operator 核心方法，接口抽象最清楚 |
| `click` | 必抄 | `Windows-Use` | 已有 Windows GUI 点击封装，最贴近我们的环境 |
| `double_click` | 必抄 | `UI-TARS SDK` | 在 operator 支持列表中显式存在，动作语义清晰 |
| `right_click` | 必抄 | `Windows-Use` | `click_tool` 明确支持左右中键，更接近 Windows 桌面交互 |
| `type_text` | 必抄 | `Windows-Use` | `type_tool` 直接对应文本输入场景 |
| `hotkey` | 必抄 | `Windows-Use` | `shortcut_tool` 已覆盖快捷键场景 |
| `scroll` | 必抄 | `Windows-Use` | `scroll_tool` 显式支持纵向/横向滚动 |
| `drag` | 必抄 | `Windows-Use` | `move_tool` 已覆盖拖拽，且更贴近 Windows 使用 |
| `wait` | 必抄 | `Windows-Use` | `wait_tool` 直接可借鉴，适合处理异步加载与窗口切换 |
| `move_mouse` | 强烈建议 | `Windows-Use` | `move_tool` 已有鼠标移动语义，可与 hover 分离 |
| `hover` | 强烈建议 | `UI-TARS SDK` | operator 支持列表中显式包含 hover，动作语义独立 |
| `open_app` | 强烈建议 | `Windows-Use` | `app_tool` 最接近 Windows 应用启动需求 |
| `switch_app` | 强烈建议 | `Windows-Use` | `app_tool` 明确支持切换应用窗口 |
| `focus_window` | 强烈建议 | `Windows-Use` | `app_tool` 与 UIA 思路结合最贴近窗口聚焦需求 |
| `locate_element` | 强烈建议 | `Windows-Use` | UIA 是现有参考里最接近 Windows 元素定位与验证的方案 |
| `view_screenshot` | 强烈建议 | `OSWorld` | 最接近“保存截图后再回看/人工复核”的证据链使用方式 |

## 5. 工具层统一抽象

在具体工具之前，先统一我们自己的工具接口理念。

### 5.1 分层结构

建议把工具层拆成三层：

1. **Agent Tool Schema 层**
   - 给主 agent / examiner 看的结构化工具定义；
   - 例如 `click(x, y, button)`、`view_screenshot(screenshot_id)`。

2. **Operator 层**
   - 统一承接 GUI 操作；
   - 核心参考 `UI-TARS SDK` 的 `screenshot()` 与 `execute()` 思路；
   - 负责把高层动作分发给底层执行器。

3. **Backend Adapter 层**
   - 真正对接 `Windows-Use` 风格的鼠标、键盘、UIA、PowerShell 或其它本地实现；
   - 后续如果替换底层实现，不影响 LangGraph 上层逻辑。

### 5.2 统一返回结构

建议所有工具统一返回以下公共字段：

- `tool_name`
- `success`
- `timestamp`
- `duration_ms`
- `error`
- `artifacts`
- `note`

其中：

- GUI 动作类工具应尽量在 `artifacts` 中挂接相关截图 ID；
- 命令类工具应尽量挂接日志文件路径；
- 失败时必须给出结构化 `error`，避免只返回自然语言描述。

## 6. 必抄工具详细设计

### 6.1 `run_command(command, shell="powershell", timeout_s=30)`

**优先级**：必抄

**最佳参考来源**：`Windows-Use`

**来源说明**：
- 上游项目：`Windows-Use`
- 对应能力：`shell_tool`
- 本仓库参考入口：`doc/references/windows-use.md`

**为什么选它**：

在所有参考对象里，`Windows-Use` 是唯一明确把 **Windows 本地命令执行** 作为 agent 常规工具封装出来的项目。我们的课设目标也明确优先支持 Windows，因此它比 OpenAI/Anthropic 那种偏通用 harness 的描述更接近实际需求。

**建议直接借鉴的点**：

- 用 PowerShell 作为默认终端语义；
- 命令执行结果结构化返回，而不是只吐一段纯文本；
- 工具层负责超时、失败码和日志落盘；
- 与 GUI 工具并列存在，而不是混进 agent prompt 里临时解释。

**我们建议的行为**：

- 默认通过 PowerShell 执行；
- 返回 `stdout`、`stderr`、`exit_code`、`duration_ms`；
- 自动把完整输出写入 `runs/<run_id>/command_logs/`；
- 对高风险命令预留拦截位。

**不建议照搬的点**：

- 不要把所有系统级功能都放进一个“万能 shell 工具”；
- 不要让 agent 直接依赖过多 PowerShell 特化语法，避免 prompt 太脆弱。

### 6.2 `take_screenshot(target="screen")`

**优先级**：必抄

**最佳参考来源**：`UI-TARS SDK`

**来源说明**：
- 上游项目：`UI-TARS SDK`
- 对应能力：`operator.screenshot()`
- 本仓库参考入口：`doc/references/ui-tars-desktop.md`

**为什么选它**：

虽然 OpenAI 的 computer use 更强调 screenshot 驱动，但就“**把截图定义成一个清晰的底层 operator 方法**”来说，`UI-TARS SDK` 更接近我们要写的代码结构。它明确要求自定义 operator 至少实现 `screenshot()` 和 `execute()` 两个核心方法，这与我们准备做的 GUI 工具适配层非常一致。

**建议直接借鉴的点**：

- 把截图做成基础观测原语；
- 返回统一的截图对象，而不是仅返回文件路径；
- 允许后续兼容屏幕截图、窗口截图、区域截图；
- 把截图方法放在 operator 抽象层，而不是散落在各工具内部。

**我们建议的行为**：

返回字段至少包括：

- `screenshot_id`
- `path`
- `width`
- `height`
- `timestamp`
- `target`

截图文件统一落到 `runs/<run_id>/screenshots/`。

**补充说明**：

虽然主接口参考 `UI-TARS SDK`，但主循环语义应同时吸收 `OpenAI Computer Use` 的 screenshot → action → observation 思路，即：**执行关键 GUI 动作后应尽量补一张新截图**。

### 6.3 `click(x, y, button="left")`

**优先级**：必抄

**最佳参考来源**：`Windows-Use`

**来源说明**：
- 上游项目：`Windows-Use`
- 对应能力：`click_tool`
- 本仓库参考入口：`doc/references/windows-use.md`

**为什么选它**：

`Windows-Use` 的点击工具就是面向 Windows 桌面交互写的，比 OpenAI sample 中偏浏览器实验环境的动作描述更接近我们要做的本地 GUI agent。

**建议直接借鉴的点**：

- 点击动作与按钮类型分离；
- 工具只负责执行，不负责解释业务含义；
- 保留坐标驱动模式，便于与 `locate_element` 解耦。

**我们建议的行为**：

- 参数：`x`, `y`, `button`, `clicks=1`
- 返回：坐标、按钮类型、是否成功、执行前后截图引用（可选）

**不建议照搬的点**：

- 不要把 hover、drag、double click 全塞成 click 的特殊文本参数；
- 这些动作应在工具接口上保持清晰边界。

### 6.4 `double_click(x, y)`

**优先级**：必抄

**最佳参考来源**：`UI-TARS SDK`

**来源说明**：
- 上游项目：`UI-TARS SDK`
- 对应能力：默认 operator 支持 `double click`
- 本仓库参考入口：`doc/references/ui-tars-desktop.md`

**为什么选它**：

`Windows-Use` 的 README 中对双击没有像左右键点击那样明确列出，而 `UI-TARS SDK` 的 operator 能力列表里把 double click 作为显式动作给出，因此更适合作为我们这个独立工具的直接来源。

**建议直接借鉴的点**：

- 把双击视为独立动作，而不是靠 `click(clicks=2)` 在 prompt 层隐含表达；
- 让模型在文件打开、桌面图标操作等场景里能稳定调用。

**我们建议的行为**：

- 参数：`x`, `y`
- 返回：坐标、双击间隔配置、成功状态

### 6.5 `right_click(x, y)`

**优先级**：必抄

**最佳参考来源**：`Windows-Use`

**来源说明**：
- 上游项目：`Windows-Use`
- 对应能力：`click_tool` 支持 `left / right / middle`
- 本仓库参考入口：`doc/references/windows-use.md`

**为什么选它**：

Windows 桌面任务里右键菜单非常常见，例如文件操作、桌面菜单、应用上下文菜单；`Windows-Use` 对按钮类型支持更贴近这个使用场景。

**建议直接借鉴的点**：

- 把按钮类型做成底层枚举；
- 执行右键后允许上层立刻请求截图或后续键盘操作。

**我们建议的行为**：

- 参数：`x`, `y`
- 返回：坐标、按钮类型固定为 `right`

### 6.6 `type_text(text, enter=False)`

**优先级**：必抄

**最佳参考来源**：`Windows-Use`

**来源说明**：
- 上游项目：`Windows-Use`
- 对应能力：`type_tool`
- 本仓库参考入口：`doc/references/windows-use.md`

**为什么选它**：

文本输入是 GUI 任务里最常见的动作之一，`Windows-Use` 已经把它独立成 `type_tool`，这与我们的工具拆分方向一致。

**建议直接借鉴的点**：

- 输入动作单独建工具，而不是混在 `hotkey` 内部；
- 允许后续扩展为粘贴模式、逐字输入模式；
- 对敏感信息输入预留显式确认钩子，吸收 Anthropic 的安全思想。

**我们建议的行为**：

- 参数：`text`, `enter=False`
- 返回：输入长度、是否附带回车、成功状态

**安全要求**：

- 若输入内容涉及敏感数据，需允许上层策略拦截；
- 不在 trace 中裸存高敏感文本原文，可按需脱敏。

### 6.7 `hotkey(keys)`

**优先级**：必抄

**最佳参考来源**：`Windows-Use`

**来源说明**：
- 上游项目：`Windows-Use`
- 对应能力：`shortcut_tool`
- 本仓库参考入口：`doc/references/windows-use.md`

**为什么选它**：

在 Windows GUI 自动化里，快捷键往往比纯鼠标更稳，例如 `Ctrl+S`、`Alt+Tab`、`Win+R`。`Windows-Use` 对这个能力的组织最直接。

**建议直接借鉴的点**：

- 快捷键作为独立工具；
- 参数采用数组或标准化字符串，而不是自然语言；
- 支持组合键与顺序键的区分。

**我们建议的行为**：

- 参数：`keys=["ctrl", "s"]` 或标准化字符串
- 返回：执行的标准化按键序列

### 6.8 `scroll(direction, amount=None)`

**优先级**：必抄

**最佳参考来源**：`Windows-Use`

**来源说明**：
- 上游项目：`Windows-Use`
- 对应能力：`scroll_tool`
- 本仓库参考入口：`doc/references/windows-use.md`

**为什么选它**：

`Windows-Use` 明确提到既支持垂直也支持水平滚动，这对桌面应用、文件列表和设置面板都很实用。

**建议直接借鉴的点**：

- 滚动方向显式化；
- 允许相对滚动量；
- 与截图配合，用于“看不见就继续找”的场景。

**我们建议的行为**：

- 参数：`direction in {up, down, left, right}`，`amount`
- 返回：方向、幅度、成功状态

### 6.9 `drag(x1, y1, x2, y2)`

**优先级**：必抄

**最佳参考来源**：`Windows-Use`

**来源说明**：
- 上游项目：`Windows-Use`
- 对应能力：`move_tool` 的 drag-and-drop 能力
- 本仓库参考入口：`doc/references/windows-use.md`

**为什么选它**：

我们的任务里很可能出现窗口移动、文件拖动、滑块拖拽等操作；`Windows-Use` 已经把 move/drag 作为一类底层动作处理，这比只停留在理论动作协议更可落地。

**建议直接借鉴的点**：

- 拖拽起点和终点显式给出；
- 允许后续扩展中间轨迹或持续时间；
- 与 `click` 分离，避免语义混杂。

**我们建议的行为**：

- 参数：`x1`, `y1`, `x2`, `y2`
- 返回：起止坐标、成功状态

### 6.10 `wait(seconds)`

**优先级**：必抄

**最佳参考来源**：`Windows-Use`

**来源说明**：
- 上游项目：`Windows-Use`
- 对应能力：`wait_tool`
- 本仓库参考入口：`doc/references/windows-use.md`

**为什么选它**：

真实桌面环境里窗口切换、菜单展开、文件保存、应用启动都存在延迟；`wait` 是让 agent 稳定运行的必要工具，而不是“偷懒动作”。

**建议直接借鉴的点**：

- 显式等待，而不是把睡眠逻辑藏在各个动作工具内部；
- 让等待也进入 trace，便于调试 agent 为什么停顿。

**同时吸收 `Anthropic` 的经验**：

Anthropic 文档明确建议 **给动作增加 delay、执行前校验、记录调试日志**，这说明等待不只是工具能力，也是安全与稳定性的组成部分。

**我们建议的行为**：

- 参数：`seconds`
- 返回：等待时长、成功状态

## 7. 强烈建议工具详细设计

### 7.1 `move_mouse(x, y)`

**优先级**：强烈建议

**最佳参考来源**：`Windows-Use`

**来源说明**：
- 上游项目：`Windows-Use`
- 对应能力：`move_tool`
- 本仓库参考入口：`doc/references/windows-use.md`

**为什么选它**：

`move_mouse` 能解决两类问题：

1. 某些交互需要先移动再判断；
2. 调试 GUI 失败时，单独移动鼠标比直接点击更容易定位问题。

**建议直接借鉴的点**：

- 把“移动”与“点击”分离；
- 为 hover、拖拽、局部探索提供更底层动作。

**我们建议的行为**：

- 参数：`x`, `y`
- 返回：目标坐标、成功状态

### 7.2 `hover(x, y, duration_ms=500)`

**优先级**：强烈建议

**最佳参考来源**：`UI-TARS SDK`

**来源说明**：
- 上游项目：`UI-TARS SDK`
- 对应能力：默认 operator 支持 `hover`
- 本仓库参考入口：`doc/references/ui-tars-desktop.md`

**为什么选它**：

UI-TARS 把 hover 当成明确动作，而不是 move 的副作用，这很适合我们处理 tooltip、下拉菜单预览、悬停高亮之类场景。

**建议直接借鉴的点**：

- hover 保持独立语义；
- 允许配置悬停时间；
- 与后续截图联动，便于观察悬停后变化。

### 7.3 `open_app(name_or_command)`

**优先级**：强烈建议

**最佳参考来源**：`Windows-Use`

**来源说明**：
- 上游项目：`Windows-Use`
- 对应能力：`app_tool`
- 本仓库参考入口：`doc/references/windows-use.md`

**为什么选它**：

如果没有应用启动能力，很多演示任务都必须靠 shell 或人工预启动，不利于展示 agent 的完整性。`Windows-Use` 的 `app_tool` 最接近我们需要的 Windows 应用管理能力。

**建议直接借鉴的点**：

- 允许按应用名或启动命令打开；
- 与窗口切换能力放在同一 backend adapter 中；
- 对应用未找到、启动失败返回结构化错误。

### 7.4 `switch_app(app_name_or_window_title)`

**优先级**：强烈建议

**最佳参考来源**：`Windows-Use`

**来源说明**：
- 上游项目：`Windows-Use`
- 对应能力：`app_tool`
- 本仓库参考入口：`doc/references/windows-use.md`

**为什么选它**：

多任务或“终端 + GUI”混合流程下，窗口切换是常态。虽然可以靠 `Alt+Tab`，但显式工具更稳定、更可控，也更便于记录。

**建议直接借鉴的点**：

- 优先按窗口标题或应用名切换；
- 失败时说明未找到窗口，而不是悄悄退化为热键。

### 7.5 `focus_window(window_title_or_handle)`

**优先级**：强烈建议

**最佳参考来源**：`Windows-Use`

**来源说明**：
- 上游项目：`Windows-Use`
- 对应能力：`app_tool` + UIA 思路
- 本仓库参考入口：`doc/references/windows-use.md`

**为什么选它**：

`switch_app` 更偏“换到某个应用”，`focus_window` 更偏“确保当前操作对象就是这个窗口”。这对点击前校验尤其重要。

**建议直接借鉴的点**：

- 把窗口聚焦做成显式动作；
- 后续可结合 UIA 检查当前活动窗口标题是否符合预期。

### 7.6 `locate_element(query, screenshot_id=None)`

**优先级**：强烈建议

**最佳参考来源**：`Windows-Use`

**来源说明**：
- 上游项目：`Windows-Use`
- 对应能力：Windows UI Automation 思路
- 本仓库参考入口：`doc/references/windows-use.md`

**为什么选它**：

在我们已引用的项目里，没有哪个项目完全等于“根据描述返回 bbox 的 Windows 工具”。但如果只看“谁最接近我们的 Windows 实际需求”，答案依然是 `Windows-Use`，因为它已经把 **UIA 作为读取界面的核心能力**。这意味着它最适合承担“定位验证通道”的来源。

**我们对这个工具的最终设计不是纯照搬，而是二阶段混合实现**：

1. **视觉定位通道**：根据截图和文本描述找大致区域；
2. **UIA 验证通道**：在 Windows 可获取到控件树时，验证候选目标是否合理。

**为什么这样设计**：

- 纯视觉更通用，但不稳定；
- 纯 UIA 对某些界面覆盖不全；
- 二者结合更符合我们“课设可演示、Windows 优先、先稳后强”的目标。

**建议直接借鉴的点**：

- 借鉴 `Windows-Use` 的 UIA 作为辅助感知层；
- 工具只返回 `bbox`、`confidence` 和解释，不直接点击；
- 让点击动作仍由主 agent 单独决策，保持轨迹清晰。

**我们建议的返回**：

- `bbox`
- `confidence`
- `source`：`vision` / `uia` / `hybrid`
- `reason`

### 7.7 `view_screenshot(screenshot_id)`

**优先级**：强烈建议

**最佳参考来源**：`OSWorld`

**来源说明**：
- 上游项目：`OSWorld`
- 对应能力：保存 screenshots / actions / video，并支持人工检查与 manual examination
- 本仓库参考入口：`doc/references/osworld.md`

**为什么选它**：

OpenAI 的 harness 证明了截图是 observation 的核心，但 `OSWorld` 更像我们要的“**把截图当成证据保存下来，并允许之后回看**”。这与我们的 examiner 机制非常一致。

**建议直接借鉴的点**：

- 截图不要只作为即时观察，还要作为 run artifact；
- 回看工具要能按 `screenshot_id` 读取并附带元信息；
- 为 examiner 和调试保留同一套证据来源。

**我们建议的行为**：

- 参数：`screenshot_id`
- 返回：路径、时间、来源步骤、分辨率、描述

## 8. 跨工具统一约束

### 8.1 吸收 `OpenAI Computer Use` 的约束

所有 GUI 动作都要尽量遵循：

- 先看截图；
- 再出一个原子动作；
- 执行后补新观察；
- 不鼓励一口气合并多步 GUI 推理。

这意味着：

- 工具设计上要偏“小而明确”；
- 主循环中应允许“动作后重新截图”。

### 8.2 吸收 `Anthropic Computer Use` 的约束

所有工具实现都应预留以下能力：

- **action 校验**：参数是否合法、坐标是否越界、窗口是否可见；
- **适度 delay**：避免 GUI 动作连发导致状态错乱；
- **日志记录**：每次工具调用都要可回溯；
- **风险拦截**：对敏感输入、危险命令或可逆性差的操作预留确认位。

### 8.3 吸收 `UI-TARS SDK` 的约束

工具层实现时，应尽量收束到统一 operator：

- `screenshot()`
- `execute(action)`

这样 LangGraph 上层不依赖具体鼠标/键盘/截图库，后续替换底层更容易。

## 9. 推荐实现顺序

### 阶段 A：先把最小闭环跑通

1. `run_command`
2. `take_screenshot`
3. `click`
4. `type_text`
5. `hotkey`
6. `wait`

### 阶段 B：补齐完整基础 GUI 动作

7. `right_click`
8. `double_click`
9. `scroll`
10. `drag`
11. `move_mouse`
12. `hover`

### 阶段 C：增强 Windows 稳定性与证据链

13. `open_app`
14. `switch_app`
15. `focus_window`
16. `view_screenshot`
17. `locate_element`

## 10. 最终落地建议

如果只用一句话总结本文件的结论，就是：

- **底层执行抄 `Windows-Use`**；
- **动作循环抄 `OpenAI Computer Use`**；
- **接口抽象抄 `UI-TARS SDK`**；
- **安全与校验抄 `Anthropic`**；
- **证据回看抄 `OSWorld`**。

对我们最关键的不是“把某个项目整套搬进来”，而是把这些参考对象中**最适合我们课设目标的那一小段工具能力**拆出来，重新组织成统一、可追溯、可替换的工具层。
