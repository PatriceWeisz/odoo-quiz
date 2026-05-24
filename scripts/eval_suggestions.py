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


def _target_version(q: dict) -> str:
    tv = q.get("target_version")
    tv = str(tv).strip() if tv is not None and str(tv).strip() else ""
    return tv or "19.0"


def main() -> None:
    ap = argparse.ArgumentParser(description="Éval de la suggestion de réponse (leave-one-out).")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--version", default="all", choices=["18.0", "19.0", "both", "all"])
    ap.add_argument("--source", default="")
    ap.add_argument("--escalate", action="store_true")
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

    print(f"📋 Banque : {len(bank)} questions ; {len(pool)} évaluables (filtres appliqués).")
    print(f"   Échantillon : {len(sample)} (seed={args.seed}) | modèle base={args.model or 'config'} "
          f"| web={'on' if args.web else 'off'} | escalade={'on ('+esc_model+')' if esc_model else 'off'}\n")

    results = []
    n_correct = 0
    tok_in: dict[str, int] = {}
    tok_out: dict[str, int] = {}
    t_start = time.perf_counter()

    for i, q in enumerate(sample, 1):
        qid = q.get("id")
        gt = _ground_truth_index(q)
        opts = _options(q)
        tv = _target_version(q)
        bank_loo = [x for x in bank if x.get("id") != qid]
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
            tok_in[used_model] = tok_in.get(used_model, 0) + int(meta.get("input_tokens") or 0)
            tok_out[used_model] = tok_out.get(used_model, 0) + int(meta.get("output_tokens") or 0)
            escalated = False
            if esc_model and (conf != "haute" or ci is None):
                sugg2, meta2 = suggest_answer(
                    q.get("title") or "", opts, ctx,
                    question_id=qid, use_web_tools=args.web, target_version=tv, model=esc_model,
                )
                ci2 = reponse_to_correct_index(sugg2.reponse, len(opts))
                m2 = meta2.get("model") or esc_model
                tok_in[m2] = tok_in.get(m2, 0) + int(meta2.get("input_tokens") or 0)
                tok_out[m2] = tok_out.get(m2, 0) + int(meta2.get("output_tokens") or 0)
                if ci2 is not None and rank.get(sugg2.confiance, 0) >= rank.get(conf, 0):
                    ci, conf, used_model, escalated = ci2, sugg2.confiance, m2, True
            ok = (ci == gt)
            n_correct += 1 if ok else 0
            results.append({"id": qid, "ok": ok, "suggested": ci, "truth": gt,
                            "confiance": conf, "model": used_model, "escalated": escalated,
                            "tier": q.get("tier"), "version": tv})
            print(f"[{i}/{len(sample)}] #{qid} {'✅' if ok else '❌'} "
                  f"(sugg={ci} vrai={gt} conf={conf}{' ↑opus' if escalated else ''})")
        except Exception as e:
            results.append({"id": qid, "error": str(e)[:160], "truth": gt})
            print(f"[{i}/{len(sample)}] #{qid} ⚠️ {str(e)[:80]}")

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
