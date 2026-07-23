from __future__ import annotations

import os
import re
import logging
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import BinaryIO
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "localstack").strip().lower()
if STORAGE_BACKEND not in {"localstack", "r2"}:
    raise ValueError("STORAGE_BACKEND must be 'localstack' or 'r2'")

S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "http://localhost:4566").rstrip("/")
S3_PUBLIC_BASE_URL = os.getenv("S3_PUBLIC_BASE_URL", "").rstrip("/")
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "teddy-travel-media")
S3_REGION = os.getenv(
    "S3_REGION", "ap-northeast-2" if STORAGE_BACKEND == "localstack" else "auto"
)
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID", "test")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "test")
LOCALSTACK_STATE_URL = os.getenv("LOCALSTACK_STATE_URL", S3_ENDPOINT_URL).rstrip("/")
S3_SAVE_STATE_AFTER_UPLOAD = os.getenv("S3_SAVE_STATE_AFTER_UPLOAD", "0") == "1"
S3_STATE_SAVE_STRICT = os.getenv("S3_STATE_SAVE_STRICT", "0") == "1"
S3_AUTO_CREATE_BUCKET = os.getenv(
    "S3_AUTO_CREATE_BUCKET", "1" if STORAGE_BACKEND == "localstack" else "0"
).strip().lower() in {"1", "true", "yes", "on"}
S3_SIGNED_URL_TTL_SECONDS = int(os.getenv("S3_SIGNED_URL_TTL_SECONDS", "600"))


def _client_config() -> Config:
    return Config(
        signature_version="s3v4",
        retries={"max_attempts": 4, "mode": "standard"},
        s3={"addressing_style": "path"},
    )


@cache
def get_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT_URL,
        region_name=S3_REGION,
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
        config=_client_config(),
    )


def validate_storage_configuration() -> None:
    if not 1 <= S3_SIGNED_URL_TTL_SECONDS <= 604800:
        raise ValueError("S3_SIGNED_URL_TTL_SECONDS must be between 1 and 604800")
    if STORAGE_BACKEND != "r2":
        return
    errors = []
    endpoint = urlsplit(S3_ENDPOINT_URL)
    if (
        endpoint.scheme != "https"
        or not endpoint.hostname
        or not endpoint.hostname.endswith(".r2.cloudflarestorage.com")
    ):
        errors.append("S3_ENDPOINT_URL must be the Cloudflare R2 S3 API endpoint")
    if S3_REGION != "auto":
        errors.append("S3_REGION must be 'auto'")
    if S3_AUTO_CREATE_BUCKET:
        errors.append("S3_AUTO_CREATE_BUCKET must be false")
    if S3_PUBLIC_BASE_URL:
        errors.append("S3_PUBLIC_BASE_URL must be empty for a private R2 bucket")
    if not AWS_ACCESS_KEY_ID or AWS_ACCESS_KEY_ID == "test":
        errors.append("AWS_ACCESS_KEY_ID must contain the R2 access key ID")
    if not AWS_SECRET_ACCESS_KEY or AWS_SECRET_ACCESS_KEY == "test":
        errors.append("AWS_SECRET_ACCESS_KEY must contain the R2 secret access key")
    if errors:
        raise RuntimeError("Invalid R2 storage configuration: " + "; ".join(errors))


def ensure_bucket():
    validate_storage_configuration()
    client = get_s3_client()

    try:
        client.head_bucket(Bucket=S3_BUCKET_NAME)
        return
    except ClientError as error:
        status_code = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        error_code = str(error.response.get("Error", {}).get("Code", ""))
        is_missing = status_code == 404 or error_code in {
            "404",
            "NoSuchBucket",
            "NotFound",
        }
        if not is_missing:
            raise RuntimeError(
                f"Cannot access configured {STORAGE_BACKEND} bucket "
                f"{S3_BUCKET_NAME!r}"
            ) from error
        if not S3_AUTO_CREATE_BUCKET:
            raise RuntimeError(
                f"Configured {STORAGE_BACKEND} bucket {S3_BUCKET_NAME!r} does not exist; "
                "create it before starting the service"
            ) from error

    if S3_REGION == "us-east-1":
        client.create_bucket(Bucket=S3_BUCKET_NAME)
        return

    client.create_bucket(
        Bucket=S3_BUCKET_NAME,
        CreateBucketConfiguration={
            "LocationConstraint": S3_REGION,
        },
    )


