#!/usr/bin/env python3
"""Clé API Anthropic + appels Messages (texte / vision). Config : config.json → anthropic."""

from __future__ import annotations

import base64
import json
import random
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
# Délai lecture HTTP par tentative (secondes) — au-delà : message explicite, sans retry infini.
ANTHROPIC_REQUEST_TIMEOUT_S = 15
# Statut public Anthropic (Atlassian Statuspage) — sans clé API, distinct de api.anthropic.com.
ANTHROPIC_STATUS_SUMMARY_JSON = "https://status.anthropic.com/api/v2/summary.json"
ANTHROPIC_STATUS_PAGE_URL = "https://status.anthropic.com"


def _cfg() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def api_available() -> bool:
    a = _cfg().get("anthropic") or {}
    return bool((a.get("api_key") or "").strip())


def llm_available() -> bool:
    return api_available() or bool(shutil.which("claude"))


def _anthropic_models() -> tuple[str, str, str]:
    a = _cfg().get("anthropic") or {}
    # IDs à jour (les anciens ex. claude-sonnet-4-20250514 peuvent renvoyer 404 après retrait API).
    vision = (a.get("vision_model") or "claude-sonnet-4-6").strip()
    text = (a.get("text_model") or a.get("model") or "claude-haiku-4-5").strip()
    answer = (a.get("answer_model") or "").strip()
    if not answer:
        # Réponses quiz : Sonnet par défaut (Haiku se trompe trop souvent sur la certification).
        answer = vision if text == "claude-haiku-4-5" else text
    return vision, text, answer


def _anthropic_key() -> str:
    return ((_cfg().get("anthropic") or {}).get("api_key") or "").strip()


def run_prompt(prompt: str, timeout: int = ANTHROPIC_REQUEST_TIMEOUT_S) -> str:
    _, text_model, _ = _anthropic_models()
    return _messages_api_text(prompt, text_model, timeout)


def run_answer_prompt(
    prompt: str,
    image_paths: list[str] | None = None,
    timeout: int = 60,
) -> str:
    """Appel pour déduire la bonne réponse / enrichir une capture (modèle answer_model, température 0)."""
    vision_model, _, answer_model = _anthropic_models()
    paths = [p for p in (image_paths or []) if p]
    if paths:
        return _messages_api_vision(prompt, paths, vision_model, timeout, temperature=0)
    return _messages_api_text(prompt, answer_model, timeout, temperature=0)


def run_prompt_with_images(
    prompt: str, image_paths: list[str], timeout: int = ANTHROPIC_REQUEST_TIMEOUT_S
) -> str:
    vision_model, _, _ = _anthropic_models()
    return _messages_api_vision(prompt, image_paths, vision_model, timeout)


def _messages_api_text(
    prompt: str, model: str, timeout: int, temperature: float | None = None
) -> str:
    key = _anthropic_key()
    if not key:
        raise RuntimeError("Clé API Anthropic absente (config.json → anthropic.api_key).")
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
    }
    if temperature is not None:
        body["temperature"] = temperature
    return _post_anthropic(body, timeout)


_VISION_MAX_EDGE = 7800  # marge sous la limite stricte Anthropic (8000 px / côté)


def _vision_safe_image(raw: bytes) -> tuple[bytes, str | None]:
    """Filet anti-dépassement : si une dimension dépasse la limite de l'API vision
    (8000 px), réduit l'image. Retourne (octets, media_type) ; media_type est None
    si l'image est inchangée."""
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(raw)) as im:
            w, h = im.size
            if max(w, h) <= _VISION_MAX_EDGE:
                return raw, None
            scale = _VISION_MAX_EDGE / float(max(w, h))
            im2 = im.convert("RGB").resize(
                (max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS
            )
            buf = io.BytesIO()
            im2.save(buf, format="PNG")
            return buf.getvalue(), "image/png"
    except Exception:
        return raw, None


def _messages_api_vision(
    prompt: str, paths: list[str], model: str, timeout: int, temperature: float | None = None
) -> str:
    key = _anthropic_key()
    if not key:
        raise RuntimeError("Clé API Anthropic absente : la lecture d’image nécessite l’API.")
    content: list[dict] = [{"type": "text", "text": prompt}]
    for p in paths:
        path = Path(p)
        if not path.is_file():
            raise RuntimeError(f"Image introuvable : {p}")
        raw = path.read_bytes()
        suf = path.suffix.lower()
        mt = "image/png"
        if suf in (".jpg", ".jpeg"):
            mt = "image/jpeg"
        elif suf == ".webp":
            mt = "image/webp"
        elif suf == ".gif":
            mt = "image/gif"
        raw2, mt2 = _vision_safe_image(raw)
        if mt2:
            raw, mt = raw2, mt2
        b64 = base64.standard_b64encode(raw).decode("ascii")
        content.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": mt, "data": b64},
            }
        )
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": content}],
    }
    if temperature is not None:
        body["temperature"] = temperature
    return _post_anthropic(body, timeout)


