# XtraStock

面向金融研究的采集、SQL 知识库、时序图谱、因子研究与多步 Agent 工程。数据采集与分析结果通过数据库契约衔接，Agent 和日报从同一证据层读取。

此仓库包含可协作开发的源码、测试、数据库定义和部署模板。业务数据库、研报原件、聊天记录、私人接口与运行配置由各部署环境独立管理。

## 工程结构

| 目录 | 职责 |
| --- | --- |
| `src/xtrastock/` | 共享配置、部署渲染等基础设施 |
| `apps/finagent/scripts/pipeline/` | 调度、采集、正文恢复、OCR、日报 |
| `apps/finagent/scripts/analytics/` | SQL 数据登记、图谱、因子、候选池、预测评估 |
| `apps/finagent/scripts/search/` | 全文检索、向量队列与索引 |
| `apps/finagent/Spider/` | 东方财富、报刊等数据源适配器 |
| `packages/mmclaw/` | 带金融工具、证据校验和多步执行的 Agent 源码 |
| `services/model_gateway/` | OCR / embedding 按需启动与空闲释放 |
| `database/` | PostgreSQL 基础设施、SQL 定义与增量迁移 |
| `config/` | 无凭据的默认配置和配置样例 |
| `deploy/` | 本地开发 Compose 与 systemd 模板 |
| `tests/`、各组件 `tests/` | 配置、安全边界、业务及生命周期测试 |

## 本地开发

需要 Python 3.11+、Git；数据库开发另需 Docker。推荐 Linux / WSL2，保留 Git 符号链接。

```bash
git clone https://github.com/Concyclics/XtraStock.git
cd XtraStock
make install
make configure
make test
make doctor
```

`make configure` 只在文件不存在时创建 `.env` 和 `config/local.toml`，权限为 `0600`。在本机填写凭据和接口；不要提交这两个文件。完整优先级和字段见 [配置说明](docs/CONFIGURATION.md)。

数据库开发：

```bash
docker compose --env-file .env -f deploy/compose.yaml up -d --build
```

Compose 创建独立命名卷，只绑定回环地址。它初始化扩展和权限角色；业务 SQL 的分阶段初始化及历史迁移边界见 [数据库说明](database/README.md)。不要将历史迁移目录按字母顺序一次性执行。

开发 Agent：先准备开发数据库及所需业务表，配置 API 和只读账号，再执行：

```bash
mkdir -p .local/workspaces/dev
cp config/mmclaw.example.json .local/workspaces/dev/mmclaw.json
.venv/bin/mmclaw -w .local/workspaces/dev
```

示例使用终端交互。QQ / 微信凭据在各自私有 workspace 配置；测试不会发送群消息。

## 验证与协作

```bash
make test-core       # 配置、权限边界、发布检查
make test-agent      # 独立进程运行原业务回归；不连接业务数据库
make test-gateway    # 模拟模型生命周期；不启动 GPU
make lint doctor
make deploy-plan    # 校验部署模板，不安装或启动服务
git add <明确选择的源码文件>
make scan           # 检查 Git 暂存区
```

Pull Request 自动执行相同的离线验收。参阅 [贡献指南](CONTRIBUTING.md)、[架构与维护边界](docs/ARCHITECTURE.md)、[部署说明](deploy/README.md)。

本仓库保留历史业务脚本入口及少量指向仓库内部的兼容链接，以便逐步重构。源码整理不等于全部历史数据补齐，也不代表任何预测模型通过上线质量门槛。

## 许可证

GPL-3.0，见 [LICENSE](LICENSE)。Agent 包含 [CrawlScript/MMClaw](https://github.com/CrawlScript/MMClaw) 的派生代码及保留的版权信息，见 [第三方说明](THIRD_PARTY_NOTICES.md)。
