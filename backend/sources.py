from __future__ import annotations

import asyncio
from html.parser import HTMLParser
import ipaddress
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import feedparser
import httpx


ALLOWED_HOSTS = {
    "arxiv.org", "export.arxiv.org", "habr.com", "habrastorage.org",
    "docs.vllm.ai", "docs.langchain.com", "qdrant.tech",
    "pytorch.org", "docling-project.github.io",
}
HABR_HUBS = ("machine_learning", "artificial_intelligence", "python", "devops")
MAX_REDIRECTS = 3
MAX_BYTES = 25 * 1024 * 1024
DOCUMENTATION_SITEMAPS = (
    "https://docs.vllm.ai/sitemap.xml",
    "https://docs.langchain.com/sitemap.xml",
    "https://qdrant.tech/sitemap.xml",
    "https://pytorch.org/sitemap.xml",
    "https://docling-project.github.io/docling/sitemap.xml",
)
_sitemap_cache: tuple[float, list[str]] = (0.0, [])


@dataclass(frozen=True)
class SourceCandidate:
    title: str
    url: str
    kind: str
    published: str | None = None


class ArxivSearchParser(HTMLParser):
    """Резервный разбор публичной страницы arXiv без выполнения скриптов."""

    def __init__(self) -> None:
        super().__init__()
        self.items: list[SourceCandidate] = []
        self.active = False
        self.title_active = False
        self.title_parts: list[str] = []
        self.pdf_url = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        if tag == "li" and "arxiv-result" in classes:
            self.active, self.title_parts, self.pdf_url = True, [], ""
        elif self.active and tag == "p" and "title" in classes:
            self.title_active = True
        elif self.active and tag == "a":
            href = values.get("href") or ""
            if href.startswith("https://arxiv.org/pdf/"):
                self.pdf_url = href

    def handle_data(self, data: str) -> None:
        if self.title_active:
            self.title_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.active and tag == "p" and self.title_active:
            self.title_active = False
        elif self.active and tag == "li":
            title = " ".join(" ".join(self.title_parts).split())
            if title and self.pdf_url:
                self.items.append(SourceCandidate(title, self.pdf_url, "arxiv"))
            self.active = False


def validate_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Разрешены только HTTPS URL источников")
    if parsed.hostname.lower() not in ALLOWED_HOSTS or parsed.port not in (None, 443):
        raise ValueError("Домен источника не разрешён")
    return parsed.hostname.lower()


async def reject_nonpublic_dns(host: str) -> None:
    infos = await asyncio.to_thread(socket.getaddrinfo, host, 443)
    if not infos:
        raise ValueError("DNS не вернул адреса")
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise ValueError("Источник указывает на внутренний адрес")


async def fetch_allowed(url: str, client: httpx.AsyncClient) -> tuple[bytes, str, str]:
    for _ in range(MAX_REDIRECTS + 1):
        host = validate_url(url)
        await reject_nonpublic_dns(host)
        async with client.stream("GET", url, follow_redirects=False, timeout=30) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise ValueError("Пустой redirect")
                url = str(response.url.join(location))
                continue
            response.raise_for_status()
            chunks, total = [], 0
            async for part in response.aiter_bytes():
                total += len(part)
                if total > MAX_BYTES:
                    raise ValueError("Источник превышает предел размера")
                chunks.append(part)
            return b"".join(chunks), response.headers.get("content-type", ""), str(response.url)
    raise ValueError("Слишком много redirects")


