import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


def load_api_module(data_dir: str):
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

    os.environ["DATA_DIR"] = data_dir

    api_path = Path(__file__).parents[1] / "app" / "api.py"
    spec = importlib.util.spec_from_file_location("test_planfix_api", api_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PlanfixCommentPayloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.api = load_api_module(cls.temp_dir.name)

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
                    "planfix_task_id": "42",
                    "planfix_project": "Project",
                    "planfix_source_name": "meeting.m4a",
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

if __name__ == "__main__":
    unittest.main()
