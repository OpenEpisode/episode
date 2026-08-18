from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

MANIFEST_FILENAME = "episode-plugin.json"
MAX_MANIFEST_BYTES = 64 * 1024
SUPPORTED_KINDS = {"device", "ingress"}
_KNOWN_KINDS = {*SUPPORTED_KINDS, "action", "processor"}
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class ExternalPluginManifest:
    root: Path
    id: str
    name: str
    version: str
    plugin_api: str
    kind: str
    entrypoint_file: Path
    entrypoint_symbol: str
    capabilities: tuple[str, ...]
    configuration_schema: Mapping[str, object]


def _required_string(document: Mapping[str, object], field: str) -> str:
    value = document.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"manifest field {field!r} must be a non-empty string")
    return value.strip()


def parse_manifest(root: Path) -> ExternalPluginManifest:
    path = root / MANIFEST_FILENAME
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("manifest exceeds the 64 KiB safety limit")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(
            f"manifest is not valid UTF-8 JSON ({error.__class__.__name__})"
        ) from error
    if not isinstance(document, dict):
        raise ValueError("manifest must contain a JSON object")
    if document.get("schema_version") != 1:
        raise ValueError("manifest schema_version must be 1")

    plugin_id = _required_string(document, "id")
    if not _IDENTIFIER.fullmatch(plugin_id):
        raise ValueError("manifest id must use lowercase letters, numbers, dots, _ or -")
    name = _required_string(document, "name")
    version = _required_string(document, "version")
    plugin_api = _required_string(document, "plugin_api")
    kind = _required_string(document, "kind")
    if kind not in _KNOWN_KINDS:
        raise ValueError(f"manifest kind {kind!r} is not recognized")

    entrypoint = _required_string(document, "entrypoint")
    file_value, separator, symbol = entrypoint.partition(":")
    if not separator or not file_value or not symbol or not symbol.isidentifier():
        raise ValueError("manifest entrypoint must use 'relative/file.py:function'")
    relative_file = Path(file_value)
    if relative_file.is_absolute() or ".." in relative_file.parts or relative_file.suffix != ".py":
        raise ValueError("manifest entrypoint must be a relative Python file inside the plugin")
    entrypoint_file = (root / relative_file).resolve()
    try:
        entrypoint_file.relative_to(root.resolve())
    except ValueError as error:
        raise ValueError("manifest entrypoint escapes the plugin directory") from error

    capabilities = document.get("capabilities", [])
    if not isinstance(capabilities, list) or not all(
        isinstance(value, str) and value for value in capabilities
    ):
        raise ValueError("manifest capabilities must be an array of non-empty strings")
    configuration_schema = document.get("configuration_schema", {})
    if not isinstance(configuration_schema, dict):
        raise ValueError("manifest configuration_schema must be an object")

    return ExternalPluginManifest(
        root=root,
        id=plugin_id,
        name=name,
        version=version,
        plugin_api=plugin_api,
        kind=kind,
        entrypoint_file=entrypoint_file,
        entrypoint_symbol=symbol,
        capabilities=tuple(capabilities),
        configuration_schema=configuration_schema,
    )
