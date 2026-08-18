from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from jarvis_worker.runtime.tool_calls.postgres_repository import (
    PostgresToolCallRepository,
)
from jarvis_worker.shared.domain.models import ToolCall


@pytest.mark.asyncio
async def test_tool_call_update_persists_permission_request_relation():
    session = AsyncMock()
    permission_request_id = uuid4()
    tool_call = ToolCall(
        id=uuid4(), task_id=uuid4(), run_id=uuid4(), step_id=uuid4(),
        provider="native", tool_name="literature.download_arxiv_pdf",
        risk_level="L2", arguments={},
        permission_request_id=permission_request_id,
        permission_status="approved", status="running",
    )

    await PostgresToolCallRepository(session).update(tool_call)

    statement = session.execute.await_args.args[0]
    params = statement.compile().params
    assert params["permission_request_id"] == permission_request_id
