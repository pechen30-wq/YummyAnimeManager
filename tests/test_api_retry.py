import unittest
from unittest.mock import Mock, patch

import requests

from main import YummyApi


def response(status, body=None):
    result = Mock(status_code=status)
    result.json.return_value = body if body is not None else {"response": {"ok": True}}
    if status >= 400:
        result.raise_for_status.side_effect = requests.exceptions.HTTPError(f"HTTP {status}")
    return result


class ApiRetryTests(unittest.TestCase):
    def setUp(self):
        self.api = YummyApi("example-token")

    def test_ssl_eof_then_success_reopens_request(self):
        first = requests.exceptions.SSLError("UNEXPECTED_EOF_WHILE_READING")
        with patch("main.requests.get", side_effect=[first, response(200)]) as get, \
             patch("main.time.sleep") as sleep:
            self.assertEqual(self.api.videos(1372), {"ok": True})
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args.kwargs["timeout"], (5, 20))
        self.assertEqual(get.call_args.kwargs["headers"]["X-Application"], "example-token")
        sleep.assert_called_once()

    def test_temporary_server_error_then_success(self):
        with patch("main.requests.get", side_effect=[response(503), response(200)]) as get, \
             patch("main.time.sleep"):
            self.assertEqual(self.api.anime("example"), {"ok": True})
        self.assertEqual(get.call_count, 2)

    def test_auth_failure_is_not_retried(self):
        with patch("main.requests.get", return_value=response(401)) as get, \
             patch("main.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "X-Application token"):
                self.api.anime("example")
        get.assert_called_once()
        sleep.assert_not_called()

    def test_retries_are_bounded_and_explain_failure(self):
        with patch("main.requests.get", side_effect=requests.exceptions.SSLError("EOF")) as get, \
             patch("main.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "после 4 попыток.*EOF"):
                self.api.videos(1372)
        self.assertEqual(get.call_count, 4)
        self.assertEqual(sleep.call_count, 3)


if __name__ == "__main__":
    unittest.main()
