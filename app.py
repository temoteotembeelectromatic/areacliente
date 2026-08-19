import os
import secrets
import time
from collections import defaultdict, deque
from datetime import date, timedelta
from io import BytesIO
from functools import wraps

import psycopg2
from psycopg2.extras import RealDictCursor
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
DATABASE_URL_2 = os.environ.get("DATABASE_URL_2", "").strip()
CLIENT_ALLOWED_NUMBERS = [
    value.strip()
    for value in os.environ.get("CLIENT_ALLOWED_NUMBERS", "").split(",")
    if value.strip()
]
if not DATABASE_URL_2 and not CLIENT_ALLOWED_NUMBERS:
    CLIENT_ALLOWED_NUMBERS = ["TESTE-001"]

# Durante os testes, a base externa pode ser consultada em modo apenas leitura
# sem uma lista de clientes autorizados. Em produção, definir a lista e usar
# EQUIPMENT_TEST_MODE=false para limitar o acesso ao contrato correcto.
equipment_test_default = "true" if DATABASE_URL_2 and not CLIENT_ALLOWED_NUMBERS else "false"
EQUIPMENT_TEST_MODE = os.environ.get("EQUIPMENT_TEST_MODE", equipment_test_default).lower() == "true"

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
    {"id": "EQ-001", "numero_cliente": "TESTE-001", "numero_contrato": "CTR-2026-001", "name": "Quadro geral de baixa tensão", "type": "Quadro elétrico", "brand": "Schneider", "model": "Prisma", "location": "Edifício principal", "status": "Operacional", "warranty_until": "2027-04-30", "in_warranty": True, "next_service": "2026-10-14"},
    {"id": "EQ-002", "numero_cliente": "TESTE-001", "numero_contrato": "CTR-2026-001", "name": "Grupo gerador 250 kVA", "type": "Grupo gerador", "brand": "Cummins", "model": "C250", "location": "Zona técnica", "status": "Operacional", "warranty_until": None, "in_warranty": False, "next_service": "2026-09-20"},
    {"id": "EQ-003", "numero_cliente": "TESTE-001", "numero_contrato": "CTR-2026-001", "name": "Sistema UPS", "type": "UPS", "brand": "Eaton", "model": "93PM", "location": "Sala de servidores", "status": "Acompanhar", "warranty_until": "2026-11-15", "in_warranty": True, "next_service": "2026-09-02"},
    {"id": "EQ-004", "numero_cliente": "TESTE-001", "numero_contrato": "CTR-2026-001", "name": "Iluminação de emergência", "type": "Iluminação", "brand": "Legrand", "model": "URA", "location": "Instalação completa", "status": "Operacional", "warranty_until": None, "in_warranty": False, "next_service": "2026-11-10"},
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


def get_equipment_filters():
    return {
        "q": request.args.get("q", "").strip()[:100],
        "cliente": request.args.get("cliente", "").strip(),
        "tipo": request.args.get("tipo", "").strip()[:100],
        "garantia": request.args.get("garantia", "").strip(),
        "vista": "tabela" if request.args.get("vista") == "tabela" else "cartoes",
    }


def has_equipment_selection(filters):
    return any(filters[key] for key in ("q", "cliente", "tipo", "garantia"))


def demo_equipment_rows(filters):
    rows = list(equipment)
    selected_client = filters["cliente"] if filters["cliente"] in CLIENT_ALLOWED_NUMBERS else ""
    if selected_client:
        rows = [row for row in rows if row["numero_cliente"] == selected_client]
    if filters["tipo"]:
        rows = [row for row in rows if row["type"] == filters["tipo"]]
    if filters["garantia"] == "1":
        rows = [row for row in rows if row["in_warranty"]]
    elif filters["garantia"] == "0":
        rows = [row for row in rows if not row["in_warranty"]]
    if filters["q"]:
        query = filters["q"].casefold()
        rows = [
            row
            for row in rows
            if query == str(row["id"] or "").casefold()
            or query == str(row["numero_contrato"] or "").casefold()
        ]
    types = sorted({row["type"] for row in equipment})
    clients = sorted({row["numero_cliente"] for row in equipment})
    if not has_equipment_selection(filters):
        rows = []
    return rows, types, clients, None, "Dados de demonstração"


