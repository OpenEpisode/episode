from __future__ import annotations

# ruff: noqa: N801 - ctypes names intentionally mirror the vendor ABI.
import base64
import ctypes
import json
import os
import signal
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

WORKER_MESSAGE_PREFIX = "EPISODE_HIKVISION_SDK="
MAX_NOTIFICATION_BYTES = 64 * 1024 * 1024

SERIALNO_LEN = 48
NAME_LEN = 32
MACADDR_LEN = 6
DEVICE_ADDRESS_LEN = 129
NET_DEV_NAME_LEN = 64
NET_DEV_TYPE_NAME_LEN = 64
FIRMWARE_VERSION_LEN = 128
NET_DVR_GET_FIRMWARE_VERSION = 3776
NET_DVR_GET_DEVICECFG_V50 = 3801


class NET_DVR_ALARMER(ctypes.Structure):
    _fields_ = [
        ("byUserIDValid", ctypes.c_ubyte),
        ("bySerialValid", ctypes.c_ubyte),
        ("byVersionValid", ctypes.c_ubyte),
        ("byDeviceNameValid", ctypes.c_ubyte),
        ("byMacAddrValid", ctypes.c_ubyte),
        ("byLinkPortValid", ctypes.c_ubyte),
        ("byDeviceIPValid", ctypes.c_ubyte),
        ("bySocketIPValid", ctypes.c_ubyte),
        ("lUserID", ctypes.c_int),
        ("sSerialNumber", ctypes.c_ubyte * SERIALNO_LEN),
        ("dwDeviceVersion", ctypes.c_uint),
        ("sDeviceName", ctypes.c_char * NAME_LEN),
        ("byMacAddr", ctypes.c_ubyte * MACADDR_LEN),
        ("wLinkPort", ctypes.c_ushort),
        ("sDeviceIP", ctypes.c_char * 128),
        ("sSocketIP", ctypes.c_char * 128),
        ("byIpProtocol", ctypes.c_ubyte),
        ("byRes1", ctypes.c_ubyte * 2),
        ("bJSONBroken", ctypes.c_ubyte),
        ("wSocketPort", ctypes.c_ushort),
        ("byRes2", ctypes.c_ubyte * 6),
    ]


class NET_DVR_DEVICEINFO_V30(ctypes.Structure):
    _fields_ = [
        ("sSerialNumber", ctypes.c_ubyte * SERIALNO_LEN),
        ("byAlarmInPortNum", ctypes.c_ubyte),
        ("byAlarmOutPortNum", ctypes.c_ubyte),
        ("byDiskNum", ctypes.c_ubyte),
        ("byDVRType", ctypes.c_ubyte),
        ("byChanNum", ctypes.c_ubyte),
        ("byStartChan", ctypes.c_ubyte),
        ("byAudioChanNum", ctypes.c_ubyte),
        ("byIPChanNum", ctypes.c_ubyte),
        ("byZeroChanNum", ctypes.c_ubyte),
        ("byMainProto", ctypes.c_ubyte),
        ("bySubProto", ctypes.c_ubyte),
        ("bySupport", ctypes.c_ubyte),
        ("bySupport1", ctypes.c_ubyte),
        ("bySupport2", ctypes.c_ubyte),
        ("wDevType", ctypes.c_ushort),
        ("bySupport3", ctypes.c_ubyte),
        ("byMultiStreamProto", ctypes.c_ubyte),
        ("byStartDChan", ctypes.c_ubyte),
        ("byStartDTalkChan", ctypes.c_ubyte),
        ("byHighDChanNum", ctypes.c_ubyte),
        ("bySupport4", ctypes.c_ubyte),
        ("byLanguageType", ctypes.c_ubyte),
        ("byVoiceInChanNum", ctypes.c_ubyte),
        ("byStartVoiceInChanNo", ctypes.c_ubyte),
        ("bySupport5", ctypes.c_ubyte),
        ("bySupport6", ctypes.c_ubyte),
        ("byMirrorChanNum", ctypes.c_ubyte),
        ("wStartMirrorChanNo", ctypes.c_ushort),
        ("bySupport7", ctypes.c_ubyte),
        ("byRes2", ctypes.c_ubyte),
    ]


