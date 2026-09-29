from __future__ import annotations

# ruff: noqa: N801 - ctypes names intentionally mirror the vendor ABI.
import base64
import ctypes
import json
import os
import signal
import sys
import threading
from pathlib import Path

from episode.plugins.hikvision.sdk.worker import (
    NET_DVR_DEVICEINFO_V40,
    NET_DVR_USER_LOGIN_INFO,
    _configure_sdk,
    _copy_text,
    _get_device_info,
)

MESSAGE_PREFIX = "EPISODE_HIKVISION_SDK_VIDEO="
MAX_VIDEO_PACKET_BYTES = 8 * 1024 * 1024
NET_DVR_GET_COMPRESSCFG_V30 = 1040


class NET_DVR_PREVIEWINFO(ctypes.Structure):
    _fields_ = [
        ("lChannel", ctypes.c_int),
        ("dwStreamType", ctypes.c_uint),
        ("dwLinkMode", ctypes.c_uint),
        ("hPlayWnd", ctypes.c_void_p),
        ("bBlocked", ctypes.c_uint),
        ("bPassbackRecord", ctypes.c_uint),
        ("byPreviewMode", ctypes.c_ubyte),
        ("byStreamID", ctypes.c_ubyte * 32),
        ("byProtoType", ctypes.c_ubyte),
        ("byRes1", ctypes.c_ubyte),
        ("byVideoCodingType", ctypes.c_ubyte),
        ("dwDisplayBufNum", ctypes.c_uint),
        ("byNPQMode", ctypes.c_ubyte),
        ("byRecvMetaData", ctypes.c_ubyte),
        ("byDataType", ctypes.c_ubyte),
        ("byRes", ctypes.c_ubyte * 213),
    ]


class NET_DVR_PACKET_INFO_EX(ctypes.Structure):
    _fields_ = [
        ("wWidth", ctypes.c_ushort),
        ("wHeight", ctypes.c_ushort),
        ("dwTimeStamp", ctypes.c_uint),
        ("dwTimeStampHigh", ctypes.c_uint),
        ("dwYear", ctypes.c_uint),
        ("dwMonth", ctypes.c_uint),
        ("dwDay", ctypes.c_uint),
        ("dwHour", ctypes.c_uint),
        ("dwMinute", ctypes.c_uint),
        ("dwSecond", ctypes.c_uint),
        ("dwMillisecond", ctypes.c_uint),
        ("dwFrameNum", ctypes.c_uint),
        ("dwFrameRate", ctypes.c_uint),
        ("dwFlag", ctypes.c_uint),
        ("dwFilePos", ctypes.c_uint),
        ("dwPacketType", ctypes.c_uint),
        ("dwPacketSize", ctypes.c_uint),
        ("pPacketBuffer", ctypes.POINTER(ctypes.c_ubyte)),
        ("byRes1", ctypes.c_ubyte * 4),
        ("dwPacketMode", ctypes.c_uint),
        ("byRes2", ctypes.c_ubyte * 16),
        ("dwReserved", ctypes.c_uint * 6),
    ]


class NET_DVR_COMPRESSION_INFO_V30(ctypes.Structure):
    _fields_ = [
        ("byStreamType", ctypes.c_ubyte),
        ("byResolution", ctypes.c_ubyte),
        ("byBitrateType", ctypes.c_ubyte),
        ("byPicQuality", ctypes.c_ubyte),
        ("dwVideoBitrate", ctypes.c_uint),
        ("dwVideoFrameRate", ctypes.c_uint),
        ("wIntervalFrameI", ctypes.c_ushort),
        ("byIntervalBPFrame", ctypes.c_ubyte),
        ("byres1", ctypes.c_ubyte),
        ("byVideoEncType", ctypes.c_ubyte),
        ("byAudioEncType", ctypes.c_ubyte),
        ("byVideoEncComplexity", ctypes.c_ubyte),
        ("byEnableSvc", ctypes.c_ubyte),
        ("byFormatType", ctypes.c_ubyte),
        ("byAudioBitRate", ctypes.c_ubyte),
        ("byStreamSmooth", ctypes.c_ubyte),
        ("byAudioSamplingRate", ctypes.c_ubyte),
        ("bySmartCodec", ctypes.c_ubyte),
        ("byDepthMapEnable", ctypes.c_ubyte),
        ("wAverageVideoBitrate", ctypes.c_ushort),
    ]


