import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import pdf_compression


class PdfCompressionTests(unittest.TestCase):
    def test_levels_map_to_existing_ghostscript_presets(self):
        self.assertEqual(
            pdf_compression.COMPRESSION_LEVELS,
            {"menor": "prepress", "media": "ebook", "maior": "screen"},
        )

    def test_uses_reference_ghostscript_options_for_each_level(self):
        for level, preset in pdf_compression.COMPRESSION_LEVELS.items():
            with self.subTest(level=level), patch("pdf_compression.subprocess.run") as run:
                self.assertTrue(pdf_compression.compress_pdf("source.pdf", "result.pdf", preset))
                command = run.call_args.args[0]
                self.assertIn(f"-dPDFSETTINGS=/{preset}", command)
                self.assertIn("-sDEVICE=pdfwrite", command)
                self.assertEqual(command[-2:], ["-sOutputFile=result.pdf", "source.pdf"])
                run.assert_called_once_with(command, check=True, timeout=120)


if __name__ == "__main__":
    unittest.main()
