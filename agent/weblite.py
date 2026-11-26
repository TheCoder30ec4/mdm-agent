"""Built-in lite web search + scrape, used when no Firecrawl is reachable (the desktop app, run.sh without Docker).
Search: the ddgs metasearch library. Scrape: one guarded GET of the page, turned into text with markdown links.
No JavaScript rendering: pages that build their content in the browser come back thin."""
import time
from urllib.parse import urljoin, urlparse

import httpx

from agent import inspect as ins

MAX_BYTES = 3_000_000
TIMEOUT = 15.0
MAX_REDIRECTS = 5
UA = "Mozilla/5.0 (compatible; mdm-agent/1.0)"
SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "head", "form", "button", "select", "nav", "aside"}
FILE_EXT = {"mp4", "m4v", "mkv", "webm", "avi", "mov", "wmv", "mpg", "mpeg", "ogv", "mp3", "m4a", "flac", "ogg", "opus",
            "wav", "aac", "zip", "7z", "rar", "tar", "gz", "xz", "bz2", "iso", "img", "dmg", "pdf", "epub", "mobi", "cbz",
            "torrent", "srt", "exe", "msi", "pkg", "deb", "rpm", "appimage", "apk"}
BLOCKS = {"p", "div", "section", "article", "main", "header", "footer", "nav", "aside", "li", "tr", "table", "ul", "ol",
          "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "br", "hr", "dt", "dd", "figure", "figcaption"}


class LiteError(Exception):
    pass


NOISE = {"download", "downloads", "free", "mp4", "mkv", "avi", "mp3", "flac", "pdf", "epub", "iso", "torrent", "hd",
         "1080p", "720p", "4k", "full", "movie", "film", "public", "domain", "archive.org", "archive", "site:archive.org",
         "direct", "link", "file", "legal", "watch", "online", "the", "a", "of", "and"}


def search(query: str, limit: int = 5) -> list[dict]:
    """Web results (ddgs, which scrapes public engines and can be rate-limited) followed by Internet Archive
    items (its official search API: stable, keyless, where most legal films, music, books and ISOs live)."""
    web, errors = [], []
    for attempt in (1, 2):  # ddgs rotates backends; one retry rides out a rate-limited or empty one
        try:
            from ddgs import DDGS
            res = DDGS(timeout=10).text(query, max_results=limit) or []
            web = [{"title": r.get("title") or "", "url": r["href"], "description": r.get("body") or ""}
                   for r in res if (r.get("href") or "").startswith(("http://", "https://"))][:limit]
            break
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}")
    try:
        ia = _archive(query, 3)
    except Exception as e:
        ia, errors = [], errors + [f"archive.org: {type(e).__name__}: {e}"]
    seen, out = set(), []
    for r in web + ia:
        if r["url"] not in seen:
            seen.add(r["url"])
            out.append(r)
    if not out and errors:
        raise LiteError("search failed: " + errors[-1])
    return out


def _archive(query: str, rows: int) -> list[dict]:
    import re
    words = [w for w in re.findall(r"[\w.'-]+", query.lower()) if w not in NOISE][:8]
    if not words:
        return []
    q = "title:(" + " ".join(w.replace(":", "") for w in words) + ")"
    r = httpx.get("https://archive.org/advancedsearch.php", timeout=10, trust_env=False, headers={"User-Agent": UA},
                  params=[("q", q), ("fl[]", "identifier"), ("fl[]", "title"), ("fl[]", "description"),
                          ("fl[]", "mediatype"), ("sort[]", "downloads desc"), ("rows", str(rows)), ("output", "json")])
    r.raise_for_status()
    out = []
    for d in r.json().get("response", {}).get("docs", []):
        ident = d.get("identifier")
        if not ident:
            continue
        desc = d.get("description")
        desc = " ".join(desc) if isinstance(desc, list) else (desc or "")
        out.append({"title": f"{d.get('title') or ident} (Internet Archive, {d.get('mediatype') or 'item'})",
                    "url": f"https://archive.org/details/{ident}", "description": " ".join(desc.split())[:300]})
    return out


