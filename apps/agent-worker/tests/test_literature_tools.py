from __future__ import annotations

import asyncio
import sys
from dataclasses import asdict
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
import pytest

import jarvis_worker.agent.literature.arxiv as arxiv_provider
from jarvis_worker.agent.artifacts.file_store import LocalArtifactFileStore
from jarvis_worker.agent.core.phases.action_validation import _scheduled_arxiv_source_urls
from jarvis_worker.agent.literature.arxiv import (
    ArxivRateLimitedError,
    ArxivRequestRejectedError,
    ArxivResponseError,
    ArxivTimeoutError,
    ArxivUnavailableError,
)
from jarvis_worker.agent.mcp.client import McpClient
from jarvis_worker.agent.research import trusted_knowledge_provenance
from jarvis_worker.agent.tool_gateway.contracts import ToolRequest
from jarvis_worker.agent.tools.literature.download_arxiv_pdf import (
    ArxivPdfDownloadExecutor,
    DownloadedPdf,
)
from jarvis_worker.agent.tools.literature.search_arxiv import ArxivSearchExecutor
from jarvis_worker.mcp_servers.literature import _parse_feed
from jarvis_worker.runtime.service import RuntimeApplicationService
from jarvis_worker.runtime_bus.messages import RuntimeEventEnvelope
from jarvis_worker.shared.domain.models import McpServer, McpTransport, new_id

_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2401.12345v2</id>
    <updated>2026-07-20T00:00:00Z</updated>
    <published>2026-07-19T00:00:00Z</published>
    <title>  Safe   Agent Systems </title>
    <summary> A bounded\n research result. </summary>
    <author><name>Alice Example</name></author>
    <author><name>Bob Example</name></author>
    <category term="cs.AI"/>
    <arxiv:primary_category term="cs.AI"/>
    <arxiv:doi>10.0000/example</arxiv:doi>
    <link rel="alternate" href="http://arxiv.org/abs/2401.12345v2"/>
    <link title="pdf" href="http://arxiv.org/pdf/2401.12345v2" type="application/pdf"/>
  </entry>
