from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from jarvis_worker.runtime.schedules.service import _execution_goal, _next_run
from jarvis_worker.shared.domain.models import ScheduledTask, new_id
from jarvis_worker.shared.domain.models import ScheduleRecurrence


def test_next_daily_run_respects_schedule_timezone():
    after = datetime(2026, 7, 26, 1, 30, tzinfo=timezone.utc)  # 上海 09:30
    result = _next_run(
        ScheduleRecurrence.DAILY, ZoneInfo("Asia/Shanghai"), 9, 0, None, after,
    )
    assert result == datetime(2026, 7, 27, 1, 0, tzinfo=timezone.utc)


def test_next_weekly_run_uses_iso_monday_zero():
    after = datetime(2026, 7, 26, 1, 0, tzinfo=timezone.utc)  # 周日
    result = _next_run(
        ScheduleRecurrence.WEEKLY, ZoneInfo("Asia/Shanghai"), 10, 0, 0, after,
    )
    assert result == datetime(2026, 7, 27, 2, 0, tzinfo=timezone.utc)


def test_elapsed_weekly_slot_moves_to_next_week():
    after = datetime(2026, 7, 27, 3, 0, tzinfo=timezone.utc)  # 周一 11:00 上海
    result = _next_run(
        ScheduleRecurrence.WEEKLY, ZoneInfo("Asia/Shanghai"), 10, 0, 0, after,
    )
    assert result == datetime(2026, 8, 3, 2, 0, tzinfo=timezone.utc)


def test_source_report_goal_uses_server_owned_exact_policy():
    item = ScheduledTask(
        id=new_id(), name="weekly", user_goal="总结安全性进展",
        recurrence=ScheduleRecurrence.WEEKLY, timezone="Asia/Shanghai",
        hour=9, minute=0, weekday=0,
        next_run_at=datetime(2026, 8, 3, 1, 0, tzinfo=timezone.utc),
        task_kind="source_report",
        source_policy={"provider": "arxiv", "query": "AI agent safety", "max_results": 4},
        authorized_tools=["literature.search_arxiv", "knowledge.create_document"],
    )
    goal = _execution_goal(item)
    assert "literature.search_arxiv" in goal
    assert "AI agent safety" in goal
    assert "max_results=4" in goal
    assert "source_urls" in goal
