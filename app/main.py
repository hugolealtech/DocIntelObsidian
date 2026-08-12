"""
docintel -- conversor local de PDF para Markdown/Obsidian.

- POST   /api/convert                envia um ou mais PDFs
- GET    /api/jobs                    lista os jobs (mais recentes primeiro)
- GET    /api/jobs/{job_id}           detalhe de um job
- POST   /api/jobs/{job_id}/cancel    cancela um job pendente ou em andamento
- GET    /api/jobs/{job_id}/download-zip   baixa .md + imagens em um .zip
- GET    /output/...                  arquivos gerados, servidos direto
- GET    /                            interface web
"""

import datetime
import io
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import uuid
import zipfile

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import converter
import db

UPLOADS_DIR = os.environ.get("DOCINTEL_UPLOADS", "/data/uploads")
OUTPUT_DIR = os.environ.get("DOCINTEL_OUTPUT", "/data/output")
N_WORKERS = int(os.environ.get("DOCINTEL_WORKERS", "2"))
WORKER_SCRIPT = os.path.join(os.path.dirname(__file__), "worker_convert.py")

# Prazo de tolerância entre terminate() (SIGTERM) e kill() (SIGKILL) forçado,
# caso o processo não responda ao pedido educado de encerrar.
CANCEL_GRACE_SECONDS = 4

os.makedirs(UPLOADS_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

app = FastAPI(title="docintel")

_job_queue: "queue.Queue[str]" = queue.Queue()

_cancel_lock = threading.Lock()
_cancelled_ids: set = set()

_proc_lock = threading.Lock()
_running_procs: dict = {}


def _parse_tags(raw: str) -> list:
    return [t.strip() for t in (raw or "").split(",") if t.strip()]


def _remove_file_silently(path) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _run_job(job_id: str) -> None:
    with _cancel_lock:
        if job_id in _cancelled_ids:
            _cancelled_ids.discard(job_id)
            job = db.get_job(job_id)
            db.update_job(job_id, status="cancelado")
            if job:
                _remove_file_silently(job.get("upload_path"))
            return

    job = db.get_job(job_id)
    if job is None:
        return

    db.update_job(job_id, status="processando")

    job_dir = os.path.join(OUTPUT_DIR, f"{job['slug']}-{job['file_hash']}")
    disciplina = (job["disciplina"] or "").strip()
    aula_num = (job["aula"] or "").strip() or converter.guess_aula_numero(job["original_filename"])
    caminho = f"{disciplina} Aula {aula_num}" if disciplina and aula_num else disciplina

    meta = {
        "slug": job["slug"],
        "display_name": converter.display_name(job["original_filename"]),
        "disciplina": disciplina,
        "professora": job["professora"],
        "caminho": caminho,
        "tags": job["tags"],
        "data_criacao": datetime.date.today().isoformat(),
    }
    meta_path = job["upload_path"] + ".meta.json"
    result_path = job["upload_path"] + ".result.json"
    try:
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f)

        cmd = [sys.executable, WORKER_SCRIPT, job["upload_path"], job_dir, meta_path, result_path]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        with _proc_lock:
            _running_procs[job_id] = proc

        _stdout, stderr = proc.communicate()

        with _proc_lock:
            _running_procs.pop(job_id, None)

        with _cancel_lock:
            foi_cancelado = job_id in _cancelled_ids
            _cancelled_ids.discard(job_id)

        if foi_cancelado:
            db.update_job(job_id, status="cancelado")
        elif proc.returncode == 0:
            try:
                with open(result_path, encoding="utf-8") as f:
                    result = json.load(f)
                db.update_job(
                    job_id,
                    status="concluido",
                    job_dir=job_dir,
                    md_path=result["md_path"],
                    n_images=result["n_images"],
                    error_message=None,
                )
            except (OSError, json.JSONDecodeError, KeyError) as exc:
                db.update_job(job_id, status="erro", error_message=f"saída inválida do worker: {exc}")
        else:
            msg = (stderr or "").strip() or "processo de conversão falhou sem detalhes"
            db.update_job(job_id, status="erro", error_message=msg[:2000])
    finally:
        _remove_file_silently(meta_path)
        _remove_file_silently(result_path)
        _remove_file_silently(job.get("upload_path"))


def _worker_loop() -> None:
    while True:
        job_id = _job_queue.get()
        try:
            _run_job(job_id)
        finally:
            _job_queue.task_done()


@app.on_event("startup")
def on_startup() -> None:
    db.init_db()
    for stuck in db.list_stuck_jobs():
        # o container foi reiniciado no meio do processamento: volta pra fila
        db.update_job(stuck["id"], status="pendente")
        _job_queue.put(stuck["id"])
    for _ in range(N_WORKERS):
        threading.Thread(target=_worker_loop, daemon=True).start()


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "workers": N_WORKERS, "fila": _job_queue.qsize()}


