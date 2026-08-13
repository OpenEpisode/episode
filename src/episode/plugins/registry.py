from __future__ import annotations

import importlib
from collections.abc import Iterable, Mapping

from episode.plugins.models import (
    PluginContext,
    PluginDeviceValidator,
    PluginFactory,
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
        capabilities: Iterable[str],
        connector_types: Iterable[str] = (),
    ) -> list[PluginRegistration]:
        configured = set(capabilities)
        configured_connectors = set(connector_types)
        return [
            registration
            for registration in self._registrations.values()
            if (
                registration.activation_capability in configured
                or (
                    registration.activation_connector_type
                    and registration.activation_connector_type in configured_connectors
                )
            )
        ]

    def for_capabilities(self, capabilities: Iterable[str]) -> list[PluginRegistration]:
        return self.for_configuration(capabilities)

    def validators(self) -> Mapping[str, PluginDeviceValidator]:
        return {
            registration.validation_capability: registration.validator
            for registration in self._registrations.values()
            if registration.validation_capability and registration.validator is not None
        }


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
                id="hikvision-sdk",
                name="Hikvision HCNetSDK",
                kind="native-sdk",
                activation_capability="hikvision_sdk",
                factory=module_plugin_factory("episode.plugins.hikvision_sdk"),
            ),
            PluginRegistration(
                id="hikvision-isapi",
                name="Hikvision ISAPI",
                kind="device-integration",
                activation_capability="isapi",
                factory=module_plugin_factory("episode.plugins.hikvision_isapi"),
                validation_capability="isapi",
                validator=module_plugin_validator("episode.plugins.hikvision_isapi.validation"),
            ),
            PluginRegistration(
                id="hikvision-alarm-server",
                name="Hikvision Alarm Server",
                kind="ingress-handler",
                activation_capability="",
                activation_connector_type="alarm_server",
                factory=module_plugin_factory("episode.plugins.hikvision_alarm"),
            ),
            PluginRegistration(
                id="hikvision-ftp",
                name="Hikvision FTP snapshots",
                kind="file-ingress-handler",
                activation_capability="",
                activation_connector_type="ftp",
                factory=module_plugin_factory("episode.plugins.hikvision_ftp"),
            ),
        ]
    )
