"""Web search, page reading and downloads.

Reading a page strips scripts, styles and chrome so the model gets prose rather
than markup — small models in particular waste their whole context on raw HTML.
"""

from __future__ import annotations

import html
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Literal

from .registry import ToolResult, ctx, tool

TOOLSET = "web"

USER_AGENT = "Mozilla/5.0 (compatible; ArcBot/1.0; +https://github.com/skorpix-code/ArcBot)"
FETCH_TIMEOUT = 25
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024

_SCRIPT_STYLE = re.compile(
    r"<(script|style|noscript|svg|head|nav|footer|aside|form)\b.*?</\1>", re.S | re.I
)
_TAG = re.compile(r"<[^>]+>")
_BLANKS = re.compile(r"\n\s*\n\s*\n+")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)


def _require_http(url: str) -> str | None:
    """Reject anything that is not plain http(s) — no file://, no gopher://."""
    parsed = urllib.parse.urlparse(url or "")
    if parsed.scheme not in ("http", "https"):
        return "Only http:// and https:// URLs are allowed."
    if not parsed.netloc:
        return "That URL has no host."
    return None


def _fetch(url: str, *, max_bytes: int = 4_000_000) -> tuple[str, bytes, str]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
        content_type = response.headers.get("Content-Type", "")
        raw = response.read(max_bytes)
        return response.geturl(), raw, content_type


def html_to_text(markup: str) -> tuple[str, str]:
    """Return ``(title, readable text)`` for an HTML document."""
    title_match = _TITLE.search(markup)
    title = html.unescape(title_match.group(1)).strip() if title_match else ""
    body = _SCRIPT_STYLE.sub(" ", markup)
    body = re.sub(r"<br\s*/?>", "\n", body, flags=re.I)
    body = re.sub(r"</(p|div|li|h[1-6]|tr|section|article)>", "\n\n", body, flags=re.I)
    body = re.sub(r"<li\b[^>]*>", "• ", body, flags=re.I)
    body = _TAG.sub("", body)
    body = html.unescape(body)
    body = "\n".join(line.strip() for line in body.splitlines())
    return title, _BLANKS.sub("\n\n", body).strip()


@tool(toolset=TOOLSET, capability="network", title="Search: {query}", preview_chars=1500)
def web_search(query: str, max_results: int = 6) -> ToolResult:
    """Search the web and return titles, URLs and snippets.

    Use this for anything time-sensitive or outside your training data, then
    read the most promising result with read_webpage.

    Args:
        query: What to search for.
        max_results: How many results to return (1-15).
    """
    try:
        from ddgs import DDGS
    except ImportError:
        return ToolResult.error(
            "Web search needs the 'ddgs' package. Ask the user to run: pip install ddgs"
        )

    limit = max(1, min(int(max_results or 6), 15))
    try:
        with DDGS() as engine:
            results = list(engine.text(query, max_results=limit))
    except Exception as exc:
        return ToolResult.error(f"Search failed: {exc}")

    if not results:
        return ToolResult(True, f"No results for {query!r}.")

    lines = []
    for index, item in enumerate(results, 1):
        title = item.get("title", "(untitled)")
        url = item.get("href") or item.get("url", "")
        snippet = " ".join((item.get("body") or "").split())[:300]
        lines.append(f"{index}. {title}\n   {url}\n   {snippet}")
    return ToolResult(
        True,
        f"Results for {query!r}:\n\n" + "\n\n".join(lines),
        {"results": [{"title": r.get("title"), "url": r.get("href") or r.get("url")} for r in results]},
    )


