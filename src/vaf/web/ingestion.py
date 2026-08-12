"""Bounded ingestion for local PRD files and public Feishu documents."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from hashlib import sha256
from html.parser import HTMLParser
from io import BytesIO
import ipaddress
import re
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import zipfile
import xml.etree.ElementTree as ET

from vaf.ports.agents import ImageInput


MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_COUNT = 8
MAX_TOTAL_IMAGE_BYTES = 20 * 1024 * 1024
ALLOWED_EXTENSIONS = {".md", ".markdown", ".txt", ".pdf", ".html", ".htm", ".docx"}
ALLOWED_IMAGE_TYPES = {
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
VISUAL_TERMS = re.compile(
    r"原型|线框|页面|界面|交互|截图|prototype|wireframe|mockup|screen(?:shot)?|\bui\b|\bux\b",
    re.IGNORECASE,
)


class DocumentIngestionError(ValueError):
    """Raised when a source cannot be safely converted into text."""


@dataclass(frozen=True)
class IngestedDocument:
    name: str
    source_type: str
    source_ref: str
    text: str
    content_hash: str
    visual_inputs: tuple[ImageInput, ...] = ()
    unresolved_visual_references: tuple[str, ...] = ()

    @property
    def character_count(self) -> int:
        return len(self.text)

    @property
    def has_visual_evidence(self) -> bool:
        return bool(self.visual_inputs)


def ingest_upload(
    filename: str,
    content: bytes,
    prototype_files: Iterable[tuple[str, bytes]] = (),
) -> IngestedDocument:
    safe_name = Path(filename or "prd.md").name
    suffix = Path(safe_name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise DocumentIngestionError(
            f"不支持的文件格式：{suffix or '无扩展名'}；支持 Markdown、PDF、DOCX、HTML 和 TXT"
    )
    _check_size(content)
    text = _extract_text(suffix, content)
    visual_inputs, unresolved = _extract_visual_inputs(suffix, content, text)
    visual_inputs = _merge_visual_inputs(
        visual_inputs,
        (_image_from_bytes(name, data) for name, data in prototype_files),
    )
    return _make_document(
        safe_name,
        "upload",
        safe_name,
        text,
        visual_inputs=visual_inputs,
        unresolved_visual_references=unresolved,
    )


def ingest_feishu(url: str) -> IngestedDocument:
    parsed = urlparse(url.strip())
    hostname = (parsed.hostname or "").lower()
    allowed_hosts = ("feishu.cn", "feishu.com", "larksuite.com")
    if parsed.scheme != "https" or not hostname or not any(
        hostname == host or hostname.endswith(f".{host}") for host in allowed_hosts
    ):
        raise DocumentIngestionError("只允许读取 HTTPS 飞书/Lark 链接，避免把本地服务当作远程文档读取")
    request = Request(
        url,
        headers={
            "User-Agent": "VAF/0.1 PRD importer",
            "Accept": "text/html,text/plain,application/xhtml+xml",
        },
    )
    try:
        with urlopen(request, timeout=20) as response:
            content = response.read(MAX_DOCUMENT_BYTES + 1)
            content_type = response.headers.get_content_type()
    except Exception as exc:
        raise DocumentIngestionError(f"飞书文档读取失败：{exc}") from exc
    _check_size(content)
    text = _extract_text(".html" if "html" in content_type else ".txt", content)
    if len(re.sub(r"\s+", "", text)) < 40:
        raise DocumentIngestionError("飞书链接没有返回可读正文；请确认链接可公开访问，或下载后上传 PDF/Markdown")
    suffix = ".html" if "html" in content_type else ".txt"
    visual_inputs, unresolved = _extract_visual_inputs(suffix, content, text)
    return _make_document(
        "feishu-document",
        "feishu",
        url,
        text,
        visual_inputs=visual_inputs,
        unresolved_visual_references=unresolved,
    )


def _make_document(
    name: str,
    source_type: str,
    source_ref: str,
    text: str,
    *,
    visual_inputs: tuple[ImageInput, ...] = (),
    unresolved_visual_references: tuple[str, ...] = (),
) -> IngestedDocument:
    normalized = text.replace("\x00", "").strip()
    if not normalized:
        raise DocumentIngestionError("文档正文为空，无法启动研发流程")
    digest = sha256(normalized.encode("utf-8"))
    for image in visual_inputs:
        digest.update(image.content_hash.encode("ascii"))
    return IngestedDocument(
        name=name,
        source_type=source_type,
        source_ref=source_ref,
        text=normalized,
        content_hash=f"sha256:{digest.hexdigest()}",
        visual_inputs=visual_inputs,
        unresolved_visual_references=unresolved_visual_references,
    )


def _check_size(content: bytes) -> None:
    if len(content) > MAX_DOCUMENT_BYTES:
        raise DocumentIngestionError("文档超过 20 MB 限制，请拆分后重新上传")


def _extract_text(suffix: str, content: bytes) -> str:
    if suffix in {".md", ".markdown", ".txt"}:
        return content.decode("utf-8-sig", errors="replace")
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise DocumentIngestionError("PDF 解析依赖未安装，请执行 pip install 'vaf[web]'") from exc
        try:
            reader = PdfReader(BytesIO(content))
            return "\n\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception as exc:
            raise DocumentIngestionError(f"PDF 解析失败：{exc}") from exc
    if suffix == ".docx":
        return _extract_docx(content)
    return _HtmlTextParser().parse(content.decode("utf-8", errors="replace"))


def _extract_docx(content: bytes) -> str:
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            xml = archive.read("word/document.xml")
        root = ET.fromstring(xml)
    except Exception as exc:
        raise DocumentIngestionError(f"DOCX 解析失败：{exc}") from exc
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{namespace}p"):
        value = "".join(node.text or "" for node in paragraph.iter(f"{namespace}t"))
        if value.strip():
            paragraphs.append(value.strip())
    return "\n\n".join(paragraphs)


def _extract_visual_inputs(
    suffix: str,
    content: bytes,
    text: str,
) -> tuple[tuple[ImageInput, ...], tuple[str, ...]]:
    decoded = content.decode("utf-8", errors="ignore")
    images: list[ImageInput] = []
    unresolved: list[str] = []

    for match in re.finditer(r"!\[([^\]]*)\]\(([^\n)]+)\)", decoded, re.IGNORECASE):
        alt, raw_source = match.group(1), match.group(2).strip().strip("<>")
        nearby = decoded[max(0, match.start() - 240) : min(len(decoded), match.end() + 120)]
        if not VISUAL_TERMS.search(f"{alt} {raw_source} {nearby}"):
            continue
        image = _image_from_reference(raw_source, f"markdown-image-{len(images) + 1}")
        if image is None:
            unresolved.append(raw_source[:500])
        else:
            images.append(image)

    for index, match in enumerate(re.finditer(r"<img\b[^>]*>", decoded, re.IGNORECASE), start=1):
        tag = match.group(0)
        nearby = decoded[max(0, match.start() - 240) : min(len(decoded), match.end() + 120)]
        source_match = re.search(r"\bsrc\s*=\s*['\"]([^'\"]+)['\"]", tag, re.IGNORECASE)
        if not source_match or not VISUAL_TERMS.search(f"{tag} {nearby}"):
            continue
        raw_source = source_match.group(1).strip()
        image = _image_from_reference(raw_source, f"html-image-{index}")
        if image is None:
            unresolved.append(raw_source[:500])
        else:
            images.append(image)

    if suffix == ".pdf" and VISUAL_TERMS.search(text):
        images.extend(_extract_pdf_images(content))
    elif suffix == ".docx" and VISUAL_TERMS.search(text):
        images.extend(_extract_docx_images(content))

    return _limit_visual_inputs(images), tuple(dict.fromkeys(unresolved))


def _extract_pdf_images(content: bytes) -> list[ImageInput]:
    try:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(content))
        images: list[ImageInput] = []
        for page_index, page in enumerate(reader.pages, start=1):
            for image_index, image in enumerate(page.images, start=1):
                name = getattr(image, "name", None) or f"page-{page_index}-{image_index}.png"
                try:
                    images.append(_image_from_bytes(name, image.data))
                except DocumentIngestionError:
                    continue
        return images
    except Exception:
        return []


def _extract_docx_images(content: bytes) -> list[ImageInput]:
    images: list[ImageInput] = []
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            for name in archive.namelist():
                if name.startswith("word/media/") and not name.endswith("/"):
                    try:
                        images.append(_image_from_bytes(Path(name).name, archive.read(name)))
                    except DocumentIngestionError:
                        continue
    except zipfile.BadZipFile:
        return []
    return images


def _image_from_reference(source: str, source_name: str) -> ImageInput | None:
    data_match = re.fullmatch(
        r"data:(image/(?:png|jpeg|gif|webp));base64,(.+)",
        source,
        re.IGNORECASE | re.DOTALL,
    )
    if data_match:
        encoded = re.sub(r"\s+", "", data_match.group(2))
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            return None
        return _image_from_bytes(source_name, data, expected_media_type=data_match.group(1).lower())
    parsed = urlparse(source)
    if parsed.scheme == "https" and _is_public_image_url(parsed):
        return ImageInput(
            source_name=source_name,
            content_hash=f"sha256:{sha256(source.encode('utf-8')).hexdigest()}",
            url=source,
        )
    return None


def _image_from_bytes(
    name: str,
    data: bytes,
    *,
    expected_media_type: str | None = None,
) -> ImageInput:
    if not data:
        raise DocumentIngestionError(f"原型图片 {Path(name).name} 为空")
    if len(data) > MAX_IMAGE_BYTES:
        raise DocumentIngestionError(f"原型图片 {Path(name).name} 超过 5 MB 限制")
    media_type = _detect_image_type(data)
    suffix_type = ALLOWED_IMAGE_TYPES.get(Path(name).suffix.lower())
    if media_type is None or (expected_media_type and media_type != expected_media_type):
        raise DocumentIngestionError(f"原型图片 {Path(name).name} 不是有效的 PNG、JPEG、GIF 或 WebP")
    if suffix_type and suffix_type != media_type:
        raise DocumentIngestionError(f"原型图片 {Path(name).name} 的扩展名与内容不一致")
    return ImageInput(
        source_name=Path(name).name or "prototype-image",
        media_type=media_type,
        data=data,
        content_hash=f"sha256:{sha256(data).hexdigest()}",
    )


def _detect_image_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _is_public_image_url(parsed) -> bool:
    hostname = (parsed.hostname or "").lower()
    if not hostname or parsed.username or parsed.password or hostname == "localhost" or hostname.endswith(".local"):
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return True
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _merge_visual_inputs(
    current: Iterable[ImageInput],
    additional: Iterable[ImageInput],
) -> tuple[ImageInput, ...]:
    return _limit_visual_inputs([*current, *additional])


def _limit_visual_inputs(images: Iterable[ImageInput]) -> tuple[ImageInput, ...]:
    unique: list[ImageInput] = []
    seen: set[str] = set()
    total_bytes = 0
    for image in images:
        if image.content_hash in seen:
            continue
        if len(unique) >= MAX_IMAGE_COUNT:
            raise DocumentIngestionError(f"原型图片超过 {MAX_IMAGE_COUNT} 张限制")
        size = len(image.data) if image.data is not None else 0
        if total_bytes + size > MAX_TOTAL_IMAGE_BYTES:
            raise DocumentIngestionError("原型图片总大小超过 20 MB 限制")
        seen.add(image.content_hash)
        total_bytes += size
        unique.append(image)
    return tuple(unique)


class _HtmlTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self._ignored_depth += 1
        elif tag.lower() in {"p", "div", "br", "li", "h1", "h2", "h3", "section"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif tag.lower() in {"p", "div", "li", "h1", "h2", "h3", "section"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)

    def parse(self, content: str) -> str:
        self.feed(content)
        return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", "".join(self.parts))).strip()
