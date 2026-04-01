# tests/test_website_crawl.py
"""Tests for website crawler."""

from scrapers.website_crawl import classify_page, normalize_url, should_crawl


def test_classify_page_donate():
    assert classify_page("https://cfi.org/donate", "Donate Now", "") == "donate"


def test_classify_page_about():
    assert classify_page("https://cfi.org/about-us", "About Us", "") == "about"


def test_classify_page_faq():
    assert classify_page("https://cfi.org/faq", "FAQ", "") == "faq"


def test_classify_page_news():
    assert classify_page("https://cfi.org/news/update", "News", "") == "news"


def test_classify_page_fallback():
    assert classify_page("https://cfi.org/xyz", "Random", "") == "other"


def test_normalize_url_strips_fragment():
    assert normalize_url("https://cfi.org/page#section") == "https://cfi.org/page"


def test_normalize_url_strips_trailing_slash():
    assert normalize_url("https://cfi.org/page/") == "https://cfi.org/page"


def test_should_crawl_same_domain():
    assert should_crawl("https://cfi.org/about", "cfi.org") is True


def test_should_crawl_external():
    assert should_crawl("https://google.com", "cfi.org") is False


def test_should_crawl_skip_assets():
    assert should_crawl("https://cfi.org/image.png", "cfi.org") is False
    assert should_crawl("https://cfi.org/style.css", "cfi.org") is False
