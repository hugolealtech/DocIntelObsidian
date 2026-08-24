"""
Processo isolado que executa uma única conversão PDF -> Markdown.

Rodar cada conversão em um processo próprio (em vez de dentro do processo
do servidor FastAPI) tem dois motivos:
  1. Cancelamento de verdade: matar o processo interrompe a conversão na
     hora, mesmo no meio de um OCR. Isso não é possível com threads Python.
  2. Isolamento: um PDF corrompido ou malformado que trave/derrube o
     processo não derruba o servidor nem os outros jobs em andamento.

Uso: python3 worker_convert.py <input_path> <job_dir> <meta_json_path> <result_json_path>
Escreve o resultado (md_path, n_images) como JSON em result_json_path -- não
no stdout, porque o pymupdf4llm imprime mensagens de diagnóstico ali mesmo
e misturaria com a saída.
Código de saída != 0 em caso de erro, com a mensagem no stderr.
"""

import json
import sys

import converter


def main() -> int:
    if len(sys.argv) != 5:
        print("uso: worker_convert.py <input_path> <job_dir> <meta_json_path> <result_json_path>", file=sys.stderr)
        return 2

    input_path, job_dir, meta_json_path, result_json_path = sys.argv[1:5]

    try:
        with open(meta_json_path, encoding="utf-8") as f:
            meta = json.load(f)
    except OSError as exc:
        print(f"não consegui ler os metadados: {exc}", file=sys.stderr)
        return 1

    try:
        result = converter.convert_pdf(input_path, job_dir, meta)
    except Exception as exc:  # noqa: BLE001 -- worker isolado, precisa reportar qualquer falha
        print(str(exc), file=sys.stderr)
        return 1

    with open(result_json_path, "w", encoding="utf-8") as f:
        json.dump(result, f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