def fetch(url: str) -> tuple[str, str]:
    """(final url, html) of a public http(s) page. Every hop goes through inspect's guard and connects to the
    validated IP (no DNS rebinding, no redirect into the LAN); html/plain text only, at most 3 MB in 15 s."""
    deadline = time.monotonic() + TIMEOUT
    with httpx.Client(transport=ins._TRANSPORT, timeout=TIMEOUT, follow_redirects=False, trust_env=False) as client:
        cur = url
        for _ in range(MAX_REDIRECTS + 1):
            try:
                ins._check(cur)
                u = httpx.URL(cur)
                if u.scheme not in ("http", "https"):
                    raise LiteError("only http and https pages can be read")
                host = u.raw_host.decode("ascii")
                ips = ins._safe_ips(host, u.port or (443 if u.scheme == "https" else 80))
            except ins.Blocked as e:
                raise LiteError(f"blocked: {e}") from None
            pinned = u.copy_with(host=ips[0], userinfo=b"")
            hdrs = {"Host": u.netloc.decode().rsplit("@", 1)[-1], "User-Agent": UA,
                    "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8", "Accept-Language": "en"}
            ext = {"sni_hostname": host} if u.scheme == "https" else {}
            resp = client.send(client.build_request("GET", pinned, headers=hdrs, extensions=ext), stream=True)
            try:
                if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                    cur = urljoin(cur, resp.headers["location"])
                    continue
                if resp.status_code >= 400:
                    raise LiteError(f"the page answered HTTP {resp.status_code}")
                ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if ctype and not (ctype.startswith("text/") or "html" in ctype or "xml" in ctype):
                    raise LiteError(f"not a web page ({ctype}); use inspect_link for files")
                buf = bytearray()
                for chunk in resp.iter_bytes():
                    buf += chunk
                    if len(buf) > MAX_BYTES or time.monotonic() > deadline:
                        break  # ponytail: a truncated page still yields its first links and text
                return cur, bytes(buf).decode(resp.encoding or "utf-8", errors="replace")
            finally:
                resp.close()
    raise LiteError("too many redirects")


def _is_file(href: str) -> bool:
    if href.startswith("magnet:"):
        return True
    name = urlparse(href).path.rsplit("/", 1)[-1]
    return "." in name and name.rsplit(".", 1)[-1].lower() in FILE_EXT


def _text(root, base: str, files: list) -> str:
    """Visible text with links as [text](absolute url), blocks on their own lines; file links also go to files."""
    out = []

    def walk(el):
        tag = el.tag if isinstance(el.tag, str) else ""
        if tag in SKIP:
            out.append(el.tail or "")
            return
        if tag == "a" and el.get("href"):
            href = urljoin(base, el.get("href").strip())
            label = " ".join(el.text_content().split())
            if urlparse(href).scheme in ("http", "https", "magnet") or href.startswith("magnet:"):
                out.append(f" [{label or href}]({href}) ")
                if _is_file(href) and href not in {f[1] for f in files}:
                    files.append((label or href.rsplit("/", 1)[-1], href))
            else:
                out.append(label)
            out.append(el.tail or "")
            return
        if tag in BLOCKS:
            out.append("\n")
        if tag and tag[:1] == "h" and tag[1:].isdigit():
            out.append("#" * int(tag[1:]) + " ")
        out.append(el.text or "")
        for child in el:
            walk(child)
        if tag in BLOCKS:
            out.append("\n")
        out.append(el.tail or "")

    walk(root)
    lines = (" ".join(line.split()) for line in "".join(out).splitlines())
    return "\n".join(line for line in lines if line)


SKIP_IA = ("_meta.xml", "_files.xml", "_meta.sqlite", "_reviews.xml", "__ia_thumb.jpg", ".afpk", "_archive.torrent")


def _mb(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "size unknown"
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.1f} MB"


