import unittest

from MMClaw.mmclaw.connectors import QQBotConnector, WeChatConnector


class ConnectorChunkingTests(unittest.TestCase):
    def test_qq_split_preserves_long_response(self):
        connector = QQBotConnector.__new__(QQBotConnector)
        content = "第一段\n" + ("中际旭创与光模块的验证结论。" * 500) + "\n最后一段"
        chunks = connector._split_text_chunks(content, 3998)

        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), content)
        self.assertTrue(all(len(chunk) <= 3998 for chunk in chunks))

    def test_wechat_split_preserves_long_response(self):
        connector = WeChatConnector.__new__(WeChatConnector)
        content = "标题\n" + ("数据截止日和证据来源必须保留。" * 300) + "\n结论"
        chunks = connector._split_text_chunks(content, 900)

        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), content)
        self.assertTrue(all(len(chunk) <= 900 for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
