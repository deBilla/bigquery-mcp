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


# Limits from the published registry schema
# (static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json).
# They are enforced only at publish time, which is after the tag is pushed and
# after PyPI has already accepted the release -- so an over-long field cannot
# be fixed in place, only in a new version. Asserting them here moves the
# failure to a pull request. v0.1.0's registry publish failed on exactly this.
REGISTRY_LIMITS = {"name": (3, 200), "title": (1, 100), "description": (1, 100)}


@pytest.mark.parametrize("field,bounds", sorted(REGISTRY_LIMITS.items()))
def test_manifest_fields_fit_the_registry_schema(manifest, field, bounds):
    low, high = bounds
    value = manifest[field]
    assert low <= len(value) <= high, (
        f"server.json {field} is {len(value)} chars; the registry allows "
        f"{low}-{high} and rejects the publish otherwise"
    )


def test_the_readme_proves_ownership_of_the_pypi_package(manifest):
    """The registry checks that the PyPI README carries the server name, as
    proof that whoever owns the package also owns the namespace.

    It can only check this against a README already on PyPI, so a missing
    marker is discovered after the release is irreversibly published and costs
    a version bump. v0.1.1 was spent on exactly that.
    """
    marker = f"mcp-name: {manifest['name']}"
    readme = (ROOT / "README.md").read_text()
    assert marker in readme, (
        f"README.md must contain the literal line {marker!r}, or the registry "
        "rejects the publish after PyPI has already accepted it"
    )


def test_the_documented_site_is_actually_in_the_repository(manifest):
    """websiteUrl is published to the registry, so a missing page is a dead
    link on someone else's listing rather than a local mistake."""
    if "websiteUrl" not in manifest:
        pytest.skip("no website declared")
    page = ROOT / "docs" / "index.html"
    assert page.exists(), "server.json declares a websiteUrl but docs/ has no page"
    # GitHub Pages runs Jekyll unless told not to, which silently drops files
    # and directories beginning with an underscore.
    assert (ROOT / "docs" / ".nojekyll").exists(), "docs/.nojekyll is missing"
    assert "<title>" in page.read_text()[:2000]


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
