import httpx
import pytest

from agent import inspect as I
from agent import mdm, web

DNS = {"example.com": ["93.184.216.34"], "evil.test": ["10.0.0.5"], "rebind.test": ["8.8.8.8", "127.0.0.1"],
       "other.org": ["1.1.1.1"], "bit.ly": ["67.199.248.10"], "v6.test": ["::ffff:7f00:1"],
       "xn--pple-43d.com": ["1.2.3.4"], "files.net": ["5.6.7.8"]}


@pytest.fixture(autouse=True)
def no_video_cli(monkeypatch):
    """HTML pages are probed with `video-info`: tests never spawn the real CLI."""
    monkeypatch.setattr(mdm, "video_info", lambda url: None)


class Routes(dict):
    seen: list


@pytest.fixture
def net(monkeypatch):
    """routes[(host, path)] -> (status, headers) ; records every request."""
    routes, seen = Routes(), []

    def handler(req: httpx.Request):
        host = req.headers["host"]
        seen.append((req.method, host, req.url.host, req.url.path))
        st, hdr = routes.get((host, req.url.path), (404, {}))
        return httpx.Response(st, headers=hdr)

    monkeypatch.setattr(I, "_TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setattr(I, "_resolve", lambda h, p: DNS[h])
    for k in ("FIRECRAWL_API_KEY", "VIRUSTOTAL_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    routes.seen = seen
    return routes


def file(ctype="video/mp4", size="1000", **extra):
    return 200, {"content-type": ctype, "content-length": size, **extra}


def lvl(r):
    return r["safety"]["level"]


def text(r):
    return " ".join(r["safety"]["reasons"])


def test_ok_https(net):
    net[("example.com", "/a.mp4")] = file()
    r = I.inspect_link("https://example.com/a.mp4")
    assert lvl(r) == "ok" and r["filename"] == "a.mp4" and r["size"] == 1000 and r["content_type"] == "video/mp4"
    assert r["kind"] == "http" and r["host"] == "example.com" and r["files"] == []
    assert "VirusTotal: no API key" in r["safety"]["skipped"]
    # HEAD only, connected to the validated IP
    assert net.seen == [("HEAD", "example.com", "93.184.216.34", "/a.mp4")]


@pytest.mark.parametrize("name", ["setup.exe", "x.MSI", "a.ps1", "t.jar", "a.apk", "a.dmg", "a.pkg", "a.com", "a.vbs",
                                  "a.scr", "a.bat", "a.cmd"])
def test_risky_ext(net, name):
    net[("example.com", "/" + name)] = file("application/octet-stream")
    assert lvl(I.inspect_link("https://example.com/" + name)) == "risky"


def test_double_extension(net):
    net[("example.com", "/movie.mp4.exe")] = file("application/octet-stream")
    r = I.inspect_link("https://example.com/movie.mp4.exe")
    assert lvl(r) == "risky" and "double extension" in text(r)


def test_disposition_name_checked(net):
    net[("example.com", "/dl")] = file("application/octet-stream", **{"content-disposition": 'attachment; filename="movie.avi.scr"'})
    r = I.inspect_link("https://example.com/dl")
    assert r["filename"] == "movie.avi.scr" and lvl(r) == "risky" and "double extension" in text(r)


def test_disposition_traversal_stripped(net):
    net[("example.com", "/dl")] = file("video/mp4", **{"content-disposition": 'attachment; filename="../../x.mp4"'})
    assert I.inspect_link("https://example.com/dl")["filename"] == "x.mp4"


def test_http_caution(net):
    net[("example.com", "/a.mp4")] = file()
    r = I.inspect_link("http://example.com/a.mp4")
    assert lvl(r) == "caution" and "TLS" in text(r)


def test_archive_unknown_size_mismatch(net):
    net[("example.com", "/a.iso")] = (200, {"content-type": "text/html"})
    r = I.inspect_link("https://example.com/a.iso")
    assert lvl(r) == "caution" and r["size"] is None
    t = text(r)
    assert "size is unknown" in t and "archive" in t and "suggests" in t


def test_shortener_and_redirect(net):
    net[("bit.ly", "/x")] = (301, {"location": "https://other.org/a.mp4"})
    net[("other.org", "/a.mp4")] = file()
    r = I.inspect_link("https://bit.ly/x")
    assert lvl(r) == "caution" and "shortener" in text(r) and "different site" in text(r)
    assert r["final_url"] == "https://other.org/a.mp4" and r["url"] == "https://bit.ly/x"


def test_ip_literal_and_punycode(net, monkeypatch):
    net[("8.8.8.8", "/a.mp4")] = file()
    assert "raw IP" in text(I.inspect_link("https://8.8.8.8/a.mp4"))
    net[("xn--pple-43d.com", "/a.mp4")] = file()
    assert "punycode" in text(I.inspect_link("https://xn--pple-43d.com/a.mp4"))


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/a", "http://localhost/a", "http://10.1.2.3/a", "http://192.168.0.1/a",
    "http://169.254.169.254/latest/meta-data", "http://[::1]/a", "http://[::ffff:127.0.0.1]/a",
    "http://[::ffff:7f00:1]/a", "http://[fe80::1]/a", "http://2130706433/a", "http://0x7f000001/a",
    "http://0x7f.1/a", "http://017700000001/a", "http://0.0.0.0/a", "http://224.0.0.1/a", "http://[fc00::1]/a",
    "http://100.64.0.1/a", "http://[64:ff9b::7f00:1]/a", "http://evil.test/a", "http://v6.test/a",
    "http://rebind.test/a", "http://localhost./a", "http://foo.localhost/a",
])
def test_ssrf_blocked(net, url):
    r = I.inspect_link(url)
    assert r.get("error", "").startswith("blocked"), r
    assert net.seen == []


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://example.com/", "javascript:alert(1)", "", "example.com/a",
                                 "https://example.com/a b"])
