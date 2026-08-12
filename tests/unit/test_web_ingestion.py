import io
import unittest
import zipfile

from vaf.web.ingestion import DocumentIngestionError, ingest_feishu, ingest_upload
from vaf.web.stacks import choose_stack


class WebIngestionTests(unittest.TestCase):
    def test_markdown_upload_is_hashed(self) -> None:
        document = ingest_upload("需求.md", "# Task Board\n\nCreate tasks.".encode("utf-8"))
        self.assertEqual(document.source_type, "upload")
        self.assertTrue(document.content_hash.startswith("sha256:"))
        self.assertIn("Task Board", document.text)
        self.assertFalse(document.has_visual_evidence)

    def test_markdown_prototype_is_recorded_as_source_evidence(self) -> None:
        document = ingest_upload(
            "需求.md",
            "# Task Board\n\n![首页原型](prototype.png)\n\nCreate tasks.".encode("utf-8"),
        )
        self.assertTrue(document.has_visual_evidence)

    def test_unrelated_markdown_image_is_not_treated_as_prototype(self) -> None:
        document = ingest_upload(
            "需求.md",
            "# Task Board\n\n![company logo](logo.png)\n\nCreate tasks.".encode("utf-8"),
        )
        self.assertFalse(document.has_visual_evidence)

    def test_docx_upload_extracts_paragraph_text(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(
                "word/document.xml",
                """<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
                  <w:body><w:p><w:r><w:t>需求标题</w:t></w:r></w:p></w:body>
                </w:document>""",
            )
        document = ingest_upload("需求.docx", buffer.getvalue())
        self.assertEqual(document.text, "需求标题")
        self.assertFalse(document.has_visual_evidence)

    def test_docx_embedded_image_is_recorded_as_source_evidence(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(
                "word/document.xml",
                """<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
                  <w:body><w:p><w:r><w:t>页面原型</w:t></w:r></w:p></w:body>
                </w:document>""",
            )
            archive.writestr("word/media/prototype.png", b"not-a-real-image-for-fixture")
        document = ingest_upload("需求.docx", buffer.getvalue())
        self.assertTrue(document.has_visual_evidence)

    def test_feishu_import_rejects_non_feishu_host(self) -> None:
        with self.assertRaisesRegex(DocumentIngestionError, "只允许读取"):
            ingest_feishu("https://example.com/document")

    def test_stack_selection_is_explainable(self) -> None:
        choice = choose_stack("使用 Vue 和 PostgreSQL 构建多租户系统")
        self.assertEqual(choice.frontend, "Vue 3 + Vite")
        self.assertEqual(choice.database, "PostgreSQL")
        self.assertTrue(choice.reason)

    def test_mysql_is_selected_only_when_prd_requests_it(self) -> None:
        choice = choose_stack("后端 FastAPI，数据库明确使用 MySQL 8")
        self.assertEqual(choice.frontend, "Vue 3 + Vite")
        self.assertEqual(choice.backend, "FastAPI")
        self.assertEqual(choice.database, "MySQL")

    def test_stack_selection_surfaces_explicit_m0_fallbacks(self) -> None:
        choice = choose_stack("前端 Next.js，后端 Next.js API Routes，数据库 Prisma，AI 使用 OpenAI，部署 Vercel")
        self.assertTrue(any("Next.js" in warning for warning in choice.warnings))
        self.assertTrue(any("Prisma" in warning for warning in choice.warnings))
        self.assertTrue(any("OpenAI" in warning for warning in choice.warnings))
        self.assertTrue(any("Vercel" in warning for warning in choice.warnings))
