from __future__ import annotations

import importlib
import os
import unittest
from unittest.mock import MagicMock, patch


class StoragePersistenceTests(unittest.TestCase):
    def test_state_save_is_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            from src.services import storage

            module = importlib.reload(storage)
            with patch("src.services.storage.urlopen") as urlopen_mock:
                module.save_s3_state_after_upload()

            urlopen_mock.assert_not_called()

    def test_state_save_posts_to_localstack_when_enabled(self):
        with patch.dict(
            os.environ,
            {
                "LOCALSTACK_STATE_URL": "http://localstack:4566/",
                "S3_SAVE_STATE_AFTER_UPLOAD": "1",
            },
        ):
            from src.services import storage

            module = importlib.reload(storage)
            response = MagicMock()
            response.__enter__.return_value.read.return_value = b'{"status":"ok"}'

            with patch("src.services.storage.urlopen", return_value=response) as urlopen_mock:
                module.save_s3_state_after_upload()

            request = urlopen_mock.call_args.args[0]
            self.assertEqual(
                request.full_url,
                "http://localstack:4566/_localstack/state/s3/save",
            )
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(urlopen_mock.call_args.kwargs["timeout"], 30)


if __name__ == "__main__":
    unittest.main()
