from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

MANIFEST_VERSION = 3


def bundle_dir(data_root: str, episode_id: str) -> str:
    return os.path.join(data_root, "episodes", episode_id)


def relative_bundle_path(data_root: str, episode_id: str, path: str | None) -> str | None:
    if not path:
        return None
    root = os.path.abspath(bundle_dir(data_root, episode_id))
    candidate = os.path.abspath(path)
    try:
        if os.path.commonpath((root, candidate)) != root:
            return None
    except ValueError:
        return None
    return os.path.relpath(candidate, root)


def write_manifest(data_root: str, episode_id: str, manifest: dict[str, Any]) -> str:
    root = bundle_dir(data_root, episode_id)
    os.makedirs(root, exist_ok=True)
    destination = os.path.join(root, "manifest.json")
    document = {
        "schema_version": MANIFEST_VERSION,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        **manifest,
    }
    fd, temporary = tempfile.mkstemp(prefix=".manifest.", suffix=".tmp", dir=root, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return destination


def append_journal(
    data_root: str,
    episode_id: str,
    entry_type: str,
    data: dict[str, Any] | None = None,
) -> str:
    root = bundle_dir(data_root, episode_id)
    os.makedirs(root, exist_ok=True)
    destination = os.path.join(root, "journal.ndjson")
    entry = {
        "at": datetime.now(tz=timezone.utc).isoformat(),
        "type": entry_type,
        "data": data or {},
    }
    with open(destination, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return destination
