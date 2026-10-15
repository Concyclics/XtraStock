# 数据库代码与迁移

- `postgres/Dockerfile`：PostgreSQL 17、pgvector、TimescaleDB 的开发镜像。
- `postgres/init/`：可用扩展、业务 schema、app / reader 角色和默认权限。密码仅从容器环境读入。
- `postgres/schema_*.sql`：既有业务 schema 定义。
- `postgres/migrate_*.sql`：已有系统演进的迁移，部分依赖已有数据、视图或 Timescale 状态。
- `migrations/`：正文恢复、规则版本、图谱查找、数据目录与证券身份索引失效等后续迁移。

Compose 只创建空库基础设施，不导入生产数据，也不盲目执行历史维护 SQL。`import_*`、压缩/解压和历史修复脚本不是空库初始化序列。

建议先用脱敏的结构备份建立团队开发基线，再逐个应用缺失迁移。首次从零建库，需要按对象依赖审核 `schema_*` 与 `migrate_*` 顺序；本次源码发布未将整个历史 SQL 集合认证为全自动空库安装器。

每项新迁移的 PR 必须说明：前置对象、只读消费者影响、锁和事务行为、校验查询、回退或备份策略。运行 `apps/finagent/scripts/ops/check_repository.py --database` 可只读检查关键数据契约，但它不证明数据完整性。

已审核的迁移可用 `.venv/bin/python -m xtrastock.psql --role admin -f <SQL文件>` 执行；该入口读取统一配置，将密码仅放在子进程环境中，不写入命令参数，遇到 SQL 错误立即返回非零。

Agent 的 reader 角色独立于采集 app 角色，默认事务只读且只授予 SELECT。迁移由 admin 执行。默认角色名与历史 SQL 中的授权保持一致；定制角色名时需同时审阅历史授权语句。
