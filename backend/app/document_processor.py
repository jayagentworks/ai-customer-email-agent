import re
import shutil
import subprocess
from tempfile import TemporaryDirectory
from dataclasses import dataclass
from pathlib import Path


SUPPORTED_KNOWLEDGE_SUFFIXES = {".md", ".txt", ".pdf", ".docx", ".doc"}


@dataclass
class ParsedChunk:
    content: str
    page_number: int | None = None
    section_title: str = ""


@dataclass
class CleanResult:
    text: str
    original_chars: int
    cleaned_chars: int
    noise_lines_removed: int


@dataclass
class ParseReport:
    file_type: str
    parser: str
    original_chars: int = 0
    cleaned_chars: int = 0
    noise_lines_removed: int = 0
    page_count: int | None = None
    pages_with_text: int | None = None
    section_count: int = 0
    table_count: int = 0
    warnings: list[str] | None = None

    def to_dict(self) -> dict:
        return {
            "file_type": self.file_type,
            "parser": self.parser,
            "original_chars": self.original_chars,
            "cleaned_chars": self.cleaned_chars,
            "noise_lines_removed": self.noise_lines_removed,
            "page_count": self.page_count,
            "pages_with_text": self.pages_with_text,
            "section_count": self.section_count,
            "table_count": self.table_count,
            "warnings": self.warnings or [],
        }


@dataclass
class ParsedDocument:
    title: str
    chunks: list[ParsedChunk]
    report: ParseReport


def parse_document(path: Path) -> ParsedDocument:
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_KNOWLEDGE_SUFFIXES:
        raise ValueError("Only .md, .txt, .pdf, .docx and .doc knowledge files are supported.")
    if suffix in {".md", ".txt"}:
        return parse_text_document(path)
    if suffix == ".pdf":
        return parse_pdf_document(path)
    if suffix == ".doc":
        return parse_doc_document(path)
    return parse_docx_document(path)


def parse_text_document(path: Path) -> ParsedDocument:
    raw_text = path.read_text(encoding="utf-8-sig")
    clean = clean_text_with_report(raw_text)
    title = extract_title(clean.text, path.stem)
    chunks = split_markdown_like_text(clean.text)
    report = ParseReport(
        file_type=path.suffix.lower().lstrip("."),
        parser="plain-text",
        original_chars=clean.original_chars,
        cleaned_chars=clean.cleaned_chars,
        noise_lines_removed=clean.noise_lines_removed,
        section_count=count_sections(chunks),
    )
    return ParsedDocument(title=title, chunks=chunks, report=report)


def parse_pdf_document(path: Path) -> ParsedDocument:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError("PDF parsing dependency is missing. Please install pypdf.") from exc

    reader = PdfReader(str(path))
    page_chunks: list[ParsedChunk] = []
    first_text = ""
    total_original_chars = 0
    total_cleaned_chars = 0
    total_noise_lines = 0
    pages_with_text = 0
    pages_with_ocr = 0
    warnings: list[str] = []

    for page_index, page in enumerate(reader.pages, start=1):
        raw_text = page.extract_text() or ""
        text_source = "text-layer"
        if not raw_text.strip():
            raw_text = ocr_pdf_page(path, page_index)
            text_source = "ocr"
            if raw_text.strip():
                pages_with_ocr += 1
            else:
                warnings.append(f"Page {page_index} had no extractable text and OCR returned no text.")

        total_original_chars += len(raw_text)
        clean = clean_text_with_report(raw_text)
        total_cleaned_chars += clean.cleaned_chars
        total_noise_lines += clean.noise_lines_removed
        if not clean.text:
            continue
        pages_with_text += 1
        first_text = first_text or clean.text
        for chunk in split_markdown_like_text(clean.text, page_number=page_index):
            page_chunks.append(chunk)
        if text_source == "ocr":
            warnings.append(f"Page {page_index} was parsed by local OCR.")

    if not page_chunks:
        raise ValueError("No extractable text found in this PDF. Local OCR also returned no reliable text.")

    if pages_with_text < len(reader.pages):
        warnings.append(f"{len(reader.pages) - pages_with_text} page(s) had no extractable text.")
    report = ParseReport(
        file_type="pdf",
        parser="pypdf + tesseract-ocr" if pages_with_ocr else "pypdf",
        original_chars=total_original_chars,
        cleaned_chars=total_cleaned_chars,
        noise_lines_removed=total_noise_lines,
        page_count=len(reader.pages),
        pages_with_text=pages_with_text,
        section_count=count_sections(page_chunks),
        warnings=warnings,
    )
    return ParsedDocument(title=extract_title(first_text, path.stem), chunks=page_chunks, report=report)


