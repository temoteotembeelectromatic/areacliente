import os
from functools import wraps

from flask import Flask, flash, redirect, render_template, request, session, url_for


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-render")

CLIENT_EMAIL = os.environ.get("CLIENT_EMAIL", "cliente@smartic.pro").lower()
CLIENT_PASSWORD = os.environ.get("CLIENT_PASSWORD", "cliente123")


client_profile = {
    "name": "Cliente Smartic",
    "company": "Empresa Cliente",
    "email": CLIENT_EMAIL,
    "phone": "+351 000 000 000",
    "account_manager": "Equipa Smartic",
}

summary_cards = [
    {"label": "Pedidos abertos", "value": "3", "tone": "warning"},
    {"label": "Faturas pendentes", "value": "2", "tone": "danger"},
    {"label": "Servicos ativos", "value": "5", "tone": "success"},
    {"label": "Ultima atualizacao", "value": "Hoje", "tone": "neutral"},
]

orders = [
    {"id": "ENC-24081", "title": "Material eletrico obra norte", "status": "Em processamento", "date": "2026-08-16"},
    {"id": "ENC-24077", "title": "Quadro tecnico QGBT", "status": "A aguardar fornecedor", "date": "2026-08-13"},
    {"id": "ENC-24069", "title": "Consumiveis manutencao", "status": "Entregue", "date": "2026-08-05"},
]

invoices = [
    {"id": "FT 2026/118", "amount": "1 284,50 EUR", "status": "Pendente", "date": "2026-08-12"},
    {"id": "FT 2026/104", "amount": "742,90 EUR", "status": "Pendente", "date": "2026-08-01"},
    {"id": "FT 2026/091", "amount": "2 305,00 EUR", "status": "Pago", "date": "2026-07-20"},
]

requests_list = [
    {"id": "SUP-1021", "subject": "Pedido de intervencao", "status": "Aberto", "date": "2026-08-16"},
    {"id": "SUP-1017", "subject": "Duvida sobre fatura", "status": "Em analise", "date": "2026-08-14"},
    {"id": "SUP-1008", "subject": "Atualizacao de contactos", "status": "Resolvido", "date": "2026-08-09"},
]


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped_view


@app.route("/")
def home():
    if session.get("logged_in"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if email == CLIENT_EMAIL and password == CLIENT_PASSWORD:
            session["logged_in"] = True
            session["client_email"] = email
            return redirect(url_for("dashboard"))

        flash("Email ou palavra-passe invalidos.", "error")

    return render_template("login.html", demo_email=CLIENT_EMAIL)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    return render_template(
        "dashboard.html",
        profile=client_profile,
        cards=summary_cards,
        orders=orders,
        invoices=invoices,
        requests=requests_list,
    )


@app.route("/encomendas")
@login_required
def encomendas():
    return render_template("list.html", title="Encomendas", items=orders)


@app.route("/faturas")
@login_required
def faturas():
    return render_template("list.html", title="Faturas", items=invoices)


@app.route("/pedidos")
@login_required
def pedidos():
    return render_template("list.html", title="Pedidos de suporte", items=requests_list)


@app.route("/perfil")
@login_required
def perfil():
    return render_template("profile.html", profile=client_profile)


@app.route("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(debug=True)
