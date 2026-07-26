"""Shared semantic contract for Magalu single-operation GraphQL responses."""


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
