import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from mmclaw.connectors import QQBotConnector


class QQImageUploadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "generated"
        self.root.mkdir()
        self.image = self.root / "chart.png"
        self.image.write_bytes(b"synthetic image bytes")
        self.connector = QQBotConnector.__new__(QQBotConnector)
        self.connector.qq_config = {"file_upload_mode": "inline", "file_server_dir": str(self.root)}
        self.request = AsyncMock(return_value={"file_info": "synthetic-media-reference"})
        self.connector._api = SimpleNamespace(_http=SimpleNamespace(request=self.request))
        self.connector._ensure_file_server = Mock(side_effect=AssertionError("Public server must not start"))

    async def test_group_and_c2c_upload_use_bytes_without_public_server(self):
        for kind, expected in [("group", "/v2/groups/test-target/files"), ("c2c", "/v2/users/test-target/files")]:
            await self.connector._upload_image_media(kind, "test-target", self.image)
            args = self.request.call_args
            self.assertTrue(args.args[0].url.endswith(expected))
            payload = args.kwargs["json"]
            self.assertEqual(self.image.read_bytes(), base64.b64decode(payload["file_data"]))
            self.assertFalse(payload["srv_send_msg"])
            self.assertNotIn("url", payload)
        self.connector._ensure_file_server.assert_not_called()

    async def test_symlink_outside_generated_directory_is_blocked(self):
        outside = Path(self.temp.name) / "private.png"
        outside.write_bytes(b"private")
        link = self.root / "link.png"
        link.symlink_to(outside)
        with self.assertRaises(ValueError):
            await self.connector._upload_image_media("group", "test-target", link)
        self.request.assert_not_called()

    async def test_oversized_or_empty_image_is_blocked(self):
        self.connector.qq_config["file_upload_max_bytes"] = 1
        with self.assertRaises(ValueError):
            await self.connector._upload_image_media("group", "test-target", self.image)
        self.image.write_bytes(b"")
        with self.assertRaises(ValueError):
            await self.connector._upload_image_media("group", "test-target", self.image)
        self.request.assert_not_called()

    async def test_missing_media_reference_is_a_failure(self):
        self.request.return_value = None
        with self.assertRaises(RuntimeError):
            await self.connector._upload_image_media("group", "test-target", self.image)


if __name__ == "__main__":
    unittest.main()
