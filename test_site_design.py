import unittest
from pathlib import Path


ROOT = Path(__file__).parent


class SiteDesignTests(unittest.TestCase):
    def test_page_uses_openaiclub_brand_and_embedded_logo(self):
        page = (ROOT / "software-site" / "index.html").read_text(encoding="utf-8")

        self.assertIn("OpenAIClub", page)
        self.assertIn('class="brand-logo"', page)
        self.assertIn("<svg", page)
        self.assertNotIn("OpenForge Signal", page)

    def test_video_hostname_is_backend_only(self):
        caddy = (ROOT / "caddy" / "Caddyfile").read_text(encoding="utf-8")
        http_block = caddy.split("http://video.example.com {", 1)[1].split("http://www.example.com", 1)[0]
        https_block = caddy.split("https://video.example.com {", 1)[1]

        self.assertNotIn("handle_path /downloads*", http_block)
        self.assertNotIn("handle_path /downloads*", https_block)
        self.assertIn("redir https://video.example.com:8443{uri}", http_block)
        self.assertIn("reverse_proxy app:5555", https_block)

    def test_www_is_the_canonical_portal_host(self):
        caddy = (ROOT / "caddy" / "Caddyfile").read_text(encoding="utf-8")
        self.assertIn("http://www.example.com {", caddy)
        self.assertIn("https://www.example.com {", caddy)
        www_blocks = caddy.split("http://www.example.com {", 1)[1].split("# Keep", 1)[0]
        self.assertIn("root * /srv/software", www_blocks)
        self.assertIn("file_server", www_blocks)

    def test_page_has_explicit_wide_narrow_layouts_and_larger_logo(self):
        page = (ROOT / "software-site" / "index.html").read_text(encoding="utf-8")

        self.assertIn("@media(min-width:1200px)", page)
        self.assertIn("@media(max-width:620px)", page)
        self.assertIn(".brand-logo{width:48px;height:48px", page)
        self.assertIn('viewBox="0 0 64 64"', page)

    def test_narrow_hero_prevents_grid_content_overflow(self):
        page = (ROOT / "software-site" / "index.html").read_text(encoding="utf-8")

        self.assertIn(".hero-grid>*{min-width:0}", page)
        self.assertIn("width:100%;max-width:445px;min-width:0", page)
        self.assertIn(".signal-line span{min-width:0", page)
        self.assertIn("grid-template-columns:minmax(0,1fr)", page)
        self.assertIn(".hero p{max-width:100%", page)
        self.assertIn(".wrap{width:min(calc(100% - 28px),1160px)}", page)


if __name__ == "__main__":
    unittest.main()
