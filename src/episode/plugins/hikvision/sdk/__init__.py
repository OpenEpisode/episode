from episode.plugins.hikvision.sdk.plugin import HikvisionSDKPlugin
from episode.plugins.models import PluginContext


def create_plugin(context: PluginContext) -> HikvisionSDKPlugin:
    return HikvisionSDKPlugin(context)


__all__ = ["HikvisionSDKPlugin", "create_plugin"]