def test_bad_scheme_or_url(net, url):
    assert I.inspect_link(url).get("error", "").startswith("blocked")


def test_redirect_to_private_blocked(net):
    net[("example.com", "/a")] = (302, {"location": "http://169.254.169.254/latest"})
    r = I.inspect_link("https://example.com/a")
    assert r["error"].startswith("blocked")
    assert [s[1] for s in net.seen] == ["example.com"]


def test_redirect_to_private_host_blocked(net):
    net[("example.com", "/a")] = (302, {"location": "https://evil.test/x"})
    assert I.inspect_link("https://example.com/a")["error"].startswith("blocked")


def test_redirect_to_file_scheme_blocked(net):
    net[("example.com", "/a")] = (302, {"location": "file:///etc/passwd"})
    assert I.inspect_link("https://example.com/a")["error"].startswith("blocked")


def test_redirect_limit(net):
    for i in range(10):
        net[("example.com", f"/{i}")] = (302, {"location": f"/{i + 1}"})
    r = I.inspect_link("https://example.com/0")
    assert "too many redirects" in r["error"]
    assert len(net.seen) == 6
    net[("example.com", "/5")] = file()  # exactly 5 redirects is allowed
    assert "error" not in I.inspect_link("https://example.com/0")


def test_head_refused_falls_back_to_zero_byte_get(net):
    calls = []

    def h(req):
        calls.append((req.method, req.headers.get("range")))
        if req.method == "HEAD":
            return httpx.Response(405)
        return httpx.Response(206, headers={"content-range": "bytes 0-0/5000", "content-type": "video/mp4"}, content=b"x")

    I._TRANSPORT = httpx.MockTransport(h)
    r = I.inspect_link("https://example.com/a.mp4")
    assert calls == [("HEAD", None), ("GET", "bytes=0-0")] and r["size"] == 5000 and lvl(r) == "ok"


def test_unreachable(net, monkeypatch):
    def boom(req):
        raise httpx.ConnectError("no")
    monkeypatch.setattr(I, "_TRANSPORT", httpx.MockTransport(boom))
    assert I.inspect_link("https://example.com/a")["error"].startswith("unreachable")


