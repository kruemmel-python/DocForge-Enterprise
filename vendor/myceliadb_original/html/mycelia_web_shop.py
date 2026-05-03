"""Mycelia shop frontend without classical SQL persistence.

Products are encrypted and stored as DAD attractors through mycelia_platform.py.
Uploaded images are deliberately not persisted by this minimal replacement;
put binary media behind a dedicated encrypted blob node before production use.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Any, Mapping

from flask import Flask, redirect, render_template_string, request, session, url_for

app = Flask(__name__)
app.secret_key = "SuperSecretShopSessionKey"
MYCELIA_API_URL = "http://127.0.0.1:9999"


def call_mycelia(command: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    req = urllib.request.Request(
        MYCELIA_API_URL,
        data=json.dumps({"command": command, "payload": dict(payload or {})}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        out = json.loads(response.read().decode("utf-8"))
        return out if isinstance(out, dict) else {"status": "error", "message": "Ungültige Antwort"}


HTML = """<!doctype html><html lang="de"><head><title>Mycelia Shop</title><style>body{background:#121212;color:#e0e0e0;font-family:monospace;padding:40px}a{color:#00ff99}.box{border:1px solid #333;background:#1e1e1e;padding:20px;margin:12px 0}input,textarea{background:#222;border:1px solid #444;color:white;padding:8px;width:100%;box-sizing:border-box;margin:4px 0}button{background:#00ff99;border:0;padding:10px;font-weight:bold}.product{border-bottom:1px dashed #333;padding:10px 0}.muted{color:#777;font-size:12px}</style></head><body><h1>MYCELIA ZERO-KNOWLEDGE E-SHOP</h1><p style="color:#ffcc66">{{ msg }}</p><div class="box"><h2>Login</h2><form method="post" action="/login"><input name="user" placeholder="Username"><input name="pass" type="password" placeholder="Passwort"><button>ATTRAKTOR PRÜFEN</button></form><p class="muted">Nutzer kommen aus dem CognitiveCore, nicht aus einer relationalen Tabelle.</p></div><div class="box"><h2>Produkt als Nutrient-Node einstellen</h2><form method="post" action="/product"><input name="name" placeholder="Name"><input name="price" placeholder="Preis"><textarea name="description" placeholder="Beschreibung"></textarea><button>GPU-VERSCHLÜSSELT SPEICHERN</button></form></div><div class="box"><h2>Rekonstruierte Produkte</h2>{% for p in products %}<div class="product"><strong>{{ p.product.name }}</strong> — {{ p.product.price }}<br>{{ p.product.description }}<br><span class="muted">sig={{ p.signature }} stability={{ p.stability }} seller={{ p.seller }}</span></div>{% else %}<p class="muted">Noch keine Produkt-Attraktoren.</p>{% endfor %}</div><p><a href="/logout">Logout</a></p></body></html>"""


@app.route("/")
@app.route("/shop")
def shop():
    products = call_mycelia("list_products", {"limit": 50}).get("products", [])
    return render_template_string(HTML, products=products, msg=session.pop("msg", ""))


@app.route("/login", methods=["POST"])
def login():
    result = call_mycelia("login_attractor", {"username": request.form.get("user", ""), "password": request.form.get("pass", "")})
    if result.get("status") == "ok":
        session["username"] = result.get("username")
        session["signature"] = result.get("signature")
        session["msg"] = "Login-Attraktor stabil."
    else:
        session["msg"] = result.get("message", "Login fehlgeschlagen.")
    return redirect(url_for("shop"))


@app.route("/product", methods=["POST"])
def product():
    seller = session.get("username", "anonymous")
    result = call_mycelia(
        "store_product",
        {
            "seller": seller,
            "product": {
                "name": request.form.get("name", ""),
                "price": request.form.get("price", ""),
                "description": request.form.get("description", ""),
            },
        },
    )
    session["msg"] = "Produkt-Attraktor gespeichert." if result.get("status") == "ok" else result.get("message", "Fehler")
    return redirect(url_for("shop"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("shop"))


if __name__ == "__main__":
    print("--- Mycelia Shop (DAD/OpenCL Backend, no relational DB) ---")
    app.run(debug=True, use_reloader=False, port=5001)
