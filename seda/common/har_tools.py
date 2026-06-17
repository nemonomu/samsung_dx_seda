import base64
import json
from pathlib import Path
from urllib.parse import urlparse


SENSITIVE_HEADER_NAMES = {"authorization", "cookie", "set-cookie", "x-api-key"}


def load_har(path):
    return json.loads(Path(path).read_text(encoding="utf-8", errors="ignore"))


def decoded_response_text(entry):
    content = entry.get("response", {}).get("content", {})
    text = content.get("text", "") or ""
    if not text:
        return ""
    if content.get("encoding") == "base64" or looks_like_base64(text):
        try:
            return base64.b64decode(text).decode("utf-8", errors="replace")
        except Exception:
            return text
    return text


def looks_like_base64(value):
    sample = value[:120]
    if len(sample) < 24:
        return False
    return all(char.isalnum() or char in "+/=" for char in sample)


def safe_endpoint(url):
    parsed = urlparse(url)
    return f"{parsed.netloc}{parsed.path}"


def request_body(entry):
    return entry.get("request", {}).get("postData", {}).get("text", "") or ""


def response_json(entry):
    text = decoded_response_text(entry)
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def find_product_arrays(value, path=""):
    arrays = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            if isinstance(child, list) and child and looks_like_product(child[0]):
                arrays.append((child_path, child))
            arrays.extend(find_product_arrays(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value[:5]):
            arrays.extend(find_product_arrays(child, f"{path}[{index}]"))
    return arrays


def find_review_arrays(value, path=""):
    arrays = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            if isinstance(child, list) and child and looks_like_review(child[0]):
                arrays.append((child_path, child))
            arrays.extend(find_review_arrays(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value[:5]):
            arrays.extend(find_review_arrays(child, f"{path}[{index}]"))
    return arrays


def looks_like_product(value):
    if not isinstance(value, dict):
        return False
    keys = {str(key).lower() for key in value}
    return bool(keys & {"sku", "idsku", "id", "title", "name", "price", "url"}) and bool(
        keys & {"title", "name", "url", "price", "oldprice", "precovenda"}
    )


def looks_like_review(value):
    if not isinstance(value, dict):
        return False
    keys = {str(key).lower() for key in value}
    content_keys = {
        "review",
        "reviews",
        "comment",
        "comments",
        "content",
        "description",
        "message",
        "text",
        "title",
        "body",
    }
    rating_keys = {"rating", "score", "stars", "star", "grade"}
    author_keys = {"author", "name", "nickname", "user", "customer"}
    return bool(keys & content_keys) and (bool(keys & rating_keys) or bool(keys & author_keys))


def summarize_har(path):
    har = load_har(path)
    summaries = []
    for index, entry in enumerate(har.get("log", {}).get("entries", [])):
        request = entry.get("request", {})
        response = entry.get("response", {})
        body = request_body(entry)
        parsed_response = response_json(entry)
        product_arrays = find_product_arrays(parsed_response) if parsed_response is not None else []
        review_arrays = find_review_arrays(parsed_response) if parsed_response is not None else []
        if not product_arrays and not any(token in request.get("url", "").lower() for token in ["graphql", "api", "search", "produto", "product"]):
            if not review_arrays and not any(token in request.get("url", "").lower() for token in ["review", "avali", "comment", "rating"]):
                continue
        summaries.append(
            {
                "index": index,
                "method": request.get("method", ""),
                "status": response.get("status", ""),
                "endpoint": safe_endpoint(request.get("url", "")),
                "request_body_length": len(body),
                "response_length": len(decoded_response_text(entry)),
                "product_arrays": [
                    {
                        "path": array_path,
                        "count": len(products),
                        "first_keys": list(products[0].keys())[:30] if products else [],
                    }
                    for array_path, products in product_arrays
                ],
                "review_arrays": [
                    {
                        "path": array_path,
                        "count": len(reviews),
                        "first_keys": list(reviews[0].keys())[:30] if reviews else [],
                    }
                    for array_path, reviews in review_arrays
                ],
            }
        )
    return summaries
