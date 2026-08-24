"""
Processo isolado que executa uma única conversão de legislação (PDF ->
Markdown estruturado por artigo). Arquivo próprio, paralelo ao
worker_convert.py padrão -- mesmo motivo (cancelamento real + isolamento),
mas chamando converter_legislacao.py em vez de converter.py.

Uso: python3 worker_convert_legislacao.py <input_path> <job_dir> <meta_json_path> <result_json_path>
Escreve o resultado (md_path, n_artigos) como JSON em result_json_path.
Código de saída != 0 em caso de erro, com a mensagem no stderr.
"""

import json
import sys

import converter_legislacao


def main() -> int:
    if len(sys.argv) != 5:
        print(
            "uso: worker_convert_legislacao.py <input_path> <job_dir> <meta_json_path> <result_json_path>",
            file=sys.stderr,
        )
        return 2

    input_path, job_dir, meta_json_path, result_json_path = sys.argv[1:5]

    try:
        with open(meta_json_path, encoding="utf-8") as f:
            meta = json.load(f)
    except OSError as exc:
        print(f"não consegui ler os metadados: {exc}", file=sys.stderr)
        return 1

    try:
        result = converter_legislacao.converter_pdf_legislacao(input_path, job_dir, meta)
    except Exception as exc:  # noqa: BLE001 -- worker isolado, precisa reportar qualquer falha
        print(str(exc), file=sys.stderr)
        return 1

    with open(result_json_path, "w", encoding="utf-8") as f:
        json.dump(result, f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