</feed>"""


def test_arxiv_atom_feed_is_normalized_to_bounded_metadata():
    results = _parse_feed(_FEED)
    assert results == [{
        "source": "arxiv",
        "arxiv_id": "2401.12345v2",
        "source_id": "arxiv:2401.12345v2",
        "source_type": "literature",
        "title": "Safe Agent Systems",
        "authors": ["Alice Example", "Bob Example"],
        "abstract": "A bounded research result.",
        "published": "2026-07-19T00:00:00Z",
        "updated": "2026-07-20T00:00:00Z",
        "categories": ["cs.AI"],
        "primary_category": "cs.AI",
        "doi": "10.0000/example",
        "journal_reference": "",
        "abstract_url": "https://arxiv.org/abs/2401.12345v2",
        "pdf_url": "https://arxiv.org/pdf/2401.12345v2",
        "canonical_url": "https://arxiv.org/abs/2401.12345v2",
        "content_scope": "abstract",
        "content_text": "A bounded research result.",
        "content_locators": ["abstract"],
        "content_sha256": "7dd598f3ea8fc5234ad6690276a0b6dc4fc20283df41f0788cbe3b95711933dd",
        "download": {
            "available": True,
            "reference": "2401.12345v2",
            "mime_type": "application/pdf",
            "url": "https://arxiv.org/pdf/2401.12345v2",
        },
    }]


def test_arxiv_provider_retries_429_with_bounded_retry_after(monkeypatch):
    delays = []
    responses = [
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(200, content=b'<feed xmlns="http://www.w3.org/2005/Atom"/>'),
    ]

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _url, params):
            assert params["max_results"] == "1"
            response = responses.pop(0)
            response.request = httpx.Request("GET", arxiv_provider._API_URL)
            return response

    monkeypatch.setattr(arxiv_provider.httpx, "Client", Client)

    result = arxiv_provider.search_arxiv_metadata(
        "agent runtime", max_results=1, delay=delays.append
    )

    assert result["result_count"] == 0
    assert delays == [3, 7]
    assert responses == []


def test_arxiv_provider_reports_bounded_rate_limit_after_retry(monkeypatch):
    delays = []

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _url, params):
            response = httpx.Response(429, headers={"Retry-After": "999"})
            response.request = httpx.Request("GET", arxiv_provider._API_URL)
            return response

    monkeypatch.setattr(arxiv_provider.httpx, "Client", Client)

    with pytest.raises(ArxivRateLimitedError) as exc:
        arxiv_provider.search_arxiv_metadata(
            "agent runtime", max_results=1, delay=delays.append
        )

    assert exc.value.retry_after_seconds == 30
    assert exc.value.attempts == 3
    assert delays == [3, 30, 30]


@pytest.mark.parametrize(
    ("failure", "expected_type", "expected_delays"),
    [
        (
            lambda request: httpx.ReadTimeout("timeout", request=request),
            ArxivTimeoutError,
            [3, 3, 6],
        ),
        (
            lambda request: httpx.ConnectError("unavailable", request=request),
            ArxivUnavailableError,
            [3, 3, 6],
        ),
    ],
)
def test_arxiv_provider_retries_transient_transport_failures(
    monkeypatch, failure, expected_type, expected_delays
):
    delays = []

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _url, params):
            del params
            request = httpx.Request("GET", arxiv_provider._API_URL)
            raise failure(request)

    monkeypatch.setattr(arxiv_provider.httpx, "Client", Client)

    with pytest.raises(expected_type) as exc:
        arxiv_provider.search_arxiv_metadata(
            "agent runtime", max_results=1, delay=delays.append
        )

    assert exc.value.attempts == 3
    assert delays == expected_delays


def test_arxiv_provider_retries_5xx_but_not_non_retryable_4xx(monkeypatch):
    delays = []
    statuses = [503, 503, 503]

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _url, params):
            del params
            response = httpx.Response(statuses.pop(0))
            response.request = httpx.Request("GET", arxiv_provider._API_URL)
            return response

    monkeypatch.setattr(arxiv_provider.httpx, "Client", Client)

    with pytest.raises(ArxivUnavailableError) as exc:
        arxiv_provider.search_arxiv_metadata(
            "agent runtime", max_results=1, delay=delays.append
        )

    assert exc.value.status_code == 503
    assert delays == [3, 3, 6]

    statuses.append(400)
    delays.clear()
    with pytest.raises(ArxivRequestRejectedError) as exc:
        arxiv_provider.search_arxiv_metadata(
            "agent runtime", max_results=1, delay=delays.append
        )
    assert exc.value.status_code == 400
    assert delays == [3]


def test_arxiv_search_executor_exposes_precise_recoverable_rate_limit():
    def searcher(*_args):
        raise ArxivRateLimitedError(12)

    result = ArxivSearchExecutor(_SourceIndex(), _Bridge(), searcher=searcher)(
        ToolRequest(
            task_id=str(uuid4()), run_id=str(uuid4()),
            tool_name="literature.search_arxiv",
            arguments={"query": "agent runtime", "max_results": 2},
        )
    )

    assert result.ok is False
    assert result.error["code"] == "ARXIV_RATE_LIMITED"
    assert result.error["recoverable"] is True
    assert result.error["details"]["retry_after_seconds"] == 12


@pytest.mark.parametrize(
    ("error", "code", "recoverable"),
    [
        (ArxivTimeoutError(3), "ARXIV_SEARCH_TIMEOUT", True),
        (ArxivUnavailableError(3, status_code=503), "ARXIV_SEARCH_UNAVAILABLE", True),
        (ArxivRequestRejectedError(400, attempts=1), "ARXIV_SEARCH_REJECTED", False),
        (ArxivResponseError(attempts=1), "ARXIV_RESPONSE_INVALID", True),
    ],
)
def test_arxiv_search_executor_preserves_provider_failure_class(
    error, code, recoverable
):
    def searcher(*_args):
        raise error

    result = ArxivSearchExecutor(_SourceIndex(), _Bridge(), searcher=searcher)(
        ToolRequest(
            task_id=str(uuid4()),
            run_id=str(uuid4()),
            tool_name="literature.search_arxiv",
            arguments={"query": "agent runtime", "max_results": 2},
        )
    )

    assert result.error["code"] == code
    assert result.error["recoverable"] is recoverable
    assert result.error["details"]["attempts"] == error.attempts


def test_runtime_joins_trusted_source_artifact_and_rag_provenance():
    artifact_id = str(uuid4())
    document_id = str(uuid4())
    job_id = str(uuid4())
    observations = [
        {"tool_name": "literature.search_arxiv", "ok": True, "data": {
            "source": "arxiv", "results": [{
                "arxiv_id": "2401.12345v2",
                "source_id": "arxiv:2401.12345v2",
                "canonical_url": "https://arxiv.org/abs/2401.12345v2",
            }],
        }},
        {"tool_name": "literature.download_arxiv_pdf", "ok": True,
         "artifact_ids": [artifact_id], "data": {
             "arxiv_id": "2401.12345", "sha256": "a" * 64,
         }},
        {"tool_name": "rag.ingest_artifact", "ok": True, "data": {
            "artifact_id": artifact_id, "document_id": document_id,
            "job_id": job_id, "status": "pending",
        }},
    ]

    assert trusted_knowledge_provenance(observations) == [{
        "source_id": "arxiv:2401.12345v2",
        "source_url": "https://arxiv.org/abs/2401.12345v2",
        "artifact_id": artifact_id,
        "artifact_sha256": "a" * 64,
        "rag_document_id": document_id,
        "rag_job_id": job_id,
        "rag_status": "pending",
    }]


def test_runtime_joins_bounded_rag_search_tool_document_and_chunk_provenance():
    tool_call_id = str(uuid4())
    artifact_id = str(uuid4())
    document_id = str(uuid4())
    primary_chunk_id = str(uuid4())
    neighbour_chunk_id = str(uuid4())
    observations = [
        {
            "tool_call_id": tool_call_id,
            "tool_name": "rag.search",
            "ok": True,
            "data": {
                "results": [{
                    "document_id": document_id,
                    "source_artifact_id": artifact_id,
                    "chunks": [
                        {"chunk_id": primary_chunk_id, "role": "primary"},
                        {"chunk_id": neighbour_chunk_id, "role": "next"},
                        {"chunk_id": neighbour_chunk_id, "role": "next"},
                        {"chunk_id": "not-a-uuid", "role": "previous"},
                    ],
                }]
            },
        },
        {
            "tool_call_id": str(uuid4()),
            "tool_name": "rag.search",
            "ok": False,
            "data": {"results": []},
        },
    ]

    assert trusted_knowledge_provenance(observations) == [
        {
            "artifact_id": artifact_id,
            "rag_document_id": document_id,
            "rag_search_tool_call_id": tool_call_id,
            "rag_chunk_id": primary_chunk_id,
        },
        {
            "artifact_id": artifact_id,
            "rag_document_id": document_id,
            "rag_search_tool_call_id": tool_call_id,
            "rag_chunk_id": neighbour_chunk_id,
        },
    ]


def test_runtime_preserves_provenance_from_multiple_rag_documents():
    tool_call_id = str(uuid4())
    document_ids = [str(uuid4()), str(uuid4())]
    artifact_ids = [str(uuid4()), str(uuid4())]
    chunk_ids = [str(uuid4()), str(uuid4())]
    observations = [{
        "tool_call_id": tool_call_id,
        "tool_name": "rag.search",
        "ok": True,
        "data": {
            "results": [
                {
                    "document_id": document_id,
                    "source_artifact_id": artifact_id,
                    "chunks": [{"chunk_id": chunk_id, "role": "primary"}],
                }
                for document_id, artifact_id, chunk_id in zip(
                    document_ids, artifact_ids, chunk_ids, strict=True
                )
            ]
        },
    }]

    links = trusted_knowledge_provenance(observations)

    assert [link["rag_document_id"] for link in links] == document_ids
    assert [link["artifact_id"] for link in links] == artifact_ids
    assert [link["rag_chunk_id"] for link in links] == chunk_ids


def test_rag_search_provenance_prioritises_primary_chunks_and_caps_fifty():
    tool_call_id = str(uuid4())
    artifact_id = str(uuid4())
    document_id = str(uuid4())
    primary_ids = [str(uuid4()) for _ in range(12)]
    results = []
    for primary_id in primary_ids:
        results.append({
            "document_id": document_id,
            "source_artifact_id": artifact_id,
            "chunks": [
                {"chunk_id": primary_id, "role": "primary"},
                *[
                    {"chunk_id": str(uuid4()), "role": "next"}
                    for _ in range(4)
                ],
            ],
        })

    links = trusted_knowledge_provenance([{
        "tool_call_id": tool_call_id,
        "tool_name": "rag.search",
        "ok": True,
        "data": {"results": results},
    }])

    assert len(links) == 50
    assert [item["rag_chunk_id"] for item in links[:12]] == primary_ids
    assert len({item["rag_chunk_id"] for item in links}) == 50


def test_literature_mcp_server_can_be_discovered_without_network_call():
    server = McpServer(
        id=new_id(),
        slug="literature",
        name="Jarvis Literature",
        transport=McpTransport.STDIO,
        command=sys.executable,
        args=["-m", "jarvis_worker.mcp_servers.literature"],
    )
    tools = asyncio.run(McpClient(timeout_seconds=10).discover(server))
    assert [tool.name for tool in tools] == ["search_arxiv"]
    assert tools[0].input_schema["required"] == ["query"]


def test_download_executor_writes_verified_pdf_to_artifact_store(tmp_path):
    artifact_id = uuid4()
    store = LocalArtifactFileStore(tmp_path, max_bytes=1024)

    def fetcher(url: str, max_bytes: int) -> DownloadedPdf:
        assert url == "https://arxiv.org/pdf/2401.12345v2"
        assert max_bytes == 1024
        return DownloadedPdf(
            b"%PDF-1.7\nverified", url, "application/pdf"
        )

    result = ArxivPdfDownloadExecutor(store, fetcher=fetcher)(ToolRequest(
        task_id=str(uuid4()),
        run_id=str(uuid4()),
        tool_name="literature.download_arxiv_pdf",
        arguments={"arxiv_id": "https://arxiv.org/abs/2401.12345v2"},
        execution_context={
            "artifact_id": str(artifact_id),
            "workspace_path": str(tmp_path),
        },
    ))

    assert result.ok is True
    assert result.kind == "file"
    assert result.artifact_ids == [str(artifact_id)]
    assert result.data["arxiv_id"] == "2401.12345v2"
    assert store.read_bytes(
        result.data["path"], expected_sha256=result.data["sha256"]
    ).startswith(b"%PDF-")
    assert result.deliverables[0].path.endswith(f"{artifact_id}.pdf")


def test_download_executor_returns_stable_error_when_artifact_quota_is_full(
    tmp_path,
):
    store = LocalArtifactFileStore(
        tmp_path,
        max_bytes=16,
        max_run_bytes=16,
        max_workspace_bytes=16,
        max_total_bytes=16,
    )
    store.write_bytes(
        uuid4(),
        b"%PDF-1.7\n123456",
        run_id=uuid4(),
        workspace_id=uuid4(),
        suffix=".pdf",
        mime_type="application/pdf",
    )
    result = ArxivPdfDownloadExecutor(
        store,
        fetcher=lambda url, _limit: DownloadedPdf(
            b"%PDF-1.7\nx", url, "application/pdf"
        ),
    )(ToolRequest(
        task_id=str(uuid4()),
        run_id=str(uuid4()),
        tool_name="literature.download_arxiv_pdf",
        arguments={"arxiv_id": "2401.12345"},
        execution_context={
            "artifact_id": str(uuid4()),
            "workspace_path": str(tmp_path),
        },
    ))

    assert result.ok is False
    assert result.error["code"] == "ARTIFACT_TOTAL_CAPACITY_EXCEEDED"


@pytest.mark.parametrize(
    ("download", "code"),
    [
        (DownloadedPdf(b"<html>blocked</html>", "https://arxiv.org/pdf/2401.12345", "text/html"), "ARXIV_PDF_INVALID"),
        (DownloadedPdf(b"%PDF-1.7", "https://evil.example/paper.pdf", "application/pdf"), "ARXIV_DOWNLOAD_FAILED"),
    ],
)
def test_download_executor_rejects_invalid_content_or_redirect(tmp_path, download, code):
    executor = ArxivPdfDownloadExecutor(
        LocalArtifactFileStore(tmp_path), fetcher=lambda _url, _limit: download
    )
    result = executor(ToolRequest(
        task_id=str(uuid4()), run_id=str(uuid4()),
        tool_name="literature.download_arxiv_pdf",
        arguments={"arxiv_id": "2401.12345"},
        execution_context={
            "artifact_id": str(uuid4()),
            "workspace_path": str(tmp_path),
        },
    ))
    assert result.ok is False
    assert result.error["code"] == code


def test_literature_deliverable_uses_runtime_owned_artifact_id(tmp_path):
    run_id, task_id, trace_id, step_id, call_id = (
        uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
    )
    artifact_id = uuid5(
        NAMESPACE_URL,
        f"jarvis:artifact:{run_id}:{call_id}:0:literature-pdf",
    )
    store = LocalArtifactFileStore(tmp_path)
    result = ArxivPdfDownloadExecutor(
        store,
        fetcher=lambda url, _limit: DownloadedPdf(
            b"%PDF-1.7\ncontent", url, "application/pdf"
        ),
    )(ToolRequest(
        task_id=str(task_id), run_id=str(run_id),
        tool_name="literature.download_arxiv_pdf",
        arguments={"arxiv_id": "2401.12345"},
        execution_context={
            "artifact_id": str(artifact_id),
            "workspace_path": str(tmp_path),
        },
    ))
    event_id = uuid4()
    envelope = RuntimeEventEnvelope(
        event_id=str(event_id), trace_id=str(trace_id), task_id=str(task_id),
        run_id=str(run_id), event_type="tool.call.finished", produced_by="worker-test",
        runtime_event={
            "id": str(event_id), "type": "tool.call.finished",
            "task_id": str(task_id), "run_id": str(run_id), "step_id": str(step_id),
            "timestamp": "2026-07-26T00:00:00+00:00",
            "payload": {"tool_call": {
                "id": str(call_id), "tool_name": "literature.download_arxiv_pdf",
                "status": "completed", "result": {
                    "kind": result.kind, "summary": result.summary, "data": result.data,
                    "deliverables": [asdict(item) for item in result.deliverables],
                },
            }},
        },
    )

    public, prepared = RuntimeApplicationService._prepare_tool_deliverables(envelope)
    assert prepared[0]["id"] == artifact_id
    assert prepared[0]["storage"] == "local_file"
    assert prepared[0]["path"] == result.data["path"]
    assert prepared[0]["path"].endswith(f"/{str(artifact_id)[:2]}/{artifact_id}.pdf")
    assert public.runtime_event["payload"]["tool_call"]["result"]["artifact_ids"] == [str(artifact_id)]


class _Bridge:
    def run(self, awaitable, timeout):
        return asyncio.run(awaitable)


class _SourceIndex:
    async def known_urls(self, schedule_id):
        return {"https://arxiv.org/abs/2401.00001"}


def test_scheduled_arxiv_search_enforces_scope_and_filters_committed_sources():
    def searcher(query, max_results, sort_by):
        assert (query, max_results, sort_by) == ("AI agents", 10, "submittedDate")
        return {"source": "arxiv", "query": query, "results": [
            {"arxiv_id": "2401.00001", "abstract_url": "https://arxiv.org/abs/2401.00001"},
            {"arxiv_id": "2401.00002", "abstract_url": "https://arxiv.org/abs/2401.00002"},
        ], "attribution": "arXiv"}

    schedule_id = uuid4()
    executor = ArxivSearchExecutor(_SourceIndex(), _Bridge(), searcher=searcher)
    result = executor(ToolRequest(
        task_id=str(uuid4()), run_id=str(uuid4()), tool_name="literature.search_arxiv",
        arguments={"query": "AI agents", "max_results": 1},
        authorization_scope={
            "type": "scheduled_task", "scheduled_task_id": str(schedule_id),
            "authorized_tools": ["literature.search_arxiv", "knowledge.create_document"],
            "source_policy": {"provider": "arxiv", "query": "AI agents", "max_results": 1},
        },
    ))
    assert result.ok is True
    assert [item["arxiv_id"] for item in result.data["results"]] == ["2401.00002"]


def test_scheduled_arxiv_search_rejects_model_argument_expansion():
    executor = ArxivSearchExecutor(_SourceIndex(), _Bridge(), searcher=lambda *_: {})
    result = executor(ToolRequest(
        task_id=str(uuid4()), run_id=str(uuid4()), tool_name="literature.search_arxiv",
        arguments={"query": "different topic", "max_results": 5},
        authorization_scope={
            "type": "scheduled_task", "scheduled_task_id": str(uuid4()),
            "authorized_tools": ["literature.search_arxiv"],
            "source_policy": {"provider": "arxiv", "query": "AI agents", "max_results": 5},
        },
    ))
    assert result.ok is False
    assert result.error["code"] == "SCHEDULE_SOURCE_SCOPE_VIOLATION"


def test_scheduled_document_sources_are_projected_from_trusted_search_result():
    assert _scheduled_arxiv_source_urls([
        {"tool_name": "literature.search_arxiv", "ok": True, "data": {
            "source": "arxiv", "results": [
                {"abstract_url": "https://arxiv.org/abs/2605.26252v1"},
                {"abstract_url": "https://evil.example/paper"},
            ],
        }},
        {"tool_name": "other", "ok": True, "data": {"results": [
            {"abstract_url": "https://arxiv.org/abs/should-not-appear"},
        ]}},
    ]) == ["https://arxiv.org/abs/2605.26252v1"]
