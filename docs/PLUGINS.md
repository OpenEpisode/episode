# Plugin authoring

Episode `0.1.0-alpha.12` introduces the first versioned contract for
out-of-tree Device and ingress plugins. A plugin can be copied into the mounted
`plugins/` directory and activated through configuration without changing,
rebuilding, or importing from Episode's internal source tree.

The contract is intentionally small and remains experimental during alpha. A
future incompatible contract will use a different `plugin_api` version rather
than silently loading incompatible code.

## What a plugin owns

A plugin may connect to an external Device or protocol, preserve opaque
deliveries, and interpret its own preserved deliveries into normalized Event or
Evidence observations. A Device plugin may also register an assigned camera's
runtime stream and snapshot endpoints. Episode still owns:

- durable raw-artifact and receipt creation;
- Device and Area authority;
- deduplication and correlation;
- Episode lifetime and action policy;
- Evidence storage and portable Episode bundles.

Plugins must import only from `episode.plugin_api`. Modules below
`episode.plugins`, `episode.ingestion`, `episode.storage`, and `episode.engine`
are implementation details and may change without a plugin API version change.

Action and processor plugin kinds are reserved for later contracts. Recording,
snapshots, AI processing, and historical reprocessing are not third-party
extension points in API version 1.

## Directory and manifest

Each plugin occupies one direct child of the mounted plugin directory:

```text
plugins/
└── my-sensor/
    ├── episode-plugin.json
    └── plugin.py
```

`episode-plugin.json` describes the plugin without executing it:

```json
{
  "schema_version": 1,
  "id": "acme-tripwire",
  "name": "Acme Tripwire",
  "version": "0.1.0",
  "plugin_api": "1",
  "kind": "device",
  "entrypoint": "plugin.py:create_plugin",
  "capabilities": ["events"],
  "configuration_schema": {
    "type": "object",
    "properties": {
      "port": {"type": "integer", "minimum": 1, "maximum": 65535}
    }
  }
}
```

Supported version-1 kinds are `device` and `ingress`. The entrypoint must be a
relative `.py` file inside the plugin directory followed by a callable name. A
larger plugin may use `package/__init__.py:create_plugin`; normal relative
imports inside that package are supported. `configuration_schema` documents the
settings contract for tooling and a future generated configuration UI. In this
alpha the plugin remains responsible for validating its setting values.

Episode reads manifests at startup but imports code only for plugins explicitly
enabled in `episode.json`. Unrelated files—including native SDK libraries—are
ignored unless their directory contains a manifest for a configured plugin.

## Activate a plugin

First create the Device and assign its Area in Episode's UI. Then add a top-level
entry to `episode.json`:

```json
{
  "plugins": [
    {
      "id": "acme-tripwire",
      "enabled": true,
      "device_ids": ["garden-tripwire"],
      "settings": {
        "port": 9876
      }
    }
  ]
}
```

`device_ids` is an explicit permission boundary. A Device plugin receives only
the active Devices listed there, including their connection credentials and its
plugin-specific Device configuration. It cannot see credentials belonging to
other Devices through the public context. An unknown or disabled Device makes
the plugin fail with a configuration error rather than silently broadening its
scope.

Restart Episode after changing plugin files or configuration. Plugin state,
version, errors, handler counters, and assigned Device integration state appear
under **System** and **Devices**.

## Implement the lifecycle

The manifest factory receives an `episode.plugin_api.PluginContext` and returns
an object with three methods:

```python
from episode.plugin_api import PluginContext, PluginState, PluginStatus


class MyPlugin:
    def __init__(self, context: PluginContext):
        self.context = context

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def status(self) -> PluginStatus:
        return PluginStatus(state=PluginState.READY)


def create_plugin(context: PluginContext) -> MyPlugin:
    return MyPlugin(context)
```

Do not open connections, start threads, or perform network I/O at module import
time. Validate configuration and allocate runtime resources in the factory or
`start()`. `stop()` must close connections, cancel owned tasks, and return
promptly.