class NET_DVR_DEVICEINFO_V40(ctypes.Structure):
    _fields_ = [
        ("struDeviceV30", NET_DVR_DEVICEINFO_V30),
        ("bySupportLock", ctypes.c_ubyte),
        ("byRetryLoginTime", ctypes.c_ubyte),
        ("byPasswordLevel", ctypes.c_ubyte),
        ("byProxyType", ctypes.c_ubyte),
        ("dwSurplusLockTime", ctypes.c_uint),
        ("byCharEncodeType", ctypes.c_ubyte),
        ("bySupportDev5", ctypes.c_ubyte),
        ("bySupport", ctypes.c_ubyte),
        ("byLoginMode", ctypes.c_ubyte),
        ("dwOEMCode", ctypes.c_uint),
        ("iResidualValidity", ctypes.c_int),
        ("byResidualValidity", ctypes.c_ubyte),
        ("bySingleStartDTalkChan", ctypes.c_ubyte),
        ("bySingleDTalkChanNums", ctypes.c_ubyte),
        ("byPassWordResetLevel", ctypes.c_ubyte),
        ("bySupportStreamEncrypt", ctypes.c_ubyte),
        ("byMarketType", ctypes.c_ubyte),
        ("byTLSCap", ctypes.c_ubyte),
        ("byRes2", ctypes.c_ubyte * 237),
    ]


class NET_DVR_FIRMWARE_VERSION_INFO(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint),
        ("szFirmwareVersion", ctypes.c_char * FIRMWARE_VERSION_LEN),
        ("byRes2", ctypes.c_ubyte * 128),
    ]


class NET_DVR_DEVICECFG_V50(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint),
        ("sDVRName", ctypes.c_ubyte * NET_DEV_NAME_LEN),
        ("dwDVRID", ctypes.c_uint),
        ("dwRecycleRecord", ctypes.c_uint),
        ("sSerialNumber", ctypes.c_ubyte * SERIALNO_LEN),
        ("dwSoftwareVersion", ctypes.c_uint),
        ("dwSoftwareBuildDate", ctypes.c_uint),
        ("dwDSPSoftwareVersion", ctypes.c_uint),
        ("dwDSPSoftwareBuildDate", ctypes.c_uint),
        ("dwPanelVersion", ctypes.c_uint),
        ("dwHardwareVersion", ctypes.c_uint),
        ("byAlarmInPortNum", ctypes.c_ubyte),
        ("byAlarmOutPortNum", ctypes.c_ubyte),
        ("byRS232Num", ctypes.c_ubyte),
        ("byRS485Num", ctypes.c_ubyte),
        ("byNetworkPortNum", ctypes.c_ubyte),
        ("byDiskCtrlNum", ctypes.c_ubyte),
        ("byDiskNum", ctypes.c_ubyte),
        ("byDVRType", ctypes.c_ubyte),
        ("byChanNum", ctypes.c_ubyte),
        ("byStartChan", ctypes.c_ubyte),
        ("byDecordChans", ctypes.c_ubyte),
        ("byVGANum", ctypes.c_ubyte),
        ("byUSBNum", ctypes.c_ubyte),
        ("byAuxoutNum", ctypes.c_ubyte),
        ("byAudioNum", ctypes.c_ubyte),
        ("byIPChanNum", ctypes.c_ubyte),
        ("byZeroChanNum", ctypes.c_ubyte),
        ("bySupport", ctypes.c_ubyte),
        ("byEsataUseage", ctypes.c_ubyte),
        ("byIPCPlug", ctypes.c_ubyte),
        ("byStorageMode", ctypes.c_ubyte),
        ("bySupport1", ctypes.c_ubyte),
        ("wDevType", ctypes.c_ushort),
        ("byDevTypeName", ctypes.c_ubyte * NET_DEV_TYPE_NAME_LEN),
        ("bySupport2", ctypes.c_ubyte),
        ("byAnalogAlarmInPortNum", ctypes.c_ubyte),
        ("byStartAlarmInNo", ctypes.c_ubyte),
        ("byStartAlarmOutNo", ctypes.c_ubyte),
        ("byStartIPAlarmInNo", ctypes.c_ubyte),
        ("byStartIPAlarmOutNo", ctypes.c_ubyte),
        ("byHighIPChanNum", ctypes.c_ubyte),
        ("byEnableRemotePowerOn", ctypes.c_ubyte),
        ("byRes2", ctypes.c_ubyte * 256),
    ]