class NET_DVR_COMPRESSIONCFG_V30(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint),
        ("struNormHighRecordPara", NET_DVR_COMPRESSION_INFO_V30),
        ("struRes", NET_DVR_COMPRESSION_INFO_V30),
        ("struEventRecordPara", NET_DVR_COMPRESSION_INFO_V30),
        ("struNetPara", NET_DVR_COMPRESSION_INFO_V30),
    ]


ES_REALPLAY_CALLBACK = ctypes.CFUNCTYPE(
    None,
    ctypes.c_int,
    ctypes.POINTER(NET_DVR_PACKET_INFO_EX),
    ctypes.c_void_p,
)

_OUTPUT_LOCK = threading.Lock()
_VIDEO_TYPES = frozenset({0, 1, 2, 3})  # header, I, B, and P packets
_FRAME_RATES = {
    5: 1,
    6: 2,
    7: 4,
    8: 6,
    9: 8,
    10: 10,
    11: 12,
    12: 16,
    13: 20,
    14: 15,
    15: 18,
    16: 22,
    17: 25,
    18: 30,
    19: 35,
    20: 40,
    21: 45,
    22: 50,
    23: 55,
    24: 60,
    25: 3,
    26: 5,
    27: 7,
    28: 9,
    29: 100,
    30: 120,
    31: 24,
    32: 48,
}
_RESOLUTIONS = {
    1: (352, 288),
    2: (176, 144),
    3: (704, 576),
    4: (704, 288),
    6: (320, 240),
    7: (160, 120),
    16: (640, 480),
    17: (1600, 1200),
    18: (800, 600),
    19: (1280, 720),
    22: (1360, 1024),
    28: (2560, 1920),
    30: (2048, 1536),
    34: (1024, 768),
    35: (1280, 1024),
    36: (960, 576),
    46: (1376, 768),
    47: (1366, 768),
    48: (1360, 768),
    56: (2304, 1296),
    64: (3840, 2160),
    66: (3072, 1728),
    67: (2592, 1944),
    70: (2560, 1440),
    86: (640, 360),
    98: (2688, 1520),
    102: (960, 720),
    146: (2592, 1520),
    157: (2560, 1536),
    169: (2944, 1656),
    179: (1536, 864),
    186: (3200, 1800),
    202: (2688, 1512),
    213: (3264, 1836),
    214: (2712, 1536),
    280: (4608, 2592),
}


def stream_capabilities(sdk, user_id: int, channel: int) -> list[dict[str, object]]:
    """Read current main/sub encoding settings without changing the camera."""
    cfg = NET_DVR_COMPRESSIONCFG_V30()
    cfg.dwSize = ctypes.sizeof(cfg)
    returned = ctypes.c_uint()
    if not sdk.NET_DVR_GetDVRConfig(
        user_id,
        NET_DVR_GET_COMPRESSCFG_V30,
        channel,
        ctypes.byref(cfg),
        ctypes.sizeof(cfg),
        ctypes.byref(returned),
    ):
        return []
    if returned.value < ctypes.sizeof(cfg):
        return []

    streams = []
    for variant, stream_type, settings in (
        ("main", 0, cfg.struNormHighRecordPara),
        ("sub", 1, cfg.struNetPara),
    ):
        # Type 0 is Hikvision's private H.264 variant. Do not advertise it until
        # its bitstream framing is verified against the recorder's Annex-B input.
        codec = {1: "h264", 10: "hevc"}.get(settings.byVideoEncType)
        if codec is None:
            continue
        width, height = _RESOLUTIONS.get(settings.byResolution, (None, None))
        streams.append(
            {
                "variant": variant,
                "stream_type": stream_type,
                "codec": codec,
                "width": width,
                "height": height,
                "frame_rate": _FRAME_RATES.get(settings.dwVideoFrameRate),
            }
        )
    return streams


