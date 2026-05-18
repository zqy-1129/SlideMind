import re
from pathlib import Path
from typing import Any

import docx
import docx.table
import docx.text.paragraph
from docx import Document

TABLE_MAX_ROWS = 30
TABLE_ROW_OVERLAP = 3
TABLE_MIN_ROWS = 2
PARSER_VERSION = "stkg_v1"

_MD_EMPHASIS_RE = re.compile(r"\*{1,2}([^*]+?)\*{1,2}|_{1,2}([^_]+?)_{1,2}")
_MD_LIST_RE = re.compile(r"^\s*[-*•]\s+|^\s*\d+[.)]\s+", re.MULTILINE)
_MD_ESCAPE_RE = re.compile(r"\\([^\n])")
_WHITESPACE_RE = re.compile(r"[\u3000\xa0\t\r ]+")
_ZH_ASCII_SPACE_RE = re.compile(
    r"(?<=[\u4e00-\u9fff]) +(?=[\u4e00-\u9fff\x21-\x7e])|(?<=[\x21-\x7e]) +(?=[\u4e00-\u9fff])"
)
_MULTI_NEWLINE_RE = re.compile(r"\n{2,}")
_LEADING_PUNCT_RE = re.compile(r"^[，,。！？；：、\"'\s]+")
_NOISE_RE = re.compile(r"^[\s\d\W]+$")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?…；;])")


def build_document_chunks(path: str, parsed_text: str | None = None) -> list[dict[str, Any]]:
    file_path = Path(path)
    if file_path.suffix.lower() == ".docx":
        try:
            blocks = _parse_docx_blocks(Document(str(file_path)))
        except Exception:
            blocks = _plain_text_blocks(parsed_text or "")
    else:
        blocks = _plain_text_blocks(parsed_text or "")

    chunks = _normalize_and_group_blocks(blocks)
    return [
        {
            "text": item["content"],
            "title_path": item.get("title_path") or "",
            "tokens": _count_tokens(item["content"]),
            "chunk_type": item.get("chunk_type") or "text",
            "parser_version": PARSER_VERSION,
            "chunk_order_index": index,
        }
        for index, item in enumerate(chunks)
        if item.get("content", "").strip()
    ]


def _parse_docx_blocks(document: Document) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None
    last_block: dict[str, Any] | None = None
    title_stack: list[str] = []
    terminal_punct = re.compile(r"[。！？…；;!?]$")
    leading_punct = re.compile(r"[：:，,]$")

    def current_title() -> str:
        return " > ".join(title for title in title_stack if title)

    def append_block(block: dict[str, Any], allow_merge: bool = True) -> None:
        nonlocal last_block
        blocks.append(block)
        last_block = block if allow_merge else None

    def flush_pending_with(content: str) -> dict[str, Any]:
        nonlocal pending
        merged = {
            "title_path": pending.get("title_path", "") if pending else current_title(),
            "content": (pending.get("content", "") if pending else "") + content,
            "chunk_type": "text",
        }
        pending = None
        return merged

    for element in document.element.body:
        tag = element.tag if isinstance(element.tag, str) else ""
        if tag.endswith("p"):
            paragraph = docx.text.paragraph.Paragraph(element, document)
            style_name = paragraph.style.name or ""
            if style_name.startswith("Heading"):
                heading_text = clean_text(paragraph.text)
                if not heading_text:
                    continue
                if pending:
                    append_block(pending)
                    pending = None
                level = _heading_level(style_name)
                while len(title_stack) < level:
                    title_stack.append("")
                title_stack[level - 1] = heading_text
                for index in range(level, len(title_stack)):
                    title_stack[index] = ""
                last_block = None
                continue

            content = _paragraph_text(paragraph)
            if not content or _is_placeholder(content):
                continue

            char_count = len(content.replace(" ", ""))
            title_path = current_title()
            if char_count >= 80:
                append_block(flush_pending_with(content) if pending else {"title_path": title_path, "content": content, "chunk_type": "text"})
            elif leading_punct.search(content):
                if pending:
                    append_block(pending)
                pending = {"title_path": title_path, "content": content, "chunk_type": "text"}
            elif pending:
                append_block(flush_pending_with(content))
            elif last_block and last_block.get("title_path") == title_path and not terminal_punct.search(str(last_block.get("content", ""))):
                last_block["content"] += content
                last_block["chunk_type"] = _merge_chunk_type(str(last_block.get("chunk_type")), "text")
            else:
                append_block({"title_path": title_path, "content": content, "chunk_type": "text"})

        elif tag.endswith("tbl"):
            table = docx.table.Table(element, document)
            headers, data_rows = _parse_table_cells(table)
            if not headers:
                continue
            title_path = current_title()
            caption = str(pending.get("content", "")) if pending else ""
            pending = None
            if len(data_rows) < TABLE_MIN_ROWS:
                content = "\n".join(item for item in (caption, _table_markdown(headers, data_rows)) if item)
                if last_block and last_block.get("title_path") == title_path:
                    last_block["content"] += "\n" + content
                    last_block["chunk_type"] = _merge_chunk_type(str(last_block.get("chunk_type")), "table")
                else:
                    append_block({"title_path": title_path, "content": content, "chunk_type": "mixed"})
                continue
            for table_chunk in _table_chunks(headers, data_rows, caption, title_path):
                append_block(table_chunk, allow_merge=False)

    if pending:
        append_block(pending)
    return blocks


