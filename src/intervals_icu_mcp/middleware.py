"""Middleware for Intervals.icu MCP server.

This module provides middleware components that run before tool execution.
"""

from collections.abc import Callable
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext

from .auth import ICUConfig, load_config, validate_credentials

# Claim that the Intervals.icu OAuth token verifier puts on every AccessToken it
# accepts. Its presence is what marks a request as an Intervals.icu bearer request:
# AccessToken.token is then the upstream Intervals.icu access token.
INTERVALS_ATHLETE_CLAIM = "intervals_icu_athlete_id"

CREDENTIALS_MISSING_MESSAGE = (
    "Intervals.icu credentials not configured. Please run 'icu-mcp-auth' to set up authentication."
)


def resolve_request_config() -> ICUConfig:
    """Build the Intervals.icu config for the current request.

    - Request authenticated through the Intervals.icu OAuth provider: **bearer mode**.
      The request's own upstream token is used with athlete ``0``, and the operator's
      API key is dropped from the config, so it can never be used for this request.
    - Otherwise (stdio, the secret-path endpoint, or no auth configured): **API-key
      mode**, unchanged from before.

    Raises:
        ToolError: when no usable credentials are available.
    """
    config = load_config()

    access_token = get_access_token()
    claims: dict[str, Any] = (access_token.claims or {}) if access_token is not None else {}
    if access_token is not None and INTERVALS_ATHLETE_CLAIM in claims:
        return config.with_bearer_token(
            access_token.token, athlete_id=str(claims[INTERVALS_ATHLETE_CLAIM])
        )

    if not validate_credentials(config):
        raise ToolError(CREDENTIALS_MISSING_MESSAGE)
    return config


class ConfigMiddleware(Middleware):
    """Middleware that resolves the Intervals.icu configuration for every tool call.

    This middleware:
    1. Builds the per-request config (bearer token or API key, see resolve_request_config)
    2. Validates that credentials are available
    3. Injects the config into the context state for tools to access via ctx.get_state("config")
    4. Raises ToolError if authentication is not configured
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next: Callable[..., Any]):
        """Resolve and validate config before every tool call."""
        config = resolve_request_config()

        # Inject config into context state for tools to access
        if context.fastmcp_context:
            await context.fastmcp_context.set_state("config", config, serializable=False)

        # Continue to the tool execution
        return await call_next(context)