def test_http_error_status_is_caution(net):
    r = I.inspect_link("https://example.com/missing.mp4")
    assert lvl(r) == "caution" and "404" in text(r)


def test_ftp_no_request(net):
    DNS["ftp.example.com"] = ["93.184.216.34"]
    r = I.inspect_link("ftp://ftp.example.com/a.mp4")
    assert net.seen == [] and lvl(r) == "caution" and "FTP" in text(r)
    assert I.inspect_link("ftp://127.0.0.1/a")["error"].startswith("blocked")


def test_magnet(net, monkeypatch):
    monkeypatch.setattr(mdm, "torrent_info", lambda s: {"name": "Demo", "info_hash": "ab", "size": 30,
                        "files": [{"index": 0, "size": 30, "path": "Demo/a.mkv"}]})
    r = I.inspect_link("magnet:?xt=urn:btih:abcdef&dn=Cool+Film")
    assert r["kind"] == "magnet" and r["title"] == "Demo" and r["size"] == 30 and r["files"][0]["index"] == 0
    assert lvl(r) == "ok" and net.seen == []


def test_magnet_failure_and_exe_in_torrent(net, monkeypatch):
    def bad(s):
        raise mdm.MdmError("no cli")
    monkeypatch.setattr(mdm, "torrent_info", bad)
    r = I.inspect_link("magnet:?xt=urn:btih:abc&dn=x")
    assert lvl(r) == "caution" and r["title"] == "x" and any("Torrent" in s for s in r["safety"]["skipped"])
    monkeypatch.setattr(mdm, "torrent_info", lambda s: {"name": "n", "size": 1, "files": [{"index": 0, "size": 1, "path": "n/setup.exe"}]})
    assert lvl(I.inspect_link("magnet:?xt=urn:btih:abc")) == "risky"


def test_torrent_url_is_a_plain_file(net, monkeypatch):
    # the CLI's torrent-info takes only a magnet or a local path, so an http .torrent is reported as a file
    net[("example.com", "/a.torrent")] = file("application/x-bittorrent", "9")
    monkeypatch.setattr(mdm, "torrent_info", lambda s: pytest.fail("torrent_info must not be called"))
    r = I.inspect_link("https://example.com/a.torrent")
    assert r["kind"] == "http" and r["files"] == [] and r["filename"] == "a.torrent" and r["size"] == 9
    assert lvl(r) == "ok"


# ---- parser differential (item: backslash / control chars) ----

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8799\\@evil.invalid/movie.mp4", "https://127.0.0.1\\.example.com/a", "https://example.com/a\tb",
    "https://example.com/a\x7f", "https://example.com/a\x0bb", "https://example.com/\u3000x", "https://%31%32%37.0.0.1/a",
])
def test_parser_differential_blocked(net, url):
    r = I.inspect_link(url)
    assert r.get("error", "").startswith("blocked"), r
    assert net.seen == []


def test_hostname_mismatch_blocked(net, monkeypatch):
    monkeypatch.setattr(mdm.httpx, "URL", lambda u: type("U", (), {"raw_host": b"127.0.0.1"})())
    assert I.inspect_link("https://example.com/a")["error"].startswith("blocked")
    assert net.seen == []


def test_redirect_with_backslash_blocked(net):
    net[("example.com", "/a")] = (302, {"location": "http://127.0.0.1\\@other.org/x"})
    assert I.inspect_link("https://example.com/a")["error"].startswith("blocked")
    assert [s[1] for s in net.seen] == ["example.com"]


def test_backslash_url_never_reaches_cli(net, tmp_path, monkeypatch):
    """Reviewer repro t_bs2: the URL must be refused by inspect AND by mdm before the CLI is spawned."""
    from agent import jobs as J
    url = "http://127.0.0.1:8799\\@evil.invalid/movie.mp4"
    assert I.inspect_link(url)["error"].startswith("blocked")
    spawned = []
    monkeypatch.setattr(mdm.subprocess, "Popen", lambda *a, **k: spawned.append(a) or pytest.fail("spawned"))
    j = J.Jobs(tmp_path / "d", tmp_path / "dl", notifier=lambda *a: None)
    job = j.approve(j.propose({"url": url, "safety": {"level": "ok"}})["id"])  # forged report
    for _ in range(100):
        if j.get_job(job["id"])["state"] == "error":
            break
        import time; time.sleep(0.02)
    assert j.get_job(job["id"])["state"] == "error" and "backslash" in j.get_job(job["id"])["error"]
    assert spawned == []


