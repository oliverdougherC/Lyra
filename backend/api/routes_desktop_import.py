"""Safe packaged-data import endpoints for the desktop migration flow."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator

from backend.desktop_import import (
    ImportAssetSummary,
    ImportPreview,
    ImportStatus,
    desktop_import_manager,
)

router = APIRouter(prefix="/api/desktop-import", tags=["desktop-import"])


class DesktopImportPreviewRequest(BaseModel):
    selection_token: str = Field(min_length=1, max_length=200)

    @field_validator("selection_token")
    @classmethod
    def _trim_path(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("Pick a folder to import.")
        return trimmed


class DesktopImportStartRequest(DesktopImportPreviewRequest):
    operation_id: str = Field(min_length=1, max_length=200)

    @field_validator("operation_id")
    @classmethod
    def _trim_operation_id(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("The import request is missing its operation id.")
        return trimmed


class DesktopImportPreviewRead(BaseModel):
    source_name: str
    source_kind: str
    class_count: int
    document_count: int
    total_entries: int
    total_bytes: int
    sample_entries: list[str]
    warnings: list[str]
    schema_version: int | None = None
    database_identity: str | None = None
    conflicts: list[str] = []
    asset_summary: DesktopImportAssetSummaryRead | None = None
    old_runtime_active: bool | None = None
    source_lock: str | None = None


class DesktopImportAssetSummaryRead(BaseModel):
    selected_models: int
    selected_model_bytes: int
    selected_caches: int
    selected_cache_bytes: int
    preserved_models: int
    preserved_model_bytes: int
    preserved_caches: int
    preserved_cache_bytes: int


class DesktopImportStatusRead(BaseModel):
    available: bool
    destination_ready: bool
    status: str
    phase: str | None
    message: str | None
    source_name: str | None
    copied_entries: int
    total_entries: int
    copied_bytes: int
    total_bytes: int
    cancel_requested: bool
    can_resume: bool
    requires_restart: bool
    preview: DesktopImportPreviewRead | None
    schema_version: int | None = None
    database_identity: str | None = None
    conflicts: list[str] = []
    asset_summary: DesktopImportAssetSummaryRead | None = None
    old_runtime_active: bool | None = None
    source_lock: str | None = None


@router.get("/status", response_model=DesktopImportStatusRead)
def read_import_status() -> DesktopImportStatusRead:
    return _status_read(desktop_import_manager.status())


@router.post("/preview", response_model=DesktopImportPreviewRead)
def preview_import(payload: DesktopImportPreviewRequest) -> DesktopImportPreviewRead:
    return _preview_read(desktop_import_manager.preview(payload.selection_token))


@router.post("/start", response_model=DesktopImportStatusRead)
def start_import(payload: DesktopImportStartRequest) -> DesktopImportStatusRead:
    return _status_read(desktop_import_manager.start(payload.selection_token, payload.operation_id))


@router.post("/cancel", response_model=DesktopImportStatusRead)
def cancel_import() -> DesktopImportStatusRead:
    return _status_read(desktop_import_manager.cancel())


@router.post("/reset", response_model=DesktopImportStatusRead)
def reset_import() -> DesktopImportStatusRead:
    return _status_read(desktop_import_manager.reset())


def _preview_read(preview: ImportPreview) -> DesktopImportPreviewRead:
    return DesktopImportPreviewRead(
        source_name=preview.source_name,
        source_kind=preview.source_kind,
        class_count=preview.class_count,
        document_count=preview.document_count,
        total_entries=preview.total_entries,
        total_bytes=preview.total_bytes,
        sample_entries=list(preview.sample_entries),
        warnings=list(preview.warnings),
        schema_version=preview.schema_version,
        database_identity=preview.database_identity,
        conflicts=list(preview.conflicts),
        asset_summary=_asset_summary_read(preview.asset_summary),
        old_runtime_active=preview.old_runtime_active,
        source_lock=preview.source_lock,
    )


def _status_read(status: ImportStatus) -> DesktopImportStatusRead:
    return DesktopImportStatusRead(
        available=status.available,
        destination_ready=status.destination_ready,
        status=status.status,
        phase=status.phase,
        message=status.message,
        source_name=status.source_name,
        copied_entries=status.copied_entries,
        total_entries=status.total_entries,
        copied_bytes=status.copied_bytes,
        total_bytes=status.total_bytes,
        cancel_requested=status.cancel_requested,
        can_resume=status.can_resume,
        requires_restart=status.requires_restart,
        preview=_preview_read(status.preview) if status.preview is not None else None,
        schema_version=status.schema_version,
        database_identity=status.database_identity,
        conflicts=list(status.conflicts),
        asset_summary=_asset_summary_read(status.asset_summary),
        old_runtime_active=status.old_runtime_active,
        source_lock=status.source_lock,
    )


def _asset_summary_read(summary: ImportAssetSummary | None) -> DesktopImportAssetSummaryRead | None:
    if summary is None:
        return None
    return DesktopImportAssetSummaryRead(
        selected_models=summary.selected_models,
        selected_model_bytes=summary.selected_model_bytes,
        selected_caches=summary.selected_caches,
        selected_cache_bytes=summary.selected_cache_bytes,
        preserved_models=summary.preserved_models,
        preserved_model_bytes=summary.preserved_model_bytes,
        preserved_caches=summary.preserved_caches,
        preserved_cache_bytes=summary.preserved_cache_bytes,
    )