@tool(toolset=TOOLSET, capability="network", title="Read {url}", preview_chars=1500)
def read_webpage(url: str, max_chars: int = 8000) -> ToolResult:
    """Fetch a web page and return it as readable text.

    Args:
        url: The http(s) URL to read.
        max_chars: Maximum characters of page text to return.
    """
    context = ctx()
    problem = _require_http(url)
    if problem:
        return ToolResult.error(problem)
    try:
        final_url, raw, content_type = _fetch(url)
    except urllib.error.HTTPError as exc:
        return ToolResult.error(f"HTTP {exc.code} fetching {url}: {exc.reason}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return ToolResult.error(f"Could not fetch {url}: {exc}")

    charset = "utf-8"
    if "charset=" in content_type:
        charset = content_type.split("charset=")[-1].split(";")[0].strip() or "utf-8"
    try:
        decoded = raw.decode(charset, errors="replace")
    except LookupError:
        decoded = raw.decode("utf-8", errors="replace")

    if "html" in content_type.lower() or decoded.lstrip()[:200].lower().startswith(("<!doctype", "<html")):
        title, text = html_to_text(decoded)
    else:
        title, text = "", decoded

    body = text[: max(500, int(max_chars or 8000))]
    header = f"{title}\n{final_url}" if title else final_url
    truncated = len(text) > len(body)
    if truncated:
        body += f"\n\n… [{len(text) - len(body):,} more characters not shown]"
    return ToolResult(True, context.clip(f"{header}\n\n{body}"), {"url": final_url, "title": title})


@tool(toolset=TOOLSET, capability="network", title="Download {url}")
def download_file(url: str, destination: str) -> ToolResult:
    """Download a file into the workspace.

    Args:
        url: The http(s) URL to download.
        destination: Where to save it, relative to the workspace.
    """
    context = ctx()
    problem = _require_http(url)
    if problem:
        return ToolResult.error(problem)
    target = context.path(destination)
    if target.exists():
        return ToolResult.error(f"{context.rel(target)} already exists.")
    target.parent.mkdir(parents=True, exist_ok=True)

    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
            written = 0
            with target.open("wb") as fh:
                while chunk := response.read(262_144):
                    written += len(chunk)
                    if written > MAX_DOWNLOAD_BYTES:
                        fh.close()
                        target.unlink(missing_ok=True)
                        return ToolResult.error(
                            f"Aborted: the file exceeds the {MAX_DOWNLOAD_BYTES // 1024 // 1024} MB limit."
                        )
                    fh.write(chunk)
    except urllib.error.HTTPError as exc:
        target.unlink(missing_ok=True)
        return ToolResult.error(f"HTTP {exc.code}: {exc.reason}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        target.unlink(missing_ok=True)
        return ToolResult.error(f"Download failed: {exc}")

    return ToolResult(
        True,
        f"Downloaded {written:,} bytes to {context.rel(target)}",
        {"path": context.rel(target), "bytes": written},
    )


@tool(toolset=TOOLSET, capability="network", title="{method} {url}", preview_chars=1200)
def http_request(
    url: str,
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"] = "GET",
    body: str = "",
    content_type: str = "application/json",
) -> ToolResult:
    """Call an HTTP API and return the status, headers and response body.

    Args:
        url: The http(s) endpoint.
        method: HTTP method.
        body: Request body for POST/PUT/PATCH.
        content_type: Content-Type header for the request body.
    """
    context = ctx()
    problem = _require_http(url)
    if problem:
        return ToolResult.error(problem)

    data = body.encode("utf-8") if body else None
    headers = {"User-Agent": USER_AGENT}
    if data:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
            payload = response.read(1_000_000).decode("utf-8", errors="replace")
            status, response_headers = response.status, dict(response.headers)
    except urllib.error.HTTPError as exc:
        payload = exc.read(200_000).decode("utf-8", errors="replace")
        status, response_headers = exc.code, dict(exc.headers or {})
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return ToolResult.error(f"Request failed: {exc}")

    shown = {k: v for k, v in response_headers.items()
             if k.lower() in ("content-type", "content-length", "location", "etag")}
    header_text = "\n".join(f"{k}: {v}" for k, v in shown.items())
    return ToolResult(
        200 <= status < 400,
        context.clip(f"HTTP {status}\n{header_text}\n\n{payload}"),
        {"status": status},
    )
