from __future__ import annotations

import unittest
from urllib.error import URLError
from urllib.request import Request

from tools.oda_snapshot_head import SameOriginRedirectHandler, safe_origin


class SnapshotHeadSafetyTests(unittest.TestCase):
    def test_safe_origin_redacts_path_query_and_userinfo(self) -> None:
        self.assertEqual(
            safe_origin("https://user:secret@oda.ft.dk/private/path?token=hidden"),
            "https://oda.ft.dk",
        )

    def test_same_origin_https_redirect_is_allowed(self) -> None:
        handler = SameOriginRedirectHandler()
        request = Request("https://oda.ft.dk/odapublish/oda.bak", method="HEAD")
        redirected = handler.redirect_request(
            request,  # type: ignore[arg-type]
            None, 302, "Found", {}, "https://oda.ft.dk/odapublish/current.bak",
        )
        self.assertIsNotNone(redirected)
        self.assertEqual(redirected.get_method(), "HEAD")  # type: ignore[union-attr]

    def test_external_or_insecure_redirect_is_blocked(self) -> None:
        handler = SameOriginRedirectHandler()
        request = Request("https://oda.ft.dk/odapublish/oda.bak", method="HEAD")
        for target in (
            "https://example.org/backup",
            "http://oda.ft.dk/backup",
            "https://oda.ft.dk.evil.example/backup",
        ):
            with self.subTest(target=target), self.assertRaises(URLError):
                handler.redirect_request(request, None, 302, "Found", {}, target)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
