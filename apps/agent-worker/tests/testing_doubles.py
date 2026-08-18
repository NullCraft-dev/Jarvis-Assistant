"""仅供 pytest 使用的确定性 Runtime 测试替身。

这些类型不属于 jarvis_worker 生产包，不能通过 Worker 配置或 Web/Gateway 入口启用。
"""
from __future__ import annotations

"""Intent detection — 共享的 user_goal 意图检测工具。

Phase 6A 修复：集中 read_file 意图的关键词和文件名检测逻辑，
消除 MockRunner 和 MockModelProvider 两套关键词漂移的风险。

MockRunner 使用 detect_read_file_path 判断是否委托 AgentRunner；
MockModelProvider 使用同一函数判断是否生成 workspace.read_file action。
"""

import re

# read_file 意图触发关键词（需同时检测到文件名才生效）
READ_FILE_KEYWORDS = [
    "读取",
    "read",
    "查看",
    "view",
    "总结",
    "summarize",
    "显示内容",
    "查看内容",
    "阅读",
    "打开",
]

# 文件名检测模式：匹配常见文件名/路径
# (含 .md, .py, .txt, .json, .yml, .yaml, .toml, .cfg 等)
_FILE_DETECT_PATTERN = re.compile(
    r"([\w.\-/]+\.(?:md|txt|py|json|yml|yaml|toml|cfg|ini|xml|html|css|js|ts|"
    r"vue|go|rs|java|sh|bash|zsh|env|gitignore|dockerfile|makefile|readme|license))",
    re.IGNORECASE,
)


def detect_read_file_path(goal: str) -> str | None:
    """检测用户输入中是否包含 read_file 意图和可识别文件名。

    同时使用 READ_FILE_KEYWORDS 和 _FILE_DETECT_PATTERN：
    1. 检查是否包含 read_file 关键词
    2. 从原始文本中提取文件名

    Args:
        goal: 用户输入原始文本（不 lower，用于提取文件名大小写）

    Returns:
        检测到的文件路径，或 None（没有 read_file 意图或无文件名）。
    """
    goal_lower = goal.lower()

    # 1. 检查是否包含 read_file 关键词
    has_read_intent = any(kw in goal_lower for kw in READ_FILE_KEYWORDS)
    if not has_read_intent:
        return None

    # 2. 提取文件名
    match = _FILE_DETECT_PATTERN.search(goal)
    if not match:
        return None

    return match.group(1)

"""MockModelProvider — 基于规则的确定性模型模拟器。

用户目标包含 workspace/list files 关键词时，返回 call_tool workspace.list_files。
包含 read/读取/查看...内容/总结 关键词且含文件名时，返回 call_tool workspace.read_file。
其他目标返回 finish，携带 mock 成功消息。

本轮不做真实 LLM 调用。
MockModelProvider 不做 workspace_root 安全判断——该职责属于 ToolGateway fail closed。
"""

import logging

from jarvis_worker.agent.core.actions import AgentAction
from jarvis_worker.agent.core.state import AgentState
from jarvis_worker.agent.models.provider import ModelProvider

log = logging.getLogger("jarvis_worker.mock_model")

# list_files 触发关键词
_LIST_KEYWORDS = [
    "list files",
    "list file",
    "列出文件",
    "列出目录",
    "查看目录",
    "查看文件",
    "查看文件列表",
    "workspace",
    "工作区",
]

# 默认完成消息
_DEFAULT_FINISH_MESSAGE = (
    "任务已完成。根据您的要求，我执行了以下步骤：\n"
    "1. 分析输入目标\n"
    "2. 确定不需要调用工具\n"
    "3. 直接返回结果\n\n"
    "这是一个 mock 执行结果，由 Jarvis Python Agent Worker 生成。"
)


