#!/usr/bin/env python3
"""Validate positional PDF-to-Markdown output for the sample apostilas."""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from pathlib import Path

import pymupdf


PAGE_MARKER_RE = re.compile(r"(?m)^\*p\. (\d+)\*$")
PAGE_SECTION_RE = re.compile(
    r"\n\n---\n\*p\. \d+\*\n\n---(?:\n\n|$)"
)
IMAGE_RE = re.compile(r"!\[\[([^\]]+)-img\d+-pg(\d+)\.png\]\]")
FORBIDDEN_RE = re.compile(r"(?i)end of page=|www\.|g7juridico|\.com\.br")
EXPECTED_HEADINGS = {
    "dir crianc adolesc a 1": (
        ("####", "DIREITO DA CRIANÇA E DO ADOLESCENTE"),
        ("####", "EVOLUÇÃO DO DIREITO DA CRIANÇA E DO ADOLESCENTE NO BRASIL"),
        ("####", "NORMATIVIDADE INTERNA"),
        ("####", "ESTATUTO DA CRIANÇA E DO ADOLESCENTE - (LEI 8069/1990)"),
        ("####", "PRINCÍPIOS DO DIREITO CRIANÇA E DO ADOLESCENTE — APLICAÇÃO PECULIAR"),
        ("####", "Bloco 5"),
        ("####", "ADOÇÃO"),
    ),
    "raciocinio logico a2": (
        ("####", "8. TABELA-VERDADE DE PROPOSIÇÕES COMPOSTAS"),
        ("#####", "8.1 TAUTOLOGIA"),
        ("#####", "8.2 Forma mais simples de Tautologia"),
        ("######", "7.2.10 Conectivo - SE, E SOMENTE SE - (Bicondicional /Dupla implicação)"),
        ("######", "9.5.1 A PRIMEIRA LEI DE AUGUSTUS DEMORGAN"),
    ),
    "dca7": (
        ("#####", "6.6- Competências do STF"),
        ("####", "A) ROC"),
        ("#####", "A.1) ROC no STF"),
    ),
}


def _canonical_name(path: Path) -> str:
    plain = unicodedata.normalize("NFKD", path.stem.casefold())
    plain = plain.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", plain).strip()


def _body_without_frontmatter(markdown: str) -> str:
    if markdown.startswith("---\n"):
        end = markdown.find("\n---\n", 4)
        if end >= 0:
            return markdown[end + 5 :]
    return markdown


def _check_forbidden_text(body: str, errors: list[str]) -> None:
    body = re.sub(r"(?s)```.*?```", "", body)
    for line_number, line in enumerate(body.splitlines(), 1):
        if line.lstrip().startswith(">"):
            continue
        if FORBIDDEN_RE.search(line):
            errors.append(f"linha {line_number}: resíduo de rodapé: {line.strip()}")


def _check_page_markers(pdf_path: Path, body: str, errors: list[str]) -> int:
    with pymupdf.open(pdf_path) as doc:
        page_count = len(doc)
    pages = [int(number) for number in PAGE_MARKER_RE.findall(body)]
    if len(pages) != page_count:
        errors.append(
            f"marcadores de página: {len(pages)}, páginas no PDF: {page_count}"
        )
    if any(right != left + 1 for left, right in zip(pages, pages[1:])):
        errors.append(f"numeração de páginas não sequencial: {pages[:12]}")
    return page_count


def _check_image_pages(
    pdf_path: Path,
    markdown_path: Path,
    body: str,
    errors: list[str],
) -> None:
    page_contents = PAGE_SECTION_RE.split(body)
    try:
        with pymupdf.open(pdf_path) as doc:
            for page_index, content in enumerate(page_contents):
                for match in IMAGE_RE.finditer(content):
                    physical_page = int(match.group(2))
                    if physical_page != page_index + 1:
                        errors.append(
                            f"imagem {match.group(1)} está na página anotada "
                            f"{page_index + 1}, mas o nome indica {physical_page}"
                        )
                    if physical_page > len(doc):
                        errors.append(
                            f"imagem {match.group(1)} referencia página inexistente"
                        )
    except (OSError, ValueError) as exc:
        errors.append(f"não foi possível conferir imagens do PDF: {exc}")

    embeds = re.findall(r"!\[\[([^\]]+)\]\]", body)
    image_dir = markdown_path.parent / "images"
    missing = [name for name in embeds if not (image_dir / name).is_file()]
    if missing:
        errors.append(f"imagens ausentes em {image_dir}: {missing[:5]}")
    files = {path.name for path in image_dir.glob("*.png")} if image_dir.is_dir() else set()
    if len(embeds) != len(files):
        errors.append(
            f"embeds Markdown ({len(embeds)}) diferem dos PNGs gerados ({len(files)})"
        )


def _normalized_with_offsets(text: str) -> tuple[str, list[int]]:
    normalized = []
    offsets = []
    for offset, char in enumerate(text):
        if char.isalnum():
            normalized.append(char.casefold())
            offsets.append(offset)
    return "".join(normalized), offsets


def _line_overlaps_image(line_bbox: tuple, image_bbox: tuple) -> bool:
    x0 = max(line_bbox[0], image_bbox[0])
    y0 = max(line_bbox[1], image_bbox[1])
    x1 = min(line_bbox[2], image_bbox[2])
    y1 = min(line_bbox[3], image_bbox[3])
    overlap = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    area = max(0.0, line_bbox[2] - line_bbox[0]) * max(
        0.0, line_bbox[3] - line_bbox[1]
    )
    return bool(area and overlap / area >= 0.60)


