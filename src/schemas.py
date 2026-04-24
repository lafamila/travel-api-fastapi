from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from .services.course_contract import OUTPUT_FORMAT_VERSION

TravelPlaceCategory = Literal[
    "food",
    "coffee",
    "bar",
    "culture",
    "nature",
    "shopping",
    "stay",
    "other",
]


class TravelReview(BaseModel):
    id: str
    placeId: str
    rating: int = Field(ge=1, le=5)
    headline: str | None = None
    body: str
    visitedAt: str | None = None
    photoUrls: list[str] = Field(default_factory=list)
    createdAt: str
    updatedAt: str


class TravelReviewCreateRequest(BaseModel):
    rating: int = Field(ge=1, le=5)
    headline: str | None = None
    body: str
    visitedAt: str | None = None
    photoUrls: list[str] = Field(default_factory=list)


class TravelPlaceBase(BaseModel):
    name: str
    category: TravelPlaceCategory = "other"
    latitude: float
    longitude: float
    address: str | None = None
    description: str | None = None
    openingHours: str | None = None
    specialNotes: str | None = None
    coverImageUrl: str | None = None
    photoUrls: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class TravelPlaceCreateRequest(TravelPlaceBase):
    pass


class ResolveGoogleMapsLinkRequest(BaseModel):
    url: str


class TravelPlaceUpdateRequest(BaseModel):
    name: str | None = None
    category: TravelPlaceCategory | None = None
    latitude: float | None = None
    longitude: float | None = None
    address: str | None = None
    description: str | None = None
    openingHours: str | None = None
    specialNotes: str | None = None
    coverImageUrl: str | None = None
    photoUrls: list[str] | None = None
    tags: list[str] | None = None


class TravelPlace(TravelPlaceBase):
    id: str
    createdAt: str
    updatedAt: str
    reviews: list[TravelReview] = Field(default_factory=list)


class GoogleMapsLinkResolution(BaseModel):
    resolvedUrl: str
    googlePlaceId: str | None = None
    googleMapsUri: str | None = None
    name: str
    address: str | None = None
    latitude: float
    longitude: float
    openingHours: str | None = None
    primaryType: str | None = None


class TripWindow(BaseModel):
    startAt: str
    endAt: str


class CourseStart(BaseModel):
    label: str | None = None
    latitude: float | None = None
    longitude: float | None = None


class SelectedPlace(BaseModel):
    placeId: str
    name: str
    latitude: float
    longitude: float
    address: str | None = None
    openingHours: str | None = None
    specialNotes: str | None = None
    coverImageUrl: str | None = None
    reviewSummary: list[str] = Field(default_factory=list)


class CourseExportRequest(BaseModel):
    tripWindow: TripWindow
    courseStart: CourseStart
    selectedPlaces: list[SelectedPlace]
    selectionContext: dict[str, Any] = Field(default_factory=dict)


class CourseExportResponse(BaseModel):
    outputFormatVersion: str = OUTPUT_FORMAT_VERSION
    payload: dict[str, Any]
    promptText: str


class TravelCourseStop(BaseModel):
    placeId: str
    placeName: str
    order: int
    scheduledAt: str | None = None
    note: str | None = None
    reasoningText: str | None = None
    transitHint: str | None = None


class TravelCourse(BaseModel):
    id: str
    title: str
    startLocation: str | None = None
    tripStartAt: str | None = None
    tripEndAt: str | None = None
    transportMode: str | None = None
    summary: str | None = None
    promptText: str | None = None
    outputFormatVersion: str = OUTPUT_FORMAT_VERSION
    stops: list[TravelCourseStop] = Field(default_factory=list)
    createdAt: str
    updatedAt: str


class TravelCourseCreateRequest(BaseModel):
    title: str
    startLocation: str | None = None
    tripStartAt: str | None = None
    tripEndAt: str | None = None
    transportMode: str | None = None
    summary: str | None = None
    promptText: str | None = None
    outputFormatVersion: str = OUTPUT_FORMAT_VERSION
    stops: list[TravelCourseStop]


class CourseImportPayload(BaseModel):
    outputFormatVersion: str = OUTPUT_FORMAT_VERSION
    course: dict[str, Any]
    validation: dict[str, Any] = Field(default_factory=dict)

    @field_validator("outputFormatVersion")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if value != OUTPUT_FORMAT_VERSION:
            raise ValueError("Unsupported outputFormatVersion")
        return value
