from episode.plugins.hikvision_isapi.plugin import HikvisionISAPIPlugin


def create_plugin(context):
    return HikvisionISAPIPlugin(context)


__all__ = ["HikvisionISAPIPlugin", "create_plugin"]
