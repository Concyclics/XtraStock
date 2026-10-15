#!/usr/bin/env python3
"""Live research replay with a capture-only connector; never sends chat messages."""
from __future__ import annotations

from xtrastock.config import bootstrap_environment
bootstrap_environment()


import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "scripts/pipeline"), str(ROOT / "MMClaw")]
from common import load_env
from mmclaw.config import ConfigManager, set_workspace
from mmclaw.kernel import MMClaw

CASES = {
    "experiments": "查阅登记入库的 eps-semantics-20261015 离线实验结果：比较2023、2024、2025年，全样本（all）、持有20个交易日、单边15基点成本下，价格基线、估值组合、估值加EPS修正和等权基线的扣费后收益。给出跨年比较与是否已获准用于预测的结论。不要混用5日结果、其他成本或其他样本。",
    "forecast_trend": "从自动候选池挑两家公司，结合已披露财务和近期价格，分析未来一周到一个月值得跟踪的走势驱动，给出能验证或推翻判断的条件。先核对预测模型的基线比较、滚动回测和区间覆盖，说明哪些判断有证据支持。不要用候选分数冒充预期收益。",
    "candidate_pool": "核对我们的自动候选池：现在探索、持续观察和重点跟踪各有多少只？挑两家公司解释长期财务论点和短期走势评分、进入或保留原因；说明每天发现、每周重排淘汰、每月复核如何运作，以及5日和20日预测目前是否有合格结果、历史挖掘的最短训练前缀能否调整。",
    "attention": "我们现在最值得继续研究的方向是什么？从自动关注更新中挑两条有来源的线索，说明为什么关注、哪些还只是观点，以及接下来应核查什么。不要当成买入推荐。",
    "learning": "我们的预测模型最近实际学到了什么？请核对最新训练和成熟结果复盘，比较模型与简单基线的表现，说明现在能否用于预测，以及下一步应该补什么。不要猜测收益。",
    "headlines": "最近有哪些值得关注的市场热点？",
    "valuation": "请分析食品饮料、电力、玻璃玻纤、煤炭板块的估值与走势，比较有代表性的公司。",
    "brief": "只用一句话告诉我现在的北京时间，不需要扩展分析。",
    "company": "Compare 中际旭创最近已披露的营收和归母净利润变化，结合近期价格表现说明两者是否一致，并列出后续需要观察什么。用中文回答。",
    "company_disclosed": "Compare 中国巨石最近已披露的营收和归母净利润变化，结合近期价格表现说明两者是否一致，并列出后续需要观察什么。用中文回答。",
    "fact_check": "有说法称“中国巨石2026年上半年收入和归母净利润都下降了”。请用已披露数据判断，并说明二者各自变化。不需要选股建议。",
    "factor": "看看300308目前有哪些可用的量化研究信号，结合近期价格表现解释这些信号能说明什么、不能说明什么；不要给目标价。",
    "graph": "从中际旭创出发，找出资料中有依据的上游或下游关联，说明每项关联的出处、日期，以及能否证明业务已经兑现。不要扩展成选股建议。",
    "paraphrase": "这几天市场在围绕哪些事情变化？先把主要线索说清楚，再顺着最有依据的一条看看具体公司发生了什么。",
    "rewrite": "把下面的交接步骤整理成简洁检查清单，只根据给出的文字，不检索任何资料，也不要增加建议：先保存当前配置；再确认备份文件可以读取；随后由管理员实施变更；最后记录验收结果。",
    "transfer_market": "Start with two recent market developments, then follow the best-supported one through to a listed company's dated news or disclosure and observed price change. Answer in Chinese and distinguish reported claims from confirmed company disclosures.",
    "transfer_company": "中国巨石最近半年报：营收与归母净利润是同步变化吗？接着对照最近一个月价格方向，说明哪里一致、哪里背离，最后给两个可验证的后续观察点。不要预测目标价。",
    "transfer_factor": "请找300308有代表性的价格、事件或关联研究信号，交代信号日期，与可用的近期走势交叉验证，再指出还不能回答的问题。不要把分位数当上涨概率，也不需要目标价或罗列全部原始记录。",
    "transfer_graph": "帮我给中际旭创做一次关联核查：找两条有原始材料支撑的供应链关系，核对每条的公开时间、关系有效时间和信息类型。区分提到合作或关联与已确认订单或收入，没有依据的时间不要补写。",
    "transfer_graph_evidence": "把中际旭创的关联资料整理成一份核查小结：挑选有出处的关联，分别说明是谁在何时披露、描述什么关系、能够支持什么结论，哪些关系时间或业务兑现仍无法确认。研报或媒体材料请按对应类型标明。",
    "transfer_edit": "仅把句子“保存发布包，核对校验和，随后在测试环境安装”改成三个步骤。不要查询外部资料，也不要补充其他内容。",
    "transfer_constraints": "已知：任务需要数据源和预算；目前两项都没提供。请只列出继续规划前应向用户确认的这两项，不要自行设定数值，也不要检索。",
}