class MockModelProvider(ModelProvider):
    """基于规则的确定性模型模拟器。

    决策逻辑：
    1. 第一轮（iteration == 0）：
       - 目标包含 read_file 关键词 + 可识别文件名 → call_tool workspace.read_file
         （workspace_root 来自 state，即使是空字符串也传入；fail closed 由 ToolGateway 保证）
       - 目标包含 list_files 关键词 → call_tool workspace.list_files
       - 其他 → finish
    2. 后续轮次（iteration > 0）：
       - 上一轮观测成功 → finish（任务完成）
       - 工具失败已在 AgentRunner 层 terminate，不应到达此处

    不负责：
    - workspace_root 安全判断（由 ToolGateway fail closed）
    - 真实 LLM 调用
    - 工具执行
    - 复杂推理/规划
    """

    @property
    def provider_name(self) -> str:
        return "mock"

    @property
    def model_name(self) -> str:
        return "mock"

    def decide_next_action(self, state: AgentState) -> AgentAction:
        """根据当前 state 决定下一个动作。"""
        goal_lower = state.user_goal.lower()

        # 第一轮：检查是否需要调用工具
        if state.iteration == 0:
            # Phase 6A: 先检查 read_file 意图（优先级高于 list_files）
            file_path = detect_read_file_path(state.user_goal)
            if file_path:
                log.info(
                    "MockModel: task_id=%s goal=%r → call_tool workspace.read_file "
                    "(path=%r)",
                    state.task_id,
                    state.user_goal[:80],
                    file_path,
                )
                # workspace_root 由 AgentRunner 从 state.workspace_root 注入，
                # MockModelProvider 只提供模型可控参数
                return AgentAction.call_tool(
                    tool_name="workspace.read_file",
                    arguments={
                        "path": file_path,
                    },
                    reason=f"用户要求读取文件: {file_path}",
                )

            if self._should_list_files(goal_lower):
                log.info(
                    "MockModel: task_id=%s goal=%r → call_tool workspace.list_files",
                    state.task_id,
                    state.user_goal[:80],
                )
                # workspace_root 由 AgentRunner 从 state.workspace_root 注入，
                # MockModelProvider 只提供模型可控参数
                return AgentAction.call_tool(
                    tool_name="workspace.list_files",
                    arguments={
                        "path": ".",
                    },
                    reason=f"用户要求查看 workspace 文件: {state.user_goal[:80]}",
                )

            # 普通目标 → 直接完成
            log.info(
                "MockModel: task_id=%s goal=%r → finish（简单任务）",
                state.task_id,
                state.user_goal[:80],
            )
            return AgentAction.finish(final_message=_DEFAULT_FINISH_MESSAGE)

        # 后续轮次：根据上一轮观测结果决定
        last_obs = state.observations[-1] if state.observations else {}
        if last_obs.get("ok"):
            log.info(
                "MockModel: task_id=%s iter=%d → finish（上轮工具成功）",
                state.task_id,
                state.iteration,
            )
            summary = last_obs.get("summary", "")
            # 根据最近使用的工具名构造合适的完成消息
            tool_data = last_obs.get("data", {})
            if isinstance(tool_data, dict) and "content" in tool_data:
                # read_file 结果
                path = tool_data.get("path", "unknown")
                chars = tool_data.get("chars_read", 0)
                return AgentAction.finish(
                    final_message=f"已成功读取文件 {path}（{chars} 字符）。"
                )
            return AgentAction.finish(
                final_message=f"工具执行成功。{summary}"
            )
        # 防御性 fallback（工具失败已在 AgentRunner 层 terminate，不应到达此处）
        log.warning(
            "MockModel: task_id=%s iter=%d → finish（fallback）",
            state.task_id,
            state.iteration,
        )
        return AgentAction.finish(
            final_message=f"Agent 完成处理（迭代 {state.iteration}）。"
        )

    @staticmethod
    def _should_list_files(goal: str) -> bool:
        """判断用户目标是否应触发 list_files。"""
        return any(kw in goal for kw in _LIST_KEYWORDS)

