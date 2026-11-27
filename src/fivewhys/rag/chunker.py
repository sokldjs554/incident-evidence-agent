"""마크다운 문서 청킹.

포스트모템 문서는 "## 섹션" 구조가 강한 문서이므로, 문자 수 고정 분할 대신
섹션 경계를 우선하는 구조 인지(chunking-by-structure) 방식을 쓴다.

- 섹션 하나가 max_chars 이하면 그대로 하나의 청크
- 초과하면 문단 단위로 나누되 overlap_chars 만큼 앞 문단 꼬리를 겹쳐
  문맥 단절을 줄인다
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$")


@dataclass
class Chunk:
    doc: str
    section: str
    text: str


def _hard_split(para: str, max_chars: int, overlap_chars: int) -> list[str]:
    """단일 문단이 max_chars를 넘으면 겹침 윈도우로 강제 분할한다."""
    step = max(max_chars - overlap_chars, 1)
    return [para[i : i + max_chars] for i in range(0, len(para), step)]


def _split_long(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    parts: list[str] = []
    buf = ""

    def flush() -> None:
        nonlocal buf
        if buf:
            parts.append(buf)
            buf = ""

    for para in paragraphs:
        if len(para) > max_chars:
            flush()
            parts.extend(_hard_split(para, max_chars, overlap_chars))
            continue
        if buf and len(buf) + len(para) + 2 > max_chars:
            parts.append(buf)
            tail = buf[-overlap_chars:] if overlap_chars else ""
            buf = (tail + "\n" + para).strip()
        else:
            buf = f"{buf}\n\n{para}".strip() if buf else para
    flush()
    return parts or [text]


def _find_headings(text: str) -> list[tuple[int, int, str]]:
    """(시작 오프셋, 끝 오프셋, 제목) 목록. 코드 펜스(```) 안은 무시한다."""
    headings: list[tuple[int, int, str]] = []
    in_fence = False
    offset = 0
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
        elif not in_fence:
            m = _HEADING_RE.match(line)
            if m:
                headings.append((offset, offset + len(line), m.group(2).strip()))
        offset += len(line) + 1
    return headings


def chunk_markdown(
    doc_name: str, text: str, max_chars: int = 900, overlap_chars: int = 120
) -> list[Chunk]:
    """마크다운 문서를 섹션 기반 청크 목록으로 나눈다."""
    headings = _find_headings(text)
    chunks: list[Chunk] = []

    if not headings:
        for part in _split_long(text, max_chars, overlap_chars):
            chunks.append(Chunk(doc=doc_name, section="", text=part))
        return chunks

    # 문서 제목(첫 헤딩 이전 내용)도 버리지 않는다
    preamble = text[: headings[0][0]].strip()
    if preamble:
        chunks.append(Chunk(doc=doc_name, section="(서문)", text=preamble))

    for i, (_, end, section_title) in enumerate(headings):
        body_start = end
        body_end = headings[i + 1][0] if i + 1 < len(headings) else len(text)
        body = text[body_start:body_end].strip()
        if not body:
            continue
        for part in _split_long(body, max_chars, overlap_chars):
            # 섹션 제목을 청크 본문에 포함시켜 검색 신호를 살린다
            chunks.append(
                Chunk(doc=doc_name, section=section_title, text=f"{section_title}\n{part}")
            )
    return chunks