# ---- executables (content type, new extensions, query names, trailing dots) ----

@pytest.mark.parametrize("ctype", sorted(I.RISKY_TYPES))
def test_risky_content_type(net, ctype):
    net[("example.com", "/dl")] = file(ctype)
    r = I.inspect_link("https://example.com/dl?id=5")
    assert lvl(r) == "risky" and ctype in text(r)


@pytest.mark.parametrize("name", ["install.sh", "Setup.command", "X.app", "a.scpt", "a.workflow", "a.dll", "a.lnk",
                                  "a.js", "a.hta", "a.reg", "a.docm", "a.xlsm", "a.pptm", "a.deb", "a.rpm",
                                  "a.AppImage", "x.app.zip"])
def test_new_risky_ext(net, name):
    net[("example.com", "/" + name)] = file("application/octet-stream")
    assert lvl(I.inspect_link("https://example.com/" + name)) == "risky"


def test_risky_disposition_new_ext(net):
    net[("example.com", "/file")] = file("application/octet-stream", **{"content-disposition": 'attachment; filename="a.js"'})
    assert lvl(I.inspect_link("https://example.com/file")) == "risky"


@pytest.mark.parametrize("q", ["file=setup.exe", "filename=movie.mp4.exe", "name=a%2Fb%2Fsetup.msi",
                               "response-content-disposition=attachment%3B%20filename%3D%22x.scr%22"])
def test_query_string_name(net, q):
    net[("example.com", "/get")] = file("application/octet-stream")
    r = I.inspect_link("https://example.com/get?" + q)
    assert lvl(r) == "risky", r["safety"]


def test_query_unrelated_key_not_flagged(net):
    net[("example.com", "/a.mp4")] = file()
    assert lvl(I.inspect_link("https://example.com/a.mp4?ref=example.com")) == "ok"


@pytest.mark.parametrize("path", ["/setup.exe.", "/setup.exe%20", "/setup.exe.%20."])
def test_trailing_dots_spaces(net, path):
    net[("example.com", path.replace("%20", " "))] = file("application/octet-stream")
    r = I.inspect_link("https://example.com" + path)
    assert lvl(r) == "risky" and r["filename"] == "setup.exe"


def test_virustotal_strips_userinfo(net, monkeypatch):
    net[("example.com", "/a.mp4")] = file()
    seen = vt(monkeypatch, 404, {})
    I.inspect_link("https://bob:hunter2@example.com/a.mp4")
    import base64
    ids = [str(r.url).rsplit("/", 1)[1] for r in seen]
    decoded = [base64.urlsafe_b64decode(i + "=" * (-len(i) % 4)).decode() for i in ids]
    assert decoded == ["https://example.com/a.mp4"]


def test_html_preview_skipped_when_firecrawl_down(net, monkeypatch):
    monkeypatch.setattr(web, "scrape", lambda u, max_chars=0: (_ for _ in ()).throw(web.WebError("connection refused")))
    net[("example.com", "/p")] = (200, {"content-type": "text/html; charset=utf-8", "content-length": "10"})
    r = I.inspect_link("https://example.com/p")
    assert any("Page preview" in s for s in r["safety"]["skipped"]) and r["title"] is None


def test_html_preview_with_key(net, monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "k")
    net[("example.com", "/p")] = (200, {"content-type": "text/html", "content-length": "10"})
    monkeypatch.setattr(web, "scrape", lambda u, max_chars=0: {"title": "T", "description": "D", "image": "https://i/x.png"})
    r = I.inspect_link("https://example.com/p")
    assert (r["title"], r["description"], r["image"]) == ("T", "D", "https://i/x.png")
    monkeypatch.setattr(web, "scrape", lambda u, max_chars=0: (_ for _ in ()).throw(web.WebError("down")))
    assert any("down" in s for s in I.inspect_link("https://example.com/p")["safety"]["skipped"])


