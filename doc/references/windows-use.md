# Windows-Use 参考笔记

## 来源

- GitHub：`https://github.com/CursorTouch/Windows-Use`

## 大致结构

Windows-Use 的重点不是 benchmark，也不是纯论文式框架，而是把 **Windows 本地操作能力** 封装给 agent 使用。它值得关注的结构主要包括：

- Windows UI Automation 相关能力
- PowerShell / 系统命令能力
- 文件读写能力
- GUI 操作封装
- LLM 决策与本地执行的对接方式

它更贴近“怎么在 Windows 上把 agent 真跑起来”。

## 值得借鉴的部分

1. **Windows UIA 思路**
   - 如果纯视觉定位不稳定，可参考 UI Automation 辅助定位或验证。
2. **PowerShell / 文件工具封装**
   - 可直接对应我们的 terminal agent 和后续文件操作需求。
3. **GUI 操作封装方式**
   - 可借鉴点击、输入、读取界面信息等工具的接口组织。
4. **本地环境执行细节**
   - 对处理 Windows 权限、焦点、路径、命令习惯很有帮助。

## 对我们项目的帮助

- **MVP 1**：可借鉴 PowerShell 与本地命令执行方式。
- **MVP 2**：可借鉴 GUI 操作接口设计。
- **MVP 3**：可借鉴 UIA 作为视觉定位的补充验证思路。
- 它对我们“Windows 优先”的技术路线特别关键。

## 可优先改写到我们项目里的点

- Windows 专用工具抽象
- PowerShell 执行约定
- GUI 动作封装层
- UIA 作为辅助感知层的设计思路

## 参考时的注意事项

- 要区分“适合直接借鉴的接口设计”和“未必适合直接照搬的具体实现”。
- 我们项目仍以自己的动作协议和 LangGraph 主循环为主，不把外部实现直接塞进主体逻辑。