def sanitize_folder(folder: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9/_-]", "", folder).strip("/")
    return cleaned or "travel"


def upload_fileobj_to_key(
    file_obj: BinaryIO,
    key: str,
    content_type: str = "application/octet-stream",
) -> dict:
    normalized_key = key.lstrip("/")
    get_s3_client().upload_fileobj(
        Fileobj=file_obj,
        Bucket=S3_BUCKET_NAME,
        Key=normalized_key,
        ExtraArgs={"ContentType": content_type or "application/octet-stream"},
    )
    save_s3_state_after_upload()
    return {
        "key": normalized_key,
        "contentType": content_type or "application/octet-stream",
    }


def upload_path_to_key(
    path: Path, key: str, content_type: str = "application/octet-stream"
) -> dict:
    with path.open("rb") as file_obj:
        return upload_fileobj_to_key(file_obj, key, content_type)


def download_object_to_path(key: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    get_s3_client().download_file(S3_BUCKET_NAME, key, str(destination))


def get_object(key: str):
    return get_s3_client().get_object(Bucket=S3_BUCKET_NAME, Key=key)


def copy_object(source_key: str, destination_key: str) -> dict:
    normalized_key = destination_key.lstrip("/")
    get_s3_client().copy_object(
        Bucket=S3_BUCKET_NAME,
        CopySource={"Bucket": S3_BUCKET_NAME, "Key": source_key},
        Key=normalized_key,
    )
    save_s3_state_after_upload()
    return {"key": normalized_key}


def delete_object(key: str, bucket_name: str = S3_BUCKET_NAME) -> None:
    get_s3_client().delete_object(Bucket=bucket_name, Key=key)


def delete_prefix(prefix: str) -> int:
    client = get_s3_client()
    deleted = 0
    continuation_token = None
    while True:
        arguments = {"Bucket": S3_BUCKET_NAME, "Prefix": prefix}
        if continuation_token:
            arguments["ContinuationToken"] = continuation_token
        response = client.list_objects_v2(**arguments)
        objects = [{"Key": item["Key"]} for item in response.get("Contents", [])]
        if objects:
            client.delete_objects(
                Bucket=S3_BUCKET_NAME,
                Delete={"Objects": objects, "Quiet": True},
            )
            deleted += len(objects)
        if not response.get("IsTruncated"):
            break
        continuation_token = response.get("NextContinuationToken")
    return deleted


def object_access_url(key: str, bucket_name: str = S3_BUCKET_NAME) -> str:
    normalized_key = key.lstrip("/")
    if STORAGE_BACKEND == "localstack" and S3_PUBLIC_BASE_URL:
        return f"{S3_PUBLIC_BASE_URL}/{bucket_name}/{normalized_key}"
    return get_s3_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket_name, "Key": normalized_key},
        ExpiresIn=S3_SIGNED_URL_TTL_SECONDS,
    )


def object_access_expires_at() -> str | None:
    if STORAGE_BACKEND == "localstack" and S3_PUBLIC_BASE_URL:
        return None
    return (
        datetime.now(UTC) + timedelta(seconds=S3_SIGNED_URL_TTL_SECONDS)
    ).isoformat()


def is_managed_object_url(url: str | None) -> bool:
    if not url:
        return False
    candidate = urlsplit(url)
    if candidate.scheme not in {"http", "https"} or not candidate.netloc:
        return False
    for base_url in (S3_ENDPOINT_URL, S3_PUBLIC_BASE_URL):
        if not base_url:
            continue
        base = urlsplit(base_url)
        if (
            candidate.scheme == base.scheme
            and candidate.netloc == base.netloc
            and candidate.path.startswith(f"{base.path.rstrip('/')}/{S3_BUCKET_NAME}/")
        ):
            return True
    return False


def save_s3_state_after_upload() -> None:
    if STORAGE_BACKEND != "localstack" or not S3_SAVE_STATE_AFTER_UPLOAD:
        return

    request = Request(
        f"{LOCALSTACK_STATE_URL}/_localstack/state/s3/save",
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            response.read()
    except URLError as error:
        logger.warning("Failed to persist LocalStack S3 state: %s", error)
        if S3_STATE_SAVE_STRICT:
            raise RuntimeError("Failed to persist LocalStack S3 state") from error
