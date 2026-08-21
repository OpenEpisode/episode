from __future__ import annotations

import importlib
from collections.abc import Iterable, Mapping

from episode.plugins.models import (
    PluginContext,
    PluginDeviceValidator,
    PluginFactory,
    PluginIntegration,
    PluginRegistration,
)


class PluginRegistry:
    def __init__(self, registrations: Iterable[PluginRegistration] = ()):
        self._registrations: dict[str, PluginRegistration] = {}
        for registration in registrations:
            self.register(registration)

    def register(self, registration: PluginRegistration) -> None:
        if registration.id in self._registrations:
            raise ValueError(f"Plugin {registration.id!r} is already registered")
        self._registrations[registration.id] = registration

    def for_configuration(
        self,
        device_config_types: Iterable[str],
        connector_types: Iterable[str] = (),
    ) -> list[PluginRegistration]:
        configured = set(device_config_types)
        configured_connectors = set(connector_types)
        return [
            registration
            for registration in self._registrations.values()
            if (
                registration.explicitly_enabled
                or registration.activation_config_type in configured
                or (
                    registration.activation_connector_type
                    and registration.activation_connector_type in configured_connectors
                )
            )
        ]

    def validators(self) -> Mapping[str, PluginDeviceValidator]:
        return {
            registration.validation_capability: registration.validator
            for registration in self._registrations.values()
            if registration.validation_capability and registration.validator is not None
        }

    def device_integrations(self) -> tuple[PluginRegistration, ...]:
        return tuple(
            registration
            for registration in self._registrations.values()
            if registration.integration and registration.integration.device_scoped
        )


def module_plugin_factory(module_name: str) -> PluginFactory:
    def create(context: PluginContext):
        module = importlib.import_module(module_name)
        return module.create_plugin(context)

    return create


def module_plugin_validator(
    module_name: str,
    function_name: str = "validate_device",
) -> PluginDeviceValidator:
    async def validate(device: object, checked_at: str, timeout: float):
        module = importlib.import_module(module_name)
        validator = getattr(module, function_name)
        return await validator(device, checked_at, timeout)

    return validate


def builtin_plugin_registry() -> PluginRegistry:
    return PluginRegistry(
        [
            PluginRegistration(
                id="onvif",
                name="ONVIF",
                kind="device-integration",
                activation_config_type="onvif",
                factory=module_plugin_factory("episode.plugins.onvif"),
                validation_capability="onvif",
                validator=module_plugin_validator("episode.plugins.onvif.validation"),
                integration=PluginIntegration(
                    type="onvif",
                    name="ONVIF",
                    device_scoped=True,
                    capabilities=("discovery", "media"),
                ),
            ),
            PluginRegistration(
                id="hikvision-sdk",
                name="Hikvision HCNetSDK",
                kind="native-sdk",
                activation_config_type="hikvision_sdk",
                factory=module_plugin_factory("episode.plugins.hikvision.sdk"),
                validation_capability="hikvision_sdk",
                integration=PluginIntegration(
                    type="hikvision_sdk",
                    name="Hikvision HCNetSDK",
                    device_scoped=True,
                    capabilities=("events", "device-information"),
                ),
            ),
            PluginRegistration(
                id="hikvision-isapi",
                name="Hikvision ISAPI",
                kind="device-integration",
                activation_config_type="isapi",
                factory=module_plugin_factory("episode.plugins.hikvision.isapi"),
                validation_capability="isapi",
                validator=module_plugin_validator("episode.plugins.hikvision.isapi.validation"),
                integration=PluginIntegration(
                    type="isapi",
                    name="Hikvision ISAPI",
                    device_scoped=True,
                    capabilities=("events",),
                ),
            ),
            PluginRegistration(
                id="hikvision-alarm-server",
                name="Hikvision Alarm Server",
                kind="ingress-handler",
                activation_config_type="",
                activation_connector_type="alarm_server",
                factory=module_plugin_factory("episode.plugins.hikvision.alarm_server"),
                integration=PluginIntegration(
                    type="hikvision_alarm_server",
                    name="Hikvision Alarm Server",
                    capabilities=("event-interpretation",),
                ),
            ),
            PluginRegistration(
                id="hikvision-ftp",
                name="Hikvision FTP snapshots",
                kind="file-ingress-handler",
                activation_config_type="",
                activation_connector_type="ftp",
                factory=module_plugin_factory("episode.plugins.hikvision.ftp"),
                integration=PluginIntegration(
                    type="hikvision_ftp",
                    name="Hikvision FTP snapshots",
                    capabilities=("snapshot-interpretation",),
                ),
            ),
        ]
    )
