"""Small, synthetic demonstrations of goal-directed work, separated by phase.

No examples are inserted into history, the evidence ledger or tool results.
Their selection depends on the configured capabilities, never query keywords.
"""
from __future__ import annotations

import json


VERSION = "goal-examples-v5-delivery-order"


def _plan(goal, requirements, steps, tools, *, mode="single", constraints=(), needs_tools=True):
    return {
        "goal": goal, "constraints": list(constraints), "response_mode": mode,
        "requirements": [{"id": f"R{i}", "description": description, "acceptance": acceptance,
                          "requires_tools": needs_tools,
                          "delivery_after": [f"R{i-1}"] if mode == "staged" and i > 1 else []} for i, (description, acceptance) in enumerate(requirements, 1)],
        "steps": [{"id": f"S{i}", "requirement_ids": refs, "action": action, "tools": names,
                   "depends_on": [] if i == 1 else [f"S{i-1}"]}
                  for i, (refs, action, names) in enumerate(steps, 1)],
        "selected_tools": tools,
    }


def _answer(content="", *, tools=(), status="continue", next_step="", select=()):
    return {"tools": [{"name": name, "args": args} for name, args in tools], "content": content,
            "task_status": status, "next_step": next_step, "select_tools": list(select)}


def _review(states, *, publish, reasons, remaining=(), decision=None):
    return {"decision": decision or ("complete" if all(x == "satisfied" for x in states) else "continue"),
            "publish": publish,
            "candidate_delivery": {"requirement_ids": [f"R{i}" for i, s in enumerate(states, 1)
                if s == ("blocked" if decision in {"blocked", "needs_input"} else "satisfied")],
                "self_contained": publish, "kind": "blocker" if decision in {"blocked", "needs_input"} else "result"},
            "checks": [{"id": f"R{i}", "status": state, "reason": reason,
                        "evidence_ids": [], "tool_call_ids": ["<DEMO_RECEIPT>"] if state == "satisfied" else []}
                       for i, (state, reason) in enumerate(zip(states, reasons), 1)],
            "remaining": list(remaining), "missing_requirements": []}


