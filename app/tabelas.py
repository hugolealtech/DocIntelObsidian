"""Extração geométrica e junção de tabelas de apostilas PDF."""

from __future__ import annotations

import re
import statistics

import pymupdf


CELL_LINE_Y_TOLERANCE = 0.40
CELL_WORD_GAP_RATIO = 0.15
TABLE_BOUNDARY_TOLERANCE = 3.0
TABLE_CONTINUATION_BOTTOM_RATIO = 0.85
TABLE_CONTINUATION_TOP_RATIO = 0.15
TABLE_MERGED_CELL_FALLBACK_RATIO = 0.30
TABLE_GLUE_CELL_FALLBACK_RATIO = 0.05
TABLE_GLUE_AVERAGE_CHARACTERS = 18.0
TABLE_RENDER_DPI_SCALE = 2.0

_ITEM_START_RE = re.compile(r"^(?:[-→]|(?:\d+\s*[-.)]))\s*")
_PUNCTUATION_RE = re.compile(r"^[.,;:!?%)\]}»”’]+$")


def extrair_tabelas_pagina(page) -> list[dict]:
    """Extrai grade e texto de cada tabela da página usando coordenadas."""
    extracted = []
    for table in page.find_tables().tables:
        rows = _extract_rows(page, table)
        if len(rows) < 2:
            continue
        column_count = table.col_count
        if column_count < 2:
            continue

        nonempty_columns = [
            index
            for index in range(column_count)
            if any(row["cells"][index] for row in rows)
        ]
        if len(nonempty_columns) < 2:
            continue

        nonempty_cells = [
            cell
            for row in rows
            for cell in row["cells"]
            if cell
        ]
        suspicious = sum(
            _average_characters_per_word(cell) > TABLE_GLUE_AVERAGE_CHARACTERS
            for cell in nonempty_cells
        )
        suspicious_ratio = suspicious / len(nonempty_cells) if nonempty_cells else 0
        grid_x = _table_grid_x(table)
        extracted.append(
            {
                "page_index": page.number,
                "page_height": page.rect.height,
                "bbox": tuple(table.bbox),
                "grid_x": grid_x,
                "column_count": column_count,
                "header": rows[0]["cells"],
                "rows": rows,
                "merged_cell_ratio": _merged_cell_ratio(table),
                "glued_cell_ratio": suspicious_ratio,
                "fallback_image": (
                    _merged_cell_ratio(table) > TABLE_MERGED_CELL_FALLBACK_RATIO
                    or suspicious_ratio >= TABLE_GLUE_CELL_FALLBACK_RATIO
                ),
            }
        )
    return extracted


def unir_tabelas_entre_paginas(tabelas_por_pagina: list[list[dict]]) -> list[dict]:
    """Combina grades contíguas e posiciona o Markdown na página final."""
    groups: list[dict] = []
    for page_tables in tabelas_por_pagina:
        for table_index, table in enumerate(page_tables):
            current = _new_group(table)
            previous = groups[-1] if groups else None
            is_boundary_pair = (
                previous is not None
                and previous["end_page"] == table["page_index"] - 1
                and table_index == 0
                and _is_page_continuation(previous, table)
            )
            if not is_boundary_pair:
                groups.append(current)
                continue

            first_row = table["rows"][0]["cells"]
            if _same_row(first_row, previous["header"]):
                continuation_rows = table["rows"][1:]
            elif _is_continuation_row(first_row):
                _append_continuation(previous["rows"][-1], first_row)
                continuation_rows = table["rows"][1:]
            else:
                continuation_rows = table["rows"]
            previous["rows"].extend(continuation_rows)
            previous["end_page"] = table["page_index"]
            previous["parts"].append(table)
            previous["fallback_image"] = (
                previous["fallback_image"] or table["fallback_image"]
            )

    for group in groups:
        _remove_blank_columns(group)
        group["markdown"] = _render_markdown(group["rows"])
        group["render_page"] = group["end_page"]
        group["render_y"] = group["parts"][-1]["bbox"][1]
        group["fallback_text"] = _plain_table_text(group["rows"])
    return groups


def is_compact_complex_table(table) -> bool:
    """Regra de compatibilidade do perfil Direito: tabelas compactas viram imagem."""
    rows = table.extract()
    return bool(rows) and len(rows) <= 3 and max(len(row) for row in rows) >= 4


