"""Builders of honest and hostile PDFs for the capped PDF reader's tests (issue #1202).

Every PDF is built from raw PDF syntax, so the bytes do not depend on a PDF writer, and
no PDF file is committed. The honest builders and the first hostile builders come from
the first attempt at issue #1202; the shapes after them reproduce the ones its review
found (each about 1 KB to 5 MB uploaded).
"""

import base64
import hashlib
import io
import struct
import zlib

import pdfplumber

_MIB = 1024 * 1024


def stream(dict_body: bytes, data: bytes) -> bytes:
    """An indirect object body holding a stream with the true /Length."""
    return b"<<" + dict_body + b" /Length %d>>\nstream\n" % len(data) + data + b"\nendstream"


def pdf(objects: list[bytes], *, trailer: bytes = b"", xref: bool = True) -> bytes:
    """A PDF of `objects`, numbered from 1, object 1 the catalog.

    With `xref` False the file has no cross-reference table and a startxref that points
    nowhere, so pdfminer recovers the objects by scanning the file.
    """
    out = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    table = len(out)
    if xref:
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
        for offset in offsets:
            out += b"%010d 00000 n \n" % offset
        out += b"trailer\n<< /Size %d /Root 1 0 R " % (len(objects) + 1) + trailer + b">>\n"
    else:
        out += b"trailer\n<< /Size %d /Root 1 0 R " % (len(objects) + 1) + trailer + b">>\n"
        table = 999_999_999
    out += b"startxref\n%d\n%%%%EOF\n" % table
    return bytes(out)


HELVETICA = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"


def pages_pdf(
    contents: list[bytes],
    *,
    font: bytes = HELVETICA,
    extra: tuple[bytes, ...] = (),
    resources: bytes = b"",
    trailer: bytes = b"",
    xref: bool = True,
) -> bytes:
    """A PDF with one page per entry of `contents` (each a stream object body).

    Objects: 1 catalog, 2 page tree, 3 the font, then for each page its content stream
    and the page, then `extra` (whose first object is number 4 + 2 * len(contents)).
    """
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"", font]
    kids = []
    for content in contents:
        objects.append(content)
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font "
            b"<< /F1 3 0 R >> " + resources + b" >> /Contents %d 0 R >>" % len(objects)
        )
        kids.append(len(objects))
    objects[1] = (
        b"<< /Type /Pages /Kids ["
        + b" ".join(b"%d 0 R" % kid for kid in kids)
        + b"] /Count %d >>" % len(kids)
    )
    objects.extend(extra)
    return pdf(objects, trailer=trailer, xref=xref)


def text_content(text: bytes) -> bytes:
    return b"BT /F1 12 Tf 72 720 Td (" + text + b") Tj ET"


def text_page(text: bytes, *, flate: bool = True) -> bytes:
    content = text_content(text)
    if flate:
        return stream(b"/Filter /FlateDecode", zlib.compress(content))
    return stream(b"", content)


