import os
import secrets
import time
from collections import defaultdict, deque
from datetime import timedelta
from functools import wraps

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY")

if not app.secret_key:
    raise RuntimeError("SECRET_KEY tem de estar definido nas variaveis de ambiente.")

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "true").lower() == "true",
)

CLIENT_EMAIL = os.environ.get("CLIENT_EMAIL", "").strip().lower()
CLIENT_PASSWORD_HASH = os.environ.get("CLIENT_PASSWORD_HASH")

if not CLIENT_EMAIL:
    raise RuntimeError("CLIENT_EMAIL tem de estar definido nas variaveis de ambiente.")

if not CLIENT_PASSWORD_HASH:
    raise RuntimeError("CLIENT_PASSWORD_HASH tem de estar definido nas variaveis de ambiente.")

MAX_LOGIN_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_LOCK_SECONDS = 15 * 60
login_attempts = defaultdict(deque)
locked_until = {}


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


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


@app.context_processor
def inject_template_helpers():
    return {"csrf_token": csrf_token}


def verify_csrf():
    submitted_token = request.form.get("csrf_token", "")
    expected_token = session.get("csrf_token", "")
    if not expected_token or not secrets.compare_digest(submitted_token, expected_token):
        abort(400)


def login_key(email):
    return f"{request.remote_addr or 'unknown'}:{email}"


def is_login_locked(key):
    return locked_until.get(key, 0) > time.time()


def register_failed_login(key):
    now = time.time()
    recent_attempts = login_attempts[key]
    while recent_attempts and recent_attempts[0] <= now - LOGIN_WINDOW_SECONDS:
        recent_attempts.popleft()
    recent_attempts.append(now)
    if len(recent_attempts) >= MAX_LOGIN_ATTEMPTS:
        locked_until[key] = now + LOGIN_LOCK_SECONDS
        recent_attempts.clear()


def clear_login_attempts(key):
    login_attempts.pop(key, None)
    locked_until.pop(key, None)


@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self'; img-src 'self' data:; "
        "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    )
    if request.is_secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.route("/")
def home():
    if session.get("logged_in"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("logged_in"):
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        verify_csrf()
        email = request.form.get("email", "").strip().lower()
        session["login_email"] = email
        return redirect(url_for("login_password"))

    session.pop("login_email", None)
    return render_template("login.html")


@app.route("/login/password", methods=["GET", "POST"])
def login_password():
    if session.get("logged_in"):
        return redirect(url_for("dashboard"))

    email = session.get("login_email")
    if not email:
        return redirect(url_for("login"))

    if request.method == "POST":
        verify_csrf()
        key = login_key(email)

        if is_login_locked(key):
            flash("Por seguranca, aguarde alguns minutos antes de tentar novamente.", "error")
            return redirect(url_for("login_password"))

        password = request.form.get("password", "")
        email_matches = secrets.compare_digest(email, CLIENT_EMAIL)
        password_matches = check_password_hash(CLIENT_PASSWORD_HASH, password)

        if email_matches and password_matches:
            session.clear()
            session["logged_in"] = True
            session["client_email"] = email
            session.permanent = True
            clear_login_attempts(key)
            return redirect(url_for("dashboard"))

        register_failed_login(key)
        flash("Nao foi possivel iniciar sessao com estes dados.", "error")
        return redirect(url_for("login_password"))

    return render_template("login.html", email=email, password_step=True)


@app.route("/logout", methods=["POST"])
def logout():
    verify_csrf()
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
