from episode.plugins.hikvision_ftp.plugin import HikvisionFTPPlugin


def create_plugin(context):
    return HikvisionFTPPlugin(context)


__all__ = ["HikvisionFTPPlugin", "create_plugin"]
