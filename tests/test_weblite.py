import sys
import types

import httpx
import pytest

from agent import inspect as ins
from agent import web, weblite

REAL_ENGINE = web.engine  # captured at import, before conftest pins it to "firecrawl"

PAGE = b"""<html><head><title>Nosferatu (1922)</title>
<meta property="og:image" content="/img/poster.jpg"><meta name="description" content="A silent film.">
<script>var secret = 1;</script><style>.x{}</style></head>
<body><h1>Nosferatu</h1><p>Download the <a href="/download/nos.mp4">MP4 file</a> or the
<a href="magnet:?xt=urn:btih:abc">torrent</a>.</p><a href="javascript:void(0)">menu</a></body></html>"""


@pytest.fixture
def site(monkeypatch):
    routes = {}

    def handler(req):
        status, headers, body = routes.get((req.headers["host"], req.url.path), (404, {}, b""))
        return httpx.Response(status, headers=headers, content=body)

    monkeypatch.setattr(ins, "_TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setattr(ins, "_resolve", lambda h, p: ["10.0.0.9"] if h == "lan.test" else ["93.184.216.34"])
    return routes


def test_scrape_page_to_text_with_links(site):
    site[("archive.test", "/details/nos")] = (200, {"content-type": "text/html; charset=utf-8"}, PAGE)
    r = weblite.scrape("https://archive.test/details/nos")
    assert r["title"] == "Nosferatu (1922)" and r["description"] == "A silent film."
    assert r["image"] == "https://archive.test/img/poster.jpg"
    assert "[MP4 file](https://archive.test/download/nos.mp4)" in r["text"]
    assert "[torrent](magnet:?xt=urn:btih:abc)" in r["text"]
    assert "# Nosferatu" in r["text"] and "secret" not in r["text"] and "javascript" not in r["text"]
    assert r["text"].startswith("Files linked on this page:\n- [MP4 file](https://archive.test/download/nos.mp4)\n"
                                "- [torrent](magnet:?xt=urn:btih:abc)\n\n")


def test_scrape_follows_redirect_and_truncates(site):
    site[("a.test", "/old")] = (301, {"location": "https://b.test/new"}, b"")
    site[("b.test", "/new")] = (200, {"content-type": "text/html"}, b"<p>" + b"word " * 5000 + b"</p>")
    r = weblite.scrape("https://a.test/old", max_chars=100)
    assert r["url"] == "https://b.test/new" and len(r["text"]) == 100


def test_redirect_into_lan_is_blocked(site):
    site[("a.test", "/go")] = (302, {"location": "http://lan.test/admin"}, b"")
    with pytest.raises(weblite.LiteError, match="blocked"):
        weblite.scrape("https://a.test/go")


def test_files_are_not_scraped(site):
    site[("a.test", "/f.mp4")] = (200, {"content-type": "video/mp4"}, b"\x00" * 10)
    with pytest.raises(weblite.LiteError, match="not a web page"):
        weblite.scrape("https://a.test/f.mp4")


def test_search_maps_ddgs(monkeypatch):
    class DDGS:
        def __init__(self, timeout=None):
            pass

        def text(self, q, max_results=5):
            assert q == "nosferatu"
            return [{"title": "A", "href": "https://a.test/x", "body": "d"}, {"title": "bad", "href": "ftp://x"}]

    monkeypatch.setitem(sys.modules, "ddgs", types.SimpleNamespace(DDGS=DDGS))
    monkeypatch.setattr(weblite, "_archive", lambda q, n: [{"title": "IA", "url": "https://a.test/x", "description": ""},
                                                           {"title": "IA2", "url": "https://archive.org/details/n", "description": ""}])
    assert weblite.search("nosferatu") == [{"title": "A", "url": "https://a.test/x", "description": "d"},  # deduped
                                           {"title": "IA2", "url": "https://archive.org/details/n", "description": ""}]


def test_search_error_is_liteerror(monkeypatch):
    class DDGS:
        def __init__(self, timeout=None):
            pass

        def text(self, *a, **k):
            raise RuntimeError("rate limited")

    monkeypatch.setitem(sys.modules, "ddgs", types.SimpleNamespace(DDGS=DDGS))
    monkeypatch.setattr(weblite, "_archive", lambda q, n: [{"title": "IA", "url": "https://archive.org/details/q", "description": ""}])
    assert [r["url"] for r in weblite.search("q")] == ["https://archive.org/details/q"]  # web engines down: archive.org still answers
    monkeypatch.setattr(weblite, "_archive", lambda q, n: (_ for _ in ()).throw(httpx.ConnectError("offline")))
    with pytest.raises(weblite.LiteError, match="offline"):
        weblite.search("q")


def test_archive_query_and_shape(monkeypatch):
    seen = {}

    def get(url, params=None, **kw):
        seen["q"] = dict(params)["q"]
        return httpx.Response(200, request=httpx.Request("GET", url), json={"response": {"docs": [
            {"identifier": "Nosferatu1922", "title": "Nosferatu (1922)", "mediatype": "movies", "description": ["A", "film"]},
            {"title": "no id"}]}})

    monkeypatch.setattr(weblite.httpx, "get", get)
    r = weblite._archive("Nosferatu 1922 public domain mp4 download", 3)
    assert seen["q"] == "title:(nosferatu 1922)"
    assert r == [{"title": "Nosferatu (1922) (Internet Archive, movies)", "url": "https://archive.org/details/Nosferatu1922",
                  "description": "A film"}]
    assert weblite._archive("free mp4 download", 3) == []


def test_web_uses_lite_when_firecrawl_unreachable(site, monkeypatch):
    monkeypatch.setattr(web, "_client", lambda: pytest.fail("firecrawl must not be called"))
    monkeypatch.setattr(web, "engine", lambda: "lite")
    monkeypatch.setattr(weblite, "search", lambda q, n: [{"title": "T", "url": "https://t.test", "description": ""}])
    assert web.search("q")[0]["url"] == "https://t.test"
    site[("archive.test", "/p")] = (200, {"content-type": "text/html"}, PAGE)
    assert web.scrape("https://archive.test/p")["title"] == "Nosferatu (1922)"
    with pytest.raises(web.WebError, match="^blocked"):  # the guard still runs first
        web.scrape("http://127.0.0.1:8000/api/jobs")


def test_engine_probe(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_URL", "http://127.0.0.1:1")  # nothing listens there
    web._ENGINE.update(at=0.0, url=None, name=None)
    assert REAL_ENGINE() == "lite"


def test_archive_item_lists_files_with_sizes(monkeypatch):
    def get(url, **kw):
        assert url == "https://archive.org/metadata/Nos%201922"
        return httpx.Response(200, request=httpx.Request("GET", url), json={
            "metadata": {"title": "Nosferatu", "licenseurl": "http://creativecommons.org/publicdomain/zero/1.0/",
                         "description": "<b>Silent</b> film"},
            "files": [{"name": "small.ogv", "size": "441700000", "format": "Ogg Video"},
                      {"name": "Nos HD.mp4", "size": "734500000", "format": "MPEG4"},
                      {"name": "x.thumbs/1.jpg", "size": "9", "format": "Thumbnail"},
                      {"name": "Nos_meta.xml", "size": "1"}]})

    monkeypatch.setattr(weblite.httpx, "get", get)
    assert weblite._archive_id("https://archive.org/details/Nos%201922") == "Nos 1922"
    assert weblite._archive_id("https://archive.org/download/x/a.mp4") is None  # a single file: inspect_link's job
    assert weblite._archive_id("https://evil.test/details/x") is None
    r = weblite._archive_item("Nos 1922", 8000)
    lines = r["text"].splitlines()
    files = [l for l in lines if l.startswith("- [")]
    assert files[0] == "- [Nos HD.mp4](https://archive.org/download/Nos%201922/Nos%20HD.mp4) 734.5 MB, MPEG4"
    assert files[1].startswith("- [small.ogv]") and "thumbs" not in r["text"] and "_meta.xml" not in r["text"]
    assert "Licence: http://creativecommons.org/publicdomain/zero/1.0/" in lines and r["description"] == "Silent film"
