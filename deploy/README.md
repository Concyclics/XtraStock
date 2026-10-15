# 部署

1. 按根目录说明安装代码，填写私有 `.env` 和 `config/local.toml`。
2. 核对数据库迁移与数据准备，设置模型目录、vLLM 路径和 GPU 编号。vLLM 环境单独安装，普通开发依赖不启动模型。
3. `make deploy-plan` 只校验模板。`python tools/render_systemd.py` 将文件写入忽略的 `.local/systemd/`。
4. 检查渲染结果、`ExecStart`、环境文件、工作区及 GPU 编号；然后用 `apps/finagent/scripts/ops/deploy_finagent.py` 查看安装计划。
5. 只有显式 `--apply` 才安装用户服务；`--activate` 才启用调度，`--restart` 必须显式列出服务。该入口会先检查必需数据库对象并保存原服务备份。

新开发仓库默认不启用任何采集、外部 API 请求或消息推送。QQ / 微信只有配置相应私有 workspace 并显式启用时才运行。

systemd 模板保留既有调度规则；使用 `Asia/Shanghai`。路径包含空白、百分号或引号时渲染器会拒绝，避免不正确的 systemd 参数分词。

`compose.yaml` 的 PostgreSQL / Meilisearch 只发布回环端口，数据位于项目命名卷。更改密码不等于变更已存在数据库卷中的角色密码；需要单独轮换。
