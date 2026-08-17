import os
import secrets
import time
from collections import defaultdict, deque
from datetime import date, timedelta
from io import BytesIO
from functools import wraps

from flask import Flask, abort, flash, redirect, render_template, request, send_file, session, url_for
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY")

if not app.secret_key:
    raise RuntimeError("SECRET_KEY tem de estar definido nas variaveis de ambiente.")

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),
    SESSION_COOKIE_NAME="smartic_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "true").lower() == "true",
)

CLIENT_EMAIL = os.environ.get("CLIENT_EMAIL", "").strip().lower()
CLIENT_PASSWORD_HASH = os.environ.get("CLIENT_PASSWORD_HASH")
CONTROLLER_LEGAL_NAME = os.environ.get("CONTROLLER_LEGAL_NAME", "").strip()
CONTROLLER_ADDRESS = os.environ.get("CONTROLLER_ADDRESS", "").strip()
PRIVACY_EMAIL = os.environ.get("PRIVACY_EMAIL", "").strip()
PRIVACY_UPDATED_AT = os.environ.get("PRIVACY_UPDATED_AT", "2026-08-17")
CONTRACT_VALID_UNTIL = os.environ.get("CONTRACT_VALID_UNTIL", "2026-12-31")

if not CLIENT_EMAIL:
    raise RuntimeError("CLIENT_EMAIL tem de estar definido nas variaveis de ambiente.")

if not CLIENT_PASSWORD_HASH:
    raise RuntimeError("CLIENT_PASSWORD_HASH tem de estar definido nas variaveis de ambiente.")

if not all((CONTROLLER_LEGAL_NAME, CONTROLLER_ADDRESS, PRIVACY_EMAIL)):
    raise RuntimeError(
        "CONTROLLER_LEGAL_NAME, CONTROLLER_ADDRESS e PRIVACY_EMAIL tem de estar definidos."
    )

try:
    contract_valid_until = date.fromisoformat(CONTRACT_VALID_UNTIL)
except ValueError as error:
    raise RuntimeError("CONTRACT_VALID_UNTIL deve usar o formato AAAA-MM-DD.") from error

MAX_LOGIN_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_LOCK_SECONDS = 15 * 60
login_attempts = defaultdict(deque)
locked_until = {}


client_profile = {
    "name": "Cliente Electromatic",
    "company": "Empresa Cliente - Contrato de Manutenção",
    "email": CLIENT_EMAIL,
    "phone": "+351 000 000 000",
    "account_manager": "Gestor de contrato Electromatic",
    "manager_email": "gestor@electromatic.pt",
    "manager_phone": "+351 210 000 000",
}

summary_cards = [
    {"label": "Equipamentos cobertos", "value": "4", "tone": "neutral"},
    {"label": "Preventivas concluídas", "value": "8", "tone": "success"},
    {"label": "Ocorrências abertas", "value": "1", "tone": "warning"},
    {"label": "Contrato válido até", "value": CONTRACT_VALID_UNTIL, "tone": "neutral"},
]

equipment = [
    {"id": "EQ-001", "name": "Quadro geral de baixa tensao", "location": "Edificio principal", "status": "Operacional", "next_service": "2026-10-14"},
    {"id": "EQ-002", "name": "Grupo gerador 250 kVA", "location": "Zona técnica", "status": "Operacional", "next_service": "2026-09-20"},
    {"id": "EQ-003", "name": "Sistema UPS", "location": "Sala de servidores", "status": "Acompanhar", "next_service": "2026-09-02"},
    {"id": "EQ-004", "name": "Iluminação de emergência", "location": "Instalação completa", "status": "Operacional", "next_service": "2026-11-10"},
]

maintenance = [
    {"id": "MP-2026-018", "title": "Manutenção preventiva - QGBT", "type": "Preventiva", "status": "Concluída", "date": "2026-08-14", "equipment": "EQ-001", "document_id": "DOC-018"},
    {"id": "MC-2026-007", "title": "Ocorrência - alarmes UPS", "type": "Corretiva", "status": "Em acompanhamento", "date": "2026-08-16", "equipment": "EQ-003", "document_id": "DOC-019"},
    {"id": "MP-2026-016", "title": "Teste de iluminação de emergência", "type": "Preventiva", "status": "Concluída", "date": "2026-07-22", "equipment": "EQ-004", "document_id": "DOC-016"},
]

documents = [
    {"id": "DOC-018", "title": "Relatório preventivo QGBT", "category": "Manutenção preventiva", "date": "2026-08-14"},
    {"id": "DOC-019", "title": "Relatório de ocorrência UPS", "category": "Manutenção corretiva", "date": "2026-08-16"},
    {"id": "DOC-016", "title": "Checklist iluminacao de emergencia", "category": "Checklist", "date": "2026-07-22"},
    {"id": "DOC-003", "title": "Guia de desbloqueio do grupo gerador", "category": "Guia técnico", "date": "2026-06-03"},
]

guides = [
    "Consulte o guia de desbloqueio antes de contactar a piquete.",
    "Os checklists de manutenção preventiva ficam disponíveis após cada intervenção.",
    "Para uma avaria urgente, contacte a piquete através do gestor de contrato.",
]

orders = [
    {"id": "ENC-24081", "title": "Material eletrico obra norte", "status": "Em processamento", "date": "2026-08-16"},
    {"id": "ENC-24077", "title": "Quadro técnico QGBT", "status": "A aguardar fornecedor", "date": "2026-08-13"},
    {"id": "ENC-24069", "title": "Consumíveis de manutenção", "status": "Entregue", "date": "2026-08-05"},
]

