from __future__ import annotations

import os
from dataclasses import asdict, is_dataclass

from episode.api.schemas import (
    EpisodeResponse,
    EventResponse,
    EvidenceResponse,
    IngestionReceiptResponse,
)


def _item_data(item) -> dict:
    if isinstance(item, dict):
        return dict(item)
    if is_dataclass(item):
        return asdict(item)
    if hasattr(item, "model_dump"):
        return dict(item.model_dump())
    return dict(vars(item))


def _receipt_source(receipt) -> str | None:
    data = _item_data(receipt)
    metadata = data.get("metadata", {})
    metadata = metadata if isinstance(metadata, dict) else {}
    transport = metadata.get("transport")
    receipt_source = data.get("source")
    source = metadata.get("interpretation_source")
    if (
        not source
        and transport == "plugin"
        and isinstance(receipt_source, str)
        and not receipt_source.startswith("plugin:")
    ):
        source = receipt_source
    if not source and not transport:
        source = receipt_source
    return str(source) if source else None


def semantic_receipt_sources(receipts) -> list[str]:
    """Return integration identities without exposing generic transport labels."""
    sources: list[str] = []
    for receipt in receipts:
        source = _receipt_source(receipt)
        if source and source not in sources:
            sources.append(source)
    return sources


def _claimed_handler(metadata: object, handler_id: str) -> bool:
    if not isinstance(metadata, dict):
        return False
    handlers = metadata.get("ingress_handlers", [])
    return any(
        isinstance(handler, dict)
        and handler.get("id") == handler_id
        and handler.get("state") == "claimed"
        for handler in handlers
    )


def _fallback_name(identifier: str) -> str:
    acronyms = {"api": "API", "ftp": "FTP", "http": "HTTP", "isapi": "ISAPI", "sdk": "SDK"}
    return " ".join(
        acronyms.get(part.lower(), part.capitalize())
        for part in identifier.replace(":", "-").replace("_", "-").split("-")
        if part
    )


def event_origins(
    event_source: str | None,
    receipts,
    integrations: list[dict] | None = None,
    event_metadata: dict | None = None,
) -> tuple[list[str], list[dict[str, str]]]:
    sources: list[str] = []
    for source in [event_source, *semantic_receipt_sources(receipts)]:
        if source and source not in sources:
            sources.append(source)

    catalog = integrations or []
    by_id = {str(item.get("id")): item for item in catalog if item.get("id")}
    by_type = {str(item.get("type")): item for item in catalog if item.get("type")}
    event_receipt_handler = str((event_metadata or {}).get("ingress_handler") or "")
    receipt_data = [_item_data(receipt) for receipt in receipts]

    origins: list[dict[str, str]] = []
    for source in sources:
        matching = [item for item in receipt_data if _receipt_source(item) == source]
        if source == event_source:
            # The canonical handler identifies its receipt even when an older plugin
            # delivery only stored a generic plugin:* source.
            for item in receipt_data:
                metadata = item.get("metadata", {})
                if isinstance(metadata, dict) and metadata.get("interpretation_source") == source:
                    matching.insert(0, item)
            if not matching and event_receipt_handler:
                matching = [
                    item
                    for item in receipt_data
                    if _claimed_handler(item.get("metadata", {}), event_receipt_handler)
                ]

        metadata = matching[0].get("metadata", {}) if matching else {}
        metadata = metadata if isinstance(metadata, dict) else {}
        plugin_id = metadata.get("plugin_id")
        if not plugin_id and source.startswith("plugin:"):
            plugin_id = source.removeprefix("plugin:")

        if plugin_id:
            plugin_id = str(plugin_id)
            integration = by_id.get(plugin_id, {})
            origins.append(
                {
                    "kind": "plugin",
                    "id": plugin_id,
                    "name": str(integration.get("name") or _fallback_name(plugin_id)),
                    "source": source,
                }
            )
            continue

        if source.startswith("event-api:"):
            integration = by_type.get("event_api", {})
            origins.append(
                {
                    "kind": "connector",
                    "id": "event_api",
                    "name": str(integration.get("name") or "Event API"),
                    "source": source,
                }
            )
            continue

        kind = "core" if source.startswith("core:") or source == "manual" else "external"
        origins.append(
            {
                "kind": kind,
                "id": source,
                "name": _fallback_name(source),
                "source": source,
            }
        )
    return sources, origins