def _write_message(message: dict[str, object]) -> None:
    payload = (MESSAGE_PREFIX + json.dumps(message, separators=(",", ":")) + "\n").encode()
    with _OUTPUT_LOCK:
        view = memoryview(payload)
        while view:
            written = os.write(sys.stdout.fileno(), view)
            view = view[written:]


def _configure_video_sdk(sdk) -> None:
    if hasattr(sdk, "NET_DVR_SetConnectTime"):
        sdk.NET_DVR_SetConnectTime.argtypes = [ctypes.c_uint, ctypes.c_uint]
        sdk.NET_DVR_SetConnectTime.restype = ctypes.c_bool
    if hasattr(sdk, "NET_DVR_SetReconnect"):
        sdk.NET_DVR_SetReconnect.argtypes = [ctypes.c_uint, ctypes.c_bool]
        sdk.NET_DVR_SetReconnect.restype = ctypes.c_bool
    sdk.NET_DVR_SetESRealPlayCallBack.argtypes = [
        ctypes.c_int,
        ES_REALPLAY_CALLBACK,
        ctypes.c_void_p,
    ]
    sdk.NET_DVR_SetESRealPlayCallBack.restype = ctypes.c_bool
    sdk.NET_DVR_RealPlay_V40.argtypes = [
        ctypes.c_int,
        ctypes.POINTER(NET_DVR_PREVIEWINFO),
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    sdk.NET_DVR_RealPlay_V40.restype = ctypes.c_int
    sdk.NET_DVR_StopRealPlay.argtypes = [ctypes.c_int]
    sdk.NET_DVR_StopRealPlay.restype = ctypes.c_bool


def _run(plugin_path: Path, config: dict[str, object]) -> int:
    stop_event = threading.Event()
    initialized = False
    user_id = -1
    preview_handle = -1
    packet_callback = None
    sdk = None
    channel = 1
    active_stream: int | None = None

    def request_stop(_signum=None, _frame=None) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    try:
        sdk = ctypes.CDLL(str(plugin_path / "libhcnetsdk.so"), mode=ctypes.RTLD_GLOBAL)
        _configure_sdk(sdk)
        _configure_video_sdk(sdk)
        initialized = bool(sdk.NET_DVR_Init())
        if not initialized:
            _write_message(
                {"type": "error", "stage": "initialize", "code": int(sdk.NET_DVR_GetLastError())}
            )
            return 10
        if hasattr(sdk, "NET_DVR_SetConnectTime"):
            sdk.NET_DVR_SetConnectTime(3000, 1)
        if hasattr(sdk, "NET_DVR_SetReconnect"):
            sdk.NET_DVR_SetReconnect(30000, False)

        login = NET_DVR_USER_LOGIN_INFO()
        _copy_text(login.sDeviceAddress, config.get("address"), "address")
        _copy_text(login.sUserName, config.get("username"), "username")
        _copy_text(login.sPassword, config.get("password"), "password")
        login.wPort = int(config.get("port", 8000))
        login.bUseAsynLogin = 0
        device_info = NET_DVR_DEVICEINFO_V40()
        user_id = int(sdk.NET_DVR_Login_V40(ctypes.byref(login), ctypes.byref(device_info)))
        if user_id < 0:
            _write_message(
                {"type": "error", "stage": "login", "code": int(sdk.NET_DVR_GetLastError())}
            )
            return 11

        channel = int(device_info.struDeviceV30.byStartChan) or 1
        streams = stream_capabilities(sdk, user_id, channel)
        _write_message(
            {"type": "ready", "device_info": _get_device_info(sdk, user_id), "streams": streams}
        )

        while not stop_event.is_set():
            line = sys.stdin.readline()
            if not line:
                break
            try:
                command = json.loads(line)
            except json.JSONDecodeError:
                _write_message({"type": "error", "stage": "command", "message": "invalid command"})
                continue
            if not isinstance(command, dict):
                continue
            if command.get("type") == "start" and active_stream is None:
                stream_type = command.get("stream_type")
                if stream_type not in {0, 1}:
                    _write_message(
                        {"type": "error", "stage": "preview", "message": "unsupported stream"}
                    )
                    continue

                preview = NET_DVR_PREVIEWINFO()
                preview.lChannel = channel
                preview.dwStreamType = stream_type
                preview.dwLinkMode = 0
                preview.hPlayWnd = None
                preview.bBlocked = 0
                preview.bPassbackRecord = 0
                preview.byProtoType = 0
                preview.dwDisplayBufNum = 1
                preview_handle = int(
                    sdk.NET_DVR_RealPlay_V40(user_id, ctypes.byref(preview), None, None)
                )
                if preview_handle < 0:
                    _write_message(
                        {
                            "type": "error",
                            "stage": "preview",
                            "code": int(sdk.NET_DVR_GetLastError()),
                        }
                    )
                    continue

                def on_packet(_handle, packet_pointer, _user) -> None:
                    try:
                        packet = packet_pointer.contents
                        if int(packet.dwPacketType) not in _VIDEO_TYPES:
                            return
                        size = int(packet.dwPacketSize)
                        if size <= 0 or size > MAX_VIDEO_PACKET_BYTES or not packet.pPacketBuffer:
                            _write_message(
                                {
                                    "type": "error",
                                    "stage": "stream",
                                    "message": "invalid SDK video packet",
                                }
                            )
                            return
                        data = ctypes.string_at(packet.pPacketBuffer, size)
                        _write_message(
                            {
                                "type": "video",
                                "length": size,
                                "payload": base64.b64encode(data).decode("ascii"),
                            }
                        )
                    except Exception:
                        _write_message(
                            {
                                "type": "error",
                                "stage": "stream",
                                "message": "SDK video packet could not be copied",
                            }
                        )

                packet_callback = ES_REALPLAY_CALLBACK(on_packet)
                if not sdk.NET_DVR_SetESRealPlayCallBack(preview_handle, packet_callback, None):
                    code = int(sdk.NET_DVR_GetLastError())
                    sdk.NET_DVR_StopRealPlay(preview_handle)
                    preview_handle = -1
                    packet_callback = None
                    _write_message({"type": "error", "stage": "preview_callback", "code": code})
                    continue
                active_stream = stream_type
                _write_message({"type": "preview_started", "stream_type": stream_type})
            elif command.get("type") == "stop" and active_stream is not None:
                sdk.NET_DVR_StopRealPlay(preview_handle)
                preview_handle = -1
                active_stream = None
                packet_callback = None
                _write_message({"type": "preview_stopped"})

        return 0
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        _write_message({"type": "error", "stage": "configuration", "message": str(exc)[:180]})
        return 12
    finally:
        if sdk is not None and preview_handle >= 0:
            sdk.NET_DVR_StopRealPlay(preview_handle)
        if sdk is not None and user_id >= 0:
            sdk.NET_DVR_Logout_V30(user_id)
        if sdk is not None and initialized:
            sdk.NET_DVR_Cleanup()
        packet_callback = None


def main() -> None:
    if len(sys.argv) != 2:
        _write_message(
            {"type": "error", "stage": "configuration", "message": "plugin path required"}
        )
        raise SystemExit(2)
    try:
        config = json.loads(sys.stdin.readline())
    except (json.JSONDecodeError, OSError):
        _write_message(
            {"type": "error", "stage": "configuration", "message": "invalid worker input"}
        )
        raise SystemExit(2)
    if not isinstance(config, dict):
        _write_message(
            {"type": "error", "stage": "configuration", "message": "invalid worker input"}
        )
        raise SystemExit(2)
    raise SystemExit(_run(Path(sys.argv[1]), config))


if __name__ == "__main__":
    main()
