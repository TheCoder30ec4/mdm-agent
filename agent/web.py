"""Firecrawl search + scrape. Tolerates dict or object responses across SDK versions."""
import httpx

from agent import settings


class WebError(Exception):
    pass


_ENGINE = {"at": 0.0, "url": None, "name": None}


def engine() -> str:
    """'firecrawl' when the configured Firecrawl accepts connections, else 'lite' (agent/weblite.py).
    Checked at most every 30 s, and again at once when the configured URL changes."""
    import socket
    import time
    from urllib.parse import urlsplit
    url = settings.effective(settings.default_path())["firecrawl_url"]
    now = time.monotonic()
    if _ENGINE["url"] != url or now - _ENGINE["at"] > 30:
        p = urlsplit(url or "")
        try:
            socket.create_connection((p.hostname, p.port or (443 if p.scheme == "https" else 80)), timeout=1).close()
            name = "firecrawl"
        except (OSError, TypeError, ValueError):
            name = "lite"
        _ENGINE.update(at=now, url=url, name=name)
    return _ENGINE["name"]


def _client():
    # Self-hosted Firecrawl by default (scripts/firecrawl_local.sh). A self-hosted API ignores the key.
    # Settings (Web search) first, then FIRECRAWL_API_URL / FIRECRAWL_API_KEY, for another instance or the cloud.
    # ponytail: reads the default library's settings.json (LIBRARY_DIR), which is where Jobs() keeps it too
    cfg = settings.effective(settings.default_path())
    url = cfg["firecrawl_url"]
    key = cfg["firecrawl_key"] or "local"
    try:
        from firecrawl import Firecrawl
    except ImportError:
        from firecrawl import FirecrawlApp as Firecrawl  # older SDKs
    return Firecrawl(api_key=key, api_url=url, max_retries=1)


def _g(obj, name, default=None):
    """Read a field from a dict or an object."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        v = obj.get(name)
    else:
        v = getattr(obj, name, None)
    return default if v is None else v


def search(query: str, limit: int = 5) -> list[dict]:
    if engine() == "lite":
        from agent import weblite
        try:
            return weblite.search(query, limit)
        except weblite.LiteError as e:
            raise WebError(str(e)) from None
    try:
        res = _client().search(query, limit=limit)
        # v2: SearchData(.web); v1: {"data": [...]} or a plain list
        items = _g(res, "web") or _g(res, "data") or (res if isinstance(res, list) else [])
        out = []
        for it in items:
            meta = _g(it, "metadata")
            url = _g(it, "url") or _g(meta, "url")
            if not url:
                continue
            out.append({
                "title": _g(it, "title") or _g(meta, "title") or "",
                "url": url,
                "description": _g(it, "description") or _g(meta, "description") or "",
            })
        return out[:limit]
    except WebError:
        raise
    except Exception as e:
        raise WebError(f"search failed: {type(e).__name__}: {e}") from None


def _guard(url: str) -> None:
    """Only public http(s) URLs: a prompt-injected page must not steer a scrape at the LAN or file://."""
    from agent import inspect, mdm  # inspect imports web at module level
    if not isinstance(url, str) or url.split(":", 1)[0].lower() not in ("http", "https"):
        raise WebError("blocked: only http and https URLs can be scraped")
    try:
        mdm._check_source(url)
        u = httpx.URL(url)
        # ponytail: checks the IPs we resolve now; Firecrawl resolves again and follows redirects itself
        # (DNS rebinding / redirect-to-LAN). Pin at the Firecrawl side if that matters.
        inspect._safe_ips(u.raw_host.decode("ascii"), u.port or (443 if u.scheme == "https" else 80))
    except (mdm.MdmError, inspect.Blocked) as e:
        raise WebError(f"blocked: {e}") from None


def scrape(url: str, max_chars: int = 8000) -> dict:
    _guard(url)
    if engine() == "lite":
        from agent import weblite
        try:
            return weblite.scrape(url, max_chars)
        except weblite.LiteError as e:
            raise WebError(str(e)) from None
    try:
        res = _client().scrape(url, formats=["markdown"])
        res = _g(res, "data", res)  # v1 wraps in "data" on some versions
        meta = _g(res, "metadata", {})
        text = _g(res, "markdown", "") or ""
        return {
            "url": _g(meta, "url") or _g(meta, "source_url") or _g(meta, "sourceURL") or url,
            "title": _g(meta, "title") or _g(meta, "og_title") or _g(meta, "ogTitle"),
            "description": _g(meta, "description") or _g(meta, "og_description") or _g(meta, "ogDescription"),
            "image": _g(meta, "og_image") or _g(meta, "ogImage"),
            "text": text[:max_chars],
        }
    except WebError:
        raise
    except Exception as e:
        raise WebError(f"scrape failed: {type(e).__name__}: {e}") from None
