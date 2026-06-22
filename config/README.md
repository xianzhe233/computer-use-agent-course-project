# 项目本地模型配置

本目录用于存放本项目后续接入 agent / examiner / GUI grounding 时要读取的模型配置。

## 文件说明

- `models.local.json`：本机实际使用的 provider、base url、api key 与角色分配；包含敏感信息，已在根目录 `.gitignore` 中忽略。
- `models.example.json`：不含密钥的模板文件，用于后续迁移或重建本地配置。

## 当前角色约定

- `locator`：使用 OpenRouter 的 `uitars`，用于 GUI 元素定位。
- `mainAgent`：使用 `mimo-token-plan-cn / mimo-v2.5`，用于主执行 agent。
- `examiner`：使用 `mimo-token-plan-cn / mimo-v2.5`，用于验收 examiner。

## 使用约定

- 后续真正接入代码时，优先把 provider 与角色读取统一指向 `config/models.local.json`。
- 如果接入层需要更细的模型 ID 映射，可在现有结构上追加字段，不要把 key 散落到代码里。