def _normalize_and_group_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for block in blocks:
        content = str(block.get("content") or "").strip()
        if not content:
            continue
        if block.get("chunk_type") == "table":
            result.extend(_split_long_text(content, block, chunk_token_size=900, overlap=120))
            continue
        sentences = _split_sentences(content)
        if not sentences:
            continue
        grouped = _group_sentences(sentences, block)
        for item in grouped:
            result.extend(_split_long_text(item["content"], item, chunk_token_size=900, overlap=120))
    return result


def _plain_text_blocks(text: str) -> list[dict[str, Any]]:
    parts = [clean_text(part) for part in re.split(r"\n{2,}", text or "") if clean_text(part)]
    if not parts and text:
        parts = [clean_text(text)]
    return [{"title_path": "", "content": part, "chunk_type": "text"} for part in parts]


def _split_sentences(text: str) -> list[str]:
    rough = _SENTENCE_SPLIT_RE.split(text)
    sentences = []
    for item in rough:
        sentence = _LEADING_PUNCT_RE.sub("", item).strip()
        if len(sentence) < 5 or _NOISE_RE.match(sentence):
            continue
        sentences.append(sentence)
    return sentences or [text]


def _group_sentences(sentences: list[str], block: dict[str, Any]) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    current: list[str] = []
    current_tokens = 0
    for sentence in sentences:
        tokens = _count_tokens(sentence)
        should_flush = current and (
            current_tokens + tokens > 620 or _jaccard_similarity(current[-1], sentence) < 0.04 and current_tokens > 220
        )
        if should_flush:
            chunks.append({**block, "content": "".join(current)})
            current = []
            current_tokens = 0
        current.append(sentence)
        current_tokens += tokens
    if current:
        chunks.append({**block, "content": "".join(current)})
    return chunks


def _split_long_text(text: str, block: dict[str, Any], chunk_token_size: int, overlap: int) -> list[dict[str, Any]]:
    if _count_tokens(text) <= chunk_token_size:
        return [{**block, "content": text}]
    step = max(1, (chunk_token_size - overlap) * 2)
    size = chunk_token_size * 2
    return [{**block, "content": text[start : start + size]} for start in range(0, len(text), step) if text[start : start + size].strip()]


def _parse_table_cells(table: docx.table.Table) -> tuple[list[str], list[list[str]]]:
    rows: list[list[str]] = []
    for row in table.rows:
        values = []
        previous = None
        for cell in row.cells:
            value = clean_text(cell.text.replace("\n", " "))
            values.append("" if value == previous else value)
            previous = value
        rows.append(values)
    if not rows:
        return [], []
    max_cols = max(len(row) for row in rows)
    for row in rows:
        row.extend([""] * (max_cols - len(row)))
    return rows[0], rows[1:]


def _table_chunks(headers: list[str], data_rows: list[list[str]], caption: str, title_path: str) -> list[dict[str, Any]]:
    stride = max(1, TABLE_MAX_ROWS - TABLE_ROW_OVERLAP)
    chunks = []
    for start in range(0, len(data_rows), stride):
        rows = data_rows[start : start + TABLE_MAX_ROWS]
        content = "\n".join(item for item in (caption, _table_markdown(headers, rows)) if item)
        chunks.append({"title_path": title_path, "content": content, "chunk_type": "table"})
        if start + TABLE_MAX_ROWS >= len(data_rows):
            break
    return chunks


def _table_markdown(headers: list[str], data_rows: list[list[str]]) -> str:
    if not headers:
        return ""
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    lines.extend("| " + " | ".join(row) + " |" for row in data_rows)
    return "\n".join(lines)


def clean_text(text: str, keep_newlines: bool = False) -> str:
    text = _MD_EMPHASIS_RE.sub(lambda match: match.group(1) or match.group(2), text or "")
    text = _MD_LIST_RE.sub("", text)
    text = _MD_ESCAPE_RE.sub(r"\1", text)
    text = _WHITESPACE_RE.sub(" ", text)
    text = _ZH_ASCII_SPACE_RE.sub("", text)
    if keep_newlines:
        text = _MULTI_NEWLINE_RE.sub("\n", text)
    else:
        text = _WHITESPACE_RE.sub(" ", text.replace("\n", " "))
    return text.strip()


def _paragraph_text(paragraph: docx.text.paragraph.Paragraph) -> str:
    parts = [run.text.strip() for run in paragraph.runs]
    raw = "".join(part for part in parts if part) or paragraph.text
    return clean_text(raw)


def _heading_level(style_name: str) -> int:
    match = re.search(r"\d+", style_name)
    if not match:
        return 1
    return max(1, min(6, int(match.group())))


def _is_placeholder(text: str) -> bool:
    return len(text.strip()) < 2 or "【" in text or "】" in text


def _merge_chunk_type(left: str, right: str) -> str:
    return left if left == right else "mixed"


def _count_tokens(text: str) -> int:
    return max(1, len(text) // 2)


def _jaccard_similarity(text_a: str, text_b: str) -> float:
    tokens_a = set(_tokenize(text_a))
    tokens_b = set(_tokenize(text_b))
    union = tokens_a | tokens_b
    return len(tokens_a & tokens_b) / len(union) if union else 0.0


def _tokenize(text: str) -> list[str]:
    try:
        import jieba

        return [token for token in jieba.lcut(text) if token.strip()]
    except Exception:
        return [text[index : index + 2] for index in range(0, len(text), 2) if text[index : index + 2].strip()]
