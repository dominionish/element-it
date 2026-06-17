import base64
import binascii
import hashlib
import html
import json
import mimetypes
import os
import re
import shutil
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlsplit, urlunsplit
from urllib.request import Request as UrlRequest, urlopen

from fastapi import BackgroundTasks, FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.requests import ClientDisconnect

from transcribe_original import transcribe, gpu_status, set_log_stream

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
INPUT_DIR = DATA_DIR / "input"
OUTPUT_DIR = DATA_DIR / "output"
JOBS_DIR = DATA_DIR / "jobs"
LOGS_DIR = DATA_DIR / "logs"
PLANFIX_EVENTS_DIR = DATA_DIR / "planfix_events"

PLANFIX_AUDIO_EXTENSIONS = {
    ext.strip().lower()
    for ext in os.getenv(
        "PLANFIX_AUDIO_EXTENSIONS",
        ".mp3,.m4a,.wav,.ogg,.opus,.webm,.aac,.flac,.mp4,.mov,.mkv,.avi",
    ).split(",")
    if ext.strip()
}
PLANFIX_ALLOWED_FILE_URL_HOSTS = {
    host.strip().lower()
    for host in os.getenv("PLANFIX_ALLOWED_FILE_URL_HOSTS", "planfix.ru,.planfix.ru").split(",")
    if host.strip()
}
PLANFIX_FILE_URL_TIMEOUT = int(os.getenv("PLANFIX_FILE_URL_TIMEOUT", "120"))
PLANFIX_ALLOWED_RESULT_HOSTS = {
    host.strip().lower()
    for host in os.getenv("PLANFIX_ALLOWED_RESULT_HOSTS", "planfix.ru,.planfix.ru").split(",")
    if host.strip()
}
PLANFIX_RESULT_WEBHOOK_ID = os.getenv("PLANFIX_RESULT_WEBHOOK_ID", "").strip().strip("/")
PLANFIX_RESULT_WEBHOOK_URL = os.getenv("PLANFIX_RESULT_WEBHOOK_URL", "").strip()
PLANFIX_RESULT_FILE_FIELD = os.getenv("PLANFIX_RESULT_FILE_FIELD", "txt_file").strip() or "txt_file"
PLANFIX_RESULT_TIMEOUT = int(os.getenv("PLANFIX_RESULT_TIMEOUT", "120"))
TRUTHY = {"1", "true", "yes", "y", "on", "да"}
PLANFIX_FILE_FIELD_KEYS = {
    "files",
    "file",
    "audio_files",
    "audio_file",
    "attachments",
    "attachment",
    "comment_files",
    "comment_file",
    "comment_attachments",
    "comment_attachment",
    "файлы",
    "файл",
    "вложения",
    "вложение",
}

for d in [INPUT_DIR, OUTPUT_DIR, JOBS_DIR, LOGS_DIR, PLANFIX_EVENTS_DIR]:
    d.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    threading.Thread(target=worker_loop, daemon=True).start()
    yield


app = FastAPI(title="Whisper Colab Transcriber API", version="1.0", lifespan=lifespan)


@app.middleware("http")
async def log_planfix_http_requests(request: Request, call_next):
    if not request.url.path.startswith("/planfix/"):
        return await call_next(request)

    started = time.time()
    planfix_log(
        "входящий HTTP-запрос",
        method=request.method,
        path=request.url.path,
        content_type=request.headers.get("content-type", ""),
        content_length=request.headers.get("content-length", ""),
        client=request.client.host if request.client else "",
    )
    try:
        response = await call_next(request)
    except Exception as error:
        planfix_log(
            "ошибка HTTP-запроса до отправки ответа",
            method=request.method,
            path=request.url.path,
            error=repr(error),
            duration_ms=int((time.time() - started) * 1000),
        )
        raise

    planfix_log(
        "HTTP-ответ отправлен",
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        duration_ms=int((time.time() - started) * 1000),
    )
    return response


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def planfix_log(message: str, **context):
    line = f"{now_iso()} [planfix] {message}"
    if context:
        line += " " + json.dumps(context, ensure_ascii=False, default=str)
    print(line, flush=True)
    with open(LOGS_DIR / "planfix_audio_parse.log", "a", encoding="utf-8") as lf:
        lf.write(line + "\n")


def safe_name(name: str) -> str:
    bad = '<>:"/\\|?*'
    for ch in bad:
        name = name.replace(ch, "_")
    return name.strip() or "audio"


def safe_folder_part(value: str) -> str:
    value = safe_name(str(value))
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"[^0-9A-Za-zА-Яа-яЁё._-]+", "_", value)
    return value.strip("._-")[:80] or "planfix"