TRANSFER_KINDS = {"transfer_market": "paraphrase", "transfer_company": "company_disclosed",
                  "transfer_factor": "factor", "transfer_graph": "graph", "transfer_graph_evidence": "graph"}


def delivery_checks(traces):
    """Check each actual publication against the immediately preceding review."""
    requirements, delivered, review, errors = {}, set(), None, []
    for row in traces:
        event = row.get("event")
        if event == "goal_plan":
            requirements = {r["id"]: r for r in row.get("requirements", [])}
        elif event == "goal_review":
            requirements.update({r["id"]: r for r in row.get("requirements", [])})
            review = row.get("review", {})
        elif event == "research_stage" and row.get("completion_status") == "complete" and row.get("task_status") == "continue":
            errors.append("Turn ended despite executor continuation commitment")
        elif event == "answer_presentation":
            if not review or not review.get("publish"):
                errors.append("Publication without a passing review")
                continue
            delivery = row.get("delivery") or review.get("candidate_delivery")
            if delivery:
                ids = delivery.get("requirement_ids", [])
                if delivery.get("self_contained") is not True:
                    errors.append("Non-self-contained stage")
            else:
                # Historic captures lack the new delivery contract. They still
                # cannot pass a stage with no newly satisfied requirement.
                ids = [k for k, r in requirements.items() if r.get("status") == "satisfied" and k not in delivered]
            fresh = set(ids) - delivered
            if not fresh:
                errors.append("Stage completes no new requirement")
            for key in ids:
                r = requirements.get(key, {})
                if r.get("status") != "satisfied":
                    errors.append("Unfinished requirement was published: " + key)
                if not set(r.get("delivery_after", [])).issubset(delivered):
                    errors.append("Requested delivery order violated: " + key)
                delivered.add(key)
    return {"passed": not errors, "errors": errors, "delivered_requirement_ids": sorted(delivered)}


def graph_capability_check(observations):
    checked, usable, errors = 0, 0, []
    for observation in observations or []:
        if observation.get("tool") != "finance_graph_analysis":
            continue
        checked += 1
        try:
            payload = observation.get("result", {})
            if isinstance(payload, str):
                payload = json.loads(payload)
            meta = payload.get("meta", {})
            for row in payload.get("rows", []):
                if (row.get("review_status") in {"approved", "classified"}
                    and row.get("source_document_id") and row.get("evidence_text")
                    and row.get("claim_class") in {"observed_fact", "extracted_claim", "model_inference"}):
                    usable += 1
            if meta.get("status") in {"error", "invalid_request", "failed"}:
                errors.append(meta.get("status"))
        except (ValueError, TypeError, AttributeError):
            errors.append("invalid graph result")
    return {"tested": bool(checked), "passed": bool(checked and usable and not errors),
            "calls": checked, "usable_relations": usable, "errors": errors}