invoices = [
    {"id": "FT 2026/118", "amount": "1 284,50 EUR", "status": "Pendente", "date": "2026-08-12"},
    {"id": "FT 2026/104", "amount": "742,90 EUR", "status": "Pendente", "date": "2026-08-01"},
    {"id": "FT 2026/091", "amount": "2 305,00 EUR", "status": "Pago", "date": "2026-07-20"},
]

requests_list = [
    {"id": "SUP-1021", "subject": "Pedido de intervenção", "status": "Aberto", "date": "2026-08-16"},
    {"id": "SUP-1017", "subject": "Duvida sobre fatura", "status": "Em analise", "date": "2026-08-14"},
    {"id": "SUP-1008", "subject": "Atualização de contactos", "status": "Resolvido", "date": "2026-08-09"},
]


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        if not is_contract_active():
            session.clear()
            flash("O acesso está suspenso porque o contrato indicado terminou.", "error")
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


def login_key():
    return request.remote_addr or "unknown"


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


def is_contract_active():
    return date.today() <= contract_valid_until


def pdf_response(filename, items):
    output = BytesIO()
    pdf = canvas.Canvas(output, pagesize=A4)
    _, page_height = A4
    y = page_height - 52
    pdf.setTitle("Documentos Electromatic")
    logo_path = os.path.join(app.root_path, "static", "electromatic-logo.png")
    pdf.drawImage(ImageReader(logo_path), 48, y - 36, width=192, height=45, mask="auto")
    y -= 54
    pdf.setFont("Helvetica-Bold", 15)
    pdf.drawString(48, y, "Área de cliente")
    y -= 24
    pdf.setFont("Helvetica", 10)
    pdf.drawString(48, y, f"Contrato válido até: {CONTRACT_VALID_UNTIL}")
    y -= 32

    for item in items:
        if y < 76:
            pdf.showPage()
            y = page_height - 64
        pdf.setFont("Helvetica-Bold", 12)
        pdf.drawString(48, y, item["title"])
        y -= 18
        pdf.setFont("Helvetica", 10)
        pdf.drawString(48, y, f"{item['id']} | {item['category']} | {item['date']}")
        y -= 26

    pdf.save()
    output.seek(0)
    return send_file(output, mimetype="application/pdf", as_attachment=True, download_name=filename)


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
    if session.get("logged_in") or request.path.startswith("/login"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
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
        # Store only the outcome, never the email address, in the browser session.
        session["login_email_matches"] = secrets.compare_digest(email, CLIENT_EMAIL)
        return redirect(url_for("login_password"))

    session.pop("login_email_matches", None)
    return render_template("login.html")


@app.route("/login/password", methods=["GET", "POST"])
def login_password():
    if session.get("logged_in"):
        return redirect(url_for("dashboard"))

    email_matches = session.get("login_email_matches")
    if email_matches is None:
        return redirect(url_for("login"))

    if request.method == "POST":
        verify_csrf()
        key = login_key()

        if is_login_locked(key):
            flash("Por segurança, aguarde alguns minutos antes de tentar novamente.", "error")
            return redirect(url_for("login_password"))

        password = request.form.get("password", "")
        password_matches = check_password_hash(CLIENT_PASSWORD_HASH, password)

        if email_matches and password_matches:
            if not is_contract_active():
                flash("O acesso não está disponível: o contrato indicado terminou.", "error")
                return redirect(url_for("login"))
            session.clear()
            session["logged_in"] = True
            session.permanent = True
            clear_login_attempts(key)
            return redirect(url_for("dashboard"))

        register_failed_login(key)
        flash("Não foi possível iniciar sessão com estes dados.", "error")
        return redirect(url_for("login_password"))

    return render_template("login.html", password_step=True)


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
        equipment=equipment,
        maintenance=maintenance,
        guides=guides,
        contract_valid_until=CONTRACT_VALID_UNTIL,
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


@app.route("/equipamentos")
@login_required
def equipamentos():
    return render_template("equipment.html", equipment=equipment)


@app.route("/manutencao")
@login_required
def manutencao():
    return render_template("maintenance.html", maintenance=maintenance)


@app.route("/documentos")
@login_required
def documentos():
    return render_template("documents.html", documents=documents)


@app.route("/documentos/<document_id>/pdf")
@login_required
def documento_pdf(document_id):
    document = next((item for item in documents if item["id"] == document_id), None)
    if not document:
        abort(404)
    return pdf_response(f"{document_id.lower()}.pdf", [document])


@app.route("/documentos/bundle.pdf")
@login_required
def documentos_bundle_pdf():
    return pdf_response("electromatic-documentos.pdf", documents)


@app.route("/apoio", methods=["GET", "POST"])
@login_required
def apoio():
    reply = None
    if request.method == "POST":
        verify_csrf()
        message = request.form.get("message", "").strip().lower()
        if any(term in message for term in ("avaria", "urgente", "piquete")):
            reply = "Para uma avaria urgente, contacte a piquete através do gestor de contrato."
        elif any(term in message for term in ("relatorio", "pdf", "checklist")):
            reply = "Os relatórios e checklists estão disponíveis na área Documentos."
        else:
            reply = "Posso orientar sobre equipamentos, manutenção, documentos ou contacto com o gestor."
    return render_template("support.html", profile=client_profile, guides=guides, reply=reply)


@app.route("/perfil")
@login_required
def perfil():
    return render_template("profile.html", profile=client_profile)


@app.route("/health")
def health():
    return {"status": "ok"}


@app.route("/privacidade")
def privacidade():
    return render_template(
        "privacy.html",
        controller_name=CONTROLLER_LEGAL_NAME,
        controller_address=CONTROLLER_ADDRESS,
        privacy_email=PRIVACY_EMAIL,
        updated_at=PRIVACY_UPDATED_AT,
    )


@app.route("/cookies")
def cookies():
    return render_template("cookies.html")


if __name__ == "__main__":
    app.run(debug=True)