async def search_arxiv(query: str, client: httpx.AsyncClient, limit: int = 8) -> list[SourceCandidate]:
    response = await client.get(
        "https://export.arxiv.org/api/query",
        params={"search_query": f"all:{query[:120]}", "start": 0, "max_results": limit, "sortBy": "relevance"},
        timeout=30,
    )
    if response.status_code in {406, 503}:
        fallback = await client.get(
            "https://arxiv.org/search/",
            params={"query": query[:120], "searchtype": "all", "abstracts": "show", "size": 25},
            timeout=30,
        )
        fallback.raise_for_status()
        parser = ArxivSearchParser()
        parser.feed(fallback.text)
        return parser.items[:limit]
    response.raise_for_status()
    root = ET.fromstring(response.content)
    atom = "{http://www.w3.org/2005/Atom}"
    candidates = []
    for entry in root.findall(f"{atom}entry"):
        title = " ".join((entry.findtext(f"{atom}title") or "").split())
        page = entry.findtext(f"{atom}id") or ""
        if page.startswith("http://arxiv.org/"):
            page = "https://" + page[len("http://"):]
        if page.startswith("https://arxiv.org/abs/"):
            candidates.append(SourceCandidate(title, page.replace("/abs/", "/pdf/"), "arxiv", entry.findtext(f"{atom}published")))
    return candidates


async def search_habr_index(query: str, client: httpx.AsyncClient, limit: int = 6) -> list[SourceCandidate]:
    """Поиск только по RSS, без обхода запрещённых /search/ страниц."""
    tokens = {word.lower() for word in query.split() if len(word) > 3}
    found: list[tuple[int, SourceCandidate]] = []
    for hub in HABR_HUBS:
        url = f"https://habr.com/ru/rss/hubs/{hub}/articles/"
        try:
            data, _, _ = await fetch_allowed(url, client)
        except (httpx.HTTPError, ValueError):
            continue
        feed = feedparser.parse(data)
        for entry in feed.entries:
            article = entry.get("link", "")
            try:
                validate_url(article)
            except ValueError:
                continue
            haystack = (entry.get("title", "") + " " + entry.get("summary", "")).lower()
            score = sum(token in haystack for token in tokens)
            if score:
                found.append((score, SourceCandidate(entry.get("title", ""), article, "habr", entry.get("published"))))
    found.sort(key=lambda item: item[0], reverse=True)
    unique: dict[str, SourceCandidate] = {}
    for _, candidate in found:
        unique.setdefault(candidate.url, candidate)
    return list(unique.values())[:limit]


async def search_documentation(query: str, client: httpx.AsyncClient, limit: int = 6) -> list[SourceCandidate]:
    """Ищет по sitemap разрешённых разделов; список кешируется на шесть часов."""
    global _sitemap_cache
    if time.time() - _sitemap_cache[0] > 21600:
        urls: list[str] = []
        for root_url in DOCUMENTATION_SITEMAPS:
            pending = [root_url]
            seen: set[str] = set()
            while pending and len(seen) < 4:
                current = pending.pop(0)
                if current in seen:
                    continue
                seen.add(current)
                try:
                    data, _, _ = await fetch_allowed(current, client)
                    tree = ET.fromstring(data)
                except (ValueError, httpx.HTTPError, ET.ParseError):
                    continue
                for element in tree.iter():
                    if not element.tag.endswith("loc") or not element.text:
                        continue
                    item = element.text.strip()
                    try:
                        host = validate_url(item)
                    except ValueError:
                        continue
                    if item.endswith(".xml") and host == urlparse(root_url).hostname:
                        if len(pending) < 3:
                            pending.append(item)
                    elif "/docs/" in item or host in {"docs.vllm.ai", "docs.langchain.com", "docling-project.github.io"}:
                        urls.append(item)
                    if len(urls) >= 5000:
                        break
                if len(urls) >= 5000:
                    break
        _sitemap_cache = (time.time(), list(dict.fromkeys(urls)))
    tokens = [word.casefold() for word in query.split() if len(word) > 3]
    ranked = []
    for url in _sitemap_cache[1]:
        path = urlparse(url).path.casefold().replace("-", "_")
        score = sum(token in path for token in tokens)
        if score:
            ranked.append((score, url))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [
        SourceCandidate(urlparse(url).path.rsplit("/", 2)[-1].replace("-", " ") or url, url, "documentation")
        for _, url in ranked[:limit]
    ]