def external_equipment_rows(filters):
    if not DATABASE_URL_2:
        return demo_equipment_rows(filters)
    if not CLIENT_ALLOWED_NUMBERS and not EQUIPMENT_TEST_MODE:
        return [], [], [], "Não existem números de cliente autorizados para esta conta.", "Base de dados externa"

    connection = None
    try:
        connection = psycopg2.connect(
            DATABASE_URL_2,
            connect_timeout=5,
            options="-c default_transaction_read_only=on -c statement_timeout=5000",
        )
        connection.set_session(readonly=True, autocommit=False)
        cursor = connection.cursor(cursor_factory=RealDictCursor)

        scope_clause = ""
        scope_params = []
        if CLIENT_ALLOWED_NUMBERS:
            scope_clause = "WHERE TRIM(COALESCE(numero_cliente, '')) = ANY(%s)"
            scope_params.append(CLIENT_ALLOWED_NUMBERS)

        cursor.execute(
            f"""
            SELECT DISTINCT TRIM(COALESCE(numero_cliente, '')) AS numero_cliente
            FROM registo_equipamentos
            {scope_clause}
            ORDER BY numero_cliente
            LIMIT 250
            """,
            scope_params,
        )
        client_numbers = [row["numero_cliente"] for row in cursor.fetchall() if row["numero_cliente"]]

        cursor.execute(
            f"""
            SELECT DISTINCT COALESCE(NULLIF(TRIM(tipo_equipamento), ''), 'Sem tipo') AS tipo
            FROM registo_equipamentos
            {scope_clause}
            ORDER BY tipo
            """,
            scope_params,
        )
        types = [row["tipo"] for row in cursor.fetchall()]

        source_label = "Base de dados externa · Teste · Apenas leitura" if EQUIPMENT_TEST_MODE else "Base de dados externa · Apenas leitura"
        if not has_equipment_selection(filters):
            return [], types, client_numbers, None, source_label

        selected_client = filters["cliente"]
        if selected_client and selected_client not in client_numbers:
            selected_client = ""

        clauses = []
        params = []
        if CLIENT_ALLOWED_NUMBERS:
            clauses.append("TRIM(COALESCE(numero_cliente, '')) = ANY(%s)")
            params.append(CLIENT_ALLOWED_NUMBERS)
        if selected_client:
            clauses.append("TRIM(COALESCE(numero_cliente, '')) = %s")
            params.append(selected_client)
        if filters["tipo"]:
            clauses.append("COALESCE(NULLIF(TRIM(tipo_equipamento), ''), 'Sem tipo') = %s")
            params.append(filters["tipo"])
        if filters["garantia"] == "1":
            clauses.append("validade_garantia >= CURRENT_DATE")
        elif filters["garantia"] == "0":
            clauses.append("(validade_garantia IS NULL OR validade_garantia < CURRENT_DATE)")
        if filters["q"]:
            clauses.append(
                """
                (CAST(numero_equipamento AS TEXT) = %s
                 OR CAST(numero_contrato AS TEXT) = %s)
                """
            )
            params.extend([filters["q"], filters["q"]])

        cursor.execute(
            f"""
            SELECT
                id,
                COALESCE(NULLIF(TRIM(numero_equipamento), ''), CAST(id AS TEXT)) AS numero_equipamento,
                TRIM(COALESCE(numero_cliente, '')) AS numero_cliente,
                TRIM(COALESCE(numero_contrato, '')) AS numero_contrato,
                COALESCE(NULLIF(TRIM(tipo_equipamento), ''), 'Sem tipo') AS tipo_equipamento,
                COALESCE(NULLIF(TRIM(marca), ''), '-') AS marca,
                COALESCE(NULLIF(TRIM(modelo), ''), '-') AS modelo,
                COALESCE(NULLIF(TRIM(posicao), ''), '-') AS posicao,
                COALESCE(NULLIF(TRIM(estado_geral), ''), 'Sem estado') AS estado_geral,
                validade_garantia,
                data_instalacao,
                (validade_garantia IS NOT NULL AND validade_garantia >= CURRENT_DATE) AS em_garantia
            FROM registo_equipamentos
            WHERE {' AND '.join(clauses) if clauses else 'TRUE'}
            ORDER BY numero_cliente, tipo_equipamento, numero_equipamento
            LIMIT 20
            """,
            params,
        )
        database_rows = cursor.fetchall()
        rows = [
            {
                "_db_id": row["id"],
                "id": row["numero_equipamento"],
                "numero_cliente": row["numero_cliente"],
                "numero_contrato": row["numero_contrato"],
                "name": row["tipo_equipamento"],
                "type": row["tipo_equipamento"],
                "brand": row["marca"],
                "model": row["modelo"],
                "location": row["posicao"],
                "status": row["estado_geral"],
                "warranty_until": row["validade_garantia"],
                "in_warranty": bool(row["em_garantia"]),
                "installed_at": row["data_instalacao"],
            }
            for row in database_rows
        ]
        equipment_ids = [row["_db_id"] for row in rows]
        if equipment_ids:
            try:
                cursor.execute(
                    """
                    SELECT equipamento_id, file_url, original_filename
                    FROM registo_equipamentos_anexos
                    WHERE equipamento_id = ANY(%s)
                    ORDER BY created_at DESC, id DESC
                    """,
                    (equipment_ids,),
                )
                photos_by_equipment = {}
                for photo in cursor.fetchall():
                    photos_by_equipment.setdefault(photo["equipamento_id"], []).append(
                        {
                            "file_url": photo["file_url"],
                            "file_name": photo["original_filename"] or "Fotografia",
                        }
                    )
                for row in rows:
                    row["photos"] = photos_by_equipment.get(row["_db_id"], [])
            except Exception:
                app.logger.exception("Falha ao consultar fotografias dos equipamentos")
                connection.rollback()
                for row in rows:
                    row["photos"] = []
        for row in rows:
            row.pop("_db_id", None)
        return rows, types, client_numbers, None, source_label
    except Exception:
        app.logger.exception("Falha ao consultar DATABASE_URL_2")
        return [], [], [], "Não foi possível consultar os equipamentos neste momento.", "Base de dados externa"
    finally:
        if connection is not None:
            connection.rollback()
            connection.close()


