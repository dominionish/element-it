# n8n + Whisper Transcriber Docker

## Что внутри

- `transcriber` — Docker-контейнер с Whisper, ffmpeg, API и watcher'ом папки задач.
- `n8n` — отдельный контейнер для автоматизаций.
- `data/input` — входные файлы.
- `data/output` — результаты `.txt`, `.docx`, `_meta.json`.
- `data/jobs` — JSON-задачи и статусы.
- `data/logs` — логи обработки.
- `models` — кеш моделей Whisper.

## Запуск GPU

```bat
run_all_gpu.bat
```

GPU-образ фиксирует PyTorch на `torch==2.7.1+cu126`. Это совместимо с драйверами, которые показывают CUDA `12.7` в `nvidia-smi`; без фикса `openai-whisper` может подтянуть слишком новый PyTorch с CUDA `13.0`, и тогда `torch.cuda.is_available()` станет `False`.

После запуска:

- n8n: http://localhost:5678
- Transcriber API: http://localhost:7861
- Проверка API: http://localhost:7861/health

## Запуск CPU

```bat
run_all_cpu.bat
```

CPU будет сильно медленнее. Для нормальной работы нужен GPU.

## Docker image CI

GitHub Actions собирает и публикует образ transcriber в GitHub Container Registry:

```text
ghcr.io/dominionish/element-it/transcriber
```

Публикация запускается после успешных базовых CI-проверок:

- автоматически при `push` в `main` или `master`;
- вручную через `workflow_dispatch`.

Основные теги:

- `latest` — последний успешный образ из `main` или `master`;
- `main` / `master` — образ из соответствующей ветки;
- `sha-<commit>` — точный образ конкретного коммита.

Для продового запуска укажи в `.env`:

```text
TRANSCRIBER_IMAGE=ghcr.io/dominionish/element-it/transcriber:latest
```

## Быстрая проверка без n8n

Открой:

```text
http://localhost:7861
```

Загрузи файл через простую форму. Получишь `job_id`.

Проверка статуса:

```text
http://localhost:7861/jobs/JOB_ID
```

Скачать результат:

```text
http://localhost:7861/download/JOB_ID/txt
http://localhost:7861/download/JOB_ID/docx
```

## Как это должно работать через n8n

Правильная схема:

1. n8n принимает файл от пользователя.
2. n8n сохраняет файл в `/data/input/<job_id>/source.m4a`.
3. n8n создаёт JSON-задачу в `/data/jobs/<job_id>.ready.json`.
4. transcriber каждые 2 секунды смотрит папку `/data/jobs`.
5. Когда видит `.ready.json`, запускает транскрибацию.
6. Результат кладёт в `/data/output/<job_id>/`.
7. n8n проверяет `/data/jobs/<job_id>.status.json` или API `/jobs/<job_id>`.
8. n8n возвращает пользователю `.docx` или `.txt`.

## Аудио из комментариев Planfix

Ручная кнопка больше не нужна. В Planfix создай автоматический сценарий:

1. Событие: `Добавлен комментарий и задача соответствует условиям`.
2. Условие: комментарий содержит файлы, если такое условие доступно в вашей конфигурации.
3. Операция: `Послать HTTP-запрос`.
4. Метод: `POST`.
5. Тип: `application/json`.

Если отдельного условия для файлов нет, сценарий можно запускать на каждый комментарий. Комментарии без аудио endpoint штатно пропускает с ответом `status=ignored`.

Основной endpoint:

```text
POST /planfix/comment-audio
```

Старый `/planfix/audio-parse` оставлен как совместимый алиас. Если публичный zrok-адрес смотрит прямо на Transcriber API (`7861`), укажи:

```text
https://my-transcriber.shares.zrok.io/planfix/comment-audio
```

В параметрах HTTP-запроса добавь:

| Параметр | Динамическое значение Planfix |
| --- | --- |
| `task_id` | Номер или ID текущей задачи |
| `project` | Проект текущей задачи |
| `company` | `Системные → Домен аккаунта` |
| `comment_id` | ID добавленного комментария |
| `comment_text` | Текст добавленного комментария |
| `comment_author` | Автор добавленного комментария |
| `comment_files` | Файлы добавленного комментария |
| `create_jobs` | `true` |

Названия полей события могут немного отличаться между конфигурациями Planfix. Выбирай их через список динамических значений именно из блока добавленного комментария, а не из поля задачи, где раньше хранилась запись.

Предпочтительный JSON после подстановки Planfix:

```json
{
  "task_id": "12345",
  "project": "Основной проект",
  "company": "company.planfix.ru",
  "comment_id": "98765",
  "comment_text": "Запись разговора",
  "comment_author": "Иван Иванов",
  "comment_files": [
    {
      "name": "voice.m4a",
      "url": "https://company.planfix.ru/..."
    }
  ],
  "create_jobs": true
}
```

Если Planfix не дает передать весь список `comment_files`, передай имя и ссылку отдельными параметрами:

```json
{
  "task_id": "{{Задача.Номер}}",
  "company": "{{Системные.Домен аккаунта}}",
  "comment_id": "{{ID добавленного комментария}}",
  "file_name": "{{Добавленный комментарий.Файлы.Имя}}",
  "file_url": "{{Добавленный комментарий.Файлы.Ссылка}}",
  "create_jobs": true
}
```

