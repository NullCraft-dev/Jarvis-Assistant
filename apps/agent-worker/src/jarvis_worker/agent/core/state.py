"""AgentState — AgentRunner / LangGraph 循环中的最小状态。

AgentState 是项目 Runtime 的状态 owner；LangGraph 只把它作为图内状态承载。
当前保持最小：只记录运行标识、用户目标、workspace 边界、观测历史、迭代计数
和 Runtime effect guard 的恢复状态。
多轮对话 MVP：新增 history_messages 承载同一会话的历史消息。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentState:
    """AgentRunner 循环中的最小状态。

    Attributes:
        task_id: 关联 task id
        run_id: 关联 run id
        user_goal: 用户输入的原始目标
        workspace_root: 允许访问的 workspace 根目录（空表示未设置）
        observations: 工具执行结果观测列表
        history_messages: 同一会话的历史消息 [{role, content}, ...]
        trusted_history_provenance: Runtime 从最近完整历史 Run 的 ToolCalls
            恢复的可信来源侧链；不进入模型消息
        skill_context: 当前 Run 解析出的 Skill 上下文；None 表示尚未解析
        intent: 当前 Run 的版本化结构化意图；checkpoint 恢复时复用
        intent_context: Runtime 冻结的匿名 RAG 文档目录；模型看不到 UUID
        intent_rejections: LLM Intent 候选被结构校验拒绝的次数
        intent_feedback: 下一次 Intent 提取使用的固定 Runtime 纠正反馈
        iteration: 当前循环轮次（从 0 开始）
        effect_guard_rejections: 因缺少必需工具成功证据而拒绝 finish 的次数
        effect_guard_feedback: 注入下一轮模型的可信 Runtime 校验反馈
        answer_guard_rejections: 最终回答结构/引用被 Runtime 拒绝的次数
        answer_guard_feedback: 下一次只读终态重写使用的可信校验反馈
        source_chain_guard_rejections: 连续源码无进展动作被 Runtime 退回的次数
        source_chain_evidence_rejections: 源码证据链不完整而返回工具规划的连续次数
        source_chain_slot_attempts: 旧版固定槽轮转状态，仅为 checkpoint 向后兼容保留
        model_output_rejections: Provider 已完成内部重试后，Runtime 触发结构化自纠的次数
        final_output: 最终输出文本（finish 时设置）
        next_step_seq: 下一轮 RuntimeEvent 的确定性序号（图循环恢复使用）
        recovery_attempts: Worker 崩溃后的持久化恢复次数
        completion_contract: Runtime 从已校验 Intent 冻结的完成要求
        loop_progress: 从可信 ToolResult observation 推导的进展快照
        stop_decision: 最近一次结构化继续/完成/澄清/失败决定
        run_control: Harness 拥有的 deadline 与模型/工具预算快照
    """

    task_id: str = ""
    run_id: str = ""
    user_goal: str = ""
    workspace_root: str = ""
    observations: list[dict[str, Any]] = field(default_factory=list)
    history_messages: list[dict[str, str]] = field(default_factory=list)
    trusted_history_provenance: list[dict[str, str]] = field(default_factory=list)
    memory_items: list[dict[str, Any]] = field(default_factory=list)
    skill_context: dict[str, Any] | None = None
    intent: dict[str, Any] | None = None
    intent_context: dict[str, Any] | None = None
    intent_rejections: int = 0
    intent_feedback: str = ""
    iteration: int = 0
    effect_guard_rejections: int = 0
    answer_guard_rejections: int = 0
    answer_guard_feedback: str = ""
    source_chain_guard_rejections: int = 0
    source_chain_evidence_rejections: int = 0
    source_chain_slot_attempts: dict[str, int] = field(default_factory=dict)
    model_output_rejections: int = 0
    effect_guard_feedback: str = ""
    final_output: str = ""
    next_step_seq: int = 1
    recovery_attempts: int = 0
    completion_contract: dict[str, Any] | None = None
    loop_progress: dict[str, Any] | None = None
    stop_decision: dict[str, Any] | None = None
    run_control: dict[str, Any] | None = None

    def add_observation(self, obs: dict[str, Any]) -> None:
        """添加一条观测记录。"""
        self.observations.append(obs)
        self.iteration += 1
