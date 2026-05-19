#!/usr/bin/env python3
"""Pipeline images de la doc Odoo (Phase 4 — scénario A).

Pour chaque page HTML doc Odoo ingérée :
  1. Parse le HTML → liste d'images candidates (avec alt-text + caption + src
     absolu).
  2. Filtre : exclut SVG/GIF/ICO et les images trop petites (min(w,h) < 200 px,
     d'après les attrs HTML quand elles sont présentes — sinon décide après
     téléchargement).
  3. Télécharge l'image (avec throttle), la convertit en WebP via Pillow,
     calcule le hash MD5 du contenu canonique. Dédup par hash : si l'image
     existe déjà localement, on réutilise le fichier.
  4. Stocke le fichier sous
     `static/doc_media/{version}/{module}/{hash[:2]}/{hash}.webp` (sharding
     par 2 premiers chars du hash pour ne pas exploser le nombre de fichiers
     par dossier).
  5. INSERT OR IGNORE dans `doc_images`.

Le linkage chunk ↔ image est fait au moment de l'ingestion : toutes les images
d'une page sont associées à tous les chunks issus de cette page (approche
pragmatique — affinable plus tard).

Utilisation typique :
    from app.doc_images import extract_image_refs, download_and_store

    refs = extract_image_refs(html, page_url=u, version="19.0", module="hr")
    for ref in refs:
        rec = download_and_store(conn, ref, throttle_s=1.0)
        # rec is None si l'image a été rejetée (taille / fetch KO / format).
"""

from __future__ import annotations

import hashlib
import io
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MEDIA_ROOT_REL = "static/doc_media"
MEDIA_ROOT = ROOT / MEDIA_ROOT_REL

MIN_SIDE_PX = 200            # Filtre min(width, height) avant download (si dispo).
MIN_SIDE_AFTER_DECODE = 200  # Filtre après décodage Pillow.
SKIP_EXTENSIONS = (".svg", ".gif", ".ico")
WEBP_QUALITY = 85
USER_AGENT = "odoo-quiz-doc-ingest/1.0 (+local study bot)"


@dataclass
class ImageRef:
    """Image candidate extraite du HTML d'une page doc Odoo."""

    source_url: str
    alt_text: str
    caption: str
    width_hint: int | None  # attr HTML width si présent
    height_hint: int | None
    page_url: str
    version: str
    module: str
    position: int           # ordre d'apparition dans la page


@dataclass
class StoredImage:
    """Résultat du download+conversion+stockage d'une image."""

    image_id: str       # hash MD5 hex du contenu WebP
    local_path: str     # chemin relatif à ROOT
    width: int
    height: int
    bytes: int


def extract_image_refs(
    html: str,
    *,
    page_url: str,
    version: str,
    module: str,
) -> list[ImageRef]:
    """Parse HTML et retourne les images candidates (avec filtre HTML basique)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    # Réduit le bruit : on cherche les images dans la zone contenu principal.
    container = None
    for sel in (
        "article.o_doc_content",
        "div[role=main] article",
        "div[role=main]",
        "article",
        "main",
    ):
        node = soup.select_one(sel)
        if node is not None:
            container = node
            break
    if container is None:
        container = soup

    refs: list[ImageRef] = []
    seen_sources: set[str] = set()
    for i, img in enumerate(container.find_all("img")):
        src = (img.get("src") or img.get("data-src") or "").strip()
        if not src:
            continue
        abs_src = urllib.parse.urljoin(page_url, src)
        low = abs_src.lower().split("?", 1)[0]
        if any(low.endswith(ext) for ext in SKIP_EXTENSIONS):
            continue
        if abs_src in seen_sources:
            continue
        seen_sources.add(abs_src)

        alt = (img.get("alt") or "").strip()
        # Caption : si l'image est dans une <figure> avec <figcaption>.
        caption = ""
        parent = img.find_parent("figure")
        if parent:
            cap = parent.find("figcaption")
            if cap:
                caption = cap.get_text(" ", strip=True)

        width_hint = _parse_dim(img.get("width"))
        height_hint = _parse_dim(img.get("height"))
        # Heuristique : si les deux dimensions sont annoncées et trop petites,
        # skip avant download (gain temps + bande passante).
        if width_hint is not None and height_hint is not None:
            if min(width_hint, height_hint) < MIN_SIDE_PX:
                continue

        refs.append(
            ImageRef(
                source_url=abs_src,
                alt_text=alt,
                caption=caption,
                width_hint=width_hint,
                height_hint=height_hint,
                page_url=page_url,
                version=version,
                module=module,
                position=i,
            )
        )
    return refs


def _parse_dim(raw: str | None) -> int | None:
    if not raw:
        return None
    m = re.match(r"\s*(\d+)", str(raw))
    return int(m.group(1)) if m else None


def _fetch_bytes(url: str, *, timeout_s: float = 60.0) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.read()
    except Exception:
        return None


def _md5_hex(data: bytes) -> str:
    h = hashlib.md5()
    h.update(data)
    return h.hexdigest()


def _local_path_for(version: str, module: str, image_id: str) -> Path:
    """Retourne le chemin absolu de destination pour une image donnée."""
    safe_module = (module or "_misc").replace("/", "__")
    sub = image_id[:2]
    return MEDIA_ROOT / version / safe_module / sub / f"{image_id}.webp"


def _to_webp(raw: bytes) -> tuple[bytes, int, int] | None:
    """Décode et convertit en WebP. Retourne (bytes_webp, width, height) ou None."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(raw)) as im:
            im.load()
            width, height = im.size
            if min(width, height) < MIN_SIDE_AFTER_DECODE:
                return None
            # Conversion d'orientation EXIF si présente.
            try:
                from PIL import ImageOps

                im = ImageOps.exif_transpose(im)
                width, height = im.size
            except Exception:
                pass
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGB")
            out = io.BytesIO()
            im.save(out, format="WEBP", quality=WEBP_QUALITY, method=4)
            return out.getvalue(), width, height
    except Exception:
        return None


