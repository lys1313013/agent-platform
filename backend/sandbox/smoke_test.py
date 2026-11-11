"""Exercise installed document dependencies inside the read-only sandbox image."""

import io
import subprocess
import tempfile
from pathlib import Path

import pdfplumber
import pypdfium2
from docx import Document
from openpyxl import Workbook, load_workbook
from PIL import Image
from pptx import Presentation
from pptx.util import Inches


def main() -> None:
    with tempfile.TemporaryDirectory(dir="/workspace") as temporary:
        root = Path(temporary)
        image = Image.new("RGB", (64, 48), "white")
        for kind in ("PNG", "JPEG", "TIFF"):
            stream = io.BytesIO()
            image.save(stream, format=kind)
            stream.seek(0)
            with Image.open(stream) as loaded:
                loaded.load()
                assert loaded.size == image.size

        pdf = root / "sample.pdf"
        image.save(pdf, format="PDF")
        with pdfplumber.open(pdf) as document:
            assert len(document.pages) == 1
        with pypdfium2.PdfDocument(pdf) as document:
            page = document[0]
            bitmap = page.render(scale=1)
            assert bitmap.to_pil().size == image.size
            bitmap.close()
            page.close()

        word = root / "sample.docx"
        document = Document()
        document.add_paragraph("sandbox smoke")
        document.save(word)
        assert Document(word).paragraphs[0].text == "sandbox smoke"

        excel = root / "sample.xlsx"
        workbook = Workbook()
        workbook.active["A1"] = "sandbox smoke"
        workbook.save(excel)
        loaded_workbook = load_workbook(excel, read_only=True)
        assert loaded_workbook.active["A1"].value == "sandbox smoke"
        loaded_workbook.close()

        slides = root / "sample.pptx"
        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1)).text = "sandbox smoke"
        presentation.save(slides)
        assert Presentation(slides).slides[0].shapes[0].text == "sandbox smoke"

    for command in (["node", "--version"], ["npm", "--version"], ["npx", "--version"]):
        subprocess.run(command, check=True)
    print("Sandbox image smoke passed: images, PDF parsing/rendering, DOCX, XLSX, PPTX, Node/npm/npx")


if __name__ == "__main__":
    main()
