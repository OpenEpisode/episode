from __future__ import annotations

import base64
import hashlib
import os
import xml.etree.ElementTree as ET

try:
    from lxml import etree as _lxml_etree
except ImportError:
    _lxml_etree = None
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

import httpx

SOAP = "http://www.w3.org/2003/05/soap-envelope"
TT = "http://www.onvif.org/ver10/schema"
TDS = "http://www.onvif.org/ver10/device/wsdl"
TRT = "http://www.onvif.org/ver10/media/wsdl"
TEV = "http://www.onvif.org/ver10/events/wsdl"
WSA = "http://www.w3.org/2005/08/addressing"
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
PASSWORD_DIGEST = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
)
BASE64_BINARY = (
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary"
)


class ONVIFError(RuntimeError):
    pass


@dataclass(frozen=True)
class ONVIFProfile:
    token: str
    name: str = ""
    encoding: str = ""
    width: int = 0
    height: int = 0
    stream_uri: str = ""
    snapshot_uri: str = ""


@dataclass
class ONVIFDevice:
    manufacturer: str = ""
    model: str = ""
    firmware_version: str = ""
    services: dict[str, str] = field(default_factory=dict)
    profiles: list[ONVIFProfile] = field(default_factory=list)
    event_topics: list[str] = field(default_factory=list)


