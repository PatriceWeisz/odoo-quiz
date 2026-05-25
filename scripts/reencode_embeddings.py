#!/usr/bin/env python3
"""
Réencodage des index vectoriels (banque de questions + documentation Odoo) avec
un modèle d'embedding plus robuste.

⚠️ POURQUOI banque ET doc ensemble :
Le modèle d'embedding est PARTAGÉ (config.json -> bank_rag.model) entre l'index
banque (`data/bank_embeddings.npz`) et l'index doc (`data/odoo_docs.sqlite`), et
la recherche doc rejette toute requête dont la dimension ne correspond pas à
l'index. Changer de modèle SANS réencoder les deux casse le RAG en silence.
Ce script réencode donc les deux par défaut, puis met à jour `config.json`.

Conçu pour tourner SUR LE SERVEUR, hors session Claude (idempotent, backups,
dry-run). Exemple :

    cd /opt/odoo-quiz
    nohup ./.venv/bin/python -m scripts.reencode_embeddings \
        --model mixedbread-ai/mxbai-embed-large-v1 --yes \
        > logs/reencode_$(date +%Y%m%dT%H%M%S).log 2>&1 &

Puis, une fois terminé :  sudo systemctl restart odoo-quiz
(ou ajouter --restart si le script est lancé en root).

Étapes par défaut (--target both) :
  1) charge le modèle, déduit la dimension ;
  2) backups horodatés de config.json + index banque + sqlite doc ;
  3) réencode la banque -> bank_embeddings.npz (+ meta) ;
  4) réencode chaque chunk doc -> sqlite (copie temp puis swap atomique) ;
  5) met à jour config.json : bank_rag.model + bank_rag.query_timeout_s.

Options utiles :
  --dry-run            charge le modèle, encode un échantillon, estime le temps,
                       N'ÉCRIT RIEN.
  --target bank|docs|both   (défaut both)
  --batch-size N       taille de lot d'embedding (défaut 32 — borne la RAM/CPU)
  --query-timeout-s F  valeur écrite dans config (défaut 3.0 ; le gros modèle est
                       plus lent qu'un MiniLM, on relâche le délai de requête)
  --no-config          ne touche pas config.json (reconstruit seulement les index)
  --yes                pas de confirmation interactive
  --restart            systemctl restart odoo-quiz à la fin (nécessite root)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import struct
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CONFIG_PATH = ROOT / "config.json"
QUESTIONS_FILE = ROOT / "questions.json"
DATA_DIR = ROOT / "data"
BANK_NPZ = DATA_DIR / "bank_embeddings.npz"
BANK_META = DATA_DIR / "bank_embeddings_meta.json"

DEFAULT_MODEL = "mixedbread-ai/mxbai-embed-large-v1"


# --------------------------------------------------------------------------- #
# Utilitaires
# --------------------------------------------------------------------------- #
def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _doc_db_path(cfg: dict) -> Path:
    rel = str(((cfg.get("odoo_docs") or {}).get("sqlite_path")) or "data/odoo_docs.sqlite")
    p = Path(rel)
    return p if p.is_absolute() else ROOT / p


def _bank_correct_index(q: dict) -> int | None:
    for j, a in enumerate(q.get("answers") or []):
        if isinstance(a, dict) and a.get("is_correct"):
            return j + 1
    return None


def _bank_rows(bank: list[dict]) -> list[tuple[int, str]]:
    """(id, title) des questions exploitables, triées par id — identique à
    bank_embeddings._rag_rows pour que l'app accepte le cache produit."""
    rows: list[tuple[int, str]] = []
    for q in bank:
        if not isinstance(q, dict) or _bank_correct_index(q) is None:
            continue
        qid = q.get("id")
        title = (q.get("title") or "").strip()
        if qid is None or not title:
            continue
        rows.append((int(qid), title))
    rows.sort(key=lambda x: x[0])
    return rows


def _vec_to_blob(vec: np.ndarray) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec.astype(np.float32).tolist())


def _atomic_replace(tmp: Path, dst: Path) -> None:
    os.replace(tmp, dst)


# --------------------------------------------------------------------------- #
# Modèle / embedding
# --------------------------------------------------------------------------- #
def _load_model(model_name: str):
    try:
        from fastembed import TextEmbedding
    except ImportError:
        sys.exit("❌ fastembed absent dans cet environnement (pip install fastembed).")
    _log(f"Chargement du modèle {model_name} (téléchargement au 1er run)…")
    t0 = time.perf_counter()
    try:
        model = TextEmbedding(model_name=model_name)
    except Exception as exc:  # modèle inconnu / réseau
        sys.exit(f"❌ Impossible de charger « {model_name} » : {exc}")
    _log(f"Modèle prêt en {time.perf_counter() - t0:.1f}s.")
    return model


