"""从 arXiv 下载 PDF 到 Jarvis Artifact Store。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse
from uuid import UUID

import httpx

from jarvis_worker.agent.artifacts.file_store import LocalArtifactFileStore
from jarvis_worker.agent.tool_gateway.contracts import (
    ToolDeliverable,
    ToolRequest,
    ToolResult,
)
from jarvis_worker.shared.storage_capacity import StorageCapacityExceeded

_ARXIV_ID = re.compile(
    r"^(?:\d{4}\.\d{4,5}|[a-z][a-z0-9.-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?$",
    re.IGNORECASE,
)
_ALLOWED_HOSTS = frozenset({"arxiv.org", "www.arxiv.org", "export.arxiv.org"})
_PDF_MIME = "application/pdf"


@dataclass(frozen=True, slots=True)
class DownloadedPdf:
    content: bytes
    final_url: str
    mime_type: str


def fetch_arxiv_pdf(url: str, max_bytes: int) -> DownloadedPdf:
    headers = {
        "Accept": "application/pdf",
        "User-Agent": "Jarvis-Assistant/0.1 (personal research assistant)",
    }
    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers=headers,
        ) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                _validate_final_url(str(response.url))
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        if int(content_length) > max_bytes:
                            raise ValueError("文献 PDF 超过 Artifact 大小上限")
                    except ValueError as exc:
                        if str(exc) == "文献 PDF 超过 Artifact 大小上限":
                            raise
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("文献 PDF 超过 Artifact 大小上限")
                    chunks.append(chunk)
                mime_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                return DownloadedPdf(b"".join(chunks), str(response.url), mime_type)
    except httpx.HTTPError as exc:
        raise ValueError("arXiv PDF 下载失败") from exc


class ArxivPdfDownloadExecutor:
    def __init__(
        self,
        file_store: LocalArtifactFileStore,
        *,
        fetcher: Callable[[str, int], DownloadedPdf] = fetch_arxiv_pdf,
    ):
        self._file_store = file_store
        self._fetcher = fetcher

    def __call__(self, request: ToolRequest) -> ToolResult:
        raw_id = request.arguments.get("arxiv_id")
        if not isinstance(raw_id, str):
            return _error("TOOL_ARGUMENTS_INVALID", "arxiv_id 必须是字符串", True)
        arxiv_id = _normalize_arxiv_id(raw_id)
        if arxiv_id is None:
            return _error("ARXIV_ID_INVALID", "arxiv_id 格式无效", True)
        try:
            artifact_id = UUID(str(request.execution_context["artifact_id"]))
            run_id = UUID(request.run_id)
            workspace_path = str(request.execution_context["workspace_path"])
        except (KeyError, TypeError, ValueError):
            return _error("ARTIFACT_CONTEXT_MISSING", "下载缺少受控 Artifact 上下文", False)

        source_url = f"https://arxiv.org/pdf/{arxiv_id}"
        try:
            downloaded = self._fetcher(source_url, self._file_store.max_bytes)
            _validate_final_url(downloaded.final_url)
            if downloaded.mime_type != _PDF_MIME or not downloaded.content.startswith(b"%PDF-"):
                return _error("ARXIV_PDF_INVALID", "arXiv 返回内容不是有效 PDF", True)
            stored = self._file_store.write_bytes(
                artifact_id,
                downloaded.content,
                run_id=run_id,
                workspace_path=workspace_path,
                suffix=".pdf",
                mime_type=_PDF_MIME,
            )
        except StorageCapacityExceeded as exc:
            return _error(exc.code, "Artifact 存储容量不足", True)
        except ValueError as exc:
            message = str(exc) if str(exc) else "arXiv PDF 下载失败"
            return _error("ARXIV_DOWNLOAD_FAILED", message, True)
        except OSError:
            return _error("ARTIFACT_WRITE_FAILED", "文献 Artifact 写入失败", True)

        return ToolResult(
            ok=True,
            kind="file",
            summary=f"已下载 arXiv 文献 {arxiv_id}",
            data={
                "downloaded": True,
                "source": "arxiv",
                "arxiv_id": arxiv_id,
                "source_url": downloaded.final_url,
                "path": stored.relative_path,
                "size_bytes": stored.size_bytes,
                "sha256": stored.sha256,
            },
            # 该 id 来自 Runtime 注入的 execution_context，而不是模型参数或
            # 远端响应。后续工具因此能使用与持久化 Artifact 相同的可信 id。
            artifact_ids=[str(artifact_id)],
            deliverables=[
                ToolDeliverable(
                    kind="file",
                    title=f"arXiv {arxiv_id}.pdf",
                    path=stored.relative_path,
                    size_bytes=stored.size_bytes,
                    mime_type=stored.mime_type,
                    content_hash=stored.sha256,
                )
            ],
            metadata={"source": "arxiv", "arxiv_id": arxiv_id},
        )


def _normalize_arxiv_id(value: str) -> str | None:
    normalized = value.strip()
    for prefix in ("https://arxiv.org/abs/", "https://arxiv.org/pdf/"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
    if normalized.lower().endswith(".pdf"):
        normalized = normalized[:-4]
    return normalized if _ARXIV_ID.fullmatch(normalized) else None


def _validate_final_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in _ALLOWED_HOSTS:
        raise ValueError("arXiv 下载重定向到了不受信任的地址")


def _error(code: str, message: str, recoverable: bool) -> ToolResult:
    return ToolResult(
        ok=False,
        summary=message,
        error={
            "code": code,
            "message": message,
            "category": "tool",
            "recoverable": recoverable,
        },
    )
