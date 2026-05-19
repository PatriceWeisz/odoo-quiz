#!/usr/bin/env python3
"""Re-ingestion d'images pour les pages déjà en base (Phase 4.4).

Ne re-crawle PAS le texte (gain de temps & on garde les chunks existants).
Pour chaque page connue dans `pages` :
  1. Fetch HTML (throttle 1 req/s).
  2. Extrait les images (filtres taille).
  3. Télécharge + WebP + stocke (dédup par hash).
  4. Lie les images aux chunks existants de la page.

Usage :
  python3 -m scripts.reingest_images                       # toutes versions
  python3 -m scripts.reingest_images --version 19.0        # une version
  python3 -m scripts.reingest_images --limit 10            # test (10 pages)
  python3 -m scripts.reingest_images --modules inventory_and_mrp/inventory
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.doc_images import (  # noqa: E402
    download_and_store,
    extract_image_refs,
    link_chunk_images,
)
from app.odoo_docs_rag import db_path, init_db  # noqa: E402
from scripts.ingest_odoo_docs import _fetch, THROTTLE_S, top_level_module  # noqa: E402


def _pages_for_version(
    conn: sqlite3.Connection,
    version: str,
) -> list[tuple[str, list[str], str]]:
    """Retourne [(url, [chunk_ids], section), …] pour les pages ayant
    au moins 1 chunk dans `version`."""
    rows = conn.execute(
        """
        SELECT p.url, p.section
        FROM pages p
        WHERE p.url IN (SELECT DISTINCT url FROM chunks WHERE version = ?)
        ORDER BY p.url
        """,
        (version,),
    ).fetchall()
    out: list[tuple[str, list[str], str]] = []
    for url, section in rows:
        cids = [
            r[0] for r in conn.execute(
                "SELECT chunk_id FROM chunks WHERE url = ? AND version = ? "
                "ORDER BY chunk_id",
                (url, version),
            ).fetchall()
        ]
        if cids:
            out.append((url, cids, section or ""))
    return out


def _module_of(url: str, version: str) -> str:
    """Approxime top_level_module sans connaître la section (déduit du path)."""
    path = urllib.parse.urlparse(url).path
    needle = f"/documentation/{version}/applications/"
    if needle in path:
        rest = path.split(needle, 1)[1].split("/")
        if len(rest) >= 2:
            return f"{rest[0]}/{rest[1].replace('.html','')}"
        if rest:
            return rest[0].replace(".html", "")
    return "_misc"


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-ingestion images pour pages existantes")
    parser.add_argument(
        "--version",
        action="append",
        choices=["18.0", "19.0"],
        help="Restreindre à une version (répétable). Défaut : 18.0 et 19.0.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Traiter au plus N pages (par version) — utile pour tester.",
    )
    parser.add_argument(
        "--modules",
        default=None,
        help="Restreindre à un CSV de modules (ex: inventory_and_mrp/inventory).",
    )
    parser.add_argument(
        "--no-throttle",
        action="store_true",
        help="Désactive le throttle 1 req/s (à utiliser avec précaution).",
    )
    args = parser.parse_args()

    throttle = 0.0 if args.no_throttle else THROTTLE_S
    versions = args.version or ["18.0", "19.0"]
    wanted_modules = (
        {m.strip() for m in args.modules.split(",") if m.strip()}
        if args.modules else None
    )

    db = db_path()
    if not db.exists():
        print(f"❌ Base introuvable : {db}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(db)
    init_db(conn)  # idempotent — crée doc_images & chunk_images si absents

    grand_total_pages = 0
    grand_total_imgs_new = 0
    grand_total_imgs_skipped = 0
    grand_total_links = 0

    for ver in versions:
        pages = _pages_for_version(conn, ver)
        if wanted_modules:
            pages = [(u, cids, s) for (u, cids, s) in pages
                     if _module_of(u, ver) in wanted_modules]
        if args.limit is not None and args.limit > 0:
            pages = pages[: args.limit]
        print(f"\n=== Version {ver} : {len(pages)} pages à traiter ===")

        for idx, (url, chunk_ids, section) in enumerate(pages, 1):
            module = _module_of(url, ver)
            if throttle > 0:
                time.sleep(throttle)
            raw = _fetch(url)
            if raw is None:
                print(f"  ! fetch KO : {url}")
                continue
            try:
                html = raw.decode("utf-8", errors="replace")
            except Exception:
                continue
            try:
                refs = extract_image_refs(html, page_url=url, version=ver, module=module)
            except Exception as e:
                print(f"  ! parse KO {url}: {e}")
                refs = []

            page_imgs_new = 0
            page_imgs_skip = 0
            page_image_ids: list[str] = []
            for ref in refs:
                stored = download_and_store(conn, ref, throttle_s=throttle)
                if stored is None:
                    page_imgs_skip += 1
                    continue
                page_image_ids.append(stored.image_id)
                page_imgs_new += 1

            links_added = 0
            if chunk_ids and page_image_ids:
                links_added = link_chunk_images(conn, chunk_ids, page_image_ids)
            conn.commit()

            grand_total_pages += 1
            grand_total_imgs_new += page_imgs_new
            grand_total_imgs_skipped += page_imgs_skip
            grand_total_links += links_added

            print(
                f"[{idx}/{len(pages)}] {url}  "
                f"→ {page_imgs_new} img, {page_imgs_skip} skip, {links_added} liens"
            )

    print()
    print(f"=== TOTAL ===")
    print(f"  Pages traitées       : {grand_total_pages}")
    print(f"  Images stockées/réutilisées : {grand_total_imgs_new}")
    print(f"  Images skipped       : {grand_total_imgs_skipped}")
    print(f"  Liens chunk↔image    : {grand_total_links}")

    n_images = conn.execute("SELECT COUNT(*) FROM doc_images").fetchone()[0]
    n_links = conn.execute("SELECT COUNT(*) FROM chunk_images").fetchone()[0]
    print(f"  doc_images total en base : {n_images}")
    print(f"  chunk_images total en base : {n_links}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
