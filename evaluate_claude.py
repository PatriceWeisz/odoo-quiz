#!/usr/bin/env python3
"""
Évaluation aveugle de Claude sur les questions du quiz.
Claude reçoit uniquement la question + les choix de réponse (sans is_correct).
On compare sa réponse aux bonnes réponses du fichier.
Résultats sauvegardés dans evaluation.json.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

QUESTIONS_FILE = Path(__file__).parent / "questions.json"
EVAL_FILE = Path(__file__).parent / "evaluation.json"


def load_questions():
    if not QUESTIONS_FILE.exists():
        sys.exit("❌ questions.json introuvable.")
    with open(QUESTIONS_FILE, encoding="utf-8") as f:
        return json.load(f).get("questions", [])


def load_eval():
    if EVAL_FILE.exists():
        with open(EVAL_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_eval(results):
    with open(EVAL_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)


def has_correct(q):
    return any(a.get("is_correct") for a in q.get("answers", []))


def claude(prompt):
    result = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Erreur CLI claude")
    return result.stdout.strip()


def blind_prompt(q):
    """Prompt sans aucune indication de la bonne réponse."""
    answers = q.get("answers", [])
    lines = "\n".join(f"  {i+1}. {a['value']}" for i, a in enumerate(answers))
    return f"""Tu passes la certification Odoo. Réponds à cette question en choisissant parmi les options proposées.

Question : {q['title']}

Options :
{lines}

Réponds UNIQUEMENT dans ce format (rien d'autre) :
REPONSE: <numéro(s) séparés par virgule si tu penses qu'il y en a plusieurs, ex: 2 ou 1,3>"""


def parse_response(text, n_answers):
    m = re.search(r"REPONSE\s*:\s*(.+)", text)
    if not m:
        return []
    nums = re.findall(r"\d+", m.group(1))
    return [int(n) - 1 for n in nums if 0 < int(n) <= n_answers]


def compare(claude_indices, q):
    answers = q.get("answers", [])
    correct_indices = {i for i, a in enumerate(answers) if a.get("is_correct")}
    claude_set = set(claude_indices)
    return claude_set == correct_indices, correct_indices, claude_set


def print_report(results, questions):
    qmap = {str(q["id"]): q for q in questions}
    total = len(results)
    correct = sum(1 for r in results.values() if r.get("correct"))
    wrong = sum(1 for r in results.values() if not r.get("correct") and r.get("claude_indices") is not None)
    errors = sum(1 for r in results.values() if r.get("error"))

    print(f"\n{'='*60}")
    print(f"RÉSULTATS DE L'ÉVALUATION AVEUGLE DE CLAUDE")
    print(f"{'='*60}")
    print(f"Questions évaluées : {total}")
    print(f"✅ Bonnes réponses : {correct} ({round(correct/total*100) if total else 0}%)")
    print(f"❌ Mauvaises réponses : {wrong} ({round(wrong/total*100) if total else 0}%)")
    print(f"⚠️  Erreurs/timeouts : {errors}")
    print(f"{'='*60}")

    wrong_list = [(qid, r) for qid, r in results.items() if not r.get("correct") and not r.get("error")]
    if wrong_list:
        print(f"\n❌ Questions ratées ({len(wrong_list)}) :\n")
        for qid, r in wrong_list[:20]:
            q = qmap.get(qid, {})
            answers = q.get("answers", [])
            correct_vals = [answers[i]["value"] for i in r.get("correct_indices", []) if i < len(answers)]
            claude_vals = [answers[i]["value"] for i in r.get("claude_indices", []) if i < len(answers)]
            print(f"  Q: {q.get('title', qid)[:70]}")
            print(f"     ✅ Fichier : {', '.join(correct_vals) or '?'}")
            print(f"     🤖 Claude : {', '.join(claude_vals) or '?'}")
            print()
        if len(wrong_list) > 20:
            print(f"  … et {len(wrong_list)-20} autres (voir evaluation.json)")


def main():
    questions = load_questions()
    evaluable = [q for q in questions if has_correct(q) and q.get("answers")]
    total_q = len(questions)

    print(f"📋 {total_q} questions au total.")
    print(f"   {len(evaluable)} ont une bonne réponse connue → évaluables.")
    print(f"   {total_q - len(evaluable)} sans bonne réponse → ignorées.")

    if not evaluable:
        sys.exit("❌ Aucune question évaluable. Lance d'abord generate_explanations.py.")

    results = load_eval()
    to_do = [q for q in evaluable if str(q["id"]) not in results]
    print(f"   {len(results)} déjà évaluées, {len(to_do)} restantes.\n")

    if not to_do:
        print("✅ Évaluation déjà complète.")
        print_report(results, questions)
        return

    try:
        subprocess.run(["claude", "--version"], capture_output=True, check=True)
    except FileNotFoundError:
        sys.exit("❌ CLI `claude` introuvable.")
    except subprocess.CalledProcessError:
        pass

    errors = 0
    for i, q in enumerate(to_do):
        pct = round(((i + 1) / len(to_do)) * 100)
        print(f"[{i+1}/{len(to_do)} — {pct}%] {q['title'][:65]}…", end=" ", flush=True)

        qid = str(q["id"])
        try:
            text = claude(blind_prompt(q))
            claude_indices = parse_response(text, len(q["answers"]))
            is_correct, correct_set, claude_set = compare(claude_indices, q)

            results[qid] = {
                "correct": is_correct,
                "correct_indices": list(correct_set),
                "claude_indices": claude_indices,
                "raw": text,
            }
            print("✅" if is_correct else "❌")

        except subprocess.TimeoutExpired:
            results[qid] = {"error": "timeout"}
            print("⏱ timeout.")
            errors += 1
        except Exception as e:
            results[qid] = {"error": str(e)}
            print(f"⚠️  {e}")
            errors += 1

        if (i + 1) % 10 == 0:
            save_eval(results)

    save_eval(results)
    print_report(results, questions)


if __name__ == "__main__":
    main()