def read_observations(path):
    path = path / "tool-results.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def evaluate_capture(case, messages, traces, observations=None):
    """Evaluate delivered content and goal checks; intermediate retries are retained."""
    bad = [m for m in messages if re.search(r"未通过证据校验|暂时无法提供可靠结论|可稍后重试|本次请求未完成", m)]
    calls = [x for x in traces if x["event"] == "tool_call" and x.get("executed")]
    substantive = any(x.get("tool") in {"finance_market_screen", "finance_market_series_batch"} for x in calls)
    stage_lengths = [len(m.split("参考来源",1)[0].strip()) for m in messages]
    meaningful_stages = all(n >= 180 for n in stage_lengths)
    repairs_passed = all(x.get("passed") for x in traces if x["event"] == "answer_repair")
    checks=[x for x in traces if x['event']=='answer_validation']
    published_valid=bool(checks) and all(x.get('passed') for x in checks)
    internal_ids = any(re.search(r"\b[0-9a-fA-F-]{32,36}:E\d{4}\b|\bE\d{4}\b", m) for m in messages)
    passed = bool(messages and not bad and not internal_ids
                  and not any(x["event"] == "turn_error" for x in traces))
    endings=[x for x in traces if x['event']=='turn_end']
    end=endings[-1] if endings else {}
    goal_complete=end.get('completion_status')=='complete' and end.get('requirements_total',0)>0 and end.get('requirements_satisfied')==end.get('requirements_total')
    if "delivered_requirement_ids" in end:
        goal_complete = goal_complete and len(set(end["delivered_requirement_ids"])) == end.get("requirements_total")
    tool_names={x['tool'] for x in calls}
    kind = TRANSFER_KINDS.get(case, case)
    if case == 'experiments':
        experiment_rows=[]
        for observation in observations or []:
            if observation.get('tool') != 'finance_research_intelligence':
                continue
            payload=observation.get('result',{})
            if isinstance(payload,str):
                try:payload=json.loads(payload)
                except ValueError:continue
            experiment_rows.extend(row for row in payload.get('rows',[]) if row.get('component')=='offline_experiment_result')
        matched=[r for r in experiment_rows if str(r.get('metrics',{}).get('horizon'))=='20'
                 and str(r.get('metrics',{}).get('cohort'))=='all'
                 and str(r.get('metrics',{}).get('cost')) in ('15','15.0')]
        passed=passed and {str(r['metrics'].get('year')) for r in matched}>={'2023','2024','2025'}
        passed=passed and all(r.get('prediction_eligible') is False for r in matched)
    demo_leak = any(re.search(r"DEMO_|演示[甲乙丙]公司|贴标签|收纳盒", m) for m in messages)
    if kind in {"brief","rewrite","transfer_edit","transfer_constraints"}:
        passed = passed and len(messages) == 1
        if case=='rewrite':passed=passed and not calls and all(s in messages[0] for s in ('配置','备份','管理员','验收'))
        if case=='transfer_edit':passed=passed and not calls and all(s in messages[0] for s in ('发布包','校验和','测试环境'))
        if case=='transfer_constraints':passed=passed and not calls and all(s in messages[0] for s in ('数据源','预算'))
    elif kind in {"attention", "learning", "candidate_pool", "forecast_trend", "experiments"}:
        passed = passed and 'finance_research_intelligence' in tool_names and sum(stage_lengths) >= 180
        if kind=='candidate_pool':
            pool_calls=[o for o in (observations or []) if o.get('tool')=='finance_research_intelligence'
                        and o.get('args',{}).get('scope') in ('pool','all')]
            observed=''.join(str(o.get('result','')) for o in pool_calls)
            passed=passed and bool(pool_calls) and 'candidate_pool_summary' in observed and 'candidate_pool' in observed
            delivered='\n'.join(messages)
            passed=passed and bool(re.search(r'可调|可以调整|可配置',delivered))
            passed=passed and not bool(re.search(r'(无法|不能|未能).{0,12}确认.{0,10}(可调|配置)',delivered))
        if kind=='forecast_trend':
            delivered='\n'.join(messages)
            observed=''.join(str(o.get('result','')) for o in (observations or [])
                if o.get('tool')=='finance_research_intelligence')
            passed=passed and 'forecast_validation' in observed and 'baseline_errors_percentage_points' in observed
            passed=passed and bool(tool_names & {'finance_market_series_batch','finance_market_screen','finance_research_bundle'})
            passed=passed and bool(re.search(r'基线',delivered)) and bool(re.search(r'条件|验证|失效|跟踪',delivered))
            passed=passed and len(delivered)>=450
    elif kind == "paraphrase":
        # Judge the requested market-to-company investigation, not a prescribed
        # number of chat messages or a particular sequence of finance tools.
        # The independent reviewer must verify at least both deliverables.
        passed = (passed and end.get('requirements_total',0) >= 2
                  and 'finance_document_retrieve' in tool_names
                  and sum(stage_lengths) >= 360)
    elif kind in {"headlines","valuation"}:
        researched = substantive
        passed = passed and researched and sum(n >= 180 for n in stage_lengths) >= 2
    else:
        required={'company':{'finance_fundamentals_batch','finance_research_bundle'},
                  'company_disclosed':{'finance_fundamentals_batch','finance_research_bundle'},
                  'fact_check':{'finance_fundamentals_batch','finance_research_bundle'},
                  'factor':{'finance_factor_analysis','finance_research_bundle'},
                  'graph':{'finance_graph_analysis','finance_research_bundle'}}[kind]
        # A concise fact check is complete when it resolves the claim and
        # gives both sourced changes; it need not be padded to research length.
        substantive_answer = (all(len(m.split("参考来源",1)[0].strip()) >= 80 for m in messages)
                              if kind == 'fact_check' else meaningful_stages)
        passed=passed and bool(tool_names & required) and substantive_answer
        if case=='fact_check':passed=passed and '22.50%' in ''.join(messages) and '73.87%' in ''.join(messages)
    delivery = delivery_checks(traces)
    graph = graph_capability_check(observations)
    passed=passed and goal_complete and published_valid and not demo_leak and delivery["passed"]
    # A document-based answer can be useful while the graph service is broken.
    # Keep these distinct; deployment acceptance of a graph case requires both.
    capability_passed = graph["passed"] if kind == "graph" else True
    return {"case":case, "question":CASES[case], "passed":passed,
          "answer_stages":len(messages), "delivery_checks": delivery, "graph_capability": graph,
          "deployment_acceptance_passed": bool(passed and capability_passed),
         "executed_tools":[x["tool"] for x in calls], "empty_fallbacks":len(bad),
         "all_repairs_passed":repairs_passed, "meaningful_stages":meaningful_stages,
         "stage_body_lengths":stage_lengths,
         "internal_citation_ids_leaked":internal_ids,
         "goal_completed":goal_complete,"goal_state":end,
         "published_answers_validated":published_valid,
         "few_shot_version":end.get("few_shot_version"), "example_content_leaked":demo_leak,
         "external_chat_messages_sent":0, "chat_history_sent":False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=CASES, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--recheck", action="store_true", help="Re-evaluate saved captures without calling tools or the model; preserve original result.json.")
    parser.add_argument("--no-few-shot", action="store_true", help="Disable examples only in this replay process for a baseline comparison.")
    args = parser.parse_args()
    if args.recheck:
        original = json.loads((args.output / "result.json").read_text())
        if original["question"] != CASES[args.case]:
            raise ValueError("Saved question does not match this case")
        messages = json.loads((args.output / "messages.json").read_text())
        traces = [json.loads(line) for line in (args.output / "trace.jsonl").read_text().splitlines()]
        result = evaluate_capture(args.case, messages, traces, read_observations(args.output))
        result.update(seconds=original["seconds"], original_passed=original["passed"], offline_recheck=True)
        (args.output / "acceptance-recheck.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
        print("RECHECK", "PASS" if result["deployment_acceptance_passed"] else "FAIL")
        return 0 if result["deployment_acceptance_passed"] else 1
    args.output.mkdir(parents=True, exist_ok=False)
    os.environ.update(load_env(ROOT / ".env.finance-agent-v3"))
    workspace = ROOT / "MMClaw/workspaces/qq_agent"
    set_workspace(workspace)
    ConfigManager.mode = "qq"
    config = json.loads((workspace / "mmclaw.json").read_text())
    config["debug"] = False
    if args.no_few_shot:
        config.setdefault("agentic_loop", {})["few_shot_enabled"] = False
    messages, traces = [], []

    def save(name, data):
        (args.output / name).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n")

    class Capture:
        def send(self, text):
            messages.append(str(text))
            save("messages.json", messages)
            print(json.dumps({"answer_stage":len(messages), "chars":len(str(text))}), flush=True)
        def start_typing(self): pass
        def stop_typing(self): pass
        def send_file(self, *args, **kwargs):
            raise RuntimeError("Capture-only replay does not send files")

    kernel = MMClaw(config, Capture(), ConfigManager.get_full_prompt(config),
                    use_stateless_arg_connector=True, stateless_use_global_memory=False)

    def trace(event, **fields):
        row = {"event":event, **fields, "timestamp":dt.datetime.now(dt.timezone.utc).isoformat()}
        traces.append(row)
        with (args.output / "trace.jsonl").open("a") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        if event in {"tool_call", "turn_end", "turn_error", "answer_repair", "research_stage", "goal_plan", "goal_review"}:
            print(json.dumps({k:v for k,v in row.items() if k in {
                "event", "tool", "executed", "blocked_reason", "error", "continued", "task_status",
                "answer_stages", "model_calls", "stop_reason", "passed", "cache_hits", "completion_status",
                "requirements_satisfied", "requirements_total", "response_mode", "selected_tools"}}, ensure_ascii=False), flush=True)

    kernel._trace = trace
    execute = kernel._execute_goal_tool
    def capture_tool(name, arguments, *options):
        if "DEMO_" in json.dumps(arguments, ensure_ascii=False):
            raise AssertionError("A synthetic example argument leaked into live tool execution")
        result = execute(name, arguments, *options)
        if name.startswith("finance_"):
            with (args.output / "tool-results.jsonl").open("a") as stream:
                stream.write(json.dumps({"tool":name,"args":arguments,"result":result[0]},
                                        ensure_ascii=False,default=str) + "\n")
        return result
    kernel._execute_goal_tool = capture_tool
    started = time.monotonic()
    kernel.chat_queue.put(CASES[args.case])
    while kernel.chat_queue.unfinished_tasks:
        if time.monotonic() - started > 900:
            kernel._stop_event.set()
            raise TimeoutError("Capture replay watchdog exceeded")
        time.sleep(0.2)
    result = evaluate_capture(args.case, messages, traces, read_observations(args.output))
    result["seconds"] = round(time.monotonic()-started,2)
    save("result.json", result)
    print("REPLAY", "PASS" if result["deployment_acceptance_passed"] else "FAIL", flush=True)
    return 0 if result["deployment_acceptance_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
