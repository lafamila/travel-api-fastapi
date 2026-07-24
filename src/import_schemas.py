from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .schemas import TravelPlaceCategory

ImportSourceType = Literal["local", "upload"]
ImportAssetRole = Literal["cover", "gallery", "review", "excluded"]
ImportExclusionReason = Literal[
    "raw",
    "video",
    "missing_gps",
    "screenshot",
    "duplicate",
    "unsupported",
    "other",
]
ImportPublishAction = Literal["create", "merge", "skip"]


class ImportBatchCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    sourceType: ImportSourceType
    localRelativePath: str | None = None

    @model_validator(mode="after")
    def validate_source(self):
        if self.sourceType == "local" and not (self.localRelativePath or "").strip():
            raise ValueError("localRelativePath is required for a local import")
        if self.sourceType == "upload" and self.localRelativePath:
            raise ValueError("localRelativePath is only valid for a local import")
        return self


class ImportReviewDraftFields(BaseModel):
    rating: int | None = Field(default=None, ge=1, le=5)
    headline: str | None = Field(default=None, max_length=255)
    body: str | None = Field(default=None, max_length=65535)
    visitedAt: datetime | None = None


def _validate_review_asset_ids(asset_ids: list[str]) -> list[str]:
    if any(not asset_id.strip() or len(asset_id) > 50 for asset_id in asset_ids):
        raise ValueError("assetIds must contain non-empty IDs of at most 50 characters")
    if len(set(asset_ids)) != len(asset_ids):
        raise ValueError("assetIds must not contain duplicates")
    return asset_ids


class ImportReviewDraftCreateRequest(ImportReviewDraftFields):
    assetIds: list[str]

    @model_validator(mode="after")
    def validate_asset_ids(self):
        _validate_review_asset_ids(self.assetIds)
        return self


class ImportReviewDraftPatchRequest(ImportReviewDraftFields):
    assetIds: list[str] | None = None

    @model_validator(mode="after")
    def validate_asset_ids(self):
        if self.assetIds is not None:
            _validate_review_asset_ids(self.assetIds)
        return self


class ImportAssetPatchRequest(BaseModel):
    role: ImportAssetRole | None = None
    exclusionReason: ImportExclusionReason | None = None
    clusterId: str | None = Field(default=None, max_length=50)


class ImportClusterDraftPatchRequest(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    category: TravelPlaceCategory | None = None
    address: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=65535)
    openingHours: str | None = Field(default=None, max_length=65535)
    specialNotes: str | None = Field(default=None, max_length=65535)
    tags: list[str] | None = None
    visibility: Literal["public", "private"] | None = None
    publishAction: ImportPublishAction | None = None
    existingPlaceId: str | None = Field(default=None, max_length=50)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    representativeAssetId: str | None = Field(default=None, max_length=50)
    mapLink: str | None = Field(default=None, max_length=2000)


class ImportAssetIdsRequest(BaseModel):
    assetIds: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_asset_ids(self):
        if any(
            not asset_id.strip() or len(asset_id) > 50 for asset_id in self.assetIds
        ):
            raise ValueError(
                "assetIds must contain non-empty IDs of at most 50 characters"
            )
        if len(set(self.assetIds)) != len(self.assetIds):
            raise ValueError("assetIds must not contain duplicates")
        return self


class ImportClusterCreateRequest(ImportClusterDraftPatchRequest):
    assetIds: list[str] = Field(min_length=1)
    visibility: Literal["public", "private"] | None = "public"
    publishAction: ImportPublishAction | None = "create"

    @model_validator(mode="after")
    def validate_new_cluster(self):
        if any(
            not asset_id.strip() or len(asset_id) > 50 for asset_id in self.assetIds
        ):
            raise ValueError(
                "assetIds must contain non-empty IDs of at most 50 characters"
            )
        if len(set(self.assetIds)) != len(self.assetIds):
            raise ValueError("assetIds must not contain duplicates")
        has_latitude = self.latitude is not None
        has_longitude = self.longitude is not None
        if has_latitude != has_longitude:
            raise ValueError("latitude and longitude must be supplied together")
        if not (self.mapLink or "").strip() and not has_latitude:
            raise ValueError("mapLink or latitude and longitude are required")
        if self.publishAction == "merge" and not self.existingPlaceId:
            raise ValueError("existingPlaceId is required for merge")
        return self


class ImportClusterMergeRequest(BaseModel):
    clusterIds: list[str] = Field(min_length=2)


class ImportClusterSplitRequest(BaseModel):
    clusterId: str
    assetIds: list[str] = Field(min_length=1)
