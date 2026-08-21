from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from episode.config import EpisodeConfig
from episode.domain.models import Area, Device, ReceiptStatus
from episode.engine.bus import EventBus
from episode.engine.engine import EpisodeEngine
from episode.ingestion.models import IngressDelivery
from episode.ingestion.router import IngressRouter
from episode.ingestion.service import IngestionService
from episode.plugins.deliveries import RawPluginDeliveryStore
from episode.plugins.hikvision.alarm_server.plugin import HikvisionAlarmPlugin
from episode.plugins.hikvision.isapi.plugin import HikvisionISAPIPlugin
from episode.plugins.hikvision.isapi.runtime import (
    ISAPIDeviceConfig,
    ISAPIDeviceConnection,
    ISAPIEventStreamDecoder,
    device_config,
)
from episode.plugins.hikvision.xml_events import HikvisionEvent
from episode.plugins.models import PluginContext, PluginInstanceState, RawPluginDelivery
from episode.storage.repository import Repository


def _xml() -> bytes:
    return (Path(__file__).parent / "fixtures/hikvision/alarm_event.xml").read_bytes()


def _video_loss_xml(state: str) -> bytes:
    return f"""<EventNotificationAlert version="2.0"
      xmlns="http://www.hikvision.com/ver20/XMLSchema">
      <ipAddress>192.0.2.10</ipAddress>
      <channelID>1</channelID>
      <dateTime>2026-08-14T10:41:18+01:00</dateTime>
      <eventType>videoloss</eventType>
      <eventState>{state}</eventState>
      <channelName>Gate camera</channelName>
    </EventNotificationAlert>""".encode()


def _configured_device(**overrides):
    value = {
        "id": "gate-camera",
        "name": "Gate camera",
        "device_type": "camera",
        "area_id": "gate",
        "ip_address": "192.0.2.10",
        "username": "admin",
        "password": "secret",
        "capabilities": ["video", "isapi"],
        "configs": {
            "isapi": {
                "protocol": "http",
                "port": 80,
                "path": "/ISAPI/Event/notification/alertStream",
                "settings": {"ignore_events": ["video_loss", "illaccess"]},
            }
        },
        "enabled": True,
    }
    value.update(overrides)
    return value


async def _pipeline(tmp_path, configured_devices=()):
    config = EpisodeConfig(data_dir=str(tmp_path / "data"), episode_timeout=30)
    repository = Repository(config)
    await repository.initialize()
    await repository.upsert_area(Area(id="gate", name="Front gate"))
    await repository.upsert_device(
        Device(
            id="gate-camera",
            name="Gate camera",
            device_type="camera",
            area_id="gate",
            ip_address="192.0.2.10",
        )
    )
    bus = EventBus()
    engine = EpisodeEngine(repository, bus, timeout=30)
    await engine.start()
    router = IngressRouter()
    ingestion = IngestionService(config.data_dir, repository, engine, router)
    sink = RawPluginDeliveryStore(ingestion)
    plugin = HikvisionISAPIPlugin(
        PluginContext(
            Path(config.plugins_dir),
            configured_devices=tuple(configured_devices),
            raw_delivery_sink=sink,
            ingress_router=router,
        )
    )
    return repository, engine, sink, plugin


def test_stream_decoder_handles_fragmentation_boundaries_and_multiple_documents():
    xml = _xml()
    decoder = ISAPIEventStreamDecoder()
    chunks = (
        b"--boundary\r\nContent-Type: application/xml\r\n\r\n" + xml[:17],
        xml[17:141],
        xml[141:] + b"\r\n--boundary\r\n" + xml + b"\r\n",
    )

    documents = []
    for chunk in chunks:
        documents.extend(decoder.feed(chunk))

    assert documents == [xml.rstrip(), xml.rstrip()]


def test_stream_decoder_rejects_unbounded_incomplete_document():
    decoder = ISAPIEventStreamDecoder(max_buffer_bytes=64)

    with pytest.raises(ValueError, match="bounded receive buffer"):
        decoder.feed(b"<?xml version='1.0'?><EventNotificationAlert>" + b"x" * 80)


def test_device_config_preserves_existing_inventory_values_and_hides_credentials_from_url():
    config, error = device_config(_configured_device())

    assert error is None
    assert config == ISAPIDeviceConfig(
        id="gate-camera",
        name="Gate camera",
        area_id="gate",
        address="192.0.2.10",
        username="admin",
        password="secret",
        ignore_events=("video_loss", "illaccess"),
    )
    assert config.url == "http://192.0.2.10/ISAPI/Event/notification/alertStream"
    assert "admin" not in config.url
    assert "secret" not in config.url