Endpoint понимает массивы, JSON-строки, повторяющиеся form-параметры и HTML-список ссылок, который Planfix может сформировать для нескольких файлов. Обрабатываются только аудио и видео; остальные вложения комментария игнорируются. Base64 и обычный `multipart/form-data` с полем `file` также поддерживаются, но для больших записей надежнее `file_url`.

Нужные переменные окружения для этого режима:

```text
PLANFIX_AUDIO_EXTENSIONS=.mp3,.m4a,.wav,.ogg,.opus,.webm,.aac,.flac,.mp4,.mov,.mkv,.avi
PLANFIX_CREATE_TRANSCRIBE_JOBS=true
PLANFIX_ALLOWED_FILE_URL_HOSTS=planfix.ru,.planfix.ru
PLANFIX_FILE_URL_TIMEOUT=120
```

Что делает endpoint:

- сразу отвечает Planfix `accepted`, не заставляя сценарий ждать загрузку большого файла;
- пропускает текстовые комментарии и комментарии только с документами;
- в фоне сохраняет аудио/видео в `data/input/planfix/<task_id>_<comment_id>_<request_id>/`;
- не запускает повторную транскрибацию при повторной доставке того же `comment_id` с теми же файлами;
- пишет лог в `data/logs/planfix_audio_parse.log`;
- если передать `create_jobs=true` или выставить `PLANFIX_CREATE_TRANSCRIBE_JOBS=true`, сразу создает задачи на транскрибацию.

## Возврат TXT-файла в Planfix

После завершения транскрибации сервис отправляет готовый `.txt` во входящий вебхук Planfix как настоящий файл `multipart/form-data`. Planfix помещает полученный файл в инфоблок, а операция `Прикрепить файлы -> Из инфоблока` прикрепляет его к комментарию.

Важно: JSON-параметр `txt_url` создаёт строковый инфоблок. Выбрать его в операции можно, но он не становится файлом автоматически.

В `.env`:

```text
PLANFIX_RESULT_WEBHOOK_ID=<id входящего вебхука>
PLANFIX_RESULT_WEBHOOK_URL=
PLANFIX_RESULT_WEBHOOK_METHOD=MULTIPART
PLANFIX_RESULT_FILE_FIELD=txt_file
PLANFIX_RESULT_TIMEOUT=120
PLANFIX_ALLOWED_API_HOSTS=planfix.ru,.planfix.ru
```

В Planfix создай или измени входящий вебхук:

1. Тип вебхука: `POST-запрос в формате multipart/form-data`.
2. Добавь параметр `task` и сохрани его в инфоблок `Задача`.
3. Добавь параметр `file_name` и сохрани его в инфоблок `Исходный файл`.
4. Добавь параметр `txt_file` и сохрани его в новый инфоблок `TXT файл`.
5. В основной операции найди задачу по инфоблоку `Задача`.
6. Выбери `Добавить комментарий`.
7. В `Прикрепить файлы` выбери `Из инфоблока -> TXT файл`.

В поле текста комментария можно указать:

```text
Расшифровка файла: {{Исходный файл}}
```

Сервис отправляет запрос на:

```text
https://<company>/webhook/file/<PLANFIX_RESULT_WEBHOOK_ID>
```

В исходном автоматическом сценарии Planfix продолжай передавать:

```json
{
  "task_id": "{{Задача.Номер}}",
  "project": "{{Задача.Проект}}",
  "company": "{{Системные.Домен аккаунта}}",
  "comment_id": "{{ID добавленного комментария}}",
  "file_name": "{{Добавленный комментарий.Файлы.Имя}}",
  "file_url": "{{Добавленный комментарий.Файлы.Ссылка}}",
  "create_jobs": true
}
```

REST API остаётся резервным режимом, если `PLANFIX_RESULT_WEBHOOK_ID` и `PLANFIX_RESULT_WEBHOOK_URL` не заданы. Для него нужны `PLANFIX_API_TOKEN`, scope `file_add` и `comment_add`.

## Формат ready-задачи

```json
{
  "job_id": "test001",
  "input_path": "/data/input/test001/source.m4a",
  "model": "medium",
  "chunk_minutes": 6,
  "enhance_audio": true,
  "output_name": "Расшифровка_совещания",
  "auto_safe_model": true
}
```

Файл должен называться:

```text
/data/jobs/test001.ready.json
```

## Альтернативный вариант для n8n через HTTP

n8n может не создавать `.ready.json`, а вызвать API:

```text
POST http://transcriber:7861/jobs
```

form-data:

```text
input_path=/data/input/test001/source.m4a
model=medium
chunk_minutes=6
enhance_audio=true
```

Внутри docker-сети использовать именно:

```text
http://transcriber:7861
```

Не `localhost`. `localhost` внутри n8n — это сам контейнер n8n.

## Рекомендуемые модели

- `small` — быстро, но грязно.
- `medium` — рабочий режим по умолчанию.
- `large-v3` — максимум качества, но может не влезть в 6 GB VRAM. В этом случае контейнер сам откатится на `medium`.

## Тест watcher без n8n

Перетащи аудио/видео на:

```bat
create_ready_job_example.bat
```

Скрипт положит файл в `data/input`, создаст `.ready.json`, а transcriber сам подхватит задачу.
