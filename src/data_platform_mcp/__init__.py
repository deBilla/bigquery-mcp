"""Read-only BigQuery MCP server."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Read from the installed distribution rather than hardcoding a literal, so
    # `--version` cannot drift from what pyproject.toml declares. That leaves
    # two places to bump for a release -- pyproject.toml and server.json -- and
    # the release workflow refuses a tag where those disagree.
    __version__ = version("data-platform-mcp")
except PackageNotFoundError:  # a source tree that was never installed
    __version__ = "0.0.0+unknown"