class NET_DVR_USER_LOGIN_INFO(ctypes.Structure):
    _fields_ = [
        ("sDeviceAddress", ctypes.c_ubyte * DEVICE_ADDRESS_LEN),
        ("byUseTransport", ctypes.c_ubyte),
        ("wPort", ctypes.c_ushort),
        ("sUserName", ctypes.c_ubyte * 64),
        ("sPassword", ctypes.c_ubyte * 64),
        ("cbLoginResult", ctypes.c_void_p),
        ("pUser", ctypes.c_void_p),
        ("bUseAsynLogin", ctypes.c_int),
        ("byProxyType", ctypes.c_ubyte),
        ("byUseUTCTime", ctypes.c_ubyte),
        ("byLoginMode", ctypes.c_ubyte),
        ("byHttps", ctypes.c_ubyte),
        ("iProxyID", ctypes.c_int),
        ("byVerifyMode", ctypes.c_ubyte),
        ("byRes3", ctypes.c_ubyte * 119),
    ]


class NET_DVR_SETUPALARM_PARAM_V50(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint),
        ("byLevel", ctypes.c_ubyte),
        ("byAlarmInfoType", ctypes.c_ubyte),
        ("byRetAlarmTypeV40", ctypes.c_ubyte),
        ("byRetDevInfoVersion", ctypes.c_ubyte),
        ("byRetVQDAlarmType", ctypes.c_ubyte),
        ("byFaceAlarmDetection", ctypes.c_ubyte),
        ("bySupport", ctypes.c_ubyte),
        ("byBrokenNetHttp", ctypes.c_ubyte),
        ("wTaskNo", ctypes.c_ushort),
        ("byDeployType", ctypes.c_ubyte),
        ("bySubScription", ctypes.c_ubyte),
        ("byBrokenNetHttpV60", ctypes.c_ubyte),
        ("byRes1", ctypes.c_ubyte),
        ("byAlarmTypeURL", ctypes.c_ubyte),
        ("byCustomCtrl", ctypes.c_ubyte),
        ("byRes4", ctypes.c_ubyte * 128),
    ]


MESSAGE_CALLBACK = ctypes.CFUNCTYPE(
    None,
    ctypes.c_int,
    ctypes.POINTER(NET_DVR_ALARMER),
    ctypes.POINTER(ctypes.c_ubyte),
    ctypes.c_uint,
    ctypes.c_void_p,
)

_emit_lock = threading.Lock()


