from episode.plugins.hikvision.ftp.plugin import HikvisionFTPPlugin


def create_plugin(context):
    return HikvisionFTPPlugin(context)


__all__ = ["HikvisionFTPPlugin", "create_plugin"]
