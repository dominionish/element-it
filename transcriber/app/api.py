import json
import os
import re
import shutil
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from transcribe_original import gpu_status, set_log_stream, transcribe

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
INPUT_DIR = DATA_DIR / "input"
OUTPUT_DIR = DATA_DIR / "output"
JOBS_DIR = DATA_DIR / "jobs"
LOGS_DIR = DATA_DIR / "logs"

ALLOWED_MODELS = {"small", "medium", "large-v3"}
TRUTHY = {"1", "true", "yes", "y", "on", "да"}

for directory in [INPUT_DIR, OUTPUT_DIR, JOBS_DIR, LOGS_DIR]:
    directory.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    threading.Thread(target=worker_loop, daemon=True).start()
    yield


app = FastAPI(title="Whisper Transcriber API", version="1.1", lifespan=lifespan)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def safe_name(name: str) -> str:
    bad = '<>:"/\\|?*'
    for ch in bad:
        name = name.replace(ch, "_")
    name = re.sub(r"\s+", " ", str(name)).strip()
    return name or "audio"


def bool_value(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in TRUTHY


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


def normalize_model(model: str) -> str:
    model = str(model or "medium").strip().lower()
    if model not in ALLOWED_MODELS:
        raise HTTPException(status_code=400, detail="model должен быть small, medium или large-v3")
    return model


def create_transcription_job(
    input_path: Path,
    model: str,
    chunk_minutes: int,
    enhance_audio: bool,
    output_name: str,
) -> dict:
    model = normalize_model(model)
    if not input_path.exists():
        raise HTTPException(status_code=404, detail=f"Файл не найден: {input_path}")

    job_id = uuid.uuid4().hex[:12]
    created_at = now_iso()
    job = {
        "job_id": job_id,
        "input_path": str(input_path),
        "model": model,
        "chunk_minutes": int(chunk_minutes),
        "enhance_audio": bool_value(enhance_audio, True),
        "output_name": safe_name(output_name or "Расшифровка_совещания"),
        "auto_safe_model": True,
        "created_at": created_at,
    }
    write_json(ready_file(job_id), job)
    update_status(
        job_id,
        job_id=job_id,
        status="queued",
        created_at=created_at,
        input_path=str(input_path),
        model=model,
    )
    return job


def process_job(job: dict):
    job_id = job["job_id"]
    log_path = LOGS_DIR / f"{job_id}.log"
    try:
        input_path = Path(job["input_path"])
        if not input_path.exists():
            raise FileNotFoundError(f"Файл не найден: {input_path}")

        out_dir = OUTPUT_DIR / job_id
        out_dir.mkdir(parents=True, exist_ok=True)
        output_name = safe_name(job.get("output_name", "Расшифровка_совещания"))

        update_status(
            job_id,
            job_id=job_id,
            status="running",
            started_at=now_iso(),
            input_path=str(input_path),
            output_dir=str(out_dir),
            model=job.get("model", "medium"),
        )

        with open(log_path, "a", encoding="utf-8") as log_file:
            set_log_stream(log_file)
            try:
                txt_path, docx_path = transcribe(
                    input_file=input_path,
                    output_dir=out_dir,
                    model_name=job.get("model", "medium"),
                    output_name=output_name,
                    chunk_minutes=int(job.get("chunk_minutes", 6)),
                    enhance_audio=bool_value(job.get("enhance_audio"), True),
                    auto_safe_model=bool_value(job.get("auto_safe_model"), True),
                )
            finally:
                set_log_stream(None)

        update_status(
            job_id,
            status="done",
            finished_at=now_iso(),
            txt_path=str(txt_path),
            docx_path=str(docx_path),
            meta_path=str(out_dir / f"{output_name}_meta.json"),
            log_path=str(log_path),
        )
    except Exception as error:
        update_status(job_id, status="error", error=str(error), log_path=str(log_path))
        with open(log_path, "a", encoding="utf-8") as log_file:
            log_file.write("\nERROR: " + repr(error) + "\n")


def process_ready_jobs_once() -> int:
    processed = 0
    ready_files = sorted(JOBS_DIR.glob("*.ready.json"))
    for ready_path in ready_files:
        try:
            job = read_json(ready_path)
            job_id = job["job_id"]
            # One worker processes one job at a time so model memory and temp
            # audio files stay isolated even when several jobs arrive together.
            consumed = JOBS_DIR / f"{job_id}.processing.json"
            try:
                ready_path.replace(consumed)
            except FileNotFoundError:
                continue
            process_job(job)
            processed += 1
        except Exception as error:
            print(f"Ошибка чтения ready job {ready_path}: {error}", flush=True)
    return processed


def worker_loop():
    while True:
        try:
            process_ready_jobs_once()
        except Exception as error:
            print(f"Ошибка worker_loop: {error}", flush=True)
        time.sleep(2)


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
    job = create_transcription_job(
        input_path=Path(input_path),
        model=model,
        chunk_minutes=chunk_minutes,
        enhance_audio=enhance_audio,
        output_name=output_name,
    )
    return {"job_id": job["job_id"], "status": "queued"}


@app.post("/upload")
async def upload_and_create_job(
    file: UploadFile = File(...),
    model: str = Form("medium"),
    chunk_minutes: int = Form(6),
    enhance_audio: bool = Form(True),
    output_name: str = Form("Расшифровка_совещания"),
):
    normalize_model(model)
    job_id = uuid.uuid4().hex[:12]
    job_input_dir = INPUT_DIR / job_id
    job_input_dir.mkdir(parents=True, exist_ok=True)
    filename = safe_name(file.filename or "audio")
    input_path = job_input_dir / filename
    with open(input_path, "wb") as output:
        shutil.copyfileobj(file.file, output)

    job = create_transcription_job(
        input_path=input_path,
        model=model,
        chunk_minutes=chunk_minutes,
        enhance_audio=enhance_audio,
        output_name=output_name,
    )
    return {"job_id": job["job_id"], "status": "queued", "status_url": f"/jobs/{job['job_id']}"}


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    path = status_file(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Задача не найдена")
    return read_json(path)


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
    status_path = status_file(job_id)
    if not status_path.exists():
        raise HTTPException(status_code=404, detail="Задача не найдена")

    status = read_json(status_path)
    if kind == "log":
        path = Path(status.get("log_path", LOGS_DIR / f"{job_id}.log"))
    elif kind == "meta":
        path = Path(status.get("meta_path") or OUTPUT_DIR / job_id / "Расшифровка_совещания_meta.json")
    else:
        path = Path(status.get(f"{kind}_path", ""))
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Файл {kind} не найден")
    return FileResponse(path, filename=path.name)


@app.get("/", response_class=HTMLResponse)
def index():
    return """
    <html><body style='font-family:Arial;max-width:800px;margin:40px auto'>
    <h2>Whisper Transcriber API</h2>
    <p>Этот сервис только транскрибирует аудио/видео. Для Planfix используй отдельный сервис planfix.</p>
    <form action='/upload' method='post' enctype='multipart/form-data'>
      <p><input type='file' name='file'></p>
      <p>Модель: <select name='model'><option>small</option><option selected>medium</option><option>large-v3</option></select></p>
      <p>Чанк, минут: <input name='chunk_minutes' value='6'></p>
      <p><label><input type='checkbox' name='enhance_audio' value='true' checked> Улучшать речь ffmpeg</label></p>
      <button type='submit'>Запустить</button>
    </form>
    </body></html>
    """
