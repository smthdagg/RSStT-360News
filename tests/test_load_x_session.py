import unittest

from scripts.load_x_session import parse_cookie_header, parse_netscape_cookie_export
from src.x_browser_fetcher import sanitize_x_cookies


class NetscapeCookieExportTests(unittest.TestCase):
    def test_parses_x_cookies_and_skips_non_x_domains(self):
        cookies = sanitize_x_cookies(parse_netscape_cookie_export(
            "# Netscape HTTP Cookie File\n"
            ".x.com\tTRUE\t/\tTRUE\t1820361954\tauth_token\tsecret\n"
            "x.com\tFALSE\t/\tFALSE\t0\tct0\tcsrf\n"
            ".example.com\tTRUE\t/\tTRUE\t0\tforeign\tno\n"
        ))

        self.assertEqual(
            [(cookie["name"], cookie["domain"], cookie.get("expires")) for cookie in cookies],
            [("auth_token", ".x.com", 1820361954), ("ct0", "x.com", None)],
        )

    def test_parses_cookie_header(self):
        cookies = parse_cookie_header("auth_token=secret; ct0=csrf")
        self.assertEqual([cookie["name"] for cookie in cookies], ["auth_token", "ct0"])


if __name__ == "__main__":
    unittest.main()
