import unittest
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from app.core.errors import AppError
from app.services.attachments import DOCX, XLSX, MAX_FILE_BYTES, content_block


def archive(files):
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
    return stream.getvalue()


class AttachmentTests(unittest.TestCase):
    def test_docx(self):
        data = archive({"word/document.xml": '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>ART-101</w:t></w:r></w:p></w:body></w:document>'})
        self.assertIn("ART-101", content_block(data, DOCX)["text"])

    def test_xlsx(self):
        data = archive({"xl/worksheets/sheet1.xml": '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row><c r="A1" t="inlineStr"><is><t>ART-101</t></is></c><c r="B1"><v>2</v></c></row></sheetData></worksheet>'})
        text = content_block(data, XLSX)["text"]
        self.assertIn("A1: ART-101", text)
        self.assertIn("B1: 2", text)

    def test_binary_blocks(self):
        self.assertEqual(content_block(b"%PDF-1.7\ntest", "application/pdf")["type"], "input_file")
        self.assertEqual(content_block(b"\xff\xd8\xfftest", "image/jpeg")["type"], "input_image")

    def test_reject_invalid_and_oversized(self):
        for data, media, status in [(b"", DOCX, 422), (b"garbage", DOCX, 422),
                                    (b"garbage", "image/jpeg", 422), (b"x", "text/html", 415),
                                    (b"x" * (MAX_FILE_BYTES + 1), "application/pdf", 413),
                                    (archive({"word/document.xml": '<!DOCTYPE x [<!ENTITY secret "x">]><x>&secret;</x>'}), DOCX, 422),
                                    (archive({"word/document.xml": "x" * (21 * 1024 * 1024)}), DOCX, 422)]:
            with self.subTest(media=media, size=len(data)):
                with self.assertRaises(AppError) as error:
                    content_block(data, media)
                self.assertEqual(error.exception.status, status)