"""Deterministic mock runner — 生成固定顺序的 RuntimeEvent 序列。

本切片不做真实 LLM / LangGraph。
ToolGateway MVP 第一刀：支持 tool scenario（workspace.list_files）。
Mock runner 模拟一个简单 agent 执行过程，输出可预测的事件序列。

事件顺序（对齐 simple_success 场景）：
  1. agent.run.started
  2. agent.step.started
  3. model.delta（模拟 streaming 文本）
  4. agent.step.completed
  5. agent.run.completed

Tool scenario 事件顺序：
  1. agent.run.started
  2. tool.call.started
  3. tool.call.finished（或 tool.call.failed）
  4. agent.run.completed（或 agent.run.failed）

3C: 支持 run_with_cancel_check，在步骤间检查 cancel flag，
收到 cancel 后停止后续事件并发出 agent.run.cancelled。

事件 id 使用 deterministic_event_id(run_id, event_type, seq) 生成。
同一 run 重试时产生相同 event id，Gateway/SSE 可基于 event.id 去重，
避免 publish 部分成功后重试产生重复事件。
"""

import logging
import time
from typing import Any, Callable

from jarvis_worker.agent.core.runner import AgentRunner
from jarvis_worker.runtime_bus.messages import RunJobMessage, RuntimeEventEnvelope
from jarvis_worker.runtime.events import (
    build_envelope,
    build_runtime_event,
    deterministic_event_id,
    deterministic_step_id,
)
from jarvis_worker.agent.tool_gateway.gateway import ToolGateway
from jarvis_worker.agent.tool_gateway.contracts import ToolRequest, ToolResult

log = logging.getLogger("jarvis_worker.mock_runner")

# Mock 输出文本
MOCK_OUTPUT_TEXT = (
    "任务已完成。根据您的要求，我执行了以下步骤：\n"
    "1. 分析输入目标\n"
    "2. 规划执行路径\n"
    "3. 完成处理\n\n"
    "这是一个 mock 执行结果，由 Jarvis Python Agent Worker 生成。"
)

# 事件序列定义：(event_type, seq, needs_step_id)
_EVENT_SEQUENCE = [
    ("agent.run.started", 1, False),
    ("agent.step.started", 2, True),
    ("model.delta", 3, True),
    ("agent.step.completed", 4, True),
    ("agent.run.completed", 5, False),
]

# Tool scenario 触发关键词（user_goal 包含任一即走 tool scenario）
_TOOL_SCENARIO_KEYWORDS = [
    "list files",
    "list file",
    "列出文件",
    "列出目录",
    "查看目录",
    "查看文件",
    "workspace",
    "工作区",
]



