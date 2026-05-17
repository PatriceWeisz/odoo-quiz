#!/usr/bin/env python3
"""
Génère traductions FR + explications Claude pour toutes les questions.
8 appels claude -p en parallèle → ~10 min pour 664 questions.
Sauvegarde atomique toutes les 20 questions traitées.
"""

import json
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

QUESTIONS_FILE = Path(__file__).parent / "questions.json"
WORKERS = 8
SAVE_EVERY = 20


def load():
    if not QUESTIONS_FILE.exists():
        sys.exit("❌ questions.json introuvable.")
    with open(QUESTIONS_FILE, encoding="utf-8") as f:
        return json.load(f)


save_lock = threading.Lock()

def save(data):
    with save_lock:
        tmp = QUESTIONS_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(QUESTIONS_FILE)


def has_correct(q):
    return any(a.get("is_correct") for a in q.get("answers", []))


def needs_work(q):
    return not q.get("explication_claude") or not q.get("title_fr")


def run_claude(prompt):
    result = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True, text=True, timeout=90
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Erreur CLI claude")
    return result.stdout.strip()


def build_prompt(q):
    answers = q.get("answers", [])
    lines = "\n".join(f"  {i+1}. {a['value']}" for i, a in enumerate(answers))

    if has_correct(q):
        correct_vals = ", ".join(a["value"] for a in answers if a.get("is_correct"))
        bonne_reponse_section = f"Bonne(s) réponse(s) connue(s) : {correct_vals}"
        bonne_reponse_line = ""
    else:
        bonne_reponse_section = "La bonne réponse n'est pas connue, détermine-la."
        bonne_reponse_line = "BONNE_REPONSE: <numéro(s) séparés par virgule>\n"

    return f"""Tu es expert certifié Odoo. Voici une question de certification Odoo en anglais.

Question : {q['title']}

Options :
{lines}

{bonne_reponse_section}

Réponds EXACTEMENT dans ce format (rien d'autre avant ou après) :

TITRE_FR: <traduction française du titre>
REPONSES_FR: <traductions numérotées, une par ligne, ex: "1. Texte traduit">
{bonne_reponse_line}EXPLICATION: <explication pédagogique en français, 5-8 lignes>"""


def parse(text, q):
    answers = q.get("answers", [])
    result = {}

    m = re.search(r"TITRE_FR\s*:\s*(.+)", text)
    result["title_fr"] = m.group(1).strip() if m else ""

    m = re.search(r"REPONSES_FR\s*:\s*([\s\S]+?)(?=\nBONNE_REPONSE|\nEXPLICATION)", text)
    translations_fr = []
    if m:
        for line in m.group(1).strip().splitlines():
            match = re.match(r"^\d+\.\s*(.+)", line.strip())
            translations_fr.append(match.group(1).strip() if match else line.strip())
    result["answers_fr"] = translations_fr

    m = re.search(r"BONNE_REPONSE\s*:\s*(.+)", text)
    if m:
        nums = re.findall(r"\d+", m.group(1))
        result["correct_indices"] = [int(n) - 1 for n in nums if 0 < int(n) <= len(answers)]
    else:
        result["correct_indices"] = None

    m = re.search(r"EXPLICATION\s*:\s*([\s\S]+)", text)
    result["explication_claude"] = m.group(1).strip() if m else text.strip()

    return result


def process_one(q):
    """Traite une question : appel Claude + parsing. Thread-safe."""
    prompt = build_prompt(q)
    text = run_claude(prompt)
    return parse(text, q)


def apply_result(q, parsed):
    """Applique le résultat parsé sur la question (appelé sous lock)."""
    had_correct = has_correct(q)
    if parsed.get("title_fr"):
        q["title_fr"] = parsed["title_fr"]

    for j, a in enumerate(q.get("answers", [])):
        if j < len(parsed.get("answers_fr", [])) and parsed["answers_fr"][j]:
            a["value_fr"] = parsed["answers_fr"][j]

    if parsed.get("correct_indices") is not None and not had_correct:
        for j, a in enumerate(q["answers"]):
            a["is_correct"] = j in parsed["correct_indices"]
        if has_correct(q):
            q["correct_answer_source"] = "claude"

    q["explication_claude"] = parsed.get("explication_claude", "")


def main():
    data = load()
    questions = data.get("questions", [])
    total = len(questions)

    to_do = [q for q in questions if needs_work(q)]
    print(f"📋 {total} questions — {len(to_do)} à traiter, {total - len(to_do)} déjà faites.")
    if not to_do:
        print("✅ Tout est complet.")
        return

    try:
        subprocess.run(["claude", "--version"], capture_output=True, check=True)
    except FileNotFoundError:
        sys.exit("❌ CLI `claude` introuvable.")
    except subprocess.CalledProcessError:
        pass

    print(f"🚀 {WORKERS} workers parallèles — ~{round(len(to_do) * 7 / WORKERS / 60)} min estimées.\n")

    done_count = 0
    error_count = 0
    write_lock = threading.Lock()
    counter_lock = threading.Lock()

    # Index questions par id pour retrouver rapidement
    q_by_id = {q["id"]: q for q in questions}

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {executor.submit(process_one, q): q for q in to_do}

        for future in as_completed(futures):
            q = futures[future]
            with counter_lock:
                done_count += 1
                n = done_count

            pct = round((n / len(to_do)) * 100)
            label = "🔍+💡" if not has_correct(q) else "💡"

            try:
                parsed = future.result()
                with write_lock:
                    apply_result(q, parsed)
                    extra = ""
                    if parsed.get("correct_indices") is not None:
                        extra = f" réponse(s): {[x+1 for x in parsed['correct_indices']]}"
                print(f"[{n}/{len(to_do)} — {pct}%] {label} {q['title'][:60]}… ✅{extra}")

                # Sauvegarde périodique
                if n % SAVE_EVERY == 0:
                    save(data)
                    print(f"  💾 Sauvegarde ({n}/{len(to_do)})")

            except Exception as e:
                with counter_lock:
                    error_count += 1
                print(f"[{n}/{len(to_do)} — {pct}%] ❌ {q['title'][:60]}… {e}")

    save(data)

    done_fr      = sum(1 for q in questions if q.get("title_fr"))
    done_claude  = sum(1 for q in questions if q.get("explication_claude"))
    done_correct = sum(1 for q in questions if has_correct(q))
    print(f"\n✅ Terminé.")
    print(f"   • {done_correct}/{total} bonnes réponses")
    print(f"   • {done_fr}/{total} traductions FR")
    print(f"   • {done_claude}/{total} explications Claude")
    if error_count:
        print(f"   ⚠️  {error_count} erreurs — relance pour compléter.")


if __name__ == "__main__":
    main()