def flate_repeated(chunk: bytes, count: int, *, head: bytes = b"", tail: bytes = b"") -> bytes:
    """zlib data of `head + chunk * count + tail`, compressed without ever holding it."""
    deflater = zlib.compressobj(9)
    parts = [deflater.compress(head)]
    block = chunk * max(1, _MIB // len(chunk))
    whole, rest = divmod(count, len(block) // len(chunk))
    for _ in range(whole):
        parts.append(deflater.compress(block))
    parts.append(deflater.compress(chunk * rest + tail))
    parts.append(deflater.flush())
    return b"".join(parts)


def bomb_page(inflated: int, *, filters: bytes = b"/FlateDecode") -> bytes:
    """A content stream of `inflated` spaces (to the content parser, nothing to do)."""
    return stream(b"/Filter " + filters, flate_repeated(b" ", inflated))


def lzw_codes(codes: list[int]) -> bytes:
    """Pack LZW codes MSB first at the widths pdfminer's LZWDecoder reads them.

    The width starts at 9 bits after a clear code and grows when the decoder's table
    reaches 511, 1023, and 2047 entries; the table gains one entry per code after the
    first one following a clear.
    """
    bits = 0
    nbits = 0
    out = bytearray()
    width = 9
    table = 258
    first = True
    for code in codes:
        bits = (bits << width) | code
        nbits += width
        while nbits >= 8:
            nbits -= 8
            out.append((bits >> nbits) & 0xFF)
        if code == 256:
            width, table, first = 9, 258, True
            continue
        if first:
            first = False
            continue
        table += 1
        if table == 511:
            width = 10
        elif table == 1023:
            width = 11
        elif table == 2047:
            width = 12
    if nbits:
        out.append((bits << (8 - nbits)) & 0xFF)
    return bytes(out)


def lzw_bomb(longest_repeats: int) -> bytes:
    """LZW codes that define ever longer runs of `a`, then repeat the longest one.

    After a clear, code 97 is `a` and each next code is the entry about to be defined,
    so entry 258 + k holds k + 2 bytes of `a`; the last definable entry, 4095, holds
    3,839 bytes, and each repeat of it is 12 bits of input.
    """
    codes = [256, 97] + list(range(258, 4096)) + [4095] * longest_repeats
    return lzw_codes(codes)


def rc4(key: bytes, data: bytes) -> bytes:
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) % 256
        state[i], state[j] = state[j], state[i]
    out = bytearray()
    i = j = 0
    for byte in data:
        i = (i + 1) % 256
        j = (j + state[i]) % 256
        state[i], state[j] = state[j], state[i]
        out.append(byte ^ state[(state[i] + state[j]) % 256])
    return bytes(out)


PASSWORD_PAD = bytes.fromhex("28bf4e5e4e758a4164004e56fffa01082e2e00b6d0683e802f0ca9fe6453697a")


def encrypted_pdf(contents: list[bytes]) -> bytes:
    """A PDF encrypted with the standard security handler, revision 2 (40-bit RC4),
    with an empty user password, so pdfminer opens it and decrypts every stream.

    `contents` are the raw (unencrypted) bodies of the page content streams; each is
    a stream whose data this function encrypts with its object's key.
    """
    owner = hashlib.md5(b"owner").digest() * 2
    permissions = -4
    file_id = b"0123456789abcdef"
    key = hashlib.md5(PASSWORD_PAD + owner + struct.pack("<i", permissions) + file_id).digest()[:5]
    user = rc4(key, PASSWORD_PAD)

    def encrypt(number: int, body: bytes) -> bytes:
        head, _, rest = body.partition(b"stream\n")
        data, _, _ = rest.rpartition(b"\nendstream")
        object_key = hashlib.md5(key + struct.pack("<i", number)[:3] + b"\0\0").digest()[:10]
        sealed = rc4(object_key, data)
        head = head.replace(b"/Length %d>>" % len(data), b"/Length %d>>" % len(sealed))
        return head + b"stream\n" + sealed + b"\nendstream"

    count = len(contents)
    encrypt_number = 4 + 2 * count
    encrypt_dict = (
        b"<< /Filter /Standard /V 1 /R 2 /O <"
        + owner.hex().encode()
        + b"> /U <"
        + user.hex().encode()
        + b"> /P %d >>" % permissions
    )
    sealed = [encrypt(4 + 2 * i, body) for i, body in enumerate(contents)]
    return pages_pdf(
        sealed,
        extra=(encrypt_dict,),
        trailer=b"/Encrypt %d 0 R /ID [<%s> <%s>] "
        % (encrypt_number, file_id.hex().encode(), file_id.hex().encode()),
    )


def lzw_encode(data: bytes) -> bytes:
    """Plain LZW of `data` (short enough that the table never fills), clear code first
    and end-of-data code last."""
    table = {bytes([i]): i for i in range(256)}
    next_code = 258
    codes = [256]
    word = b""
    for byte in data:
        grown = word + bytes([byte])
        if grown in table:
            word = grown
            continue
        codes.append(table[word])
        table[grown] = next_code
        next_code += 1
        word = bytes([byte])
    if word:
        codes.append(table[word])
    codes.append(257)
    assert next_code < 4096
    return lzw_codes(codes)


def runlength_encode(data: bytes) -> bytes:
    out = bytearray()
    for i in range(0, len(data), 128):
        chunk = data[i : i + 128]
        out += bytes([len(chunk) - 1]) + chunk
    return bytes(out) + b"\x80"


def png_rows(data: bytes, columns: int) -> bytes:
    """`data` (a multiple of `columns` long) as PNG predictor rows, cycling the None,
    Sub, Up, Average, and Paeth row filters, one byte a pixel."""
    rows = [data[i : i + columns] for i in range(0, len(data), columns)]
    out = bytearray()
    above = bytes(columns)
    for n, row in enumerate(rows):
        kind = n % 5
        encoded = bytearray()
        for j, x in enumerate(row):
            left = row[j - 1] if j else 0
            up = above[j]
            upper_left = above[j - 1] if j else 0
            if kind == 0:
                predicted = 0
            elif kind == 1:
                predicted = left
            elif kind == 2:
                predicted = up
            elif kind == 3:
                predicted = (left + up) // 2
            else:
                p = left + up - upper_left
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - upper_left)
                predicted = left if pa <= pb and pa <= pc else up if pb <= pc else upper_left
            encoded.append((x - predicted) & 255)
        out += bytes([kind]) + encoded
        above = row
    return bytes(out)


