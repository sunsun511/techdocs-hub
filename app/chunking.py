"""Keep section context and paragraph/code boundaries; do not execute HTML or code."""
import re
from lxml import html


def blocks(content: str, format: str):
    if format == "html":
        tree = html.fromstring(content)
        scopes = tree.xpath('//*[@id="docContent"]') or tree.xpath('//main') or tree.xpath('//*[contains(concat(" ",normalize-space(@class)," ")," chapter ")]')
        tree = scopes[0] if scopes else tree
        for el in tree.xpath('.//script|.//style|.//nav|.//footer|.//aside'):
            el.drop_tree()
        parts = []
        for el in tree.xpath('.//h1|.//h2|.//h3|.//h4|.//h5|.//h6|.//p|.//pre|.//li[not(.//p) and not(.//li)]|.//table'):
            if any(a.tag in ["pre", "table"] for a in el.iterancestors()):
                continue
            text = "\n".join(el.itertext()) if el.tag == "pre" else " ".join(el.text_content().split())
            if text.strip():
                parts.append((int(el.tag[1]) if re.fullmatch(r"h[1-6]", el.tag) else 0, text.strip()))
    elif format == "markdown":
        cleaned = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", content, flags=re.S)
        cleaned = re.sub(r"\{\{[<%].*?[%>]\}\}", "", cleaned, flags=re.S)
        parts, acc = [], []
        fence = None

        def flush():
            if acc:
                parts.append((0, "\n".join(acc).strip()))
                acc.clear()

        for line in cleaned.splitlines():
            marker = re.match(r"^\s*(`{3,}|~{3,})", line)
            if marker:
                if fence is None:
                    flush()
                    fence = marker.group(1)[0]
                    acc.append(line)
                elif marker.group(1)[0] == fence:
                    acc.append(line)
                    flush()
                    fence = None
                else:
                    acc.append(line)
            elif fence:
                acc.append(line)
            elif (heading := re.match(r"^(#{1,6})\s+(.+)", line)):
                flush()
                parts.append((len(heading.group(1)), heading.group(2)))
            elif not line.strip():
                flush()
            else:
                acc.append(line)
        flush()
    else:
        parts = [(0, p.strip()) for p in re.split(r"\n\s*\n", content) if p.strip()]
    headings, result = {}, []
    seen = set()
    for level, text in parts:
        if level:
            headings = {k: v for k, v in headings.items() if k < level}
            headings[level] = text.rstrip(" #")
            continue
        path = " / ".join(headings[k] for k in sorted(headings))
        if (path, text) in seen:
            continue
        seen.add((path, text))
        if text:
            result.append({"heading": path, "text": text})
    return result


def chunk_document(content: str, format: str, title: str):
    chunks, pending = [], []
    size = 0

    def flush():
        nonlocal size
        if pending:
            chunks.append({"heading": pending[0]["heading"] or title, "text": "\n\n".join(p["text"] for p in pending)})
        pending.clear()
        size = 0

    for block in blocks(content, format):
        # English technical corpus: 150-word maximum, character bound also
        # prevents one unsegmented Chinese paragraph becoming unbounded.
        count = len(block["text"].split())
        if pending and (pending[0]["heading"] != block["heading"] or size + count > 150 or size >= 60):
            flush()
        if count > 150 or len(block["text"]) > 3000:
            flush()
            if count > 150:
                words = block["text"].split()
                pieces = [" ".join(words[i:i + 150]) for i in range(0, len(words), 110)]
            else:
                pieces = [block["text"][i:i + 2400] for i in range(0, len(block["text"]), 2000)]
            chunks.extend({"heading": block["heading"] or title, "text": p} for p in pieces if p)
        else:
            pending.append(block)
            size += count
    flush()
    return chunks
