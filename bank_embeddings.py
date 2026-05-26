#!/usr/bin/env python3
"""Index d'embeddings banque (fastembed local) — pré-calculé, recherche ~ms à la requête."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)


def _bank_correct_index(q: dict) -> int | None:
    for j, a in enumerate(q.get("answers") or []):
        if isinstance(a, dict) and a.get("is_correct"):
            return j + 1
    return None

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "data"
CACHE_NPZ = CACHE_DIR / "bank_embeddings.npz"
CACHE_META = CACHE_DIR / "bank_embeddings_meta.json"

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
# Délai d'embedding d'UNE requête banque (thread). Défaut bas, adapté à MiniLM ;
# surchargeable via config.json -> bank_rag.query_timeout_s. À augmenter pour un
# modèle plus gros/lent (ex. mxbai-embed-large), sinon la requête expire et le RAG
# vectoriel banque retombe en silence sur le matching lexical.
QUERY_EMBED_TIMEOUT_S = 0.25
MIN_COSINE_DEFAULT = 0.28

_lock = threading.Lock()
_index: "BankVectorIndex | None" = None
_model = None
_model_failed = False
_warmup_started = False


@dataclass(frozen=True)
class BankVectorIndex:
    ids: np.ndarray  # int64 (n,)
    matrix: np.ndarray  # float32 (n, dim) L2-normalized
    fingerprint: str
    model_name: str

    def top_ids(self, query_vec: np.ndarray, *, top_n: int, min_cosine: float) -> list[tuple[int, float]]:
        q = query_vec.astype(np.float32, copy=False)
        norm = float(np.linalg.norm(q))
        if norm < 1e-9:
            return []
        q = q / norm
        scores = self.matrix @ q
        if scores.size == 0:
            return []
        k = min(top_n, scores.size)
        idx = np.argpartition(scores, -k)[-k:]
        idx = idx[np.argsort(scores[idx])[::-1]]
        out: list[tuple[int, float]] = []
        for i in idx:
            sc = float(scores[i])
            if sc >= min_cosine:
                out.append((int(self.ids[i]), sc))
        return out


def _load_rag_config() -> dict[str, Any]:
    cfg_path = ROOT / "config.json"
    if not cfg_path.exists():
        return {}
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        br = cfg.get("bank_rag")
        return br if isinstance(br, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def embeddings_enabled() -> bool:
    return bool(_load_rag_config().get("embeddings", True))


def _model_name() -> str:
    return str(_load_rag_config().get("model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL


def _min_cosine() -> float:
    try:
        return float(_load_rag_config().get("min_cosine", MIN_COSINE_DEFAULT))
    except (TypeError, ValueError):
        return MIN_COSINE_DEFAULT


def _query_timeout_s() -> float:
    """Délai d'embedding requête (config bank_rag.query_timeout_s, défaut module)."""
    try:
        return float(_load_rag_config().get("query_timeout_s", QUERY_EMBED_TIMEOUT_S))
    except (TypeError, ValueError):
        return QUERY_EMBED_TIMEOUT_S


def bank_rag_fingerprint(question_bank: list[dict]) -> str:
    lines: list[str] = []
    for q in question_bank:
        if not isinstance(q, dict) or _bank_correct_index(q) is None:
            continue
        qid = q.get("id")
        title = (q.get("title") or "").strip()
        if qid is None or not title:
            continue
        lines.append(f"{int(qid)}\t{title}")
    lines.sort()
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return digest[:20]


def _rag_rows(question_bank: list[dict]) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    for q in question_bank:
        if not isinstance(q, dict) or _bank_correct_index(q) is None:
            continue
        qid = q.get("id")
        title = (q.get("title") or "").strip()
        if qid is None or not title:
            continue
        rows.append((int(qid), title))
    rows.sort(key=lambda x: x[0])
    return rows


def _get_embed_model():
    global _model, _model_failed
    if _model_failed:
        return None
    if _model is not None:
        return _model
    with _lock:
        if _model_failed:
            return None
        if _model is not None:
            return _model
        try:
            from fastembed import TextEmbedding
        except ImportError:
            log.warning("fastembed absent — RAG vectoriel désactivé (pip install fastembed)")
            _model_failed = True
            return None
        try:
            _model = TextEmbedding(model_name=_model_name())
        except Exception as exc:
            log.warning("Chargement modèle embeddings échoué : %s", exc)
            _model_failed = True
            return None
        return _model


def embed_texts(texts: list[str]) -> np.ndarray | None:
    """Embeddings L2-normalisés (batch) — partagé banque + doc Odoo."""
    return _embed_texts(texts)


def embed_query_text(text: str, *, timeout_s: float | None = None) -> np.ndarray | None:
    """Vecteur requête ; timeout_s=None → pas de limite (ingestion / scripts)."""
    if timeout_s is None:
        title = (text or "").strip()
        if not title:
            return None
        mat = _embed_texts([title])
        return mat[0] if mat is not None and mat.shape[0] else None
    return _embed_query(text)