def public_event(event, receipts=(), integrations: list[dict] | None = None) -> EventResponse:
    data = _item_data(event)
    source = data.pop("source", None)
    existing_sources = list(data.pop("sources", []))
    sources, origins = event_origins(source, receipts, integrations, data.get("metadata"))
    for existing in existing_sources:
        if existing not in sources:
            sources.append(existing)
    data["sources"] = sources
    data["origins"] = origins
    data["has_raw_payload"] = bool(data.pop("raw_payload_path", None))
    return EventResponse.model_validate(data)


def public_receipt(receipt) -> IngestionReceiptResponse:
    data = asdict(receipt) if not isinstance(receipt, dict) else dict(receipt)
    data["has_artifact"] = bool(data.get("artifact_id"))
    metadata = data.get("metadata", {})
    if isinstance(metadata, dict):
        data["transport"] = metadata.get("transport")
        data["reason"] = metadata.get("reason")
    return IngestionReceiptResponse.model_validate(data)


def public_evidence(evidence) -> EvidenceResponse:
    data = asdict(evidence) if not isinstance(evidence, dict) else dict(evidence)
    data.pop("file_path", None)
    return EvidenceResponse.model_validate(data)


def event_annotations(event) -> tuple[dict[str, float] | None, str | None]:
    metadata = event.get("metadata", {}) if isinstance(event, dict) else event.metadata
    if not isinstance(metadata, dict):
        return None, None
    bounding_box = metadata.get("bounding_box")
    target_type = metadata.get("target_type")
    return (
        bounding_box if isinstance(bounding_box, dict) else None,
        target_type if isinstance(target_type, str) else None,
    )


def event_embedded_picture(event) -> dict[str, object] | None:
    metadata = event.get("metadata", {}) if isinstance(event, dict) else event.metadata
    if not isinstance(metadata, dict):
        return None

    descriptor = metadata.get("embedded_picture")
    if isinstance(descriptor, dict):
        picture = dict(descriptor)
    else:
        return None

    offset = picture.get("offset")
    byte_size = picture.get("byte_size")
    mime_type = picture.get("mime_type")
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or offset < 0
        or isinstance(byte_size, bool)
        or not isinstance(byte_size, int)
        or byte_size <= 0
        or not isinstance(mime_type, str)
        or not mime_type.startswith("image/")
    ):
        return None

    supplied_name = os.path.basename(str(picture.get("filename") or "event-picture.jpg"))
    filename = (
        "".join(
            character
            if character.isascii() and (character.isalnum() or character in ".-_")
            else "_"
            for character in supplied_name
        ).strip("._")
        or "event-picture.jpg"
    )
    checksum = picture.get("sha256")
    if not (
        isinstance(checksum, str)
        and len(checksum) == 64
        and all(character in "0123456789abcdefABCDEF" for character in checksum)
    ):
        checksum = None
    return {
        "offset": offset,
        "byte_size": byte_size,
        "mime_type": mime_type,
        "filename": filename,
        "sha256": checksum,
    }


def episode_trigger_type(event_type: str | None) -> str | None:
    normalized = (event_type or "").lower()
    if normalized == "doorbell":
        return "doorbell"
    if normalized in {"manual", "manual_trigger"}:
        return "manual"
    if "motion" in normalized or normalized in {
        "human_detection",
        "vehicle_detection",
        "linedetection",
        "fielddetection",
    }:
        return "motion"
    return None


def public_episode(episode, trigger_event_type: str | None = None) -> EpisodeResponse:
    data = asdict(episode) if not isinstance(episode, dict) else dict(episode)
    data["trigger_type"] = episode_trigger_type(trigger_event_type)
    return EpisodeResponse.model_validate(data)
