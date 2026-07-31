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

    def for_capabilities(self, capabilities: Iterable[str]) -> list[PluginRegistration]:
        configured = set(capabilities)
        return [
            registration
            for registration in self._registrations.values()
            if registration.activation_capability in configured
        ]


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
            )
        ]
    )
