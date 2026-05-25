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
import os
import random
import sys
import time
from datetime import datetime
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
    ap.add_argument("--holdout-sample", action="store_true",
                    help="HELD-OUT : exclut TOUT l'échantillon de test de la banque RAG (les N "
                         "questions testées ET leurs réponses) au lieu du simple leave-one-out. "
                         "Les autres questions (autres Udemy, générées, doc) restent disponibles.")
    ap.add_argument("--warm-index", dest="warm_index", action="store_true", default=True,
                    help="réchauffe l'index vectoriel banque (défaut on) — sinon repli lexical.")
    ap.add_argument("--no-warm-index", dest="warm_index", action="store_false")
    ap.add_argument("--query-timeout-s", type=float, default=60.0,
                    help="timeout d'embedding requête pendant l'éval (généreux : batch offline).")
    ap.add_argument("--time-budget-min", type=int, default=0,
                    help="plafond global en minutes (0 = illimité) : arrête de collecter au-delà "
                         "et rapporte les résultats partiels.")
    ap.add_argument("--abstain-below", choices=["none", "basse", "moyenne"], default="basse",
                    help="abstention (0 pt au lieu de -0.5) quand la confiance est <= ce niveau. "
                         "Défaut 'basse'. EV(répondre)=1.5p-0.5 > 0 ssi p>1/3, donc on s'abstient "
                         "seulement quand la confiance/justesse est trop faible.")
    ap.add_argument("--escalate-cutoff-frac", type=float, default=0.75,
                    help="n'escalade (Opus) que tant que le temps écoulé < cette fraction du "
                         "budget global ; au-delà, on garde la réponse de base pour ne pas "
                         "dépasser le temps imparti.")
    ap.add_argument("--verbose", action="store_true",
                    help="affichage temps réel : pour chaque question, énoncé + réponse de "
                         "Claude + bonne réponse banque (pratique avec tail -f, concurrency basse).")
    ap.add_argument("--progress-file", default="",
                    help="écrit la progression incrémentale (JSON) après chaque question, pour "
                         "le tableau de bord web live (/eval-live).")
    ap.add_argument("--label", default="", help="libellé lisible de l'éval (affiché dans le hub).")
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

    # Held-out : on retire TOUT l'échantillon de test du RAG (pas seulement la
    # question courante), pour tester en aveugle ces N tout en gardant le reste.
    holdout = bool(args.holdout_sample)
    test_ids = {q.get("id") for q in sample}

    # Index vectoriel : warm-up (charge le cache mxbai) + timeout requête généreux,
    # sinon le RAG banque retombe sur le lexical au lieu d'utiliser les embeddings.
    if args.warm_index:
        try:
            import bank_embeddings as _be
            _be._query_timeout_s = lambda: float(args.query_timeout_s)
            _be.QUERY_EMBED_TIMEOUT_S = float(args.query_timeout_s)
            ok_idx = _be.warmup_bank_embeddings(bank)
            idx = _be.get_bank_vector_index()
            print(f"   Index vectoriel : {'prêt ('+idx.model_name+', dim '+str(idx.matrix.shape[1])+')' if (ok_idx and idx) else 'indisponible → repli lexical'}")
        except Exception as e:
            print(f"   Index vectoriel : échec warmup ({e}) → repli lexical")

    esc_model = escalation_model() if args.escalate else ""
    rank = {"basse": 0, "moyenne": 1, "haute": 2}
    abstain_level = {"none": -1, "basse": 0, "moyenne": 1}[args.abstain_below]
    n_test = len(sample)
    exclude_src = {s.strip().lower() for s in args.rag_exclude_source.split(",") if s.strip()}
    # Ablation taille de banque : sous-ensemble fixe d'ids conservés (None = tout garder).
    keep_ids: set | None = None
    if args.rag_bank_fraction < 1.0:
        eligible = [x.get("id") for x in bank if _src(x) not in exclude_src and x.get("id") is not None]
        random.Random(args.seed + 7).shuffle(eligible)
        keep_ids = set(eligible[: max(1, int(len(eligible) * args.rag_bank_fraction))])
    rag_bank_size = len([x for x in bank if _src(x) not in exclude_src
                         and not (holdout and x.get("id") in test_ids)
                         and (keep_ids is None or x.get("id") in keep_ids)])

    print(f"📋 Banque : {len(bank)} questions ; {len(pool)} évaluables (filtres appliqués).")
    print(f"   Jeu de test : source={args.source or 'toutes'} | échantillon={len(sample)} (seed={args.seed})")
    print(f"   Banque RAG  : {rag_bank_size} questions"
          f"{' (sources exclues: '+','.join(sorted(exclude_src))+')' if exclude_src else ''}")
    if holdout:
        other_udemy = len([x for x in bank if _src(x) == "udemy" and x.get("id") not in test_ids])
        print(f"   Mode HELD-OUT : les {len(test_ids)} questions de test sont retirées du RAG ; "
              f"{other_udemy} autres Udemy restent disponibles dans le contexte.")
    print(f"   Modèle base={args.model or 'config'} | web={'on' if args.web else 'off'} "
          f"| escalade={'on ('+esc_model+')' if esc_model else 'off'}\n")

    results = []
    tok_in: dict[str, int] = {}
    tok_out: dict[str, int] = {}
    t_start = time.perf_counter()
    started_at = datetime.now().isoformat(timespec="seconds")

    progress_path = Path(args.progress_file) if args.progress_file else None

    def _write_progress(status: str = "running") -> None:
        """Écrit l'état courant (JSON atomique) pour le tableau de bord web live."""
        if progress_path is None:
            return
        done_r = [r for r in results if "error" not in r]
        errs = [r for r in results if "error" in r]
        tmo = [r for r in errs if r.get("timeout")]

        def _absta(r: dict) -> bool:
            return rank.get(r.get("confiance"), 0) <= abstain_level

        ans = [r for r in done_r if not _absta(r)]
        absd = [r for r in done_r if _absta(r)]
        ac = sum(1 for r in ans if r.get("ok"))
        aw = len(ans) - ac
        allc = sum(1 for r in done_r if r.get("ok"))
        allw = len(done_r) - allc
        cost_now = 0.0
        for m in set(list(tok_in) + list(tok_out)):
            pin, pout = _price_for(m)
            cost_now += tok_in.get(m, 0) / 1e6 * pin + tok_out.get(m, 0) / 1e6 * pout
        rows = []
        for r in results:
            err = "error" in r
            rows.append({
                "seq": r.get("seq"), "id": r.get("id"),
                "title": r.get("title"), "version": r.get("version"),
                "module": r.get("module"), "confiance": r.get("confiance"),
                "escalated": r.get("escalated"), "latency_s": r.get("latency_s"),
                "suggested": r.get("suggested"), "truth": r.get("truth"),
                "suggested_text": r.get("suggested_text"), "truth_text": r.get("truth_text"),
                "options": r.get("options"),
                "ok": r.get("ok"), "abstain": (None if err else _absta(r)),
                "error": r.get("error"), "timeout": r.get("timeout"),
            })
        payload = {
            "status": status,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "n_test": n_test, "done": len(results),
            "params": {"model": args.model or "config", "escalate": bool(esc_model),
                       "holdout": holdout, "abstain_below": args.abstain_below,
                       "concurrency": args.concurrency, "budget_min": args.time_budget_min,
                       "source": args.source, "seed": args.seed, "limit": args.limit,
                       "label": args.label, "started_at": started_at,
                       "rag_exclude": args.rag_exclude_source, "version": args.version},
            "totals": {"answered": len(ans), "abstained": len(absd),
                       "correct": ac, "wrong": aw,
                       "accuracy_answered": round(ac / len(ans) * 100, 1) if ans else 0.0,
                       "odoo_abstain": round(ac * 1.0 - aw * 0.5, 1),
                       "odoo_all": round(allc * 1.0 - allw * 0.5, 1),
                       "odoo_max": n_test,
                       "timeouts": len(tmo), "api_errors": len(errs),
                       "cost_usd": round(cost_now, 3),
                       "avg_latency_s": round(
                           sum(float(r.get("latency_s") or 0) for r in done_r) / len(done_r), 2)
                           if done_r else 0.0,
                       "elapsed_s": round(time.perf_counter() - t_start, 1)},
            "results": rows,
        }
        try:
            tmp = progress_path.with_name(progress_path.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, progress_path)
        except OSError:
            pass

    if progress_path is not None:
        _write_progress("running")

    def _eval_one(q: dict) -> dict:
        qid = q.get("id")
        gt = _ground_truth_index(q)
        opts = _options(q)
        tv = _target_version(q)
        # Held-out : on exclut tout l'échantillon de test ; sinon leave-one-out.
        excl_ids = test_ids if holdout else {qid}
        bank_loo = [
            x for x in bank
            if x.get("id") not in excl_ids and _src(x) not in exclude_src
            and (keep_ids is None or x.get("id") in keep_ids)
        ]

        def _txt(idx: int | None) -> str | None:
            return opts[idx - 1] if isinstance(idx, int) and 1 <= idx <= len(opts) else None

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
            lat = float(meta.get("latency_s") or 0)
            toks.append((used_model, int(meta.get("input_tokens") or 0), int(meta.get("output_tokens") or 0)))
            escalated = False
            # Escalade Opus seulement si le budget global le permet (sinon on garde la base
            # pour ne pas dépasser le temps imparti — "optimiser le temps de réflexion").
            esc_allowed = (budget_s is None) or ((time.perf_counter() - t_start) < budget_s * args.escalate_cutoff_frac)
            if esc_model and esc_allowed and (conf != "haute" or ci is None):
                sugg2, meta2 = suggest_answer(
                    q.get("title") or "", opts, ctx,
                    question_id=qid, use_web_tools=args.web, target_version=tv, model=esc_model,
                )
                ci2 = reponse_to_correct_index(sugg2.reponse, len(opts))
                m2 = meta2.get("model") or esc_model
                lat += float(meta2.get("latency_s") or 0)
                toks.append((m2, int(meta2.get("input_tokens") or 0), int(meta2.get("output_tokens") or 0)))
                if ci2 is not None and rank.get(sugg2.confiance, 0) >= rank.get(conf, 0):
                    ci, conf, used_model, escalated = ci2, sugg2.confiance, m2, True
            return {"id": qid, "ok": (ci == gt), "suggested": ci, "truth": gt,
                    "latency_s": round(lat, 2),
                    "suggested_text": _txt(ci), "truth_text": _txt(gt),
                    "options": opts,
                    "title": (q.get("title") or "").strip(),
                    "module": q.get("module"),
                    "confiance": conf, "model": used_model, "escalated": escalated,
                    "tier": q.get("tier"), "version": tv, "_toks": toks}
        except Exception as e:
            msg = str(e)
            low = msg.lower()
            is_timeout = any(k in low for k in (
                "timeout", "timed out", "déla", "delai", "read timed out",
                "deadline", "etimedout", "503", "529", "overloaded"))
            return {"id": qid, "error": msg[:200], "timeout": is_timeout, "truth": gt,
                    "options": opts, "title": (q.get("title") or "").strip(), "_toks": toks}

    from concurrent.futures import ThreadPoolExecutor, as_completed

    budget_s = args.time_budget_min * 60 if args.time_budget_min > 0 else None
    stopped_budget = False
    done_n = 0
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
        futs = [ex.submit(_eval_one, q) for q in sample]
        for fut in as_completed(futs):
            r = fut.result()
            done_n += 1
            r["seq"] = done_n
            for (m, ti, to) in r.pop("_toks", []):
                tok_in[m] = tok_in.get(m, 0) + ti
                tok_out[m] = tok_out.get(m, 0) + to
            results.append(r)
            if "error" in r:
                tag = "⏱️ TIMEOUT" if r.get("timeout") else "⚠️"
                print(f"[{done_n}/{len(sample)}] #{r['id']} {tag} {r['error'][:80]}", flush=True)
            else:
                mark = "✅" if r["ok"] else "❌"
                print(f"[{done_n}/{len(sample)}] #{r['id']} {mark} "
                      f"(sugg={r['suggested']} vrai={r['truth']} conf={r['confiance']}"
                      f"{' ↑opus' if r['escalated'] else ''}{' ' + str(r.get('latency_s'))+'s' if r.get('latency_s') else ''})",
                      flush=True)
                if args.verbose:
                    print(f"      Q [v{str(r.get('version','')).replace('.0','')}/{r.get('module') or '-'}] : "
                          f"{(r.get('title') or '')[:160]}", flush=True)
                    print(f"      🤖 Claude : {r.get('suggested_text')}", flush=True)
                    print(f"      ✅ Banque : {r.get('truth_text')}", flush=True)
                    print("      " + "-" * 50, flush=True)
            _write_progress("running")
            if budget_s and (time.perf_counter() - t_start) > budget_s:
                stopped_budget = True
                print(f"⏱️ Budget {args.time_budget_min} min atteint — arrêt "
                      f"({done_n}/{len(sample)} traitées, résultats partiels).", flush=True)
                for f in futs:
                    f.cancel()
                break

    elapsed = time.perf_counter() - t_start
    n_test = len(sample)
    done = [r for r in results if "error" not in r]          # avec suggestion exploitable
    errs_api = [r for r in results if "error" in r]
    timeouts = [r for r in errs_api if r.get("timeout")]
    n_correct = sum(1 for r in done if r.get("ok"))
    acc = (n_correct / len(done) * 100) if done else 0.0

    # --- Abstention : 0 pt (mieux que -0.5) si confiance <= seuil --abstain-below ---
    rank = {"basse": 0, "moyenne": 1, "haute": 2}
    abstain_level = {"none": -1, "basse": 0, "moyenne": 1}[args.abstain_below]

    def is_abstain(r: dict) -> bool:
        return rank.get(r.get("confiance"), 0) <= abstain_level

    answered = [r for r in done if not is_abstain(r)]
    abstained = [r for r in done if is_abstain(r)]
    ans_correct = sum(1 for r in answered if r.get("ok"))
    ans_wrong = len(answered) - ans_correct

    def odoo(c: int, w: int) -> float:
        return c * 1.0 - w * 0.5

    score_abstain = odoo(ans_correct, ans_wrong)            # avec abstention
    score_all = odoo(n_correct, len(done) - n_correct)      # si on répond à tout

    # Coût estimé
    cost = 0.0
    for m in set(list(tok_in) + list(tok_out)):
        pin, pout = _price_for(m)
        cost += tok_in.get(m, 0) / 1e6 * pin + tok_out.get(m, 0) / 1e6 * pout

    def acc_by(key: str) -> dict:
        groups: dict[str, list[bool]] = {}
        for r in done:
            groups.setdefault(str(r.get(key)), []).append(bool(r["ok"]))
        return {k: f"{sum(v)}/{len(v)} ({round(sum(v)/len(v)*100)}%)" for k, v in sorted(groups.items())}

    lats = sorted(float(r.get("latency_s") or 0) for r in done)

    def _pct(p: float) -> float:
        return lats[min(len(lats) - 1, int(len(lats) * p))] if lats else 0.0

    print("\n" + "=" * 64)
    print(f"RÉSULTAT — held-out {n_test} Udemy en aveugle (autres Udemy + générées + doc gardées)")
    print(f"  Traitées : {len(done)} avec suggestion | {len(errs_api)} erreurs API "
          f"(dont {len(timeouts)} timeouts)")
    print(f"  Exactitude brute (toutes suggestions) : {n_correct}/{len(done)} = {acc:.1f}%")
    print("-" * 64)
    print(f"  Politique d'abstention : confiance <= '{args.abstain_below}' → s'abstenir (0 pt)")
    print(f"  Répondu : {len(answered)}  ({ans_correct} bonnes, {ans_wrong} mauvaises)  |  "
          f"Abstentions : {len(abstained)}")
    if answered:
        print(f"  Exactitude sur répondu : {ans_correct}/{len(answered)} = {round(ans_correct/len(answered)*100)}%")
    print(f"  SCORE Odoo (+1 / -0.5 / 0) AVEC abstention : {score_abstain:+.1f} / {n_test} "
          f"({round(score_abstain/max(1,n_test)*100)}% du max)")
    print(f"  SCORE Odoo si on répond à TOUT            : {score_all:+.1f} / {n_test}")
    print("-" * 64)
    print("  Par confiance — n | exactitude | EV/question si on répond (EV=1.5p-0.5) :")
    for lvl in ("haute", "moyenne", "basse"):
        grp = [r for r in done if r.get("confiance") == lvl]
        if not grp:
            continue
        c = sum(1 for r in grp if r.get("ok"))
        n = len(grp)
        a = c / n
        ev = 1.5 * a - 0.5
        print(f"     {lvl:8s}: {n:3d} | {c}/{n} ({round(a*100)}%) | EV={ev:+.2f} "
              f"{'→ répondre' if ev > 0 else '→ s’abstenir'}")
    print("-" * 64)
    print(f"  Par version : {acc_by('version')}")
    print(f"  Par tier    : {acc_by('tier')}")
    print(f"  Temps : total {elapsed:.0f}s | /question moy {(sum(lats)/len(lats) if lats else 0):.1f}s "
          f"médiane {_pct(0.5):.1f}s p90 {_pct(0.9):.1f}s max {(max(lats) if lats else 0):.1f}s")
    if budget_s:
        print(f"  Budget global : {args.time_budget_min} min — "
              f"{'⚠️ ATTEINT (arrêt anticipé)' if stopped_budget else 'respecté'}")
    print(f"  Coût estimé : ${cost:.3f}")
    print("=" * 64)

    # --- Timeouts détectés ---
    if timeouts:
        print(f"\n=== ⏱️ TIMEOUTS détectés : {len(timeouts)} ===")
        for r in timeouts:
            print(f"   #{r['id']} : {r['error'][:120]}")

    # --- Détail de TOUTES les erreurs répondues : banque vs Claude ---
    wrongs = [r for r in answered if not r.get("ok")]
    print(f"\n=== ERREURS RÉPONDUES (détail) : {len(wrongs)} — bonne réponse banque vs suggestion Claude ===")
    for r in sorted(wrongs, key=lambda x: (str(x.get("version")), str(x.get("module")))):
        v = str(r.get("version", "")).replace(".0", "")
        print(f"#{r['id']} [v{v}] {r.get('module') or '-'} | conf={r.get('confiance')}"
              f"{' ↑opus' if r.get('escalated') else ''} | {r.get('latency_s')}s")
        print(f"   Q : {(r.get('title') or '')[:160]}")
        print(f"   ✅ banque : {r.get('truth_text')}")
        print(f"   ❌ Claude : {r.get('suggested_text')}")

    # --- Abstentions (auraient rapporté quoi ?) ---
    if abstained:
        would_ok = sum(1 for r in abstained if r.get("ok"))
        print(f"\n=== ABSTENTIONS : {len(abstained)} (confiance <= {args.abstain_below}) "
              f"— dont {would_ok} auraient été justes / {len(abstained)-would_ok} fausses ===")
        for r in sorted(abstained, key=lambda x: str(x.get("confiance"))):
            print(f"   #{r['id']} conf={r.get('confiance')} "
                  f"({'aurait eu juste' if r.get('ok') else 'aurait eu faux'}) : "
                  f"{(r.get('title') or '')[:100]}")

    if errs_api:
        print(f"\n=== ERREURS API : {len(errs_api)} (non scorées) ===")
        for r in errs_api:
            print(f"   #{r['id']}{' ⏱️' if r.get('timeout') else ''} : {r['error'][:140]}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "params": vars(args),
        "n_test": n_test, "n_done": len(done),
        "accuracy_pct": round(acc, 1),
        "n_correct": n_correct,
        "abstain_below": args.abstain_below,
        "answered": len(answered), "answered_correct": ans_correct, "answered_wrong": ans_wrong,
        "abstained": len(abstained),
        "odoo_score_abstain": score_abstain, "odoo_score_answer_all": score_all,
        "odoo_max": n_test,
        "n_timeouts": len(timeouts), "n_api_errors": len(errs_api),
        "stopped_budget": stopped_budget,
        "elapsed_s": round(elapsed, 1),
        "latency_per_q": {"mean": round(sum(lats)/len(lats), 2) if lats else 0,
                           "median": round(_pct(0.5), 2), "p90": round(_pct(0.9), 2),
                           "max": round(max(lats), 2) if lats else 0},
        "cost_usd_est": round(cost, 3), "tokens_in": tok_in, "tokens_out": tok_out,
        "results": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nDétail JSON écrit : {out_path}")
    _write_progress("done")


if __name__ == "__main__":
    main()
