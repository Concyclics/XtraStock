# 采集、SQL 与 Agent 的共同契约

## 数据流

研报和新闻采集保留来源原件与本地缓存；`incremental_import.py` 按源记录及附件版本检查变化，在同一事务内写业务表和 `ops.source_import_versions`。失败批次整体回滚，已恢复正文不能被后续空采集记录覆盖。源文件与数据库行的粒度不同，数量不能直接相减认定遗漏。

公告目录、正文恢复、OCR、全文索引、向量是独立阶段。正文恢复必须核对原编号/标题/证券身份和发布日期，保存原件哈希；原件不存在、身份冲突、解析失败保留明确状态。原生文本成功不等于图片已完整 OCR。检索和向量消费必须匹配当前正文版本；旧向量不能合并计入当前模型覆盖率。

| 输入/派生 | SQL 消费入口 | Agent 方法 |
|---|---|---|
| 数据定义及使用限制 | `ops.data_asset_registry` | `finance_data_status(scope='catalog')` |
| 时点、质量、覆盖状态 | `ops.dataset_watermarks` | `finance_data_status(scope='status')` |
| 价格、复权与生命周期 | Canonical V2 + 对应版本 | `finance_market_series_batch`、`finance_market_screen` |
| 实际财务和预测事实 | `company.financial_facts`、规范研报/合格预测视图 | `finance_fundamentals_batch` |
| 正文与附件 | `corpus.documents`、`corpus.document_files` | `finance_document_retrieve` |
| 当前有效因子 | `factor.valid_daily_scores_v1` + 验证记录 | `finance_factor_analysis` |
| 时态关系、证据和审核 | `kg.relation_versions` | `finance_graph_analysis` |
| 候选池、研究关注、学习 | 共享版本化研究表 | `finance_research_intelligence` |
| 历史实验结果及解释契约 | `research.artifact_records` + 文件版本登记 | `finance_research_intelligence(scope='experiments')` |

目录中的依赖和使用契约决定工具如何组合，不依赖硬编码的“已有六张表”清单。目录不返回私有文件路径；需要原件时通过已授权文档接口读取。各数据集覆盖分母不同，不把覆盖率平均成整体准确率。没有直接水位的数据集标为 `not_measured`。

目录每页最多 10 项，以 `catalog_next_offset` 在同一截止时点续页，避免工具输出预算省略后半段定义。证券身份映射变更通过 `sql/research_identity_search_invalidation_v1.sql` 同事务触发索引重新入队；正在处理的租约保留并递增版本。索引版本取正文、事件及身份可见时间的最大值，保留原始证券代码。

## 实验结果查询

项目可保存 `EXPERIMENT_CONTRACT.json`，由同一登记器入 SQL。它指定结果文件及内容 SHA-256、单位、样本维度、逐年训练说明和门槛状态。Agent 与日报读取 SQL 解释契约；源结果改动而未更新契约时，不能继续作为已指定的实验结果消费。

先选择项目，再按 `year/cohort/horizon/cost/model` 匹配比较；Agent 返回每页上限 5 行，用 `experiment_next_offset` 续页并固定 `decision_cutoff`。分页和省略不等于完整实验只有这些行。未知项目没有契约时单位保持未知，不能猜成百分比。

`eps-expanded-history-20261015` 中，单边成本 15 是 15 基点、horizon 20 是 20 个交易日，收益和回撤为小数。2021 年训练为空，只能解释为固定规则诊断；2022–2025 有实际训练，2023–2025 已被此前研究观察。登记和回测结果均不修改生产批准，新策略仍未通过门槛。

## 变更后的验收

- 原件/源版本 → 入库正文及事实 → 检索/图谱 → Agent 返回，逐层验证，不能用目录存在代替内容验证。
- 本次 SQL 工具回归检查真实数据；测试不向 QQ/微信发送消息。
- 更新 Agent 的 schema、方法签名、子进程读取及任务提示须同步。子进程代码下次调用生效；进程内 schema/prompt 变更需在验收后重新加载 QQ。
- 日报和 Agent 共用 SQL 读取与实验契约；日常生成使用 `gpt-6-astra + high`，本次仓库验收不重新发送日报。
