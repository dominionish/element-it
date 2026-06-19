import importlib.util
import os
import sys
import tempfile
import types
import unittest
import json
from pathlib import Path


def install_transcriber_stubs():
    transcribe_stub = types.ModuleType("transcribe_original")
    transcribe_stub.transcribe = lambda **kwargs: (None, None)
    transcribe_stub.gpu_status = lambda: {"cuda_available": False, "device": "cpu"}
    transcribe_stub.set_log_stream = lambda stream: None
    sys.modules["transcribe_original"] = transcribe_stub

    multipart_stub = types.ModuleType("python_multipart")
    multipart_stub.__version__ = "0.0.20"
    multipart_parser_stub = types.ModuleType("python_multipart.multipart")
    multipart_parser_stub.parse_options_header = lambda value: (value, {})
    sys.modules["python_multipart"] = multipart_stub
    sys.modules["python_multipart.multipart"] = multipart_parser_stub


def install_analysis_stubs():
    docx_stub = types.ModuleType("docx")
    docx_stub.Document = object
    docx_enum_stub = types.ModuleType("docx.enum")
    docx_enum_text_stub = types.ModuleType("docx.enum.text")
    docx_enum_text_stub.WD_ALIGN_PARAGRAPH = types.SimpleNamespace(CENTER=1)
    docx_shared_stub = types.ModuleType("docx.shared")
    docx_shared_stub.Pt = lambda value: value
    sys.modules["docx"] = docx_stub
    sys.modules["docx.enum"] = docx_enum_stub
    sys.modules["docx.enum.text"] = docx_enum_text_stub
    sys.modules["docx.shared"] = docx_shared_stub


