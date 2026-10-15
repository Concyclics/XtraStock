# 配置与凭据

所有新入口通过 `xtrastock.config.Settings` 读取同一套配置。优先级：进程环境变量 > 根目录 `.env` > `config/local.toml` > `config/defaults.toml`。

可用 `XTRASTOCK_CONFIG`、`XTRASTOCK_ENV_FILE` 指向仓库外的私有文件。指定文件不存在会直接报错，不会悄悄采用另一台机器的设置。`.env` 使用 dotenv 引号规则，不展开其中的环境变量引用。

| 配置组 | 内容 | 常用环境变量 |
| --- | --- | --- |
| `database` | 地址、端口、数据库、SSL、管理员/写入/只读角色 | `FINANCE_PGHOST`、`FINANCE_PGPORT`、`PGSSLMODE` |
| 数据库凭据 | 角色密码，仅本地填写 | `POSTGRES_PASSWORD`、`APP_DB_PASSWORD`、`AI_READONLY_DB_PASSWORD` |
| `api` | Agent / 上游兼容接口与模型 | `FINANCE_API_BASE_URL`、`FINANCE_API_KEY`、`FINANCE_AGENT_V3_MODEL` |
| `daily` | 日报模型和思考预算 | `FINANCE_DAILY_MODEL`、`FINANCE_DAILY_REASONING_EFFORT` |
| `embedding` | 接口、模型、向量维度、鉴权变量名 | `FINANCE_EMBEDDING_BASE_URL`、`FINANCE_EMBEDDING_MODEL`、`FINANCE_EMBEDDING_DIMENSIONS` |
| `ocr`、`gateway` | 按需网关与两个本地模型后端 | `FINAGENT_GATEWAY_KEY`、`FINAGENT_OCR_BACKEND_URL`、`FINAGENT_EMBEDDING_BACKEND_URL` |
| `search` | 检索地址、索引名、密钥 | `MEILI_URL`、`MEILI_INDEX`、`MEILI_MASTER_KEY` |
| `paths` | 代码根、私有状态、工作区、研究数据、模型目录 | 在本地 TOML 中配置 |
| `deployment` | vLLM 可执行文件、OCR 与 embedding GPU 编号 | 在本地 TOML 中配置 |

旧入口的环境变量名称由统一映射维护。SQL 写入模块使用 app 角色，迁移使用 admin 角色，Agent 查询只使用 reader 角色；writer/admin 的通用环境变量不能覆盖 reader 身份。未填写密码时在连接前失败。

默认远端 API 是 `.invalid` 占位域名。Agent 默认模型为 `gpt-6-luna`，日报为 `gpt-6-astra` / `high`；接入方须提供兼容这些模型名的接口或调整配置。OCR 和向量的网关密钥与上游模型密钥分开，避免把上游凭据发给本地模型服务。

开发 Agent 样例选择 `openai_compatible_xtrastock` 引擎。加载时从统一配置覆盖接口、模型和密钥，仅保留在内存中；原始 workspace JSON 不写入共享配置的密钥。其他自定义引擎保持原有配置。

API / DB 改址只修改本地文件。修改向量模型或维度还需对应 SQL schema、模型状态与回填，不属于仅改一个地址的操作。

旧 source-cache 和部分报告输出仍使用 `apps/finagent` 下的相对目录，这些目录已忽略。结构化分析数据保存在 PostgreSQL，原件与大型文件只登记元数据和校验信息。不要将整套工作目录作为 Git 提交。

Python 依赖统一在根目录 `pyproject.toml` 声明，以 `config/development.lock` 约束验收版本。Agent 的上游包依赖保留在其 `setup.py`，安装时使用同一份约束；不再维护多份互相冲突的生产环境冻结清单。