def external_maintenance_history(selected_equipment):
    if not DATABASE_URL_2:
        equipment_numbers = sorted(row["id"] for row in equipment)
        rows = [
            {
                "id": item["id"],
                "title": item["title"],
                "date": item["date"],
                "contract": "-",
                "equipment": item["equipment"],
                "location": "-",
                "status": item["status"],
                "technician": "-",
            }
            for item in maintenance
            if item["equipment"] == selected_equipment
        ] if selected_equipment else []
        return equipment_numbers, rows

    connection = None
    try:
        connection = psycopg2.connect(
            DATABASE_URL_2,
            connect_timeout=5,
            options="-c default_transaction_read_only=on -c statement_timeout=5000",
        )
        connection.set_session(readonly=True, autocommit=False)
        cursor = connection.cursor(cursor_factory=RealDictCursor)

        scope_clause = ""
        scope_params = []
        if CLIENT_ALLOWED_NUMBERS:
            scope_clause = "WHERE TRIM(COALESCE(numero_cliente, '')) = ANY(%s)"
            scope_params.append(CLIENT_ALLOWED_NUMBERS)

        cursor.execute(
            f"""
            SELECT DISTINCT TRIM(COALESCE(numero_equipamento, '')) AS numero_equipamento
            FROM registo_equipamentos
            {scope_clause}
              AND TRIM(COALESCE(numero_equipamento, '')) <> ''
            ORDER BY numero_equipamento
            LIMIT 500
            """ if scope_clause else """
            SELECT DISTINCT TRIM(COALESCE(numero_equipamento, '')) AS numero_equipamento
            FROM registo_equipamentos
            WHERE TRIM(COALESCE(numero_equipamento, '')) <> ''
            ORDER BY numero_equipamento
            LIMIT 500
            """,
            scope_params,
        )
        equipment_numbers = [row["numero_equipamento"] for row in cursor.fetchall()]
        if not selected_equipment:
            return equipment_numbers, None
        if selected_equipment not in equipment_numbers:
            return equipment_numbers, "O equipamento seleccionado não está disponível para esta conta."

        cursor.execute(
            """
            SELECT
                id,
                tipo_checklist,
                data_checklist,
                numero_contrato,
                numero_equipamento,
                posicao,
                estado,
                tecnicos,
                criado_por_nome,
                created_at
            FROM checklists_manutencao
            WHERE TRIM(COALESCE(numero_equipamento, '')) = %s
            ORDER BY data_checklist DESC NULLS LAST, created_at DESC NULLS LAST, id DESC
            LIMIT 100
            """,
            (selected_equipment,),
        )
        rows = [
            {
                "id": row["id"],
                "title": row["tipo_checklist"] or "Intervenção",
                "date": row["data_checklist"] or row["created_at"],
                "contract": row["numero_contrato"] or "-",
                "equipment": row["numero_equipamento"] or selected_equipment,
                "location": row["posicao"] or "-",
                "status": row["estado"] or "-",
                "technician": row["tecnicos"] or row["criado_por_nome"] or "-",
            }
            for row in cursor.fetchall()
        ]
        return equipment_numbers, rows
    except Exception:
        app.logger.exception("Falha ao consultar histórico de manutenção")
        return [], "Não foi possível consultar o histórico de manutenção neste momento."
    finally:
        if connection is not None:
            connection.rollback()
            connection.close()


