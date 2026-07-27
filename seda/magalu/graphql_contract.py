"""Shared semantic contract for Magalu single-operation GraphQL responses."""


_TERMINAL_SHIPPING_MESSAGES = {
    "Frete indisponível para sua região.",
    "Produto esgotado!",
}


def graphql_envelope_error(payload, *, require_item=False):
    """Return a stable error token when a decoded GraphQL envelope is invalid.

    This validator is intentionally for the existing single-operation request
    paths.  Raw/batched transports may legitimately use a top-level list and
    must not call it.
    """
    if not isinstance(payload, dict):
        return "invalid_json"
    if payload.get("errors"):
        return "graphql_errors"
    if "data" not in payload:
        return "graphql_data_missing"
    data = payload.get("data")
    if not isinstance(data, dict):
        return "graphql_data_invalid_type"
    if require_item and not data.get("item"):
        return "graphql_item_missing"
    return ""


def graphql_terminal_business_error(operation, payload):
    """Return a non-retryable business-result token for a known operation.

    The envelope remains a GraphQL error and must not be consumed as normal
    operation data.  This classifier only tells retry loops when another
    identical request cannot improve the result.
    """
    if operation != "shippingQuery" or not isinstance(payload, dict):
        return ""
    errors = payload.get("errors")
    if not isinstance(errors, list) or not errors:
        return ""
    if all(_is_terminal_shipping_error(error) for error in errors):
        return "shipping_not_available"
    return ""


def _is_terminal_shipping_error(error):
    if not isinstance(error, dict):
        return False
    path = error.get("path")
    extensions = error.get("extensions")
    if path != ["shipping"] or not isinstance(extensions, dict):
        return False
    return (
        str(error.get("message") or "").strip() in _TERMINAL_SHIPPING_MESSAGES
        and str(extensions.get("service") or "").strip().casefold() == "freight"
        and str(extensions.get("code") or "").strip().casefold() == "not_available"
        and str(extensions.get("status") or "").strip().casefold()
        == "resource_not_found"
    )
