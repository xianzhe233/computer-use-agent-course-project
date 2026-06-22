# Anthropic Computer Use 参考笔记

## 来源

- 官方总文档：`https://docs.anthropic.com/en/docs/agents-and-tools/computer-use`
- 工具文档：`https://docs.claude.com/en/docs/agents-and-tools/tool-use/computer-use-tool`

## 大致结构

Anthropic 的 computer use 更强调“**工具组合**”而不只是视觉点击本身。常见结构是：

- `computer` tool：做截图、点击、输入、滚动等桌面交互
- `bash` tool：执行命令
- `text editor` tool：编辑文本或文件
- 安全机制：限制高风险操作、强调环境隔离、处理失败和异常状态

它把 computer use 放进一个更完整的 agent 工具箱里，而不是单一 GUI 动作接口。

## 值得借鉴的部分

1. **computer tool + bash + text editor 的组合思路**
   - 很适合映射到我们“terminal 分支 + GUI 分支”的结构。
2. **安全与风险提示**
   - 可借鉴对危险命令、误操作、越权操作的限制思路。
3. **错误处理与重试意识**
   - 对我们设计 reject / rework 流程有帮助。
4. **工具说明风格**
   - 适合参考其工具定义方式来写自己的提示词与 tool schema。

## 对我们项目的帮助

- **MVP 1**：可借鉴 `bash` / `text editor` 类工具的组织方式。
- **MVP 2~4**：可借鉴 GUI 工具的定义和安全注意事项。
- 对 examiner 之外的“执行层约束”也有帮助，因为它提醒我们不能只考虑能不能做，还要考虑怎么防止乱做。

## 可优先改写到我们项目里的点

- 工具列表与工具职责划分
- 工具说明文字和动作 schema
- 错误处理提示
- 高风险动作限制规则

## 参考时的注意事项

- Anthropic 的文档会比较强调通用 agent 安全，不一定直接适配我们的课设简化范围。
- 我们应优先吸收其：
  - 工具组合思想
  - 安全边界
  - 错误恢复逻辑
  而不是机械照搬全部规范。