def ocr_pdf_page(path: Path, page_number: int) -> str:
    pdftoppm = find_executable("pdftoppm")
    tesseract = find_executable("tesseract")
    if not pdftoppm or not tesseract:
        return ""

    with TemporaryDirectory(prefix="pdf-ocr-") as tmp:
        output_prefix = Path(tmp) / f"page-{page_number}"
        render_command = [
            str(pdftoppm),
            "-f",
            str(page_number),
            "-l",
            str(page_number),
            "-r",
            "220",
            "-png",
            str(path),
            str(output_prefix),
        ]
        try:
            render_result = subprocess.run(render_command, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if render_result.returncode != 0:
            return ""

        rendered_pages = sorted(Path(tmp).glob(f"{output_prefix.name}-*.png"))
        if not rendered_pages:
            rendered_pages = sorted(Path(tmp).glob("*.png"))
        if not rendered_pages:
            return ""

        ocr_texts: list[str] = []
        for image_path in rendered_pages:
            ocr_command = [
                str(tesseract),
                str(image_path),
                "stdout",
                "-l",
                "chi_sim+eng",
                "--psm",
                "6",
            ]
            try:
                ocr_result = subprocess.run(ocr_command, capture_output=True, text=True, timeout=90)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if ocr_result.returncode == 0 and ocr_result.stdout.strip():
                ocr_texts.append(ocr_result.stdout.strip())
        return "\n\n".join(ocr_texts).strip()


def parse_docx_document(path: Path) -> ParsedDocument:
    try:
        from docx import Document
    except ImportError as exc:
        raise ValueError("DOCX parsing dependency is missing. Please install python-docx.") from exc

    document = Document(str(path))
    blocks: list[str] = []
    current_section = ""
    raw_chars = 0

    for paragraph in document.paragraphs:
        raw_chars += len(paragraph.text or "")
        text = clean_inline_text(paragraph.text)
        if not text:
            continue
        style_name = (paragraph.style.name or "").lower() if paragraph.style else ""
        if style_name.startswith("heading"):
            current_section = text
            blocks.append(f"## {text}")
        elif current_section:
            blocks.append(text)
        else:
            blocks.append(text)

    for table in document.tables:
        table_text = table_to_text(table)
        raw_chars += len(table_text)
        if table_text:
            blocks.append(table_text)

    clean = clean_text_with_report("\n\n".join(blocks))
    if not clean.text:
        raise ValueError("No extractable text found in this DOCX.")
    chunks = split_markdown_like_text(clean.text)
    report = ParseReport(
        file_type="docx",
        parser="python-docx",
        original_chars=raw_chars,
        cleaned_chars=clean.cleaned_chars,
        noise_lines_removed=clean.noise_lines_removed,
        section_count=count_sections(chunks),
        table_count=len(document.tables),
    )
    return ParsedDocument(title=extract_title(clean.text, path.stem), chunks=chunks, report=report)


def parse_doc_document(path: Path) -> ParsedDocument:
    conversion_error = ""
    try:
        converted_path = convert_doc_to_docx(path)
        try:
            parsed = parse_docx_document(converted_path)
            parsed.report.file_type = "doc"
            parsed.report.parser = "LibreOffice -> python-docx"
            parsed.report.warnings = [*(parsed.report.warnings or []), "Converted from legacy .doc to .docx before parsing."]
            return parsed
        finally:
            converted_path.unlink(missing_ok=True)
    except ValueError as exc:
        conversion_error = str(exc)

    try:
        import olefile
    except ImportError as exc:
        raise ValueError("DOC parsing dependency is missing. Please install olefile.") from exc

    if not olefile.isOleFile(str(path)):
        raise ValueError(f"{conversion_error} Fallback failed: this .doc file is not a valid legacy Word OLE document.")

    text_parts: list[str] = []
    with olefile.OleFileIO(str(path)) as ole:
        stream_names = ["/".join(item) for item in ole.listdir(streams=True)]
        preferred_streams = [name for name in stream_names if name in {"WordDocument", "1Table", "0Table"}]
        candidate_streams = preferred_streams or stream_names

        for stream_name in candidate_streams:
            try:
                data = ole.openstream(stream_name).read()
            except OSError:
                continue
            text_parts.extend(extract_readable_strings(data))

    clean = clean_text_with_report("\n\n".join(text_parts))
    if len(clean.text) < 20:
        raise ValueError(
            f"{conversion_error} Fallback failed: no reliable text could be extracted from this .doc file. "
            "Please convert it to .docx or PDF and upload again."
        )
    chunks = split_markdown_like_text(clean.text)
    report = ParseReport(
        file_type="doc",
        parser="olefile fallback",
        original_chars=sum(len(part) for part in text_parts),
        cleaned_chars=clean.cleaned_chars,
        noise_lines_removed=clean.noise_lines_removed,
        section_count=count_sections(chunks),
        warnings=[conversion_error, "Used OLE text extraction fallback; formatting may be incomplete."],
    )
    return ParsedDocument(title=extract_title(clean.text, path.stem), chunks=chunks, report=report)


def convert_doc_to_docx(path: Path) -> Path:
    soffice = find_soffice()
    if not soffice:
        raise ValueError("LibreOffice was not found, so .doc could not be converted to .docx.")

    with TemporaryDirectory() as tmp:
        output_dir = Path(tmp)
        command = [
            str(soffice),
            "--headless",
            "--convert-to",
            "docx",
            "--outdir",
            str(output_dir),
            str(path),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        converted = output_dir / f"{path.stem}.docx"
        if result.returncode != 0 or not converted.exists():
            message = (result.stderr or result.stdout or "LibreOffice did not produce a DOCX file.").strip()
            raise ValueError(f"LibreOffice conversion failed: {message}")

        stable_path = path.with_suffix(".converted.docx")
        stable_path.write_bytes(converted.read_bytes())
        return stable_path


def find_soffice() -> Path | None:
    found = find_executable("soffice")
    if found:
        return found

    candidates = [
        Path("C:/Program Files/LibreOffice/program/soffice.exe"),
        Path("C:/Program Files (x86)/LibreOffice/program/soffice.exe"),
        Path("B:/LibreOffice/program/soffice.exe"),
        Path("B:/tools/LibreOffice/program/soffice.exe"),
        Path("A:/LibreOffice/program/soffice.exe"),
        Path("A:/tools/LibreOffice/program/soffice.exe"),
    ]
    return next((candidate for candidate in candidates if candidate.exists()), None)


def find_executable(name: str) -> Path | None:
    candidates = [name]
    if not name.endswith(".exe"):
        candidates.append(f"{name}.exe")
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return Path(found)
    return None


def clean_text(text: str) -> str:
    return clean_text_with_report(text).text


def clean_text_with_report(text: str) -> CleanResult:
    original_chars = len(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [clean_inline_text(line) for line in text.split("\n")]
    noise_lines_removed = sum(1 for line in lines if is_noise_line(line))
    lines = [line for line in lines if not is_noise_line(line)]
    text = "\n".join(lines)
    text = re.sub(r"(?<![。！？.!?:：；;])\n(?!\n|[#\-*0-9])", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    return CleanResult(
        text=text,
        original_chars=original_chars,
        cleaned_chars=len(text),
        noise_lines_removed=noise_lines_removed,
    )


def extract_readable_strings(data: bytes) -> list[str]:
    candidates: list[str] = []
    candidates.extend(extract_decoded_runs(data.decode("utf-16le", errors="ignore")))
    candidates.extend(extract_decoded_runs(data.decode("latin-1", errors="ignore")))

    seen: set[str] = set()
    readable: list[str] = []
    for candidate in candidates:
        normalized = clean_inline_text(candidate)
        if len(normalized) < 4 or normalized in seen:
            continue
        if readable_ratio(normalized) < 0.55:
            continue
        seen.add(normalized)
        readable.append(normalized)
    return readable


def extract_decoded_runs(text: str) -> list[str]:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]+", "\n", text)
    pattern = r"[\u4e00-\u9fffA-Za-z0-9，。！？、；：,.!?;:'\"()\[\]《》<>/\-_\s]{4,}"
    return [match.group(0).strip() for match in re.finditer(pattern, text) if match.group(0).strip()]


def readable_ratio(text: str) -> float:
    if not text:
        return 0.0
    readable_chars = re.findall(r"[\u4e00-\u9fffA-Za-z0-9，。！？、；：,.!?;:'\"()\[\]《》<>/\-_\s]", text)
    return len(readable_chars) / len(text)


def clean_inline_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def is_noise_line(line: str) -> bool:
    if not line:
        return False
    if re.fullmatch(r"[-_—=]{3,}", line):
        return True
    if re.fullmatch(r"(page\s*)?\d+(/\d+)?", line, flags=re.IGNORECASE):
        return True
    if re.fullmatch(r"第\s*\d+\s*页\s*(共\s*\d+\s*页)?", line):
        return True
    return False


def split_markdown_like_text(text: str, page_number: int | None = None, max_chars: int = 900, overlap_chars: int = 100) -> list[ParsedChunk]:
    sections = split_into_sections(text)
    chunks: list[ParsedChunk] = []

    for section_title, section_blocks in sections:
        chunks.extend(
            split_blocks_recursively(
                section_blocks,
                page_number=page_number,
                section_title=section_title,
                max_chars=max_chars,
                overlap_chars=overlap_chars,
            )
        )
    return chunks


def split_into_sections(text: str) -> list[tuple[str, list[str]]]:
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    sections: list[tuple[str, list[str]]] = []
    current_title = ""
    current_blocks: list[str] = []

    for block in blocks:
        heading = extract_section_heading(block)
        if heading and current_blocks:
            sections.append((current_title, current_blocks))
            current_blocks = []
        if heading:
            current_title = heading
            if is_standalone_heading_block(block):
                continue
        current_blocks.append(block)

    if current_blocks:
        sections.append((current_title, current_blocks))
    return sections


def split_blocks_recursively(
    blocks: list[str],
    page_number: int | None,
    section_title: str,
    max_chars: int,
    overlap_chars: int,
) -> list[ParsedChunk]:
    pieces: list[str] = []
    for block in blocks:
        pieces.extend(split_oversized_block(block, max_chars=max_chars))
    return pack_chunk_pieces(
        pieces,
        page_number=page_number,
        section_title=section_title,
        max_chars=max_chars,
        overlap_chars=overlap_chars,
    )


def split_oversized_block(block: str, max_chars: int) -> list[str]:
    block = block.strip()
    if not block:
        return []
    if len(block) <= max_chars:
        return [block]

    lines = [line.strip() for line in block.splitlines() if line.strip()]
    if len(lines) > 1:
        line_pieces: list[str] = []
        for line in lines:
            line_pieces.extend(split_oversized_block(line, max_chars=max_chars))
        return line_pieces

    sentences = split_sentences(block)
    if len(sentences) > 1:
        return pack_text_units(sentences, max_chars=max_chars)

    return split_by_chars(block, max_chars=max_chars)


def split_sentences(text: str) -> list[str]:
    sentence_pattern = r"[^。！？!?；;.\n]+[。！？!?；;.]?"
    sentences = [match.group(0).strip() for match in re.finditer(sentence_pattern, text) if match.group(0).strip()]
    return sentences or [text.strip()]


def pack_text_units(units: list[str], max_chars: int) -> list[str]:
    packed: list[str] = []
    current = ""
    for unit in units:
        if len(unit) > max_chars:
            if current:
                packed.append(current.strip())
                current = ""
            packed.extend(split_by_chars(unit, max_chars=max_chars))
            continue
        candidate = join_sentence_units(current, unit) if current else unit
        if current and len(candidate) > max_chars:
            packed.append(current.strip())
            current = unit
        else:
            current = candidate
    if current:
        packed.append(current.strip())
    return packed


def join_sentence_units(left: str, right: str) -> str:
    if not left:
        return right
    separator = "" if left[-1] in "。！？；" else " "
    return f"{left}{separator}{right}".strip()


def split_by_chars(text: str, max_chars: int) -> list[str]:
    return [text[index : index + max_chars].strip() for index in range(0, len(text), max_chars) if text[index : index + max_chars].strip()]


def pack_chunk_pieces(
    pieces: list[str],
    page_number: int | None,
    section_title: str,
    max_chars: int,
    overlap_chars: int,
) -> list[ParsedChunk]:
    chunks: list[ParsedChunk] = []
    current = ""

    for piece in pieces:
        separator = "\n\n" if "\n" in piece or piece.startswith("#") else " "
        candidate = f"{current}{separator}{piece}".strip() if current else piece
        if current and len(candidate) > max_chars:
            chunks.append(ParsedChunk(content=current.strip(), page_number=page_number, section_title=section_title))
            overlap = tail_overlap(current, overlap_chars)
            current = f"{overlap}{separator}{piece}".strip() if overlap else piece
            if len(current) > max_chars:
                current = piece
                if len(current) > max_chars:
                    for part in split_by_chars(current, max_chars=max_chars):
                        chunks.append(ParsedChunk(content=part.strip(), page_number=page_number, section_title=section_title))
                    current = ""
        else:
            current = candidate

    if current:
        chunks.append(ParsedChunk(content=current.strip(), page_number=page_number, section_title=section_title))
    return chunks


def count_sections(chunks: list[ParsedChunk]) -> int:
    return len({chunk.section_title for chunk in chunks if chunk.section_title})


def tail_overlap(text: str, overlap_chars: int) -> str:
    if overlap_chars <= 0 or len(text) <= overlap_chars:
        return ""
    tail = text[-overlap_chars:].strip()
    sentence_start = max(tail.rfind("。"), tail.rfind("."), tail.rfind("\n"))
    return tail[sentence_start + 1 :].strip() if sentence_start >= 0 else tail


def extract_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        stripped = line.strip("# ").strip()
        if stripped and not is_noise_line(stripped):
            return stripped[:120]
    return fallback.replace("_", " ").replace("-", " ").title()


def extract_heading(block: str) -> str:
    first_line = block.splitlines()[0].strip()
    markdown_heading = re.match(r"^#{1,4}\s+(.+)$", first_line)
    if markdown_heading:
        return markdown_heading.group(1).strip()[:120]
    if len(first_line) <= 60 and re.match(r"^(\d+(\.\d+)*[、. ]*)?[\u4e00-\u9fffA-Za-z].*", first_line):
        if len(block.splitlines()) == 1 or first_line.endswith(("：", ":")):
            return first_line.strip("：:")[:120]
    return ""


def extract_section_heading(block: str) -> str:
    first_line = block.splitlines()[0].strip()
    markdown_heading = re.match(r"^#{1,4}\s+(.+)$", first_line)
    if markdown_heading:
        return markdown_heading.group(1).strip()[:120]
    if len(block.splitlines()) != 1 or len(first_line) > 80:
        return ""
    numbered_heading = re.match(r"^\d+(\.\d+)*[.、 ]+[\u4e00-\u9fffA-Za-z].+", first_line)
    if numbered_heading:
        return first_line.strip(":：")[:120]
    if first_line.endswith((":","：")):
        return first_line.strip(":：")[:120]
    return ""


def is_standalone_heading_block(block: str) -> bool:
    first_line = block.splitlines()[0].strip() if block.splitlines() else ""
    return len(block.splitlines()) == 1 and bool(re.match(r"^#{1,4}\s+.+$", first_line))


def table_to_text(table) -> str:
    rows: list[str] = []
    for row in table.rows:
        cells = [clean_inline_text(cell.text) for cell in row.cells]
        cells = [cell for cell in cells if cell]
        if cells:
            rows.append(" | ".join(cells))
    return "\n".join(rows)
