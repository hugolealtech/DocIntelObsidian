import argparse
from collections import Counter
import difflib
import os
import re
import shutil
from pathlib import Path


PAGE_MARKER_RE = re.compile(r"\n\n---\n\*p\. (\d+)\*\n\n---\n")
EMPTY_PARENS_RE = re.compile(r"\(\s*\)")
MARKDOWN_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
NUMBERED_TITLE_RE = re.compile(r"^(\d+(?:\.\d+)*)\)\s+(.+)$")
CONTINUATION_RE = re.compile(r"^\(continu(?:ação|ando)\)\s*(.*)$", re.IGNORECASE)
STRIKETHROUGH_MARKER_RE = re.compile(r"¬{2,}(?P<text>.+?)¬{2,}")


def _counts(text):
    headings = {level: 0 for level in range(1, 7)}
    for line in text.splitlines():
        match = MARKDOWN_HEADING_RE.match(line)
        if match:
            headings[len(match.group(1))] += 1
    return {
        "negation": text.count("¬"),
        "headings": headings,
        "empty_parentheses": len(EMPTY_PARENS_RE.findall(text)),
    }


def _plain_title(line):
    line = line.strip()
    return re.sub(r"^#{1,6}\s*", "", line).strip()


def _title_text(line):
    return _plain_title(line).replace("**", "").strip()


def _format_title(line, summary_item=False):
    title = _title_text(line)
    display_title = _plain_title(line)
    if not title:
        return line
    if title.casefold() == "roteiro de aula":
        return f"# {display_title}"
    if title.casefold() == "sumário" or re.match(r"^Tema:\s*\S", title, re.IGNORECASE):
        return f"## {display_title}"
    if summary_item:
        return f"- {display_title.lstrip('- ').strip()}"
    if len(title) > 80 or title.endswith("."):
        return line
    if re.match(r"^Bloco\s+\d+\s*$", title, re.IGNORECASE):
        return f"### {display_title}"
    match = NUMBERED_TITLE_RE.match(title)
    if not match:
        return line
    depth = match.group(1).count(".")
    return f"{'#' * min(3 + depth, 6)} {display_title}"


def _extract_table(pdf_path):
    import pymupdf

    with pymupdf.open(pdf_path) as document:
        tables = document[4].find_tables().tables
        if not tables:
            raise ValueError("Nenhuma tabela encontrada na página 5 do PDF")
        rows = tables[0].extract()
    if len(rows) != 5 or any(len(row) != 3 for row in rows):
        raise ValueError(f"Tabela esperada 5x3; extração retornou {len(rows)} linhas")
    rows = [
        [(cell or "").replace("\n", " ").replace("|", r"\|") for cell in row]
        for row in rows
    ]
    markdown = ["|" + "|".join(rows[0]) + "|", "|---|---|---|"]
    markdown.extend("|" + "|".join(row) + "|" for row in rows[1:])
    return "\n".join(markdown)


def _replace_broken_table(text, table):
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("|Art. 9º")), None)
    if start is None:
        raise ValueError("Início do bloco da tabela de improbidade não encontrado")
    end = next((i for i in range(start, len(lines)) if re.match(r"^\|O\s+agente público", lines[i])), None)
    if end is None:
        raise ValueError("Fim do bloco da tabela de improbidade não encontrado")
    lines[start:end + 1] = table.splitlines()
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def _split_marker_line(line):
    quote = re.match(r"^(\s*>\s*)", line)
    quote_prefix = quote.group(1) if quote else ""
    content = line[len(quote_prefix):] if quote else line
    content = re.sub(r"¬{2,}●¬{2,}\s*¬{2,}", "\n- ", content)
    content = re.sub(r"¬{2,}\s*¬{2,}", "\n", content)
    content = re.sub(r"¬{2,}", "\n", content)
    pieces = content.splitlines()
    if not pieces:
        return [line]
    result = []
    for piece in pieces:
        if not piece.strip():
            continue
        result.append((quote_prefix if quote_prefix else "") + piece.rstrip())
    return result or ([quote_prefix.rstrip()] if quote_prefix else [])


