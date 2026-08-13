from episode.plugins.models import PluginContext
from episode.plugins.onvif.plugin import ONVIFPlugin


def create_plugin(context: PluginContext) -> ONVIFPlugin:
    return ONVIFPlugin(context)


__all__ = ["ONVIFPlugin", "create_plugin"]
