from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, UploadFile, status

from ..auth_utils import get_current_user
from ..services.storage import upload_image

router = APIRouter(prefix="/api/uploads", tags=["uploads"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def upload_files(
    files: list[UploadFile] = File(...),
    folder: str = Form("travel"),
    user: dict = Depends(get_current_user),
):
    _ = user
    uploaded = []
    for file in files:
        uploaded.append(
            upload_image(
                file_obj=file.file,
                filename=file.filename or "upload.bin",
                content_type=file.content_type or "application/octet-stream",
                folder=folder,
            )
        )

    return uploaded