def load_app_module(file_name: str, module_name: str, data_dir: str):
    os.environ["DATA_DIR"] = data_dir
    app_path = Path(__file__).parents[1] / "app" / file_name
    spec = importlib.util.spec_from_file_location(module_name, app_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PlanfixCommentPayloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        os.environ["PLANFIX_TRANSCRIBER_URL"] = "http://transcriber:7861"
        cls.api = load_app_module("planfix_service.py", "test_planfix_service", cls.temp_dir.name)

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def test_extracts_nested_comment_files_and_rejects_non_audio(self):
        payload = {
            "task_id": "42",
            "comment": {
                "id": "comment-7",
                "files": [
                    {"name": "voice.ogg", "url": "https://example.planfix.ru/voice.ogg"},
                    {"name": "notes.pdf", "url": "https://example.planfix.ru/notes.pdf"},
                ],
            },
        }

        items = self.api.extract_request_file_items(payload, [])
        accepted, rejected = self.api.filter_audio_request_items(items)
        metadata = self.api.planfix_comment_metadata(payload)

        self.assertEqual(["voice.ogg"], [item["name"] for item in accepted])
        self.assertEqual(["notes.pdf"], [item["name"] for item in rejected])
        self.assertEqual("comment-7", metadata["comment_id"])

    def test_extracts_multiple_html_links_from_comment_files(self):
        payload = {
            "comment_files": (
                '<a href="https://example.planfix.ru/one.mp3">one.mp3</a><br>'
                '<a href="https://example.planfix.ru/two.m4a">two.m4a</a>'
            )
        }

        items = self.api.extract_request_file_items(payload, [])

        self.assertEqual(["one.mp3", "two.m4a"], [item["name"] for item in items])

    def test_extracts_multiple_html_links_from_file_url_fallback(self):
        payload = {
            "file_url": (
                '<a href="https://example.planfix.ru/one.mp3">one.mp3</a><br>'
                '<a href="https://example.planfix.ru/two.m4a">two.m4a</a>'
            )
        }

        items = self.api.extract_request_file_items(payload, [])

        self.assertEqual(["one.mp3", "two.m4a"], [item["name"] for item in items])

    def test_comment_id_prevents_duplicate_processing(self):
        metadata = {"comment_id": "comment-9", "comment_text": "", "comment_author": "", "event_id": ""}
        first_file = [{"source": "url", "name": "one.mp3", "url": "https://example.planfix.ru/one.mp3?token=1"}]
        refreshed_link = [{"source": "url", "name": "one.mp3", "url": "https://example.planfix.ru/one.mp3?token=2"}]
        second_file = [{"source": "url", "name": "two.mp3", "url": "https://example.planfix.ru/two.mp3"}]

        first, _, _ = self.api.reserve_planfix_event("task-42", metadata, "request-1", first_file)
        second, _, previous = self.api.reserve_planfix_event("task-42", metadata, "request-2", refreshed_link)
        different_file, _, _ = self.api.reserve_planfix_event("task-42", metadata, "request-3", second_file)

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertTrue(different_file)
        self.assertEqual("request-1", previous["request_id"])

    def test_planfix_creates_transcriber_job_through_http_api(self):
        captured = {}

        def fake_transcriber_request(path, data=None, timeout=None):
            captured["path"] = path
            captured["data"] = data
            captured["timeout"] = timeout
            return {"job_id": "job-1", "status": "queued"}

        audio_path = Path(self.temp_dir.name) / "voice.ogg"
        audio_path.write_bytes(b"audio")
        original_request = self.api.transcriber_json_request
        self.api.transcriber_json_request = fake_transcriber_request
        try:
            job_id = self.api.create_planfix_transcription_job(
                audio_path,
                "42",
                "example.planfix.ru",
                "voice.ogg",
                {"project": "Project", "model": "small"},
                None,
            )
        finally:
            self.api.transcriber_json_request = original_request

        queued = self.api.read_json(self.api.planfix_result_queue_file("job-1"))
        self.assertEqual("job-1", job_id)
        self.assertEqual("/jobs", captured["path"])
        self.assertEqual(str(audio_path), captured["data"]["input_path"])
        self.assertEqual("small", captured["data"]["model"])
        self.assertEqual("waiting", queued["status"])
        self.assertEqual("42", queued["task_id"])

    def test_result_webhook_sends_txt_as_multipart_file(self):
        captured = {}

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self, size=-1):
                return b'{"result":"success"}'

        def fake_urlopen(request, timeout):
            captured["request"] = request
            return Response()

        txt_path = Path(self.temp_dir.name) / "result.txt"
        txt_path.write_text("transcribed text", encoding="utf-8")
        original_urlopen = self.api.urlopen
        original_webhook_id = self.api.PLANFIX_RESULT_WEBHOOK_ID
        self.api.urlopen = fake_urlopen
        self.api.PLANFIX_RESULT_WEBHOOK_ID = "webhook-id"
        try:
            result = self.api.send_planfix_result(
                {
                    "job_id": "job-1",
                    "task_id": "42",
                    "project": "Project",
                    "source_name": "meeting.m4a",
                    "company": "example.planfix.ru",
                },
                txt_path,
            )
        finally:
            self.api.urlopen = original_urlopen
            self.api.PLANFIX_RESULT_WEBHOOK_ID = original_webhook_id

        request = captured["request"]
        self.assertTrue(result["sent"])
        self.assertEqual("https://example.planfix.ru/webhook/file/webhook-id", request.full_url)
        self.assertTrue(request.get_header("Content-type").startswith("multipart/form-data; boundary="))
        self.assertIn(b'name="task"\r\n\r\n42', request.data)
        self.assertNotIn(b'name="txt_file_name"', request.data)
        self.assertNotIn(b'name="txt_base64"', request.data)
        self.assertIn(b'name="txt_file"; filename="meeting.txt"', request.data)
        self.assertIn(b"transcribed text", request.data)

    def test_cyrillic_result_filename_is_transliterated_for_planfix(self):
        name = self.api.ascii_multipart_filename("Седая ночь - Юрий Шатунов.txt")

        self.assertEqual("Sedaya noch - Yuriy Shatunov.txt", name)
        self.assertNotIn("?", name)

    def test_analysis_stage_downloads_and_sends_both_documents(self):
        queue_path = self.api.PLANFIX_RESULTS_DIR / "analysis-stage.json"
        job = {
            "status": "analysis_waiting",
            "job_id": "transcription-job",
            "analysis_job_id": "analysis-job",
            "analysis_polls": 0,
            "task_id": "42",
            "project": "Project",
            "company": "example.planfix.ru",
            "source_name": "meeting.m4a",
        }
        self.api.write_json(queue_path, job)
        sent_types = []

        original_status = self.api.analysis_json_request
        original_download = self.api.analysis_download_file
        original_send = self.api.send_planfix_result
        self.api.analysis_json_request = lambda path: {"status": "done"}
        self.api.analysis_download_file = lambda path, target: target.write_bytes(b"docx")

        def fake_send(job_data, path, **kwargs):
            sent_types.append(kwargs["document_type"])
            return {"sent": True, "file_name": path.name}

        self.api.send_planfix_result = fake_send
        try:
            processed = self.api.process_analysis_result_stage(queue_path, job)
        finally:
            self.api.analysis_json_request = original_status
            self.api.analysis_download_file = original_download
            self.api.send_planfix_result = original_send

        saved = self.api.read_json(queue_path)
        self.assertEqual(1, processed)
        self.assertEqual("analysis_sent", saved["status"])
        self.assertEqual(["essay", "protocol"], sent_types)


class MeetingAnalysisApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        install_analysis_stubs()
        cls.api = load_app_module("analysis_service.py", "test_analysis_service", cls.temp_dir.name)

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def test_proxyapi_request_uses_chat_completions_model_prompt_and_schema(self):
        captured = {}
        result = {
            "essay": "Краткое эссе",
            "meeting_title": "Совещание",
            "meeting_date": "Не указана",
            "participants": [],
            "summary": "Итоги",
            "agenda": [],
            "decisions": [],
            "assignments": [],
        }

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return json.dumps({
                    "id": "chatcmpl-1",
                    "model": "gpt-5-mini-2025-08-07",
                    "choices": [{
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(result, ensure_ascii=False)},
                    }],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50},
                }, ensure_ascii=False).encode("utf-8")

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return Response()

        original_urlopen = self.api.urlopen
        original_key = self.api.PROXYAPI_API_KEY
        self.api.urlopen = fake_urlopen
        self.api.PROXYAPI_API_KEY = "test-key"
        try:
            response = self.api.proxyapi_request("Текст стенограммы")
        finally:
            self.api.urlopen = original_urlopen
            self.api.PROXYAPI_API_KEY = original_key

        request_payload = json.loads(captured["request"].data.decode("utf-8"))
        self.assertEqual("gpt-5-mini-2025-08-07", request_payload["model"])
        self.assertIn(self.api.MEETING_ANALYSIS_PROMPT, request_payload["messages"][1]["content"])
        self.assertEqual("json_schema", request_payload["response_format"]["type"])
        self.assertEqual(
            "meeting_analysis",
            request_payload["response_format"]["json_schema"]["name"],
        )
        self.assertEqual("Краткое эссе", response["essay"])
        self.assertEqual("chatcmpl-1", response["provider_response_id"])

    def test_create_job_is_idempotent_for_request_key(self):
        request = self.api.AnalysisJobRequest(
            transcript="Текст стенограммы",
            task_id="42",
            source_name="meeting.m4a",
            request_key="transcription-job-42",
        )

        first = self.api.create_job(request)
        second = self.api.create_job(request)
        status = self.api.read_json(self.api.status_file(first["job_id"]))

        self.assertEqual(first["job_id"], second["job_id"])
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual("queued", status["status"])
        self.assertEqual(first["job_id"], status["job_id"])


class TranscriberQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        install_transcriber_stubs()
        cls.api = load_app_module("api.py", "test_transcriber_api", cls.temp_dir.name)

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def test_ready_jobs_are_processed_sequentially(self):
        order = []
        original_process_job = self.api.process_job
        self.api.process_job = lambda job: order.append(job["job_id"])
        try:
            self.api.write_json(self.api.ready_file("job-2"), {"job_id": "job-2"})
            self.api.write_json(self.api.ready_file("job-1"), {"job_id": "job-1"})

            processed = self.api.process_ready_jobs_once()
        finally:
            self.api.process_job = original_process_job

        self.assertEqual(2, processed)
        self.assertEqual(["job-1", "job-2"], order)


if __name__ == "__main__":
    unittest.main()
