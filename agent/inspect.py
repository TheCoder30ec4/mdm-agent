"""Link preview + safety heuristics. HEAD only; SSRF guard on every hop; connects to the validated IP."""
import base64
import ipaddress
import mimetypes
import os
import posixpath
import re
import socket
from urllib.parse import parse_qs, unquote, urljoin, urlparse, urlsplit, urlunsplit

import httpx

from agent import mdm, web

SCHEMES = ("http", "https", "ftp", "ftps", "magnet")
MAX_REDIRECTS = 5
TIMEOUT = 10.0
RISKY_EXT = {".exe", ".msi", ".scr", ".bat", ".cmd", ".ps1", ".jar", ".apk", ".dmg", ".pkg", ".com", ".vbs",
             ".sh", ".command", ".app", ".scpt", ".workflow", ".dll", ".lnk", ".js", ".hta", ".reg", ".docm",
             ".xlsm", ".pptm", ".deb", ".rpm", ".appimage"}
RISKY_TYPES = {"application/x-msdownload", "application/x-dosexec", "application/x-msdos-program",
               "application/vnd.android.package-archive", "application/x-apple-diskimage", "application/java-archive",
               "application/x-sh", "application/x-executable", "application/x-mach-binary"}
# ponytail: fixed key list; a file name hidden under any other query key is not seen
QUERY_NAME_KEYS = {"file", "filename", "name", "fn", "f", "download", "dl", "attachment", "path",
                   "response-content-disposition"}
CAUTION_EXT = {".zip", ".iso", ".rar", ".7z"}
# extensions that make "x.mp4.exe" look innocent
DECOY_EXT = {".mp4", ".mkv", ".avi", ".mov", ".mp3", ".flac", ".wav", ".pdf", ".doc", ".docx", ".xls", ".xlsx",
             ".ppt", ".pptx", ".txt", ".jpg", ".jpeg", ".png", ".gif", ".epub", ".zip", ".rar", ".7z", ".iso", ".csv"}
SHORTENERS = {"bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "is.gd", "buff.ly", "rebrand.ly", "cutt.ly",
              "shorturl.at", "tiny.cc", "rb.gy", "s.id", "v.gd", "lnkd.in", "t.ly", "bl.ink", "short.io", "adf.ly"}
GENERIC_TYPES = {"application/octet-stream", "binary/octet-stream", "application/force-download",
                 "application/download", "application/x-download"}
_RANK = {"ok": 0, "caution": 1, "risky": 2}

_TRANSPORT = None  # tests inject an httpx transport here


class Blocked(Exception):
    pass


class Unreachable(Exception):
    pass


# ---- SSRF guard -------------------------------------------------------------------------------

def _ip_blocked(ip) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = ip.ipv4_mapped or (ip.sixtofour) or (ip.teredo[1] if ip.teredo else None)
        if embedded is None and ip in ipaddress.ip_network("64:ff9b::/96"):
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is not None and _ip_blocked(embedded):
            return True
    return (not ip.is_global or ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified)


