#!/usr/bin/env python3
"""
Ingestion documentation Odoo → data/odoo_docs.sqlite

Source d'URLs : sitemap.xml (prioritaire), repli searchindex.js si 404.
Throttle : 1 requête/s. Filtre --section (applications par défaut).
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

THROTTLE_S = 1.0
CHUNK_TOKENS = 600
OVERLAP_TOKENS = 80
DEFAULT_VERSION = "18.0"
SECTION_CHOICES = ("applications", "developer", "administration", "all")
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
_SKIP_DOC_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".pdf", ".zip")

_CONTENT_SELECTORS = (
    "article.o_doc_content",
    "div.document",
    "main article",
    "div[role=main] article",
    "div[role=main]",
    "article",
)
_STRIP_SELECTORS = (
    "nav",
    "header",
    "footer",
    ".o_side_nav",
    ".navbar",
    ".o_on_this_page",
    "script",
    "style",
    "noscript",
)


def doc_prefix(version: str) -> str:
    return f"/documentation/{version.strip('/')}/"


def base_url(version: str) -> str:
    return f"https://www.odoo.com{doc_prefix(version)}"


def sitemap_url(version: str) -> str:
    return f"{base_url(version)}sitemap.xml"


def _load_cfg_db_path() -> Path:
    from app.odoo_docs_rag import db_path

    return db_path()


def _fetch(url: str, timeout: float = 60.0) -> bytes | None:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "odoo-quiz-doc-ingest/1.0 (+local study bot)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as exc:
        print(f"  ! fetch échoué {url}: {exc}", file=sys.stderr)
        return None


def _normalize_doc_url(url: str, version: str) -> str | None:
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.netloc and parsed.netloc not in ("www.odoo.com", "odoo.com"):
        return None
    path = parsed.path.split("#")[0].split("?")[0]
    prefix = doc_prefix(version)
    if not path.startswith(prefix):
        return None
    # Exclure locales (/documentation/19.0/fr/...)
    rest = path[len(prefix) :]
    if re.match(r"^[a-z]{2}(_[A-Z]{2})?/", rest):
        return None
    low = path.lower()
    if any(low.endswith(ext) for ext in _SKIP_DOC_SUFFIXES):
        return None
    if not path.endswith(".html"):
        if path.endswith("/"):
            path = path + "index.html"
        else:
            path = path + ".html"
    return urllib.parse.urlunparse(("https", "www.odoo.com", path, "", "", ""))


def _parse_sitemap_xml(xml_bytes: bytes, version: str) -> list[str]:
    """Extrait les URLs doc depuis un urlset ou suit un sitemapindex."""
    root = ET.fromstring(xml_bytes)
    tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag
    urls: list[str] = []

    if tag == "sitemapindex":
        for sm in root.findall("sm:sitemap", SITEMAP_NS) + root.findall("sitemap"):
            loc = sm.find("sm:loc", SITEMAP_NS) or sm.find("loc")
            if loc is None or not (loc.text or "").strip():
                continue
            child_url = loc.text.strip()
            time.sleep(THROTTLE_S)
            child_raw = _fetch(child_url)
            if child_raw:
                urls.extend(_parse_sitemap_xml(child_raw, version))
        return urls

    for url_el in root.findall("sm:url", SITEMAP_NS) + root.findall("url"):
        loc = url_el.find("sm:loc", SITEMAP_NS) or url_el.find("loc")
        if loc is None or not (loc.text or "").strip():
            continue
        norm = _normalize_doc_url(loc.text.strip(), version)
        if norm:
            urls.append(norm)
    return urls


def _urls_from_sitemap(version: str, *, sitemap_override: str | None = None) -> list[str]:
    url = sitemap_override or sitemap_url(version)
    raw = _fetch(url)
    if raw is None:
        return []
    try:
        return _parse_sitemap_xml(raw, version)
    except ET.ParseError as exc:
        print(f"  ! sitemap XML invalide ({url}): {exc}", file=sys.stderr)
        return []


def _urls_from_searchindex(version: str) -> list[str]:
    """Repli Sphinx : docnames dans searchindex.js → URLs .html."""
    idx_url = f"{base_url(version)}searchindex.js"
    raw = _fetch(idx_url)
    if raw is None:
        return []
    text = raw.decode("utf-8", errors="replace")
    m = re.search(r"docnames\s*:\s*\[(.*?)\]\s*,\s*envversion", text, re.DOTALL)
    if not m:
        return []
    names = re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))
    prefix = base_url(version)
    out: list[str] = []
    for name in names:
        if not name or name.startswith("_"):
            continue
        norm = _normalize_doc_url(f"{prefix}{name}.html", version)
        if norm:
            out.append(norm)
    return sorted(set(out))


def discover_urls(
    version: str,
    *,
    sitemap_override: str | None = None,
) -> tuple[list[str], str]:
    """
    Retourne (urls, source_label).
    Essaie sitemap.xml puis searchindex.js.
    """
    urls = _urls_from_sitemap(version, sitemap_override=sitemap_override)
    if urls:
        return sorted(set(urls)), "sitemap.xml"
    sm_url = sitemap_override or sitemap_url(version)
    print(
        f"⚠ {sm_url} indisponible ou vide — repli sur searchindex.js",
        file=sys.stderr,
    )
    urls = _urls_from_searchindex(version)
    return sorted(set(urls)), "searchindex.js"


def filter_by_modules(urls: list[str], modules_csv: str | None, version: str) -> list[str]:
    """Filtre URLs dont le chemin applications correspond à un module cert (liste CSV)."""
    if not modules_csv or not modules_csv.strip():
        return urls
    wanted = {m.strip() for m in modules_csv.split(",") if m.strip()}
    if not wanted:
        return urls
    from scripts.sitemap_inventory import assign_module

    return [u for u in urls if assign_module(u, version) in wanted]


def filter_by_section(urls: list[str], section: str, version: str) -> list[str]:
    if section == "all":
        return urls
    prefix = doc_prefix(version) + section + "/"
    section_index = doc_prefix(version) + section + ".html"
    return [u for u in urls if u.startswith(f"https://www.odoo.com{prefix}") or u.endswith(section_index)]


def top_level_module(url: str, section: str, version: str) -> str:
    """Premier segment après applications|developer|administration."""
    path = urllib.parse.urlparse(url).path
    parts = [p for p in path.split("/") if p]
    ver_parts = doc_prefix(version).strip("/").split("/")
    # documentation, 19.0, section, module, ...
    try:
        i = parts.index(ver_parts[-1])  # version token
        if section != "all" and i + 2 < len(parts):
            return parts[i + 2].replace(".html", "")
        if section == "all" and i + 2 < len(parts):
            return f"{parts[i + 1]}/{parts[i + 2]}".replace(".html", "")
    except (ValueError, IndexError):
        pass
    return "(racine)"


def _extract_page(html: bytes, url: str) -> tuple[str, str, str]:
    soup = BeautifulSoup(html, "html.parser")
    for sel in _STRIP_SELECTORS:
        for el in soup.select(sel):
            el.decompose()
    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(" ", strip=True)
    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True)
    section = ""
    crumb = soup.select_one(".breadcrumb, nav.breadcrumb, ol.breadcrumb")
    if crumb:
        section = crumb.get_text(" › ", strip=True)
    body_el = None
    for sel in _CONTENT_SELECTORS:
        body_el = soup.select_one(sel)
        if body_el:
            break
    if body_el is None:
        body_el = soup.body or soup
    text = body_el.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return title, section, text


def _chunk_text(text: str) -> list[str]:
    try:
        import tiktoken
    except ImportError:
        raise SystemExit("tiktoken requis : pip install tiktoken") from None
    enc = tiktoken.get_encoding("cl100k_base")
    tokens = enc.encode(text)
    if not tokens:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(tokens):
        end = min(start + CHUNK_TOKENS, len(tokens))
        piece = enc.decode(tokens[start:end]).strip()
        if piece:
            chunks.append(piece)
        if end >= len(tokens):
            break
        start = max(0, end - OVERLAP_TOKENS)
    return chunks


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _chunk_id(version: str, url: str, index: int) -> str:
    """
    Identifiant stable et unique par version (ex. 19.0__sales__crm__chunk_2).
    Pas de collision entre v18 et v19 pour la même page relative.
    """
    path = urllib.parse.urlparse(url).path
    prefix = doc_prefix(version)
    rel = path[len(prefix) :].replace(".html", "").strip("/")
    safe = re.sub(r"[^a-zA-Z0-9]+", "_", rel).strip("_") or "index"
    safe = re.sub(r"_+", "_", safe)
    return f"{version}__{safe}__chunk_{index}"


def _print_module_table(module_chunks: Counter[str]) -> None:
    if not module_chunks:
        return
    print("\n── Chunks par module (top-level) ──")
    name_w = max(len(k) for k in module_chunks)
    name_w = max(name_w, len("module"))
    print(f"{'module':<{name_w}}  chunks")
    print(f"{'-' * name_w}  ------")
    for mod, n in module_chunks.most_common():
        print(f"{mod:<{name_w}}  {n}")
    print(f"{'TOTAL':<{name_w}}  {sum(module_chunks.values())}")


def ingest(
    *,
    version: str,
    section: str,
    dry_run: bool = False,
    sitemap_override: str | None = None,
    limit: int | None = None,
    modules: str | None = None,
) -> int:
    from app.doc_schema import normalize_doc_version

    version = normalize_doc_version(version)
    all_urls, source = discover_urls(version, sitemap_override=sitemap_override)
    urls = filter_by_section(all_urls, section, version)
    urls = filter_by_modules(urls, modules, version)
    if limit is not None and limit > 0:
        urls = urls[:limit]

    print(f"Source : {source}")
    print(f"Version : {version} · section : {section}")
    print(f"URLs retenues : {len(urls)} / {len(all_urls)} dans le catalogue\n")

    if dry_run:
        for u in urls:
            print(u)
        print(f"\nDry-run : {len(urls)} URL(s) seraient traitées (aucun fetch).")
        by_mod: Counter[str] = Counter()
        for u in urls:
            by_mod[top_level_module(u, section, version)] += 1
        print("\n(Pages par module — estimation 1 page = 1 entrée)")
        _print_module_table(by_mod)
        return 0

    from app.odoo_docs_rag import init_db, invalidate_doc_index_cache, store_chunk
    from bank_embeddings import embed_texts

    db = _load_cfg_db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    init_db(conn)

    pages_done = 0
    pages_skipped = 0
    module_chunks: Counter[str] = Counter()

    for url in urls:
        time.sleep(THROTTLE_S)
        raw = _fetch(url)
        if raw is None:
            pages_skipped += 1
            continue
        title, section_breadcrumb, text = _extract_page(raw, url)
        if len(text) < 80:
            pages_skipped += 1
            continue
        chash = _content_hash(text)
        mod = top_level_module(url, section, version)
        pages_done += 1
        print(f"[{pages_done}/{len(urls)}] {url} ({len(text)} chars)")

        row = conn.execute("SELECT content_hash FROM pages WHERE url = ?", (url,)).fetchone()
        if row and row[0] == chash:
            n_existing = conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE url = ? AND version = ?",
                (url, version),
            ).fetchone()[0]
            module_chunks[mod] += int(n_existing or 0)
            continue

        conn.execute("DELETE FROM chunks WHERE url = ? AND version = ?", (url, version))
        conn.execute(
            """
            INSERT OR REPLACE INTO pages (url, title, section, content_hash, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            """,
            (url, title, section_breadcrumb, chash),
        )
        parts = _chunk_text(text)
        if not parts:
            conn.commit()
            continue
        vecs = embed_texts(parts)
        if vecs is None:
            print("  ! embeddings indisponibles", file=sys.stderr)
            conn.commit()
            continue
        for i, (part, vec) in enumerate(zip(parts, vecs)):
            store_chunk(
                conn,
                chunk_id=_chunk_id(version, url, i),
                url=url,
                title=title,
                section=section_breadcrumb,
                text=part,
                embedding=vec,
                version=version,
            )
            module_chunks[mod] += 1
        conn.commit()

    n = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    n_ver = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE version = ?", (version,)
    ).fetchone()[0]
    by_ver = dict(
        conn.execute(
            "SELECT version, COUNT(*) FROM chunks GROUP BY version ORDER BY version"
        ).fetchall()
    )
    conn.close()
    invalidate_doc_index_cache()

    print(
        f"\nTerminé : {pages_done} pages ingérées, {pages_skipped} ignorées, "
        f"{n_ver} chunks v{version} ({n} total en base, {db})"
    )
    print("Chunks par version :", ", ".join(f"{k}={v}" for k, v in by_ver.items()))
    _print_module_table(module_chunks)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingestion doc Odoo (sitemap + section)")
    parser.add_argument(
        "--version",
        default=DEFAULT_VERSION,
        help=f"Version doc Odoo (défaut : {DEFAULT_VERSION})",
    )
    parser.add_argument(
        "--section",
        choices=SECTION_CHOICES,
        default="applications",
        help="Section à ingérer (défaut : applications)",
    )
    parser.add_argument(
        "--sitemap-url",
        default=None,
        help="URL sitemap (défaut : …/documentation/<version>/sitemap.xml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Lister les URLs sans fetch ni écriture SQLite",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Traiter au plus N URLs (tests, ex. 50 avant full crawl)",
    )
    parser.add_argument(
        "--modules",
        default=None,
        help="Re-ingestion partielle : modules cert CSV (ex. inventory_and_mrp/inventory,productivity/ai)",
    )
    args = parser.parse_args()
    return ingest(
        version=args.version.strip(),
        section=args.section,
        dry_run=args.dry_run,
        sitemap_override=args.sitemap_url,
        limit=args.limit,
        modules=args.modules,
    )


if __name__ == "__main__":
    raise SystemExit(main())