def tiff_rows(data: bytes, columns: int) -> bytes:
    out = bytearray()
    for i in range(0, len(data), columns):
        row = data[i : i + columns]
        out += bytes([row[0]]) + bytes((row[j] - row[j - 1]) & 255 for j in range(1, len(row)))
    return bytes(out)


def padded_text(text: bytes, multiple: int) -> bytes:
    content = text_content(text)
    return content + b" " * (-len(content) % multiple)


def flat_pages_pdf(count: int, *, first: bytes | None = None) -> bytes:
    """`count` pages sharing one empty content stream; with `first`, page 1 has that
    content stream instead."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"", HELVETICA, stream(b"", b" ")]
    first_ref = 4
    if first is not None:
        objects.append(first)
        first_ref = 5
    base = len(objects) + 1
    objects[1] = (
        b"<< /Type /Pages /Kids ["
        + b" ".join(b"%d 0 R" % (base + i) for i in range(count))
        + b"] /Count %d >>" % count
    )
    for i in range(count):
        contents = first_ref if i == 0 else 4
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
            b"<< /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % contents
        )
    return pdf(objects)


def xref_stream_pdf(objects: list[bytes], in_objstm: set[int]) -> bytes:
    """A PDF 1.5 file: the objects numbered in `in_objstm` sit in one object stream,
    and a cross-reference stream (PNG Up predictor, five columns, Flate) indexes
    everything, as pdfTeX and most current writers produce."""
    out = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    entries: dict[int, tuple[int, int, int]] = {}
    packed_numbers = sorted(in_objstm)
    objstm_number = len(objects) + 1
    xref_number = len(objects) + 2
    for number, body in enumerate(objects, start=1):
        if number in in_objstm:
            continue
        entries[number] = (1, len(out), 0)
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    header = bytearray()
    payload = bytearray()
    for index, number in enumerate(packed_numbers):
        header += b"%d %d " % (number, len(payload))
        payload += objects[number - 1] + b"\n"
        entries[number] = (2, objstm_number, index)
    entries[objstm_number] = (1, len(out), 0)
    out += (
        b"%d 0 obj\n" % objstm_number
        + stream(
            b"/Type /ObjStm /N %d /First %d /Filter /FlateDecode"
            % (len(packed_numbers), len(header)),
            zlib.compress(bytes(header + payload)),
        )
        + b"\nendobj\n"
    )
    entries[xref_number] = (1, len(out), 0)
    rows = bytearray()
    above = bytes(5)
    for number in range(xref_number + 1):
        kind, field2, field3 = entries.get(number, (0, 0, 65535 if number == 0 else 0))
        row = bytes([kind]) + struct.pack(">I", field2)[1:] + struct.pack(">B", field3 & 0xFF)
        rows += b"\x02" + bytes((x - a) & 255 for x, a in zip(row, above, strict=True))
        above = row
    out += (
        b"%d 0 obj\n" % xref_number
        + stream(
            b"/Type /XRef /Size %d /W [1 3 1] /Root 1 0 R /Filter /FlateDecode "
            b"/DecodeParms << /Predictor 12 /Columns 5 >>" % (xref_number + 1),
            zlib.compress(bytes(rows)),
        )
        + b"\nendobj\n"
    )
    out += b"startxref\n%d\n%%%%EOF\n" % entries[xref_number][1]
    return bytes(out)


def xref_stream_text_pdf(text: bytes) -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [5 0 R] /Count 1 >>",
        HELVETICA,
        text_page(text),
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
        b"<< /Font << /F1 3 0 R >> >> /Contents 4 0 R >>",
    ]
    return xref_stream_pdf(objects, {1, 2, 3, 5})


def tounicode_font(cmap_body: bytes) -> tuple[bytes, bytes]:
    """A font (object 3) whose /ToUnicode is the first extra object of a one-page PDF."""
    cmap = (
        b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap "
        b"1 begincodespacerange <00> <FF> endcodespacerange " + cmap_body + b" endcmap end end"
    )
    font = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /ToUnicode 6 0 R >>"
    return font, stream(b"", cmap)


def image_xobject(data: bytes, filters: bytes = b"/FlateDecode") -> bytes:
    return stream(
        b"/Type /XObject /Subtype /Image /Width 8 /Height 8 /ColorSpace /DeviceGray "
        b"/BitsPerComponent 8 /Filter " + filters,
        data,
    )


def form_xobject(content: bytes) -> bytes:
    return stream(
        b"/Type /XObject /Subtype /Form /BBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >>",
        content,
    )


def plain_read(data: bytes) -> str:
    """What the reader returned before issue #1202: pdfplumber's own walk of every page,
    the texts of the pages with text joined by a blank line."""
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return "\n\n".join(t for page in pdf.pages if (t := page.extract_text() or ""))


# --- Honest PDFs ---


def honest_pdfs() -> dict[str, bytes]:
    hello = text_content(b"Hello from a filtered stream")
    padded16 = padded_text(b"Predicted rows", 16)
    a85 = base64.a85encode(hello) + b"~>"
    image = image_xobject(zlib.compress(bytes(64)))
    form = form_xobject(text_content(b"Inside a form"))
    small_cmap_font, small_cmap = tounicode_font(b"1 beginbfchar <48> <0058> endbfchar")
    return {
        "three-flate-pages": pages_pdf(
            [text_page(b"Page one"), text_page(b"Page two"), text_page(b"Page three")]
        ),
        "uncompressed": pages_pdf([text_page(b"Plain stream", flate=False)]),
        "empty-page-between": pages_pdf(
            [text_page(b"Before"), stream(b"", b" "), text_page(b"After")]
        ),
        "rc4-encrypted": encrypted_pdf([text_page(b"Encrypted words")]),
        "no-xref-table": pages_pdf([text_page(b"Recovered by scanning")], xref=False),
        "xref-and-object-streams": xref_stream_text_pdf(b"Packed objects"),
        "flate-twice": pages_pdf(
            [stream(b"/Filter [/FlateDecode /FlateDecode]", zlib.compress(zlib.compress(hello)))]
        ),
        "lzw": pages_pdf([stream(b"/Filter /LZWDecode", lzw_encode(hello))]),
        "flate-ascii85": pages_pdf(
            [stream(b"/Filter [/FlateDecode /ASCII85Decode]", zlib.compress(a85))]
        ),
        "asciihex": pages_pdf([stream(b"/Filter /ASCIIHexDecode", hello.hex().encode() + b">")]),
        "runlength": pages_pdf([stream(b"/Filter /RunLengthDecode", runlength_encode(hello))]),
        "png-predictor": pages_pdf(
            [
                stream(
                    b"/Filter /FlateDecode /DecodeParms << /Predictor 15 /Columns 16 >>",
                    zlib.compress(png_rows(padded16, 16)),
                )
            ]
        ),
        "tiff-predictor": pages_pdf(
            [
                stream(
                    b"/Filter /FlateDecode /DecodeParms << /Predictor 2 /Columns 16 >>",
                    zlib.compress(tiff_rows(padded16, 16)),
                )
            ]
        ),
        "truncated-flate": pages_pdf([stream(b"/Filter /FlateDecode", zlib.compress(hello)[:-4])]),
        "image-and-form": pages_pdf(
            [stream(b"", b"q 10 0 0 10 0 0 cm /Im Do Q /Fm Do BT /F1 9 Tf 9 9 Td (Page) Tj ET")],
            resources=b"/XObject << /Im 6 0 R /Fm 7 0 R >>",
            extra=(image, form),
        ),
        "tounicode-map": pages_pdf([text_page(b"HHH")], font=small_cmap_font, extra=(small_cmap,)),
        "non-ascii-text": pages_pdf(
            [stream(b"", b"BT /F1 12 Tf 72 720 Td (caf\xe9 na\xefve \xa9 \xb5) Tj ET")]
        ),
    }


# --- Hostile PDFs ---


def type0_font(descendant_extra: bytes) -> tuple[bytes, bytes]:
    """A Type0 font (object 3) and its CIDFontType2 descendant (object 6, the first
    extra object of a one-page PDF)."""
    font = (
        b"<< /Type /Font /Subtype /Type0 /BaseFont /X /Encoding /Identity-H "
        b"/DescendantFonts [6 0 R] >>"
    )
    descendant = (
        b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /X /CIDSystemInfo "
        b"<< /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> " + descendant_extra + b" >>"
    )
    return font, descendant


_CID_TEXT = stream(b"/Filter /FlateDecode", zlib.compress(b"BT /F1 12 Tf 72 720 Td <0001> Tj ET"))


def truetype_cmap_font(segments: int) -> bytes:
    """A TrueType font whose format 4 cmap has `segments` segments that each cover the
    whole code range (start 0, end 0xFFFF): pdfminer walks every code of every segment,
    about 2e9 steps for 32,767 segments."""
    cmap = bytearray(struct.pack(">HH", 0, 1) + struct.pack(">HHL", 3, 1, 12))
    sub = struct.pack(">HHH", 4, 0, 0) + struct.pack(">HHHH", segments * 2, 0, 0, 0)
    sub += struct.pack(f">{segments}H", *([0xFFFF] * segments)) + b"\0\0"
    sub += struct.pack(f">{segments}H", *([0] * segments)) * 3
    cmap += sub
    head = struct.pack(">4sHHHH", b"\0\1\0\0", 1, 0, 0, 0)
    table = struct.pack(">4sLLL", b"cmap", 0, 28, len(cmap))
    return head + table + bytes(cmap)


def truetype_cmap_pdf(segments: int) -> bytes:
    font, descendant = type0_font(b"/FontDescriptor 7 0 R")
    descriptor = b"<< /Type /FontDescriptor /FontName /X /Flags 4 /FontFile2 8 0 R >>"
    fontfile = stream(b"/Filter /FlateDecode", zlib.compress(truetype_cmap_font(segments), 9))
    return pages_pdf([_CID_TEXT], font=font, extra=(descendant, descriptor, fontfile))


def cid_widths_pdf(last_cid: int) -> bytes:
    """A CID font whose /W gives one width to the range 0 to `last_cid`: pdfminer
    builds a dict entry per code (about 3 GB for 40 million) from an 833-byte file."""
    font, descendant = type0_font(b"/W [0 %d 500]" % last_cid)
    return pages_pdf([_CID_TEXT], font=font, extra=(descendant,))


def predictor_row_pdf(predictor: int, row_bytes: int, first: bytes = b"") -> bytes:
    """One predictor row of `row_bytes` bytes, set through /Colors with one column."""
    return pages_pdf(
        [
            stream(
                b"/Filter /FlateDecode /DecodeParms << /Predictor %d /Colors %d /Columns 1 >>"
                % (predictor, row_bytes),
                flate_repeated(b"\0", row_bytes, head=first),
            )
        ]
    )


def xref_index_pdf(count: int) -> bytes:
    """A cross-reference stream whose /Index names `count` objects and holds none."""
    body = bytearray(b"%PDF-1.5\n")
    body += b"1 0 obj\n<< /Type /Catalog >>\nendobj\n"
    offset = len(body)
    body += (
        b"2 0 obj\n<< /Type /XRef /Size %d /W [1 4 1] /Index [0 %d] /Root 1 0 R /Length 0 >>"
        b"\nstream\n\nendstream\nendobj\n" % (count, count)
    )
    body += b"startxref\n%d\n%%%%EOF\n" % offset
    return bytes(body)


def page_tree_pdf(depth: int) -> bytes:
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    for i in range(depth):
        objects.append(b"<< /Type /Pages /Kids [%d 0 R] /Count 1 >>" % (i + 3))
    objects.append(b"<< /Type /Page /MediaBox [0 0 1 1] >>")
    return pdf(objects)


def fresh_names_pdf(count: int) -> bytes:
    """A document info dict of `count` distinct names, Flate-packed in an object stream
    (about 1.2 MB for 450,000): pdfminer keeps every name it meets for good."""
    names = b" ".join(b"/n%d 1" % i for i in range(count))
    objstm = stream(
        b"/Type /ObjStm /N 1 /First 5 /Filter /FlateDecode",
        zlib.compress(b"50 0 << /Keywords << " + names + b" >> >>", 9),
    )
    return pages_pdf([text_page(b"Names")], extra=(objstm,), trailer=b"/Info 50 0 R ", xref=False)


def object_stream_numbers_pdf(count: int) -> bytes:
    objstm = stream(
        b"/Type /ObjStm /N 1 /First 5 /Filter /FlateDecode",
        flate_repeated(b"1 ", count, head=b"50 0 << /Title [", tail=b"] >>"),
    )
    return pages_pdf([text_page(b"Hi")], extra=(objstm,), xref=False)


def form_drawn_pdf(times: int) -> bytes:
    form = form_xobject(text_content(b"Inside a form"))
    content = stream(b"/Filter /FlateDecode", zlib.compress(b"/Fm Do " * times))
    return pages_pdf([content], resources=b"/XObject << /Fm 6 0 R >>", extra=(form,))


def content_pdf(chunk: bytes, count: int) -> bytes:
    """One page whose Flate content stream is `chunk` repeated `count` times."""
    return pages_pdf([stream(b"/Filter /FlateDecode", flate_repeated(chunk, count))])


def hostile_pdfs():
    """Builders, not bytes, so a parametrized test builds only its own shape."""
    bomb = 200 * _MIB
    return {
        "the-issues-200-mib-flate": lambda: pages_pdf([bomb_page(bomb)]),
        "flate-inside-flate": lambda: pages_pdf(
            [
                stream(
                    b"/Filter [/FlateDecode /FlateDecode]",
                    zlib.compress(flate_repeated(b" ", bomb), 9),
                )
            ]
        ),
        "lzw-runs": lambda: pages_pdf([stream(b"/Filter /LZWDecode", lzw_bomb(200_000))]),
        "ascii85-z-behind-flate": lambda: pages_pdf(
            [stream(b"/Filter [/FlateDecode /ASCII85Decode]", flate_repeated(b"z", 60 * _MIB))]
        ),
        "runlength-behind-flate": lambda: pages_pdf(
            [stream(b"/Filter [/FlateDecode /RunLengthDecode]", flate_repeated(b"\x81 ", 4 * _MIB))]
        ),
        "png-predictor-huge-columns": lambda: pages_pdf(
            [
                stream(
                    b"/Filter /FlateDecode /DecodeParms << /Predictor 12 /Columns 1000000000 >>",
                    zlib.compress(bytes(10)),
                )
            ]
        ),
        "png-predictor-one-huge-row-via-colors": lambda: predictor_row_pdf(11, 30_000_000, b"\x01"),
        "tiff-predictor-row": lambda: predictor_row_pdf(2, 30_000_000),
        "ccitt-huge-columns": lambda: pages_pdf(
            [
                stream(
                    b"/Filter /CCITTFaxDecode /DecodeParms << /K -1 /Columns 33000000 >>",
                    b"\x00" * 64,
                )
            ]
        ),
        "truncated-flate-bomb": lambda: pages_pdf(
            [stream(b"/Filter /FlateDecode", flate_repeated(b" ", bomb)[:-8])]
        ),
        "tounicode-range-of-4e9": lambda: pages_pdf(
            [text_page(b"Mapped")],
            font=tounicode_font(b"1 beginbfrange <00000000> <FFFFFFFF> <0041> endbfrange")[0],
            extra=(tounicode_font(b"1 beginbfrange <00000000> <FFFFFFFF> <0041> endbfrange")[1],),
        ),
        "two-million-q": lambda: content_pdf(b"q ", 2_000_000),
        "200k-rectangles": lambda: content_pdf(b"0 0 1 1 re f ", 200_000),
        "form-drawn-10000-times": lambda: form_drawn_pdf(10_000),
        "object-stream-of-15m-numbers": lambda: object_stream_numbers_pdf(15 * _MIB),
        "450k-fresh-names": lambda: fresh_names_pdf(450_000),
        "cid-widths-range-40m": lambda: cid_widths_pdf(40_000_000),
        "truetype-cmap-32767-full-segments": lambda: truetype_cmap_pdf(32_767),
        "rc4-stream-of-4.9-mb": lambda: encrypted_pdf([stream(b"", b" " * 4_900_000)]),
        "open-bracket-4.19m-in-content": lambda: content_pdf(b"[", 4_190_000),
        "open-dict-2.09m-in-content": lambda: content_pdf(b"<<", 2_090_000),
        "page-tree-5000-deep": lambda: page_tree_pdf(5_000),
        "xref-stream-index-2e9": lambda: xref_index_pdf(2_000_000_000),
    }
