# Native SDKs

This directory is mounted read-only at `/opt/episode/plugins` by Docker
Compose. It holds optional, user-supplied native SDK runtime files; it is not a
general-purpose code plugin directory.

Vendor binaries below this directory are ignored by Git and excluded from the
container build context. See the
[Hikvision setup guide](../docs/HIKVISION_SETUP.md#hikvision-hcnetsdk) for the
expected `hikvision-sdk/` layout.