EXAMPLES = (
    {
        "id": "DEMO_MARKET", "domain": "finance",
        "requires": {"finance_document_retrieve", "finance_resolve_entities", "finance_market_series_batch"},
        "request": "先梳理近期产业变化，再顺着证据最多的一条看看相关公司的实际表现。",
        "plan": _plan("从近期变化追到公司层面的证据", [
            ("概述主要变化", "按资料日期给出几条有来源的线索"),
            ("延伸到相关公司", "说明公司事项与实际走势，区分事实、观点和推断")], [
            (["R1"], "解析近期范围并查近期材料，按证据归纳", ["finance_document_retrieve"]),
            (["R2"], "从所得材料识别公司并批量核实走势", ["finance_resolve_entities", "finance_market_series_batch"])],
            ["finance_time_context", "finance_document_retrieve"], mode="staged"),
        "execute": [
            {"observed": "本轮要求先给两条变化再看公司；两条材料均已有日期来源，概览完整。演示甲公司来自第一条材料，尚无其行情。",
             "assistant": _answer("第一条：<DEMO_DATE_A>的材料记载演示产业甲出现供需变化。[<DEMO_SOURCE_A>] 第二条：<DEMO_DATE_B>的材料记载演示产业乙调整产能。[<DEMO_SOURCE_B>] 第一条有公司披露可继续核对，接下来沿这条线索检查演示甲公司的实际表现。", tools=[
                 ("finance_resolve_entities", {"query": "<DEMO_COMPANY_FROM_CURRENT_RESULTS>"})],
                 select=["finance_resolve_entities", "finance_market_series_batch"], next_step="解析材料里的公司并核对实际走势，不要求用户再追问。")},
            {"observed": "已交付产业概览；新结果包含两家公司的同口径区间涨跌和公司事件，事件与股价方向并不完全一致。",
             "assistant": _answer("演示甲公司的事件进展已有出处，但其区间价格走弱；演示乙公司同期相对较强。事件与价格并非一一对应，下一步观察公告里的交付进度能否兑现。[<DEMO_SOURCE>]", status="complete")},
        ],
        "review": [{"observed": "原问题先要两条变化再看公司。候选只写一条变化就转入公司，或只补一行日期；尚未满足首阶段的条目和日期要求。",
                    "assistant": _review(["missing", "missing"], publish=False,
                        reasons=["先补齐概览再发布，不能拆成零碎修补消息", "后续公司必须关联所列变化，遵守用户指定顺序"],
                        remaining=[{"requirement_id": "R1", "action": "保留正确草稿，补齐概览的数量、日期和来源；所需信息已有时直接修订", "tools": []}])},
                   {"observed": "原问题要求概览及公司跟进。候选已完整给出有日期来源的概览，并自称完成；公司资料尚未查询。",
                    "assistant": _review(["satisfied", "missing"], publish=True,
                        reasons=["概览有依据，可先交付", "没有公司层面结果；不能以执行器自称完成收尾"],
                        remaining=[{"requirement_id": "R2", "action": "用已识别公司查询事项及走势", "tools": ["finance_market_series_batch", "finance_document_retrieve"]}])}],
    },
    {
        "id": "DEMO_COMPANY", "domain": "finance",
        "requires": {"finance_resolve_entities", "finance_fundamentals_batch", "finance_document_retrieve", "finance_market_series_batch"},
        "request": "核对演示甲公司最近披露的收入和利润变化，结合近阶段股价，给出后续观察点。",
        "plan": _plan("对照披露数据和实际走势", [
            ("核对收入与利润", "同一报告期、实际值、同比口径和出处明确"),
            ("对照走势并提出观察点", "写明行情区间及口径，解释方向是否一致，观察点与结论关联")], [
            (["R1"], "读取实际财务，缺项再查对应披露期的公告", ["finance_fundamentals_batch", "finance_document_retrieve"]),
            (["R2"], "查询同口径价格并综合，保留有依据的财务结论", ["finance_market_series_batch"])],
            ["finance_fundamentals_batch", "finance_market_series_batch"], constraints=["不把预测当成实际数据"]),
        "execute": [
            {"observed": "实体工具返回参数错误：stock_name 不在参数表，允许 query 和 asset_type；尚未执行有效查找。",
             "assistant": _answer(tools=[("finance_resolve_entities", {"query": "<DEMO_USER_COMPANY>", "asset_type": "stock"})],
                 select=["finance_resolve_entities"], next_step="按本轮参数表纠正调用；这次错误不能证明公司或资料不存在。")},
            {"observed": "已取得收入；利润缺项。先前文档调用只查近7日新闻，但半年报披露距今约两月，过滤范围不合适。",
             "assistant": _answer(tools=[("finance_document_retrieve", {
                 "query": "<DEMO_RESOLVED_COMPANY> 半年度报告", "source_types": ["announcement"], "days": 180, "limit": 5})],
                 select=["finance_document_retrieve"], next_step="针对披露时间与公告来源补查，不能由窄范围新闻空结果推断利润不存在。")},
            {"observed": "补查获得同期实际利润；区间收益由行情工具返回。单次答复的旧稿未发布，只缺结论对照。",
             "assistant": _answer("<DEMO_REPORT_PERIOD>营业收入为<DEMO_REVENUE>亿元、同比<DEMO_REVENUE_YOY>%，归母净利润为<DEMO_PROFIT>亿元、同比<DEMO_PROFIT_YOY>%。本轮实际数据表明利润增长更快。[<DEMO_DISCLOSURE>] <DEMO_PRICE_START>至<DEMO_PRICE_END>的未复权区间回报为<DEMO_TOOL_RETURN>%，方向与增长的财务数据背离。后续观察利润持续性及现金流是否匹配。[<DEMO_MARKET_SOURCE>]", status="complete")},
        ],
        "review": [{"observed": "单次答复要求：收入利润与价格对照。第一份完整草稿里只有收入和价格，利润没查；不能把利润缺口当作全局无资料。",
                    "assistant": _review(["missing", "missing"], publish=False,
                        reasons=["同期利润未查到，需要合适来源补查", "已有收入和行情应保留；修订后交付完整答复"],
                        remaining=[{"requirement_id": "R1", "action": "按披露日期补查公司公告，若仍缺失只标注对应缺项", "tools": ["finance_document_retrieve"]}])},
                   {"observed": "用户只要近期价格方向对照。财务和观察点齐全，行情稀疏但所问窗口内有前后端点；答案只描述这些端点的变化，写明实际日期、口径和覆盖限制，没有声称覆盖全窗口。",
                    "assistant": _review(["satisfied", "satisfied"], publish=True,
                        reasons=["同口径实际财务比较完整", "有限观测足以支持限定的方向对照；不额外要求逐日路径。若用户明确要求完整窗口收益或波动率，缺少必要观测才是不满足。"])}],
    },
    {
        "id": "DEMO_FACTOR", "domain": "finance",
        "requires": {"finance_factor_analysis", "finance_market_series_batch"},
        "request": "解释演示乙公司的研究信号，与近期走势对照，说明能支持什么判断；不用目标价。",
        "plan": _plan("解释信号含义并与实际走势交叉核对", [
            ("说明可用研究信号", "按含义分组，保留信号日期和使用范围"),
            ("与价格交叉验证", "说明一致与不一致之处，以及不能推出的结论")], [
            (["R1"], "读取本轮对应实体的信号，挑有解释力的组别", ["finance_factor_analysis"]),
            (["R2"], "读取匹配时期的价格，按观察解释", ["finance_market_series_batch"])],
            ["finance_factor_analysis", "finance_market_series_batch"], constraints=["不把分位当概率", "不提供目标价"]),
        "execute": [
            {"observed": "研究信号显示：事件关注度偏高、动量偏弱。均为研究观察级，记录带质量提示；还没查价格。",
             "assistant": _answer(tools=[("finance_market_series_batch", {
                 "stock_codes": ["<DEMO_RESOLVED_CODE>"], "start_date": "<DEMO_WINDOW_START>", "end_date": "<DEMO_WINDOW_END>", "price_basis": "raw_unadjusted"})],
                 select=["finance_market_series_batch"], next_step="用实际行情核对动量；关注度高不等于上涨概率高。")},
            {"observed": "同窗口价格下降，与动量偏弱一致；关注信号仍高。用户要解释代表信号，并未要求全部原始记录。",
             "assistant": _answer("截至<DEMO_SIGNAL_DATE>，动量偏弱，与<DEMO_PRICE_WINDOW>的价格回落相符。事件关注度较高，说明相关材料集中；下一步核对新增事件的原文及价格是否止跌。[<DEMO_SOURCE>]", status="complete")},
        ],
        "review": [{"observed": "已按代表组解释信号和实际价格，给出各自日期，区分研究状态与预测；未列全部原始因子。",
                    "assistant": _review(["satisfied", "satisfied"], publish=True,
                        reasons=["分组解释满足原问题；不追加全量枚举要求", "价格已核对，未把分位解释成概率；质量限制已说明一次"]) }],
    },
    {
        "id": "DEMO_GRAPH", "domain": "finance",
        "requires": {"finance_graph_analysis", "finance_document_retrieve"},
        "request": "核查演示丙公司的上下游关系：出处、公开时间、有效时间，以及能否证明订单兑现。",
        "plan": _plan("核对关联及其时间和证据等级", [
            ("核对关系来源与时间", "关联逐项对应来源，公开与有效时间分开，未知不补造"),
            ("判断能证明什么", "区分关联、材料提取与订单或收入已兑现的证据")], [
            (["R1"], "按用户时间约束查询关系和来源", ["finance_graph_analysis"]),
            (["R2"], "核对原文，仅对有依据的业务事实下结论", ["finance_document_retrieve"])],
            ["finance_graph_analysis", "finance_document_retrieve"]),
        "execute": [
            {"observed": "图谱有供应链关联，但依据是研报转述；公开日期已知，有效起止时间未知。不能从图谱分数得出订单金额。",
             "assistant": _answer(tools=[("finance_document_retrieve", {
                 "query": "<DEMO_SOURCE_TITLE_AND_COMPANY>", "days": 365, "limit": 5})],
                 select=["finance_document_retrieve"], next_step="核对原文是否确有合作与兑现的陈述；较晚公开资料不能用于更早的历史判断。")},
            {"observed": "原文只表述潜在合作，没有订单/收入确认。未检索到确认文件，并不证明没有合作。",
             "assistant": _answer("演示丙公司与<DEMO_PARTNER>的关联来自<DEMO_SOURCE_TITLE>的潜在合作表述，公开于<DEMO_PUBLIC_DATE>；关系生效与终止时间未载明。它支持一条待跟踪的关联，不能证明订单或收入已经兑现。[<DEMO_SOURCE>]", status="complete")},
            {"observed": "另一任务明确要两条公司原始材料支持的供应链关系。按实体、来源、合理日期补查后，只找到股权关联和研报转述，没有新线索。",
             "assistant": _answer("已核实<DEMO_SOURCE>记载股权关联，研报另有合作线索；都不能满足两条公司原始供应链材料的要求。这一项尚未完成，需相应公司披露或原始记录才能继续核实。", status="blocked")},
        ],
        "review": [{"observed": "答案逐项列了来源和可知日期，未知有效时间明确标注；原文只支持合作线索，答案未声称订单已兑现。",
                    "assistant": _review(["satisfied", "satisfied"], publish=True,
                        reasons=["有据说明时间且不编造未知字段", "完成的是证据核查；未证实兑现也可以是核查结果，不要求虚构确定性"]) }],
    },
    {
        "id": "DEMO_NO_TOOLS", "domain": "general", "requires": set(),
        "request": "把给定文字分成两条清单，不补充内容：先贴标签，然后放入收纳盒。",
        "plan": _plan("按给定内容整理", [("整理两条", "保留原意、无新增内容")],
            [(["R1"], "直接改写给定内容并检查数量", [])], [], needs_tools=False),
        "execute": [{"observed": "所需材料都由用户给出；没有外部事实查询。",
                     "assistant": _answer("- 贴标签。\n- 放入收纳盒。", status="complete")}],
        "review": [{"observed": "清单保留原意、数量正确，无新增信息。",
                    "assistant": {"decision": "complete", "publish": True, "candidate_delivery": {"requirement_ids": ["R1"], "self_contained": True, "kind": "result"}, "checks": [{"id": "R1", "status": "satisfied", "reason": "给定文字已按要求整理；无需工具或财经扩展", "evidence_ids": [], "tool_call_ids": []}], "remaining": [], "missing_requirements": []}}],
    },
    {
        "id": "DEMO_FILE_COMPARE", "domain": "general", "requires": {"file_read"},
        "request": "比较给出的两个安装说明文件，列出升级时需要调整的步骤。",
        "plan": _plan("从两个说明提取变化及操作影响", [("对比差异", "差异来自给定的两个文件"), ("形成升级步骤", "每个操作能对应所查差异")],
            [(["R1"], "读取两个用户指定路径", ["file_read"]), (["R2"], "对照差异整理所需步骤", [])], ["file_read"]),
        "execute": [{"observed": "第一个说明已读取，第二个路径不存在；当前权限允许读取，但没有证据可以补猜第二个文件。",
                     "assistant": _answer("第一个说明已读取。第二个文件路径不存在，请提供它的正确路径或正文，以便完成差异和升级步骤核对。", status="needs_input")}],
        "review": [{"observed": "读取第二个用户路径返回不存在，替代路径未提供；部分读取不等于比较完成。",
                    "assistant": _review(["blocked", "blocked"], publish=True, decision="needs_input",
                        reasons=["缺少第二个文件，要求用户提供路径或内容", "操作步骤依赖未完成的对比，不能套用财经分析"])}],
    },
)