def _literal_ip(host: str):
    """IP if host is any IP literal form (dotted, decimal, hex, octal, v6), else None."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except (OSError, ValueError):
        return None


def _resolve(host: str, port: int) -> list[str]:
    return list({ai[4][0] for ai in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})


def _safe_ips(host: str, port: int) -> list[str]:
    """Resolve host and return its IPs; raise Blocked if any is non-public."""
    host = host.rstrip(".").lower()
    if not host:
        raise Blocked("empty host")
    if host == "localhost" or host.endswith(".localhost"):
        raise Blocked(f"{host} is a local address")
    lit = _literal_ip(host)
    if lit is not None:
        if _ip_blocked(lit):
            raise Blocked(f"{host} is a private, loopback or reserved address")
        return [str(lit)]
    try:
        ips = _resolve(host, port)
    except OSError:
        raise Blocked(f"cannot resolve {host}") from None
    if not ips:
        raise Blocked(f"cannot resolve {host}")
    for s in ips:
        ip = ipaddress.ip_address(s.split("%")[0])
        if _ip_blocked(ip):
            raise Blocked(f"{host} resolves to a private, loopback or reserved address")
    return ips


# ---- network ----------------------------------------------------------------------------------

def _hop(client: httpx.Client, url: str):
    """One request (HEAD, else 0-byte ranged GET) to the validated IP. Returns a closed response (headers only)."""
    u = httpx.URL(url)
    host = u.raw_host.decode("ascii")
    port = u.port or (443 if u.scheme == "https" else 80)
    ips = _safe_ips(host, port)
    last = None
    for ip in ips:
        pinned = u.copy_with(host=ip, userinfo=b"")
        hdrs = {"Host": u.netloc.decode().rsplit("@", 1)[-1], "User-Agent": "mdm-agent/1.0", "Accept": "*/*"}
        ext = {"sni_hostname": host} if u.scheme == "https" else {}
        try:
            for method in ("HEAD", "GET"):
                h = dict(hdrs, Range="bytes=0-0") if method == "GET" else hdrs
                resp = client.send(client.build_request(method, pinned, headers=h, extensions=ext), stream=True)
                resp.close()  # never read a body
                if method == "HEAD" and resp.status_code in (400, 403, 405, 501):
                    continue
                return resp
        except httpx.TransportError as e:
            last = e
    raise last or httpx.ConnectError("no address")


def _cd_name(cd: str) -> str:
    m = re.search(r"filename\*\s*=\s*[^']*'[^']*'([^;]+)", cd, re.I) or re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.I)
    return unquote(m.group(1)).strip() if m else ""


def _clean(s: str) -> str:
    # trailing dots/spaces are dropped by Windows, so "setup.exe." is setup.exe
    return re.sub(r"[\x00-\x1f]", "", posixpath.basename(s.replace("\\", "/"))).strip().rstrip(". ")


def _filename(resp, final_url: str):
    name = _cd_name(resp.headers.get("content-disposition", ""))
    from_url = unquote(posixpath.basename(urlparse(final_url).path))
    return _clean(name) or _clean(from_url) or None, _clean(from_url) or None


def _query_names(url: str) -> list[str]:
    """File names passed in the query string, e.g. ?file=setup.exe or S3's response-content-disposition."""
    out = []
    for k, vals in parse_qs(urlparse(url).query).items():
        if k.lower() in QUERY_NAME_KEYS:
            out += [_clean(_cd_name(v) or v) for v in vals]
    return [n for n in out if n]


def _size(resp):
    cr = resp.headers.get("content-range", "")
    m = re.search(r"/(\d+)$", cr)
    if m:
        return int(m.group(1))
    cl = resp.headers.get("content-length", "")
    return int(cl) if cl.isdigit() and resp.status_code == 200 else None


# ---- main -------------------------------------------------------------------------------------

def _same_site(a: str, b: str) -> bool:
    """archive.org -> dn711307.ca.archive.org is the same site; archive.org -> evil.com is not.
    ponytail: parent-domain heuristic, no public-suffix list; on 3-label hosts like x.co.uk it treats
    any *.co.uk as the same site. Use tldextract if that matters."""
    a, b = a.lower().rstrip("."), b.lower().rstrip(".")
    base = a.split(".", 1)[1] if a.count(".") >= 2 else a
    return a == b or b.endswith("." + a) or b == base or b.endswith("." + base)


def inspect_link(url: str) -> dict:
    try:
        return _inspect(url)
    except Blocked as e:
        return {"error": f"blocked: {e}"}
    except Unreachable as e:
        return {"error": f"unreachable: {e}"}
    except Exception as e:  # never raise for bad input
        return {"error": f"could not inspect link: {type(e).__name__}"}


def _check(url: str) -> None:
    """Same syntactic gate mdm applies before spawning (scheme, no backslash/whitespace/control chars,
    urllib and httpx agree on the host), so inspect never judges a different host than MDM connects to."""
    try:
        mdm._check_source(url)
    except mdm.MdmError as e:
        raise Blocked(str(e)) from None