@pytest.mark.asyncio
async def test_raw_isapi_delivery_is_preserved_before_plugin_creates_event(tmp_path):
    repository, engine, sink, plugin = await _pipeline(tmp_path)
    await plugin.start()
    payload = _xml()
    received_at = datetime.now(tz=timezone.utc)
    try:
        await sink(
            RawPluginDelivery(
                plugin_id="hikvision-isapi",
                device_id="gate-camera",
                area_id="gate",
                received_at=received_at,
                payload=payload,
                source="hikvision:isapi",
                media_type="application/xml",
                artifact_type="event_payload",
                metadata={"ignore_events": []},
            )
        )

        events = await repository.list_events()
        receipts = await repository.list_ingestion_receipts()
        artifact = await repository.get_raw_artifact(receipts[0].artifact_id)
        assert len(events) == len(receipts) == 1
        assert events[0].event_type == "human_detection"
        assert events[0].source == "hikvision:isapi"
        assert events[0].metadata["bounding_box"] == {
            "x": 0.125,
            "y": 0.25,
            "width": 0.5,
            "height": 0.625,
        }
        assert receipts[0].source == "hikvision:isapi"
        assert receipts[0].status == ReceiptStatus.ACCEPTED
        assert receipts[0].event_id == events[0].id
        assert Path(artifact.file_path).read_bytes() == payload
        assert artifact.mime_type == "application/xml"
        assert artifact.sealed
        assert plugin.status().metrics["claimed"] == 1
    finally:
        await plugin.stop()
        await engine.stop()
        await repository.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "ignore_events", "expected_status"),
    [
        (b"not xml", [], ReceiptStatus.REJECTED),
        (_xml(), ["human_detection"], ReceiptStatus.IGNORED),
        (_xml(), ["VMD"], ReceiptStatus.IGNORED),
    ],
)
async def test_malformed_and_ignored_isapi_deliveries_remain_raw(
    tmp_path, payload, ignore_events, expected_status
):
    repository, engine, sink, plugin = await _pipeline(tmp_path)
    await plugin.start()
    try:
        await sink(
            RawPluginDelivery(
                plugin_id="hikvision-isapi",
                device_id="gate-camera",
                area_id="gate",
                received_at=datetime.now(tz=timezone.utc),
                payload=payload,
                source="hikvision:isapi",
                media_type="application/xml",
                artifact_type="event_payload",
                metadata={"ignore_events": ignore_events},
            )
        )

        receipts = await repository.list_ingestion_receipts()
        artifact = await repository.get_raw_artifact(receipts[0].artifact_id)
        assert receipts[0].status == expected_status
        assert await repository.list_events() == []
        assert Path(artifact.file_path).read_bytes() == payload
    finally:
        await plugin.stop()
        await engine.stop()
        await repository.close()


class _HangingStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self._chunks = chunks
        self._wait = asyncio.Event()

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk
        await self._wait.wait()


@pytest.mark.asyncio
async def test_device_connection_preserves_fragmented_stream_and_stops_cleanly():
    deliveries = []
    xml = _xml()

    async def sink(delivery):
        deliveries.append(delivery)

    def client_factory(auth):
        async def handler(request):
            assert request.url.path == "/ISAPI/Event/notification/alertStream"
            return httpx.Response(
                200,
                stream=_HangingStream([b"headers\r\n" + xml[:50], xml[50:]]),
            )

        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            auth=auth,
            timeout=None,
        )

    connection = ISAPIDeviceConnection(
        ISAPIDeviceConfig(
            id="gate-camera",
            name="Gate camera",
            area_id="gate",
            address="192.0.2.10",
            username="admin",
            password="secret",
        ),
        sink,
        client_factory=client_factory,
        reconnect_delay=0.01,
    )

    await connection.start()
    for _attempt in range(100):
        if deliveries:
            break
        await asyncio.sleep(0.01)

    assert len(deliveries) == 1
    assert deliveries[0].payload == xml.rstrip()
    assert deliveries[0].source == "hikvision:isapi"
    assert connection.status().state == PluginInstanceState.RUNNING
    assert connection.status().messages_received == 1
    assert connection.status().details["connected"] is True
    assert connection.status().details["connection_count"] == 1
    assert connection.status().details["reconnects"] == 0
    assert connection.status().details["last_stream_activity_at"]

    await connection.stop()
    assert connection.status().state == PluginInstanceState.STOPPED


