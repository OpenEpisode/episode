"""Manifest-based loading for explicitly configured third-party plugins."""

from episode.plugins.external.discovery import MANIFEST_FILENAME, discover_external_plugins

__all__ = ["MANIFEST_FILENAME", "discover_external_plugins"]
