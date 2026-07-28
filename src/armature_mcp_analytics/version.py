"""SDK identity reported to Armature ingest (batch ``sdk`` field, User-Agent).

The version comes from the installed distribution's metadata, never a
hardcoded string, so it always matches what the customer actually installed.
A source tree without an installed distribution reports the development
placeholder instead of impersonating a release.
"""

from __future__ import annotations

from importlib import metadata

SDK_LANGUAGE = "python"


def _detect_version() -> str:
    try:
        return metadata.version("armature-mcp-analytics")
    except metadata.PackageNotFoundError:
        return "0.0.0-development"


SDK_VERSION = _detect_version()

SDK_IDENTITY = {"language": SDK_LANGUAGE, "version": SDK_VERSION}

SDK_USER_AGENT = f"armature-mcp-analytics-python/{SDK_VERSION}"
