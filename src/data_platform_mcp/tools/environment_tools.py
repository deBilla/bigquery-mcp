"""Reporting this server's own configuration.

The only tool that touches nothing outside this process, which is why it is
registered as closed-world. An agent that is unsure which environment a
question refers to should call this rather than guess: guessing means answering
a question about one warehouse from another, and nothing in the answer would
reveal it.
"""

from __future__ import annotations

from ..config import get_settings
from ..registration import register_tool


def list_environments() -> dict:
    """List the configured BigQuery environments and which one is the default.

    Call this when the user names an environment you have not seen, or when a
    question could plausibly be about more than one. Free — reads only this
    server's configuration.
    """
    settings = get_settings()
    return {
        "default_environment": settings.default_environment,
        "count": len(settings.environments),
        "environments": [
            {
                "name": env.name,
                "project": env.project,
                "location": env.location,
                "aliases": list(env.aliases),
                # Named so an agent can explain a refusal before triggering it.
                "readable_datasets": sorted(env.dataset_allowlist) or "all",
                "is_default": env.name == settings.default_environment,
            }
            for env in settings.environments
        ],
    }


def register(mcp) -> None:
    register_tool(mcp, list_environments, open_world=False)
