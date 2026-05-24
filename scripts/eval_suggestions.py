#!/usr/bin/env python3
"""
Harnais d'évaluation de la SUGGESTION de réponse (pipeline production).

Mesure l'exactitude de la suggestion Claude sur des questions de la banque dont
la bonne réponse est connue (vérité terrain = is_correct). Le pipeline réel est
utilisé : contexte RAG (banque + doc Odoo) + modèle answer_model, avec escalade
Opus optionnelle. **Leave-one-out** : la question testée est retirée de la banque
RAG pour éviter toute fuite (sinon elle serait son propre plus proche voisin).

Permet d'A/B tester : escalade on/off, modèle de base, modèle d'embedding
(changer anthropic.answer_model ou bank_rag.model dans config.json puis relancer).

Usage (depuis la racine du projet) :
    python3 -m scripts.eval_suggestions --limit 40 --seed 1
    python3 -m scripts.eval_suggestions --limit 40 --seed 1 --escalate
    python3 -m scripts.eval_suggestions --limit 40 --seed 1 --model claude-opus-4-6
    python3 -m scripts.eval_suggestions --limit 40 --version 19.0 --web

Options principales :
    --limit N        nombre de questions évaluées (défaut 40)
    --seed S         graine d'échantillonnage (défaut 0) — même seed = même échantillon
    --version V      filtre 18.0 | 19.0 | both | all (défaut all)
    --source SRC     filtre la source (claude, udemy, user, …) ; défaut toutes
    --escalate       active l'escalade Opus quand la confiance n'est pas haute
    --model M        force le modèle de base (ex. claude-opus-4-6) — A/B modèle
    --web            active les outils web (plus lent/coûteux, proche prod)
    --include-image  inclut les questions « à image » (l'image n'est PAS fournie)
    --out FILE       écrit le détail JSON (défaut data/eval_suggestions.json)
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

QUESTIONS_FILE = ROOT / "questions.json"

# Tarifs USD / million de jetons (doc Anthropic, mai 2026).
PRICES = {
    "opus": (5.0, 25.0),
    "sonnet": (3.0, 15.0),
    "haiku": (1.0, 5.0),
}


def _price_for(model: str) -> tuple[float, float]:
    m = (model or "").lower()
    if "opus" in m:
        return PRICES["opus"]
    if "haiku" in m:
        return PRICES["haiku"]
    return PRICES["sonnet"]


def _ground_truth_index(q: dict) -> int | None:
    cis = [i + 1 for i, a in enumerate(q.get("answers") or []) if a.get("is_correct")]
    return cis[0] if len(cis) == 1 else None


def _options(q: dict) -> list[str]:
    return [str((a.get("value") or "")).strip() for a in (q.get("answers") or [])]


def _src(q: dict) -> str:
    return str((q.get("correct_answer_source") or q.get("source") or "")).strip().lower()


def _target_version(q: dict) -> str:
    tv = q.get("target_version")
    tv = str(tv).strip() if tv is not None and str(tv).strip() else ""
    return tv or "19.0"


def main() -> None:
    ap = argparse.ArgumentParser(description="Éval de la suggestion de réponse (leave-one-out).")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--version", default="all", choices=["18.0", "19.0", "both", "all"])
    ap.add_argument("--source", default="",
                    help="filtre la source du jeu de TEST (ex. udemy)")
    ap.add_argument("--rag-exclude-source", default="",
                    help="retire ces sources de la BANQUE RAG (liste séparée par virgule, "
                         "ex. udemy) pour éviter que la question testée se retrouve via un doublon")
    ap.add_argument("--rag-bank-fraction", type=float, default=1.0,
                    help="ablation : ne garder qu'une fraction (0-1) de la banque RAG (sous-"
                         "échantillon fixe, graine seed+7) pour mesurer l'effet de la TAILLE de banque")
    ap.add_argument("--escalate", action="store_true")
    ap.add_argument("--concurrency", type=int, default=1,
                    help="nombre d'évaluations en parallèle (les appels API, lents, sont "
                         "concurrents ; ex. 5 pour ~5× plus rapide sur un gros run)")
    ap.add_argument("--model", default="")
    ap.add_argument("--web", action="store_true")
    ap.add_argument("--include-image", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "data" / "eval_suggestions.json"))
    args = ap.parse_args()

    from app.llm import api_available, escalation_model, reponse_to_correct_index, suggest_answer
    from app.rag import build_context

    if not api_available():
        sys.exit("❌ Clé API Anthropic absente (config.json → anthropic.api_key).")

    bank = json.loads(QUESTIONS_FILE.read_text(encoding="utf-8")).get("questions", [])
    by_id = {q.get("id"): q for q in bank if isinstance(q, dict) and q.get("id") is not None}

    def evaluable(q: dict) -> bool:
        if _ground_truth_index(q) is None:
            return False
        if len([o for o in _options(q) if o]) < 2:
            return False
        if not (q.get("title") or "").strip():
            return False
        if not args.include_image and q.get("needs_question_image"):
            return False
        if args.version != "all" and _target_version(q) != args.version:
            return False
        if args.source and (q.get("correct_answer_source") or q.get("source")) != args.source:
            return False
        return True

    pool = [q for q in bank if isinstance(q, dict) and evaluable(q)]
    rng = random.Random(args.seed)
    rng.shuffle(pool)
    sample = pool[: args.limit]

    esc_model = escalation_model() if args.escalate else ""
    rank = {"basse": 0, "moyenne": 1, "haute": 2}
    exclude_src = {s.strip().lower() for s in args.rag_exclude_source.split(",") if s.strip()}
    # Ablation taille de banque : sous-ensemble fixe d'ids conservés (None = tout garder).
    keep_ids: set | None = None
    if args.rag_bank_fraction < 1.0:
        eligible = [x.get("id") for x in bank if _src(x) not in exclude_src and x.get("id") is not None]
        random.Random(args.seed + 7).shuffle(eligible)
        keep_ids = set(eligible[: max(1, int(len(eligible) * args.rag_bank_fraction))])
    rag_bank_size = len([x for x in bank if _src(x) not in exclude_src
                         and (keep_ids is None or x.get("id") in keep_ids)])

    print(f"📋 Banque : {len(bank)} questions ; {len(pool)} évaluables (filtres appliqués).")
    print(f"   Jeu de test : source={args.source or 'toutes'} | échantillon={len(sample)} (seed={args.seed})")
    print(f"   Banque RAG  : {rag_bank_size} questions"
          f"{' (sources exclues: '+','.join(sorted(exclude_src))+')' if exclude_src else ''}")
    print(f"   Modèle base={args.model or 'config'} | web={'on' if args.web else 'off'} "
          f"| escalade={'on ('+esc_model+')' if esc_model else 'off'}\n")

    results = []
    tok_in: dict[str, int] = {}
    tok_out: dict[str, int] = {}
    t_start = time.perf_counter()

    def _eval_one(q: dict) -> dict:
        qid = q.get("id")
        gt = _ground_truth_index(q)
        opts = _options(q)
        tv = _target_version(q)
        bank_loo = [
            x for x in bank
            if x.get("id") != qid and _src(x) not in exclude_src
            and (keep_ids is None or x.get("id") in keep_ids)
        ]
        toks: list[tuple] = []
        try:
            ctx = build_context(q.get("title") or "", opts, question_bank=bank_loo, target_version=tv)
            sugg, meta = suggest_answer(
                q.get("title") or "", opts, ctx,
                question_id=qid, use_web_tools=args.web, target_version=tv,
                model=(args.model or None),
            )
            ci = reponse_to_correct_index(sugg.reponse, len(opts))
            conf = sugg.confiance
            used_model = meta.get("model") or args.model or "?"
            toks.append((used_model, int(meta.get("input_tokens") or 0), int(meta.get("output_tokens") or 0)))
            escalated = False
            if esc_model and (conf != "haute" or ci is None):
                sugg2, meta2 = suggest_answer(
                    q.get("title") or "", opts, ctx,
                    question_id=qid, use_web_tools=args.web, target_version=tv, model=esc_model,
                )
                ci2 = reponse_to_correct_index(sugg2.reponse, len(opts))
                m2 = meta2.get("model") or esc_model
                toks.append((m2, int(meta2.get("input_tokens") or 0), int(meta2.get("output_tokens") or 0)))
                if ci2 is not None and rank.get(sugg2.confiance, 0) >= rank.get(conf, 0):
                    ci, conf, used_model, escalated = ci2, sugg2.confiance, m2, True
            return {"id": qid, "ok": (ci == gt), "suggested": ci, "truth": gt,
                    "confiance": conf, "model": used_model, "escalated": escalated,
                    "tier": q.get("tier"), "version": tv, "_toks": toks}
        except Exception as e:
            return {"id": qid, "error": str(e)[:160], "truth": gt, "_toks": toks}

    from concurrent.futures import ThreadPoolExecutor, as_completed

    done_n = 0
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
        futs = [ex.submit(_eval_one, q) for q in sample]
        for fut in as_completed(futs):
            r = fut.result()
            done_n += 1
            for (m, ti, to) in r.pop("_toks", []):
                tok_in[m] = tok_in.get(m, 0) + ti
                tok_out[m] = tok_out.get(m, 0) + to
            results.append(r)
            if "error" in r:
                print(f"[{done_n}/{len(sample)}] #{r['id']} ⚠️ {r['error'][:80]}", flush=True)
            else:
                print(f"[{done_n}/{len(sample)}] #{r['id']} {'✅' if r['ok'] else '❌'} "
                      f"(sugg={r['suggested']} vrai={r['truth']} conf={r['confiance']}"
                      f"{' ↑opus' if r['escalated'] else ''})", flush=True)

    n_correct = sum(1 for r in results if r.get("ok"))
    elapsed = time.perf_counter() - t_start
    done = [r for r in results if "error" not in r]
    acc = (n_correct / len(done) * 100) if done else 0.0

    # Coût estimé
    cost = 0.0
    for m in set(list(tok_in) + list(tok_out)):
        pin, pout = _price_for(m)
        cost += tok_in.get(m, 0) / 1e6 * pin + tok_out.get(m, 0) / 1e6 * pout

    # Ventilation par version / par confiance
    def acc_by(key: str) -> dict:
        groups: dict[str, list[bool]] = {}
        for r in done:
            groups.setdefault(str(r.get(key)), []).append(bool(r["ok"]))
        return {k: f"{sum(v)}/{len(v)} ({round(sum(v)/len(v)*100)}%)" for k, v in sorted(groups.items())}

    print("\n" + "=" * 56)
    print(f"ACCURACY : {n_correct}/{len(done)} = {acc:.1f}%   (erreurs: {len(results)-len(done)})")
    print(f"Durée : {elapsed:.0f}s ({elapsed/max(1,len(sample)):.1f}s/question)")
    print(f"Jetons : { {m: (tok_in.get(m,0), tok_out.get(m,0)) for m in set(list(tok_in)+list(tok_out))} }")
    print(f"Coût estimé : ${cost:.3f}")
    print(f"Par version  : {acc_by('version')}")
    print(f"Par tier     : {acc_by('tier')}")
    print(f"Par confiance: {acc_by('confiance')}")
    print("=" * 56)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "params": vars(args), "accuracy_pct": round(acc, 1),
        "n_correct": n_correct, "n_done": len(done), "elapsed_s": round(elapsed, 1),
        "cost_usd_est": round(cost, 3), "tokens_in": tok_in, "tokens_out": tok_out,
        "results": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Détail écrit : {out_path}")


if __name__ == "__main__":
    main()
