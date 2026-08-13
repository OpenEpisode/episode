from episode.plugins.hikvision.alarm_server.plugin import HikvisionAlarmPlugin
from episode.plugins.models import PluginContext


def create_plugin(context: PluginContext) -> HikvisionAlarmPlugin:
    return HikvisionAlarmPlugin(context)


__all__ = ["HikvisionAlarmPlugin", "create_plugin"]
