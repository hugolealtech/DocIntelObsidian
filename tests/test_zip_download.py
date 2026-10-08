import asyncio
import io
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))


class ZipDownloadTests(unittest.TestCase):
    def test_zip_keeps_existing_files_adds_original_and_applies_selected_preset(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = os.path.join(temp_dir, "output")
            uploads_dir = os.path.join(temp_dir, "uploads")
            os.makedirs(output_dir)
            os.makedirs(uploads_dir)
            with patch.dict(os.environ, {"DOCINTEL_OUTPUT": output_dir, "DOCINTEL_UPLOADS": uploads_dir}):
                import main

            job_dir = os.path.join(output_dir, "job")
            image_dir = os.path.join(job_dir, "images")
            os.makedirs(image_dir)
            paths = {
                "original.pdf": b"original-pdf",
                "lesson.md": b"markdown",
                "images/figure.png": b"image",
            }
            for relative_path, content in paths.items():
                absolute_path = os.path.join(job_dir, relative_path)
                os.makedirs(os.path.dirname(absolute_path), exist_ok=True)
                with open(absolute_path, "wb") as file:
                    file.write(content)

            job = {
                "job_dir": job_dir,
                "md_path": os.path.join(job_dir, "lesson.md"),
                "original_filename": "lesson-original.pdf",
            }
            presets = []

            def fake_compress(source, destination, power):
                presets.append(power)
                shutil.copyfile(source, destination)
                return True

            async def response_body(response):
                return b"".join([chunk async for chunk in response.body_iterator])

            for level, expected_preset in main.COMPRESSION_LEVELS.items():
                with self.subTest(level=level), patch.object(main.db, "get_job", return_value=job), patch.object(
                    main, "compress_pdf", side_effect=fake_compress
                ):
                    response = main.job_download_zip("job-id", level)
                    archive_bytes = asyncio.run(response_body(response))
                    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                        self.assertEqual(
                            set(archive.namelist()),
                            {"lesson.md", "images/figure.png", "lesson-original.pdf"},
                        )
                        self.assertEqual(archive.read("lesson.md"), b"markdown")
                        self.assertEqual(archive.read("images/figure.png"), b"image")
                        self.assertEqual(archive.read("lesson-original.pdf"), b"original-pdf")
                self.assertEqual(presets[-1], expected_preset)


if __name__ == "__main__":
    unittest.main()