@pytest.mark.asyncio
async def test_device_connection_reconnects_when_open_stream_becomes_idle():
    deliveries = []
    requests = 0

    async def sink(delivery):
        deliveries.append(delivery)

    def client_factory(auth):
        async def handler(request):
            nonlocal requests
            requests += 1
            return httpx.Response(200, stream=_HangingStream([_xml()]))

        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            auth=auth,
            timeout=None,
        )

    connection = ISAPIDeviceConnection(
        ISAPIDeviceConfig(
            id="gate-camera",
            name="Gate camera",
            area_id="gate",
            address="192.0.2.10",
            username="admin",
            password="secret",
        ),
        sink,
        client_factory=client_factory,
        reconnect_delay=0.01,
        stream_idle_timeout=0.02,
    )

    await connection.start()
    for _attempt in range(100):
        if len(deliveries) >= 2:
            break
        await asyncio.sleep(0.01)

    status = connection.status()
    assert len(deliveries) >= 2
    assert requests >= 2
    assert status.state == PluginInstanceState.RUNNING
    assert status.details["connected"] is True
    assert status.details["connection_count"] >= 2
    assert status.details["reconnects"] >= 1

    await connection.stop()


@pytest.mark.asyncio
async def test_device_connection_reports_idle_stream_while_waiting_to_reconnect():
    async def sink(_delivery):
        raise AssertionError("an idle stream must not create a delivery")

    def client_factory(auth):
        async def handler(request):
            return httpx.Response(200, stream=_HangingStream([]))

        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            auth=auth,
            timeout=None,
        )

    connection = ISAPIDeviceConnection(
        ISAPIDeviceConfig(
            id="gate-camera",
            name="Gate camera",
            area_id="gate",
            address="192.0.2.10",
            username="admin",
            password="secret",
        ),
        sink,
        client_factory=client_factory,
        reconnect_delay=60,
        stream_idle_timeout=0.01,
    )

    await connection.start()
    for _attempt in range(100):
        if connection.status().state == PluginInstanceState.STARTING:
            if connection.status().error:
                break
        await asyncio.sleep(0.01)

    status = connection.status()
    assert status.state == PluginInstanceState.STARTING
    assert status.error == "ISAPI event stream was idle for 0.01s; reconnecting."
    assert status.details["connected"] is False
    assert status.details["last_disconnect_at"]

    await connection.stop()


@pytest.mark.asyncio
async def test_device_connection_preserves_ignored_state_transitions_not_heartbeats():
    deliveries = []
    notifications = [
        _video_loss_xml("inactive"),
        _video_loss_xml("inactive"),
        _video_loss_xml("active"),
        _video_loss_xml("active"),
        _video_loss_xml("inactive"),
    ]

    async def sink(delivery):
        deliveries.append(delivery)

    def client_factory(auth):
        async def handler(request):
            return httpx.Response(
                200,
                stream=_HangingStream([b"\r\n".join(notifications)]),
            )

        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            auth=auth,
            timeout=None,
        )

    connection = ISAPIDeviceConnection(
        ISAPIDeviceConfig(
            id="gate-camera",
            name="Gate camera",
            area_id="gate",
            address="192.0.2.10",
            username="admin",
            password="secret",
            ignore_events=("videoloss",),
        ),
        sink,
        client_factory=client_factory,
        reconnect_delay=0.01,
    )

    await connection.start()
    for _attempt in range(100):
        if connection.status().messages_received == len(notifications):
            break
        await asyncio.sleep(0.01)

    assert [HikvisionEvent.from_bytes(item.payload).event_state.value for item in deliveries] == [
        "inactive",
        "active",
        "inactive",
    ]
    assert connection.status().messages_received == 5
    assert connection.status().details["deliveries_preserved"] == 3
    assert connection.status().details["deliveries_suppressed"] == 2

    await connection.stop()


