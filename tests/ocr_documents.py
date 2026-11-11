"""Minimal, structurally valid documents for the admission/input-boundary tests.

Dev/test only. Each builder emits the smallest byte sequence the input boundary
(issue #10) accepts for its container: a real decoder would reject these as
empty images/documents, which is why the engine is always faked here. Built by
hand rather than checked in as binaries so the tests carry no copyrighted or
sensitive document content.
"""

import struct
import zlib

PDF_HEADER = b"%PDF-1.7\n%%EOF\n"


def pdf_bytes(pages: int = 1, *, trailer: bool = True, encrypted: bool = False) -> bytes:
    """A PDF whose page objects, trailer, and encryption entry are settable."""
    body = b"%PDF-1.7\n"
    for number in range(1, pages + 1):
        body += f"{number} 0 obj\n<< /Type /Page /Parent 1 0 R >>\nendobj\n".encode()
    if encrypted:
        body += b"<< /Encrypt 9 0 R >>\n"
    if trailer:
        body += b"trailer\n<< /Size 3 >>\n"
    return body + b"%%EOF\n"


def png_bytes(width: int = 1, height: int = 1, *, complete: bool = True) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    body = b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr)
    if complete:
        body += _png_chunk(b"IEND", b"")
    return body


def jpeg_bytes(width: int = 1, height: int = 1, *, complete: bool = True) -> bytes:
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, height, width, 3)
    body = b"\xff\xd8" + b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + sof
    body += b"\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    return body + (b"\xff\xd9" if complete else b"")


# One IFD is 2 + 2 x 12 + 4 bytes; the first one starts right after the header.
_TIFF_HEADER = 8
_TIFF_IFD_BYTES = 30


def tiff_bytes(
    frames: int = 1, width: int = 1, height: int = 1, *, complete: bool = True
) -> bytes:
    blocks = []
    for index in range(frames):
        following = index + 1
        next_offset = _TIFF_HEADER + following * _TIFF_IFD_BYTES if following < frames else 0
        block = struct.pack("<H", 2)
        block += struct.pack("<HHI", 256, 3, 1) + struct.pack("<HH", width, 0)
        block += struct.pack("<HHI", 257, 3, 1) + struct.pack("<HH", height, 0)
        blocks.append(block + struct.pack("<I", next_offset))
    body = b"II*\x00" + struct.pack("<I", _TIFF_HEADER) + b"".join(blocks)
    return body if complete else body[:_TIFF_HEADER]


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    chunk = kind + payload
    return struct.pack(">I", len(payload)) + chunk + struct.pack(">I", zlib.crc32(chunk))
