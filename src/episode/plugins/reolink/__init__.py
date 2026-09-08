import logging

from episode.plugins.models import PluginContext
from episode.plugins.reolink.plugin import ReolinkPlugin

logger = logging.getLogger(__name__)


def create_plugin(context: PluginContext) -> ReolinkPlugin:
    """Create and return the Reolink Baichuan plugin for the given context."""
    logger.debug(
        "Creating Reolink Baichuan plugin: configured_devices=%d",
        len(context.configured_devices),
    )
    plugin = ReolinkPlugin(context)
    logger.info(
        "Reolink plugin created: id=%s devices=%d",
        plugin.status().id,
        len(plugin._configured_devices),
    )
    return plugin


__all__ = ["ReolinkPlugin", "create_plugin"]
