# 全市场日报

日报覆盖已入库的 A 股市场，按估值观察、热点与催化、长期候选池变化组织内容。
只生成一份完整报告；QQ、微信按平台字数限制分段发送，不再另外生成或发送省流版。
日报解释模型仍由 `FINANCE_DAILY_MODEL` 和 `FINANCE_DAILY_REASONING_EFFORT` 配置，默认 `gpt-6-astra`、`high`。

## 数据和选材

- `config/market_daily.json`：新闻和研报窗口、资料条数、行业分散限额、编辑排序词。
  排序词只影响新闻优先级，不作为行业准入条件。所有行业均参与取材。
- `scripts/pipeline/market_daily_inputs.py`：通过只读 SQL 生成估值筛选、行业研报关注统计和本期研究选题。
- 新闻和公告：按来源分配篇幅；去掉已隔离、模板、解析失败资料；新闻相同标题去重。
  财经相关内容优先，例行章程、会议等材料降低优先级。
- 研报：按行业轮选，避免单一行业占满模型上下文。行业关注度按已收录机构与报告数量计算，不能称为资金流入或涨幅。
- 估值：复用 `value_plan.balanced_consensus` 和 `valuations` 的财年、机构去重、股本口径和同业政策。
  每家机构取截止时间前最新预测，再计算 EPS 中位数与预测 PE；排除明确停牌、ST 或不可交易状态。
  金融行业不套用普通行业 PE 同业估值；来源不足只展示“估值观察”。同业折价不是预期收益。
- 图谱：读取本期跨行业选题及热点行业相关公司的已审核关系，保留来源与知识截止时间。
- 长期候选池：读已有冻结成员、变化、财务依据和研究预测。日报编辑选题不修改池状态，也不代表交易持仓。

所有统计在 Python/SQL 中计算；模型引用冻结的 evidence ID 进行解释。保留源数据可用时间，禁止使用截止时间后的盈利预测和行情修订。
市场覆盖统计只描述本库证券与行情覆盖，不把样本统计称为全市场指数或全部上市公司。

## 输出与兼容

用户可见标题为“全市场盘前日报”。主要内容包括核心判断、市场概览、低估候选与估值观察、新闻催化、行业热点、候选池变化和跟踪条件。
没有依据的栏目不填充模板文字；具体数据缺口集中说明。

为保留历史报告、原调度和发送去重状态，暂时兼容以下内部标识：

- 入口：`scripts/pipeline/daily_ai_hardware_report_v3.py`。
- DB 类型：`ai_hardware_daily_v3` / `ai_hardware_daily_v3_shadow`。
- 路径：`analysis/AI_Hardware/runs_v3`、`daily`、`daily_v3_shadow`。
- 新快照：`schema_version=market_report_snapshot:v4`、`report_scope=all_a_share`。
- 队列正文继续使用原 `daily_report` 幂等键及分段序号，不插入 `daily_brief`。

旧工作区即使保留 `daily_report_brief_enabled` 或 `daily_report_generation`，也不能触发第二次模型调用。
旧发送状态中的 `brief_sent` 仅保留用于历史审计；新发送写为 `false`。部署不得清空已发送日期。

新估值观察不会自动授权买卖建议。已验收的交易计划有独立的批准和投递流程，配置见 `config/daily_value_plan.json`，批准校验见 `scripts/analytics/run_daily_value_plan.py`。

## 验收

在仓库根目录运行 `make test-agent` 和 `make lint`。重点回归：

- `tests.test_market_daily`：跨行业分散、时间截止、盈利预测、同业估值语义、模型输入预算、引用和队列幂等性。
- `MMClaw.tests.test_daily_report_model`：两种渠道只发完整报告、微信主动获取和定时发送、QQ 原异步发送能力。
- `MMClaw.tests.test_qq_daily_dedup`：已发送和已失败任务不重复投递。
- `tests.test_daily_reasoning_contract`：日报模型保持 high 思考预算。

真实模型验收使用不带 `--publish` 的日报命令，检查快照 `selection_receipt`、实际模型回执、`validation.json` 和可读报告。
检查 `generation_mode`、逐条声明校验和主要章节内容；仅退出码为零或来源摘要可读，不代表完整模型验收通过。测试不执行群聊投递。
