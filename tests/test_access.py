"""Token access: MDM_TOKEN login cookie, ALLOWED_HOSTS, signed links without a cookie (contracts/library.md "Access")."""
import pytest
from fastapi.testclient import TestClient

import server
from tests.test_server import DATA, StubAgent, StubJobs

REMOTE = "media.tailnet.ts.net"


@pytest.fixture
def remote(tmp_path, monkeypatch):
    monkeypatch.setenv("ALLOWED_HOSTS", f"{REMOTE}, other.example")
    monkeypatch.setenv("MDM_TOKEN", "correct horse battery staple")
    monkeypatch.delenv("MDM_SECRET", raising=False)
    jobs = StubJobs(tmp_path)
    jobs.add_item("i_1", {"a.mp4": DATA})
    app = server.create_app(jobs=jobs, agent=StubAgent())
    return TestClient(app, base_url=f"https://{REMOTE}"), app, jobs


def login(c, token="correct horse battery staple"):
    return c.post("/api/login", json={"token": token})


def test_remote_needs_login(remote):
    c, _, _ = remote
    for path in ("/api/proposals", "/api/jobs", "/api/library", "/api/chats", "/api/library/i_1/stream", "/api/events"):
        r = c.get(path)
        assert r.status_code == 401 and r.json() == {"detail": "login required"}, path
    assert c.post("/api/chat", json={"session": "s", "message": "hi"}).status_code == 401
    assert c.post("/api/logout").status_code == 401


def test_page_health_and_login_stay_open(remote):
    c, _, _ = remote
    assert c.get("/").status_code == 200
    assert c.get("/static/index.html").status_code == 200
    assert c.get("/api/health").status_code == 200


def test_login_then_200(remote):
    c, _, _ = remote
    r = login(c)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("mdm_session=")
    low = cookie.lower()
    assert "httponly" in low and "samesite=strict" in low and "secure" in low and "max-age=2592000" in low
    assert "correct horse" not in cookie
    assert c.get("/api/proposals").status_code == 200
    assert c.get("/api/library/i_1/stream", headers={"Range": "bytes=0-99"}).status_code == 206
    assert c.post("/api/logout").status_code == 200
    assert c.get("/api/proposals").status_code == 401


def test_wrong_token_401(remote):
    c, _, _ = remote
    r = login(c, "wrong")
    assert r.status_code == 401 and "set-cookie" not in r.headers
    assert c.get("/api/proposals").status_code == 401


@pytest.mark.parametrize("value", ["", "garbage", "9999999999.deadbeef", "1.abc", "9999999999"])
def test_forged_cookie_401(remote, value):
    c, _, _ = remote
    c.cookies.set("mdm_session", value)
    assert c.get("/api/proposals").status_code == 401


def test_expired_cookie_401(remote, monkeypatch):
    c, _, _ = remote
    login(c)
    monkeypatch.setattr(server.time, "time", lambda: 9e9)
    assert c.get("/api/proposals").status_code == 401


def test_changing_token_signs_out(remote, monkeypatch, tmp_path):
    c, _, jobs = remote
    login(c)
    monkeypatch.setenv("MDM_TOKEN", "a new token")
    c2 = TestClient(server.create_app(jobs=jobs, agent=StubAgent()), base_url=f"https://{REMOTE}", cookies=c.cookies)
    assert c2.get("/api/proposals").status_code == 401


def test_secure_flag_only_over_https(remote):
    _, app, _ = remote
    plain = TestClient(app, base_url=f"http://{REMOTE}")
    assert "secure" not in login(plain).headers["set-cookie"].lower()
    # X-Forwarded-Proto counts only from a loopback client (tailscale serve on the same host)
    spoofed = TestClient(app, base_url=f"http://{REMOTE}", headers={"X-Forwarded-Proto": "https"})
    assert "secure" not in login(spoofed).headers["set-cookie"].lower()
    local = TestClient(app, base_url=f"http://{REMOTE}", headers={"X-Forwarded-Proto": "https"}, client=("127.0.0.1", 5000))
    assert "secure" in login(local).headers["set-cookie"].lower()


def test_signed_link_needs_no_cookie(remote):
    c, app, _ = remote
    login(c)
    url = c.post("/api/library/i_1/link").json()["url"]
    vlc = TestClient(app, base_url=f"https://{REMOTE}")
    assert vlc.get(url).content == DATA
    assert vlc.get(url, headers={"Range": "bytes=0-99"}).status_code == 206
    assert vlc.get(url.replace("file=0", "file=1")).status_code == 403
    assert vlc.get(url[:-4] + "zzzz").status_code == 403
    # a signature only opens the stream endpoint
    assert vlc.get(url.replace("/stream", "/download")).status_code == 401
    assert vlc.get("/api/library?sig=x").status_code == 401


def test_extra_hosts_and_bad_host(remote):
    _, app, _ = remote
    assert TestClient(app, base_url="https://other.example").get("/api/health").status_code == 200
    assert TestClient(app, base_url="https://evil.example").get("/api/health").status_code == 403


def test_token_on_loopback_also_needs_login(remote):
    _, app, _ = remote
    c = TestClient(app, base_url="http://127.0.0.1:8000")
    assert c.get("/api/proposals").status_code == 401
    login(c)
    assert c.get("/api/proposals").status_code == 200


def test_allowed_hosts_without_token_refuses_to_start(monkeypatch):
    monkeypatch.setenv("ALLOWED_HOSTS", REMOTE)
    monkeypatch.delenv("MDM_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="ALLOWED_HOSTS is set but MDM_TOKEN is not"):
        server.create_app(jobs=StubJobs(), agent=StubAgent())


def test_real_app_checks_config_at_startup(monkeypatch):
    monkeypatch.setenv("ALLOWED_HOSTS", REMOTE)
    monkeypatch.delenv("MDM_TOKEN", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    app = server.create_app()  # real: config is read in lifespan, after .env
    with pytest.raises(RuntimeError, match="MDM_TOKEN"):
        with TestClient(app, base_url="http://localhost"):
            pass


def test_loopback_without_token_unchanged(monkeypatch, tmp_path):
    for k in ("ALLOWED_HOSTS", "MDM_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    jobs = StubJobs(tmp_path)
    jobs.add_item("i_1", {"a.mp4": DATA})
    c = TestClient(server.create_app(jobs=jobs, agent=StubAgent()), base_url="http://127.0.0.1:8000")
    assert c.get("/api/proposals").status_code == 200
    assert c.get("/api/library/i_1/stream").content == DATA
    assert c.post("/api/login", json={"token": "x"}).status_code == 400
    assert TestClient(c.app, base_url=f"https://{REMOTE}").get("/api/health").status_code == 403