def _inspect(url: str) -> dict:
    url = (url or "").strip() if isinstance(url, str) else ""
    if not url:
        raise Blocked("invalid url")
    scheme = urlparse(url).scheme.lower()
    if scheme not in SCHEMES:
        raise Blocked(f"unsupported scheme '{scheme}'")
    _check(url)
    p = urlparse(url)
    reasons: list[tuple[str, str]] = []
    skipped: list[str] = []
    rep = {"url": url, "final_url": url, "host": None, "kind": "http", "title": None, "description": None,
           "image": None, "filename": None, "size": None, "content_type": None, "files": []}

    def add(level, text):
        reasons.append((level, text))

    if scheme == "magnet":
        rep["kind"] = "magnet"
        dn = parse_qs(p.query).get("dn", [None])[0]
        rep["title"] = rep["filename"] = dn
        _torrent(rep, url, add, skipped)
        skipped.append("VirusTotal: not applicable to magnet links")
        return _finish(rep, reasons, skipped)

    # --- follow redirects by hand, validating every hop
    first_host = (p.hostname or "").lower()
    cur, hops, resp = url, [], None
    with httpx.Client(transport=_TRANSPORT, timeout=TIMEOUT, follow_redirects=False, trust_env=False) as client:
        for n in range(MAX_REDIRECTS + 1):
            cp = urlparse(cur)
            if cp.scheme.lower() not in ("http", "https", "ftp", "ftps"):
                raise Blocked(f"redirect to unsupported scheme '{cp.scheme}'")
            _check(cur)
            if not cp.hostname:
                raise Blocked("url has no host")
            if cp.scheme.lower() in ("ftp", "ftps"):  # httpx cannot speak FTP: validate only, no request
                _safe_ips(cp.hostname, cp.port or 21)
                resp = None
                break
            try:
                resp = _hop(client, cur)
            except httpx.HTTPError as e:
                raise Unreachable(type(e).__name__) from None
            if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                if n == MAX_REDIRECTS:
                    raise Blocked("too many redirects")
                cur = urljoin(cur, resp.headers["location"])
                hops.append(cur)
                continue
            break

    final = cur
    fp = urlparse(final)
    fhost = (fp.hostname or "").lower()
    rep["final_url"], rep["host"] = final, fhost
    strip = lambda h: h[4:] if h.startswith("www.") else h

    # --- transport / host heuristics
    for u in {urlparse(x).scheme.lower() for x in [url, final]}:
        if u in ("http", "ftp"):
            add("caution", f"The link uses {u.upper()} without encryption (no TLS).")
    if p.username or p.password:
        add("caution", "The link contains embedded credentials.")
    if not _same_site(strip(first_host), strip(fhost)):
        add("caution", f"The link redirects to a different site ({first_host} -> {fhost}).")
    for h in [first_host] + [(urlparse(x).hostname or "").lower() for x in hops]:
        if h in SHORTENERS or any(h.endswith("." + s) for s in SHORTENERS):
            add("caution", f"The link goes through a URL shortener ({h}).")
            break
    for h in dict.fromkeys([first_host, fhost]):
        if _literal_ip(h) is not None:
            add("caution", f"The host is a raw IP address ({h}).")
        if any(lbl.startswith("xn--") for lbl in h.split(".")):
            add("caution", f"The host uses punycode, which can imitate another site ({h}).")

    # --- response heuristics
    ctype = None
    if resp is not None:
        rep["http_status"] = resp.status_code  # the direct-download path refuses to start on a server error
        if resp.status_code >= 400 and resp.status_code != 416:
            add("caution", f"The server answered HTTP {resp.status_code}; the link may not work.")
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower() or None
        rep["size"] = _size(resp)
    else:
        skipped.append("Size and type: FTP links are not probed")
    rep["content_type"] = ctype
    name, url_name = _filename(resp, final) if resp is not None else _filename(_NoResp(), final)
    rep["filename"] = name
    if rep["size"] is None:
        add("caution", "The file size is unknown.")

    # --- filename rules
    if ctype in RISKY_TYPES:
        add("risky", f"The server says this file is an executable or installer ({ctype}).")
    names = [name, url_name] + _query_names(url) + (_query_names(final) if final != url else [])
    for n in dict.fromkeys(x.lower() for x in names if x):
        root, ext = posixpath.splitext(n)
        inner = posixpath.splitext(root)[1]
        if ext in RISKY_EXT:
            add("risky", f"'{n}' is an executable or installer ({ext}).")
            if inner in DECOY_EXT:
                add("risky", f"'{n}' has a double extension that disguises an executable.")
        elif ext in CAUTION_EXT:
            if inner in RISKY_EXT:
                add("risky", f"'{n}' is an archive that likely holds an executable or app ({inner}).")
            else:
                add("caution", f"'{n}' is an archive or disk image ({ext}); check what is inside before opening it.")
    if name and ctype and ctype not in GENERIC_TYPES:
        guess = mimetypes.guess_type(name)[0]
        if guess and guess.split("/")[0] != ctype.split("/")[0]:
            add("caution", f"The file name suggests {guess} but the server says {ctype}.")

    # --- html preview (an http .torrent is reported as a plain file: the CLI reads torrents only from magnets
    # or local paths, so it is downloaded with `get` like any other file)
    if ctype in ("text/html", "application/xhtml+xml"):
        _preview(rep, final, skipped)

    _virustotal(url, final, add, skipped)
    return _finish(rep, reasons, skipped)


