"""Adaptação isolada do helper Ghostscript usado no exemplo de compressão."""

import subprocess


COMPRESSION_LEVELS = {
    "menor": "prepress",
    "media": "ebook",
    "maior": "screen",
}


def compress_pdf(input_path: str, output_path: str, power: str = "ebook") -> bool:
    """Comprime PDF com as mesmas opções Ghostscript do script de referência."""
    gs_command = [
        "gs", "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.4",
        f"-dPDFSETTINGS=/{power}", "-dNOPAUSE", "-dQUIET", "-dBATCH",
        f"-sOutputFile={output_path}", input_path,
    ]
    try:
        subprocess.run(gs_command, check=True, timeout=120)
        return True
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, OSError):
        return False
