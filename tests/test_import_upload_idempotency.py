from __future__ import annotations

import hashlib
import io
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from fastapi import FastAPI, HTTPException, UploadFile
from starlette.datastructures import Headers

from src.routers.imports import (
    _upload_idempotent_file,
    _validate_client_file_ids,
    router,
    upload_import_files,
)
from src.services.import_repository import (
    UploadAssetBusyError,
    lock_uploaded_asset,
)


def _upload_file(
    name: str = "photo.jpg",
    content: bytes = b"photo",
) -> UploadFile:
    return UploadFile(
        io.BytesIO(content),
        size=len(content),
        filename=name,
        headers=Headers({"content-type": "image/jpeg"}),
    )


class _Claim:
    def __init__(self, existing: dict | None = None) -> None:
        self.existing = existing
        self.save = MagicMock()


class ImportUploadValidationTests(unittest.TestCase):
    def test_client_file_ids_are_optional_and_positional(self) -> None:
        self.assertEqual(_validate_client_file_ids(None, 2), [None, None])
        self.assertEqual(
            _validate_client_file_ids(["file-1", "file_2.jpg"], 2),
            ["file-1", "file_2.jpg"],
        )

    def test_client_file_ids_must_match_file_count(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            _validate_client_file_ids(["file-1"], 2)

        self.assertEqual(raised.exception.status_code, 400)

    def test_client_file_ids_reject_unsafe_or_duplicate_values(self) -> None:
        for values in (["../file"], ["file 1"], [""], ["a" * 129]):
            with self.subTest(values=values):
                with self.assertRaises(HTTPException) as raised:
                    _validate_client_file_ids(values, 1)
                self.assertEqual(raised.exception.status_code, 400)

        with self.assertRaises(HTTPException) as raised:
            _validate_client_file_ids(["same", "same"], 2)
        self.assertEqual(raised.exception.status_code, 400)


class ImportIdempotentUploadTests(unittest.TestCase):
    def test_completed_client_file_returns_existing_asset_without_upload(self) -> None:
        existing = {
            "id": "asset-existing",
            "original_name": "original.jpg",
            "byte_size": 123,
        }
        claim = _Claim(existing)

        @contextmanager
        def locked(_batch_id: str, _source_ref: str):
            yield claim

        with (
            patch("src.routers.imports.lock_uploaded_asset", side_effect=locked),
            patch("src.routers.imports.upload_fileobj_to_key") as upload,
        ):
            result = _upload_idempotent_file(
                batch_id="batch-1",
                file=_upload_file(),
                filename="retry.jpg",
                size=5,
                client_file_id="client-file-1",
            )

        self.assertEqual(
            result,
            {
                "id": "asset-existing",
                "originalName": "original.jpg",
                "byteSize": 123,
            },
        )
        upload.assert_not_called()
        claim.save.assert_not_called()

    def test_new_client_file_uses_stable_source_ref_and_object_key(self) -> None:
        claim = _Claim()
        digest = hashlib.sha256(b"client-file-1").hexdigest()
        expected_source_ref = f"upload:client:{digest}"
        expected_key = f"imports/batch-1/staging/client/{digest}"
        claim.save.return_value = {
            "id": "asset-1",
            "original_name": "photo.jpg",
            "byte_size": 5,
        }

        @contextmanager
        def locked(batch_id: str, source_ref: str):
            self.assertEqual(batch_id, "batch-1")
            self.assertEqual(source_ref, expected_source_ref)
            yield claim

        with (
            patch("src.routers.imports.lock_uploaded_asset", side_effect=locked),
            patch("src.routers.imports.upload_fileobj_to_key") as upload,
        ):
            result = _upload_idempotent_file(
                batch_id="batch-1",
                file=_upload_file(),
                filename="photo.jpg",
                size=5,
                client_file_id="client-file-1",
            )

        self.assertEqual(result["id"], "asset-1")
        self.assertEqual(upload.call_args.args[1], expected_key)
        self.assertEqual(claim.save.call_args.kwargs["source_ref"], expected_source_ref)
        self.assertEqual(claim.save.call_args.kwargs["staging_key"], expected_key)

    def test_database_failure_deletes_uploaded_stable_object(self) -> None:
        claim = _Claim()
        claim.save.side_effect = ValueError("batch moved")
        digest = hashlib.sha256(b"client-file-1").hexdigest()
        expected_key = f"imports/batch-1/staging/client/{digest}"

        @contextmanager
        def locked(_batch_id: str, _source_ref: str):
            yield claim

        with (
            patch("src.routers.imports.lock_uploaded_asset", side_effect=locked),
            patch("src.routers.imports.upload_fileobj_to_key"),
            patch("src.routers.imports.delete_object") as delete,
        ):
            with self.assertRaises(HTTPException) as raised:
                _upload_idempotent_file(
                    batch_id="batch-1",
                    file=_upload_file(),
                    filename="photo.jpg",
                    size=5,
                    client_file_id="client-file-1",
                )

        self.assertEqual(raised.exception.status_code, 409)
        delete.assert_called_once_with(expected_key)

    def test_failed_storage_attempt_cleans_stable_object_key(self) -> None:
        claim = _Claim()
        digest = hashlib.sha256(b"client-file-1").hexdigest()
        expected_key = f"imports/batch-1/staging/client/{digest}"

        @contextmanager
        def locked(_batch_id: str, _source_ref: str):
            yield claim

        with (
            patch("src.routers.imports.lock_uploaded_asset", side_effect=locked),
            patch(
                "src.routers.imports.upload_fileobj_to_key",
                side_effect=RuntimeError("connection lost"),
            ),
            patch("src.routers.imports.delete_object") as delete,
        ):
            with self.assertRaises(RuntimeError):
                _upload_idempotent_file(
                    batch_id="batch-1",
                    file=_upload_file(),
                    filename="photo.jpg",
                    size=5,
                    client_file_id="client-file-1",
                )

        delete.assert_called_once_with(expected_key)

    def test_concurrent_client_file_request_does_not_upload(self) -> None:
        with (
            patch(
                "src.routers.imports.lock_uploaded_asset",
                side_effect=UploadAssetBusyError("source"),
            ),
            patch("src.routers.imports.upload_fileobj_to_key") as upload,
        ):
            with self.assertRaises(HTTPException) as raised:
                _upload_idempotent_file(
                    batch_id="batch-1",
                    file=_upload_file(),
                    filename="photo.jpg",
                    size=5,
                    client_file_id="client-file-1",
                )

        self.assertEqual(raised.exception.status_code, 409)
        upload.assert_not_called()


class ImportLegacyUploadCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_client_file_ids_keep_random_upload_contract(self) -> None:
        file = _upload_file()
        with (
            patch(
                "src.routers.imports._require_batch",
                return_value={"source_type": "upload", "status": "draft"},
            ),
            patch("src.routers.imports.generate_id", return_value="upload-random"),
            patch("src.routers.imports.upload_fileobj_to_key") as upload,
            patch("src.routers.imports.add_uploaded_asset", return_value="asset-1") as add,
        ):
            result = await upload_import_files(
                batch_id="batch-1",
                files=[file],
                clientFileIds=None,
            )

        expected_key = "imports/batch-1/staging/upload-random/photo.jpg"
        self.assertEqual(upload.call_args.args[1], expected_key)
        self.assertEqual(
            add.call_args.kwargs["source_ref"],
            "upload:upload-random:photo.jpg",
        )
        self.assertEqual(add.call_args.kwargs["staging_key"], expected_key)
        self.assertEqual(result["files"][0]["id"], "asset-1")


class ImportUploadMultipartContractTests(unittest.TestCase):
    def test_route_declares_camel_case_client_file_ids_form_field(self) -> None:
        app = FastAPI()
        app.include_router(router)
        schema = app.openapi()
        body_schema = schema["paths"]["/api/imports/{batch_id}/files"]["post"][
            "requestBody"
        ]["content"]["multipart/form-data"]["schema"]
        component_name = body_schema["$ref"].rsplit("/", 1)[-1]
        properties = schema["components"]["schemas"][component_name]["properties"]

        self.assertIn("clientFileIds", properties)
        self.assertNotIn("client_file_ids", properties)
        self.assertEqual(
            properties["clientFileIds"]["anyOf"][0]["type"],
            "array",
        )


class ImportUploadRepositoryLockTests(unittest.TestCase):
    def test_lock_returns_existing_asset_and_releases_without_writes(self) -> None:
        connection = MagicMock()
        cursor = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        existing = {
            "id": "asset-1",
            "batch_id": "batch-1",
            "source_ref": "source-1",
            "original_name": "photo.jpg",
            "media_type": "image/jpeg",
            "byte_size": 5,
            "staging_key": "key-1",
        }
        cursor.fetchone.side_effect = [{"acquired": 1}, existing]
        db = MagicMock()
        db.__enter__.return_value = connection

        with patch("src.services.import_repository.get_db_connection", return_value=db):
            with lock_uploaded_asset("batch-1", "source-1") as claim:
                self.assertEqual(claim.existing, existing)

        connection.rollback.assert_called_once()
        self.assertIn("GET_LOCK", cursor.execute.call_args_list[0].args[0])
        self.assertIn("RELEASE_LOCK", cursor.execute.call_args_list[-1].args[0])

    def test_claim_saves_asset_on_lock_connection_before_release(self) -> None:
        connection = MagicMock()
        cursor = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        cursor.fetchone.side_effect = [{"acquired": 1}, None]
        db = MagicMock()
        db.__enter__.return_value = connection

        with (
            patch(
                "src.services.import_repository.get_db_connection",
                return_value=db,
            ),
            patch(
                "src.services.import_repository.add_uploaded_asset",
                return_value="asset-1",
            ) as add,
        ):
            with lock_uploaded_asset("batch-1", "source-1") as claim:
                asset = claim.save(
                    batch_id="batch-1",
                    source_ref="source-1",
                    original_name="photo.jpg",
                    media_type="image/jpeg",
                    byte_size=5,
                    staging_key="key-1",
                )
                connection.commit.assert_called_once()
                self.assertNotIn(
                    "RELEASE_LOCK",
                    cursor.execute.call_args_list[-1].args[0],
                )

        self.assertEqual(asset["id"], "asset-1")
        self.assertIs(add.call_args.kwargs["cursor"], cursor)
        self.assertIn("RELEASE_LOCK", cursor.execute.call_args_list[-1].args[0])

    def test_lock_rejects_a_concurrent_holder(self) -> None:
        connection = MagicMock()
        cursor = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        cursor.fetchone.return_value = {"acquired": 0}
        db = MagicMock()
        db.__enter__.return_value = connection

        with patch("src.services.import_repository.get_db_connection", return_value=db):
            with self.assertRaises(UploadAssetBusyError):
                with lock_uploaded_asset("batch-1", "source-1"):
                    pass

        self.assertEqual(cursor.execute.call_count, 1)


if __name__ == "__main__":
    unittest.main()