def _restore_strikethrough_markers(text):
    return STRIKETHROUGH_MARKER_RE.sub(
        lambda match: f"~~{match.group('text')}~~",
        text,
    )


def _remove_page_number(content, page_number):
    lines = content.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if not lines[index].strip():
            continue
        match = re.match(r"^(?P<quote>\s*>\s*)?(?P<body>.*?)(?P<space>[ \t]+)(?P<number>\d+)[ \t]*$", lines[index])
        if match and match.group("number") == page_number:
            if re.search(r"\bEra:.*\bPassou:", lines[index], re.IGNORECASE):
                break
            remainder = match.group("body").rstrip()
            if not remainder and match.group("quote"):
                lines.pop(index)
            else:
                lines[index] = (match.group("quote") or "") + remainder
        break
    return "\n".join(lines)


def _split_long_clauses(line):
    if len(line) < 240 or re.search(r"\bEra:.*\bPassou:", line, re.IGNORECASE):
        return [line]
    quote = re.match(r"^(\s*>\s*)", line)
    quote_prefix = quote.group(1) if quote else ""
    content = line[len(quote_prefix):] if quote else line
    chunks = re.split(
        r"[ \t]+(?=(?:[IVXLCDM]{1,8}\s*[-–—]\s+|§\s*\d+\b|(?:\([a-z]\)|[a-z]\))\s+))",
        content,
        flags=re.IGNORECASE,
    )
    return [quote_prefix + chunk for chunk in chunks]


def _restore_titles(text):
    fixed = []
    for line in text.splitlines():
        title = _title_text(line)
        if re.match(r"^4\)\s+Sanções da improbidade\b", title, re.IGNORECASE):
            fixed.append("4) Sanções da improbidade (art. 12)")
        elif re.match(r"^7\)\s+Indisponibilidade de Bens\b", title, re.IGNORECASE):
            fixed.append("7) Indisponibilidade de Bens – art. 16")
        elif re.match(r"^\(continuando\)\s+Procedimento Judicial\b", title, re.IGNORECASE):
            fixed.append("*(continuando)* Procedimento Judicial - arts. 17 a 18-A")
        else:
            fixed.append(line)
    return "\n".join(fixed)


def _normalize_page_markers(text):
    parts = PAGE_MARKER_RE.split(text)
    output = []
    for index in range(0, len(parts), 2):
        content = parts[index]
        if index + 1 < len(parts):
            page_number = parts[index + 1]
            lines = []
            for line in content.splitlines():
                lines.extend(_split_marker_line(line))
            content = _remove_page_number("\n".join(lines), page_number)
            output.extend((content, f"\n\n---\n*p. {page_number}*\n\n---\n"))
        else:
            lines = []
            for line in content.splitlines():
                lines.extend(_split_marker_line(line))
            output.append("\n".join(lines))
    text = "".join(output)
    return "\n".join(
        chunk
        for line in text.splitlines()
        for chunk in _split_long_clauses(line)
    )


def _format_titles(text):
    frontmatter_end = text.find("\n---\n", 4)
    if text.startswith("---\n") and frontmatter_end >= 0:
        prefix_end = frontmatter_end + len("\n---\n")
        prefix, body = text[:prefix_end], text[prefix_end:]
    else:
        prefix, body = "", text

    lines = body.splitlines()
    in_summary = False
    formatted = []
    for line in lines:
        stripped = line.strip()
        title = _title_text(stripped)
        if in_summary and stripped == "---":
            in_summary = False
        if title.casefold() == "sumário":
            formatted.append(f"## {_plain_title(stripped)}")
            in_summary = True
            continue
        if in_summary and stripped:
            if re.match(r"^Tema:\s*\S", title, re.IGNORECASE):
                formatted.append(f"## {_plain_title(stripped)}")
            else:
                summary_item = _plain_title(stripped).lstrip("- ").strip()
                formatted.append(f"- {summary_item}")
            continue
        continuation = CONTINUATION_RE.match(title)
        if continuation:
            if "**" in stripped or re.search(r"(?<!\*)\*(?!\*)", stripped):
                formatted.append(line)
                continue
            subject = continuation.group(1).strip()
            if subject and formatted and _plain_title(formatted[-1]).casefold() == subject.casefold():
                continue
            label = "continuação" if "continua" in title.casefold() else "continuando"
            formatted.append(f"*({label})* {subject}".rstrip())
            continue
        formatted.append(_format_title(line))
    return prefix + "\n".join(formatted)


