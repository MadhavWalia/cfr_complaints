"""Small but real PDFs for the procedure tests."""

from __future__ import annotations


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _page(lines: list[str], extra: str = "") -> bytes:
    text = "".join(f"BT /F1 10 Tf 50 {760 - 14 * i} Td ({_escape(line)}) Tj ET\n" for i, line in enumerate(lines))
    return (text + extra).encode("latin-1")


def _pdf(pages: list[bytes], *, with_image: bool = False) -> bytes:
    """A small but real PDF: page objects, one font, optionally one image XObject."""
    n = len(pages)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    font, image, bold = 3 + 2 * n, 4 + 2 * n, 5 + 2 * n
    xobject = f"/XObject << /Im0 {image} 0 R >>" if with_image else ""
    objects = {1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode()}
    for i, stream in enumerate(pages):
        page, content = 3 + 2 * i, 4 + 2 * i
        objects[page] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content} 0 R "
            f"/Resources << /Font << /F1 {font} 0 R /F2 {bold} 0 R >> {xobject} >> >>"
        ).encode()
        objects[content] = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream)
    objects[font] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    objects[bold] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>"
    if with_image:
        objects[image] = (
            b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray "
            b"/BitsPerComponent 8 /Length 1 >>\nstream\n\x00\nendstream"
        )
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += b"%d 0 obj\n%s\nendobj\n" % (number, objects[number])
    xref = len(out)
    size = max(objects) + 1
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for number in range(1, size):
        out += b"%010d 00000 n \n" % offsets.get(number, 0)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref)
    return bytes(out)


def text_at(x: int, y: int, text: str, *, font: str = "F1", size: int = 10) -> str:
    return f"BT /{font} {size} Tf {x} {y} Td ({_escape(text)}) Tj ET\n"


def grid(rows: list[list[str]], *, x: int = 50, top: int = 400, width: int = 150, height: int = 24) -> str:
    """A ruled table with text in its cells."""
    out = ""
    for r, row in enumerate(rows):
        y = top - height * (r + 1)
        for c, cell in enumerate(row):
            out += f"{x + width * c} {y} {width} {height} re S\n" + text_at(x + width * c + 4, y + 8, cell)
    return out