def download_and_store(
    conn: sqlite3.Connection,
    ref: ImageRef,
    *,
    throttle_s: float = 0.0,
) -> StoredImage | None:
    """Télécharge ref.source_url, convertit, dédup et stocke.

    Retourne `StoredImage` ou None si l'image est rejetée (taille, format,
    fetch KO). Pré-vérifie le cache DB : si une `source_url` est déjà connue
    dans `doc_images`, on saute le download (idempotent).
    """
    row = conn.execute(
        "SELECT image_id, local_path, width, height, bytes FROM doc_images WHERE source_url = ?",
        (ref.source_url,),
    ).fetchone()
    if row:
        image_id, local_path, width, height, byts = row
        # Met à jour alt/caption si on en a une nouvelle (premier match) et
        # qu'on n'avait rien avant.
        if ref.alt_text or ref.caption:
            conn.execute(
                """
                UPDATE doc_images
                SET alt_text = CASE WHEN alt_text = '' THEN ? ELSE alt_text END,
                    caption  = CASE WHEN caption = '' THEN ? ELSE caption END
                WHERE image_id = ?
                """,
                (ref.alt_text, ref.caption, image_id),
            )
        return StoredImage(
            image_id=image_id, local_path=local_path,
            width=int(width or 0), height=int(height or 0), bytes=int(byts or 0),
        )

    if throttle_s > 0:
        time.sleep(throttle_s)
    raw = _fetch_bytes(ref.source_url)
    if raw is None:
        return None
    webp = _to_webp(raw)
    if webp is None:
        return None
    blob, width, height = webp
    image_id = _md5_hex(blob)

    # Vérifie qu'on n'a pas déjà ce hash (collision content-based dedup même si
    # ref.source_url est différente).
    existing = conn.execute(
        "SELECT local_path FROM doc_images WHERE image_id = ?", (image_id,)
    ).fetchone()
    if existing:
        local_rel = existing[0]
    else:
        dest = _local_path_for(ref.version, ref.module, image_id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)
        local_rel = str(dest.relative_to(ROOT))

    conn.execute(
        """
        INSERT OR IGNORE INTO doc_images (
            image_id, source_url, local_path, mime, width, height, bytes,
            alt_text, caption
        ) VALUES (?, ?, ?, 'image/webp', ?, ?, ?, ?, ?)
        """,
        (
            image_id, ref.source_url, local_rel, width, height, len(blob),
            ref.alt_text, ref.caption,
        ),
    )
    return StoredImage(
        image_id=image_id, local_path=local_rel,
        width=width, height=height, bytes=len(blob),
    )


def link_chunk_images(
    conn: sqlite3.Connection,
    chunk_ids: list[str],
    image_ids: list[str],
) -> int:
    """Associe chaque image à chaque chunk passé (relation N-N).

    Retourne le nombre de liens créés (en comptant uniquement les nouveaux —
    PRIMARY KEY assure l'idempotence).
    """
    n = 0
    for chunk_id in chunk_ids:
        for pos, image_id in enumerate(image_ids):
            cur = conn.execute(
                "INSERT OR IGNORE INTO chunk_images (chunk_id, image_id, position) "
                "VALUES (?, ?, ?)",
                (chunk_id, image_id, pos),
            )
            n += cur.rowcount
    return n


def images_for_chunk(
    conn: sqlite3.Connection, chunk_id: str,
) -> list[dict]:
    """Retourne les images associées à un chunk donné (ordre = position)."""
    rows = conn.execute(
        """
        SELECT i.image_id, i.local_path, i.alt_text, i.caption, i.width, i.height
        FROM chunk_images ci
        JOIN doc_images i ON i.image_id = ci.image_id
        WHERE ci.chunk_id = ?
        ORDER BY ci.position ASC
        """,
        (chunk_id,),
    ).fetchall()
    return [
        {
            "image_id": r[0],
            "local_path": r[1],
            "alt_text": r[2],
            "caption": r[3],
            "width": r[4],
            "height": r[5],
        }
        for r in rows
    ]


__all__ = [
    "ImageRef",
    "StoredImage",
    "MEDIA_ROOT_REL",
    "MEDIA_ROOT",
    "extract_image_refs",
    "download_and_store",
    "link_chunk_images",
    "images_for_chunk",
]