def test_video_page_gets_video_info(net, monkeypatch):
    seen = []
    info = {"title": "Cat", "heights": [1080, 720], "subtitles": []}
    monkeypatch.setattr(mdm, "video_info", lambda u: seen.append(u) or info)
    monkeypatch.setattr(web, "scrape", lambda u, max_chars=0: {"title": "T", "description": "D", "image": None})
    net[("example.com", "/watch")] = (200, {"content-type": "text/html", "content-length": "10"})
    r = I.inspect_link("https://example.com/watch")
    assert r["video"] == info and seen == ["https://example.com/watch"]
    assert not any("yt-dlp" in x for x in r["safety"]["skipped"])


def test_video_info_only_for_html_pages(net, monkeypatch):
    monkeypatch.setattr(mdm, "video_info", lambda u: (_ for _ in ()).throw(AssertionError("must not be called")))
    net[("example.com", "/a.mp4")] = file()
    r = I.inspect_link("https://example.com/a.mp4")
    assert "video" not in r


def test_ytdlp_missing_is_noted_not_fatal(net, monkeypatch):
    def boom(u):
        raise mdm.MdmError("yt-dlp is not installed (install-ytdlp)")
    monkeypatch.setattr(mdm, "video_info", boom)
    net[("example.com", "/p")] = (200, {"content-type": "text/html", "content-length": "10"})
    r = I.inspect_link("https://example.com/p")
    assert "video" not in r and "yt-dlp is not installed: run misterdownloadmanager-cli install-ytdlp" in r["safety"]["skipped"]
    assert r["safety"]["level"] == "ok"  # a missing optional tool never changes the verdict


def test_other_video_info_failures_are_silent(net, monkeypatch):
    monkeypatch.setattr(mdm, "video_info", lambda u: (_ for _ in ()).throw(mdm.MdmError("Unsupported URL")))
    net[("example.com", "/p")] = (200, {"content-type": "text/html", "content-length": "10"})
    r = I.inspect_link("https://example.com/p")
    assert "video" not in r and not any("yt-dlp" in x for x in r["safety"]["skipped"])


def vt(monkeypatch, status, body):
    monkeypatch.setenv("VIRUSTOTAL_API_KEY", "vtkey")
    seen = []

    def h(req):
        seen.append(req)
        return httpx.Response(status, json=body)
    monkeypatch.setattr(I, "_VT_TRANSPORT", httpx.MockTransport(h))
    return seen


def stats(m=0, s=0):
    return {"data": {"attributes": {"last_analysis_stats": {"malicious": m, "suspicious": s, "harmless": 50}}}}


def test_virustotal_malicious(net, monkeypatch):
    net[("example.com", "/a.mp4")] = file()
    seen = vt(monkeypatch, 200, stats(m=3))
    r = I.inspect_link("https://example.com/a.mp4")
    assert lvl(r) == "risky" and "VirusTotal" in text(r) and seen[0].headers["x-apikey"] == "vtkey"
    assert "VirusTotal: no API key" not in r["safety"]["skipped"]


def test_virustotal_clean_suspicious_unknown_fail(net, monkeypatch):
    net[("example.com", "/a.mp4")] = file()
    vt(monkeypatch, 200, stats())
    assert lvl(I.inspect_link("https://example.com/a.mp4")) == "ok"
    vt(monkeypatch, 200, stats(s=2))
    assert lvl(I.inspect_link("https://example.com/a.mp4")) == "caution"
    vt(monkeypatch, 404, {})
    r = I.inspect_link("https://example.com/a.mp4")
    assert lvl(r) == "ok" and any("not yet known" in s for s in r["safety"]["skipped"])
    vt(monkeypatch, 500, {})
    assert lvl(I.inspect_link("https://example.com/a.mp4")) == "caution"


# ---- web.py / notify.py (mocked) ----

