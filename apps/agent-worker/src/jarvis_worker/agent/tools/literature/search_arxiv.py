"""Bounded first-party arXiv search for unattended schedules."""

from __future__ import annotations

from typing import Callable
from uuid import UUID

from jarvis_worker.agent.literature.arxiv import (
    ArxivRateLimitedError,
    ArxivRequestRejectedError,
    ArxivResponseError,
    ArxivTimeoutError,
    ArxivUnavailableError,
    normalize_query,
    search_arxiv_metadata,
)
from jarvis_worker.agent.tool_gateway.contracts import ToolRequest, ToolResult


class ArxivSearchExecutor:
    def __init__(self, source_index, async_bridge, *, searcher: Callable = search_arxiv_metadata):
        self._source_index = source_index
        self._bridge = async_bridge
        self._searcher = searcher

    def __call__(self, request: ToolRequest) -> ToolResult:
        try:
            query = normalize_query(request.arguments.get("query", ""))
            max_results = request.arguments.get("max_results", 5)
            if not isinstance(max_results, int) or isinstance(max_results, bool) or not 1 <= max_results <= 10:
                raise ValueError("max_results 必须在 1 到 10 之间")
            scope = request.authorization_scope or {}
            known_urls: set[str] = set()
            if scope.get("type") == "scheduled_task":
                policy = scope.get("source_policy") or {}
                if (
                    policy.get("provider") != "arxiv"
                    or query != policy.get("query")
                    or max_results != policy.get("max_results")
                    or request.tool_name not in scope.get("authorized_tools", [])
                ):
                    return _error("SCHEDULE_SOURCE_SCOPE_VIOLATION", "检索参数超出定期任务授权范围", False)
                schedule_id = UUID(str(scope["scheduled_task_id"]))
                known_urls = self._bridge.run(self._source_index.known_urls(schedule_id), timeout=10)

            # Fetch the provider maximum, then apply the plan's smaller result bound after
            # filtering sources already committed to this schedule's reports.
            payload = self._searcher(query, 10, "submittedDate")
            fresh = [item for item in payload["results"] if item.get("abstract_url") not in known_urls][:max_results]
            payload["results"] = fresh
            payload["result_count"] = len(fresh)
            payload["known_source_count"] = len(known_urls)
            return ToolResult(
                ok=True, kind="json",
                summary=f"arXiv 检索完成：{len(fresh)} 条未收录结果",
                data=payload, metadata={"source": "arxiv", "deduplicated": True},
            )
        except ArxivRateLimitedError as exc:
            return _error(
                "ARXIV_RATE_LIMITED",
                "arXiv 请求频率受限，请稍后重试",
                True,
                retry_after_seconds=exc.retry_after_seconds,
                attempts=exc.attempts,
            )
        except ArxivTimeoutError as exc:
            return _error(
                "ARXIV_SEARCH_TIMEOUT",
                "arXiv 请求超时，已完成有限次数重试",
                True,
                attempts=exc.attempts,
            )
        except ArxivUnavailableError as exc:
            details = {"attempts": exc.attempts}
            if exc.status_code is not None:
                details["status_code"] = exc.status_code
            return _error(
                "ARXIV_SEARCH_UNAVAILABLE",
                "arXiv 服务暂时不可用，已完成有限次数重试",
                True,
                **details,
            )
        except ArxivRequestRejectedError as exc:
            return _error(
                "ARXIV_SEARCH_REJECTED",
                "arXiv 拒绝了检索请求",
                False,
                attempts=exc.attempts,
                status_code=exc.status_code,
            )
        except ArxivResponseError as exc:
            return _error(
                "ARXIV_RESPONSE_INVALID",
                "arXiv 返回了无法安全解析的元数据",
                True,
                attempts=exc.attempts,
            )
        except (KeyError, TypeError, ValueError) as exc:
            return _error("ARXIV_SEARCH_INVALID", str(exc) or "arXiv 检索参数无效", True)
        except Exception:
            return _error("ARXIV_SEARCH_FAILED", "arXiv 元数据检索失败", True)


def _error(
    code: str,
    message: str,
    recoverable: bool,
    **details,
) -> ToolResult:
    error = {
        "code": code,
        "message": message,
        "category": "tool",
        "recoverable": recoverable,
    }
    if details:
        error["details"] = details
    return ToolResult(ok=False, summary=message, error=error)
