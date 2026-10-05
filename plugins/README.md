# Runtime plugins

Docker Compose mounts this directory read-only at `/opt/episode/plugins`.
It holds two kinds of optional, user-supplied content:

- Native SDK runtime files used by built-in integrations. See the
  [Hikvision SDK layout and activation](../docs/HIKVISION_SETUP.md#hikvision-hcnetsdk).
- External Device or ingress plugins, each in its own directory with an
  `episode-plugin.json` manifest and Python entrypoint. See
  [plugin authoring and activation](../docs/PLUGINS.md).

Installed files do not activate an integration. Built-in Device integrations
are enabled through Device configuration. External plugins require an explicit
entry in `episode.json`'s top-level `plugins` array and an application restart;
Device plugins receive only their assigned Devices through the public context.

External plugins are trusted executable code, not sandboxed extensions. Native
SDKs remain user-supplied and must not be redistributed without permission.
All content here except this README is ignored by Git and excluded from the
container build context. The maintained external example lives in
[`examples/plugins/udp-sensor`](../examples/plugins/udp-sensor), where it can be
reviewed and tested before copying it into this runtime directory.
