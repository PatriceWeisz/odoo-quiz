#!/usr/bin/env python3
"""Recadrage et enregistrement des images de question (Udemy / capture)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent
MEDIA_DIR = ROOT / "static" / "question_media"
MAX_EDGE = 1100
WEBP_QUALITY = 82


def ensure_media_dir() -> None:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)


def media_relative_path(qid: int) -> str:
    return f"question_media/q_{int(qid)}.webp"


def absolute_static_path(rel: str) -> Path:
    return ROOT / "static" / rel.replace("\\", "/").lstrip("/")


def normalize_question_media_rel(raw: Any) -> str:
    """Retourne un chemin relatif à ``static/`` (ex. ``question_media/q_12.webp``), ou ``''`` si invalide."""
    if raw is None:
        return ""
    s = str(raw).strip().replace("\\", "/")
    if not s or ".." in s or ":" in s:
        return ""
    low = s.lower()
    if low.startswith("/static/"):
        s = s[len("/static/") :]
    elif low.startswith("static/"):
        s = s[7:]
    s = s.lstrip("/")
    if not s.startswith("question_media/"):
        return ""
    return s


def has_valid_question_image(q: dict) -> bool:
    rel = normalize_question_media_rel(q.get("question_image"))
    if not rel:
        return False
    p = absolute_static_path(rel)
    try:
        return p.is_file() and p.resolve().is_relative_to((ROOT / "static" / "question_media").resolve())
    except (OSError, ValueError):
        return p.is_file()


def _normalize_crop_rel(crop: dict | None) -> dict | None:
    if not crop or not isinstance(crop, dict):
        return None
    try:
        left = float(crop.get("left"))
        top = float(crop.get("top"))
        width = float(crop.get("width"))
        height = float(crop.get("height"))
    except (TypeError, ValueError):
        return None
    for v in (left, top, width, height):
        if v != v:  # NaN
            return None
    left, top = max(0.0, min(1.0, left)), max(0.0, min(1.0, top))
    width = max(0.0, min(1.0, width))
    height = max(0.0, min(1.0, height))
    if width < 0.02 or height < 0.02:
        return None
    if left + width > 1.001:
        width = max(0.02, 1.0 - left)
    if top + height > 1.001:
        height = max(0.02, 1.0 - top)
    return {"left": left, "top": top, "width": width, "height": height}


def save_question_image_from_screenshot(
    screenshot_path: str,
    qid: int,
    crop_rel: dict | None,
    needs_question_image: bool,
) -> str:
    """Découpe (ou image entière) → WebP sous static/question_media/. Retourne chemin relatif à static/."""
    if not needs_question_image:
        return ""

    from PIL import Image

    ensure_media_dir()
    rel_out = media_relative_path(qid)
    dest = absolute_static_path(rel_out)

    box = _normalize_crop_rel(crop_rel)
    with Image.open(screenshot_path) as im:
        im = im.convert("RGBA")
        w, h = im.size
        if w < 8 or h < 8:
            return ""
        if box:
            l = int(box["left"] * w)
            t = int(box["top"] * h)
            r = int(min(w, (box["left"] + box["width"]) * w))
            b = int(min(h, (box["top"] + box["height"]) * h))
            l, t = max(0, l), max(0, t)
            r, b = max(l + 4, r), max(t + 4, b)
            crop_im = im.crop((l, t, r, b))
        else:
            crop_im = im

        crop_im = _downscale_if_needed(crop_im, MAX_EDGE)
        rgb = Image.new("RGB", crop_im.size, (255, 255, 255))
        alpha = crop_im.split()[3] if crop_im.mode == "RGBA" else None
        rgb.paste(crop_im.convert("RGBA"), mask=alpha)
        rgb.save(dest, "WEBP", quality=WEBP_QUALITY, method=4)

    return rel_out


def _downscale_if_needed(im, max_edge: int):
    from PIL import Image

    w, h = im.size
    m = max(w, h)
    if m <= max_edge:
        return im
    scale = max_edge / float(m)
    nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
    return im.resize((nw, nh), Image.Resampling.LANCZOS)


def crop_region_to_temp_png(
    screenshot_path: str,
    crop_rel: dict | None,
    *,
    pad: float = 0.04,
    max_edge: int = 1400,
) -> str | None:
    """Découpe la région ``crop_rel`` (coordonnées relatives 0..1) d'une capture et
    l'enregistre dans un PNG temporaire (avec une petite marge de contexte).

    Sert à n'envoyer à Claude QUE l'image concernée par la question (au lieu de
    toute la page) : plus lisible, et reste largement sous la limite de taille de
    l'API vision. Retourne le chemin du PNG, ou None si pas de zone exploitable.
    """
    box = _normalize_crop_rel(crop_rel)
    if not box:
        return None
    try:
        import tempfile

        from PIL import Image

        with Image.open(screenshot_path) as im:
            im = im.convert("RGB")
            w, h = im.size
            if w < 8 or h < 8:
                return None
            l = max(0, int((box["left"] - pad) * w))
            t = max(0, int((box["top"] - pad) * h))
            r = min(w, int((box["left"] + box["width"] + pad) * w))
            b = min(h, int((box["top"] + box["height"] + pad) * h))
            if r - l < 8 or b - t < 8:
                return None
            crop_im = _downscale_if_needed(im.crop((l, t, r, b)), max_edge)
            fd, path = tempfile.mkstemp(suffix=".png", prefix="ans_crop_")
            import os

            os.close(fd)
            crop_im.save(path, "PNG")
            return path
    except Exception:
        return None


def preview_region_data_url(
    screenshot_path: str,
    crop_rel: dict | None,
    needs_question_image: bool,
    *,
    max_edge: int = 520,
    max_edge_full: int = 680,
    quality: int = 78,
) -> str | None:
    """Aperçu WebP en data URL pour la page de validation (sans écrire sur le disque).

    Si la question n'est pas « image », on renvoie quand même une vignette de la capture
    entière (réduite) pour garder le zoom contextuel sur l'écran collé.
    """
    import base64
    import io

    from PIL import Image

    box = _normalize_crop_rel(crop_rel) if needs_question_image else None
    edge = max_edge if needs_question_image else max_edge_full
    try:
        with Image.open(screenshot_path) as im:
            im = im.convert("RGBA")
            w, h = im.size
            if w < 8 or h < 8:
                return None
            if box and needs_question_image:
                l = int(box["left"] * w)
                t = int(box["top"] * h)
                r = int(min(w, (box["left"] + box["width"]) * w))
                b = int(min(h, (box["top"] + box["height"]) * h))
                l, t = max(0, l), max(0, t)
                r, b = max(l + 4, r), max(t + 4, b)
                crop_im = im.crop((l, t, r, b))
            else:
                crop_im = im
            crop_im = _downscale_if_needed(crop_im, edge)
            rgb = Image.new("RGB", crop_im.size, (255, 255, 255))
            alpha = crop_im.split()[3] if crop_im.mode == "RGBA" else None
            rgb.paste(crop_im.convert("RGBA"), mask=alpha)
            buf = io.BytesIO()
            rgb.save(buf, format="WEBP", quality=quality, method=4)
            b64 = base64.standard_b64encode(buf.getvalue()).decode("ascii")
            return f"data:image/webp;base64,{b64}"
    except OSError:
        return None