def normalized_field_key(value: str) -> str:
    return re.sub(r"[\s.-]+", "_", str(value).strip().lower())


def ready_file(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.ready.json"


def status_file(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.status.json"


def write_json(path: Path, data: dict):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def write_json_exclusive(path: Path, data: dict) -> bool:
    try:
        with open(path, "x", encoding="utf-8") as output:
            json.dump(data, output, ensure_ascii=False, indent=2)
        return True
    except FileExistsError:
        return False


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def first_present(data: dict, keys: list[str], default: str = "") -> str:
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        value = str(value).strip()
        if value:
            return value
    return default


def add_payload_value(payload: dict, key: str, value):
    if key not in payload:
        payload[key] = value
    elif isinstance(payload[key], list):
        payload[key].append(value)
    else:
        payload[key] = [payload[key], value]


def nested_dicts(data, max_depth: int = 4):
    queue = [(data, 0)]
    while queue:
        value, depth = queue.pop(0)
        if isinstance(value, dict):
            yield value
            if depth < max_depth:
                queue.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list) and depth < max_depth:
            queue.extend((item, depth + 1) for item in value)


def first_nested_present(data: dict, keys: list[str], default: str = "") -> str:
    for item in nested_dicts(data):
        value = first_present(item, keys)
        if value:
            return value
    return default


def planfix_comment_contexts(payload: dict) -> list[dict]:
    context_keys = {
        "comment",
        "added_comment",
        "new_comment",
        "comment_data",
        "комментарий",
        "добавленный_комментарий",
        "новый_комментарий",
    }
    contexts = []
    for item in nested_dicts(payload):
        for key, value in item.items():
            if normalized_field_key(key) in context_keys and isinstance(value, dict):
                contexts.append(value)
    return contexts


def planfix_comment_metadata(payload: dict) -> dict:
    contexts = planfix_comment_contexts(payload)
    comment_id = first_nested_present(
        payload,
        ["comment_id", "commentId", "comment_action_id", "action_id", "actionId"],
    )
    comment_text = first_nested_present(
        payload,
        ["comment_text", "commentText", "comment_body"],
    )
    comment_author = first_nested_present(
        payload,
        ["comment_author", "commentAuthor", "author_name", "user_name"],
    )
    for context in contexts:
        comment_id = comment_id or first_present(context, ["id", "comment_id", "commentId"])
        comment_text = comment_text or first_present(
            context,
            ["text", "body", "description", "comment_text", "Текст"],
        )
        comment_author = comment_author or first_present(
            context,
            ["author_name", "user_name", "comment_author", "Автор"],
        )

    return {
        "comment_id": comment_id,
        "comment_text": comment_text,
        "comment_author": comment_author,
        "event_id": first_nested_present(payload, ["event_id", "eventId", "request_event_id"]),
    }


def planfix_file_fingerprint(request_files: list[dict]) -> str:
    markers = []
    for item in request_files:
        source = item.get("source", "")
        if source == "url":
            parts = urlsplit(item.get("url", ""))
            identity = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        elif source == "upload":
            identity = hashlib.sha256(item.get("content_bytes", b"")).hexdigest()
        else:
            identity = hashlib.sha256(item.get("base64", "").encode("utf-8")).hexdigest()
        markers.append(f"{source}|{item.get('name', '')}|{identity}")
    raw = "\n".join(sorted(markers)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16] if raw else ""


def planfix_event_key(task_id: str, metadata: dict, request_files: Optional[list[dict]] = None) -> str:
    event_id = metadata.get("comment_id") or metadata.get("event_id")
    if not event_id:
        return ""
    file_fingerprint = planfix_file_fingerprint(request_files or [])
    parts = [safe_folder_part(task_id), safe_folder_part(event_id)]
    if file_fingerprint:
        parts.append(file_fingerprint)
    return "_".join(parts)


def reserve_planfix_event(
    task_id: str,
    metadata: dict,
    request_id: str,
    request_files: Optional[list[dict]] = None,
) -> tuple[bool, Optional[Path], dict]:
    event_key = planfix_event_key(task_id, metadata, request_files)
    if not event_key:
        return True, None, {}

    path = PLANFIX_EVENTS_DIR / f"{event_key}.json"
    previous = {}
    if path.exists():
        try:
            previous = read_json(path)
        except Exception:
            previous = {}
        if previous.get("status") == "failed":
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    receipt = {
        "status": "accepted",
        "request_id": request_id,
        "task_id": task_id,
        "file_fingerprint": planfix_file_fingerprint(request_files or []),
        **metadata,
        "accepted_at": now_iso(),
    }
    return write_json_exclusive(path, receipt), path, previous


def update_planfix_event(path: Optional[Path], **values):
    if not path:
        return
    data = {}
    if path.exists():
        try:
            data = read_json(path)
        except Exception:
            data = {}
    data.update(values)
    data["updated_at"] = now_iso()
    write_json(path, data)


def bool_value(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in TRUTHY


def unique_output_path(directory: Path, filename: str) -> Path:
    base = safe_name(filename)
    path = directory / base
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 1000):
        candidate = directory / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
    return directory / f"{stem}_{uuid.uuid4().hex[:8]}{suffix}"


def host_allowed(host: str, allowed_hosts: set[str]) -> bool:
    host = (host or "").lower()
    for allowed in allowed_hosts:
        if allowed.startswith(".") and host.endswith(allowed):
            return True
        if host == allowed:
            return True
    return False


def normalize_planfix_domain(value: str) -> str:
    value = first_scalar(value)
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    host = (urlsplit(text).hostname or "").lower()
    return host if host_allowed(host, PLANFIX_ALLOWED_RESULT_HOSTS) else ""


def looks_like_base64_key(key: str) -> bool:
    key = key.lower()
    return "base64" in key or key in {"file_data", "audio_data"}


def looks_like_url(value: str) -> bool:
    return isinstance(value, str) and value.strip().lower().startswith(("http://", "https://"))


def extract_href(value: str) -> str:
    if not isinstance(value, str):
        return ""
    match = re.search(r'href=["\']([^"\']+)["\']', value, flags=re.IGNORECASE)
    return match.group(1) if match else ""


def extract_html_file_links(value: str) -> list[dict]:
    if not isinstance(value, str):
        return []
    matches = re.findall(
        r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        value,
        flags=re.IGNORECASE | re.DOTALL,
    )
    result = []
    for url, label in matches:
        name = re.sub(r"<[^>]+>", "", label)
        result.append({
            "url": html.unescape(url).strip(),
            "name": html.unescape(name).strip(),
        })
    return result


def normalize_url_value(value) -> str:
    value = first_scalar(value)
    if not isinstance(value, str):
        return ""
    text = value.strip()
    href = extract_href(text)
    if href:
        return href
    return text if looks_like_url(text) else ""


def file_url_allowed(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme != "https":
        return False
    host = (parts.hostname or "").lower()
    return host_allowed(host, PLANFIX_ALLOWED_FILE_URL_HOSTS)


def hide_large_payload_values(value):
    if isinstance(value, dict):
        return {
            key: hide_large_payload_values(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [hide_large_payload_values(item) for item in value]
    if isinstance(value, str) and len(value) > 300:
        return f"<omitted {len(value)} chars>"
    return value


def parse_jsonish(value):
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def first_scalar(value):
    if isinstance(value, list):
        for item in value:
            item = first_scalar(item)
            if item not in [None, ""]:
                return item
        return ""
    return value


def find_value(data: dict, keys: list[str], default: str = ""):
    for key in keys:
        value = data.get(key)
        value = first_scalar(value)
        if value not in [None, ""]:
            return value
    return default


def normalize_request_file_item(item, index: int, payload: dict) -> Optional[dict]:
    if isinstance(item, str):
        item = parse_jsonish(item)
    if isinstance(item, str):
        item = {"url": item} if looks_like_url(item) else {"base64": item}
    if not isinstance(item, dict):
        return None

    file_url = normalize_url_value(find_value(
        item,
        [
            "file_url",
            "audio_url",
            "download_url",
            "downloadLink",
            "downloadUrl",
            "url",
            "href",
            "link",
            "Ссылка",
            "Html-Link",
        ],
    ))
    content = find_value(
        item,
        [
            "base64",
            "file_base64",
            "audio_base64",
            "content_base64",
            "data_base64",
            "file_data",
            "audio_data",
        ],
    )
    if not content:
        for key, value in item.items():
            if looks_like_base64_key(key) and value:
                content = value
                break
    if not content and not file_url:
        return None

    name = find_value(
        item,
        [
            "name",
            "file_name",
            "filename",
            "fileName",
            "originalName",
            "title",
            "Имя",
        ],
    ) or first_present(
        payload,
        ["file_name", "filename", "audio_name", "name", "fileName", "originalName", "Имя"],
        default=f"planfix_audio_{index}.m4a",
    )
    content_type = find_value(item, ["mimeType", "mime", "contentType", "content_type"], "")
    if file_url:
        return {
            "source": "url",
            "name": safe_name(str(name)),
            "url": file_url,
            "content_type": str(content_type or ""),
        }
    return {
        "source": "base64",
        "name": safe_name(str(name)),
        "base64": str(content),
        "content_type": str(content_type or ""),
    }


def expand_request_file_value(value, payload: dict, start_index: int) -> list[dict]:
    value = parse_jsonish(value)
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(expand_request_file_value(item, payload, start_index + len(result)))
        return result
    if isinstance(value, dict):
        normalized = normalize_request_file_item(value, start_index, payload)
        return [normalized] if normalized else []
    if isinstance(value, str):
        html_links = extract_html_file_links(value)
        if html_links:
            return [
                item
                for offset, link in enumerate(html_links)
                if (item := normalize_request_file_item(link, start_index + offset, payload))
            ]

        lines = [line.strip() for line in value.splitlines() if line.strip()]
        if len(lines) > 1 and all(looks_like_url(line) for line in lines):
            return [
                item
                for offset, line in enumerate(lines)
                if (item := normalize_request_file_item(line, start_index + offset, payload))
            ]

    normalized = normalize_request_file_item(value, start_index, payload)
    return [normalized] if normalized else []


def iter_planfix_file_values(payload: dict):
    for item in nested_dicts(payload):
        for key, value in item.items():
            if normalized_field_key(key) in PLANFIX_FILE_FIELD_KEYS:
                yield value


def value_list(value) -> list:
    value = parse_jsonish(value)
    return value if isinstance(value, list) else [value]


def extract_request_file_items(payload: dict, uploaded_files: list[dict]) -> list[dict]:
    items = list(uploaded_files)

    for value in iter_planfix_file_values(payload):
        items.extend(expand_request_file_value(value, payload, len(items) + 1))

    name_values = []
    for name_key in ["file_name", "filename", "audio_name", "name", "fileName", "originalName", "Имя"]:
        if payload.get(name_key) not in [None, ""]:
            name_values = value_list(payload[name_key])
            break
    found_direct_values = False
    for key in [
        "file_url",
        "audio_url",
        "download_url",
        "downloadLink",
        "downloadUrl",
        "file_base64",
        "audio_base64",
        "content_base64",
        "data_base64",
        "file_data",
        "audio_data",
    ]:
        if payload.get(key) in [None, ""]:
            continue
        found_direct_values = True
        for offset, value in enumerate(value_list(payload[key])):
            scoped_payload = dict(payload)
            if name_values and name_values[0] not in [None, ""]:
                scoped_payload["file_name"] = name_values[min(offset, len(name_values) - 1)]
            items.extend(expand_request_file_value(value, scoped_payload, len(items) + 1))

    if not found_direct_values:
        direct_item = normalize_request_file_item(payload, len(items) + 1, payload)
        if direct_item:
            items.append(direct_item)

    seen = set()
    unique_items = []
    for item in items:
        marker = (
            item.get("source"),
            item.get("name"),
            item.get("url", ""),
            item.get("base64", "")[:80],
            len(item.get("content_bytes", b"")),
        )
        if marker in seen:
            continue
        seen.add(marker)
        unique_items.append(item)
    return unique_items


def decode_base64_payload(value: str) -> tuple[bytes, str]:
    content_type = ""
    text = value.strip()
    if text.startswith("data:") and "," in text:
        header, text = text.split(",", 1)
        content_type = header[5:].split(";")[0]
    compact = re.sub(r"\s+", "", text)
    try:
        return base64.b64decode(compact, validate=True), content_type
    except binascii.Error:
        return base64.b64decode(compact), content_type


def filename_with_content_type(name: str, content_type: str, index: int) -> str:
    name = safe_name(name or f"planfix_audio_{index}.m4a")
    if Path(name).suffix:
        return name
    guessed = mimetypes.guess_extension(content_type or "") or ".m4a"
    return safe_name(f"{name}{guessed}")


def should_accept_audio(name: str, content_type: str) -> bool:
    content_type = (content_type or "").split(";", 1)[0].strip().lower()
    if content_type.startswith("audio/") or content_type.startswith("video/"):
        return True
    ext = Path(name).suffix.lower()
    if ext:
        return ext in PLANFIX_AUDIO_EXTENSIONS
    return not content_type or content_type == "application/octet-stream"


def filter_audio_request_items(items: list[dict]) -> tuple[list[dict], list[dict]]:
    accepted = []
    rejected = []
    for item in items:
        if should_accept_audio(item.get("name", ""), item.get("content_type", "")):
            accepted.append(item)
        else:
            rejected.append(item)
    return accepted, rejected


def filename_from_content_disposition(value: str) -> str:
    if not value:
        return ""
    match = re.search(r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';]+)', value, flags=re.IGNORECASE)
    if not match:
        return ""
    return safe_name(match.group(1))


def url_path_filename(url: str) -> str:
    name = Path(urlsplit(url).path).name
    return safe_name(name) if name else ""


def save_request_file_item(item: dict, target_dir: Path, index: int) -> dict:
    content_type = item.get("content_type", "")
    if item.get("source") == "upload":
        content = item["content_bytes"]
        name = filename_with_content_type(item.get("name", ""), content_type, index)
        if not should_accept_audio(name, content_type):
            raise RuntimeError(f"Файл {name} не похож на аудио/видео")

        out_path = unique_output_path(target_dir, name)
        out_path.write_bytes(content)
        return {
            "name": name,
            "path": str(out_path),
            "size_bytes": out_path.stat().st_size,
            "content_type": content_type,
        }

    if item.get("source") == "url":
        url = item["url"]
        if not file_url_allowed(url):
            raise RuntimeError(f"Ссылка на файл не разрешена: {urlsplit(url).hostname or url}")
        req = UrlRequest(url, headers={"User-Agent": "whisper-transcriber/1.0"}, method="GET")
        try:
            with urlopen(req, timeout=PLANFIX_FILE_URL_TIMEOUT) as resp:
                content_type = content_type or resp.headers.get_content_type()
                header_name = filename_from_content_disposition(resp.headers.get("content-disposition", ""))
                name = filename_with_content_type(
                    item.get("name") or header_name or url_path_filename(url),
                    content_type,
                    index,
                )
                if not should_accept_audio(name, content_type):
                    raise RuntimeError(f"Файл {name} не похож на аудио/видео")
                out_path = unique_output_path(target_dir, name)
                with open(out_path, "wb") as out:
                    shutil.copyfileobj(resp, out)
        except HTTPError as e:
            raise RuntimeError(f"Не удалось скачать файл по ссылке, HTTP {e.code}") from e
        except URLError as e:
            raise RuntimeError(f"Не удалось скачать файл по ссылке: {e.reason}") from e
        return {
            "name": name,
            "path": str(out_path),
            "size_bytes": out_path.stat().st_size,
            "content_type": content_type,
            "source_url": url,
        }

    else:
        content, data_url_content_type = decode_base64_payload(item["base64"])
        content_type = content_type or data_url_content_type

        name = filename_with_content_type(item.get("name", ""), content_type, index)
        if not should_accept_audio(name, content_type):
            raise RuntimeError(f"Файл {name} не похож на аудио/видео")

        out_path = unique_output_path(target_dir, name)
        out_path.write_bytes(content)
        return {
            "name": name,
            "path": str(out_path),
            "size_bytes": out_path.stat().st_size,
            "content_type": content_type,
        }


def queue_planfix_transcription(input_path: Path, task_id: str, company: str, source_name: str, payload: dict) -> str:
    job_id = uuid.uuid4().hex[:12]
    output_name = safe_name(f"{company}_{task_id}_{Path(source_name).stem}")[:120]
    project = first_present(payload, ["project", "project_id", "projectId", "Проект"])
    job = {
        "job_id": job_id,
        "input_path": str(input_path),
        "model": "medium",
        "chunk_minutes": 6,
        "enhance_audio": True,
        "output_name": output_name,
        "auto_safe_model": True,
        "source": "planfix_audio_parse",
        "planfix_task_id": task_id,
        "planfix_project": project,
        "planfix_source_name": source_name,
        "company": company,
        "created_at": now_iso(),
    }
    write_json(ready_file(job_id), job)
    update_status(job_id, job_id=job_id, status="queued", created_at=job["created_at"], input_path=str(input_path))
    return job_id


def handle_planfix_audio_parse_request(
    request_id: str,
    task_id: str,
    company: str,
    payload: dict,
    request_files: list[dict],
    event_receipt_path: Optional[Path] = None,
):
    comment = planfix_comment_metadata(payload)
    folder_parts = [safe_folder_part(task_id)]
    if comment["comment_id"]:
        folder_parts.append(safe_folder_part(comment["comment_id"]))
    folder_parts.append(request_id)
    target_dir = INPUT_DIR / "planfix" / "_".join(folder_parts)

    create_jobs = bool_value(
        first_scalar(payload.get("create_jobs")),
        default=bool_value(os.getenv("PLANFIX_CREATE_TRANSCRIBE_JOBS"), True),
    )
    saved_files = []
    skipped = []

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        write_json(target_dir / "request.json", hide_large_payload_values(payload))
        planfix_log(
            "начинаю сохранение файлов из комментария",
            request_id=request_id,
            task_id=task_id,
            comment_id=comment["comment_id"],
            company=company,
        )
        planfix_log(
            "получены файлы в запросе",
            request_id=request_id,
            task_id=task_id,
            company=company,
            comment_id=comment["comment_id"],
            count=len(request_files),
            files=[item.get("name") for item in request_files],
        )

        for index, item in enumerate(request_files, start=1):
            try:
                record = save_request_file_item(item, target_dir, index)
            except Exception as e:
                skipped.append({"name": item.get("name"), "reason": str(e)})
                planfix_log(
                    "файл пропущен",
                    request_id=request_id,
                    task_id=task_id,
                    company=company,
                    comment_id=comment["comment_id"],
                    file_name=item.get("name"),
                    reason=str(e),
                )
                continue

            if create_jobs:
                record["job_id"] = queue_planfix_transcription(Path(record["path"]), task_id, company, record["name"], payload)
            saved_files.append(record)
            planfix_log(
                "файл сохранен",
                request_id=request_id,
                task_id=task_id,
                company=company,
                comment_id=comment["comment_id"],
                file_name=record["name"],
                saved_to=record["path"],
                size_bytes=record["size_bytes"],
                job_id=record.get("job_id"),
            )

        write_json(
            target_dir / "download_result.json",
            {
                "request_id": request_id,
                "task_id": task_id,
                "company": company,
                **comment,
                "saved_files": saved_files,
                "skipped": skipped,
                "finished_at": now_iso(),
            },
        )
        planfix_log(
            "сохранение файлов завершено",
            request_id=request_id,
            task_id=task_id,
            company=company,
            comment_id=comment["comment_id"],
            saved_count=len(saved_files),
            skipped_count=len(skipped),
            directory=str(target_dir),
        )
        event_status = "completed" if saved_files else "failed"
        update_planfix_event(
            event_receipt_path,
            status=event_status,
            saved_count=len(saved_files),
            skipped_count=len(skipped),
            directory=str(target_dir),
            error="Все файлы были пропущены" if not saved_files else "",
            finished_at=now_iso(),
        )
    except Exception as e:
        planfix_log(
            "ошибка загрузки файлов",
            request_id=request_id,
            task_id=task_id,
            company=company,
            comment_id=comment["comment_id"],
            error=str(e),
        )
        update_planfix_event(event_receipt_path, status="failed", error=str(e), failed_at=now_iso())


async def parse_planfix_audio_payload(request: Request) -> tuple[dict, list[dict]]:
    payload = {}
    uploaded_files = []
    content_type = request.headers.get("content-type", "").lower()

    for key, value in request.query_params.multi_items():
        add_payload_value(payload, key, value)

    if "application/json" in content_type:
        data = await request.json()
        if isinstance(data, dict):
            payload.update(data)
        return payload, uploaded_files

    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form = await request.form()
        for key, value in form.multi_items():
            if hasattr(value, "filename") and hasattr(value, "read"):
                uploaded_files.append({
                    "source": "upload",
                    "field": key,
                    "name": safe_name(value.filename or key),
                    "content_type": getattr(value, "content_type", "") or "",
                    "content_bytes": await value.read(),
                })
            else:
                add_payload_value(payload, key, str(value))
        return payload, uploaded_files

    raw = (await request.body()).decode("utf-8", errors="replace").strip()
    if not raw:
        return payload, uploaded_files
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            payload.update(data)
            return payload, uploaded_files
    except json.JSONDecodeError:
        pass

    for key, value in parse_qsl(raw, keep_blank_values=True):
        add_payload_value(payload, key, value)
    return payload, uploaded_files


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


CYRILLIC_TRANSLITERATION = str.maketrans(
    {
        "А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D", "Е": "E", "Ё": "Yo",
        "Ж": "Zh", "З": "Z", "И": "I", "Й": "Y", "К": "K", "Л": "L", "М": "M",
        "Н": "N", "О": "O", "П": "P", "Р": "R", "С": "S", "Т": "T", "У": "U",
        "Ф": "F", "Х": "Kh", "Ц": "Ts", "Ч": "Ch", "Ш": "Sh", "Щ": "Sch",
        "Ъ": "", "Ы": "Y", "Ь": "", "Э": "E", "Ю": "Yu", "Я": "Ya",
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
        "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    }
)


def ascii_multipart_filename(file_name: str) -> str:
    path = Path(file_name)
    stem = path.stem.translate(CYRILLIC_TRANSLITERATION)
    suffix = re.sub(r"[^A-Za-z0-9.]+", "", path.suffix) or ".txt"
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" ._-")
    return f"{stem or 'transcription'}{suffix}"


def planfix_result_file_name(job: dict, txt_path: Path) -> str:
    source_name = str(job.get("planfix_source_name") or "").strip()
    if source_name:
        return f"{Path(source_name).stem}.txt"
    return txt_path.name


def encode_multipart_form(
    fields: dict,
    file_field: str,
    path: Path,
    upload_name: Optional[str] = None,
) -> tuple[bytes, str]:
    boundary = f"----whisper-transcriber-{uuid.uuid4().hex}"
    file_name = ascii_multipart_filename(upload_name or path.name)
    content_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
    parts = []
    for name, value in fields.items():
        if value in [None, ""]:
            continue
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    parts.append(
        (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{file_field}"; filename="{file_name}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
        + path.read_bytes()
        + b"\r\n"
    )
    body = b"".join(parts) + f"--{boundary}--\r\n".encode("ascii")
    return body, boundary


def build_planfix_result_webhook_url(job: dict) -> str:
    if PLANFIX_RESULT_WEBHOOK_URL:
        parts = urlsplit(PLANFIX_RESULT_WEBHOOK_URL)
        if parts.scheme == "https" and host_allowed(parts.hostname or "", PLANFIX_ALLOWED_RESULT_HOSTS):
            return PLANFIX_RESULT_WEBHOOK_URL
        return ""
    if not PLANFIX_RESULT_WEBHOOK_ID:
        return ""
    domain = normalize_planfix_domain(job.get("company", ""))
    return f"https://{domain}/webhook/file/{PLANFIX_RESULT_WEBHOOK_ID}" if domain else ""


def send_planfix_result(job: dict, txt_path: Path) -> dict:
    webhook_url = build_planfix_result_webhook_url(job)
    if not webhook_url:
        return {
            "sent": False,
            "mode": "multipart_webhook",
            "reason": "PLANFIX_RESULT_WEBHOOK_ID is not configured or webhook URL is invalid",
        }

    txt_path = Path(txt_path)
    upload_name = planfix_result_file_name(job, txt_path)
    fields = {
        "task": job.get("planfix_task_id", ""),
        "project": job.get("planfix_project", ""),
        "job_id": job.get("job_id", ""),
        "file_name": job.get("planfix_source_name", ""),
    }
    try:
        body, boundary = encode_multipart_form(
            fields,
            PLANFIX_RESULT_FILE_FIELD,
            txt_path,
            upload_name,
        )
        request = UrlRequest(
            webhook_url,
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "application/json",
                "User-Agent": "whisper-transcriber/1.0",
            },
            method="POST",
        )
        with urlopen(request, timeout=PLANFIX_RESULT_TIMEOUT) as response:
            response_body = response.read(2000).decode("utf-8", errors="replace")
            result = {
                "sent": True,
                "mode": "multipart_webhook",
                "status_code": response.status,
                "txt_file_name": txt_path.name,
                "uploaded_file_name": ascii_multipart_filename(upload_name),
                "txt_size_bytes": txt_path.stat().st_size,
                "response_preview": response_body[:500],
            }
        planfix_log(
            "TXT-файл отправлен в файловый инфоблок Planfix",
            **result,
            task_id=job.get("planfix_task_id"),
            job_id=job.get("job_id"),
        )
        return result
    except Exception as error:
        result = {
            "sent": False,
            "mode": "multipart_webhook",
            "error": str(error),
            "txt_file_name": txt_path.name,
            "txt_size_bytes": txt_path.stat().st_size if txt_path.exists() else 0,
        }
        planfix_log(
            "ошибка отправки TXT-файла в Planfix webhook",
            **result,
            task_id=job.get("planfix_task_id"),
            job_id=job.get("job_id"),
        )
        return result


def process_job(job: dict):
    job_id = job["job_id"]
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

        with open(log_path, "a", encoding="utf-8") as lf:
            set_log_stream(lf)
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
                set_log_stream(None)

        planfix_result = None
        if job.get("source") == "planfix_audio_parse":
            planfix_result = send_planfix_result(job, txt_path)

        update_status(
            job_id,
            status="done",
            finished_at=now_iso(),
            txt_path=str(txt_path),
            docx_path=str(docx_path),
            log_path=str(log_path),
            planfix_result=planfix_result,
        )
    except Exception as e:
        update_status(job_id, status="error", error=str(e), log_path=str(log_path))
        with open(log_path, "a", encoding="utf-8") as lf:
            lf.write("\nERROR: " + repr(e) + "\n")
def process_ready_jobs_once() -> int:
    processed = 0
    ready_files = sorted(JOBS_DIR.glob("*.ready.json"))
    for rf in ready_files:
        try:
            job = read_json(rf)
            job_id = job["job_id"]
            # One GPU processes one job at a time. This also keeps model memory
            # and temporary audio files isolated for multi-attachment comments.
            consumed = JOBS_DIR / f"{job_id}.processing.json"
            try:
                rf.replace(consumed)
            except FileNotFoundError:
                continue
            process_job(job)
            processed += 1
        except Exception as e:
            print(f"Ошибка чтения ready job {rf}: {e}", flush=True)
    return processed


def worker_loop():
    while True:
        try:
            process_ready_jobs_once()
        except Exception as e:
            print(f"Ошибка worker_loop: {e}", flush=True)
        time.sleep(2)


@app.get("/health")
def health():
    return {"status": "ok", "gpu": gpu_status(), "data_dir": str(DATA_DIR)}


@app.post("/planfix/comment-audio")
@app.post("/planfix/audio-parse")
async def planfix_audio_parse(request: Request, background_tasks: BackgroundTasks):
    try:
        payload, uploaded_files = await parse_planfix_audio_payload(request)
    except ClientDisconnect:
        planfix_log(
            "запрос оборван клиентом во время чтения тела",
            content_type=request.headers.get("content-type", ""),
            content_length=request.headers.get("content-length", ""),
        )
        return JSONResponse(
            status_code=499,
            content={
                "detail": (
                    "Клиент разорвал соединение до того, как сервер дочитал тело запроса. "
                    "Для больших файлов передавайте file_url, а не file_base64."
                )
            },
        )

    task_id = first_present(
        payload,
        [
            "task_id",
            "taskId",
            "task",
            "planfix_task_id",
            "object_task_id",
            "object_id",
            "objectId",
            "objectid",
        ],
    )
    company = first_present(
        payload,
        [
            "company",
            "company_name",
            "companyName",
            "counterparty",
            "client",
            "organization",
            "Компания",
            "Контрагент",
        ],
        default="Без компании",
    )

    if not task_id:
        raise HTTPException(
            status_code=400,
            detail="Передайте task_id из Planfix, чтобы сервис знал, у какой задачи брать файлы",
        )

    request_id = uuid.uuid4().hex[:10]
    comment = planfix_comment_metadata(payload)
    request_files, rejected_files = filter_audio_request_items(
        extract_request_file_items(payload, uploaded_files)
    )
    if not request_files:
        planfix_log(
            "комментарий пропущен: аудиофайл не найден",
            request_id=request_id,
            task_id=task_id,
            comment_id=comment["comment_id"],
            payload_keys=sorted(payload.keys()),
            rejected_files=[item.get("name") for item in rejected_files],
        )
        return {
            "status": "ignored",
            "reason": "no_audio_files",
            "request_id": request_id,
            "task_id": task_id,
            "comment_id": comment["comment_id"],
            "rejected_files_count": len(rejected_files),
        }

    reserved, event_receipt_path, previous_receipt = reserve_planfix_event(
        task_id,
        comment,
        request_id,
        request_files,
    )
    if not reserved:
        planfix_log(
            "повторное событие комментария пропущено",
            request_id=request_id,
            task_id=task_id,
            comment_id=comment["comment_id"],
            previous_request_id=previous_receipt.get("request_id"),
        )
        return {
            "status": "duplicate",
            "request_id": previous_receipt.get("request_id"),
            "task_id": task_id,
            "comment_id": comment["comment_id"],
            "files_count": len(request_files),
        }

    planfix_log(
        "событие нового комментария принято",
        request_id=request_id,
        task_id=task_id,
        comment_id=comment["comment_id"],
        comment_author=comment["comment_author"],
        company=company,
        payload_keys=sorted(payload.keys()),
        files_count=len(request_files),
        rejected_files=[item.get("name") for item in rejected_files],
    )
    background_tasks.add_task(
        handle_planfix_audio_parse_request,
        request_id,
        task_id,
        company,
        payload,
        request_files,
        event_receipt_path,
    )
    return {
        "status": "accepted",
        "request_id": request_id,
        "task_id": task_id,
        "comment_id": comment["comment_id"],
        "company": company,
        "files_count": len(request_files),
        "rejected_files_count": len(rejected_files),
        "create_jobs": bool_value(
            first_scalar(payload.get("create_jobs")),
            default=bool_value(os.getenv("PLANFIX_CREATE_TRANSCRIBE_JOBS"), True),
        ),
        "log": str(LOGS_DIR / "planfix_audio_parse.log"),
    }


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
