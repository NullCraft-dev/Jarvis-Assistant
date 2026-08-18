"""凭据检测与脱敏。

只识别可用于认证的高置信度秘密，不把邮箱、电话等一般个人信息混入该策略。
调用方仍需依据业务语义决定是拒绝持久化，还是只做展示脱敏。
"""

from __future__ import annotations

import re

_CREDENTIAL_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----", re.I),
    re.compile(r"\b(?:sk-(?:proj-)?|ghp_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(
        r"\b(?:api[_ -]?key|auth[_ -]?token|access[_ -]?token|refresh[_ -]?token|client[_ -]?secret|"
        r"password|passwd|密码|密钥|令牌)\s*[:=]\s*[\"']?[^\s,;\"']{8,}",
        re.I,
    ),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b", re.I),
)

_PERSISTENCE_REQUEST_PATTERNS = (
    re.compile(r"(?:记住|保存|存储|储存|记录|以后(?:自动)?使用|下次(?:自动)?使用)"),
    re.compile(
        r"\b(?:remember|save|store|persist)\b|\buse\b.{0,40}\b(?:later|future)\b",
        re.I,
    ),
)


def contains_credential(value: str) -> bool:
    return isinstance(value, str) and any(pattern.search(value) for pattern in _CREDENTIAL_PATTERNS)


def requests_credential_persistence(value: str) -> bool:
    """识别要求长期保存或后续自动使用凭据的高风险请求。"""
    return contains_credential(value) and any(
        pattern.search(value) for pattern in _PERSISTENCE_REQUEST_PATTERNS
    )


def redact_credentials(value: str, replacement: str = "[已隐藏凭据]") -> str:
    if not isinstance(value, str):
        return ""
    result = value
    for pattern in _CREDENTIAL_PATTERNS:
        result = pattern.sub(replacement, result)
    return result
