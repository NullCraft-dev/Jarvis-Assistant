"""Agent Loop Engineering 的结构化控制面。"""

from jarvis_worker.agent.loop.contracts import (
    COMPLETION_CONTRACT_VERSION,
    LOOP_PROGRESS_VERSION,
    LOOP_STOP_DECISION_VERSION,
    CompletionContract,
    LoopProgressSnapshot,
    StopDecision,
)
from jarvis_worker.agent.loop.controller import LoopController

__all__ = [
    "COMPLETION_CONTRACT_VERSION",
    "LOOP_PROGRESS_VERSION",
    "LOOP_STOP_DECISION_VERSION",
    "CompletionContract",
    "LoopController",
    "LoopProgressSnapshot",
    "StopDecision",
]
