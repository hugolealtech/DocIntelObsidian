import os
import tempfile
import unittest

from app import tabelas


class TableExtractionTests(unittest.TestCase):
    def test_extracts_text_by_character_geometry_and_keeps_list_breaks(self):
        import pymupdf

        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = os.path.join(temp_dir, "cells.pdf")
            doc = pymupdf.open()
            page = doc.new_page(width=360, height=260)
            x_edges = (30, 180, 330)
            y_edges = (50, 80, 210)
            for x in x_edges:
                page.draw_line((x, y_edges[0]), (x, y_edges[-1]))
            for y in y_edges:
                page.draw_line((x_edges[0], y), (x_edges[-1], y))
            page.insert_text((38, 68), "Coluna A")
            page.insert_text((188, 68), "Coluna B")
            page.insert_text((38, 102), "Crimes do art. 7º, I, do CP")
            page.insert_text((38, 122), "- Veja a regra")
            page.insert_text((38, 142), "continua na linha seguinte")
            page.insert_text((188, 102), "Texto | protegido")
            doc.save(pdf_path)
            doc.close()

            with pymupdf.open(pdf_path) as saved:
                tables = tabelas.extrair_tabelas_pagina(saved[0])

        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0]["column_count"], 2)
        self.assertEqual(
            tables[0]["rows"][1]["cells"][0],
            "Crimes do art. 7º, I, do CP<br>- Veja a regra continua na linha seguinte",
        )
        self.assertEqual(tables[0]["rows"][1]["cells"][1], r"Texto \| protegido")

    def test_joins_page_continuations_into_one_table_using_first_header(self):
        def table(page_index, y0, y1, rows):
            return {
                "page_index": page_index,
                "page_height": 100,
                "bbox": (10, y0, 90, y1),
                "grid_x": (10, 35, 60, 90),
                "column_count": 3,
                "header": rows[0]["cells"],
                "rows": rows,
                "fallback_image": False,
            }

        first = table(
            0,
            80,
            98,
            [
                {"cells": ["I", "II", "III"]},
                {"cells": ["Crime do Presidente", "Conteúdo II", "Conteúdo III"]},
            ],
        )
        second = table(
            1,
            1,
            90,
            [
                {"cells": ["- continuação", "", ""]},
                {"cells": ["Linha 2", "Linha 2b", "Linha 2c"]},
            ],
        )
        third = table(
            2,
            1,
            90,
            [
                {"cells": ["- fim", "Texto contínuo", ""]},
                {"cells": ["Linha 3", "", "Linha 3c"]},
            ],
        )

        merged = tabelas.unir_tabelas_entre_paginas([[first], [second], [third]])

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["render_page"], 2)
        self.assertEqual(len(merged[0]["rows"]), 4)
        self.assertIn(
            "Crime do Presidente<br>- continuação",
            merged[0]["rows"][1]["cells"][0],
        )
        self.assertIn("- fim", merged[0]["rows"][2]["cells"][0])
        self.assertIn("Texto contínuo", merged[0]["rows"][2]["cells"][1])
        self.assertEqual(merged[0]["markdown"].count("|---|---|---|"), 1)
        self.assertEqual(merged[0]["markdown"].count("\n"), 4)

    def test_does_not_join_tables_with_a_different_grid(self):
        first = {
            "page_index": 0,
            "page_height": 100,
            "bbox": (10, 80, 90, 99),
            "grid_x": (10, 35, 60, 90),
            "column_count": 3,
            "header": ["A", "B", "C"],
            "rows": [{"cells": ["A", "B", "C"]}, {"cells": ["1", "2", "3"]}],
            "fallback_image": False,
        }
        second = {
            **first,
            "page_index": 1,
            "bbox": (10, 1, 90, 80),
            "grid_x": (10, 30, 60, 90),
            "header": ["D", "E", "F"],
            "rows": [{"cells": ["D", "E", "F"]}, {"cells": ["4", "5", "6"]}],
        }
        self.assertEqual(
            len(tabelas.unir_tabelas_entre_paginas([[first], [second]])), 2
        )
