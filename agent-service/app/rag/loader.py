"""知识库加载：把 app/knowledge/*.md 读成 Document，并按 Markdown 标题切分。

切分粒度选择"标题优先"而不是"固定字数硬切"：
知识库是人工维护的规则文档，**一个二级标题就是一个完整语义单元**
（如"## 逾期罚金"）。按标题切能让召回结果直接定位到读者关心的那一条规则；
固定字数硬切会把"罚金 0.2 元/天"和"封顶为书价"切到两个 chunk 里，
LLM 拿到半句话就容易编。超长 section 再做二次窗口切分（见 splitter.split_chunks）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Iterable

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


@dataclass
class Document:
    """一篇知识库文档。"""

    doc_id: str
    title: str
    text: str
    source: str
    meta: dict[str, str] = field(default_factory=dict)


@dataclass
class Section:
    """文档内的一个标题单元。"""

    doc_id: str
    doc_title: str
    heading: str
    breadcrumb: str
    text: str
    source: str
    line_no: int = 0


def load_documents(knowledge_dir: str) -> list[Document]:
    """读取目录下所有 .md / .txt 文档。

    编码固定 utf-8-sig：Windows 上用记事本编辑过的中文文件常带 BOM，
    不处理会污染第一个标题导致检索不到。
    """
    docs: list[Document] = []
    if not os.path.isdir(knowledge_dir):
        return docs
    for name in sorted(os.listdir(knowledge_dir)):
        if not name.lower().endswith((".md", ".txt")):
            continue
        path = os.path.join(knowledge_dir, name)
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8-sig") as fh:
            raw = fh.read()
        title = _first_heading(raw) or os.path.splitext(name)[0]
        docs.append(
            Document(
                doc_id=os.path.splitext(name)[0],
                title=title,
                text=raw.strip(),
                source=f"knowledge/{name}",
                meta={"file": name},
            )
        )
    return docs


def _first_heading(text: str) -> str | None:
    for line in text.splitlines():
        m = _HEADING_RE.match(line.strip())
        if m and m.group(1) == "#":
            return m.group(2).strip()
    return None


def split_sections(doc: Document) -> list[Section]:
    """按 Markdown 标题切 section，并维护 breadcrumb（如"借阅规则 > 逾期与罚金"）。

    breadcrumb 会被拼进 chunk 文本再建索引：
    用户问"罚金多少"时，只有把"借阅规则/逾期"这类上下文带进 chunk，
    BM25 才能把"0.2 元/天"这一条和其他金额类文本区分开。
    """
    sections: list[Section] = []
    heading_stack: list[str] = [doc.title]
    buf: list[str] = []
    current_heading = doc.title
    current_line = 0

    def flush() -> None:
        body = "\n".join(buf).strip()
        buf.clear()
        if not body:
            return
        breadcrumb = " > ".join(heading_stack)
        sections.append(
            Section(
                doc_id=doc.doc_id,
                doc_title=doc.title,
                heading=current_heading,
                breadcrumb=breadcrumb,
                text=body,
                source=doc.source,
                line_no=current_line,
            )
        )

    for line_no, line in enumerate(doc.text.splitlines(), start=1):
        stripped = line.strip()
        m = _HEADING_RE.match(stripped)
        if m:
            level = len(m.group(1))
            heading_text = m.group(2).strip()
            if level == 1:
                # 文档大标题不进正文 chunk，但保留在 breadcrumb 里
                heading_stack = [heading_text]
                current_heading = heading_text
                current_line = line_no
                continue
            flush()
            # 同级或更浅的标题出现时，先砍掉它后面的旧标题，保持层级路径正确
            heading_stack = heading_stack[: level - 1]
            heading_stack.append(heading_text)
            current_heading = heading_text
            current_line = line_no
            continue
        buf.append(line)
    flush()
    return sections


def iter_sections(docs: Iterable[Document]) -> list[Section]:
    out: list[Section] = []
    for doc in docs:
        out.extend(split_sections(doc))
    return out


__all__ = ["Document", "Section", "load_documents", "split_sections", "iter_sections"]
