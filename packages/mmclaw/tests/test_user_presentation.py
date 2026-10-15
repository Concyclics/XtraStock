"""Finance delivery keeps audit references internally and renders readable sources."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mmclaw.connectors import QQBotConnector, WeChatConnector
from mmclaw.evidence import EvidenceStore
from mmclaw.kernel import MMClaw
from mmclaw.presentation import (
    render_finance_text, render_report_file, publish_readable_report, source_catalog,
)

DOCUMENT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
E1 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb:E0001"
E2 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb:E0002"


def financial_item(eid=E1):
    return {
        "evidence_id": eid, "claim_type": "actual", "entity_id": "603986",
        "metric_id": "revenue", "value": {"value": "12345600000", "unit": "CNY"},
        "source_id": "cninfo_official", "source_document_id": DOCUMENT,
        "period_end": "2026-06-30", "quality_status": "verified",
    }


class PresentationTests(unittest.TestCase):
    def test_sources_are_deduplicated_without_altering_numbers_or_dates(self):
        text = f"603986：2026-06-30营收123.46亿元，同比增长12.35%。[{E1}][{E2}]"
        result = render_finance_text(text, [financial_item(), financial_item(E2)])
        self.assertIn("603986：2026-06-30营收123.46亿元，同比增长12.35%", result)
        self.assertIn("[来源1]", result)
        self.assertNotIn("来源2", result)
        self.assertIn("巨潮资讯（公司公告）", result)
        self.assertNotIn(E1, result)
        self.assertNotIn(DOCUMENT, result)

    def test_same_document_with_different_financial_periods_is_not_mislabelled(self):
        old = financial_item(E2)
        old["period_end"] = "2025-06-30"
        text = render_finance_text(f"[{E1}] [{E2}]", [financial_item(), old])
        self.assertIn("来源2", text)
        self.assertIn("报告期2025-06-30", text)
        self.assertIn("报告期2026-06-30", text)

    def test_quality_limits_survive_in_plain_chinese(self):
        text = render_finance_text("行情 quality_status=unverified；因子 lifecycle_status=shadow；"
                                   "same_day_ai_hardware_reports为空，不能作为上涨概率。")
        for needed in ("尚未充分核验", "研究阶段", "当日相关研报为空", "不能作为上涨概率"):
            self.assertIn(needed, text)
        for removed in ("quality_status", "lifecycle_status", "unverified", "shadow"):
            self.assertNotIn(removed, text)

    def test_complete_evidence_ledger_phrase_does_not_leave_duplicate_words(self):
        for phrase in ("本证据账本", "证据账本", "本证据台账", "本证据账"):
            with self.subTest(phrase=phrase):
                text = render_finance_text(f"{phrase}不足以确认估值。")
                self.assertEqual("本次资料不足以确认估值。", text)
                self.assertEqual(text, render_finance_text(text))

    def test_clock_control_citations_disappear_without_losing_time(self):
        clock = {"evidence_id": E1, "claim_type": "data_quality", "source_id": "system:clock"}
        result = render_finance_text(f"北京时间2026-10-12 20:30:15 [{E1}]。", [clock])
        self.assertIn("2026-10-12 20:30:15", result)
        self.assertNotIn("参考来源", result)
        self.assertNotIn(E1, result)

    def test_unknown_ids_do_not_gain_invented_source_metadata(self):
        result = render_finance_text(f"尚缺原文。相关文档：`{DOCUMENT}`。")
        self.assertEqual("尚缺原文。", result)
        self.assertNotIn("参考来源", result)

    def test_debug_metadata_is_removed_but_same_line_data_warning_survives(self):
        result = render_finance_text("request_id=abcdef0123456789；财务数据未更新。\n"
                                     + "snapshot_sha256=" + "a" * 64 + "\n追踪编号：1234abcd")
        self.assertIn("财务数据未更新", result)
        for forbidden in ("request_id", "snapshot_sha256", "追踪编号", "abcdef", "aaaa"):
            self.assertNotIn(forbidden, result)

    def test_truncated_citation_is_flagged_without_guessing_the_document(self):
        shortened = DOCUMENT[:-4]
        catalog = {DOCUMENT: {"claim_type": "document_claim", "value": {"title": "原始公告"}}}
        result = render_finance_text(f"原文：{shortened}。", catalog=catalog)
        self.assertIn("出处待核对", result)
        self.assertNotIn("原始公告", result)
        self.assertNotIn(shortened, result)

    def test_link_targets_remain_intact_even_when_they_contain_ids(self):
        url = f"https://example.test/notice/{DOCUMENT}?sha256=" + "a" * 64
        result = render_finance_text(f"公告[原文]({url})，质量为unverified。")
        self.assertIn(url, result)
        self.assertIn("尚未充分核验", result)

    def test_v3_report_and_quality_references(self):
        snapshot = {"evidence": [
            {"evidence_id": "REP0001", "claim_type": "document_claim", "source_id": "某证券研究所",
             "value": {"report_title": "行业研究", "institution": "某证券研究所"}, "as_of_date": "2026-10-11"},
            {"evidence_id": "DQ0001", "claim_type": "data_quality"},
        ]}
        result = render_finance_text("报告日期2026-10-12；行业观察 [evidence: REP0001, DQ0001]。\n"
                                     "缺口 missing:company.financial_facts", snapshot)
        self.assertIn("某证券研究所《行业研究》，2026-10-11", result)
        self.assertIn("缺少资料:财务数据", result)
        self.assertNotIn("DQ0001", result)
        self.assertNotIn("evidence:", result)

    def test_focus_citations_keep_distinct_names_without_internal_ids(self):
        evidence=[{'evidence_id':f'FOCUS{i:04d}','claim_type':'interpretation',
            'metric_id':'research_focus','as_of_date':'2026-10-13',
            'value':{'label':label}} for i,label in enumerate(['光模块扩产','股东增持'],1)]
        result=render_finance_text('关注两条线索 [evidence: FOCUS0001, FOCUS0002]。',evidence)
        self.assertIn('研究关注《光模块扩产》，2026-10-13',result)
        self.assertIn('研究关注《股东增持》，2026-10-13',result)
        self.assertIn('[来源2]',result)
        self.assertNotIn('FOCUS',result);self.assertNotIn('evidence:',result)
        self.assertEqual(result,render_finance_text(result,evidence))

    def test_graph_entities_are_not_misrepresented_as_document_publishers(self):
        data = {"relation_version_id": DOCUMENT, "source_name": "工业富联", "target_name": "GPU"}
        result = render_finance_text(DOCUMENT, data)
        self.assertIn("产业关联资料：工业富联 → GPU（原文出处未提供）", result)

    def test_repeated_delivery_does_not_duplicate_sources_or_change_formatting(self):
        catalog = source_catalog([financial_item()])
        text = f"数据 [{E1}]；相关文档：`{'d' * 32}`、`{'e' * 32}`。\n"
        result = render_finance_text(text, catalog=catalog)
        self.assertEqual(result, render_finance_text(result, catalog=catalog))
        self.assertEqual(result, render_finance_text(result))

    def test_source_titles_cannot_reintroduce_internal_ids(self):
        item = {"evidence_id": E1, "claim_type": "document_claim",
                "value": {"title": f"<b>公司公告</b> {DOCUMENT}", "source_name": "cninfo"}}
        result = render_finance_text(f"[{E1}]", [item])
        self.assertIn("公司公告", result)
        self.assertNotIn(DOCUMENT, result)
        self.assertNotIn("<b>", result)

    def test_legacy_degraded_report_does_not_expose_raw_provider_exception(self):
        result = render_finance_text("## 数据质量与限制\n- 模型生成降级原因：TimeoutError private/internal/path trace_id=12345678\n"
                                     "- 行情缺失，暂不能判断。")
        self.assertIn("未能完成生成或通过核验", result)
        self.assertIn("行情缺失", result)
        self.assertNotIn("TimeoutError", result)
        self.assertNotIn("private", result)


class DeliveryTests(unittest.TestCase):
    def test_guard_validates_and_audits_original_but_only_readable_text_is_sent_and_saved(self):
        agent = MMClaw.__new__(MMClaw)
        agent.config = {"tool_profile": "finance_chat_readonly"}
        agent.connector = mock.Mock()
        agent._trace = mock.Mock()
        agent._append_model_message = mock.Mock()
        store = EvidenceStore("c" * 32, "分析603986营收")
        store.items[E1] = financial_item()
        store.persist = mock.Mock()
        store.persist_audit = mock.Mock()
        draft = f"603986截至2026-06-30营收123.46亿元 [{E1}]。"
        agent._deliver_answer(draft, store, [], False, [], True)
        self.assertEqual("passed", store.persist_audit.call_args.args[2])
        self.assertEqual(draft, store.persist_audit.call_args.args[1])
        delivered = agent.connector.send.call_args.args[0]
        self.assertNotIn(E1, delivered)
        self.assertIn("巨潮资讯", delivered)
        self.assertEqual(delivered, agent._append_model_message.call_args.args[0]["content"])
        self.assertEqual(draft, agent._trace.call_args.kwargs["audited_answer"])

    def test_incomplete_repair_keeps_available_facts_without_audit_error_template(self):
        store = EvidenceStore("c" * 32, "分析")
        store.items[E1] = financial_item()
        raw = store.deterministic_fallback({"violations": [{"code": "unsupported_numeric_value"}]})
        text = render_finance_text(raw, store.items)
        self.assertIn("营业收入", text)
        self.assertNotIn("未通过证据校验", text)
        for forbidden in ("unsupported_numeric_value", E1, "metric_id", "source_id", "value"):
            self.assertNotIn(forbidden, text)

    def test_report_publication_preserves_audited_original_and_readable_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            draft, daily, pack = root / "draft.md", root / "2026-10-12.md", root / "data_pack.json"
            original = f"当日相关研报为空。背景文档：{DOCUMENT}。"
            draft.write_text(original)
            pack.write_text(json.dumps({"recent_ai_hardware_reports": [{"document_id": DOCUMENT,
                "report_title": "上期研究", "institution": "某证券", "report_date": "2026-10-11"}]}))
            public = publish_readable_report(draft, daily, pack)
            self.assertEqual(original, (root / "report_audited.md").read_text())
            self.assertEqual(original, draft.read_text())
            self.assertIn("某证券《上期研究》，2026-10-11", public)
            self.assertIn("当日相关研报为空", public)
            self.assertNotIn(DOCUMENT, public)
            self.assertEqual(public.strip(), render_report_file(daily))

    def test_both_daily_connectors_render_old_reports_before_chunking_without_sending(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "2026-10-12.md"
            path.write_text(f"昨日行情未核验 [{E1}]；本期因子为shadow。")
            path.with_suffix(".sources.json").write_text(json.dumps(source_catalog([financial_item()])))
            for cls in (QQBotConnector, WeChatConnector):
                with self.subTest(connector=cls.__name__):
                    connector = cls.__new__(cls)
                    connector._today_key = mock.Mock(return_value="2026-10-12")
                    connector._int_config = mock.Mock(side_effect=lambda key, default: default)
                    output = "\n".join(connector._daily_report_messages(path))
                    self.assertNotIn(E1, output)
                    self.assertNotIn("shadow", output)
                    self.assertIn("巨潮资讯", output)
                    self.assertIn("昨日行情未核验", output)


if __name__ == "__main__":
    unittest.main()
