"""百度高精度 OCR Provider adapter。

本模块只负责调用外部 OCR，不决定哪些页面应当外发。调用必须由上层经过
ToolGateway / PermissionManager 后触发。
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urlencode

import httpx

from jarvis_worker.agent.rag.contracts import OcrResult, OcrSpan


_TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
_ACCURATE_URL = "https://aip.baidubce.com/rest/2.0/ocr/v1/accurate"
_SUPPORTED_MIME_TYPES = {"image/jpeg", "image/png", "image/bmp"}
_MAX_ENCODED_IMAGE_BYTES = 4 * 1024 * 1024


class BaiduOcrError(RuntimeError):
    """不包含凭据、响应正文或原始供应商异常的安全错误。"""


class BaiduOcrProvider:
    provider_name = "baidu"
    model_version = "accurate-v1"

    def __init__(
        self,
        *,
        api_key: str,
        secret_key: str,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        if not api_key.strip() or not secret_key.strip():
            raise ValueError("百度 OCR API key 和 secret key 不能为空")
        if timeout_seconds <= 0:
            raise ValueError("百度 OCR timeout 必须大于 0")
        self._api_key = api_key
        self._secret_key = secret_key
        self._client_factory = client_factory or (
            lambda: httpx.AsyncClient(timeout=timeout_seconds)
        )
        self._access_token: str | None = None
        self._token_expires_at = 0.0

    async def recognize(
        self, *, image: bytes, mime_type: str, languages: Sequence[str]
    ) -> OcrResult:
        if mime_type.casefold() not in _SUPPORTED_MIME_TYPES:
            raise ValueError("百度 OCR 仅接受 JPEG、PNG 或 BMP 图片")
        if not image:
            raise ValueError("百度 OCR 图片不能为空")
        encoded = base64.b64encode(image).decode("ascii")
        language_type = _language_type(languages)
        form = {
            "image": encoded,
            "language_type": language_type,
            "detect_direction": "true",
            "vertexes_location": "true",
            "probability": "true",
        }
        if len(urlencode(form).encode("ascii")) > _MAX_ENCODED_IMAGE_BYTES:
            raise ValueError("百度 OCR 编码后的图片超过 4 MiB 上限")

        token = await self._get_access_token()
        try:
            async with self._client_factory() as client:
                response = await client.post(
                    _ACCURATE_URL,
                    params={"access_token": token},
                    data=form,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            raise BaiduOcrError("百度 OCR 服务暂时不可用") from exc
        payload = _safe_json(response)
        if response.status_code != 200 or "error_code" in payload:
            raise BaiduOcrError("百度 OCR 识别失败")
        return _parse_result(payload, language_type=language_type)

    async def _get_access_token(self) -> str:
        now = time.monotonic()
        if self._access_token and now < self._token_expires_at:
            return self._access_token
        try:
            async with self._client_factory() as client:
                response = await client.post(
                    _TOKEN_URL,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self._api_key,
                        "client_secret": self._secret_key,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            raise BaiduOcrError("百度 OCR 认证服务暂时不可用") from exc
        payload = _safe_json(response)
        token = payload.get("access_token")
        if response.status_code != 200 or not isinstance(token, str) or not token:
            raise BaiduOcrError("百度 OCR 认证失败")
        expires_in = payload.get("expires_in", 2_592_000)
        ttl = float(expires_in) if isinstance(expires_in, (int, float)) else 2_592_000.0
        self._access_token = token
        self._token_expires_at = now + max(60.0, ttl - 300.0)
        return token


def _language_type(languages: Sequence[str]) -> str:
    normalized = {item.strip().casefold() for item in languages if item.strip()}
    if normalized and normalized <= {"en", "eng", "english"}:
        return "ENG"
    if normalized and normalized <= {"ja", "jpn", "japanese"}:
        return "JAP"
    if normalized and normalized <= {"ko", "kor", "korean"}:
        return "KOR"
    return "CHN_ENG"


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise BaiduOcrError("百度 OCR 返回了无效响应") from exc
    if not isinstance(payload, dict):
        raise BaiduOcrError("百度 OCR 返回了无效响应")
    return payload


def _parse_result(payload: dict[str, Any], *, language_type: str) -> OcrResult:
    spans: list[OcrSpan] = []
    for item in payload.get("words_result", []):
        if not isinstance(item, dict):
            continue
        text = str(item.get("words") or "").strip()
        location = item.get("location")
        if not text or not isinstance(location, dict):
            continue
        try:
            left = float(location["left"])
            top = float(location["top"])
            width = float(location["width"])
            height = float(location["height"])
        except (KeyError, TypeError, ValueError):
            continue
        if width <= 0 or height <= 0:
            continue
        probability = item.get("probability")
        confidence = 1.0
        if isinstance(probability, dict):
            average = probability.get("average")
            if isinstance(average, (int, float)):
                confidence = min(max(float(average), 0.0), 1.0)
        spans.append(
            OcrSpan(
                text=text,
                bounding_box=(left, top, left + width, top + height),
                confidence=confidence,
            )
        )
    text = "\n".join(span.text for span in spans)
    return OcrResult(
        text=text,
        spans=tuple(spans),
        language=language_type,
        provider=BaiduOcrProvider.provider_name,
        model_version=BaiduOcrProvider.model_version,
    )
