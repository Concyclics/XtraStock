# 协作开发

1. 从 `main` 创建 `feature/<topic>` 或 `fix/<topic>` 分支。一项业务变更保持在一个可审阅的 PR。
2. 依据组件目录分工；跨采集、SQL、Agent 的变更在 PR 中列出数据契约影响。修改共享配置或数据库需额外审核。
3. 本地使用个人 `.env` 和开发数据库。不要直接连接他人的生产库做单元测试。
4. 提交前运行 `make test lint doctor deploy-plan`，暂存后运行 `make scan`。CI 无生产凭据，不请求外部模型、不发送聊天消息。
5. 新配置写入 `config/defaults.toml` 或样例，并在 `src/xtrastock/config.py` 建立必要的旧变量映射。禁止在业务模块新增部署地址或读取另一台机器的文件路径。
6. SQL 新增迁移文件，说明前置版本、事务与锁行为、执行后验证和回退方式。不要修改已经在共享环境执行的历史迁移内容。
7. 修复应有针对性的回归测试；涉及 SQL 的集成测试必须明确 opt-in，使用隔离开发库。不得为了通过测试屏蔽真实失败。

`CODEOWNERS` 当前由仓库所有者兜底。团队成员加入后，可按 `apps`、`packages`、`database`、`services` 分配维护者。

`config/development.lock` 固定本次验收的开发依赖；`config/public_hosts.json` 登记源码可包含的公共文档、数据源和上游服务域名。新增主机需在 PR 中说明用途；实际部署的私人主机只填写私有配置，不加入公共域名清单。

运行目录、数据卷、模型缓存、研报与聊天记忆均位于忽略路径。通过安全渠道分发凭据；PR、Issue 和测试日志只写变量名，不写值。

旧 pipeline 和 analytics 尚各有一个 `common.py`。现有回归按模块启动独立进程，避免 Python import 缓存串用。新代码使用完整模块名；后续拆包时逐个删除兼容入口并更新调用方。
