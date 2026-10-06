from __future__ import annotations

import re

PII_PATTERNS = (
    re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"),
    re.compile(r"(?:\+7|8)[\s()-]*\d{3}[\s()-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}"),
)
INJECTION_MARKERS = (
    "ignore previous instructions", "ignore all instructions", "system prompt",
    "забудь предыдущие инструкции", "игнорируй предыдущие инструкции",
)


def redact_pii(text: str) -> str:
    for pattern in PII_PATTERNS:
        text = pattern.sub("[СКРЫТО]", text)
    return text


def contains_pii(text: str) -> bool:
    return any(pattern.search(text) for pattern in PII_PATTERNS)


def flag_injection(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in INJECTION_MARKERS)


def clean_model_report(report: str, valid_citations: set[str]) -> str:
    def citation(match: re.Match[str]) -> str:
        marker = match.group(1)
        return match.group(0) if marker in valid_citations else "[неподтверждённая ссылка]"

    report = re.sub(r"\[(E\d+)\]", citation, report)
    return redact_pii(report)


def report_blocks(report: str) -> list[dict]:
    """Top-level Markdown blocks, with display math and its citation kept together."""
    from markdown_it import MarkdownIt

    lines = report.splitlines()
    blocks = []
    normal = []

    def flush_normal():
        content = "\n".join(normal)
        for token in MarkdownIt("default").parse(content):
            if token.level != 0 or token.map is None or token.nesting == -1:
                continue
            start, end = token.map
            kind = "heading" if token.type == "heading_open" else token.type.removesuffix("_open")
            blocks.append({"kind": kind,
                           "level": int(token.tag[1:]) if kind == "heading" else 0,
                           "text": "\n".join(normal[start:end]).strip()})
        normal.clear()

    index = 0
    fence = ""
    while index < len(lines):
        stripped = lines[index].strip()
        code = re.match(r"^(`{3,}|~{3,})", stripped)
        if code:
            if not fence:
                fence = code[1]
            elif code[1][0] == fence[0] and len(code[1]) >= len(fence):
                fence = ""
        if stripped.startswith("$$") and not fence:
            flush_normal()
            math_lines = [lines[index]]
            index += 1
            if stripped.count("$$") < 2:
                while index < len(lines):
                    math_lines.append(lines[index])
                    index += 1
                    if "$$" in math_lines[-1]:
                        break
            cursor = index
            while cursor < len(lines) and not lines[cursor].strip():
                cursor += 1
            # A formula citation is a separate line; never consume the next
            # formula or unrelated paragraph when the citation is missing.
            if cursor < len(lines) and re.match(r"^(?:Источник|Источник формулы):", lines[cursor].strip()):
                math_lines.append(lines[cursor])
                index = cursor + 1
            blocks.append({"kind": "math", "level": 0, "text": "\n".join(math_lines)})
        else:
            normal.append(lines[index])
            index += 1
    flush_normal()
    return [{"id": f"B{index}", **block} for index, block in enumerate(blocks, 1)]


def join_report_blocks(blocks: list[dict]) -> str:
    """Remove headings whose section has no surviving content."""
    kept = []
    for index, block in enumerate(blocks):
        if block["kind"] == "heading":
            has_content = False
            for following in blocks[index + 1:]:
                if following["kind"] == "heading" and following["level"] <= block["level"]:
                    break
                if following["kind"] != "heading":
                    has_content = True
                    break
            if not has_content:
                continue
        kept.append(block["text"])
    return "\n\n".join(kept).strip()


def _cited_block_valid(text: str, by_marker: dict[str, str]) -> bool:
    markers = re.findall(r"\[(E\d+)\]", text)
    if (not markers or any(marker not in by_marker for marker in markers)
            or "[неподтверждённая ссылка]" in text):
        return False
    source = " ".join(by_marker[marker] for marker in markers)
    # List numbering and citation identifiers are presentation, not measured values.
    claim = re.sub(r"\[E\d+\]", "", text)
    claim = re.sub(r"(?m)^\s*\d+[.)]\s+", "", claim)
    def numbers(value):
        return {n.replace(",", ".") for n in re.findall(r"\b\d+(?:[.,]\d+)?\b", value)}
    if not numbers(claim).issubset(numbers(source)):
        return False
    source_compact = re.sub(r"\s+", "", source)
    display = re.findall(r"\$\$(.*?)\$\$", claim, re.S)
    inline_text = re.sub(r"\$\$.*?\$\$", "", claim, flags=re.S)
    inline = re.findall(r"(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)", inline_text)
    if claim.count("$$") % 2:
        return False
    return all(re.sub(r"\s+", "", formula) in source_compact for formula in display + inline)


def filter_cited_blocks(report: str, evidence: list[dict]) -> tuple[str, list[str]]:
    """Mechanical checks only; semantic review MUST follow before publication.

    A paragraph's closing citations cover the whole paragraph, including soft
    line breaks. Headers are retained for the semantic reviewer, not trusted.
    """
    by_marker = {item["marker"]: item["text"] for item in evidence}
    accepted = []
    issues = []
    for block in report_blocks(report):
        text = block["text"]
        if block["kind"] == "heading":
            accepted.append(block)
            continue
        if block["kind"] == "table":
            lines = text.splitlines()
            rows = []
            for row in lines[2:]:
                if _cited_block_valid(row, by_marker):
                    rows.append(row)
                else:
                    issues.append(f"{block['id']}: строка таблицы без проверяемой ссылки, числа или формулы: {row}")
            if rows:
                accepted.append({**block, "text": "\n".join(lines[:2] + rows)})
        elif _cited_block_valid(text, by_marker):
            accepted.append(block)
        else:
            issues.append(f"{block['id']}: блок без проверяемой ссылки, числа или формулы: {text}")
    return join_report_blocks(accepted), issues


def keep_cited_claims(report: str, evidence: list[dict]) -> tuple[str, int]:
    """Compatibility wrapper; this is not a semantic verification verdict."""
    checked, issues = filter_cited_blocks(report, evidence)
    return checked, len(issues)