def _emit(message: dict) -> None:
    encoded = (
        WORKER_MESSAGE_PREFIX + json.dumps(message, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode()
    with _emit_lock:
        os.write(sys.stdout.fileno(), encoded)


def _copy_text(target, value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} is required")
    encoded = value.encode("utf-8")
    if len(encoded) >= len(target):
        raise ValueError(f"{field_name} is too long")
    target[: len(encoded)] = encoded


def _configure_sdk(sdk) -> None:
    sdk.NET_DVR_Init.argtypes = []
    sdk.NET_DVR_Init.restype = ctypes.c_bool
    sdk.NET_DVR_Cleanup.argtypes = []
    sdk.NET_DVR_Cleanup.restype = ctypes.c_bool
    sdk.NET_DVR_GetLastError.argtypes = []
    sdk.NET_DVR_GetLastError.restype = ctypes.c_int
    sdk.NET_DVR_SetDVRMessageCallBack_V51.argtypes = [
        ctypes.c_int,
        MESSAGE_CALLBACK,
        ctypes.c_void_p,
    ]
    sdk.NET_DVR_SetDVRMessageCallBack_V51.restype = ctypes.c_bool
    sdk.NET_DVR_Login_V40.argtypes = [
        ctypes.POINTER(NET_DVR_USER_LOGIN_INFO),
        ctypes.POINTER(NET_DVR_DEVICEINFO_V40),
    ]
    sdk.NET_DVR_Login_V40.restype = ctypes.c_int
    sdk.NET_DVR_GetDVRConfig.argtypes = [
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.POINTER(ctypes.c_uint),
    ]
    sdk.NET_DVR_GetDVRConfig.restype = ctypes.c_bool
    sdk.NET_DVR_Logout_V30.argtypes = [ctypes.c_int]
    sdk.NET_DVR_Logout_V30.restype = ctypes.c_bool
    sdk.NET_DVR_SetupAlarmChan_V50.argtypes = [
        ctypes.c_int,
        ctypes.POINTER(NET_DVR_SETUPALARM_PARAM_V50),
        ctypes.c_void_p,
        ctypes.c_uint,
    ]
    sdk.NET_DVR_SetupAlarmChan_V50.restype = ctypes.c_int
    sdk.NET_DVR_CloseAlarmChan_V30.argtypes = [ctypes.c_int]
    sdk.NET_DVR_CloseAlarmChan_V30.restype = ctypes.c_bool


def _sdk_error(sdk, stage: str) -> int:
    code = int(sdk.NET_DVR_GetLastError())
    _emit({"type": "error", "stage": stage, "code": code})
    return code


def _sdk_text(value) -> str | None:
    raw = bytes(value).split(b"\0", 1)[0]
    if not raw:
        return None
    return raw.decode("utf-8", errors="replace").strip() or None


def _format_firmware_version(version: int, build_date: int) -> str | None:
    if not version:
        return None
    if version >> 24:
        version_text = f"V{version >> 24}.{(version >> 16) & 0xFF}.{version & 0xFFFF}"
    else:
        version_text = f"V{(version >> 16) & 0xFFFF}.{version & 0xFFFF}"

    year = (build_date >> 16) & 0xFFFF
    month = (build_date >> 8) & 0xFF
    day = build_date & 0xFF
    if year and 1 <= month <= 12 and 1 <= day <= 31:
        return f"{version_text} build {year:02d}{month:02d}{day:02d}"
    return version_text


def _get_device_info(sdk, user_id: int) -> dict[str, str]:
    info = {"manufacturer": "Hikvision"}
    bytes_returned = ctypes.c_uint()

    firmware = NET_DVR_FIRMWARE_VERSION_INFO()
    firmware.dwSize = ctypes.sizeof(firmware)
    if sdk.NET_DVR_GetDVRConfig(
        user_id,
        NET_DVR_GET_FIRMWARE_VERSION,
        0,
        ctypes.byref(firmware),
        ctypes.sizeof(firmware),
        ctypes.byref(bytes_returned),
    ):
        version = _sdk_text(firmware.szFirmwareVersion)
        if version:
            info["firmware_version"] = version

    device = NET_DVR_DEVICECFG_V50()
    device.dwSize = ctypes.sizeof(device)
    bytes_returned.value = 0
    if sdk.NET_DVR_GetDVRConfig(
        user_id,
        NET_DVR_GET_DEVICECFG_V50,
        0,
        ctypes.byref(device),
        ctypes.sizeof(device),
        ctypes.byref(bytes_returned),
    ):
        model = _sdk_text(device.byDevTypeName)
        if model:
            info["model"] = model
        if "firmware_version" not in info:
            version = _format_firmware_version(
                device.dwSoftwareVersion,
                device.dwSoftwareBuildDate,
            )
            if version:
                info["firmware_version"] = version

    return info


def run(plugin_path: Path, config: dict) -> int:
    stop_event = threading.Event()
    initialized = False
    user_id = -1
    alarm_handle = -1
    callback = None
    sdk = None

    def request_stop(_signum=None, _frame=None) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    try:
        port = config.get("port", 8000)
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")

        sdk = ctypes.CDLL(str(plugin_path / "libhcnetsdk.so"), mode=ctypes.RTLD_GLOBAL)
        _configure_sdk(sdk)
        initialized = bool(sdk.NET_DVR_Init())
        if not initialized:
            _sdk_error(sdk, "initialize")
            return 10

        def on_alarm(command, _alarmer, buffer, buffer_length, _user) -> None:
            try:
                length = int(buffer_length)
                if length < 0 or length > MAX_NOTIFICATION_BYTES:
                    _emit(
                        {
                            "type": "notification_rejected",
                            "command": int(command),
                            "length": length,
                            "reason": "invalid_length",
                        }
                    )
                    return
                payload = ctypes.string_at(buffer, length) if length else b""
                _emit(
                    {
                        "type": "alarm",
                        "command": int(command),
                        "length": length,
                        "received_at": datetime.now(tz=timezone.utc).isoformat(),
                        "payload": base64.b64encode(payload).decode("ascii"),
                    }
                )
            except Exception:
                _emit({"type": "callback_error", "reason": "notification_copy_failed"})

        callback = MESSAGE_CALLBACK(on_alarm)
        if not sdk.NET_DVR_SetDVRMessageCallBack_V51(0, callback, None):
            _sdk_error(sdk, "callback")
            return 11

        login = NET_DVR_USER_LOGIN_INFO()
        _copy_text(login.sDeviceAddress, config.get("address"), "address")
        _copy_text(login.sUserName, config.get("username"), "username")
        _copy_text(login.sPassword, config.get("password"), "password")
        login.wPort = port
        login.bUseAsynLogin = 0

        device_info = NET_DVR_DEVICEINFO_V40()
        user_id = int(sdk.NET_DVR_Login_V40(ctypes.byref(login), ctypes.byref(device_info)))
        if user_id < 0:
            _sdk_error(sdk, "login")
            return 12

        alarm_params = NET_DVR_SETUPALARM_PARAM_V50()
        alarm_params.dwSize = ctypes.sizeof(alarm_params)
        alarm_params.byLevel = 1
        alarm_params.byAlarmInfoType = 1
        alarm_params.byRetAlarmTypeV40 = 1
        alarm_params.byRetVQDAlarmType = 1
        alarm_params.byFaceAlarmDetection = 1
        alarm_params.byDeployType = 1

        alarm_handle = int(
            sdk.NET_DVR_SetupAlarmChan_V50(
                user_id,
                ctypes.byref(alarm_params),
                None,
                0,
            )
        )
        if alarm_handle < 0:
            _sdk_error(sdk, "subscribe")
            return 13

        _emit({"type": "ready", "device_info": _get_device_info(sdk, user_id)})
        while not stop_event.wait(1):
            pass
        return 0
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        _emit({"type": "error", "stage": "configuration", "message": str(exc)})
        return 14
    finally:
        if sdk is not None and alarm_handle >= 0:
            sdk.NET_DVR_CloseAlarmChan_V30(alarm_handle)
        if sdk is not None and user_id >= 0:
            sdk.NET_DVR_Logout_V30(user_id)
        if sdk is not None and initialized:
            sdk.NET_DVR_Cleanup()
        callback = None


def main() -> None:
    if len(sys.argv) != 2:
        _emit({"type": "error", "stage": "configuration", "message": "plugin path required"})
        raise SystemExit(2)
    try:
        config = json.loads(sys.stdin.readline())
    except (json.JSONDecodeError, OSError):
        _emit({"type": "error", "stage": "configuration", "message": "invalid worker input"})
        raise SystemExit(2)
    if not isinstance(config, dict):
        _emit({"type": "error", "stage": "configuration", "message": "invalid worker input"})
        raise SystemExit(2)
    raise SystemExit(run(Path(sys.argv[1]), config))


if __name__ == "__main__":
    main()
