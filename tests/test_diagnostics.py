import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import diagnostics


class DiagnosticTests(unittest.TestCase):
    def test_opt_in_log_redacts_url_query(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "application.log"
            with patch.object(diagnostics, "LOG_DIR", Path(directory)), \
                 patch.object(diagnostics, "LOG_FILE", path):
                diagnostics.configure_logging(True)
                diagnostics.LOGGER.error("Request failed: https://example.com/video?token=secret")
                diagnostics.LOGGER.error("X-Application: private-value")
                diagnostics.configure_logging(False)
            content = path.read_text(encoding="utf-8")
            self.assertIn("Request failed", content)
            self.assertNotIn("secret", content)
            self.assertNotIn("private-value", content)
            self.assertIn("https://example.com/video", content)


if __name__ == "__main__":
    unittest.main()
