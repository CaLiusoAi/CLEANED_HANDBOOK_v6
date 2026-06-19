#!/usr/bin/env python3
"""Build a chunked Pine Script v6 RAG dataset from canonical TradingView docs.

The crawler is intentionally scoped to the official TradingView Pine Script v6
User Manual and Reference Manual URL spaces. It writes raw HTML, normalized page
JSON, per-page chunk JSONL files, a combined JSONL dataset, and a CSV manifest.
It uses only the Python standard library so it can run in restricted local
Python environments without installing third-party packages.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import time
from collections import deque
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

BASE_ALLOWED = (
    "https://www.tradingview.com/pine-script-docs/",
    "https://www.tradingview.com/pine-script-reference/v6/",
)

DEFAULT_SEED_URLS = (
    "https://www.tradingview.com/pine-script-docs/",
    "https://www.tradingview.com/pine-script-docs/welcome/",
    "https://www.tradingview.com/pine-script-docs/release-notes/",
    "https://www.tradingview.com/pine-script-reference/v6/",
)

TAG_TOKENS = (
    "alerts", "arrays", "bar states", "boxes", "chart points", "colors", "debugging",
    "drawings", "execution model", "fills", "inputs", "libraries", "lines", "maps",
    "matrices", "methods", "objects", "operators", "plots", "repainting", "sessions",
    "strategies", "tables", "time", "types",
)

SKIP_EXTENSIONS = (
    ".7z", ".avi", ".css", ".csv", ".doc", ".docx", ".gif", ".gz", ".ico",
    ".jpg", ".jpeg", ".js", ".json", ".mp3", ".mp4", ".pdf", ".png", ".svg",
    ".tar", ".webp", ".xls", ".xlsx", ".zip",
)

BLOCK_TAGS = {"h1", "h2", "h3", "h4", "p", "li", "pre", "table"}
SKIP_TAGS = {"nav", "footer", "aside", "script", "style", "noscript", "form"}


class PineDocsHTMLParser(HTMLParser):
    """Small docs-oriented HTML extractor for links, title, and content blocks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.title_parts: list[str] = []
        self.canonical_url = ""
        self.in_title = False
        self.main_depth = 0
        self.body_depth = 0
        self.skip_depth = 0
        self.current_tag = ""
        self.current_parts: list[str] = []
        self.blocks: list[tuple[str, str]] = []

    @property
    def in_content(self) -> bool:
        return (self.main_depth > 0 or self.body_depth > 0) and self.skip_depth == 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key.lower(): value or "" for key, value in attrs}
        if tag == "a" and attrs_dict.get("href"):
            self.links.append(attrs_dict["href"])
        if tag == "link" and attrs_dict.get("rel") == "canonical" and attrs_dict.get("href"):
            self.canonical_url = attrs_dict["href"]
        if tag == "title":
            self.in_title = True
        if tag == "main":
            self.main_depth += 1
        if tag == "body":
            self.body_depth += 1
        if tag in SKIP_TAGS and self.in_content:
            self.skip_depth += 1
        if tag in BLOCK_TAGS and self.in_content and not self.current_tag:
            self.current_tag = tag
            self.current_parts = []
        if tag in {"br", "tr"} and self.current_tag:
            self.current_parts.append("\n")
        if tag in {"td", "th"} and self.current_tag == "table":
            self.current_parts.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if self.current_tag == tag:
            text = clean_text("".join(self.current_parts))
            if text:
                self.blocks.append((tag, text))
            self.current_tag = ""
            self.current_parts = []
        if tag in SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        if tag == "main" and self.main_depth:
            self.main_depth -= 1
        if tag == "body" and self.body_depth:
            self.body_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
        if self.current_tag and self.in_content:
            self.current_parts.append(data)


def canonicalize(url: str) -> str:
    parsed = urlparse(url.strip())
    return urlunparse(parsed._replace(fragment="", query="")).rstrip("/") + ("/" if urlparse(url.strip()).path.endswith("/") else "")


def allowed(url: str) -> bool:
    url = canonicalize(url)
    lower_path = urlparse(url).path.lower()
    if lower_path.endswith(SKIP_EXTENSIONS):
        return False
    return any(url.startswith(prefix) for prefix in BASE_ALLOWED)