@app.post("/api/convert")
async def convert(
    files: list[UploadFile] = File(...),
    disciplina: str = Form(""),
    professora: str = Form(""),
    aula: str = Form(""),
    tags: str = Form(""),
) -> JSONResponse:
    tag_list = _parse_tags(tags)
    created = []

    for upload in files:
        original_filename = upload.filename or "documento.pdf"
        if not original_filename.lower().endswith(".pdf"):
            created.append(
                {"original_filename": original_filename, "erro": "não é um PDF, ignorado"}
            )
            continue

        job_id = uuid.uuid4().hex[:12]
        slug = converter.slugify(original_filename)
        upload_path = os.path.join(UPLOADS_DIR, f"{job_id}__{original_filename}")

        with open(upload_path, "wb") as f:
            shutil.copyfileobj(upload.file, f)

        file_hash = converter.file_hash(upload_path)

        db.create_job(
            job_id=job_id,
            original_filename=original_filename,
            slug=slug,
            file_hash=file_hash,
            upload_path=upload_path,
            disciplina=disciplina,
            professora=professora,
            aula=aula,
            tags=tag_list,
        )

        existing = db.find_done_by_hash(file_hash)
        if existing:
            # mesmo conteúdo já convertido antes: reaproveita a saída, não reprocessa
            db.update_job(
                job_id,
                status="concluido",
                job_dir=existing["job_dir"],
                md_path=existing["md_path"],
                n_images=existing["n_images"],
                reaproveitado=1,
            )
            _remove_file_silently(upload_path)
        else:
            _job_queue.put(job_id)

        created.append({"id": job_id, "original_filename": original_filename})

    return JSONResponse({"jobs": created})


@app.get("/api/jobs")
def jobs(limit: int = 500) -> list:
    return db.list_jobs(limit=limit)


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: str):
    job = db.get_job(job_id)
    if job is None:
        return JSONResponse({"erro": "job não encontrado"}, status_code=404)
    return job


@app.post("/api/jobs/{job_id}/cancel")
def job_cancel(job_id: str):
    job = db.get_job(job_id)
    if job is None:
        return JSONResponse({"erro": "job não encontrado"}, status_code=404)

    status = job["status"]

    if status == "pendente":
        with _cancel_lock:
            _cancelled_ids.add(job_id)
        db.update_job(job_id, status="cancelado")
        _remove_file_silently(job.get("upload_path"))
        return {"status": "cancelado"}

    if status == "processando":
        with _cancel_lock:
            _cancelled_ids.add(job_id)
        with _proc_lock:
            proc = _running_procs.get(job_id)
        if proc is not None:
            proc.terminate()

            def _forcar_kill(p=proc):
                try:
                    p.wait(timeout=CANCEL_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    p.kill()

            threading.Thread(target=_forcar_kill, daemon=True).start()
        return {"status": "cancelando"}

    return JSONResponse(
        {"erro": f"job já está '{status}', não há o que cancelar"}, status_code=400
    )


@app.get("/api/jobs/{job_id}/download")
def job_download(job_id: str):
    job = db.get_job(job_id)
    if job is None or not job.get("md_path") or not os.path.exists(job["md_path"]):
        return JSONResponse({"erro": "arquivo não encontrado"}, status_code=404)
    return FileResponse(job["md_path"], filename=os.path.basename(job["md_path"]))


@app.get("/api/jobs/{job_id}/download-zip")
def job_download_zip(job_id: str):
    job = db.get_job(job_id)
    if job is None or not job.get("md_path") or not os.path.exists(job["md_path"]):
        return JSONResponse({"erro": "arquivo não encontrado"}, status_code=404)

    job_dir = job["job_dir"]
    md_path = job["md_path"]
    img_dir = os.path.join(job_dir, "images")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(md_path, arcname=os.path.basename(md_path))
        if os.path.isdir(img_dir):
            for fname in sorted(os.listdir(img_dir)):
                zf.write(os.path.join(img_dir, fname), arcname=f"images/{fname}")
    buffer.seek(0)

    zip_name = os.path.splitext(os.path.basename(md_path))[0] + ".zip"
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
    )


# Arquivos gerados (md + imagens), servidos diretamente para quem quiser
# apontar o Obsidian para a pasta de saída ou baixar um arquivo específico.
app.mount("/output", StaticFiles(directory=OUTPUT_DIR), name="output")

# Interface web (estática, sem build step)
app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static"), html=True), name="static")
