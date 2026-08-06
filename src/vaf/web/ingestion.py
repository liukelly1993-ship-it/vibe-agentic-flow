"""Bounded ingestion for local PRD files and public Feishu documents."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from html.parser import HTMLParser
from io import BytesIO
import re
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import zipfile
import xml.etree.ElementTree as ET


MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
ALLOWED_EXTENSIONS = {".md", ".markdown", ".txt", ".pdf", ".html", ".htm", ".docx"}


class DocumentIngestionError(ValueError):
    """Raised when a source cannot be safely converted into text."""


@dataclass(frozen=True)
class IngestedDocument:
    name: str
    source_type: str
    source_ref: str
    text: str
    content_hash: str

    @property
    def character_count(self) -> int:
        return len(self.text)


def ingest_upload(filename: str, content: bytes) -> IngestedDocument:
    safe_name = Path(filename or "prd.md").name
    suffix = Path(safe_name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise DocumentIngestionError(
            f"不支持的文件格式：{suffix or '无扩展名'}；支持 Markdown、PDF、DOCX、HTML 和 TXT"
        )
    _check_size(content)
    text = _extract_text(suffix, content)
    return _make_document(safe_name, "upload", safe_name, text)


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
    return _make_document("feishu-document", "feishu", url, text)


def _make_document(name: str, source_type: str, source_ref: str, text: str) -> IngestedDocument:
    normalized = text.replace("\x00", "").strip()
    if not normalized:
        raise DocumentIngestionError("文档正文为空，无法启动研发流程")
    return IngestedDocument(
        name=name,
        source_type=source_type,
        source_ref=source_ref,
        text=normalized,
        content_hash=f"sha256:{sha256(normalized.encode('utf-8')).hexdigest()}",
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
