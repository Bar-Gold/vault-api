"""Shared request plumbing for the outbound clients.

Both clients need the same two things, and getting either wrong shows up as an opaque 500:

1. **Transport failures must be mapped.** A DNS failure, refused connection or read timeout
   raises `httpx.RequestError`, which is *not* an HTTP response — without this it escapes the
   client, past the routes' `except ExternalServiceError`, and the caller gets
   "Internal Server Error" with no clue which system was unreachable.
2. **Non-2xx responses must be mapped**, with a service-specific way of digging the message out
   of the body.

Everything funnels into the library's `ExternalServiceError`, which the routes turn into a 502
(or 504 for timeouts).
"""

from typing import Any, Callable, Optional

import httpx
from loguru import logger
from tashtiot_apis_library.connectors import ExternalServiceError

# Used for both a transport timeout and a gateway-style failure, so the route can tell the
# caller "the upstream was too slow" rather than "the upstream said no".
GATEWAY_TIMEOUT = 504
BAD_GATEWAY = 502


def default_detail(response: Any) -> str:
    """Fallback body extraction: JSON as-is, else raw text, always length-capped."""
    try:
        return str(response.json())[:500]
    except ValueError:
        return (response.text or "")[:500]


async def request(
    client: Any,
    method: str,
    url: str,
    service_name: str,
    detail_from_response: Optional[Callable[[Any], str]] = None,
    **kwargs: Any,
) -> Any:
    """Perform a request, raising `ExternalServiceError` for transport *and* HTTP failures."""
    try:
        response = await client.request(method, url, **kwargs)

    except httpx.TimeoutException as exc:
        detail = f"Timed out calling {service_name} at {url}: {exc}"
        logger.error(detail)
        raise ExternalServiceError(
            service_name=service_name, detail=detail, status_code=GATEWAY_TIMEOUT
        ) from exc

    except httpx.RequestError as exc:
        # Connect errors, DNS failures, TLS failures, protocol errors.
        detail = f"Could not reach {service_name} at {url}: {exc.__class__.__name__}: {exc}"
        logger.error(detail)
        raise ExternalServiceError(
            service_name=service_name, detail=detail, status_code=BAD_GATEWAY
        ) from exc

    if response.status_code >= 400:
        extract = detail_from_response or default_detail
        detail = extract(response)
        logger.error(f"{service_name} {method} {url} failed ({response.status_code}): {detail}")
        raise ExternalServiceError(
            service_name=service_name, detail=detail, status_code=response.status_code
        )

    return response