def url_to_id(url: str) -> str:
    return hashlib.sha1(canonicalize(url).encode("utf-8")).hexdigest()[:16]


def clean_text(value: str) -> str:
    value = html.unescape(value)
    value = re.sub(r"\r", "\n", value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r" ?\| ?\n", "\n", value)
    value = re.sub(r"\n[ \t]+", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def fetch(url: str, headers: dict[str, str], timeout: int) -> str:
    request = Request(url, headers=headers)
    with urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def parse_html(html_text: str) -> PineDocsHTMLParser:
    parser = PineDocsHTMLParser()
    parser.feed(html_text)
    parser.close()
    return parser


def extract_links(html_text: str, base_url: str) -> list[str]:
    parser = parse_html(html_text)
    links = {canonicalize(urljoin(base_url, href)) for href in parser.links if href and not href.startswith(("mailto:", "tel:", "javascript:"))}
    return sorted(link for link in links if allowed(link))


def detect_content_type(url: str) -> tuple[str, str, str]:
    if "/pine-script-reference/v6/" in url:
        return "tradingview_reference", "v6", "reference_entry"
    if "/migration-guides/" in url:
        return "tradingview_manual", "v6", "migration_guide"
    if "/release-notes/" in url:
        return "tradingview_manual", "v6", "release_note"
    return "tradingview_manual", "v6", "manual_page"


def parse_page(url: str, html_text: str) -> dict:
    parser = parse_html(html_text)
    title = clean_text(" ".join(parser.title_parts)) or url
    canonical_url = canonicalize(urljoin(url, parser.canonical_url)) if parser.canonical_url else canonicalize(url)
    headings: list[dict[str, str]] = []
    content_parts: list[str] = []
    code_blocks: list[str] = []

    for tag, text in parser.blocks:
        if tag in {"h1", "h2", "h3", "h4"}:
            headings.append({"level": tag, "text": text})
            content_parts.append(f"\n\n{text}\n")
        elif tag == "pre":
            code_blocks.append(text)
            content_parts.append(f"\n```pine\n{text}\n```\n")
        elif tag == "li":
            content_parts.append(f"- {text}")
        else:
            content_parts.append(text)

    source, version, content_type = detect_content_type(canonical_url)
    return {
        "url": canonical_url,
        "fetched_url": canonicalize(url),
        "title": title,
        "source": source,
        "version": version,
        "content_type": content_type,
        "section_path": " > ".join(item["text"] for item in headings[:4]),
        "headings": headings,
        "code_blocks": code_blocks,
        "text": clean_text("\n".join(content_parts)),
    }


def split_sections(text: str) -> list[str]:
    blocks = re.split(r"\n(?=[A-Z][^\n]{0,120}\n)", text)
    return [block.strip() for block in blocks if block.strip()]


def chunk_text(text: str, max_chars: int, overlap: int) -> list[str]:
    text = clean_text(text)
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        if end < len(text):
            candidate = text[start:end]
            cut = max(candidate.rfind("\n\n"), candidate.rfind(". "))
            if cut > max_chars * 0.6:
                end = start + cut + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(0, end - overlap)
    return chunks


def tags_for(page: dict, chunk: str) -> list[str]:
    haystack = " ".join([page["url"], page["title"], page["section_path"], chunk]).lower()
    return sorted({token.replace(" ", "_") for token in TAG_TOKENS if token in haystack})


def build_chunks(page: dict, max_chars: int, overlap: int) -> list[dict]:
    sections = split_sections(page["text"]) or [page["text"]]
    chunks: list[dict] = []
    chunk_counter = 0
    for section in sections:
        for chunk in chunk_text(section, max_chars=max_chars, overlap=overlap):
            chunks.append({
                "doc_id": f"{page['version']}_{page['content_type']}_{url_to_id(page['url'])}_{chunk_counter:04d}",
                "source": page["source"],
                "url": page["url"],
                "title": page["title"],
                "section_path": page["section_path"],
                "version": page["version"],
                "content_type": page["content_type"],
                "chunk_index": chunk_counter,
                "text": chunk,
                "code_blocks": page["code_blocks"][:5],
                "headings": page["headings"][:8],
                "tags": tags_for(page, chunk),
                "char_count": len(chunk),
                "embedding_text": clean_text(f"{page['title']}\n{page['section_path']}\n{chunk}"),
            })
            chunk_counter += 1
    return chunks


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_manifest(path: Path, rows: list[dict]) -> None:
    fieldnames = ["doc_id", "title", "url", "source", "version", "content_type", "chunk_index", "char_count", "tags"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({**{key: row[key] for key in fieldnames if key != "tags"}, "tags": ",".join(row["tags"])})


def crawl(args: argparse.Namespace) -> dict:
    outdir = Path(args.outdir)
    raw_dir = outdir / "raw_pages"
    norm_dir = outdir / "normalized_pages"
    chunk_dir = outdir / "chunks"
    for directory in (raw_dir, norm_dir, chunk_dir):
        directory.mkdir(parents=True, exist_ok=True)

    headers = {"User-Agent": args.user_agent}
    queue: deque[str] = deque(args.seed_url)
    visited: set[str] = set()
    raw_index: list[dict] = []
    page_records: list[dict] = []
    all_chunks: list[dict] = []
    failed_fetches: list[dict] = []

    while queue:
        url = canonicalize(queue.popleft())
        if url in visited or not allowed(url):
            continue
        if args.max_pages and len(page_records) >= args.max_pages:
            break
        visited.add(url)
        print(f"fetch {url}", flush=True)
        try:
            html_text = fetch(url, headers=headers, timeout=args.timeout)
        except Exception as exc:
            failed_fetches.append({"url": url, "error": str(exc)})
            print(f"skip {url}: {exc}", flush=True)
            continue
        uid = url_to_id(url)
        raw_path = raw_dir / f"{uid}.html"
        raw_path.write_text(html_text, encoding="utf-8")
        raw_index.append({"url": url, "raw_file": str(raw_path)})

        for link in extract_links(html_text, url):
            if link not in visited:
                queue.append(link)

        page = parse_page(url, html_text)
        page_records.append(page)
        write_json(norm_dir / f"{uid}.json", page)
        page_chunks = build_chunks(page, max_chars=args.max_chars, overlap=args.overlap)
        all_chunks.extend(page_chunks)
        write_jsonl(chunk_dir / f"{uid}.jsonl", page_chunks)
        time.sleep(args.delay)

    write_json(outdir / "raw_index.json", raw_index)
    write_json(outdir / "failed_fetches.json", failed_fetches)
    write_jsonl(outdir / "pine_v6_rag_dataset.jsonl", all_chunks)
    write_manifest(outdir / "pine_v6_rag_manifest.csv", all_chunks)
    summary = {"pages": len(page_records), "chunks": len(all_chunks), "failed_fetches": len(failed_fetches), "jsonl": str(outdir / "pine_v6_rag_dataset.jsonl"), "manifest": str(outdir / "pine_v6_rag_manifest.csv")}
    write_json(outdir / "build_summary.json", summary)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outdir", default="output", help="Directory for raw, normalized, chunk, JSONL, and manifest outputs.")
    parser.add_argument("--seed-url", action="append", default=list(DEFAULT_SEED_URLS), help="Seed URL to crawl. May be repeated.")
    parser.add_argument("--max-pages", type=int, default=0, help="Optional page cap for smoke tests; 0 means no cap.")
    parser.add_argument("--max-chars", type=int, default=1800, help="Maximum characters per retrieval chunk.")
    parser.add_argument("--overlap", type=int, default=250, help="Character overlap between adjacent chunks.")
    parser.add_argument("--delay", type=float, default=0.8, help="Polite delay between requests, in seconds.")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds.")
    parser.add_argument("--user-agent", default="Mozilla/5.0 (compatible; pine-v6-rag-builder/1.0; +https://example.com)", help="User-Agent header for TradingView requests.")
    return parser.parse_args()


def main() -> None:
    print(json.dumps(crawl(parse_args()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