def _operation(namespace: str, name: str) -> ET.Element:
    return ET.Element(f"{{{namespace}}}{name}")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class ONVIFClient:
    """Minimal ONVIF SOAP client supporting Digest and WS-UsernameToken."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        protocol: str = "http",
        port: int | None = 80,
        path: str = "/onvif/device_service",
        auth_mode: str = "digest_wsse",
        timeout: float = 15,
        relaxed_validation: bool = False,
    ):
        self.host = host
        self.username = username
        self.password = password
        self.auth_mode = auth_mode
        port_part = f":{port}" if port and port not in (80, 443) else ""
        self.device_url = f"{protocol}://{host}{port_part}{path}"
        self._clock_offset = timedelta()
        self.relaxed_validation = relaxed_validation
        self._client = httpx.AsyncClient(
            auth=httpx.DigestAuth(username, password),
            timeout=httpx.Timeout(timeout, read=max(timeout, 45)),
            follow_redirects=False,
        )

    async def close(self) -> None:
        await self._client.aclose()

    def _normalize_url(self, value: str) -> str:
        """Use the configured host when a device advertises an unusable XAddr."""
        parsed = urlsplit(value)
        if not parsed.scheme or not parsed.netloc:
            return value
        configured = urlsplit(self.device_url)
        port = f":{parsed.port}" if parsed.port else ""
        return urlunsplit(
            (parsed.scheme, f"{configured.hostname}{port}", parsed.path, parsed.query, "")
        )

    def _envelope(self, operation: ET.Element, *, authenticated: bool) -> bytes:
        root = ET.Element(f"{{{SOAP}}}Envelope")
        header = ET.SubElement(root, f"{{{SOAP}}}Header")
        if authenticated and self.auth_mode != "digest":
            nonce = os.urandom(16)
            now = datetime.now(timezone.utc) + self._clock_offset
            created = now.isoformat(timespec="seconds").replace("+00:00", "Z")
            digest = base64.b64encode(
                hashlib.sha1(nonce + created.encode() + self.password.encode()).digest()
            ).decode()
            security = ET.SubElement(header, f"{{{WSSE}}}Security")
            token = ET.SubElement(security, f"{{{WSSE}}}UsernameToken")
            ET.SubElement(token, f"{{{WSSE}}}Username").text = self.username
            ET.SubElement(token, f"{{{WSSE}}}Password", {"Type": PASSWORD_DIGEST}).text = digest
            ET.SubElement(
                token,
                f"{{{WSSE}}}Nonce",
                {"EncodingType": BASE64_BINARY},
            ).text = base64.b64encode(nonce).decode()
            ET.SubElement(token, f"{{{WSU}}}Created").text = created
        ET.SubElement(root, f"{{{SOAP}}}Body").append(operation)
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    async def call(
        self,
        url: str,
        action: str,
        operation: ET.Element,
        *,
        authenticated: bool = True,
    ) -> tuple[ET.Element, bytes]:
        response = await self._client.post(
            url,
            content=self._envelope(operation, authenticated=authenticated),
            headers={"Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"'},
        )
        response.raise_for_status()
        raw = response.content
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as error:
            if self.relaxed_validation:
                root = _lxml_etree.fromstring(raw, parser=_lxml_etree.XMLParser(recover=True))
            else:
                raise ONVIFError("Camera returned invalid SOAP XML") from error
        fault = root.find(f".//{{{SOAP}}}Fault")
        if fault is not None:
            reason = fault.findtext(f".//{{{SOAP}}}Text", "ONVIF SOAP fault")
            raise ONVIFError(reason)
        return root, raw

    async def synchronize_time(self) -> None:
        try:
            root, _ = await self.call(
                self.device_url,
                f"{TDS}/GetSystemDateAndTime",
                _operation(TDS, "GetSystemDateAndTime"),
                authenticated=False,
            )
            date = root.find(f".//{{{TT}}}UTCDateTime/{{{TT}}}Date")
            time = root.find(f".//{{{TT}}}UTCDateTime/{{{TT}}}Time")
            if date is None or time is None:
                return
            camera_time = datetime(
                int(date.findtext(f"{{{TT}}}Year", "0")),
                int(date.findtext(f"{{{TT}}}Month", "0")),
                int(date.findtext(f"{{{TT}}}Day", "0")),
                int(time.findtext(f"{{{TT}}}Hour", "0")),
                int(time.findtext(f"{{{TT}}}Minute", "0")),
                int(time.findtext(f"{{{TT}}}Second", "0")),
                tzinfo=timezone.utc,
            )
            self._clock_offset = camera_time - datetime.now(timezone.utc)
        except (httpx.HTTPError, ONVIFError, ValueError):
            return

    async def discover(self) -> ONVIFDevice:
        await self.synchronize_time()
        services_root, _ = await self.call(
            self.device_url,
            f"{TDS}/GetServices",
            self._get_services_operation(),
        )
        services: dict[str, str] = {}
        for service in services_root.findall(f".//{{{TDS}}}Service"):
            namespace = service.findtext(f"{{{TDS}}}Namespace", "")
            xaddr = service.findtext(f"{{{TDS}}}XAddr", "")
            if namespace and xaddr:
                services[namespace] = self._normalize_url(xaddr)

        device = ONVIFDevice(services=services)
        await self._load_device_information(device)
        await self._load_media_profiles(device)
        await self._load_event_topics(device)
        return device

    @staticmethod
    def _get_services_operation() -> ET.Element:
        operation = _operation(TDS, "GetServices")
        ET.SubElement(operation, f"{{{TDS}}}IncludeCapability").text = "true"
        return operation

    async def _load_device_information(self, device: ONVIFDevice) -> None:
        try:
            root, _ = await self.call(
                self.device_url,
                f"{TDS}/GetDeviceInformation",
                _operation(TDS, "GetDeviceInformation"),
            )
        except (httpx.HTTPError, ONVIFError):
            return
        device.manufacturer = root.findtext(f".//{{{TDS}}}Manufacturer", "")
        device.model = root.findtext(f".//{{{TDS}}}Model", "")
        device.firmware_version = root.findtext(f".//{{{TDS}}}FirmwareVersion", "")

    async def _load_media_profiles(self, device: ONVIFDevice) -> None:
        media_url = device.services.get(TRT)
        if not media_url:
            return
        root, _ = await self.call(
            media_url,
            f"{TRT}/GetProfiles",
            _operation(TRT, "GetProfiles"),
        )
        for profile in root.findall(f".//{{{TRT}}}Profiles"):
            token = profile.attrib.get("token", "")
            if not token:
                continue
            encoder = profile.find(f"{{{TT}}}VideoEncoderConfiguration")
            resolution = encoder.find(f"{{{TT}}}Resolution") if encoder is not None else None
            device.profiles.append(
                ONVIFProfile(
                    token=token,
                    name=profile.findtext(f"{{{TT}}}Name", ""),
                    encoding=(
                        encoder.findtext(f"{{{TT}}}Encoding", "") if encoder is not None else ""
                    ),
                    width=(
                        int(resolution.findtext(f"{{{TT}}}Width", "0"))
                        if resolution is not None
                        else 0
                    ),
                    height=(
                        int(resolution.findtext(f"{{{TT}}}Height", "0"))
                        if resolution is not None
                        else 0
                    ),
                    stream_uri=await self._get_stream_uri(media_url, token),
                    snapshot_uri=await self._get_snapshot_uri(media_url, token),
                )
            )

    async def _get_stream_uri(self, media_url: str, token: str) -> str:
        operation = _operation(TRT, "GetStreamUri")
        setup = ET.SubElement(operation, f"{{{TRT}}}StreamSetup")
        ET.SubElement(setup, f"{{{TT}}}Stream").text = "RTP-Unicast"
        transport = ET.SubElement(setup, f"{{{TT}}}Transport")
        ET.SubElement(transport, f"{{{TT}}}Protocol").text = "RTSP"
        ET.SubElement(operation, f"{{{TRT}}}ProfileToken").text = token
        root, _ = await self.call(media_url, f"{TRT}/GetStreamUri", operation)
        return self._normalize_url(root.findtext(f".//{{{TT}}}Uri", ""))

    async def _get_snapshot_uri(self, media_url: str, token: str) -> str:
        operation = _operation(TRT, "GetSnapshotUri")
        ET.SubElement(operation, f"{{{TRT}}}ProfileToken").text = token
        try:
            root, _ = await self.call(media_url, f"{TRT}/GetSnapshotUri", operation)
            return self._normalize_url(root.findtext(f".//{{{TT}}}Uri", ""))
        except (httpx.HTTPError, ONVIFError):
            return ""

    async def _load_event_topics(self, device: ONVIFDevice) -> None:
        events_url = device.services.get(TEV)
        if not events_url:
            return
        try:
            root, _ = await self.call(
                events_url,
                f"{TEV}/EventPortType/GetEventPropertiesRequest",
                _operation(TEV, "GetEventProperties"),
            )
        except (httpx.HTTPError, ONVIFError):
            return
        topic_set = root.find(".//{http://docs.oasis-open.org/wsn/t-1}TopicSet")
        if topic_set is not None:
            device.event_topics = sorted(
                {_local_name(node.tag) for node in topic_set.iter() if node is not topic_set}
            )

    async def create_pull_point(self, events_url: str) -> str:
        operation = _operation(TEV, "CreatePullPointSubscription")
        ET.SubElement(operation, f"{{{TEV}}}InitialTerminationTime").text = "PT2M"
        root, _ = await self.call(
            events_url,
            f"{TEV}/EventPortType/CreatePullPointSubscriptionRequest",
            operation,
        )
        address = root.findtext(f".//{{{WSA}}}Address", "")
        if not address:
            address = root.findtext(
                ".//{http://schemas.xmlsoap.org/ws/2004/08/addressing}Address", ""
            )
        if not address:
            raise ONVIFError("Camera did not return a pull-point subscription address")
        return self._normalize_url(address)

    async def pull_messages(
        self,
        subscription_url: str,
        *,
        timeout: int = 30,
        limit: int = 100,
    ) -> tuple[ET.Element, bytes]:
        operation = _operation(TEV, "PullMessages")
        ET.SubElement(operation, f"{{{TEV}}}Timeout").text = f"PT{timeout}S"
        ET.SubElement(operation, f"{{{TEV}}}MessageLimit").text = str(limit)
        return await self.call(
            subscription_url,
            f"{TEV}/PullPointSubscription/PullMessagesRequest",
            operation,
        )

    async def renew(self, subscription_url: str) -> None:
        namespace = "http://docs.oasis-open.org/wsn/b-2"
        operation = _operation(namespace, "Renew")
        ET.SubElement(operation, f"{{{namespace}}}TerminationTime").text = "PT2M"
        await self.call(
            subscription_url,
            "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/RenewRequest",
            operation,
        )

    async def unsubscribe(self, subscription_url: str) -> None:
        operation = _operation("http://docs.oasis-open.org/wsn/b-2", "Unsubscribe")
        await self.call(
            subscription_url,
            "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/UnsubscribeRequest",
            operation,
        )
