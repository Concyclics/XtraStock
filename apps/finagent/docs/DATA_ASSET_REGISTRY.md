# FinAgent 分析数据与实验产物登记

适用本机部署。SQL 保存结构化正文、行情、财务、图谱、因子、模型参数、分析结果和任务状态；PDF、OCR 原件及大型实验矩阵保留在用户控制的文件系统，并登记路径和内容版本。不能仅备份 PostgreSQL 而遗漏附件目录。

## 权威入口

维护定义统一位于 `scripts/analytics/data_asset_contracts.py`，生产者和消费者对应关系见 [采集与 Agent 契约](COLLECTION_AND_AGENT_CONTRACT.md)。Agent 使用 `finance_data_status(scope='catalog')` 读取目录，`scope='status'` 读取实际水位；未测量的目录项不能作为缺失或完整的结论。需要仅刷新定义时执行 `research_asset_registry.py --catalog-only`，不会扫描原件或实验大文件。

- `ops.data_asset_registry`：数据集位置、粒度、依赖、使用约束；不要把不同粒度的表或索引数量直接相加。
- `corpus.documents` / `corpus.document_files`：正文及附件。空正文、原件损坏、待 OCR 与质量过滤分别记录。
- `market.canonical_bars_daily_v2`：标准行情；结合交易日历、证券生命周期、公司行动及复权版本。
- `company.financial_facts`：区分实际与预测、单位、来源和口径的财务事实；其覆盖须单独测量。
- `company.financial_metrics`：供应商历史指标，首次公告日期与供应商修订日期不能混用。该表不是 `company.financial_statement_metrics` 的别名。
- `company.financial_statement_metrics`：研报正文抽取的财务指标，不能假定完整覆盖官方历史财报。
- `market.events_raw` 的 `financials_raw` 类别：保留供应商财报原始字段及版本。目录键为 `market.financial_events`，研究面板从这些原始版本核对时点与口径。
- `research.report_forecasts_usable`：通过语义检查的研报预测，区分实际/预测、EPS 元/股、净利润元、预测年份。
- `kg.relation_versions`：同时保留事实有效时间、知识可见时间、证据与审核状态。图谱特征存在不证明完整刷新成功。
- `factor.valid_daily_scores_v1`：消费者优先使用的有效因子视图；退休版本不应进入最新得分缓存。
- `analytics.forecast_models` / `analytics.learning_models_v1`：模型与学习结果；研究状态不等于生产批准。
- `research.experiment_artifacts` / `research.artifact_records`：不可变文件版本、SHA-256、位置、登记时间及小型结果表。
- `ops.dataset_watermarks` / `analytics.dataset_coverage_latest`：结合上游 `ops.crawler_runs` 判断新鲜度；历史待重放区间在 `quant.repair_queue`。

## 实验登记和消费

`finance-research-registry.timer` 每个北京时间整点后的 35 分执行一次。脚本用数据库锁防止重叠；变化中的文件延期处理，已登记版本保留。手动执行：

```sh
.venv/bin/python scripts/analytics/research_asset_registry.py --output logs/research_asset_registry.json
```

登记范围为 `FinAgent/research`。小型 JSON/CSV/TSV 结果可进入 SQL；大型矩阵和其他附件只登记。`historical-source` 原始下载树登记为数据集，通过其 manifest/worklist 定位；不把它误称为已逐文件完成校验。代码备份、符号链接、凭据及隐藏目录不纳入结果载荷。变更判断先比较文件大小和 mtime；未变化文件不代表每小时重新计算过 SHA-256。

Agent 的 `finance_research_intelligence` 支持 `scope=experiments`，并可按项目、年份、持有期、成本、样本范围和模型过滤。返回结果保留实际登记可见时间、研究状态和单位；不向模型暴露本机文件路径或密钥。`eps-semantics-20261015` 的成本 15 表示单边 15 基点，horizon 20 表示 20 个交易日，收益为小数。其他项目没有单位约定时须查原协议，不能沿用这一口径。

项目的 `EXPERIMENT_CONTRACT.json` 也进入 SQL；已提供契约的项目只返回其中指定且内容哈希匹配的结果。单位和逐年训练状态由该契约提供，不能从项目名字猜测。扩大样本的 `eps-expanded-history-20261015` 已单独登记，2021 固定规则诊断与 2022–2025 实际训练必须区分。Agent 结果每页最多 5 行，通过 `experiment_next_offset` 续页，固定原决策截止时间和筛选条件；默认第一页不等于全实验。

日报数据包也包含实验登记摘要。登记不会创建模型生产批准，或绕过预测质量验收。

财务研究通过 `financial_history.py` 分别构建公开日期重建与实际系统观察视图。首次公告、最新更新和源记录日期取较晚者，保守延后一日；系统视图还要求实际入库时间已到。每个北京时间收盘只选最新可见报告期及版本，同一时点的冲突和最新报告缺项保持缺失。毛利率、累计ROE、同比百分数转为小数，报告月份和距期末时间一并保留。供应商更新日期不能代替完整历史版本存档。

业绩报表采集器读取实际返回的 `XSMLL` 毛利率，并保留 `BPS` 每股净资产及 `MGJYXJJE` 每股经营现金流。该报表没有提供资产负债率时不伪造该字段；每股现金流和净资产的股数口径未核实前，不构造现金流资产比或现金流权益比。

## 缺失正文恢复

`backfill_missing_report_bodies.py` 从东方财富原编号 PDF 地址补取空正文，校验报告标题或原编号地址与证券身份，保存原 PDF 和哈希，再原子写入正文及检索队列。失败及待人工核查项留在 `ops.document_body_recovery`。

原生文本提取成功不等于图片/图表完成 OCR；元数据明确记录 `full_visual_ocr_verified=false`。既有正文不会被覆盖；后续空的旧采集记录也不能抹掉已恢复正文。HTTP 404、损坏原件、身份不能确认的附件不得标记为完成。

部分行业研报的网站标题包含 PDF 封面没有的系列前缀。此时只接受原报告编号地址、封面完整长副标题和相差不超过三天的发布日期同时匹配；过短副标题、仅后文提及或日期不符仍留待核查。`--retry-identity` 只重新核查此前的身份失败记录，不将其他失败一并清空。

## 刷新和验收

图谱先执行正常依赖检查，再进行文档事件、候选抽取、概念发现、关系、时态图谱及图特征刷新；因子随后执行图谱依赖检查。日常更新使用有界日期窗口，较早变更保留在历史修复队列。队列存在不等于历史已重放。

时态图谱及每日图特征写入使用同一事务锁；与历史补录竞争时最多排队 180 秒，超时会回滚并明确失败。不会绕过互斥锁，也不会因短暂竞争立即丢弃整条刷新链路。

预聚合状态必须同时满足输出非空、实际有效数据日期足够新、上游刷新成功。不要因目录、表或旧得分存在就宣布更新成功。

实测与修复日志存放在 `FinAgent/research/data-completion-20261015`。模型验收使用本地捕获输出，不向 QQ/微信发送测试消息。
