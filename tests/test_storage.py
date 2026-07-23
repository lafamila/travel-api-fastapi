from __future__ import annotations

import importlib
import os
import unittest
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError


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

    def test_localstack_uses_public_browser_url(self):
        with patch.dict(
            os.environ,
            {
                "STORAGE_BACKEND": "localstack",
                "S3_PUBLIC_BASE_URL": "http://localhost:4566/",
                "S3_BUCKET_NAME": "travel-dev",
            },
            clear=True,
        ):
            from src.services import storage

            module = importlib.reload(storage)

        self.assertTrue(module.S3_AUTO_CREATE_BUCKET)
        self.assertEqual(
            module.object_access_url("places/example.jpg"),
            "http://localhost:4566/travel-dev/places/example.jpg",
        )
        self.assertIsNone(module.object_access_expires_at())
        self.assertTrue(
            module.is_managed_object_url(
                "http://localhost:4566/travel-dev/places/example.jpg"
            )
        )
        self.assertFalse(
            module.is_managed_object_url("https://images.example/place.jpg")
        )

    def test_r2_uses_presigned_url_and_never_auto_creates_bucket(self):
        with patch.dict(
            os.environ,
            {
                "STORAGE_BACKEND": "r2",
                "S3_ENDPOINT_URL": "https://account.r2.cloudflarestorage.com",
                "S3_BUCKET_NAME": "teddy-travel-prod",
                "S3_REGION": "auto",
                "AWS_ACCESS_KEY_ID": "access",
                "AWS_SECRET_ACCESS_KEY": "secret",
            },
            clear=True,
        ):
            from src.services import storage

            module = importlib.reload(storage)
            client = MagicMock()
            client.generate_presigned_url.return_value = "https://signed.example/object"
            with patch.object(module, "get_s3_client", return_value=client):
                url = module.object_access_url("places/example.jpg")

        self.assertFalse(module.S3_AUTO_CREATE_BUCKET)
        self.assertEqual(url, "https://signed.example/object")
        self.assertTrue(
            module.is_managed_object_url(
                "https://account.r2.cloudflarestorage.com/"
                "teddy-travel-prod/places/example.jpg?X-Amz-Signature=test"
            )
        )
        client.generate_presigned_url.assert_called_once_with(
            "get_object",
            Params={
                "Bucket": "teddy-travel-prod",
                "Key": "places/example.jpg",
            },
            ExpiresIn=600,
        )

    def test_r2_missing_bucket_fails_without_create_attempt(self):
        with patch.dict(
            os.environ,
            {
                "STORAGE_BACKEND": "r2",
                "S3_ENDPOINT_URL": "https://account.r2.cloudflarestorage.com",
                "S3_BUCKET_NAME": "teddy-travel-prod",
                "AWS_ACCESS_KEY_ID": "access",
                "AWS_SECRET_ACCESS_KEY": "secret",
            },
            clear=True,
        ):
            from src.services import storage

            module = importlib.reload(storage)
            client = MagicMock()
            client.head_bucket.side_effect = ClientError(
                {
                    "Error": {"Code": "NoSuchBucket", "Message": "missing"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "HeadBucket",
            )
            with patch.object(module, "get_s3_client", return_value=client):
                with self.assertRaisesRegex(RuntimeError, "does not exist"):
                    module.ensure_bucket()

        client.create_bucket.assert_not_called()

    def test_r2_rejects_localstack_only_settings(self):
        with patch.dict(
            os.environ,
            {
                "STORAGE_BACKEND": "r2",
                "S3_REGION": "ap-northeast-2",
                "S3_AUTO_CREATE_BUCKET": "true",
                "S3_PUBLIC_BASE_URL": "http://localhost:4566",
                "AWS_ACCESS_KEY_ID": "test",
                "AWS_SECRET_ACCESS_KEY": "test",
            },
            clear=True,
        ):
            from src.services import storage

            module = importlib.reload(storage)
            with self.assertRaisesRegex(RuntimeError, "Invalid R2"):
                module.validate_storage_configuration()


if __name__ == "__main__":
    unittest.main()
