import unittest

from url_history import remember_url


class HistoryTests(unittest.TestCase):
    def test_latest_ten_unique_links(self):
        history = [f"https://example.com/{i}" for i in range(10)]
        updated = remember_url(history, " https://example.com/5 ")
        self.assertEqual(len(updated), 10)
        self.assertEqual(updated[0], "https://example.com/5")
        self.assertEqual(updated.count("https://example.com/5"), 1)
        newer = remember_url(updated, "https://example.com/new")
        self.assertEqual(len(newer), 10)
        self.assertNotIn("https://example.com/9", newer)


if __name__ == "__main__":
    unittest.main()
