#!/usr/bin/env python3
"""
Diagnostic sitemap vs ingestion : ratios chunks/URL, tailles, URLs manquantes.

python3 -m scripts.sitemap_inventory
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.audit_doc_coverage import (  # noqa: E402
    CERT_FUNCTIONAL_MODULES,
    MODULE_URL_PATHS,
    V19_ONLY_MODULES,
)

SITEMAP_VS_REPORT = ROOT / "data" / "sitemap_vs_ingestion.md"
DIAGNOSTIC_REPORT = ROOT / "data" / "diagnostic_recommendation.md"

RATIO_OK_LO = 4.0
RATIO_OK_HI = 10.0
RATIO_LOW = 3.0
RATIO_HIGH = 12.0
NATURAL_CEILING_MAX_URLS = 8
TOKEN_SAMPLE_PER_MODULE = 100


@dataclass
class ModuleStats:
    module: str
    urls_v18: int = 0
    urls_v19: int = 0
    chunks_v18: int = 0
    chunks_v19: int = 0
    urls_db_v18: set[str] = field(default_factory=set)
    urls_db_v19: set[str] = field(default_factory=set)
    token_samples_v18: list[int] = field(default_factory=list)
    token_samples_v19: list[int] = field(default_factory=list)


def _applications_rel(url: str, version: str) -> str | None:
    path = urllib.parse.urlparse(url).path
    prefix = f"/documentation/{version}/applications/"
    if not path.startswith(prefix):
        return None
    rel = path[len(prefix) :].split("#")[0].split("?")[0]
    if rel.endswith(".html"):
        rel = rel[:-5]
    return rel.strip("/") or None


def _path_matches(rel: str, path_prefix: str) -> bool:
    return rel == path_prefix or rel.startswith(path_prefix + "/")


def assign_module(url: str, version: str) -> str | None:
    rel = _applications_rel(url, version)
    if not rel:
        return None
    best_mod: str | None = None
    best_len = -1
    for mod in CERT_FUNCTIONAL_MODULES:
        candidates = MODULE_URL_PATHS.get(mod, [mod])
        for p in candidates:
            if _path_matches(rel, p) and len(p) > best_len:
                best_mod = mod
                best_len = len(p)
    return best_mod


def _ratio(chunks: int, urls: int) -> float | None:
    if urls <= 0:
        return None
    return round(chunks / urls, 1)


def _diagnose(module: str, urls: int, chunks: int, ratio: float | None, version: str) -> str:
    if urls == 0 and chunks == 0:
        if module in V19_ONLY_MODULES and version == "18.0":
            return "— (v19-only)"
        return "— (absent sitemap)"
    if urls == 0 and chunks > 0:
        return "⚠️ chunks sans URL sitemap"
    if ratio is None:
        return "—"
    if ratio < RATIO_LOW:
        if chunks < 30 and urls <= NATURAL_CEILING_MAX_URLS:
            return f"OK plafond naturel (ratio {ratio}x, peu de pages)"
        return f"❌ incomplet (ratio {ratio}x)"
    if ratio > RATIO_HIGH:
        return f"⚠️ over-chunking? (ratio {ratio}x)"
    if RATIO_OK_LO <= ratio <= RATIO_OK_HI:
        return f"OK (ratio ~{ratio}x)"
    return f"OK (ratio {ratio}x)"


def fetch_sitemap_urls(version: str) -> tuple[list[str], str]:
    from scripts.ingest_odoo_docs import discover_urls, filter_by_section

    all_urls, source = discover_urls(version)
    urls = filter_by_section(all_urls, "applications", version)
    return urls, source


def load_db_urls_and_chunks(conn: sqlite3.Connection) -> tuple[dict, dict]:
    """module -> version -> {urls set, chunk count, texts for sampling}."""
    url_sets: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    chunk_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    texts: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))

    rows = conn.execute(
        "SELECT url, version, text FROM chunks WHERE version IN ('18.0', '19.0')"
    ).fetchall()
    for url, ver, text in rows:
        mod = assign_module(url, str(ver))
        if not mod:
            continue
        url_sets[mod][str(ver)].add(url)
        chunk_counts[mod][str(ver)] += 1
        samples = texts[mod][str(ver)]
        if len(samples) < TOKEN_SAMPLE_PER_MODULE:
            samples.append(text or "")

    return url_sets, chunk_counts, texts


def _token_stats(texts: list[str]) -> dict[str, float | int]:
    if not texts:
        return {"n": 0, "avg_chars": 0, "avg_tokens": 0, "min_tokens": 0, "max_tokens": 0}
    try:
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        counts = [len(enc.encode(t)) for t in texts if t]
    except Exception:
        counts = [max(1, len(t) // 4) for t in texts if t]
    if not counts:
        return {"n": 0, "avg_chars": 0, "avg_tokens": 0, "min_tokens": 0, "max_tokens": 0}
    chars = [len(t) for t in texts if t]
    return {
        "n": len(counts),
        "avg_chars": int(sum(chars) / len(chars)),
        "avg_tokens": int(sum(counts) / len(counts)),
        "min_tokens": min(counts),
        "max_tokens": max(counts),
    }


def _missing_urls(sitemap: set[str], db: set[str]) -> list[str]:
    return sorted(sitemap - db)


def build_reports(
    stats: dict[str, ModuleStats],
    sitemap_by_mod: dict[str, dict[str, set[str]]],
    texts: dict[str, dict[str, list[str]]],
    *,
    sources: dict[str, str],
) -> tuple[str, str]:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # --- sitemap_vs_ingestion.md ---
    lines = [
        "# Sitemap vs ingestion",
        "",
        f"*Généré le {now}*",
        "",
        f"Sources URLs : v18 `{sources.get('18.0', '?')}` · v19 `{sources.get('19.0', '?')}`",
        "",
        "**Lecture des ratios** : 4–10 chunks/URL = normal (~600 tokens/chunk). "
        f"< {RATIO_LOW} = pages manquantes ou mauvais découpage. "
        f"> {RATIO_HIGH} = possible sur-découpage.",
        "",
        "## Comparaison sitemap / chunks",
        "",
        "| Module | URLs sitemap v18 | Chunks v18 | Ratio | URLs sitemap v19 | Chunks v19 | Ratio | Diagnostic |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]

    priority: list[tuple[float, str, str]] = []

    for mod in CERT_FUNCTIONAL_MODULES:
        st = stats[mod]
        r18 = _ratio(st.chunks_v18, st.urls_v18)
        r19 = _ratio(st.chunks_v19, st.urls_v19)
        r18s = f"{r18}x" if r18 is not None else "—"
        r19s = f"{r19}x" if r19 is not None else "—"
        diag_parts = []
        if st.urls_v18 or st.chunks_v18:
            diag_parts.append(f"v18: {_diagnose(mod, st.urls_v18, st.chunks_v18, r18, '18.0')}")
        if st.urls_v19 or st.chunks_v19:
            diag_parts.append(f"v19: {_diagnose(mod, st.urls_v19, st.chunks_v19, r19, '19.0')}")
        diag = " · ".join(diag_parts) if diag_parts else "—"
        lines.append(
            f"| `{mod}` | {st.urls_v18} | {st.chunks_v18} | {r18s} | "
            f"{st.urls_v19} | {st.chunks_v19} | {r19s} | {diag} |"
        )
        for ver, urls_n, ch_n, ratio in (
            ("18.0", st.urls_v18, st.chunks_v18, r18),
            ("19.0", st.urls_v19, st.chunks_v19, r19),
        ):
            if urls_n > 0 and ratio is not None and ratio < RATIO_LOW:
                priority.append((ratio, mod, ver))

    lines.extend(["", "## Taille moyenne des chunks (échantillon)", ""])
    lines.append(
        "| Module | Ver. | n échant. | avg chars | avg tokens | min | max | Anomalie découpage |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---|")

    chunk_anomalies: list[str] = []
    for mod in CERT_FUNCTIONAL_MODULES:
        for ver in ("18.0", "19.0"):
            ts = _token_stats(texts.get(mod, {}).get(ver, []))
            if ts["n"] == 0:
                continue
            avg_t = int(ts["avg_tokens"])
            if avg_t < 250:
                anom = f"⚠️ chunks courts (moy. {avg_t} tok)"
                chunk_anomalies.append(f"- `{mod}` v{ver.replace('.0','')} : moyenne {avg_t} tokens")
            elif avg_t > 900:
                anom = f"⚠️ chunks longs (moy. {avg_t} tok)"
                chunk_anomalies.append(f"- `{mod}` v{ver.replace('.0','')} : moyenne {avg_t} tokens")
            else:
                anom = "✅"
            lines.append(
                f"| `{mod}` | {ver} | {ts['n']} | {ts['avg_chars']} | {avg_t} | "
                f"{ts['min_tokens']} | {ts['max_tokens']} | {anom} |"
            )

    # Top 5 worst ratios
    priority.sort(key=lambda x: (x[0], x[1]))
    worst = priority[:5]
    if not worst:
        all_ratios: list[tuple[float, str, str]] = []
        for m in CERT_FUNCTIONAL_MODULES:
            st = stats[m]
            r18 = _ratio(st.chunks_v18, st.urls_v18)
            r19 = _ratio(st.chunks_v19, st.urls_v19)
            if r18 is not None and st.urls_v18 > 0:
                all_ratios.append((r18, m, "18.0"))
            if r19 is not None and st.urls_v19 > 0:
                all_ratios.append((r19, m, "19.0"))
        worst = sorted(all_ratios, key=lambda x: x[0])[:5]

    lines.extend(["", "## Échantillonnage — URLs manquantes (top 5 ratios bas)", ""])
    for ratio, mod, ver in worst:
        sm = sitemap_by_mod.get(mod, {}).get(ver, set())
        db = stats[mod].urls_db_v18 if ver == "18.0" else stats[mod].urls_db_v19
        missing = _missing_urls(sm, db)
        lines.append(f"### `{mod}` · v{ver.replace('.0', '')} (ratio {ratio}x)")
        lines.append(f"- URLs sitemap : **{len(sm)}** · URLs distinctes en base : **{len(db)}** · **Manquantes : {len(missing)}**")
        if missing:
            lines.append("- Premières URLs absentes de la base :")
            for u in missing[:10]:
                lines.append(f"  - `{u}`")
        else:
            lines.append("- Aucune URL sitemap absente de la base (couverture URL complète).")
        lines.append("")

    sitemap_md = "\n".join(lines)

    # --- diagnostic_recommendation.md ---
    recrawl: list[str] = []
    natural: list[str] = []

    for mod in CERT_FUNCTIONAL_MODULES:
        st = stats[mod]
        for ver, urls_n, ch_n, sm_set, db_set in (
            ("18.0", st.urls_v18, st.chunks_v18, sitemap_by_mod.get(mod, {}).get("18.0", set()), st.urls_db_v18),
            ("19.0", st.urls_v19, st.chunks_v19, sitemap_by_mod.get(mod, {}).get("19.0", set()), st.urls_db_v19),
        ):
            if urls_n == 0:
                continue
            missing_n = len(sm_set - db_set)
            ratio = _ratio(ch_n, urls_n)
            if missing_n > 0 and (ratio is None or ratio < RATIO_LOW):
                recrawl.append(
                    f"- `{mod}` v{ver.replace('.0', '')} : {urls_n} URLs au sitemap, "
                    f"{len(db_set)} ingérées ({missing_n} manquantes)"
                )
            elif missing_n == 0 and urls_n <= NATURAL_CEILING_MAX_URLS:
                natural.append(
                    f"- `{mod}` : {urls_n} URLs au sitemap, tout ingéré → **{ch_n}** chunks (plafond naturel)"
                )

    rec_lines = [
        "# Recommandation",
        "",
        f"*Généré le {now}*",
        "",
        "## Modules à re-crawler en priorité",
        "",
    ]
    if recrawl:
        rec_lines.extend(recrawl)
    else:
        rec_lines.append("- *(aucun — ratios et couverture URL satisfaisants)*")

    rec_lines.extend(["", "## Modules au plafond naturel (NE PAS re-crawler)", ""])
    if natural:
        rec_lines.extend(natural)
    else:
        rec_lines.append("- *(aucun identifié automatiquement)*")

    rec_lines.extend(["", "## Anomalies de découpage", ""])
    if chunk_anomalies:
        rec_lines.extend(chunk_anomalies)
    else:
        rec_lines.append("- Aucune — moyennes entre ~400 et 700 tokens sur l’échantillon.")

    rec_lines.extend(
        [
            "",
            "## Action recommandée",
            "",
        ]
    )
    if recrawl:
        mods_v19 = sorted({line.split("`")[1] for line in recrawl if "v19" in line})
        mods_v18 = sorted({line.split("`")[1] for line in recrawl if "v18" in line})
        rec_lines.append("- [ ] Lancer une re-ingestion ciblée (`--modules`) sur les modules listés en priorité")
        if mods_v19:
            rec_lines.append(f"  - v19 : `{','.join(mods_v19)}`")
        if mods_v18:
            rec_lines.append(f"  - v18 : `{','.join(mods_v18)}`")
        rec_lines.append("- [ ] Ajuster les quotas du plan de génération pour les modules au plafond naturel")
        rec_lines.append("- [ ] Procéder à la génération avec la couverture actuelle")
    else:
        rec_lines.append("- [ ] Re-ingestion ciblée non nécessaire selon sitemap")
        rec_lines.append("- [x] Ajuster les quotas (`plan_generation.py`) pour modules petits")
        rec_lines.append("- [ ] Procéder à la génération avec la couverture actuelle (cas optimiste)")

    rec_lines.append("")
    rec_lines.append(
        "**Règle quotas** : `target_questions = min(planned_quota, n_chunks × 2)` "
        "(voir `scripts/plan_generation.py`)."
    )

    return sitemap_md, "\n".join(rec_lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnostic sitemap vs chunks ingérés")
    parser.add_argument("--skip-fetch", action="store_true", help="Ne pas refetch sitemap (tests)")
    args = parser.parse_args()

    from app.odoo_docs_rag import db_path

    db = db_path()
    if not db.exists():
        print(f"❌ Base introuvable : {db}", file=sys.stderr)
        return 1

    stats: dict[str, ModuleStats] = {m: ModuleStats(module=m) for m in CERT_FUNCTIONAL_MODULES}
    sitemap_by_mod: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    sources: dict[str, str] = {}

    if not args.skip_fetch:
        for ver in ("18.0", "19.0"):
            print(f"Fetch catalogue {ver}…")
            urls, src = fetch_sitemap_urls(ver)
            sources[ver] = src
            print(f"  {len(urls)} URLs applications ({src})")
            seen_pages: dict[str, set[str]] = defaultdict(set)
            for url in urls:
                mod = assign_module(url, ver)
                if not mod:
                    continue
                rel = _applications_rel(url, ver) or url
                if rel in seen_pages[mod]:
                    continue
                seen_pages[mod].add(rel)
                sitemap_by_mod[mod][ver].add(url)
            for mod in CERT_FUNCTIONAL_MODULES:
                st = stats[mod]
                n = len(sitemap_by_mod[mod][ver])
                if ver == "18.0":
                    st.urls_v18 = n
                else:
                    st.urls_v19 = n

    conn = sqlite3.connect(db)
    try:
        url_sets, chunk_counts, texts = load_db_urls_and_chunks(conn)
    finally:
        conn.close()

    for mod in CERT_FUNCTIONAL_MODULES:
        st = stats[mod]
        st.chunks_v18 = chunk_counts[mod]["18.0"]
        st.chunks_v19 = chunk_counts[mod]["19.0"]
        st.urls_db_v18 = url_sets[mod]["18.0"]
        st.urls_db_v19 = url_sets[mod]["19.0"]

    sitemap_md, rec_md = build_reports(stats, sitemap_by_mod, texts, sources=sources)
    SITEMAP_VS_REPORT.parent.mkdir(parents=True, exist_ok=True)
    SITEMAP_VS_REPORT.write_text(sitemap_md, encoding="utf-8")
    DIAGNOSTIC_REPORT.write_text(rec_md, encoding="utf-8")

    print(sitemap_md[:3500])
    if len(sitemap_md) > 3500:
        print(f"\n… ({len(sitemap_md)} caractères, voir fichier complet)")
    print(f"\n→ {SITEMAP_VS_REPORT}")
    print(f"→ {DIAGNOSTIC_REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