def _check_native_reading_order(
    pdf_path: Path, body: str, errors: list[str]
) -> None:
    page_contents = PAGE_SECTION_RE.split(body)
    with pymupdf.open(pdf_path) as doc:
        for page_index, page in enumerate(doc):
            if page_index >= len(page_contents):
                break
            page_dict = page.get_text("dict")
            blocks = page_dict.get("blocks", [])
            native_chars = sum(
                len(span.get("text", ""))
                for block in blocks
                if block.get("type") == 0
                for line in block.get("lines", [])
                for span in line.get("spans", [])
            )
            if native_chars < 30:
                continue

            images = sorted(
                (
                    block for block in blocks
                    if block.get("type") == 1
                ),
                key=lambda block: (block["bbox"][1], block["bbox"][0]),
            )
            if not images:
                continue
            content = page_contents[page_index]
            embeds = list(IMAGE_RE.finditer(content))
            if len(embeds) != len(images):
                continue
            for image, embed in zip(images, embeds):
                image_bbox = tuple(image["bbox"])
                position = embed.start()
                before = []
                after = []
                for block in blocks:
                    if block.get("type") != 0:
                        continue
                    for line in block.get("lines", []):
                        text = "".join(
                            span.get("text", "") for span in line.get("spans", [])
                        ).strip()
                        if len(re.sub(r"\W", "", text)) < 8 or _line_overlaps_image(
                            tuple(line.get("bbox", block["bbox"])), image_bbox
                        ):
                            continue
                        y0 = line.get("bbox", block["bbox"])[1]
                        (before if y0 < image_bbox[1] else after).append((y0, text))
                if before:
                    anchor_text = sorted(before, key=lambda item: item[0])[-1][1]
                    anchor, _ = _normalized_with_offsets(anchor_text)
                    prefix, _ = _normalized_with_offsets(content[:position])
                    match = prefix.rfind(anchor)
                    if match < 0:
                        errors.append(
                            f"página {page_index + 1}: embed aparece antes do texto "
                            f"precedente do PDF ({anchor_text[:60]})"
                        )
                if after:
                    anchor_text = sorted(after, key=lambda item: item[0])[0][1]
                    anchor, _ = _normalized_with_offsets(anchor_text)
                    after_embed = content[embed.end() :]
                    suffix, _ = _normalized_with_offsets(after_embed)
                    match = suffix.find(anchor)
                    if match < 0:
                        errors.append(
                            f"página {page_index + 1}: embed aparece depois do texto "
                            f"seguinte do PDF ({anchor_text[:60]})"
                        )


def _check_headings(markdown_path: Path, body: str, errors: list[str]) -> None:
    short_headings = re.findall(r"(?m)^#{1,3}\s+.+$", body)
    if short_headings:
        errors.append(f"headings com menos de 4 hashes: {short_headings[:5]}")
    partial_citations = re.findall(
        r"(?m)^#{4,}\s+.*(?:Art\.\s*\d+|CF/88).*$", body, flags=re.IGNORECASE
    )
    if partial_citations:
        errors.append(f"possível citação em negrito parcial convertida em título: {partial_citations[:3]}")

    expected = EXPECTED_HEADINGS.get(_canonical_name(markdown_path), ())
    for hashes, title in expected:
        if f"{hashes} {title}" not in body:
            errors.append(f"heading esperado ausente: {hashes} {title}")

    summary_headings = re.findall(r"(?im)^#{4,}\s+sumário\s*$", body)
    if len(summary_headings) > 1:
        errors.append(f"Sumário aparece {len(summary_headings)} vezes no corpo")


def _check_assunto(markdown: str, pdf_path: Path, errors: list[str]) -> None:
    match = re.search(
        r"(?m)^assunto:\s*\|-\s*\n(?P<items>(?:[ \t]+.*(?:\n|$))*)",
        markdown,
    )
    items = [
        line.strip()
        for line in match.group("items").splitlines()
        if line.strip()
    ] if match else []
    if not items:
        errors.append("o campo assunto do front matter está vazio")
        return
    if len(items) != len(set(items)):
        errors.append("itens duplicados no campo assunto")
    canonical = _canonical_name(pdf_path)
    if "dir crianc adolesc a 1" in canonical and len(items) != 11:
        errors.append(f"assunto de DIR deve ter 11 itens; encontrado: {len(items)}")


def verify_pair(pdf_path: Path, markdown_path: Path) -> list[str]:
    errors = []
    try:
        markdown = markdown_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"não foi possível ler {markdown_path}: {exc}"]
    body = _body_without_frontmatter(markdown)
    page_count = _check_page_markers(pdf_path, body, errors)
    _check_forbidden_text(body, errors)
    _check_image_pages(pdf_path, markdown_path, body, errors)
    _check_native_reading_order(pdf_path, body, errors)
    _check_headings(markdown_path, body, errors)
    _check_assunto(markdown, pdf_path, errors)
    embed_count = len(re.findall(r"!\[\[", body))
    print(
        f"{pdf_path.name}: {page_count} páginas; "
        f"{embed_count} embeds; "
        f"{'OK' if not errors else f'{len(errors)} erro(s)'}"
    )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Valida a paginação, imagens, headings e resíduos do Markdown gerado."
    )
    parser.add_argument(
        "--pair",
        nargs=2,
        action="append",
        metavar=("PDF", "MD"),
        required=True,
        help="par PDF/Markdown; pode ser passado várias vezes",
    )
    args = parser.parse_args()
    failures = []
    for pdf_name, markdown_name in args.pair:
        failures.extend(
            f"{markdown_name}: {error}"
            for error in verify_pair(Path(pdf_name), Path(markdown_name))
        )
    if failures:
        print("\n".join(f"ERRO: {failure}" for failure in failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
