# Security policy

Episode handles device credentials and potentially sensitive evidence. Please do
not publish credentials, private camera addresses, raw evidence, or security
vulnerability details in a public issue.

## Supported versions

During pre-1.0 development, only the most recent tagged prerelease receives
security fixes. The project is not yet suitable for direct Internet exposure
and does not provide authentication.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository under
**Security → Advisories → Report a vulnerability**. If that option is not
available, open a public issue containing no technical details and ask the
maintainer to establish private contact.

Please include the affected Episode version, deployment environment, impact, and
minimal reproduction steps. Remove all passwords, tokens, private addresses, and
captured evidence.

General bugs and hardening suggestions that do not expose a vulnerability can be
reported through the public issue tracker.

## Deployment and plugin trust

Treat Episode, its Event and Alarm Server endpoints, FTP service, camera
network, and mounted plugin directory as one trusted local security boundary.
Third-party plugins execute as trusted code inside the Episode process and are
not sandboxed. Review them before installation and keep the plugin mount
read-only.

The unauthenticated API also exposes operational configuration, including the
active Capture profile. A client with network access can select a restrictive
or empty profile and prevent later active Events from starting capture. Limit
UI/API access with the host firewall or a trusted reverse proxy until Episode
provides authentication and authorization.

Treat an Episode-start webhook URL as a credential: Discord and similar URLs
contain bearer-like secret tokens. Configure it through **System →
Notifications**, do not publish it in diagnostics or issues, and rotate it if
it is exposed. Episode stores the URL in its local SQLite settings but does not
return or log it; the UI displays only a fixed mask and whether a destination
exists. Prefer
HTTPS; plain HTTP is appropriate only inside a trusted network where
interception is acceptable. The notification payload contains operational
Episode, Area, Device, Event, and timestamp information, but no raw payload,
Evidence bytes, camera credentials, or absolute filesystem paths. The optional
global External Episode URL is not secret and is returned by the installation
settings API; it is
operator-supplied rather than inferred from an untrusted request host and is
validated without credentials, queries, or fragments.

SHA-256 checksums detect accidental or later byte changes; they are not a
signature, trusted timestamp, or legal chain of custody. Signed manifests and
external timestamping remain future hardening work.
