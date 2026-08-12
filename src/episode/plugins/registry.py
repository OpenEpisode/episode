from __future__ import annotations

import importlib
from collections.abc import Iterable

from episode.plugins.models import PluginContext, PluginFactory, PluginRegistration


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


def module_plugin_factory(module_name: str) -> PluginFactory:
    def create(context: PluginContext):
        module = importlib.import_module(module_name)
        return module.create_plugin(context)

    return create


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
