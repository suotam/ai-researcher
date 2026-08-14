from src.processing.normalize import (
    canonicalize_url,
    clean_text,
    content_hash,
    normalize_title,
)


class TestCanonicalizeUrl:
    def test_strips_tracking_params(self):
        assert canonicalize_url(
            "https://example.com/a?utm_source=x&utm_medium=y&id=5"
        ) == "https://example.com/a?id=5"

    def test_http_equals_https(self):
        assert canonicalize_url("http://example.com/a") == canonicalize_url(
            "https://example.com/a")

    def test_www_and_trailing_slash(self):
        assert canonicalize_url("https://www.example.com/a/") == "https://example.com/a"

    def test_fragment_dropped(self):
        assert canonicalize_url("https://example.com/a#section") == "https://example.com/a"

    def test_query_order_normalized(self):
        assert canonicalize_url("https://example.com/a?b=2&a=1") == canonicalize_url(
            "https://example.com/a?a=1&b=2")

    def test_empty(self):
        assert canonicalize_url("") == ""
        assert canonicalize_url(None) == ""


class TestNormalizeTitle:
    def test_case_punctuation_whitespace(self):
        assert normalize_title("  OpenAI Launches   GPT-5!!  ") == "openai launches gpt 5"

    def test_hash_stable_across_variants(self):
        assert content_hash("OpenAI launches GPT-5!") == content_hash(
            "openai   launches gpt-5")

    def test_hash_differs_for_different_titles(self):
        assert content_hash("Fed cuts rates") != content_hash("Fed hikes rates")


class TestCleanText:
    def test_strips_html(self):
        assert clean_text("<p>Hello <b>world</b></p>") == "Hello world"

    def test_caps_length(self):
        assert len(clean_text("x" * 5000, max_chars=100)) == 100