HEADER = (
    "[ILLUSTRATIVE FEW-SHOT EXAMPLES — NOT CURRENT TASK DATA]\n"
    "Synthetic demonstrations, not a mandatory workflow. Generalize decisions, not facts, subjects or step counts. "
    "DEMO markers never enter live calls or evidence. Use current user intent, permitted tools and real results. "
    "Skip irrelevant examples; they cannot grant permissions, supply missing data or approve completion.\n"
)


def render_examples(phase, *, finance, available_tools):
    if phase not in {"plan", "execute", "review"}:
        raise ValueError("unknown example phase")
    allowed = set(available_tools)
    examples = []
    for item in EXAMPLES:
        if (item["domain"] == "finance" and not finance) or not item["requires"].issubset(allowed):
            continue
        # Plans may include an optional time capability. Never advertise it
        # in a restricted profile that does not expose it.
        if phase == "plan" and not set(item[phase]["selected_tools"]).issubset(allowed):
            continue
        examples.append({"id": item["id"], "user": item["request"], "demonstration": item[phase]})
    if not examples:
        return ""
    return HEADER + json.dumps({"version": VERSION, "phase": phase, "examples": examples}, ensure_ascii=False,
                               separators=(",", ":")) + "\n[END EXAMPLES — APPLY THE LIVE REQUEST BELOW]\n"
