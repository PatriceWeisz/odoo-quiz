#!/usr/bin/env python3
"""
Valide que chaque module déclaré dans `app.study_modules.STUDY_MODULES` a bien
des URLs correspondantes côté Odoo Docs (v18 et v19).

Pour chaque (module, version), compte les URLs sitemap dont le chemin commence
par `/applications/<url_path>/`. Si zéro, propose des correspondances proches
(préfixe / sous-chaîne / segment final identique) pour aider à corriger
`MODULE_URL_PATHS`.

Usage :
  python3 -m scripts.validate_module_paths
  python3 -m scripts.validate_module_paths --output data/module_paths_report.md
  python3 -m scripts.validate_module_paths --version 19.0    # une seule version

Aucune écriture en base : pure lecture sitemap/searchindex.
"""

from __future__ import annotations

import argparse
import sys
import urllib.parse
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.study_modules import (  # noqa: E402
    ALL_TIERS,
    STUDY_MODULES,
    V19_ONLY_MODULES,
    is_v19_only,
    tier_of,
    url_paths_for,
)
from scripts.ingest_odoo_docs import discover_urls, doc_prefix  # noqa: E402

DEFAULT_REPORT = ROOT / "data" / "module_paths_report.md"
VERSIONS_DEFAULT = ("18.0", "19.0")


def _applications_root(version: str) -> str:
    return doc_prefix(version) + "applications/"


def _segments_in_applications(url: str, version: str) -> list[str]:
    """Retourne les segments de chemin sous /applications/ pour une URL.

    Ex: /documentation/19.0/applications/sales/crm/foo.html
        → ['sales', 'crm', 'foo.html']
    """
    path = urllib.parse.urlparse(url).path
    root = _applications_root(version)
    if not path.startswith(root):
        return []
    rest = path[len(root):]
    return [s for s in rest.split("/") if s]


def _count_urls_for_path(urls: list[str], version: str, url_path: str) -> int:
    """Combien d'URLs commencent par /applications/<url_path>/ ou
    /applications/<url_path>.html ?"""
    base = _applications_root(version) + url_path
    n = 0
    for u in urls:
        path = urllib.parse.urlparse(u).path
        if path.startswith(base + "/") or path == base + ".html":
            n += 1
    return n


def _candidate_paths(urls: list[str], version: str) -> dict[str, int]:
    """Inventaire tous les "chemins de module" (1 ou 2 segments) présents sous
    /applications/ et leur nombre d'URLs. Sert à proposer des corrections.

    Ex: 'sales', 'sales/crm', 'productivity/whatsapp'.
    """
    counts: dict[str, int] = defaultdict(int)
    for u in urls:
        segs = _segments_in_applications(u, version)
        if not segs:
            continue
        first = segs[0].replace(".html", "")
        counts[first] += 1
        if len(segs) >= 2:
            second = segs[1].replace(".html", "")
            counts[f"{first}/{second}"] += 1
    return dict(counts)


def _suggest_corrections(module: str, candidates: dict[str, int], top: int = 5) -> list[tuple[str, int]]:
    """Renvoie une liste de (path_candidat, n_urls) plausibles pour `module`."""
    module_lc = module.lower()
    tail = module_lc.rsplit("/", 1)[-1]
    scored: list[tuple[int, str, int]] = []
    for cand, n in candidates.items():
        cand_lc = cand.lower()
        if n == 0:
            continue
        score = 0
        if cand_lc == module_lc:
            score += 100
        if cand_lc.endswith("/" + tail) or cand_lc == tail:
            score += 60
        if tail in cand_lc:
            score += 30
        # Ressemblance préfixale (le module commence pareil)
        common = sum(1 for a, b in zip(cand_lc, module_lc) if a == b)
        score += common
        if score > 30:
            scored.append((score, cand, n))
    scored.sort(reverse=True)
    return [(c, n) for _, c, n in scored[:top]]


def validate(versions: tuple[str, ...] = VERSIONS_DEFAULT) -> dict:
    out: dict = {"versions": {}, "modules": []}

    urls_by_ver: dict[str, list[str]] = {}
    candidates_by_ver: dict[str, dict[str, int]] = {}
    for ver in versions:
        urls, source = discover_urls(ver)
        urls_by_ver[ver] = urls
        candidates_by_ver[ver] = _candidate_paths(urls, ver)
        out["versions"][ver] = {"source": source, "total_urls": len(urls)}

    for tier in ALL_TIERS:
        for module in STUDY_MODULES[tier]:
            row: dict = {
                "module": module,
                "tier": tier,
                "url_paths": url_paths_for(module),
                "v19_only": is_v19_only(module),
                "counts": {},
                "missing": [],
                "suggestions": {},
            }
            for ver in versions:
                # Pour les modules v19-only en v18, on attend 0.
                expected_zero = (ver == "18.0" and is_v19_only(module))
                total = 0
                for path in row["url_paths"]:
                    total += _count_urls_for_path(urls_by_ver[ver], ver, path)
                row["counts"][ver] = total
                if total == 0 and not expected_zero:
                    row["missing"].append(ver)
                    row["suggestions"][ver] = _suggest_corrections(module, candidates_by_ver[ver])
            out["modules"].append(row)
    return out


