import pytest

from jarvis_worker.shared.security import (
    contains_credential,
    redact_credentials,
    requests_credential_persistence,
)


@pytest.mark.parametrize(
    "value",
    [
        "api_key=super-secret-value-123",
        "Authorization: Bearer abcdefghijklmnop",
        'export const AUTH_TOKEN = "fake-eval-token"',
        "sk-proj-abcdefghijklmnopqrstuv",
        "AKIAABCDEFGHIJKLMNOP",
        "-----BEGIN PRIVATE KEY-----",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature123",
    ],
)
def test_credential_family_is_detected_and_redacted(value):
    assert contains_credential(value) is True
    assert value not in redact_credentials(value)


@pytest.mark.parametrize(
    "value",
    ["普通项目偏好", "联系邮箱 user@example.com", "解释 API key 应如何安全存放"],
)
def test_non_credential_text_is_not_misclassified(value):
    assert contains_credential(value) is False


@pytest.mark.parametrize(
    "value",
    [
        "记住我的 API key 是 sk-proj-abcdefghijklmnopqrstuv，以后自动使用",
        "Please save api_key=super-secret-value-123 for later use",
    ],
)
def test_credential_persistence_request_is_detected(value):
    assert requests_credential_persistence(value) is True


def test_credential_without_persistence_request_is_not_escalated():
    assert requests_credential_persistence("校验 api_key=super-secret-value-123 是否有效") is False
