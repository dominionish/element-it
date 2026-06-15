import json
import os
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from transcribe_original import transcribe, gpu_status

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
INPUT_DIR = DATA_DIR / "input"
OUTPUT_DIR = DATA_DIR / "output"
JOBS_DIR = DATA_DIR / "jobs"
LOGS_DIR = DATA_DIR / "logs"

for d in [INPUT_DIR, OUTPUT_DIR, JOBS_DIR, LOGS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Whisper Colab Transcriber API", version="1.0")
lock = threading.Lock()
running_jobs = set()


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def safe_name(name: str) -> str:
    bad = '<>:"/\\|?*'
    for ch in bad:
        name = name.replace(ch, "_")
    return name.strip() or "audio"


def job_file(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.json"


def ready_file(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.ready.json"


def status_file(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.status.json"


def write_json(path: Path, data: dict):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def update_status(job_key: str, **kwargs):
    path = status_file(job_key)
    data = {}
    if path.exists():
        try:
            data = read_json(path)
        except Exception:
            data = {}
    data.update(kwargs)
    data["updated_at"] = now_iso()
    write_json(path, data)


def process_job(job: dict):
    job_id = job["job_id"]
    if job_id in running_jobs:
        return
    running_jobs.add(job_id)
    log_path = LOGS_DIR / f"{job_id}.log"
    try:
        input_path = Path(job["input_path"])
        if not input_path.exists():
            raise FileNotFoundError(f"Файл не найден: {input_path}")

        out_dir = OUTPUT_DIR / job_id
        out_dir.mkdir(parents=True, exist_ok=True)

        update_status(
            job_id,
            job_id=job_id,
            status="running",
            started_at=now_iso(),
            input_path=str(input_path),
            output_dir=str(out_dir),
            model=job.get("model", "medium"),
        )

        # Дублируем stdout в лог задачи, чтобы n8n/человек видел ход обработки.
        import sys
        class Tee:
            def __init__(self, *streams):
                self.streams = streams
            def write(self, data):
                for s in self.streams:
                    s.write(data)
                    s.flush()
            def flush(self):
                for s in self.streams:
                    s.flush()

        old_stdout = sys.stdout
        old_stderr = sys.stderr
        with open(log_path, "a", encoding="utf-8") as lf:
            sys.stdout = Tee(old_stdout, lf)
            sys.stderr = Tee(old_stderr, lf)
            try:
                txt_path, docx_path = transcribe(
                    input_file=input_path,
                    output_dir=out_dir,
                    model_name=job.get("model", "medium"),
                    output_name=job.get("output_name", "Расшифровка_совещания"),
                    chunk_minutes=int(job.get("chunk_minutes", 6)),
                    enhance_audio=bool(job.get("enhance_audio", True)),
                    auto_safe_model=bool(job.get("auto_safe_model", True)),
                )
            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr

        update_status(
            job_id,
            status="done",
            finished_at=now_iso(),
            txt_path=str(txt_path),
            docx_path=str(docx_path),
            log_path=str(log_path),
        )
    except Exception as e:
        update_status(job_id, status="error", error=str(e), log_path=str(log_path))
        with open(log_path, "a", encoding="utf-8") as lf:
            lf.write("\nERROR: " + repr(e) + "\n")
    finally:
        running_jobs.discard(job_id)


def worker_loop():
    while True:
        try:
            ready_files = sorted(JOBS_DIR.glob("*.ready.json"))
            for rf in ready_files:
                try:
                    job = read_json(rf)
                    job_id = job["job_id"]
                    # Атомарно убираем ready-файл, чтобы не запустить задачу дважды.
                    consumed = JOBS_DIR / f"{job_id}.processing.json"
                    try:
                        rf.replace(consumed)
                    except FileNotFoundError:
                        continue
                    threading.Thread(target=process_job, args=(job,), daemon=True).start()
                except Exception as e:
                    print(f"Ошибка чтения ready job {rf}: {e}", flush=True)
        except Exception as e:
            print(f"Ошибка worker_loop: {e}", flush=True)
        time.sleep(2)


@app.on_event("startup")
def startup_event():
    threading.Thread(target=worker_loop, daemon=True).start()


@app.get("/health")
def health():
    return {"status": "ok", "gpu": gpu_status(), "data_dir": str(DATA_DIR)}


@app.post("/jobs")
def create_job(
    input_path: str = Form(...),
    model: str = Form("medium"),
    chunk_minutes: int = Form(6),
    enhance_audio: bool = Form(True),
    output_name: str = Form("Расшифровка_совещания"),
):
    if model not in ["small", "medium", "large-v3"]:
        raise HTTPException(status_code=400, detail="model должен быть small, medium или large-v3")
    p = Path(input_path)
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"Файл не найден: {input_path}")
    job_id = uuid.uuid4().hex[:12]
    job = {
        "job_id": job_id,
        "input_path": str(p),
        "model": model,
        "chunk_minutes": chunk_minutes,
        "enhance_audio": enhance_audio,
        "output_name": output_name,
        "auto_safe_model": True,
        "created_at": now_iso(),
    }
    write_json(ready_file(job_id), job)
    update_status(job_id, job_id=job_id, status="queued", created_at=job["created_at"], input_path=str(p))
    return {"job_id": job_id, "status": "queued"}


@app.post("/upload")
async def upload_and_create_job(
    file: UploadFile = File(...),
    model: str = Form("medium"),
    chunk_minutes: int = Form(6),
    enhance_audio: bool = Form(True),
):
    if model not in ["small", "medium", "large-v3"]:
        raise HTTPException(status_code=400, detail="model должен быть small, medium или large-v3")
    job_id = uuid.uuid4().hex[:12]
    job_input_dir = INPUT_DIR / job_id
    job_input_dir.mkdir(parents=True, exist_ok=True)
    filename = safe_name(file.filename or "audio")
    input_path = job_input_dir / filename
    with open(input_path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    job = {
        "job_id": job_id,
        "input_path": str(input_path),
        "model": model,
        "chunk_minutes": chunk_minutes,
        "enhance_audio": enhance_audio,
        "output_name": "Расшифровка_совещания",
        "auto_safe_model": True,
        "created_at": now_iso(),
    }
    write_json(ready_file(job_id), job)
    update_status(job_id, job_id=job_id, status="queued", created_at=job["created_at"], input_path=str(input_path))
    return {"job_id": job_id, "status": "queued", "status_url": f"/jobs/{job_id}"}


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    sf = status_file(job_id)
    if not sf.exists():
        raise HTTPException(status_code=404, detail="Задача не найдена")
    return read_json(sf)


@app.get("/jobs/{job_id}/log")
def get_log(job_id: str):
    path = LOGS_DIR / f"{job_id}.log"
    if not path.exists():
        return JSONResponse({"log": "Лог пока не создан"})
    return {"log": path.read_text(encoding="utf-8", errors="replace")[-20000:]}


@app.get("/download/{job_id}/{kind}")
def download(job_id: str, kind: str):
    if kind not in ["txt", "docx", "meta", "log"]:
        raise HTTPException(status_code=400, detail="kind: txt, docx, meta или log")
    sf = status_file(job_id)
    if not sf.exists():
        raise HTTPException(status_code=404, detail="Задача не найдена")
    st = read_json(sf)
    if kind == "log":
        path = Path(st.get("log_path", LOGS_DIR / f"{job_id}.log"))
    elif kind == "meta":
        path = OUTPUT_DIR / job_id / "Расшифровка_совещания_meta.json"
    else:
        path = Path(st.get(f"{kind}_path", ""))
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Файл {kind} не найден")
    return FileResponse(path, filename=path.name)


@app.get("/", response_class=HTMLResponse)
def index():
    return """
    <html><body style='font-family:Arial;max-width:800px;margin:40px auto'>
    <h2>Whisper Transcriber API</h2>
    <p>Проверка без n8n. Для n8n используй POST /upload или папку /data/input + ready JSON.</p>
    <form action='/upload' method='post' enctype='multipart/form-data'>
      <p><input type='file' name='file'></p>
      <p>Модель: <select name='model'><option>small</option><option selected>medium</option><option>large-v3</option></select></p>
      <p>Чанк, минут: <input name='chunk_minutes' value='6'></p>
      <p><label><input type='checkbox' name='enhance_audio' value='true' checked> Улучшать речь ffmpeg</label></p>
      <button type='submit'>Запустить</button>
    </form>
    </body></html>
    """
