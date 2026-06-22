# Git / Commit 规则

如果项目尚未启用 git，本文件可先保留，待需要提交与推送时再正式执行。

## 1. 适用范围

1. 任何由 AI 生成、建议或实际执行的 commit message，都应遵循本文档。
2. **没有用户明确指令时，AI 不得执行 `git commit` / `git push`**；只能准备建议消息。
3. 用户只说“提交吧 / commit”而未指定提交信息时，AI 应按本文档自动生成规范标题。
4. 用户给出不规范标题时，AI 应先给出规范化建议；如用户坚持原文，以用户明确要求为准。

## 2. 统一格式

提交标题统一使用：

```text
<type>(<scope>): <summary>
```

要求：

1. `type` 必填，小写英文。
2. `scope` 必填，小写英文。
3. `summary` 用简洁英文描述“做了什么”，不加句号。
4. 标题建议控制在 72 个字符以内。

## 3. 允许的 `type`

| type | 适用场景 |
| --- | --- |
| `feat` | 新增功能 |
| `fix` | 修复 bug 或错误行为 |
| `refactor` | 不改变行为的重构 |
| `perf` | 以性能优化为目标的改动 |
| `docs` | 仅文档改动 |
| `test` | 测试相关改动 |
| `build` | 构建、打包、环境配置、依赖安装链路 |
| `ci` | CI / 自动化流程 |
| `chore` | 杂项维护 |
| `revert` | 回滚已有提交 |

不要使用 `update`、`misc`、`wip`、`refine` 这类自由格式前缀。

## 4. `scope` 选择规则

优先使用当前仓库中真实存在、职责稳定的边界。

| scope | 对应范围 |
| --- | --- |
| `repo` | 根目录、工作区配置、仓库级治理 |
| `docs` | `AGENTS.md`、`doc/` 下的协作与规则文档 |
| `app` | 单一应用主模块 |
| `web` | Web 前端 |
| `api` | 接口定义或服务接口实现 |
| `db` | 数据库设计、迁移或脚本 |
| `ui` | 组件、样式与交互 |
| `scripts` | 自动化脚本 |
| `infra` | 部署、环境、构建或运维配置 |
| `test` | 测试代码与测试资源 |
| `release` | 打包产物与发布流程 |

补充规则：

1. 如果一次改动跨多个子目录，但本质上是一件仓库级工作，优先用 `repo`。
2. 如果只是本地规则文档变动，优先用 `docs`。
3. 如果未来新增稳定模块，且不在表中，可以直接使用真实模块名的小写英文/短横线形式。

## 5. `summary` 写法

推荐写法：

```text
verb + object + outcome
```

例如：

- `docs(docs): add local collaboration rules`
- `build(repo): initialize workspace scripts`
- `fix(api): align request validation`

要求：

1. 用清晰动词开头，如 `add` / `fix` / `refactor` / `optimize` / `remove` / `document`。
2. 优先描述结果，不写过细实现细节。
3. 避免 `some fixes`、`update files`、`misc changes` 这类空泛写法。

## 6. commit body 与验证

默认可以只写单行标题；但遇到以下情况，建议补充 body：

- 改动跨多个模块
- 涉及构建、环境、数据库、打包
- 需要记录验证命令或迁移说明

建议模板：

```text
<type>(<scope>): <summary>

Why:
- brief reason

Validation:
- actual command only
```

要求：

1. `Validation` 里只写实际跑过的命令。
2. 不要伪造验证结果。

## 7. AI 执行流程

1. 先确认用户已经明确要求提交或推送。
2. 先看 `git status`，确认改动范围。
3. 确认这次改动是一件逻辑上完整的事；如果不是，先拆分。
4. 如果本次改动影响本地规则，先同步更新 `AGENTS.md`、`doc/architecture.md`、`doc/progress.md`、`doc/log.md`。
5. 若用户未指定标题，提交前先给出拟用标题。
6. 真正执行 `git commit` 时，标题与 body 必须与实际改动一致。
7. `git push` 必须单独获得用户明确授权。

## 8. 最小结论

未来默认要求：

1. 标题统一使用 `type(scope): summary`。
2. `type` 只用标准集合。
3. `scope` 应落到真实职责边界。
4. AI 未获明确授权时，不能擅自 `commit` 或 `push`。
