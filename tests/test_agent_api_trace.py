import gzip
import json
import tempfile
import unittest
from pathlib import Path

from blender.scripts.agent_api_trace import (
    record_api_exception,
    record_api_response,
    start_api_trace,
)


class Prepared:
    body = b'{"wire":true}'
    headers = {"Authorization": "Bearer secret", "Content-Type": "application/json"}


class Response:
    content = b'{"id":"response-id","choices":[]}'
    status_code = 200
    url = "https://example.invalid/v1/chat?token=secret&mode=test"
    headers = {"X-Request-ID": "response-id", "Set-Cookie": "secret"}
    request = Prepared()


class AgentApiTraceTests(unittest.TestCase):
    def test_full_payload_and_response_are_retained_with_secrets_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trace = start_api_trace(
                Path(temporary),
                stage="codegen/review",
                attempt=1,
                method="POST",
                endpoint="https://example.invalid/v1/chat?api_key=secret&mode=test",
                headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
                payload={"messages": [{"content": "完整输入"}]},
            )
            record_api_response(trace, Response(), elapsed_seconds=1.25)
            with gzip.open(trace / "request_payload.json.gz", "rt", encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["messages"][0]["content"], "完整输入")
            with gzip.open(trace / "response_body.bin.gz", "rb") as handle:
                self.assertEqual(handle.read(), Response.content)
            with gzip.open(trace / "wire_request_body.bin.gz", "rb") as handle:
                self.assertEqual(handle.read(), Prepared.body)
            request_meta = json.loads((trace / "request_meta.json").read_text())
            response_meta = json.loads((trace / "response_meta.json").read_text())
            self.assertEqual(request_meta["headers"]["Authorization"], "<redacted>")
            self.assertNotIn("secret", request_meta["endpoint"])
            self.assertEqual(response_meta["request_headers"]["Authorization"], "<redacted>")
            self.assertEqual(response_meta["headers"]["Set-Cookie"], "<redacted>")

    def test_exception_is_retained(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trace = start_api_trace(
                Path(temporary),
                stage="codegen",
                attempt=2,
                method="POST",
                endpoint="https://example.invalid",
                headers={},
                payload={"prompt": "x"},
            )
            record_api_exception(trace, TimeoutError("black hole"), elapsed_seconds=5.0)
            payload = json.loads((trace / "exception.json").read_text())
            self.assertEqual(payload["exception_type"], "TimeoutError")
            self.assertEqual(payload["message"], "black hole")


if __name__ == "__main__":
    unittest.main()
