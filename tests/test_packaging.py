"""Distribution metadata that is published and therefore must not drift.

The release workflow checks these too, but only when a tag is pushed -- by
which point a mismatch has already been committed and the fix is a retagging
exercise. Checking on every run turns that into a failing pull request.

These read the repository's own files, so they are meaningful only in a
checkout; a wheel-only install has no pyproject.toml and the suite does not
ship inside the wheel.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

import data_platform_mcp

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
SERVER_JSON = ROOT / "server.json"

pytestmark = pytest.mark.skipif(
    not PYPROJECT.exists(), reason="not running from a source checkout"
)


@pytest.fixture(scope="module")
def project():
    return tomllib.loads(PYPROJECT.read_text())["project"]


@pytest.fixture(scope="module")
def manifest():
    return json.loads(SERVER_JSON.read_text())


def test_reported_version_is_not_the_uninstalled_fallback():
    # If this fires, the package is not installed and every other version
    # assertion below would be comparing against a placeholder.
    assert data_platform_mcp.__version__ != "0.0.0+unknown"


def test_reported_version_matches_the_distribution(project):
    """`--version` is read from installed metadata precisely so this holds."""
    assert data_platform_mcp.__version__ == project["version"]


def test_the_registry_manifest_agrees_on_the_version(project, manifest):
    versions = {manifest["version"]} | {p["version"] for p in manifest["packages"]}
    assert versions == {project["version"]}


def test_the_registry_manifest_points_at_the_right_package(project, manifest):
    """The registry stores a pointer and resolves it on PyPI; a wrong
    identifier publishes a listing that installs someone else's package."""
    assert manifest["packages"][0]["identifier"] == project["name"]
    assert manifest["packages"][0]["registryType"] == "pypi"


def test_the_console_script_points_at_a_real_entry_point(project):
    target = project["scripts"]["data-platform-mcp"]
    module, _, attr = target.partition(":")
    imported = __import__(module, fromlist=[attr])
    assert callable(getattr(imported, attr))


def test_the_manifest_declares_stdio_transport(manifest):
    assert manifest["packages"][0]["transport"]["type"] == "stdio"


def test_documented_environment_variables_are_ones_the_server_reads(manifest):
    """A manifest naming a variable the code ignores is a lie told to every
    client that renders it during setup."""
    source = "\n".join(
        path.read_text() for path in (ROOT / "src").rglob("*.py")
    )
    for variable in manifest["packages"][0]["environmentVariables"]:
        assert variable["name"] in source, f"{variable['name']} is never read"
