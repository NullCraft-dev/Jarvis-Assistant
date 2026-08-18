"""arXiv metadata provider shared by native and MCP adapters."""

from __future__ import annotations

import hashlib
import re
import time
import xml.etree.ElementTree as ET
from typing import Any, Callable

import httpx

_API_URL = "https://export.arxiv.org/api/query"
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_WHITESPACE = re.compile(r"\s+")
_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV = "{http://arxiv.org/schemas/atom}"
_MAX_ATTEMPTS = 3
_REQUEST_TIMEOUT_SECONDS = 15.0
_DEFAULT_RETRY_AFTER_SECONDS = 10
_MIN_RETRY_AFTER_SECONDS = 3
_MAX_RETRY_AFTER_SECONDS = 30


class ArxivProviderError(RuntimeError):
    """Safe, classified failure returned by the first-party provider adapter."""

    def __init__(
        self,
        message: str,
        *,
        attempts: int,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.status_code = status_code


class ArxivRateLimitedError(ArxivProviderError):
    """The provider remained rate-limited after bounded retries."""

    def __init__(self, retry_after_seconds: int, *, attempts: int = 1) -> None:
        super().__init__(
            "arXiv 请求频率受限，请稍后重试",
            attempts=attempts,
            status_code=429,
        )
        self.retry_after_seconds = retry_after_seconds


class ArxivTimeoutError(ArxivProviderError):
    """All bounded attempts timed out."""

    def __init__(self, attempts: int) -> None:
        super().__init__("arXiv 请求超时", attempts=attempts)


class ArxivUnavailableError(ArxivProviderError):
    """Transport or retryable upstream failure exhausted its budget."""

    def __init__(self, attempts: int, *, status_code: int | None = None) -> None:
        super().__init__(
            "arXiv 服务暂时不可用",
            attempts=attempts,
            status_code=status_code,
        )


class ArxivRequestRejectedError(ArxivProviderError):
    """The provider rejected a non-retryable request."""

    def __init__(self, status_code: int, *, attempts: int) -> None:
        super().__init__(
            "arXiv 拒绝了检索请求",
            attempts=attempts,
            status_code=status_code,
        )


class ArxivResponseError(ArxivProviderError):
    """The provider returned an unsafe or malformed response."""

    def __init__(self, *, attempts: int) -> None:
        super().__init__("arXiv 返回了无效元数据", attempts=attempts)


def normalize_query(query: str) -> str:
    normalized = _WHITESPACE.sub(" ", query).strip()
    if not normalized or len(normalized) > 300:
        raise ValueError("query 长度必须为 1 到 300 个字符")
    return normalized


def search_arxiv_metadata(
    query: str,
    max_results: int = 5,
    sort_by: str = "submittedDate",
    *,
    delay: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    normalized = normalize_query(query)
    if not isinstance(max_results, int) or isinstance(max_results, bool) or not 1 <= max_results <= 10:
        raise ValueError("max_results 必须在 1 到 10 之间")
    if sort_by not in {"relevance", "lastUpdatedDate", "submittedDate"}:
        raise ValueError("sort_by 不受支持")

    # arXiv legacy API asks clients to keep a single connection and wait at least
    # three seconds between requests. A conservative delay also works when this
    # provider is invoked through a short-lived stdio MCP process.
    delay(3)
    params = {
        "search_query": f'all:"{normalized.replace(chr(34), " ")}"',
        "start": "0",
        "max_results": str(max_results),
        "sortBy": sort_by,
        "sortOrder": "descending",
    }
    headers = {
        "Accept": "application/atom+xml",
        "User-Agent": "Jarvis-Assistant/0.1 (personal research assistant)",
    }
    with httpx.Client(
        timeout=httpx.Timeout(_REQUEST_TIMEOUT_SECONDS, connect=5.0),
        follow_redirects=False,
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        headers=headers,
    ) as client:
        response = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = client.get(_API_URL, params=params)
            except httpx.TimeoutException as exc:
                if attempt >= _MAX_ATTEMPTS:
                    raise ArxivTimeoutError(attempt) from exc
                delay(_transport_retry_delay(attempt))
                continue
            except httpx.TransportError as exc:
                if attempt >= _MAX_ATTEMPTS:
                    raise ArxivUnavailableError(attempt) from exc
                delay(_transport_retry_delay(attempt))
                continue

            if response.status_code == 429:
                retry_after = _bounded_retry_after(response.headers.get("Retry-After"))
                if attempt >= _MAX_ATTEMPTS:
                    raise ArxivRateLimitedError(retry_after, attempts=attempt)
                delay(retry_after)
                continue
            if 500 <= response.status_code <= 599:
                if attempt >= _MAX_ATTEMPTS:
                    raise ArxivUnavailableError(
                        attempt, status_code=response.status_code
                    )
                delay(_transport_retry_delay(attempt))
                continue
            if response.is_error:
                raise ArxivRequestRejectedError(
                    response.status_code, attempts=attempt
                )
            break
        assert response is not None
    if len(response.content) > _MAX_RESPONSE_BYTES:
        raise ArxivResponseError(attempts=attempt)
    try:
        results = parse_feed(response.content)
    except ValueError as exc:
        raise ArxivResponseError(attempts=attempt) from exc
    return {
        "source": "arxiv",
        "query": normalized,
        "result_count": len(results),
        "results": results,
        "attribution": "Thank you to arXiv for use of its open access interoperability.",
    }


def _transport_retry_delay(attempt: int) -> int:
    """Return a deterministic, arXiv-compliant bounded backoff."""
    return min(_MIN_RETRY_AFTER_SECONDS * attempt, _MAX_RETRY_AFTER_SECONDS)


def parse_feed(content: bytes) -> list[dict[str, Any]]:
    if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        raise ValueError("arXiv XML 包含不允许的声明")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError("arXiv 返回了无效 Atom XML") from exc

    results: list[dict[str, Any]] = []
    for entry in root.findall(f"{_ATOM}entry"):
        identifier = _text(entry, f"{_ATOM}id")
        arxiv_id = identifier.rstrip("/").rsplit("/", 1)[-1]
        authors = [_clean(_text(author, f"{_ATOM}name"), 200) for author in entry.findall(f"{_ATOM}author")[:20]]
        categories = [value for node in entry.findall(f"{_ATOM}category")[:20] if (value := node.attrib.get("term", ""))]
        abstract_url = ""
        pdf_url = ""
        for link in entry.findall(f"{_ATOM}link"):
            href = link.attrib.get("href", "")
            if link.attrib.get("rel") == "alternate":
                abstract_url = href
            if link.attrib.get("type") == "application/pdf":
                pdf_url = href.replace("http://", "https://", 1)
        if not abstract_url:
            abstract_url = f"https://arxiv.org/abs/{arxiv_id}"
        if not pdf_url:
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
        primary = entry.find(f"{_ARXIV}primary_category")
        abstract = _clean(_text(entry, f"{_ATOM}summary"), 3000)
        canonical_url = abstract_url.replace("http://", "https://", 1)
        content_sha256 = hashlib.sha256(abstract.encode("utf-8")).hexdigest()
        results.append({
            "source": "arxiv", "arxiv_id": arxiv_id,
            "source_id": f"arxiv:{arxiv_id}",
            "source_type": "literature",
            "title": _clean(_text(entry, f"{_ATOM}title"), 500),
            "authors": authors, "abstract": abstract,
            "published": _text(entry, f"{_ATOM}published"), "updated": _text(entry, f"{_ATOM}updated"),
            "categories": categories, "primary_category": primary.attrib.get("term", "") if primary is not None else "",
            "doi": _text(entry, f"{_ARXIV}doi"), "journal_reference": _text(entry, f"{_ARXIV}journal_ref"),
            "abstract_url": canonical_url, "pdf_url": pdf_url,
            "canonical_url": canonical_url,
            "content_scope": "abstract",
            "content_text": abstract,
            "content_locators": ["abstract"],
            "content_sha256": content_sha256,
            "download": {
                "available": True,
                "reference": arxiv_id,
                "mime_type": "application/pdf",
                "url": pdf_url,
            },
        })
    return results


def _text(node: ET.Element, path: str) -> str:
    child = node.find(path)
    return child.text.strip() if child is not None and child.text else ""


def _clean(value: str, max_chars: int) -> str:
    return _WHITESPACE.sub(" ", value).strip()[:max_chars]


def _bounded_retry_after(raw_value: str | None) -> int:
    try:
        seconds = int(raw_value or "")
    except ValueError:
        seconds = _DEFAULT_RETRY_AFTER_SECONDS
    return max(_MIN_RETRY_AFTER_SECONDS, min(seconds, _MAX_RETRY_AFTER_SECONDS))