## Preserve before interpreting

Register a handler, then submit exact source bytes through the public ingress
service:

```python
from episode.plugin_api import (
    EventObservation,
    HandlerRegistration,
    HandlerResult,
    RawDelivery,
)


async def interpret(delivery):
    return HandlerResult(
        claimed=True,
        event=EventObservation(
            timestamp=delivery.received_at,
            event_type="tripwire",
            source="acme:tripwire",
        ),
    )


context.ingress.register(
    HandlerRegistration(
        id="events",
        matcher=lambda delivery: delivery.media_type == "application/octet-stream",
        handler=interpret,
    )
)

await context.ingress.submit(
    RawDelivery(
        device_id=context.devices[0].id,
        received_at=received_at,
        payload=exact_source_bytes,
        source="acme:tripwire",
    )
)
```

Episode seals and checksums the `RawDelivery` and creates its receipt before the
handler sees `StoredDelivery`. A malformed payload should therefore return a
claimed `HandlerResult` with `ReceiptStatus.REJECTED`; do not discard it before
submission. Handler exceptions and timeouts reject that receipt and update
health metrics without stopping other handlers.

The adapter supplies the assigned Device and Area to normalized observations.
A plugin cannot redirect an observation to an unassigned Device by changing its
result. Use `dedup_key` only when the source protocol provides a genuinely
stable observation identity.

A `device` plugin's handlers receive only deliveries submitted by that same
plugin, and Episode forces observations back to the assigned Device and Area. An
`ingress` plugin instead registers matchers over deliveries already preserved by
shared non-plugin transports such as HTTP or FTP. It receives no Device
credentials unless `device_ids` are explicitly assigned, but it may return a
parsed `device_id` or `device_address` for core resolution. Because an ingress
plugin can inspect matching shared payloads, enable only plugins you trust.

## Register camera media

A Device plugin that discovers media can make it available to Episode's existing
recording and snapshot actions:

```python
from episode.plugin_api import MediaSource

context.media.register(
    MediaSource(
        device_id=context.devices[0].id,
        stream_uri="rtsp://camera.example/live",
        snapshot_uri="http://camera.example/snapshot.jpg",
        username=context.devices[0].username,
        password=context.devices[0].password,
        profile_token="main",
    )
)
```

Only explicitly assigned Devices may be registered. Call
`context.media.unregister(device_id)` when replacing an endpoint; Episode also
removes media owned by the plugin during shutdown. Media registration is runtime
state: it does not rewrite evidence or editable Device configuration.

## Working example

[`examples/plugins/udp-sensor`](../examples/plugins/udp-sensor) is a complete,
dependency-free Device plugin. It accepts UDP messages in the form
`event_type:active` or `event_type:inactive`, preserves each datagram exactly,
and emits a normalized Event.

To try it from a release checkout:

```bash
cp -a examples/plugins/udp-sensor plugins/udp-sensor
```

Create a Sensor Device in the UI, configure `example-udp-sensor` as shown above,
and expose the configured UDP port with a local `compose.override.yaml`:

```yaml
services:
  episode:
    ports:
      - "9876:9876/udp"
```

After restarting Episode, send a test message:

```bash
printf 'tripwire:active' | nc -u -w1 episode-host 9876
```

The example is educational and is not intended as an authenticated production
protocol.

## Trust and failure boundaries

Third-party plugins are trusted executable Python code. API version 1 is not a
sandbox: a plugin can consume CPU or memory, block the event loop, access files
available to the Episode process, and read credentials for its assigned
Devices. Install only reviewed plugins and keep the `plugins/` mount read-only.

Episode isolates ordinary startup, status, handler, timeout, and shutdown
exceptions so one failing plugin does not prevent other configured integrations
from operating. Native code that requires stronger crash isolation should use a
supervised worker process, as the built-in Hikvision HCNetSDK integration does.
Process-level sandboxing and third-party dependency installation are outside the
Alpha.12 contract.
