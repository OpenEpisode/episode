from __future__ import annotations

import logging
from pathlib import Path

from episode import plugin_api
from episode.config import ExternalPluginConfig
from episode.plugins.external.manifest import (
    MANIFEST_FILENAME,
    SUPPORTED_KINDS,
    ExternalPluginManifest,
    parse_manifest,
)
from episode.plugins.external.runtime import ExternalManagedPlugin
from episode.plugins.models import (
    ManagedPlugin,
    PluginContext,
    PluginIntegration,
    PluginRegistration,
    PluginState,
)

logger = logging.getLogger(__name__)
MAX_PLUGIN_DIRECTORIES = 256


def _unavailable_registration(
    configured: ExternalPluginConfig,
    state: PluginState,
    error: str,
    *,
    name: str | None = None,
    version: str | None = None,
) -> PluginRegistration:
    def unavailable_factory(_context: PluginContext):
        raise RuntimeError("Unavailable plugins must not be loaded")

    return PluginRegistration(
        id=configured.id,
        name=name or configured.id,
        kind="external",
        activation_config_type="",
        factory=unavailable_factory,
        explicitly_enabled=True,
        configured_device_ids=tuple(configured.device_ids),
        installed_version=version,
        unavailable_state=state,
        unavailable_error=error,
    )


def _installed_manifests(
    plugins_dir: Path,
    enabled: list[ExternalPluginConfig],
) -> tuple[dict[str, ExternalPluginManifest], dict[str, str]] | list[PluginRegistration]:
    manifests: dict[str, ExternalPluginManifest] = {}
    failures: dict[str, str] = {}
    duplicate_ids: set[str] = set()
    if not plugins_dir.is_dir():
        return manifests, failures
    try:
        roots = sorted(path for path in plugins_dir.iterdir() if path.is_dir())
        resolved_plugins_dir = plugins_dir.resolve(strict=True)
    except OSError as error:
        logger.warning("Configured plugin directory %s cannot be read: %s", plugins_dir, error)
        return [
            _unavailable_registration(
                configured,
                PluginState.INCOMPLETE,
                "The configured plugin directory cannot be read.",
            )
            for configured in enabled
        ]
    for index, root in enumerate(roots):
        if index >= MAX_PLUGIN_DIRECTORIES:
            logger.warning(
                "Ignoring plugin directories after the %d-entry limit",
                MAX_PLUGIN_DIRECTORIES,
            )
            break
        if not (root / MANIFEST_FILENAME).is_file():
            continue
        try:
            root.resolve(strict=True).relative_to(resolved_plugins_dir)
            (root / MANIFEST_FILENAME).resolve(strict=True).relative_to(root.resolve(strict=True))
        except (OSError, ValueError):
            failures[root.name] = "plugin manifest escapes the configured plugin root"
            logger.warning("Ignoring plugin directory outside %s: %s", plugins_dir, root)
            continue
        try:
            manifest = parse_manifest(root)
        except (OSError, ValueError) as error:
            failures[root.name] = str(error)
            logger.warning("Ignoring invalid plugin manifest in %s: %s", root, error)
            continue
        if manifest.id in manifests or manifest.id in duplicate_ids:
            failures[manifest.id] = "more than one installed manifest uses this plugin id"
            manifests.pop(manifest.id, None)
            duplicate_ids.add(manifest.id)
            continue
        manifests[manifest.id] = manifest
    return manifests, failures


def _registration(
    configured: ExternalPluginConfig,
    manifest: ExternalPluginManifest,
) -> PluginRegistration:
    def factory(context: PluginContext) -> ManagedPlugin:
        return ExternalManagedPlugin(manifest, configured, context)

    return PluginRegistration(
        id=manifest.id,
        name=manifest.name,
        kind=manifest.kind,
        activation_config_type="",
        factory=factory,
        integration=PluginIntegration(
            type=manifest.id,
            name=manifest.name,
            device_scoped=manifest.kind == "device",
            capabilities=manifest.capabilities,
            manufacturer_scope=manifest.manufacturer_scope,
            manufacturer_scope_kind=manifest.manufacturer_scope_kind,
            device_types=manifest.device_types,
        ),
        explicitly_enabled=True,
        configured_device_ids=tuple(configured.device_ids),
        installed_version=manifest.version,
    )


def external_plugin_catalog(plugins_dir: Path) -> list[dict[str, object]]:
    """Read installed manifests for onboarding without importing plugin code.

    Entries from this catalogue are informational until the plugin is
    explicitly enabled in configuration.  In particular, callers must not
    treat them as validation or activation permissions.
    """
    installed = _installed_manifests(plugins_dir, [])
    if isinstance(installed, list):
        return []
    manifests, _failures = installed
    return [
        {
            "id": manifest.id,
            "name": manifest.name,
            "type": manifest.id,
            "kind": manifest.kind,
            "capabilities": list(manifest.capabilities),
            "manufacturer_scope": list(manifest.manufacturer_scope),
            "manufacturer_scope_kind": manifest.manufacturer_scope_kind,
            "device_types": list(manifest.device_types),
            "configured": False,
            "available": manifest.plugin_api == plugin_api.PLUGIN_API_VERSION,
            "selection_required": True,
            "activation_required": True,
        }
        for manifest in sorted(manifests.values(), key=lambda value: value.id)
        if manifest.kind == "device"
    ]


def discover_external_plugins(
    plugins_dir: Path,
    configurations: tuple[ExternalPluginConfig, ...] | list[ExternalPluginConfig],
) -> list[PluginRegistration]:
    """Discover manifests without importing plugin code; return only enabled entries."""
    enabled = [configured for configured in configurations if configured.enabled]
    if not enabled:
        return []

    installed = _installed_manifests(plugins_dir, enabled)
    if isinstance(installed, list):
        return installed
    manifests, failures = installed

    registrations: list[PluginRegistration] = []
    for configured in enabled:
        if configured.id in failures:
            registrations.append(
                _unavailable_registration(
                    configured,
                    PluginState.INCOMPLETE,
                    f"Plugin manifest is invalid: {failures[configured.id]}",
                )
            )
            continue
        manifest = manifests.get(configured.id)
        if manifest is None:
            registrations.append(
                _unavailable_registration(
                    configured,
                    PluginState.NOT_INSTALLED,
                    f"No {MANIFEST_FILENAME} was found for this configured plugin.",
                )
            )
            continue
        if manifest.plugin_api != plugin_api.PLUGIN_API_VERSION:
            registrations.append(
                _unavailable_registration(
                    configured,
                    PluginState.INCOMPATIBLE,
                    (
                        f"Plugin requires API {manifest.plugin_api}; "
                        f"Episode provides API {plugin_api.PLUGIN_API_VERSION}."
                    ),
                    name=manifest.name,
                    version=manifest.version,
                )
            )
            continue
        if manifest.kind not in SUPPORTED_KINDS:
            registrations.append(
                _unavailable_registration(
                    configured,
                    PluginState.INCOMPATIBLE,
                    f"Plugin kind {manifest.kind!r} is reserved but not supported yet.",
                    name=manifest.name,
                    version=manifest.version,
                )
            )
            continue
        if not manifest.entrypoint_file.is_file():
            registrations.append(
                _unavailable_registration(
                    configured,
                    PluginState.INCOMPLETE,
                    f"Plugin entrypoint {manifest.entrypoint_file.name!r} is missing.",
                    name=manifest.name,
                    version=manifest.version,
                )
            )
            continue
        registrations.append(_registration(configured, manifest))
    return registrations
