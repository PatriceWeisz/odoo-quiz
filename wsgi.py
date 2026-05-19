"""WSGI shim — charge app.py par chemin de fichier pour contourner la collision
de nom avec le package app/. Utilisé par gunicorn en prod (`gunicorn wsgi:app`).

Pourquoi : Python privilégie les packages aux modules. `import app` retourne
donc le package app/, masquant le fichier app.py qui contient le Flask app.
Ce shim charge explicitement app.py par chemin de fichier.

Usage local : python app.py (inchangé)
Usage prod  : gunicorn wsgi:app
"""

import importlib.util
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("odoo_quiz_app", _here / "app.py")
_module = importlib.util.module_from_spec(_spec)
# Référencer dans sys.modules pour que les imports relatifs / fonctions d'app.py
# se résolvent correctement.
sys.modules["odoo_quiz_app"] = _module
_spec.loader.exec_module(_module)

app = _module.app  # Objet Flask exporté par app.py (`app = Flask(__name__)`)
