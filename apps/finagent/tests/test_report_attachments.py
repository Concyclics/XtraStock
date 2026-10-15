import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts/pipeline"))
import report_attachments as attachments
import pdf_ocr_markdown as converter
import ocr_discover_queue as discovery


class ReportAttachmentTests(unittest.TestCase):
    def test_text_disguised_as_pdf_bypasses_backend_and_preserves_content(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / "report.pdf"
            body = "【内容摘要】\r\n公司营收增长，净利润为 12.35 亿元。\r\n"
            source.write_bytes(body.encode("gb18030"))
            with patch.object(converter, "get_first_model", side_effect=AssertionError("must not call GPU")):
                self.assertEqual(converter.main([str(source), "-o", str(root / "out"), "--no-progress"]), 0)
            md, quality, _ = attachments.output_paths(source, root / "out")
            self.assertEqual(md.read_text(), body.replace("\r\n", "\n"))
            self.assertEqual(attachments.validate_outputs(source, md, quality)["encoding"], "gb18030")

    def test_non_pdf_downloads_identified_and_error_pages_rejected(self):
        self.assertEqual(attachments.validate_download("有效附件正文".encode()), "text")
        for payload in (b"<html>Access denied</html>", b'{"error":"denied"}', b"\x00\x01garbage"):
            with self.assertRaises(ValueError):
                attachments.validate_download(payload)

    def test_xlsx_reads_all_sheets_percent_dates_and_error_values(self):
        from openpyxl import Workbook
        import datetime
        book = Workbook(); sheet = book.active; sheet.title = "汇总"
        sheet["A1"] = "收入|增长"; sheet["B1"] = .125; sheet["B1"].number_format = "0.00%"
        hidden = book.create_sheet("明细"); hidden.sheet_state = "hidden"
        hidden["C2"] = datetime.datetime(2023, 5, 4); hidden["D2"] = "#DIV/0!"
        buf = io.BytesIO(); book.save(buf)
        body, info, sheets = attachments.extract_native(buf.getvalue())
        self.assertEqual(info["cell_count"], 4)
        self.assertEqual(info["sheet_count"], 2)
        self.assertEqual(info["source_error_cells"], 1)
        self.assertIn("12.50%", body); self.assertIn("收入\\|增长", body)
        self.assertIn("2023-05-04", body); self.assertIn("#DIV/0!", body)
        self.assertEqual(sheets[1]["cells"][0]["address"], "C2")

    def test_modified_output_and_partial_quality_cannot_be_marked_ok(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); path = root / "a.pdf"; path.write_text("原始附件文本")
            attachments.convert_native(path, root / "out")
            md, quality, _ = attachments.output_paths(path, root / "out")
            md.write_text("changed")
            with self.assertRaisesRegex(ValueError, "checksum"):
                attachments.validate_outputs(path, md, quality)
            summary = json.loads(quality.read_text()); summary["failed_pages"] = [2]
            quality.write_text(json.dumps(summary))
            with self.assertRaisesRegex(ValueError, "incomplete"):
                attachments.validate_outputs(path, md, quality)

    def test_pdf_parse_error_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); path = root / "bad.pdf"; path.write_bytes(b"%PDF-1.7\ninvalid")
            with patch.object(converter, "convert_pdf", side_effect=ValueError("broken page tree")):
                self.assertEqual(converter.main([str(path), "-o", str(root / "out"), "--model", "test", "--no-progress"]), 1)

    def test_discovery_canonicalizes_aliases(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); real = root / "real"; real.mkdir(); (real / "a.pdf").write_text("source")
            alias = root / "alias"; alias.symlink_to(real, target_is_directory=True)
            rows = discovery.discover_one("stock", alias, root / "out", 600)
            self.assertEqual(rows[0]["pdf_path"], str(real / "a.pdf"))

    def test_attachment_full_text_index_keeps_tail_and_meili_stays_bounded(self):
        sys.path.insert(0, str(ROOT / "scripts/search"))
        import sync_document_search as search
        row = {"id": "test", "source_type": "research_report", "source_name": "eastmoney",
               "title": "研究报告", "body": "正文" * 4000 + "末表独有关键字", "full_body_search": True}
        indexed = search.build_search_row(row, 6000)
        self.assertIn("末表独有关键字", indexed["search_text"])
        self.assertLessEqual(len(search.meili_doc(indexed, 1800)["body"]), 1800)
        row["full_body_search"] = False
        self.assertNotIn("末表独有关键字", search.build_search_row(row, 6000)["search_text"])

    def test_numeric_index_handles_chinese_prefix_sign_and_thousands(self):
        sys.path.insert(0, str(ROOT / "scripts/search"))
        import sync_document_search as search
        text = "同比增长119.38%，亏损-19.63%，营收1,776.58亿元，同比增长+2.50%"
        self.assertEqual(set(search.numeric_search_text(text).split()),
                         {"119.38", "-19.63", "1776.58", "2.50"})

    def test_oversized_document_keeps_searchable_tail_with_bounded_ranking(self):
        sys.path.insert(0, str(ROOT / "scripts/search"))
        import sync_document_search as search
        body = " ".join(f"token{n}" for n in range(150000)) + " 末页金额1,234,567.89"
        indexed = search.build_search_row({
            "id": "test", "source_type": "announcement", "source_name": "cninfo",
            "title": "完整公告", "body": body, "full_body_search": True,
        }, 6000)
        with patch.object(search, "connect") as connect:
            search.upsert_fts([indexed])
        payload = connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value.executemany.call_args.args[1][0]
        self.assertTrue(payload["search_text"].endswith("末页金额1,234,567.89"))
        self.assertEqual(payload["body_chars"], len(body))
        self.assertLess(len(payload["fts_text"].encode()), 1048576)
        self.assertNotIn("末页金额", payload["fts_text"])
        self.assertTrue(json.loads(payload["metadata"])["fts_vector_prefix_only"])

    def test_deepseek_wrapped_layout_is_cleaned_and_figures_are_detected(self):
        raw = "<|ref|>image<|/ref|><|det|>[[10, 20, 800, 900]]<|/det|>\n图表说明\n"
        self.assertEqual(converter.clean_markdown(raw, False), "图表说明")
        self.assertEqual(converter.extract_layout_boxes(raw, {"image", "figure"}), [("image", (10, 20, 800, 900))])
        self.assertIn("image[[", converter.clean_markdown(raw, True))

    def test_short_native_chapter_title_does_not_imply_ocr_failure(self):
        quality = converter.assess_quality(page_number=1, markdown="投资建议", raw_markdown="投资建议", embedded_text="投资建议\n1", finish_reason="stop", elapsed_sec=1)
        self.assertNotIn("short_output", quality.warnings)
        quality = converter.assess_quality(page_number=1, markdown="", raw_markdown="", embedded_text="投资建议\n1", finish_reason="stop", elapsed_sec=1)
        self.assertIn("short_output", quality.warnings)

    def test_native_figure_text_preserves_numbers_but_rejects_mixed_raster(self):
        import fitz
        from PIL import Image
        with fitz.open() as doc:
            page = doc.new_page(width=200, height=200)
            page.insert_text((15, 50), "Revenue 12345.67 Profit 890.12", fontsize=8)
            box = (0, 0, 1000, 1000)
            text = converter.native_vector_figure_text(page, box)
            self.assertIn("12345.67", text); self.assertIn("890.12", text)
            page.set_rotation(90)
            self.assertIsNone(converter.native_vector_figure_text(page, box))
            page.set_rotation(0)
            image = io.BytesIO(); Image.new("RGB", (10, 10), "white").save(image, format="PNG")
            page.insert_image(fitz.Rect(150, 150, 190, 190), stream=image.getvalue())
            self.assertIsNone(converter.native_vector_figure_text(page, box))

    def test_padding_does_not_turn_chapter_icon_into_large_figure(self):
        from PIL import Image
        image = Image.new("RGB", (1200, 1700), "white")
        icon = (730, 247, 786, 286)
        self.assertTrue(converter.is_small_figure(image, icon))
        crop = converter.crop_normalized_box(image, icon, 35)
        self.assertGreater(crop.height, image.height * .04)
        self.assertFalse(converter.is_small_figure(image, (55, 466, 789, 822)))

    def test_ocr_request_retains_html_cell_tokens(self):
        session = Mock(); response = session.post.return_value
        table = "<table><tr><td>1776.58</td><td>002430.SZ</td></tr></table>"
        response.json.return_value = {"choices": [{"message": {"content": table}, "finish_reason": "stop"}]}
        text, _, _ = converter.call_ocr(session=session, base_url="http://localhost/v1", model="test",
            image_bytes=b"test", prompt="ocr", max_tokens=1024, temperature=0, timeout=1, retries=0)
        self.assertFalse(session.post.call_args.kwargs["json"]["skip_special_tokens"])
        self.assertEqual(converter.clean_markdown(text, False), table)

    def test_truncated_page_or_figure_is_not_a_successful_conversion(self):
        import fitz
        for responses in [
            [("partial page", "length", {})],
            [("<|ref|>image<|/ref|><|det|>[[100,100,900,900]]<|/det|>\n正文", "stop", {}),
             ("partial figure", "length", {})],
        ]:
            with self.subTest(responses=len(responses)), tempfile.TemporaryDirectory() as temp:
                root = Path(temp); source = root / "source.pdf"
                with fitz.open() as doc:
                    doc.new_page().insert_text((50, 50), "Document body")
                    doc.save(source)
                with patch.object(converter, "call_ocr", side_effect=responses):
                    code = converter.main([str(source), "-o", str(root / "out"),
                                           "--model", "test", "--no-progress"])
                self.assertEqual(code, 1)
                md, quality, _ = attachments.output_paths(source, root / "out")
                self.assertEqual(json.loads(quality.read_text())["failed_pages"], [1])
                with self.assertRaisesRegex(ValueError, "incomplete"):
                    attachments.validate_outputs(source, md, quality)

    def test_truncated_figure_uses_bounded_grounded_retry(self):
        with patch.object(converter, 'call_ocr', side_effect=[('repeated', 'length', {}), ('原文金额 123.45', 'stop', {})]) as call:
            text, finish, _ = converter.call_figure_ocr(prompt=converter.DEFAULT_FIGURE_PROMPT)
            self.assertEqual(text, '原文金额 123.45')
            self.assertEqual(finish, 'stop')
            self.assertEqual(call.call_count, 2)
            self.assertEqual(call.call_args.kwargs['prompt'], converter.DEFAULT_PROMPT)

    def test_explicit_ocr_rotation_preserves_source_and_records_orientation(self):
        import fitz
        from PIL import Image
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / "sideways.pdf"
            with fitz.open() as doc:
                page = doc.new_page(width=100, height=200)
                page.draw_rect(fitz.Rect(0, 0, 30, 30), color=(1, 0, 0), fill=(1, 0, 0))
                doc.save(source)
            original = source.read_bytes()
            with fitz.open(source) as doc:
                image = converter.render_page_to_image(doc[0], 72, 0, 90)
                self.assertEqual(image.size, (200, 100))
                self.assertEqual(image.getpixel((185, 15)), (255, 0, 0))
            def read_image(**kwargs):
                image = Image.open(io.BytesIO(kwargs['image_bytes']))
                self.assertGreater(image.width, image.height)
                return ('Correct orientation 1,421,540,472 shares', 'stop', {})
            with patch.object(converter, 'call_ocr', side_effect=read_image):
                code = converter.main([str(source), '-o', str(root/'out'), '--model', 'test',
                                       '--rotate-clockwise', '90', '--no-progress'])
            self.assertEqual(code, 0)
            self.assertEqual(source.read_bytes(), original)
            md, quality, _ = attachments.output_paths(source, root/'out')
            self.assertEqual(attachments.validate_outputs(source, md, quality)['rotate_clockwise'], 90)
            # An orientation change must not reuse an otherwise validated output.
            with patch.object(converter, 'call_ocr', return_value=('Updated orientation text', 'stop', {})) as call:
                converter.main([str(source), '-o', str(root/'out'), '--model', 'test', '--no-progress'])
            call.assert_called_once()

    def test_page_retry_reuses_only_intact_cache_and_blank_review_is_source_bound(self):
        import fitz
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'source.pdf'
            with fitz.open() as doc:
                for _ in range(3):doc.new_page()
                doc.save(source)
            base=[str(source),'-o',str(root/'out'),'--model','test','--no-progress']
            with patch.object(converter,'call_ocr',side_effect=[
                ('Page one content. '*20,'stop',{}),('','length',{}),('Page three content. '*20,'stop',{})]):
                self.assertEqual(converter.main(base),1)
            options=['--retry-failed-pages','--confirmed-blank-page','2','--blank-source-sha256',attachments.sha256(source.read_bytes())]
            with patch.object(converter,'call_ocr',side_effect=AssertionError('validated pages must be reused')):
                self.assertEqual(converter.main(base+options),0)
            md,q,_=attachments.output_paths(source,root/'out')
            summary=attachments.validate_outputs(source,md,q)
            self.assertEqual(summary['cached_pages'],[1,3])
            self.assertEqual(summary['confirmed_blank_pages'],[2])
            self.assertEqual(summary['pages'],3)
            # Corrupt cache content must force fresh OCR, even when its stored
            # per-page flags still say successful.
            md.write_text(md.read_text()+'tampered')
            with patch.object(converter,'call_ocr',return_value=('Re-read content. '*20,'stop',{})) as call:
                self.assertEqual(converter.main(base+options),0)
                self.assertEqual(call.call_count,2)
            with patch.object(converter,'call_ocr',side_effect=AssertionError('bad review hash')):
                self.assertEqual(converter.main(base+['--confirmed-blank-page','2','--blank-source-sha256','wrong']),1)

    def test_worker_rejects_nonempty_but_failed_quality(self):
        import argparse
        import ocr_worker as worker
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); pdf = root / "source.pdf"; pdf.write_text("原始附件")
            md = root / "result.md"; md.write_text("partial result")
            q = root / "quality.json"; q.write_text(json.dumps({"pages": 2, "total_chars": 20, "failed_pages": [2]}))
            item = {"id": 1, "report_type": "stock", "pdf_path": str(pdf), "md_path": str(md), "quality_json_path": str(q)}
            args = argparse.Namespace(pdf_workers=1, min_age_seconds=0, no_figure_pass=False, max_retries=3)
            with patch.object(worker, "run_logged"), patch.object(worker, "log_path", return_value=root / "log"), patch.object(worker, "mark_ok") as ok, patch.object(worker, "mark_failed") as failed:
                self.assertFalse(worker.process_item(item, args))
                ok.assert_not_called(); failed.assert_called_once()

    def test_import_retains_failure_without_markdown_and_keeps_web_summary(self):
        from collections import defaultdict
        sys.path.insert(0, str(ROOT / "scripts"))
        import prepare_eastmoney_reports_import as reports
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); pdf = root / "broken.pdf"; pdf.write_bytes(b"%PDF-broken")
            q = root / "quality.json"
            q.write_text(json.dumps({"error": "broken page tree", "failed_pages": ["open_or_convert"]}))
            row = defaultdict(str, {"报告链接": "https://example.org/?infocode=AP123456789",
                                   "报告名称": "报告", "日期": "2026-10-12", "原始文本": "网页摘要保留"})
            paths = {"pdf": pdf, "ocr_md": None, "ocr_quality_json": q, "ocr_quality_md": None}
            with patch.object(reports, "resolve_paths", return_value=paths):
                result = reports.prepare_report("industry", row)
            self.assertEqual(result.ocr_status, "partial")
            self.assertEqual(result.content_text, "网页摘要保留")
            self.assertEqual(json.loads(result.metadata)["attachment_failure"]["error"], "broken page tree")


if __name__ == "__main__":
    unittest.main()
