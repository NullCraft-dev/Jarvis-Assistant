"""本地 PaddleOCR-VL 完整 Pipeline adapter。

布局分析在 PaddleOCR 客户端完成；元素识别通过 localhost MLX-VLM 服务执行。
不直接调用 MLX HTTP API，避免绕过完整的版面检测、裁剪和阅读顺序恢复。
"""

from __future__ import annotations

import asyncio
import hashlib
import site
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from jarvis_worker.agent.rag.preprocessing.contracts import (
    DocumentNode,
    DocumentNodeType,
    NodeExtractionMethod,
    StructurePageResult,
)
from jarvis_worker.agent.rag.preprocessing.identifiers import build_document_node_id

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class PaddleOcrVlError(RuntimeError):
    """不泄露模型输出、路径或内部异常的安全错误。"""


@dataclass(frozen=True, slots=True)
class PaddleOcrVlConfig:
    server_url: str = "http://127.0.0.1:8111/"
    model_name: str = "PaddlePaddle/PaddleOCR-VL-1.6"
    pipeline_version: str = "v1.6"
    max_concurrency: int = 1
    max_pixels: int = 4_000_000
    max_new_tokens: int = 4096
    use_chart_recognition: bool = True
    use_ocr_for_image_block: bool = True
    client_site_packages: str = ""

    def __post_init__(self) -> None:
        parsed = urlparse(self.server_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in _LOCAL_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("MLX-VLM server_url 必须是无凭据的 localhost HTTP 地址")
        if self.max_concurrency != 1:
            raise ValueError("本地 PaddleOCR-VL 第一版必须使用单并发")
        if not self.model_name.strip() or not self.pipeline_version.strip():
            raise ValueError("PaddleOCR-VL model/pipeline version 不能为空")
        if self.max_pixels < 1 or self.max_new_tokens < 1:
            raise ValueError("PaddleOCR-VL 资源上限必须大于 0")


class PaddleOcrVlProvider:
    provider_name = "paddleocr-vl-local"

    def __init__(
        self,
        config: PaddleOcrVlConfig | None = None,
        *,
        pipeline_factory: Callable[[PaddleOcrVlConfig], Any] | None = None,
        image_decoder: Callable[[bytes], Any] | None = None,
    ) -> None:
        self._config = config or PaddleOcrVlConfig()
        self.provider_version = self._config.pipeline_version
        self._pipeline_factory = pipeline_factory or _create_pipeline
        self._image_decoder = image_decoder or _decode_image
        self._pipeline: Any | None = None
        self._semaphore = asyncio.Semaphore(1)
        self._waiting = 0
        self._active = 0

    @property
    def waiting_requests(self) -> int:
        return self._waiting

    @property
    def active_requests(self) -> int:
        return self._active

    async def analyze_page(
        self,
        *,
        image: bytes,
        mime_type: str,
        page_number: int,
        page_width: float,
        page_height: float,
    ) -> StructurePageResult:
        if mime_type != "image/png" or not image:
            raise ValueError("PaddleOCR-VL 页面必须是非空 PNG")
        if page_number < 1 or page_width <= 0 or page_height <= 0:
            raise ValueError("PaddleOCR-VL 页面定位无效")

        self._waiting += 1
        acquired = False
        try:
            async with self._semaphore:
                self._waiting -= 1
                acquired = True
                self._active = 1
                try:
                    nodes = await asyncio.to_thread(
                        self._analyze_sync,
                        image,
                        page_number,
                        page_width,
                        page_height,
                    )
                finally:
                    self._active = 0
        except asyncio.CancelledError:
            if not acquired:
                self._waiting -= 1
            raise
        except PaddleOcrVlError:
            raise
        except Exception as exc:
            raise PaddleOcrVlError("本地 PaddleOCR-VL 页面解析失败") from exc
        return StructurePageResult(
            page_number=page_number,
            nodes=nodes,
            provider=self.provider_name,
            provider_version=self.provider_version,
        )

    def _analyze_sync(
        self,
        image: bytes,
        page_number: int,
        page_width: float,
        page_height: float,
    ) -> tuple[DocumentNode, ...]:
        if self._pipeline is None:
            try:
                self._pipeline = self._pipeline_factory(self._config)
            except Exception as exc:
                raise PaddleOcrVlError("本地 PaddleOCR-VL Pipeline 初始化失败") from exc
        image_value = _limit_image_pixels(
            self._image_decoder(image), self._config.max_pixels
        )
        try:
            results = self._pipeline.predict(
                image_value,
                use_layout_detection=True,
                use_chart_recognition=self._config.use_chart_recognition,
                use_ocr_for_image_block=self._config.use_ocr_for_image_block,
                format_block_content=True,
                max_new_tokens=self._config.max_new_tokens,
                temperature=0.0,
            )
        except Exception as exc:
            raise PaddleOcrVlError("本地 PaddleOCR-VL 推理失败") from exc
        result_list = list(results)
        if len(result_list) != 1:
            raise PaddleOcrVlError("本地 PaddleOCR-VL 返回页面数量异常")
        payload = getattr(result_list[0], "json", None)
        if callable(payload):
            payload = payload()
        if not isinstance(payload, dict):
            raise PaddleOcrVlError("本地 PaddleOCR-VL 返回结构无效")
        return _parse_nodes(
            payload,
            page_number=page_number,
            page_width=page_width,
            page_height=page_height,
            version=self.provider_version,
        )


def _create_pipeline(config: PaddleOcrVlConfig) -> Any:
    if config.client_site_packages:
        client_site_packages = Path(config.client_site_packages).expanduser().resolve()
        if not client_site_packages.is_dir():
            raise PaddleOcrVlError("本地 PaddleOCR-VL 客户端依赖目录不存在")
        # PaddleOCR 的重型依赖保存在项目隔离运行时中；addsitedir 追加到
        # sys.path 尾部，避免覆盖 Conda 主环境已经加载的核心 Runtime 依赖。
        site.addsitedir(str(client_site_packages))
    try:
        from paddleocr import PaddleOCRVL
    except ImportError as exc:
        raise PaddleOcrVlError(
            "未安装本地 PaddleOCR-VL 可选运行环境"
        ) from exc
    return PaddleOCRVL(
        pipeline_version=config.pipeline_version,
        vl_rec_backend="mlx-vlm-server",
        vl_rec_server_url=config.server_url,
        vl_rec_api_model_name=config.model_name,
        vl_rec_max_concurrency=1,
        use_layout_detection=True,
        use_chart_recognition=config.use_chart_recognition,
    )


def _decode_image(image: bytes) -> Any:
    try:
        import numpy
        from PIL import Image
    except ImportError as exc:
        raise PaddleOcrVlError(
            "未安装本地 PaddleOCR-VL 图片解码依赖"
        ) from exc
    try:
        with Image.open(BytesIO(image)) as source:
            return numpy.asarray(source.convert("RGB"))
    except Exception as exc:
        raise PaddleOcrVlError("PaddleOCR-VL 页面图片无法解码") from exc


def _limit_image_pixels(image: Any, max_pixels: int) -> Any:
    shape = getattr(image, "shape", None)
    if not isinstance(shape, tuple) or len(shape) < 2:
        return image
    height, width = int(shape[0]), int(shape[1])
    if height < 1 or width < 1 or height * width <= max_pixels:
        return image
    try:
        import numpy
        from PIL import Image
    except ImportError as exc:
        raise PaddleOcrVlError("无法应用 PaddleOCR-VL 页面像素上限") from exc
    scale = (max_pixels / (height * width)) ** 0.5
    target = (max(1, int(width * scale)), max(1, int(height * scale)))
    return numpy.asarray(Image.fromarray(image).resize(target, Image.Resampling.LANCZOS))


def _parse_nodes(
    payload: dict[str, Any],
    *,
    page_number: int,
    page_width: float,
    page_height: float,
    version: str,
) -> tuple[DocumentNode, ...]:
    root = payload.get("res") if isinstance(payload.get("res"), dict) else payload
    raw_nodes = root.get("parsing_res_list")
    if not isinstance(raw_nodes, list):
        raise PaddleOcrVlError("本地 PaddleOCR-VL 缺少版面解析结果")
    source_width = _positive_number(root.get("width")) or page_width
    source_height = _positive_number(root.get("height")) or page_height
    nodes: list[DocumentNode] = []
    for fallback_order, raw in enumerate(raw_nodes):
        if not isinstance(raw, dict):
            continue
        raw_text = str(raw.get("block_content") or "").strip()
        node_type = _node_type(str(raw.get("block_label") or ""))
        text, source_format = _normalize_content(raw_text, node_type)
        bbox = _scaled_bbox(
            raw.get("block_bbox"),
            source_width=source_width,
            source_height=source_height,
            page_width=page_width,
            page_height=page_height,
        )
        if bbox is None or not text:
            continue
        raw_order = raw.get("block_order")
        order_index = int(raw_order) if isinstance(raw_order, int) and raw_order >= 0 else fallback_order
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        node_id = build_document_node_id(
            page_number=page_number,
            order_index=order_index,
            node_type=node_type,
            bounding_box=bbox,
            extraction_method=NodeExtractionMethod.PADDLEOCR_VL,
            extraction_version=version,
            content_hash=content_hash,
        )
        nodes.append(
            DocumentNode(
                node_id=node_id,
                node_type=node_type,
                page_number=page_number,
                order_index=order_index,
                bounding_box=bbox,
                page_width=page_width,
                page_height=page_height,
                text=text,
                structured_data={
                    "source_label": str(raw.get("block_label") or ""),
                    "source_format": source_format,
                    "source_content_hash": hashlib.sha256(
                        raw_text.encode("utf-8")
                    ).hexdigest(),
                },
                extraction_method=NodeExtractionMethod.PADDLEOCR_VL,
                extraction_version=version,
                confidence=0.9,
            )
        )
    return tuple(sorted(nodes, key=lambda node: node.order_index))


def _node_type(label: str) -> DocumentNodeType:
    normalized = label.strip().casefold().replace("-", "_")
    if normalized in {"doc_title", "paragraph_title", "title", "header"}:
        return DocumentNodeType.HEADING
    if normalized in {"table", "table_body"}:
        return DocumentNodeType.TABLE
    if normalized in {"formula", "display_formula", "inline_formula", "equation"}:
        return DocumentNodeType.FORMULA
    if normalized in {"chart", "plot"}:
        return DocumentNodeType.CHART
    if normalized in {"image", "figure"}:
        return DocumentNodeType.IMAGE
    if normalized in {"figure_title", "table_title", "caption"}:
        return DocumentNodeType.CAPTION
    if normalized in {"code", "algorithm"}:
        return DocumentNodeType.CODE
    if normalized in {"list", "reference", "references"}:
        return DocumentNodeType.LIST
    return DocumentNodeType.PARAGRAPH


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def _normalize_content(
    content: str, node_type: DocumentNodeType
) -> tuple[str, str]:
    if node_type not in {DocumentNodeType.TABLE, DocumentNodeType.CHART}:
        return content, "text"
    if "<table" not in content.casefold():
        return content, "text"
    parser = _TableParser()
    try:
        parser.feed(content)
    except ValueError:
        return content, "html"
    if not parser.rows:
        return content, "html"
    width = max(len(row) for row in parser.rows)
    rows = [row + [""] * (width - len(row)) for row in parser.rows]
    escaped = [[cell.replace("|", "\\|") for cell in row] for row in rows]
    markdown = ["| " + " | ".join(escaped[0]) + " |"]
    markdown.append("| " + " | ".join("---" for _ in range(width)) + " |")
    markdown.extend("| " + " | ".join(row) + " |" for row in escaped[1:])
    return "\n".join(markdown), "html_table_normalized_to_markdown"


def _positive_number(value: object) -> float | None:
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


def _scaled_bbox(
    value: object,
    *,
    source_width: float,
    source_height: float,
    page_width: float,
    page_height: float,
) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(item) for item in value)
    except (TypeError, ValueError):
        return None
    scaled = (
        max(0.0, min(page_width, x0 / source_width * page_width)),
        max(0.0, min(page_height, y0 / source_height * page_height)),
        max(0.0, min(page_width, x1 / source_width * page_width)),
        max(0.0, min(page_height, y1 / source_height * page_height)),
    )
    if scaled[2] <= scaled[0] or scaled[3] <= scaled[1]:
        return None
    return scaled
