"""Safe request diagnostics and bounded retry policy for the private builder agent."""

import httpx

MESSAGES = {
    "timeout": "The builder timed out while waiting for the model. Try a smaller request or a faster model.",
    "authentication": "The model provider rejected its credentials. Check the server-side provider key or Ollama sign-in.",
    "permission": "The model provider denied access. Check account permissions for the selected model.",
    "model_missing": "The model provider could not find the selected model. Check its name and endpoint.",
    "rate_limit": "The model provider is rate-limiting requests or has no available quota. Wait and retry or choose another model.",
    "provider_unavailable": "The model provider is temporarily unavailable. Retry later or choose another model.",
    "connection": "The builder could not maintain its model/server connection. Check that Ollama or the configured provider is reachable.",
    "context_limit": "The request exceeds the model's context limit. Select fewer source files or a model with a larger context window.",
    "provider_request": "The model provider rejected the request format or options. Check that the selected model supports tools.",
    "model_error": "The model call failed inside the Harnest builder agent.",
    "server_auth": "Studio's private agent rejected its session credentials. Restart Studio to reconnect.",
    "invalid_response": "The Harnest builder server returned an invalid response.",
    "runtime_error": "The Harnest builder runtime failed while processing the request.",
}
RETRYABLE = {"timeout", "rate_limit", "provider_unavailable", "connection"}


def failure_category(error: Exception, diagnostic: dict, session: str | None) -> str:
    """Prefer session-matched native model evidence; never display arbitrary response text."""
    category = diagnostic.get("category")
    if diagnostic.get("session") == session and session and isinstance(category, str) and category in MESSAGES:
        return category
    if isinstance(error, httpx.TimeoutException):
        return "timeout"
    if isinstance(error, httpx.TransportError):
        return "connection"
    if isinstance(error, httpx.HTTPStatusError):
        return {401: "server_auth", 403: "server_auth", 429: "rate_limit", 502: "provider_unavailable",
                503: "provider_unavailable", 504: "timeout"}.get(error.response.status_code, "runtime_error")
    return "invalid_response"