def _extract_rows(page, table) -> list[dict]:
    rows = []
    for table_row in table.rows:
        cells = []
        merged_count = 0
        for cell_bbox in table_row.cells:
            if cell_bbox is None:
                cells.append("")
                merged_count += 1
                continue
            text = _extract_cell_text(page, cell_bbox)
            cells.append(text.replace("|", r"\|"))
        if not any(cell.strip() for cell in cells):
            continue
        rows.append({"cells": cells, "merged_cells": merged_count})
    return rows


def _extract_cell_text(page, bbox) -> str:
    rect = pymupdf.Rect(bbox)
    words = page.get_text("words", clip=rect)
    raw = page.get_text("rawdict", clip=rect)
    chars = [
        {
            "text": char.get("c", ""),
            "bbox": tuple(char.get("bbox", (0, 0, 0, 0))),
            "size": float(span.get("size", 0)),
        }
        for block in raw.get("blocks", [])
        if block.get("type") == 0
        for line in block.get("lines", [])
        for span in line.get("spans", [])
        for char in span.get("chars", [])
        if char.get("c", "")
    ]
    if chars:
        lines = _group_char_lines(chars)
        text_lines = [_join_chars(line) for line in lines]
    else:
        text_lines = _words_as_lines(words)
    text = _format_cell_lines(text_lines)
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)
    text = re.sub(
        r"(?i)(\bart\.\s*7º,\s*(?:I{1,3}|IV|V))\s+(do CP\b)",
        r"\1, \2",
        text,
    )
    return re.sub(r"(?i)(§\s*3º)\s+(do CP\b)", r"\1, \2", text)


def _group_char_lines(chars: list[dict]) -> list[list[dict]]:
    ordered = sorted(
        chars,
        key=lambda char: (
            (char["bbox"][1] + char["bbox"][3]) / 2,
            char["bbox"][0],
        ),
    )
    groups: list[list[dict]] = []
    for char in ordered:
        center_y = (char["bbox"][1] + char["bbox"][3]) / 2
        height = max(1.0, char["bbox"][3] - char["bbox"][1])
        tolerance = height * CELL_LINE_Y_TOLERANCE
        matching = next(
            (
                group
                for group in reversed(groups)
                if abs(group["center_y"] - center_y)
                <= max(tolerance, group["tolerance"])
            ),
            None,
        )
        if matching is None:
            groups.append(
                {
                    "center_y": center_y,
                    "tolerance": tolerance,
                    "chars": [char],
                }
            )
        else:
            matching["chars"].append(char)
            matching["center_y"] = statistics.mean(
                (matching["center_y"], center_y)
            )
            matching["tolerance"] = max(matching["tolerance"], tolerance)
    return [
        sorted(group["chars"], key=lambda char: char["bbox"][0])
        for group in groups
    ]


def _join_chars(chars: list[dict]) -> str:
    result: list[str] = []
    previous = None
    for char in chars:
        value = char["text"]
        if value.isspace():
            if result and not result[-1].endswith(" "):
                result.append(" ")
            previous = char
            continue

        if _PUNCTUATION_RE.match(value) and result:
            result[-1] = result[-1].rstrip()

        if previous is not None and result and not result[-1].endswith(" "):
            gap = char["bbox"][0] - previous["bbox"][2]
            font_size = max(previous["size"], char["size"], 1.0)
            previous_text = previous["text"]
            if (
                gap >= font_size * CELL_WORD_GAP_RATIO
                and not _PUNCTUATION_RE.match(value)
                and not _PUNCTUATION_RE.match(previous_text)
            ):
                result.append(" ")
        result.append(value)
        previous = char
    return "".join(result).strip()


def _words_as_lines(words: list[tuple]) -> list[str]:
    ordered = sorted(words, key=lambda word: ((word[1] + word[3]) / 2, word[0]))
    lines: list[list[tuple]] = []
    for word in ordered:
        center_y = (word[1] + word[3]) / 2
        tolerance = max(1.0, (word[3] - word[1]) * CELL_LINE_Y_TOLERANCE)
        if lines and abs(lines[-1][0] - center_y) <= tolerance:
            lines[-1][1].append(word)
        else:
            lines.append((center_y, [word]))
    result = []
    for _, row in lines:
        row.sort(key=lambda word: word[0])
        values = []
        for word in row:
            value = word[4]
            if values and _PUNCTUATION_RE.match(value):
                values[-1] += value
            else:
                values.append(value)
        result.append(" ".join(values))
    return result


def _format_cell_lines(lines: list[str]) -> str:
    result = ""
    for line in lines:
        text = re.sub(r"\s+", " ", line).strip()
        if not text:
            continue
        if _ITEM_START_RE.match(text):
            text = re.sub(r"^-\s*", "- ", text, count=1)
            result += ("<br>" if result else "") + text
        elif result:
            result += " " + text
        else:
            result = text
    return result