class MockRunner:
    """Deterministic mock runner — 模拟单次 agent run。

    用法：
        runner = MockRunner(worker_id="worker-01")
        envelopes = runner.run(run_job)

    3C: 支持 run_with_cancel_check，在步骤间检查 cancel flag。

    运行模式：
    - dev_mock_scenarios_enabled=True（mock/dev 模式）：
      保留 permission/tool/simple_success DEV scenario 行为。
    - dev_mock_scenarios_enabled=False（真实模式）：
      所有正常 RunJobMessage 进入 AgentRunner，不根据 user_goal 关键词决定路由。
      permission/权限等自然语言不会触发 DEV mock scenario。

    幂等性：
        同一 job 多次 run() 产生相同 event id（基于 run_id + event_type + seq），
        确保 publish 部分成功后重试不会产生重复事件。
    """

    def __init__(
        self,
        worker_id: str,
        step_delay_ms: int = 0,
        tool_gateway: ToolGateway | None = None,
        default_workspace_root: str = "",
        agent_runner: AgentRunner | None = None,
        dev_mock_scenarios_enabled: bool = True,
    ):
        self._worker_id = worker_id
        self._step_delay_ms = max(step_delay_ms, 0)
        self._tool_gateway = tool_gateway
        self._default_workspace_root = default_workspace_root
        self._agent_runner = agent_runner
        self._dev_mock_scenarios_enabled = dev_mock_scenarios_enabled

    @staticmethod
    def _is_permission_scenario(job: RunJobMessage) -> bool:
        """通过 user_goal 判断是否走 permission 场景。"""
        goal = job.user_goal.lower()
        return "permission" in goal or "权限" in goal

    @staticmethod
    def _is_tool_scenario(job: RunJobMessage) -> bool:
        """通过 user_goal 判断是否走 tool scenario（list_files / read_file）。

        Phase 6A: 新增 read_file 意图检测。
        """
        goal = job.user_goal.lower()
        # 检查 list_files 关键词
        if any(kw in goal for kw in _TOOL_SCENARIO_KEYWORDS):
            return True
        # 检查 read_file 意图
        return MockRunner._is_read_file_intent(job)

    @staticmethod
    def _is_read_file_intent(job: RunJobMessage) -> bool:
        """检查 user_goal 是否包含 read_file 意图 + 可识别文件名。

        复用 intent_detection.detect_read_file_path，保证与 MockModelProvider
        使用同一份关键词和文件名检测逻辑，防止漂移。
        """
        return detect_read_file_path(job.user_goal) is not None

    @property
    def worker_id(self) -> str:
        return self._worker_id

    def run(self, job: RunJobMessage) -> list[RuntimeEventEnvelope]:
        """执行一次 deterministic mock run（无 cancel 检查）。"""
        return self._do_run(job, cancel_check=None)

    def run_with_cancel_check(
        self,
        job: RunJobMessage,
        cancel_check: Callable[[], bool] | None = None,
        pause_check: Callable[[], str | None] | None = None,
        wait_decision: Callable[[str], str | None] | None = None,
        publish_cb: Callable[[RuntimeEventEnvelope], None] | None = None,
        prepare_wait: Callable[[str], None] | None = None,
        history_messages: list[dict[str, str]] | None = None,
    ) -> list[RuntimeEventEnvelope]:
        """执行 mock run，在步骤间检查 cancel flag。

        Args:
            job: RunJobMessage
            cancel_check: 返回 True 表示已收到 cancel
            wait_decision: permission scenario 时调用，传入 request_id，返回 decision 或 None
            publish_cb: 每生成一个事件时立即调用（用于 permission.required 在等待前发布）
            prepare_wait: 在 publish_cb 前登记 pending request_id（防止 decision 早到竞态）

        Returns:
            事件列表。
        """
        return self._do_run(job, cancel_check=cancel_check, wait_decision=wait_decision,
                            publish_cb=publish_cb, prepare_wait=prepare_wait)

    def _do_run(
        self,
        job: RunJobMessage,
        cancel_check: Callable[[], bool] | None = None,
        wait_decision: Callable[[str], str | None] | None = None,
        publish_cb: Callable[[RuntimeEventEnvelope], None] | None = None,
        prepare_wait: Callable[[str], None] | None = None,
    ) -> list[RuntimeEventEnvelope]:
        """内部实现：执行 mock run 事件序列。

        路由策略：
        - agent_runner 存在 且 dev_mock_scenarios 关闭（真实模式）：
          所有任务进入 AgentRunner，不根据 user_goal 关键词决定路由。
        - agent_runner 存在 且 dev_mock_scenarios 开启（mock/dev 模式）：
          保留 legacy DEV scenario 行为。
        - agent_runner 不存在：
          回退到 legacy mock 行为。
        """
        # 真实模式：所有任务进入 AgentRunner
        if self._agent_runner is not None and not self._dev_mock_scenarios_enabled:
            return self._agent_runner.run(
                job,
                default_workspace_root=self._default_workspace_root,
                cancel_check=cancel_check,
            )

        # DEV mock 模式：保留原有行为
        # Permission MVP: user_goal 包含 "permission" / "权限" → permission scenario
        if self._is_permission_scenario(job) and wait_decision is not None:
            return self._do_permission_run(job, cancel_check, wait_decision, publish_cb, prepare_wait)

        # Tool scenario: user_goal 包含 tool 关键词 → AgentRunner 或 legacy
        if self._is_tool_scenario(job):
            if self._agent_runner is not None:
                return self._agent_runner.run(
                    job,
                    default_workspace_root=self._default_workspace_root,
                    cancel_check=cancel_check,
                )
            elif self._tool_gateway is not None:
                if self._is_read_file_intent(job):
                    log.warning(
                        "read_file intent 但无 AgentRunner，fallback simple_success: goal=%r",
                        job.user_goal[:80],
                    )
                else:
                    return self._do_tool_run(job, cancel_check)

        task_id = job.task_id
        run_id = job.run_id
        trace_id = job.trace_id
        step_id = deterministic_step_id(run_id, seq=1)

        envelopes: list[RuntimeEventEnvelope] = []

        for event_type, seq, needs_step in _EVENT_SEQUENCE:
            # 步骤间 delay（默认 0，不影响测试速度；dev/manual 可设置以制造 cancel 窗口）
            if self._step_delay_ms > 0:
                time.sleep(self._step_delay_ms / 1000.0)

            # 3C: 步骤间检查 cancel flag
            if cancel_check and cancel_check():
                log.info(
                    "mock runner 收到 cancel: run_id=%s event=%s seq=%d",
                    run_id,
                    event_type,
                    seq,
                )
                # 发出 agent.run.cancelled terminal event
                cancel_eid = deterministic_event_id(
                    run_id, "agent.run.cancelled", 99
                )
                cancel_event = build_runtime_event(
                    event_type="agent.run.cancelled",
                    task_id=task_id,
                    run_id=run_id,
                    event_id=cancel_eid,
                    payload={
                        "run_id": run_id,
                        "reason": "cancelled_by_user",
                    },
                )
                envelopes.append(
                    self._make_event(trace_id, cancel_event)
                )
                return envelopes

            eid = deterministic_event_id(run_id, event_type, seq)

            kwargs: dict[str, Any] = {
                "event_type": event_type,
                "task_id": task_id,
                "run_id": run_id,
                "event_id": eid,
            }
            if needs_step:
                kwargs["step_id"] = step_id

            # 按事件类型构造 payload
            payload: dict[str, Any] = {}
            if event_type == "agent.run.started":
                payload = {"agent_id": "agent-default", "mode": "single_agent"}
            elif event_type == "agent.step.started":
                payload = {
                    "step": {"id": step_id, "type": "mock_execution", "status": "started"}
                }
            elif event_type == "model.delta":
                payload = {"delta": MOCK_OUTPUT_TEXT, "accumulated": MOCK_OUTPUT_TEXT}
            elif event_type == "agent.step.completed":
                payload = {
                    "step": {
                        "id": step_id,
                        "type": "mock_execution",
                        "status": "completed",
                        "output": MOCK_OUTPUT_TEXT,
                    }
                }
            elif event_type == "agent.run.completed":
                payload = {"output": MOCK_OUTPUT_TEXT, "total_steps": 1}

            kwargs["payload"] = payload
            event = build_runtime_event(**kwargs)
            envelopes.append(self._make_event(trace_id, event))

        return envelopes

    def _make_event(
        self, trace_id: str, event: dict[str, Any]
    ) -> RuntimeEventEnvelope:
        """构造并校验 envelope。"""
        env = build_envelope(event, trace_id, self._worker_id)
        env.validate()
        return env

    # -- Permission MVP scenario --

    def _do_permission_run(
        self,
        job: RunJobMessage,
        cancel_check: Callable[[], bool] | None = None,
        wait_decision: Callable[[str], str | None] | None = None,
        publish_cb: Callable[[RuntimeEventEnvelope], None] | None = None,
        prepare_wait: Callable[[str], None] | None = None,
    ) -> list[RuntimeEventEnvelope]:
        """Permission required 场景（deterministic mock）。

        事件序列：
          1. agent.run.started
          2. tool.call.started (shell, L3)
          3. permission.required
          4. [等待 decision]
          5a. approve → permission.resolved → tool.call.finished → agent.run.completed
          5b. deny → permission.resolved → tool.call.failed → agent.run.failed
          5c. timeout → agent.run.failed (PERMISSION_TIMEOUT)
        """
        task_id = job.task_id
        run_id = job.run_id
        trace_id = job.trace_id
        step_id = deterministic_step_id(run_id, seq=1)
        tool_call_id = deterministic_event_id(run_id, "tool.call", 20)
        perm_req_id = deterministic_event_id(run_id, "perm_req", 21)

        envelopes: list[RuntimeEventEnvelope] = []

        # 1. agent.run.started
        envelopes.append(self._make_event(trace_id, build_runtime_event(
            event_type="agent.run.started", task_id=task_id, run_id=run_id,
            event_id=deterministic_event_id(run_id, "agent.run.started", 1),
            payload={"agent_id": "agent-default", "mode": "single_agent"},
        )))

        # 2. tool.call.started
        envelopes.append(self._make_event(trace_id, build_runtime_event(
            event_type="tool.call.started", task_id=task_id, run_id=run_id,
            step_id=step_id,
            event_id=deterministic_event_id(run_id, "tool.call.started", 2),
            payload={"tool_call": {
                "id": tool_call_id, "tool_name": "shell", "provider": "native",
                "risk_level": "L3", "status": "pending",
            }},
        )))

        # check cancel before permission.required
        if cancel_check and cancel_check():
            cancel_eid = deterministic_event_id(run_id, "agent.run.cancelled", 99)
            envelopes.append(self._make_event(trace_id, build_runtime_event(
                event_type="agent.run.cancelled", task_id=task_id, run_id=run_id,
                event_id=cancel_eid,
                payload={"run_id": run_id, "reason": "cancelled_by_user"},
            )))
            return envelopes

        # 3. permission.required
        now_iso = _iso_now()
        envelopes.append(self._make_event(trace_id, build_runtime_event(
            event_type="permission.required", task_id=task_id, run_id=run_id,
            step_id=step_id,
            event_id=deterministic_event_id(run_id, "permission.required", 3),
            payload={"request": {
                "id": perm_req_id,
                "task_id": task_id,
                "run_id": run_id,
                "step_id": step_id,
                "tool_name": "shell",
                "action_summary": "执行 Shell 命令: ls -la ~/Desktop",
                "reason": "需要确认 Shell 命令执行权限",
                "risk_level": "L3",
                "scope": {"type": "once"},
                "arguments_summary": {"command": "ls -la ~/Desktop"},
                "allowed_decisions": ["allow_once", "deny"],
                "created_at": now_iso,
            }},
        )))

        # 4. 先登记 pending request_id（防止 decision 早到竞态），
        #    再发布已有事件到 event stream（UI 可见后用户才能 approve/deny）
        if prepare_wait:
            prepare_wait(perm_req_id)
        if publish_cb:
            for env in envelopes:
                publish_cb(env)

        # 5. 等待 decision
        decision = wait_decision(perm_req_id) if wait_decision else None

        approved = decision is not None and decision not in ("deny", "")
        is_timeout = decision is None

        if not is_timeout:
            # approve 或 deny：生成 permission.resolved
            envelopes.append(self._make_event(trace_id, build_runtime_event(
                event_type="permission.resolved", task_id=task_id, run_id=run_id,
                step_id=step_id,
                event_id=deterministic_event_id(run_id, "permission.resolved", 4),
                payload={
                    "request_id": perm_req_id,
                    "decision": decision or "deny",
                    "tool_call_id": tool_call_id,
                },
            )))

        if approved:
            # 5a. approve
            envelopes.append(self._make_event(trace_id, build_runtime_event(
                event_type="tool.call.finished", task_id=task_id, run_id=run_id,
                step_id=step_id,
                event_id=deterministic_event_id(run_id, "tool.call.finished", 5),
                payload={"tool_call": {
                    "id": tool_call_id, "tool_name": "shell", "status": "completed",
                    "result": {"kind": "text", "summary": "命令执行成功"},
                }},
            )))
            envelopes.append(self._make_event(trace_id, build_runtime_event(
                event_type="agent.run.completed", task_id=task_id, run_id=run_id,
                event_id=deterministic_event_id(run_id, "agent.run.completed", 6),
                payload={"output": "权限已批准，任务完成", "total_steps": 1},
            )))
        elif decision == "deny":
            # 5b. deny
            envelopes.append(self._make_event(trace_id, build_runtime_event(
                event_type="tool.call.failed", task_id=task_id, run_id=run_id,
                step_id=step_id,
                event_id=deterministic_event_id(run_id, "tool.call.failed", 5),
                payload={"tool_call": {
                    "id": tool_call_id, "tool_name": "shell", "status": "failed",
                    "error": {"code": "PERMISSION_DENIED", "message": "用户拒绝了权限请求"},
                }},
            )))
            envelopes.append(self._make_event(trace_id, build_runtime_event(
                event_type="agent.run.failed", task_id=task_id, run_id=run_id,
                event_id=deterministic_event_id(run_id, "agent.run.failed", 6),
                payload={"error": {"code": "PERMISSION_DENIED", "message": "用户拒绝了权限请求"}},
            )))
        else:
            # 5c. timeout — 不生成 permission.resolved，明确标记为 timeout
            envelopes.append(self._make_event(trace_id, build_runtime_event(
                event_type="tool.call.failed", task_id=task_id, run_id=run_id,
                step_id=step_id,
                event_id=deterministic_event_id(run_id, "tool.call.failed", 5),
                payload={"tool_call": {
                    "id": tool_call_id, "tool_name": "shell", "status": "failed",
                    "error": {"code": "PERMISSION_TIMEOUT", "message": "等待用户授权超时"},
                }},
            )))
            envelopes.append(self._make_event(trace_id, build_runtime_event(
                event_type="agent.run.failed", task_id=task_id, run_id=run_id,
                event_id=deterministic_event_id(run_id, "agent.run.failed", 6),
                payload={"error": {"code": "PERMISSION_TIMEOUT", "message": "等待用户授权超时"}},
            )))

        return envelopes


    # -- ToolGateway MVP: workspace.list_files scenario --

    def _do_tool_run(
        self,
        job: RunJobMessage,
        cancel_check: Callable[[], bool] | None = None,
    ) -> list[RuntimeEventEnvelope]:
        """Tool scenario — 通过 ToolGateway 执行 workspace.list_files。

        事件序列：
          1. agent.run.started
          2. tool.call.started
          3a. tool.call.finished + agent.run.completed（成功）
          3b. tool.call.failed + agent.run.failed（失败）
        """
        task_id = job.task_id
        run_id = job.run_id
        trace_id = job.trace_id
        step_id = deterministic_step_id(run_id, seq=1)
        tool_call_id = deterministic_event_id(run_id, "tool.call", 10)
        tool_name = "workspace.list_files"

        envelopes: list[RuntimeEventEnvelope] = []

        # 1. agent.run.started
        envelopes.append(self._make_event(trace_id, build_runtime_event(
            event_type="agent.run.started", task_id=task_id, run_id=run_id,
            event_id=deterministic_event_id(run_id, "agent.run.started", 1),
            payload={"agent_id": "agent-default", "mode": "single_agent"},
        )))

        # 检查 cancel
        if cancel_check and cancel_check():
            cancel_eid = deterministic_event_id(run_id, "agent.run.cancelled", 99)
            envelopes.append(self._make_event(trace_id, build_runtime_event(
                event_type="agent.run.cancelled", task_id=task_id, run_id=run_id,
                event_id=cancel_eid,
                payload={"run_id": run_id, "reason": "cancelled_by_user"},
            )))
            return envelopes

        # 确定 workspace_root：job.workspace_path 优先，否则用默认配置
        ws_root = job.workspace_path or self._default_workspace_root

        # 2. tool.call.started
        tool_started_payload = {
            "tool_call": {
                "id": tool_call_id,
                "tool_name": tool_name,
                "provider": "native",
                "risk_level": "L0",
                "status": "running",
                "arguments_summary": {"workspace_root": ws_root or "(未设置)"},
            }
        }
        envelopes.append(self._make_event(trace_id, build_runtime_event(
            event_type="tool.call.started", task_id=task_id, run_id=run_id,
            step_id=step_id,
            event_id=deterministic_event_id(run_id, "tool.call.started", 2),
            payload=tool_started_payload,
        )))

        # 检查 cancel
        if cancel_check and cancel_check():
            cancel_eid = deterministic_event_id(run_id, "agent.run.cancelled", 99)
            envelopes.append(self._make_event(trace_id, build_runtime_event(
                event_type="agent.run.cancelled", task_id=task_id, run_id=run_id,
                event_id=cancel_eid,
                payload={"run_id": run_id, "reason": "cancelled_by_user"},
            )))
            return envelopes

        # 3. 通过 ToolGateway 执行工具
        # workspace_root: job.workspace_path > 默认配置；path 默认 "." 即可
        tool_request = ToolRequest(
            task_id=task_id,
            run_id=run_id,
            step_id=step_id,
            tool_name=tool_name,
            arguments={
                "workspace_root": ws_root,
                "path": ".",  # workspace root 本身
            },
            reason=f"user goal: {job.user_goal[:80]}",
            requested_by="agent",
        )

        assert self._tool_gateway is not None  # 调用方保证
        tool_result: ToolResult = self._tool_gateway.execute(tool_request)

        if tool_result.ok:
            # 3a. 成功
            result_summary = tool_result.summary
            result_data = tool_result.data

            # 构造简洁的 entries 摘要（避免 payload 过大）
            entries_summary = None
            if result_data and isinstance(result_data, dict):
                entries = result_data.get("entries", [])
                if entries:
                    entries_summary = [
                        {"name": e["name"], "type": e["type"]}
                        for e in entries[:20]  # 最多取 20 条
                    ]

            tool_finished_payload = {
                "tool_call": {
                    "id": tool_call_id,
                    "tool_name": tool_name,
                    "status": "completed",
                    "result": {
                        "kind": tool_result.kind,
                        "summary": result_summary,
                        "data": result_data,
                    },
                    "provider": "native",
                    "risk_level": "L0",
                }
            }
            # 将 entries 摘要放在顶层便于前端展示
            if entries_summary:
                tool_finished_payload["entries_summary"] = entries_summary
                tool_finished_payload["entries_count"] = len(entries_summary)

            envelopes.append(self._make_event(trace_id, build_runtime_event(
                event_type="tool.call.finished", task_id=task_id, run_id=run_id,
                step_id=step_id,
                event_id=deterministic_event_id(run_id, "tool.call.finished", 3),
                payload=tool_finished_payload,
            )))

            # agent.run.completed
            log.info("tool scenario 完成: tool=%s result=%s", tool_name, result_summary[:100])
            envelopes.append(self._make_event(trace_id, build_runtime_event(
                event_type="agent.run.completed", task_id=task_id, run_id=run_id,
                event_id=deterministic_event_id(run_id, "agent.run.completed", 4),
                payload={
                    "output": f"workspace.list_files 执行成功。{result_summary}",
                    "total_steps": 1,
                    "tool_result_summary": result_summary,
                },
            )))
        else:
            # 3b. 失败
            error_info = tool_result.error or {
                "code": "TOOL_FAILED",
                "message": tool_result.summary,
                "category": "tool",
                "recoverable": False,
            }

            tool_failed_payload = {
                "tool_call": {
                    "id": tool_call_id,
                    "tool_name": tool_name,
                    "status": "failed",
                    "error": error_info,
                    "provider": "native",
                    "risk_level": "L0",
                }
            }
            envelopes.append(self._make_event(trace_id, build_runtime_event(
                event_type="tool.call.failed", task_id=task_id, run_id=run_id,
                step_id=step_id,
                event_id=deterministic_event_id(run_id, "tool.call.failed", 3),
                payload=tool_failed_payload,
            )))

            # agent.run.failed
            log.warning(
                "tool scenario 失败: tool=%s error=%s",
                tool_name,
                error_info.get("message", "unknown"),
            )
            envelopes.append(self._make_event(trace_id, build_runtime_event(
                event_type="agent.run.failed", task_id=task_id, run_id=run_id,
                event_id=deterministic_event_id(run_id, "agent.run.failed", 4),
                payload={
                    "error": error_info,
                },
            )))

        return envelopes


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