def _embed_batches(model, texts: list[str], batch_size: int, label: str) -> np.ndarray:
    """Embeddings L2-normalisés, par lots, avec progression."""
    out: list[np.ndarray] = []
    n = len(texts)
    t0 = time.perf_counter()
    done = 0
    for start in range(0, n, batch_size):
        chunk = texts[start : start + batch_size]
        vecs = np.array(list(model.embed(chunk)), dtype=np.float32)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        out.append(vecs / np.maximum(norms, 1e-9))
        done += len(chunk)
        elapsed = time.perf_counter() - t0
        rate = done / elapsed if elapsed > 0 else 0
        eta = (n - done) / rate if rate > 0 else 0
        _log(f"  {label}: {done}/{n}  ({rate:.0f}/s, ETA {eta:.0f}s)")
    return np.vstack(out) if out else np.zeros((0, 0), dtype=np.float32)


def _probe_dim(model) -> int:
    v = np.array(list(model.embed(["dimension probe"]))[0], dtype=np.float32)
    return int(v.shape[0])


# --------------------------------------------------------------------------- #
# Réencodage banque
# --------------------------------------------------------------------------- #
def reencode_bank(model, model_name: str, batch_size: int) -> dict:
    from bank_embeddings import bank_rag_fingerprint

    bank = json.loads(QUESTIONS_FILE.read_text(encoding="utf-8")).get("questions", [])
    rows = _bank_rows(bank)
    if not rows:
        sys.exit("❌ Aucune question exploitable dans questions.json.")
    fp = bank_rag_fingerprint(bank)
    ids = np.array([i for i, _ in rows], dtype=np.int64)
    titles = [t for _, t in rows]

    _log(f"Banque : {len(rows)} questions à encoder (fingerprint {fp}).")
    matrix = _embed_batches(model, titles, batch_size, "banque")
    dim = int(matrix.shape[1]) if matrix.ndim == 2 else 0

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # ⚠️ np.savez_compressed AJOUTE « .npz » si le nom ne finit pas par .npz :
    # le fichier temporaire doit donc déjà finir par .npz, sinon os.replace
    # cible un fichier inexistant.
    tmp_npz = BANK_NPZ.with_name(BANK_NPZ.stem + ".tmp.npz")
    np.savez_compressed(tmp_npz, ids=ids, matrix=matrix)
    _atomic_replace(tmp_npz, BANK_NPZ)

    meta = {"fingerprint": fp, "model": model_name, "count": int(ids.size), "dim": dim}
    tmp_meta = BANK_META.with_name(BANK_META.name + ".tmp")
    tmp_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    _atomic_replace(tmp_meta, BANK_META)
    _log(f"✅ Banque réencodée : {ids.size} vecteurs, dim {dim} -> {BANK_NPZ.name}")
    return meta


# --------------------------------------------------------------------------- #
# Réencodage doc Odoo (sqlite)
# --------------------------------------------------------------------------- #
def reencode_docs(model, cfg: dict, batch_size: int, expected_dim: int) -> dict:
    src = _doc_db_path(cfg)
    if not src.exists():
        sys.exit(f"❌ Base doc introuvable : {src}")
    tmp = src.with_name(src.name + ".reencode.tmp")
    if tmp.exists():
        tmp.unlink()
    _log(f"Doc : copie de travail {tmp.name}…")
    shutil.copy2(src, tmp)

    conn = sqlite3.connect(tmp)
    try:
        rows = conn.execute("SELECT chunk_id, text FROM chunks ORDER BY rowid").fetchall()
        n = len(rows)
        if n == 0:
            sys.exit("❌ Aucun chunk doc à réencoder.")
        _log(f"Doc : {n} chunks à réencoder.")
        t0 = time.perf_counter()
        done = 0
        for start in range(0, n, batch_size):
            batch = rows[start : start + batch_size]
            texts = [(t or "") for _, t in batch]
            vecs = np.array(list(model.embed(texts)), dtype=np.float32)
            norms = np.linalg.norm(vecs, axis=1, keepdims=True)
            vecs = vecs / np.maximum(norms, 1e-9)
            updates = [
                (_vec_to_blob(vecs[k]), batch[k][0]) for k in range(len(batch))
            ]
            conn.executemany("UPDATE chunks SET embedding = ? WHERE chunk_id = ?", updates)
            conn.commit()
            done += len(batch)
            elapsed = time.perf_counter() - t0
            rate = done / elapsed if elapsed > 0 else 0
            eta = (n - done) / rate if rate > 0 else 0
            _log(f"  doc: {done}/{n}  ({rate:.0f}/s, ETA {eta:.0f}s)")

        # Vérification de dimension sur un échantillon
        blob = conn.execute("SELECT embedding FROM chunks LIMIT 1").fetchone()[0]
        got_dim = len(blob) // 4
        if got_dim != expected_dim:
            sys.exit(f"❌ Dim doc {got_dim} != dim modèle {expected_dim} — abandon (DB temp conservée : {tmp}).")
    finally:
        conn.close()

    _atomic_replace(tmp, src)
    _log(f"✅ Doc réencodée : {n} chunks, dim {expected_dim} -> {src.name}")
    return {"count": n, "dim": expected_dim}


