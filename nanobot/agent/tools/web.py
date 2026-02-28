"""Web tools: web_search and web_fetch."""

import html
import json
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from nanobot.agent.tools.base import Tool

# Shared constants
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_2) AppleWebKit/537.36"
MAX_REDIRECTS = 5  # Limit redirects to prevent DoS attacks
DDG_LITE_URL = "https://lite.duckduckgo.com/lite/"
DDG_RESULT_LIMIT = 20
ANCHOR_RE = re.compile(
    r"<a(?P<attrs>[^>]*class=['\"]result-link['\"][^>]*)>(?P<title>.*?)</a>",
    flags=re.S,
)
HREF_RE = re.compile(r"href=['\"]([^'\"]+)['\"]", flags=re.S)
SNIPPET_RE = re.compile(r"class=['\"]result-snippet['\"][^>]*>(.*?)</td>", flags=re.S)


def _strip_tags(text: str) -> str:
    """Remove HTML tags and decode entities."""
    text = re.sub(r"<script[\s\S]*?</script>", "", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", "", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def _normalize(text: str) -> str:
    """Normalize whitespace."""
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _validate_url(url: str) -> tuple[bool, str]:
    """Validate URL: must be http(s) with valid domain."""
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return False, f"Only http/https allowed, got '{p.scheme or 'none'}'"
        if not p.netloc:
            return False, "Missing domain"
        return True, ""
    except Exception as e:
        return False, str(e)


def _clean_html_text(text: str) -> str:
    """Strip tags and collapse whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(text))).strip()


def _to_result_url(href: str) -> str:
    """Extract final URL from DuckDuckGo redirect links when present."""
    raw = html.unescape(href).strip()
    if raw.startswith("//"):
        raw = "https:" + raw
    uddg = parse_qs(urlparse(raw).query).get("uddg")
    return unquote(uddg[0]) if uddg and uddg[0] else raw


def _parse_ddg_lite_results(page: str) -> list[dict[str, str]]:
    """Parse DuckDuckGo Lite result links/snippets from HTML."""
    if "Unfortunately, bots use DuckDuckGo too" in page or "anomaly-modal" in page:
        raise RuntimeError("DuckDuckGo anti-bot challenge encountered; retry later")

    anchors = list(ANCHOR_RE.finditer(page))
    results: list[dict[str, str]] = []
    seen: set[str] = set()

    for i, match in enumerate(anchors):
        row_start = page.rfind("<tr", 0, match.start())
        if row_start != -1 and "result-sponsored" in page[row_start : match.start()]:
            continue

        href_match = HREF_RE.search(match.group("attrs"))
        if not href_match:
            continue

        link = _to_result_url(href_match.group(1))
        title = _clean_html_text(match.group("title"))
        if not title or not link or link in seen:
            continue

        next_start = anchors[i + 1].start() if i + 1 < len(anchors) else len(page)
        snippet_match = SNIPPET_RE.search(page[match.end() : next_start])
        description = _clean_html_text(snippet_match.group(1)) if snippet_match else ""

        seen.add(link)
        results.append({"title": title, "link": link, "description": description})
        if len(results) >= DDG_RESULT_LIMIT:
            break

    return results


class WebSearchTool(Tool):
    """Search the web using DuckDuckGo Lite."""

    name = "web_search"
    description = "Search the web. Returns titles, URLs, and snippets."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "count": {
                "type": "integer",
                "description": "Results (1-10)",
                "minimum": 1,
                "maximum": 10,
            },
            "market": {
                "type": "string",
                "description": "Locale hint like en-US",
                "default": "en-US",
            },
        },
        "required": ["query"],
    }

    def __init__(self, max_results: int = 5):
        self.max_results = max_results

    async def execute(
        self,
        query: str,
        count: int | None = None,
        market: str = "en-US",
        **kwargs: Any,
    ) -> str:
        try:
            n = min(max(count or self.max_results, 1), 10)
            query = query.strip()
            market = (market or "en-US").strip()
            if not query:
                return "Error: query must not be empty"

            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.get(
                    DDG_LITE_URL,
                    params={"q": query, "kl": market.lower()},
                    headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8"},
                )
                r.raise_for_status()

            results = _parse_ddg_lite_results(r.text)
            if not results:
                return f"No results for: {query}"

            lines = [f"Results for: {query}\n"]
            for i, item in enumerate(results[:n], 1):
                lines.append(f"{i}. {item.get('title', '')}\n   {item.get('link', '')}")
                if desc := item.get("description"):
                    lines.append(f"   {desc}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"


class WebFetchTool(Tool):
    """Fetch and extract content from a URL using Readability."""

    name = "web_fetch"
    description = "Fetch URL and extract readable content (HTML → markdown/text)."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "extractMode": {"type": "string", "enum": ["markdown", "text"], "default": "markdown"},
            "maxChars": {"type": "integer", "minimum": 100},
        },
        "required": ["url"],
    }

    def __init__(self, max_chars: int = 50000):
        self.max_chars = max_chars

    async def execute(
        self, url: str, extractMode: str = "markdown", maxChars: int | None = None, **kwargs: Any
    ) -> str:
        from readability import Document

        max_chars = maxChars or self.max_chars

        # Validate URL before fetching
        is_valid, error_msg = _validate_url(url)
        if not is_valid:
            return json.dumps(
                {"error": f"URL validation failed: {error_msg}", "url": url}, ensure_ascii=False
            )

        try:
            async with httpx.AsyncClient(
                follow_redirects=True, max_redirects=MAX_REDIRECTS, timeout=30.0
            ) as client:
                r = await client.get(url, headers={"User-Agent": USER_AGENT})
                r.raise_for_status()

            ctype = r.headers.get("content-type", "")

            # JSON
            if "application/json" in ctype:
                text, extractor = json.dumps(r.json(), indent=2, ensure_ascii=False), "json"
            # HTML
            elif "text/html" in ctype or r.text[:256].lower().startswith(("<!doctype", "<html")):
                doc = Document(r.text)
                content = (
                    self._to_markdown(doc.summary())
                    if extractMode == "markdown"
                    else _strip_tags(doc.summary())
                )
                text = f"# {doc.title()}\n\n{content}" if doc.title() else content
                extractor = "readability"
            else:
                text, extractor = r.text, "raw"

            truncated = len(text) > max_chars
            if truncated:
                text = text[:max_chars]

            return json.dumps(
                {
                    "url": url,
                    "finalUrl": str(r.url),
                    "status": r.status_code,
                    "extractor": extractor,
                    "truncated": truncated,
                    "length": len(text),
                    "text": text,
                },
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps({"error": str(e), "url": url}, ensure_ascii=False)

    def _to_markdown(self, html: str) -> str:
        """Convert HTML to markdown."""
        # Convert links, headings, lists before stripping tags
        text = re.sub(
            r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>',
            lambda m: f"[{_strip_tags(m[2])}]({m[1]})",
            html,
            flags=re.I,
        )
        text = re.sub(
            r"<h([1-6])[^>]*>([\s\S]*?)</h\1>",
            lambda m: f"\n{'#' * int(m[1])} {_strip_tags(m[2])}\n",
            text,
            flags=re.I,
        )
        text = re.sub(
            r"<li[^>]*>([\s\S]*?)</li>", lambda m: f"\n- {_strip_tags(m[1])}", text, flags=re.I
        )
        text = re.sub(r"</(p|div|section|article)>", "\n\n", text, flags=re.I)
        text = re.sub(r"<(br|hr)\s*/?>", "\n", text, flags=re.I)
        return _normalize(_strip_tags(text))