def maintenance_detail(intervention_id):
    if not DATABASE_URL_2:
        return next((item for item in maintenance if item["id"] == intervention_id), None)
    if not str(intervention_id).isdigit():
        return None

    connection = None
    try:
        connection = psycopg2.connect(
            DATABASE_URL_2,
            connect_timeout=5,
            options="-c default_transaction_read_only=on -c statement_timeout=5000",
        )
        connection.set_session(readonly=True, autocommit=False)
        cursor = connection.cursor(cursor_factory=RealDictCursor)
        scope_clause = ""
        params = [int(intervention_id)]
        if CLIENT_ALLOWED_NUMBERS:
            scope_clause = "AND TRIM(COALESCE(e.numero_cliente, '')) = ANY(%s)"
            params.append(CLIENT_ALLOWED_NUMBERS)
        cursor.execute(
            f"""
            SELECT c.*, e.tipo_equipamento, e.marca, e.modelo, e.numero_cliente
            FROM checklists_manutencao c
            LEFT JOIN registo_equipamentos e
              ON TRIM(COALESCE(e.numero_equipamento, '')) = TRIM(COALESCE(c.numero_equipamento, ''))
            WHERE c.id = %s {scope_clause}
            ORDER BY e.id DESC NULLS LAST
            LIMIT 1
            """,
            params,
        )
        detail = cursor.fetchone()
        if not detail:
            return None
        cursor.execute(
            """
            SELECT file_url, original_filename
            FROM checklists_anexos
            WHERE checklist_id = %s
            ORDER BY id
            """,
            (int(intervention_id),),
        )
        detail["photos"] = [
            {"file_url": row["file_url"], "file_name": row["original_filename"] or "Anexo"}
            for row in cursor.fetchall()
        ]
        return detail
    except Exception:
        app.logger.exception("Falha ao consultar detalhe da intervenção")
        return None
    finally:
        if connection is not None:
            connection.rollback()
            connection.close()


@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self'; img-src 'self' https: data:; "
        "script-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
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
    filters = get_equipment_filters()
    rows, types, client_numbers, error, source_label = external_equipment_rows(filters)
    return render_template(
        "equipment.html",
        equipment=rows,
        equipment_types=types,
        client_numbers=client_numbers,
        filters=filters,
        source_label=source_label,
        error=error,
    )


@app.route("/manutencao")
@login_required
def manutencao():
    selected_equipment = request.args.get("equipamento", "").strip()[:100]
    equipment_numbers, history = external_maintenance_history(selected_equipment)
    error = history if isinstance(history, str) else None
    rows = [] if error else (history or [])
    return render_template(
        "maintenance.html",
        maintenance=rows,
        equipment_numbers=equipment_numbers,
        selected_equipment=selected_equipment,
        error=error,
    )


@app.route("/manutencao/<intervention_id>")
@login_required
def manutencao_detalhe(intervention_id):
    detail = maintenance_detail(intervention_id)
    if not detail:
        abort(404)
    return render_template("maintenance_detail.html", detail=detail)


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
