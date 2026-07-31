from __future__ import annotations

import ctypes
import json
import os
import sys
from pathlib import Path

from episode.plugins.manager import PROBE_RESULT_PREFIX


def _emit(payload: dict) -> None:
    message = PROBE_RESULT_PREFIX + json.dumps(payload, separators=(",", ":")) + "\n"
    os.write(sys.stdout.fileno(), message.encode())


def _version_string(encoded: int) -> str:
    return ".".join(
        str(part)
        for part in (
            (encoded >> 24) & 0xFF,
            (encoded >> 16) & 0xFF,
            (encoded >> 8) & 0xFF,
            encoded & 0xFF,
        )
    )


def probe(plugin_path: Path) -> int:
    library_path = plugin_path / "libhcnetsdk.so"
    initialized = False
    try:
        sdk = ctypes.CDLL(str(library_path), mode=ctypes.RTLD_GLOBAL)
        sdk.NET_DVR_Init.argtypes = []
        sdk.NET_DVR_Init.restype = ctypes.c_bool
        sdk.NET_DVR_Cleanup.argtypes = []
        sdk.NET_DVR_Cleanup.restype = ctypes.c_bool
        sdk.NET_DVR_GetSDKBuildVersion.argtypes = []
        sdk.NET_DVR_GetSDKBuildVersion.restype = ctypes.c_uint

        initialized = bool(sdk.NET_DVR_Init())
        if not initialized:
            _emit({"ok": False, "error": "HCNetSDK initialization failed."})
            return 1

        version = _version_string(int(sdk.NET_DVR_GetSDKBuildVersion()))
        cleaned_up = bool(sdk.NET_DVR_Cleanup())
        initialized = False
        if not cleaned_up:
            _emit({"ok": False, "error": "HCNetSDK cleanup failed."})
            return 1

        _emit({"ok": True, "version": version})
        return 0
    except (AttributeError, OSError) as exc:
        _emit({"ok": False, "error": f"HCNetSDK could not be loaded: {exc}"})
        return 1
    finally:
        if initialized:
            sdk.NET_DVR_Cleanup()


def main() -> None:
    if len(sys.argv) != 2:
        _emit({"ok": False, "error": "Expected one plugin directory argument."})
        raise SystemExit(2)
    raise SystemExit(probe(Path(sys.argv[1])))


if __name__ == "__main__":
    main()