# --------------------------------------------------------------------------- #
# config.json
# --------------------------------------------------------------------------- #
def update_config(model_name: str, query_timeout_s: float) -> None:
    cfg = _load_config()
    br = cfg.get("bank_rag")
    if not isinstance(br, dict):
        br = {}
        cfg["bank_rag"] = br
    br["model"] = model_name
    br["query_timeout_s"] = query_timeout_s
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    _atomic_replace(tmp, CONFIG_PATH)
    _log(f"✅ config.json : bank_rag.model={model_name}, query_timeout_s={query_timeout_s}")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description="Réencode les index vectoriels (banque + doc Odoo).")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"modèle fastembed (défaut {DEFAULT_MODEL})")
    ap.add_argument("--target", choices=["bank", "docs", "both"], default="both")
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--query-timeout-s", type=float, default=3.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-config", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--restart", action="store_true", help="systemctl restart odoo-quiz à la fin (root)")
    args = ap.parse_args()

    cfg = _load_config()
    old_model = (cfg.get("bank_rag") or {}).get("model", "?")

    model = _load_model(args.model)
    dim = _probe_dim(model)
    _log(f"Dimension du modèle : {dim}")

    # Comptages pour le plan
    bank = json.loads(QUESTIONS_FILE.read_text(encoding="utf-8")).get("questions", [])
    n_bank = len(_bank_rows(bank))
    doc_db = _doc_db_path(cfg)
    n_docs = 0
    if doc_db.exists():
        c = sqlite3.connect(doc_db)
        try:
            n_docs = int(c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
        finally:
            c.close()

    print("\n" + "=" * 64)
    print(f"  Modèle actuel  : {old_model}")
    print(f"  Nouveau modèle : {args.model}  (dim {dim})")
    print(f"  Cible          : {args.target}")
    print(f"  Banque         : {n_bank} questions")
    print(f"  Doc Odoo       : {n_docs} chunks  ({doc_db})")
    print(f"  Batch size     : {args.batch_size}")
    print(f"  query_timeout_s: {args.query_timeout_s} (écrit dans config)" + (" [no-config]" if args.no_config else ""))
    print("=" * 64 + "\n")

    if args.dry_run:
        _log("DRY-RUN : test d'encodage d'un échantillon (rien n'est écrit).")
        sample = [t for _, t in _bank_rows(bank)[:64]] or ["échantillon"]
        t0 = time.perf_counter()
        _ = _embed_batches(model, sample, args.batch_size, "échantillon")
        dt = time.perf_counter() - t0
        per = dt / max(1, len(sample))
        est_bank = per * n_bank
        est_docs = per * n_docs * 4  # chunks plus longs que des titres -> facteur indicatif
        _log(f"~{per*1000:.0f} ms/texte (titres). Estimation : banque ~{est_bank:.0f}s, "
             f"doc ~{est_docs:.0f}s (ordre de grandeur).")
        _log("DRY-RUN terminé. Relancer sans --dry-run pour appliquer.")
        return

    if not args.yes:
        resp = input("Confirmer le réencodage + MAJ config ? [y/N] ").strip().lower()
        if resp not in ("y", "yes", "o", "oui"):
            sys.exit("Annulé.")

    # Backups
    stamp = _ts()
    _log("Backups…")
    if CONFIG_PATH.exists():
        shutil.copy2(CONFIG_PATH, CONFIG_PATH.with_name(f"config.json.bak.{stamp}"))
    for p in (BANK_NPZ, BANK_META):
        if p.exists():
            shutil.copy2(p, p.with_name(p.name + f".bak.{stamp}"))
    if doc_db.exists() and args.target in ("docs", "both"):
        shutil.copy2(doc_db, doc_db.with_name(doc_db.name + f".bak.{stamp}"))
    _log(f"Backups horodatés .bak.{stamp} créés.")

    if args.target in ("bank", "both"):
        bmeta = reencode_bank(model, args.model, args.batch_size)
        if bmeta["dim"] != dim:
            sys.exit(f"❌ Dim banque {bmeta['dim']} != {dim} — abandon.")

    if args.target in ("docs", "both"):
        reencode_docs(model, cfg, args.batch_size, dim)

    if not args.no_config:
        update_config(args.model, args.query_timeout_s)

    print("\n" + "=" * 64)
    _log("✅ Réencodage terminé.")
    print("  Prochaine étape : redémarrer le service pour recharger les index :")
    print("      sudo systemctl restart odoo-quiz")
    print("  Vérifier ensuite : curl -s localhost:5001/health")
    print("=" * 64)

    if args.restart:
        _log("Redémarrage du service (--restart)…")
        rc = os.system("systemctl restart odoo-quiz")
        _log(f"systemctl restart odoo-quiz -> code {rc}")


if __name__ == "__main__":
    main()
