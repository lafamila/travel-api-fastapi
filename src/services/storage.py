from __future__ import annotations

import os
import re
import uuid
from typing import BinaryIO

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "http://localhost:4566")
S3_PUBLIC_BASE_URL = os.getenv("S3_PUBLIC_BASE_URL", "http://localhost:4566")
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "teddy-travel-media")
S3_REGION = os.getenv("S3_REGION", "ap-northeast-2")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID", "test")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "test")


def get_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT_URL,
        region_name=S3_REGION,
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
        config=Config(s3={"addressing_style": "path"}),
    )


def ensure_bucket():
    client = get_s3_client()

    try:
        client.head_bucket(Bucket=S3_BUCKET_NAME)
        return
    except ClientError:
        pass

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


def upload_image(file_obj: BinaryIO, filename: str, content_type: str, folder: str):
    extension = os.path.splitext(filename)[1].lower()
    key = f"{sanitize_folder(folder)}/{uuid.uuid4().hex}{extension}"

    client = get_s3_client()
    client.upload_fileobj(
        Fileobj=file_obj,
        Bucket=S3_BUCKET_NAME,
        Key=key,
        ExtraArgs={
            "ContentType": content_type or "application/octet-stream",
        },
    )

    public_base = S3_PUBLIC_BASE_URL.rstrip("/")
    return {
        "key": key,
        "url": f"{public_base}/{S3_BUCKET_NAME}/{key}",
        "contentType": content_type or "application/octet-stream",
        "filename": filename,
    }
