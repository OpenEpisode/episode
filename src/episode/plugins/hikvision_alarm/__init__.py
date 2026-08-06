from episode.plugins.hikvision_alarm.plugin import HikvisionAlarmPlugin
from episode.plugins.models import PluginContext


def create_plugin(context: PluginContext) -> HikvisionAlarmPlugin:
    return HikvisionAlarmPlugin(context)


__all__ = ["HikvisionAlarmPlugin", "create_plugin"]