def _archive_item(ident: str, max_chars: int) -> dict:
    """An Internet Archive item through its metadata API: every file with its size and format, plus the licence.
    The details and download pages are mostly menus and scripts; this is what a researcher needs."""
    from urllib.parse import quote
    r = httpx.get(f"https://archive.org/metadata/{quote(ident)}", timeout=10, trust_env=False, headers={"User-Agent": UA})
    r.raise_for_status()
    d = r.json()
    m = d.get("metadata") or {}
    if not m:
        raise LiteError(f"no Internet Archive item named {ident}")

    def one(v):
        return " ".join(v) if isinstance(v, list) else str(v or "")

    desc = one(m.get("description"))
    if "<" in desc:  # uploaders often paste HTML
        import lxml.html
        try:
            desc = lxml.html.fragment_fromstring(desc, create_parent="div").text_content()
        except Exception:
            pass
    desc = " ".join(desc.split())
    files = [f for f in d.get("files") or [] if f.get("name") and not f["name"].endswith(SKIP_IA)
             and ".thumbs/" not in f["name"] and f.get("format") != "Thumbnail"]
    files.sort(key=lambda f: -int(f.get("size") or 0))
    lines = [f"# {one(m.get('title')) or ident}",
             f"Creator: {one(m.get('creator'))}" if m.get("creator") else "",
             f"Date: {one(m.get('date') or m.get('year'))}" if m.get("date") or m.get("year") else "",
             f"Licence: {one(m.get('licenseurl'))}" if m.get("licenseurl") else "Licence: not stated",
             desc[:600], "",
             f"Files ({len(files)}), largest first. Direct download URLs:"]
    for f in files[:40]:
        lines.append(f"- [{f['name']}](https://archive.org/download/{quote(ident)}/{quote(f['name'])}) "
                     f"{_mb(f.get('size'))}, {f.get('format') or 'unknown format'}")
    lines.append(f"- [{ident}_archive.torrent](https://archive.org/download/{quote(ident)}/{quote(ident)}_archive.torrent) "
                 "torrent of the whole item")
    return {"url": f"https://archive.org/details/{ident}", "title": one(m.get("title")) or ident,
            "description": desc[:300] or None,
            "image": f"https://archive.org/services/img/{quote(ident)}",
            "text": "\n".join(x for x in lines if x is not None)[:max_chars]}


def _archive_id(url: str) -> str | None:
    """The item id of an archive.org details, download-folder or metadata URL (not a single file in it)."""
    p = urlparse(url)
    if (p.hostname or "").lower() not in ("archive.org", "www.archive.org"):
        return None
    parts = [x for x in p.path.split("/") if x]
    from urllib.parse import unquote
    if len(parts) >= 2 and parts[0] in ("details", "metadata"):
        return unquote(parts[1])
    if len(parts) == 2 and parts[0] == "download" and not _is_file(url):
        return unquote(parts[1])
    return None


def scrape(url: str, max_chars: int = 8000) -> dict:
    import lxml.html
    ident = _archive_id(url)
    if ident:
        try:
            return _archive_item(ident, max_chars)
        except Exception:
            pass  # ponytail: the plain page is still worth reading when the API is down
    try:
        final, html = fetch(url)
    except LiteError:
        raise
    except Exception as e:
        raise LiteError(f"scrape failed: {type(e).__name__}: {e}") from None
    if not html.strip():
        return {"url": final, "title": None, "description": None, "image": None, "text": ""}
    try:
        doc = lxml.html.document_fromstring(html)
    except Exception:  # plain text or unparsable markup
        return {"url": final, "title": None, "description": None, "image": None, "text": html[:max_chars]}

    def meta(*names):
        for n in names:
            v = doc.xpath(f'string(//meta[@property="{n}" or @name="{n}"]/@content)').strip()
            if v:
                return v
        return None

    image = meta("og:image", "twitter:image")
    body = doc.find("body")
    files = []
    text = _text(body if body is not None else doc, final, files)
    if files:  # what a download researcher needs first; the page text after it is often cut off
        text = "Files linked on this page:\n" + "\n".join(f"- [{n}]({u})" for n, u in files[:40]) + "\n\n" + text
    return {
        "url": final,
        "title": meta("og:title") or (doc.findtext(".//title") or "").strip() or None,
        "description": meta("og:description", "description"),
        "image": urljoin(final, image) if image else None,
        "text": text[:max_chars],
    }