class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_web_shapes(monkeypatch):
    seen = {}

    class Fake:
        def __init__(self, **kw):
            seen.update(kw)

    import firecrawl
    monkeypatch.setattr(firecrawl, "Firecrawl", Fake)
    monkeypatch.setattr(I, "_resolve", lambda h, p: ["93.184.216.34"])  # scrape's SSRF guard resolves the host
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.delenv("FIRECRAWL_API_URL", raising=False)
    web._client()  # self-hosted by default: local URL, placeholder key
    assert seen["api_url"] == "http://localhost:3002" and seen["api_key"] == "local"
    monkeypatch.setenv("FIRECRAWL_API_URL", "http://fc:3002")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "k")
    web._client()
    assert seen["api_url"] == "http://fc:3002" and seen["api_key"] == "k"

    class C:  # v2 style: objects
        def search(self, q, limit=5):
            return _Obj(web=[_Obj(url="https://a", title="A", description="d"), _Obj(url=None, title="skip")])

        def scrape(self, url, formats=None):
            return _Obj(markdown="m" * 50, metadata=_Obj(title="T", description="D", og_image="https://i", url=url))

    monkeypatch.setattr(web, "_client", lambda: C())
    assert web.search("q") == [{"title": "A", "url": "https://a", "description": "d"}]
    s = web.scrape("https://a", max_chars=10)
    assert s == {"url": "https://a", "title": "T", "description": "D", "image": "https://i", "text": "m" * 10}

    class D:  # v1 style: dicts
        def search(self, q, limit=5):
            return {"data": [{"url": "https://b", "title": "B", "description": "e"}]}

        def scrape(self, url, formats=None):
            return {"markdown": "x", "metadata": {"ogTitle": "O", "sourceURL": url}}

    monkeypatch.setattr(web, "_client", lambda: D())
    assert web.search("q")[0]["url"] == "https://b"
    assert web.scrape("https://b")["title"] == "O"

    class E:
        def search(self, *a, **k):
            raise RuntimeError("secret-key-xyz")
    monkeypatch.setattr(web, "_client", lambda: E())
    with pytest.raises(web.WebError):
        web.search("q")


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000/api/jobs", "http://localhost:3002/", "http://169.254.169.254/latest/meta-data/",
                                 "http://10.0.0.5/", "http://192.168.1.1/", "http://evil.test/", "http://[::1]/",
                                 "file:///etc/passwd", "ftp://example.com/a", "javascript:alert(1)", "",
                                 "http://127.0.0.1:8799\\@example.com/x"])
def test_scrape_blocked(net, monkeypatch, url):
    monkeypatch.setattr(web, "_client", lambda: pytest.fail("firecrawl must not be called"))
    with pytest.raises(web.WebError, match="^blocked"):
        web.scrape(url)


def test_scrape_public_allowed(net, monkeypatch):
    class C:
        def scrape(self, url, formats=None):
            return {"markdown": "hi", "metadata": {"title": "T"}}
    monkeypatch.setattr(web, "_client", lambda: C())
    assert web.scrape("https://example.com/p")["title"] == "T"


def test_notify_argv_and_noop(monkeypatch):
    from agent import notify
    calls = []
    monkeypatch.setattr(notify.subprocess, "run", lambda args, **k: calls.append(args))
    monkeypatch.setattr(notify.sys, "platform", "darwin")
    notify.notify('T"; do shell script "x', "m\\\"")
    assert calls[0][-3:] == ["--", 'm\\"', 'T"; do shell script "x'] and not any("shell script" in a for a in calls[0][:-1])
    monkeypatch.setattr(notify.sys, "platform", "linux")
    notify.notify("a", "b")
    assert len(calls) == 1
    monkeypatch.setattr(notify.sys, "platform", "darwin")
    monkeypatch.setattr(notify.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    notify.notify("a", "b")  # never raises


def test_same_site_redirects():
    assert I._same_site("archive.org", "dn711307.ca.archive.org")
    assert I._same_site("www.example.com", "cdn.example.com")
    assert not I._same_site("archive.org", "evil.com")
    assert not I._same_site("archive.org", "archive.org.evil.com")