def _table_grid_x(table) -> tuple[float, ...]:
    coordinates = sorted(
        coordinate
        for row in table.rows
        for cell in row.cells
        if cell is not None
        for coordinate in (cell[0], cell[2])
    )
    edges: list[float] = []
    for coordinate in coordinates:
        if not edges or coordinate - edges[-1] > TABLE_BOUNDARY_TOLERANCE:
            edges.append(coordinate)
        else:
            edges[-1] = statistics.mean((edges[-1], coordinate))
    return tuple(edges)


def _merged_cell_ratio(table) -> float:
    cells = [cell for row in table.rows for cell in row.cells]
    return sum(cell is None for cell in cells) / len(cells) if cells else 0.0


def _average_characters_per_word(text: str) -> float:
    plain = re.sub(r"<br>", " ", text, flags=re.IGNORECASE)
    words = re.findall(r"[^\W_]+", plain, flags=re.UNICODE)
    if not words:
        return 0.0
    return sum(len(word) for word in words) / len(words)


def _new_group(table: dict) -> dict:
    return {
        "header": list(table["header"]),
        "rows": [dict(row, cells=list(row["cells"])) for row in table["rows"]],
        "start_page": table["page_index"],
        "end_page": table["page_index"],
        "parts": [table],
        "fallback_image": table["fallback_image"],
    }


def _is_page_continuation(previous: dict, current: dict) -> bool:
    old = previous["parts"][-1]
    new = current
    if old["bbox"][3] < old["page_height"] * TABLE_CONTINUATION_BOTTOM_RATIO:
        return False
    if new["bbox"][1] > new["page_height"] * TABLE_CONTINUATION_TOP_RATIO:
        return False
    if old["column_count"] != new["column_count"]:
        return False
    if len(old["grid_x"]) != len(new["grid_x"]):
        return False
    if any(
        abs(left - right) > TABLE_BOUNDARY_TOLERANCE
        for left, right in zip(old["grid_x"], new["grid_x"])
    ):
        return False
    first_row = current["rows"][0]["cells"]
    if _same_row(first_row, previous["header"]):
        return True
    return not _looks_like_new_table_header(first_row)


def _same_row(left: list[str], right: list[str]) -> bool:
    normalize = lambda value: re.sub(r"\s+", " ", value).strip().casefold()
    return len(left) == len(right) and all(
        normalize(a) == normalize(b) for a, b in zip(left, right)
    )


def _looks_like_new_table_header(cells: list[str]) -> bool:
    nonempty = [cell for cell in cells if cell.strip()]
    if len(nonempty) != len(cells) or not nonempty:
        return False
    if any(_ITEM_START_RE.match(cell.strip()) for cell in nonempty):
        return False
    return all(len(cell) <= 90 for cell in nonempty)


def _is_continuation_row(cells: list[str]) -> bool:
    if any(not cell.strip() for cell in cells):
        return True
    return not _looks_like_new_table_header(cells)


def _append_continuation(target: dict, cells: list[str]) -> None:
    target_cells = target["cells"]
    for index, cell in enumerate(cells):
        if not cell.strip():
            continue
        if index >= len(target_cells):
            continue
        target_cells[index] = (
            f"{target_cells[index]}<br>{cell}"
            if target_cells[index]
            else cell
        )


def _remove_blank_columns(group: dict) -> None:
    rows = group["rows"]
    width = max((len(row["cells"]) for row in rows), default=0)
    keep = [
        index
        for index in range(width)
        if any(
            index < len(row["cells"]) and row["cells"][index].strip()
            for row in rows
        )
    ]
    group["header"] = [
        group["header"][index]
        for index in keep
        if index < len(group["header"])
    ]
    for row in rows:
        row["cells"] = [
            row["cells"][index]
            for index in keep
            if index < len(row["cells"])
        ]


def _render_markdown(rows: list[dict]) -> str:
    if not rows:
        return ""
    width = max(len(row["cells"]) for row in rows)
    rendered = [
        "|" + "|".join(
            row["cells"] + [""] * (width - len(row["cells"]))
        ) + "|"
        for row in rows
    ]
    rendered.insert(1, "|" + "|".join("---" for _ in range(width)) + "|")
    return "\n".join(rendered)


def _plain_table_text(rows: list[dict]) -> str:
    return "\n".join(
        " | ".join(cell.replace("<br>", "\n") for cell in row["cells"] if cell)
        for row in rows
        if any(row["cells"])
    )
