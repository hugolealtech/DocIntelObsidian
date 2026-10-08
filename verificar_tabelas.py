#!/usr/bin/env python3
"""Valida a tabela de extraterritorialidade do PDF de Direito Penal."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


PAGE_MARKER_RE = re.compile(r"(?m)^\*p\. (\d+)\*$")
TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
EXPECTED_HEADERS = (
    "Crimes do art. 7º, I, do CP",
    "Crimes do art. 7º, II, do CP",
    "Crimes do art. 7º, §3º, do CP",
)


def _without_frontmatter(markdown: str) -> str:
    if markdown.startswith("---\n"):
        end = markdown.find("\n---\n", 4)
        if end >= 0:
            return markdown[end + 5 :]
    return markdown


def _table_blocks(body: str) -> list[tuple[int, int, list[str]]]:
    lines = body.splitlines()
    blocks = []
    index = 0
    while index < len(lines):
        if not TABLE_ROW_RE.match(lines[index]):
            index += 1
            continue
        start = index
        while index < len(lines) and TABLE_ROW_RE.match(lines[index]):
            index += 1
        blocks.append((start, index, lines[start:index]))
    return blocks


def _cells(row: str) -> list[str]:
    content = row.strip()
    if content.startswith("|") and content.endswith("|"):
        content = content[1:-1]
    return [cell.strip() for cell in content.split("|")]


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def verify_table_pair(pdf_path: Path, markdown_path: Path) -> list[str]:
    errors = []
    try:
        markdown = markdown_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"não foi possível ler {markdown_path}: {exc}"]
    body = _without_frontmatter(markdown)
    blocks = _table_blocks(body)
    target_blocks = [
        (start, end, block)
        for start, end, block in blocks
        if any(EXPECTED_HEADERS[0] in row for row in block)
    ]
    if len(target_blocks) != 1:
        return [
            "esperada uma única tabela Markdown de extraterritorialidade; "
            f"encontradas {len(target_blocks)}"
        ]

    start, end, rows = target_blocks[0]
    header = _cells(rows[0])
    if len(header) != 3 or any(
        _normalized(expected) not in _normalized(actual)
        for expected, actual in zip(EXPECTED_HEADERS, header)
    ):
        errors.append(f"cabeçalho esperado de 3 colunas ausente: {header}")
    if len(rows) != 9:
        errors.append(f"esperados 7 dados + cabeçalho + separador; linhas={len(rows)}")
    widths = [len(_cells(row)) for row in rows]
    if any(width != 3 for width in widths):
        errors.append(f"tabela não tem 3 células em todas as linhas: {widths}")
    if any(re.fullmatch(r"\|\s*\|\s*\|\s*\|", row) for row in rows):
        errors.append("linha vazia `|||` encontrada na tabela")
    if any(
        all(not _cells(row)[column] for row in rows[2:])
        for column in range(3)
    ):
        errors.append("coluna 100% vazia encontrada")

    body_cells = [
        cell
        for row in rows[2:]
        for cell in _cells(row)
    ]
    word_ratios = []
    for cell in body_cells:
        plain = re.sub(r"<br\s*/?>", " ", cell, flags=re.IGNORECASE)
        words = re.findall(r"[^\W_]+", plain)
        if words:
            word_ratios.append(sum(map(len, words)) / len(words))
        if re.search(r"\b(?:Crimesdoart|Vejaquenão|princípiodajustiça)\b", cell, re.I):
            errors.append(f"palavras coladas detectadas: {cell[:100]}")
        if re.search(r"\s[.,](?:\s|$)", cell):
            errors.append(f"pontuação separada da palavra anterior: {cell[:100]}")
        if re.search(
            r"(?<!<br>)(?:^| )-\s+(?:Veja|Crime|Esse|Trata-se|Ex\.?|Art\.)",
            cell,
            re.IGNORECASE,
        ):
            errors.append(f"item de lista sem quebra `<br>`: {cell[:100]}")
    if word_ratios and sum(word_ratios) / len(word_ratios) > 14:
        errors.append(
            f"média de caracteres por palavra acima de 14: "
            f"{sum(word_ratios) / len(word_ratios):.2f}"
        )

    pages = [
        (int(match.group(1)), body[: match.start()].count("\n"))
        for match in PAGE_MARKER_RE.finditer(body)
    ]
    marker_positions = {
        number: position for number, position in pages
    }
    if not (
        marker_positions.get(15, len(body)) < start
        and marker_positions.get(16, len(body)) < start
        and start < marker_positions.get(17, -1)
    ):
        errors.append("marcadores p. 15–17 não delimitam a tabela completa em p. 17")
    if re.search(r"(?m)^\*p\. \d+\*$", "\n".join(rows)):
        errors.append("marcador de página dentro da tabela")
    if start >= end:
        errors.append("bloco de tabela vazio")

    if not pdf_path.is_file():
        errors.append(f"PDF de origem não encontrado: {pdf_path}")
    print(
        f"{pdf_path.name}: tabela verificada; "
        f"{'OK' if not errors else f'{len(errors)} erro(s)'}"
    )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Valida a tabela contínua de extraterritorialidade nas pp. 15–17."
    )
    parser.add_argument("--pair", nargs=2, required=True, metavar=("PDF", "MD"))
    args = parser.parse_args()
    errors = verify_table_pair(Path(args.pair[0]), Path(args.pair[1]))
    if errors:
        print("\n".join(f"ERRO: {error}" for error in errors), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