class _NoResp:
    headers: dict = {}


def _torrent(rep, src, add, skipped):
    try:
        info = mdm.torrent_info(src)
    except mdm.MdmError as e:
        skipped.append(f"Torrent contents: {e}")
        add("caution", "The torrent contents could not be read.")
        return
    rep["title"] = info.get("name") or rep["title"]
    rep["size"] = info.get("size")
    rep["files"] = info.get("files") or []
    for f in rep["files"]:
        ext = posixpath.splitext(str(f.get("path", "")).lower())[1]
        if ext in RISKY_EXT:
            add("risky", f"The torrent contains an executable or installer ({f.get('path')}).")
            break


def _preview(rep, url, skipped):
    try:
        s = web.scrape(url, max_chars=500)
    except web.WebError as e:
        skipped.append(f"Page preview: {e}")
        return
    rep["title"], rep["description"], rep["image"] = s.get("title"), s.get("description"), s.get("image")


def _virustotal(url, final, add, skipped):
    key = os.environ.get("VIRUSTOTAL_API_KEY")
    if not key:
        skipped.append("VirusTotal: no API key")
        return
    try:
        for u in dict.fromkeys(_no_userinfo(x) for x in [url, final]):
            vid = base64.urlsafe_b64encode(u.encode()).decode().rstrip("=")
            with httpx.Client(transport=_VT_TRANSPORT, timeout=TIMEOUT, trust_env=False) as c:
                r = c.get(f"https://www.virustotal.com/api/v3/urls/{vid}", headers={"x-apikey": key})
            if r.status_code == 404:
                skipped.append("VirusTotal: URL not yet known to VirusTotal")
                continue
            r.raise_for_status()
            st = r.json()["data"]["attributes"]["last_analysis_stats"]
            if st.get("malicious", 0) > 0:
                add("risky", f"VirusTotal: {st['malicious']} engine(s) flag this URL as malicious.")
            elif st.get("suspicious", 0) > 0:
                add("caution", f"VirusTotal: {st['suspicious']} engine(s) flag this URL as suspicious.")
    except Exception:
        skipped.append("VirusTotal: lookup failed")
        add("caution", "VirusTotal could not be checked.")


_VT_TRANSPORT = None  # tests inject


def _no_userinfo(url: str) -> str:
    """Never send embedded credentials (user:pass@) to VirusTotal."""
    p = urlsplit(url)
    return urlunsplit(p._replace(netloc=p.netloc.rpartition("@")[2]))


def _finish(rep, reasons, skipped):
    level = max((l for l, _ in reasons), key=_RANK.get, default="ok")
    rep["safety"] = {"level": level, "reasons": list(dict.fromkeys(t for _, t in reasons)), "skipped": skipped}
    return rep
