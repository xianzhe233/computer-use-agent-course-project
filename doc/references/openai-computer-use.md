# OpenAI Computer Use 参考笔记

## 来源

- 官方文档：`https://developers.openai.com/api/docs/guides/tools-computer-use`

## 大致结构

OpenAI 的 computer use 文档核心不是“一个会点鼠标的大模型”，而是一个 **harness**：

1. 给模型当前截图和任务目标。
2. 模型输出下一步 GUI 动作。
3. 外部执行器真正执行动作。
4. 再截图，把新观察结果回传给模型。
5. 循环直到模型认为结束。

也就是说，重点在于：

- screenshot 驱动
- action/observation 循环
- 模型只负责决策
- 环境层负责真正操作和记录

## 值得借鉴的部分

1. **agent loop 设计**
   - 很适合直接对应我们项目中的 main agent 主循环。
2. **screenshot → action → observation 思路**
   - 与我们的截图证据链设计天然一致。
3. **harness 与模型解耦**
   - 我们也应让 LangGraph 管流程，工具层负责执行，而不是把逻辑都塞进 prompt。
4. **动作结构化**
   - 适合参考它的动作协议思路来定义我们的 `click / type / hotkey / finish_request`。

## 对我们项目的帮助

- **MVP 1~4 都有帮助**，尤其是主循环和执行框架。
- 能帮助我们把项目写成“模型负责决策，系统负责执行与记录”的清晰结构。
- 对 `take_screenshot` 的必要性提供了很强的理论和工程依据。

## 可优先改写到我们项目里的点

- 主循环状态机
- GUI 动作协议
- 截图驱动的 observation 机制
- 执行动作后再观察的流程约束

## 参考时的注意事项

- OpenAI 文档更偏能力接口和 harness 思想，不会替你完成 Windows 细节适配。
- 我们需要在它的基础上再补：
  - Windows 工具实现
  - examiner 验收闭环
  - 历史截图回看和证据链存储