def process(text, table):
    if text.count("¬"):
        text = _restore_strikethrough_markers(text)
        text = _normalize_page_markers(text)
    text = _restore_titles(text)
    text = _replace_broken_table(text, table)
    text = _format_titles(text)
    return text


def _validate_preserved_content(before, after):
    def frontmatter(text):
        if not text.startswith("---\n"):
            return ""
        end = text.find("\n---\n", 4)
        return text[:end + 5] if end >= 0 else ""

    if frontmatter(before) != frontmatter(after):
        raise ValueError("Front matter foi alterado")
    emphasis_pattern = re.compile(
        r"\*\*[^*\n]+?\*\*|(?<!\*)\*(?!\*)[^*\n]+?(?<!\*)\*(?!\*)"
    )
    before_emphasis = Counter(emphasis_pattern.findall(before))
    after_emphasis = Counter(emphasis_pattern.findall(after))
    if before_emphasis - after_emphasis:
        raise ValueError("Negritos ou itálicos foram alterados")
    for pattern, name in (
        (r"!\[\[[^\]\n]+\]\]", "imagens"),
        (r"(?m)^> \[!(?:quote|attention)\].*$", "callouts"),
        (r"(?m)^\*p\. \d+\*$", "marcadores de página"),
    ):
        before_matches = re.findall(pattern, before)
        after_matches = re.findall(pattern, after)
        if name == "callouts":
            before_matches = [match.rstrip() for match in before_matches]
            after_matches = [match.rstrip() for match in after_matches]
        if before_matches != after_matches:
            raise ValueError(f"Conteúdo protegido foi alterado: {name}")
    if sum(line.strip() == "---" for line in before.splitlines()) != sum(
        line.strip() == "---" for line in after.splitlines()
    ):
        raise ValueError("Marcadores horizontais foram alterados")
    if len(re.findall(r"(?m)^>", after)) < len(re.findall(r"(?m)^>", before)):
        raise ValueError("Linhas de citação foram removidas")


def main():
    parser = argparse.ArgumentParser(description="Pós-processamento determinístico de apostilas no perfil Direito")
    parser.add_argument("markdown", type=Path)
    parser.add_argument("--disciplina", choices=("direito", "logica"), required=True)
    parser.add_argument("--pdf", type=Path, help="PDF original; padrão: original.pdf ao lado do Markdown")
    parser.add_argument("--apply", action="store_true", help="Aplica ao Markdown após criar e conferir .bak")
    args = parser.parse_args()

    if args.disciplina != "direito":
        print("Perfil lógica: sem alterações.")
        return

    target = args.markdown
    backup = target.with_suffix(target.suffix + ".bak")
    if not backup.exists():
        shutil.copy2(target, backup)
    elif args.apply and backup.read_bytes() != target.read_bytes():
        raise SystemExit(f"Backup existente diverge do Markdown atual: {backup}")

    source = backup.read_text(encoding="utf-8")
    pdf_path = args.pdf or target.with_name("original.pdf")
    transformed = process(source, _extract_table(pdf_path))
    _validate_preserved_content(source, transformed)
    before, after = _counts(source), _counts(transformed)
    if re.search(r"¬{2,}", transformed) or after["empty_parentheses"]:
        raise SystemExit("Validação falhou: ainda há marcadores '¬¬' ou parênteses vazios")
    if any(line.rstrip().endswith("–") for line in transformed.splitlines()):
        raise SystemExit("Validação falhou: título ainda termina com '–'")

    print(f"Contagens antes: {before}")
    print(f"Contagens depois: {after}")
    print("Diff das linhas alteradas:")
    print("".join(difflib.unified_diff(
        source.splitlines(keepends=True), transformed.splitlines(keepends=True),
        fromfile=str(backup), tofile=str(target), n=0,
    )), end="")

    if args.apply:
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(transformed, encoding="utf-8")
        os.replace(temporary, target)


if __name__ == "__main__":
    main()