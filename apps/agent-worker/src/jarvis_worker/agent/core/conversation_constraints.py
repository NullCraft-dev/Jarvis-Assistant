"""Host-owned constraints for follow-ups that operate on prior conversation output."""

from __future__ import annotations

import re

_HISTORY_REFERENCE = re.compile(
    r"(?:上一(?:条|轮)(?:回答|回复|结果|内容)?|刚才(?:的)?(?:回答|回复|结果|内容|比较|总结)|"
    r"前述|上述|这个(?:回答|回复|结果|表格|总结)|比较结果|"
    r"previous\s+(?:answer|response|result)|above\s+(?:answer|response|result))",
    re.IGNORECASE,
)
_TRANSFORM_ACTION = re.compile(
    r"(?:压缩|改写|重写|润色|翻译|格式化|整理|转换|缩写|概括|总结|改成|变成|"
    r"compress|rewrite|reformat|format|translate|summari[sz]e|turn\s+.+\s+into)",
    re.IGNORECASE,
)
_EVIDENCE_REFRESH = re.compile(
    r"(?:重新|再次|再)\s*(?:搜索|检索|查找|核对|验证|查证)|"
    r"(?:核对|验证|查证).{0,16}(?:原文|来源|引用|证据)|"
    r"(?:补充|新增).{0,16}(?:来源|引用|证据|事实)|"
    r"(?:re-?search|re-?retrieve|verify|fact[- ]?check|check).{0,30}"
    r"(?:source|citation|evidence|original)",
    re.IGNORECASE,
)
_EXTERNAL_EFFECT = re.compile(
    r"(?:保存|写入|创建|新建|上传|发送|发布|提交|导出|加入|"
    r"存入|删除|移动|重命名|覆盖|修改|更新|"
    r"save|write|create|upload|send|publish|submit|export|store|delete|move|rename|"
    r"overwrite|update)",
    re.IGNORECASE,
)
_CITATION_REFERENCE = re.compile(r"(?:引用|出处|来源|citation|source)", re.IGNORECASE)
_CITATION_VERIFICATION = re.compile(
    r"(?:支持|证明|吻合|对应|准确|正确|有误|错误|support|verify|accurate|correct|wrong)",
    re.IGNORECASE,
)
_DEICTIC_DOCUMENT_REFERENCE = re.compile(
    r"(?:这|那|刚才)(?:一)?(?:份|篇|个)?\s*"
    r"(?:(?:《[^》]{2,200}》|[A-Za-z][A-Za-z0-9_.+\-]*(?:\s+[A-Za-z0-9_.+\-]+){0,5})\s*)?"
    r"(?:手册|文档|论文|PDF|资料|文件|报告)|"
    r"(?:this|that|previous)\s+(?:manual|document|paper|pdf|file|report)",
    re.IGNORECASE,
)


def is_prior_answer_transform_goal(user_goal: str) -> bool:
    """Return true only for a transformation of existing conversational output.

    Explicit requests to refresh or verify evidence keep the normal retrieval path.
    """

    if not isinstance(user_goal, str):
        return False
    goal = user_goal.strip()[:10_000]
    return bool(
        goal
        and _HISTORY_REFERENCE.search(goal)
        and _TRANSFORM_ACTION.search(goal)
        and not _EVIDENCE_REFRESH.search(goal)
        and not _EXTERNAL_EFFECT.search(goal)
    )


def is_citation_verification_goal(user_goal: str) -> bool:
    """Recognize follow-ups asking whether a prior citation supports a claim."""

    if not isinstance(user_goal, str):
        return False
    goal = user_goal.strip()[:10_000]
    return bool(
        goal
        and _HISTORY_REFERENCE.search(goal)
        and _CITATION_REFERENCE.search(goal)
        and _CITATION_VERIFICATION.search(goal)
    )


def is_deictic_document_reference_goal(user_goal: str) -> bool:
    """Recognize a document reference that needs conversational grounding."""

    if not isinstance(user_goal, str):
        return False
    return bool(_DEICTIC_DOCUMENT_REFERENCE.search(user_goal.strip()[:10_000]))