def _build_markdown(report: dict) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Validation des chemins de modules — sitemap Odoo",
        "",
        f"*Généré le {now}*",
        "",
        "## Sources",
        "",
    ]
    for ver, info in report["versions"].items():
        lines.append(f"- v{ver} : `{info['source']}` — **{info['total_urls']}** URLs totales (toutes sections).")
    lines.extend(["", "## Modules par tier", ""])

    by_tier: dict[str, list[dict]] = defaultdict(list)
    for m in report["modules"]:
        by_tier[m["tier"]].append(m)

    for tier in ALL_TIERS:
        rows = by_tier[tier]
        lines.append(f"### Tier `{tier}` ({len(rows)} modules)")
        lines.append("")
        lines.append("| Module | url_paths | v18 URLs | v19 URLs | Statut |")
        lines.append("|---|---|---:|---:|---|")
        for m in rows:
            up = ", ".join(f"`{p}`" for p in m["url_paths"])
            v18 = m["counts"].get("18.0", "—")
            v19 = m["counts"].get("19.0", "—")
            if m["v19_only"]:
                if m["counts"].get("19.0", 0) > 0 and m["counts"].get("18.0", 0) == 0:
                    status = "✅ v19-only OK"
                elif m["counts"].get("19.0", 0) > 0 and m["counts"].get("18.0", 0) > 0:
                    status = "⚠️ v18 inattendu"
                else:
                    status = "❌ absent v19"
            else:
                if m["counts"].get("18.0", 0) > 0 and m["counts"].get("19.0", 0) > 0:
                    status = "✅"
                elif m["counts"].get("18.0", 0) == 0 and m["counts"].get("19.0", 0) > 0:
                    status = "⚠️ v18 manquant"
                elif m["counts"].get("18.0", 0) > 0 and m["counts"].get("19.0", 0) == 0:
                    status = "⚠️ v19 manquant"
                else:
                    status = "❌ absent v18 et v19"
            lines.append(f"| `{m['module']}` | {up} | {v18} | {v19} | {status} |")
        lines.append("")

    # Section suggestions
    suspects = [m for m in report["modules"] if m["missing"]]
    if suspects:
        lines.append("## Modules à corriger")
        lines.append("")
        lines.append("Chemins suggérés (suggéré_path, n_urls trouvées) — à reporter dans "
                     "`app.study_modules.MODULE_URL_PATHS` si pertinent :")
        lines.append("")
        for m in suspects:
            lines.append(f"### `{m['module']}` (tier {m['tier']})")
            lines.append(f"Versions sans URL : {', '.join(m['missing'])}")
            for ver, sugg in m["suggestions"].items():
                if sugg:
                    sug_str = "; ".join(f"`{p}` ({n})" for p, n in sugg)
                    lines.append(f"- v{ver} suggestions : {sug_str}")
                else:
                    lines.append(f"- v{ver} : aucune suggestion (module probablement absent du sitemap)")
            lines.append("")
    else:
        lines.append("## ✅ Tous les modules ont des URLs valides en v18 et v19 (sauf v19-only attendus).")
        lines.append("")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Valider les chemins de modules contre les sitemaps Odoo")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_REPORT,
        help=f"Rapport markdown (défaut : {DEFAULT_REPORT})",
    )
    parser.add_argument(
        "--version",
        action="append",
        choices=["18.0", "19.0"],
        help="Restreindre à une version (répétable). Défaut : 18.0 et 19.0.",
    )
    args = parser.parse_args()

    versions = tuple(args.version) if args.version else VERSIONS_DEFAULT
    report = validate(versions=versions)
    md = _build_markdown(report)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(md, encoding="utf-8")

    print(md)
    print(f"\n→ Rapport écrit : {args.output}")
    n_suspects = sum(1 for m in report["modules"] if m["missing"])
    print(f"Modules à corriger : {n_suspects}")
    return 0 if n_suspects == 0 else 0  # ne fail pas — c'est un rapport diagnostic


if __name__ == "__main__":
    raise SystemExit(main())