def _embed_texts(texts: list[str]) -> np.ndarray | None:
    if not texts:
        return None
    model = _get_embed_model()
    if model is None:
        return None
    try:
        # batch_size borné (32) pour limiter le pic mémoire avec un gros modèle
        # type mxbai-large-v1 : sans cette borne, fastembed prend batch_size=256
        # et peut allouer >3 Go d'arena onnxruntime (cause d'OOM observée le 26 mai
        # 2026 sur le VPS Hetzner CX23/4 Go avant l'upgrade en CX33).
        vecs = np.array(list(model.embed(texts, batch_size=32)), dtype=np.float32)
    except Exception as exc:
        log.warning("Embedding batch échoué : %s", exc)
        return None
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms = np.maximum(norms, 1e-9)
    return vecs / norms


def _embed_query(title: str) -> np.ndarray | None:
    title = (title or "").strip()
    if not title:
        return None
    model = _get_embed_model()
    if model is None:
        return None

    result: list[np.ndarray | None] = [None]

    def _run() -> None:
        try:
            result[0] = np.array(list(model.embed([title]))[0], dtype=np.float32)
        except Exception:
            result[0] = None

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(timeout=_query_timeout_s())
    if th.is_alive() or result[0] is None:
        return None
    return result[0]


def _save_cache(index: BankVectorIndex) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(CACHE_NPZ, ids=index.ids, matrix=index.matrix)
    meta = {
        "fingerprint": index.fingerprint,
        "model": index.model_name,
        "count": int(index.ids.size),
        "dim": int(index.matrix.shape[1]) if index.matrix.ndim == 2 else 0,
    }
    CACHE_META.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_cache(fingerprint: str, model_name: str) -> BankVectorIndex | None:
    if not CACHE_NPZ.exists() or not CACHE_META.exists():
        return None
    try:
        meta = json.loads(CACHE_META.read_text(encoding="utf-8"))
        if meta.get("fingerprint") != fingerprint or meta.get("model") != model_name:
            return None
        data = np.load(CACHE_NPZ)
        ids = data["ids"].astype(np.int64, copy=False)
        matrix = data["matrix"].astype(np.float32, copy=False)
        if ids.size == 0 or matrix.shape[0] != ids.size:
            return None
        return BankVectorIndex(ids=ids, matrix=matrix, fingerprint=fingerprint, model_name=model_name)
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        log.debug("Cache embeddings illisible : %s", exc)
        return None


def build_bank_vector_index(question_bank: list[dict]) -> BankVectorIndex | None:
    if not embeddings_enabled():
        return None
    rows = _rag_rows(question_bank)
    if not rows:
        return None
    fp = bank_rag_fingerprint(question_bank)
    model_name = _model_name()
    cached = _load_cache(fp, model_name)
    if cached is not None:
        return cached
    texts = [t for _, t in rows]
    matrix = _embed_texts(texts)
    if matrix is None:
        return None
    ids = np.array([i for i, _ in rows], dtype=np.int64)
    index = BankVectorIndex(ids=ids, matrix=matrix, fingerprint=fp, model_name=model_name)
    try:
        _save_cache(index)
    except OSError as exc:
        log.warning("Écriture cache embeddings : %s", exc)
    return index


def get_bank_vector_index() -> BankVectorIndex | None:
    return _index


def clear_bank_vector_index() -> None:
    """Réinitialise l'index en mémoire (rechargement banque sans effacer le cache disque)."""
    global _index, _warmup_started
    with _lock:
        _index = None
        _warmup_started = False


def invalidate_embedding_cache() -> None:
    """Efface mémoire + fichiers cache (après modification de questions.json)."""
    clear_bank_vector_index()
    for p in (CACHE_NPZ, CACHE_META):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def _set_index(index: BankVectorIndex | None) -> None:
    global _index
    with _lock:
        _index = index


def warmup_bank_embeddings(question_bank: list[dict]) -> bool:
    """Construit ou charge l'index (bloquant — à appeler en arrière-plan)."""
    if not embeddings_enabled():
        return False
    try:
        index = build_bank_vector_index(question_bank)
    except Exception as exc:
        log.warning("Warmup embeddings : %s", exc)
        return False
    if index is not None:
        _set_index(index)
        log.info("RAG vectoriel prêt : %s questions indexées", index.ids.size)
        return True
    return False


def schedule_bank_embedding_warmup(question_bank: list[dict]) -> None:
    """Lance le warmup en thread daemon (ne bloque pas Flask)."""
    global _warmup_started
    if not embeddings_enabled():
        return
    with _lock:
        if _warmup_started and _index is not None:
            return
        _warmup_started = True

    bank_copy = list(question_bank)

    def _run() -> None:
        warmup_bank_embeddings(bank_copy)

    threading.Thread(target=_run, name="bank-embed-warmup", daemon=True).start()


def vector_similar_question_ids(
    new_title: str,
    *,
    top_n: int = 12,
    min_cosine: float | None = None,
) -> list[tuple[int, float]]:
    """IDs banque les plus proches par cosine (index pré-calculé)."""
    index = get_bank_vector_index()
    if index is None:
        return []
    qv = _embed_query(new_title)
    if qv is None:
        return []
    mc = _min_cosine() if min_cosine is None else min_cosine
    return index.top_ids(qv, top_n=top_n, min_cosine=mc)