def _anthropic_timeout_message(timeout: int) -> str:
    return (
        f"La requête vers Anthropic a dépassé le délai de {timeout} s. "
        "Réessayez dans un instant, ou avec une capture plus légère / moins de questions à la fois."
    )


def _urlerror_is_read_timeout(e: urllib.error.URLError) -> bool:
    r = getattr(e, "reason", None)
    if isinstance(r, socket.timeout):
        return True
    if isinstance(r, TimeoutError):
        return True
    if isinstance(r, OSError) and getattr(r, "errno", None) == 110:  # ETIMEDOUT (Linux)
        return True
    s = str(r) if r is not None else str(e)
    return "timed out" in s.lower() or "expire" in s.lower()


def anthropic_public_api_status(timeout: float = 4.0) -> dict[str, Any]:
    """Lit la page de statut publique (composant « Claude API »). Ne consomme pas de quota Messages."""
    out: dict[str, Any] = {
        "fetched": False,
        "source": ANTHROPIC_STATUS_SUMMARY_JSON,
        "status_page": ANTHROPIC_STATUS_PAGE_URL,
        "claude_api_component": None,
        "page_indicator": None,
        "page_description": None,
        "open_incident_name": None,
        "error": None,
    }
    try:
        req = urllib.request.Request(
            ANTHROPIC_STATUS_SUMMARY_JSON,
            method="GET",
            headers={"User-Agent": "odoo-quiz/anthropic-status-check"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        out["fetched"] = True
        st = data.get("status") or {}
        out["page_indicator"] = st.get("indicator")
        out["page_description"] = st.get("description")
        for c in data.get("components") or []:
            name = c.get("name") or ""
            if "api.anthropic.com" in name:
                out["claude_api_component"] = {
                    "name": name,
                    "status": c.get("status"),
                }
                break
        for inc in data.get("incidents") or []:
            if not isinstance(inc, dict):
                continue
            if inc.get("status") in ("resolved", "postmortem"):
                continue
            nm = (inc.get("name") or "").strip()
            if nm:
                out["open_incident_name"] = nm
                break
    except Exception as e:
        out["error"] = str(e)[:240]
    return out


def _anthropic_public_status_hint_fr(snap: dict[str, Any]) -> str:
    """Phrase courte pour compléter un message d’erreur API (529, etc.)."""
    if not snap.get("fetched"):
        err = snap.get("error") or "erreur réseau"
        return (
            f"Vérification statut public impossible ({err}). "
            f"Ouvrez {ANTHROPIC_STATUS_PAGE_URL} pour l’état officiel."
        )
    comp = snap.get("claude_api_component")
    api_st = (comp or {}).get("status") or "inconnu"
    parts = [f"Statut public « Claude API » : {api_st}."]
    if snap.get("page_description"):
        parts.append(str(snap["page_description"]).strip() + ".")
    if snap.get("open_incident_name"):
        parts.append(f"Incident : {str(snap['open_incident_name'])[:200]}.")
    parts.append(f"Détail : {ANTHROPIC_STATUS_PAGE_URL} .")
    return " ".join(parts)


def _post_anthropic(body: dict, timeout: int) -> str:
    key = _anthropic_key()
    data = json.dumps(body).encode("utf-8")
    max_attempts = 6
    base_delay_s = 2.0
    for attempt in range(max_attempts):
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=data,
            method="POST",
            headers={
                "content-type": "application/json",
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                out = json.loads(resp.read().decode("utf-8"))
            break
        except socket.timeout as e:
            raise RuntimeError(_anthropic_timeout_message(timeout)) from e
        except TimeoutError as e:
            raise RuntimeError(_anthropic_timeout_message(timeout)) from e
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            retryable = e.code in (429, 503, 529)
            if retryable and attempt < max_attempts - 1:
                wait = min(90.0, base_delay_s * (2**attempt) + random.uniform(0, 0.4))
                ra = e.headers.get("Retry-After")
                if ra:
                    try:
                        wait = max(wait, float(ra))
                    except ValueError:
                        pass
                time.sleep(wait)
                continue
            hint = ""
            if e.code == 404 and "not_found_error" in err and "model" in err:
                hint = (
                    " — Vérifiez anthropic.vision_model / text_model dans config.json "
                    "(liste : https://docs.anthropic.com/en/docs/about-claude/models )."
                )
            overload = e.code == 529 or "overloaded" in err.lower()
            tail = (
                " — Les serveurs Anthropic sont saturés (réessayez dans une minute)."
                if overload
                else ""
            )
            if overload:
                tail += " — " + _anthropic_public_status_hint_fr(anthropic_public_api_status(timeout=4.0))
            raise RuntimeError(f"API Anthropic HTTP {e.code}: {err[:800]}{hint}{tail}") from e
        except urllib.error.URLError as e:
            if _urlerror_is_read_timeout(e):
                raise RuntimeError(_anthropic_timeout_message(timeout)) from e
            if attempt < max_attempts - 1:
                time.sleep(min(30.0, base_delay_s * (2**attempt)))
                continue
            raise RuntimeError(f"Réseau API Anthropic : {e}") from e

    parts = out.get("content") or []
    texts = [p.get("text", "") for p in parts if p.get("type") == "text"]
    return "\n".join(texts).strip()


def _repair_json_fences(s: str) -> str:
    """Guillemets typographiques, virgules finales avant ] ou }."""
    t = s.replace("\u201c", '"').replace("\u201d", '"').replace("\u00ab", '"').replace("\u00bb", '"')
    t = re.sub(r",(\s*[\]}])", r"\1", t)
    return t


def _repair_title_unescaped_quotes(s: str) -> str:
    """Remplace les guillemets ASCII internes du champ title par des apostrophes (erreur fréquente du modèle)."""
    m = re.search(r'"title"\s*:\s*"', s)
    if not m:
        return s
    val_start = m.end()
    m2 = re.search(r'",\s*"answers"', s[val_start:])
    if not m2:
        return s
    inner_end = val_start + m2.start()
    inner = s[val_start:inner_end]
    if '"' not in inner:
        return s
    fixed = inner.replace('\\"', "\x00").replace('"', "'").replace("\x00", '\\"')
    return s[:val_start] + fixed + s[inner_end:]


def _extract_first_json_value(s: str) -> str:
    """Première valeur JSON (objet ou tableau) en respectant imbrication."""
    s = s.strip()
    if not s:
        raise ValueError("Réponse vide.")
    if s[0] not in "[{":
        idx_br = s.find("{")
        idx_sq = s.find("[")
        candidates = [i for i in (idx_br, idx_sq) if i >= 0]
        if not candidates:
            raise ValueError("Aucun objet JSON trouvé dans la réponse du modèle.")
        s = s[min(candidates) :]
    depth = 0
    for i, ch in enumerate(s):
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                return s[: i + 1]
    raise ValueError("JSON incomplet (accolades ou crochets non équilibrés).")


def parse_json_value(text: str):
    """Parse une réponse modèle : objet JSON ou tableau d’objets (bloc ```json``` toléré)."""
    s = (text or "").strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", s, re.I)
    if m:
        s = m.group(1).strip()
    last_err: Exception | None = None
    chunks: list[str] = [s]
    try:
        ext = _extract_first_json_value(s)
        if ext != s:
            chunks.append(ext)
    except ValueError:
        pass
    for chunk in chunks:
        variants: list[str] = []
        for base in (chunk, _repair_json_fences(chunk)):
            if base not in variants:
                variants.append(base)
            tq = _repair_title_unescaped_quotes(base)
            if tq not in variants:
                variants.append(tq)
            both = _repair_title_unescaped_quotes(_repair_json_fences(base))
            if both not in variants:
                variants.append(both)
        for cand in variants:
            try:
                return json.loads(cand)
            except json.JSONDecodeError as e:
                last_err = e
                continue
    hint = str(last_err) if last_err else "inconnu"
    raise ValueError(
        f"Impossible de parser le JSON du modèle ({hint}). "
        f"Réessayez ou recadrez la capture. Extrait : {(s or '')[:500]}"
    )


def parse_json_object(text: str) -> dict:
    """Extrait le premier objet JSON (compatibilité ; préférer parse_json_value pour liste ou objet)."""
    val = parse_json_value(text)
    if isinstance(val, list):
        if not val:
            raise ValueError("Liste JSON vide.")
        if not isinstance(val[0], dict):
            raise ValueError("La liste JSON doit contenir des objets.")
        return val[0]
    if isinstance(val, dict):
        return val
    raise ValueError("Le JSON doit être un objet ou une liste d’objets.")
