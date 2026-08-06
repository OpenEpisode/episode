"""Compatibility import for the former vendor-coupled Alarm Server module."""

from episode.connectors.http_ingress import HTTPIngressConnector


class AlarmServerConnector(HTTPIngressConnector):
    def __init__(self, name, ingestion, config, api_port):
        super().__init__(
            name,
            ingestion,
            config,
            api_port,
            connector_type="alarm_server",
        )