@pytest.mark.asyncio
async def test_device_connection_reports_authentication_failure_without_crashing_plugin():
    async def sink(_delivery):
        raise AssertionError("authentication failures must not create deliveries")

    def client_factory(auth):
        async def handler(request):
            return httpx.Response(401, request=request)

        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            auth=auth,
            timeout=None,
        )

    connection = ISAPIDeviceConnection(
        ISAPIDeviceConfig(
            id="gate-camera",
            name="Gate camera",
            area_id="gate",
            address="192.0.2.10",
            username="admin",
            password="wrong",
        ),
        sink,
        client_factory=client_factory,
        reconnect_delay=60,
    )
    await connection.start()
    for _attempt in range(100):
        if connection.status().state == PluginInstanceState.FAILED:
            break
        await asyncio.sleep(0.01)

    assert connection.status().state == PluginInstanceState.FAILED
    assert connection.status().error == "ISAPI returned HTTP 401."

    await connection.stop()


class _FakeConnection:
    def __init__(self, config, _sink):
        self.config = config
        self.started = False
        self.stopped = False

    def status(self):
        from episode.plugins.models import PluginInstanceStatus

        return PluginInstanceStatus(
            id=self.config.id,
            name=self.config.name,
            state=(PluginInstanceState.RUNNING if self.started else PluginInstanceState.STARTING),
        )

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


@pytest.mark.asyncio
async def test_plugin_starts_only_configured_isapi_devices_and_reports_each_instance(tmp_path):
    connections = []

    def factory(config, sink):
        connection = _FakeConnection(config, sink)
        connections.append(connection)
        return connection

    router = IngressRouter()
    plugin = HikvisionISAPIPlugin(
        PluginContext(
            tmp_path,
            configured_devices=(
                _configured_device(),
                _configured_device(id="disabled", enabled=False),
                _configured_device(id="onvif-only", configs={"onvif": {}}),
            ),
            raw_delivery_sink=lambda _delivery: None,
            ingress_router=router,
        ),
        connection_factory=factory,
    )

    await plugin.start()

    assert [connection.config.id for connection in connections] == ["gate-camera"]
    assert plugin.status().state.value == "ready"
    assert [instance.id for instance in plugin.status().instances] == ["gate-camera"]

    await plugin.stop()
    assert connections[0].stopped


@pytest.mark.asyncio
async def test_isapi_and_alarm_deliveries_share_one_canonical_event(tmp_path):
    config = EpisodeConfig(data_dir=str(tmp_path / "data"), episode_timeout=30)
    repository = Repository(config)
    await repository.initialize()
    await repository.upsert_area(Area(id="gate", name="Front gate"))
    await repository.upsert_device(
        Device(
            id="gate-camera",
            name="Gate camera",
            device_type="camera",
            area_id="gate",
            ip_address="192.0.2.10",
        )
    )
    bus = EventBus()
    engine = EpisodeEngine(repository, bus, timeout=30)
    await engine.start()
    router = IngressRouter()
    ingestion = IngestionService(config.data_dir, repository, engine, router)
    sink = RawPluginDeliveryStore(ingestion)
    context = PluginContext(
        Path(config.plugins_dir),
        raw_delivery_sink=sink,
        ingress_router=router,
    )
    isapi = HikvisionISAPIPlugin(context)
    alarm = HikvisionAlarmPlugin(context)
    await isapi.start()
    await alarm.start()
    payload = _xml()
    received_at = datetime.now(tz=timezone.utc)
    try:
        await sink(
            RawPluginDelivery(
                plugin_id="hikvision-isapi",
                device_id="gate-camera",
                area_id="gate",
                received_at=received_at,
                payload=payload,
                source="hikvision:isapi",
                media_type="application/xml",
                artifact_type="event_payload",
                metadata={"ignore_events": []},
            )
        )
        await ingestion.accept(
            IngressDelivery(
                source="http:alarm_server",
                transport="http",
                received_at=received_at,
                payload=payload,
                media_type="application/xml",
                device_id="gate-camera",
                area_id="gate",
                metadata={"connector_type": "alarm_server"},
            )
        )

        events = await repository.list_events()
        receipts = await repository.list_ingestion_receipts()
        episodes = await repository.list_episodes()
        assert len(events) == len(episodes) == 1
        assert events[0].event_type == "human_detection"
        assert len(receipts) == 2
        assert {receipt.event_id for receipt in receipts} == {events[0].id}
        assert {receipt.source for receipt in receipts} == {
            "hikvision:isapi",
            "http:alarm_server",
        }
    finally:
        await alarm.stop()
        await isapi.stop()
        await engine.stop()
        await repository.close()
