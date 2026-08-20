import json
import os
import secrets
import time
from collections import defaultdict, deque
from datetime import date, timedelta
from io import BytesIO
from functools import wraps

import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash


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
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
CLIENT_ALLOWED_NUMBERS = [
    value.strip()
    for value in os.environ.get("CLIENT_ALLOWED_NUMBERS", "").split(",")
    if value.strip()
]

CLIENT_USER_ACCOUNTS_JSON = os.environ.get("CLIENT_USER_ACCOUNTS_JSON", "").strip()
client_accounts = []
if CLIENT_USER_ACCOUNTS_JSON:
    try:
        raw_accounts = json.loads(CLIENT_USER_ACCOUNTS_JSON)
    except json.JSONDecodeError as error:
        raise RuntimeError("CLIENT_USER_ACCOUNTS_JSON deve conter JSON válido.") from error
    if not isinstance(raw_accounts, list) or not raw_accounts:
        raise RuntimeError("CLIENT_USER_ACCOUNTS_JSON deve ser uma lista com pelo menos um utilizador.")
    for raw_account in raw_accounts:
        if not isinstance(raw_account, dict):
            raise RuntimeError("Cada utilizador em CLIENT_USER_ACCOUNTS_JSON deve ser um objecto.")
        email = str(raw_account.get("email", "")).strip().lower()
        password_hash = str(raw_account.get("password_hash", "")).strip()
        client_numbers = [
            str(value).strip()
            for value in raw_account.get("client_numbers", [])
            if str(value).strip()
        ]
        if not email or not password_hash or not client_numbers:
            raise RuntimeError(
                "Cada utilizador deve ter email, password_hash e pelo menos um client_number."
            )
        client_accounts.append(
            {
                "email": email,
                "password_hash": password_hash,
                "client_numbers": client_numbers,
                "name": str(raw_account.get("name") or email),
                "role": str(raw_account.get("role") or "Utilizador"),
            }
        )
else:
    client_accounts = [
        {
            "email": CLIENT_EMAIL,
            "password_hash": CLIENT_PASSWORD_HASH,
            "client_numbers": CLIENT_ALLOWED_NUMBERS,
            "name": "Administrador do contrato",
            "role": "Administrador",
        }
    ]

if not DATABASE_URL_2 and not CLIENT_ALLOWED_NUMBERS:
    CLIENT_ALLOWED_NUMBERS = ["TESTE-001"]

# Durante os testes, a base externa pode ser consultada em modo apenas leitura
# sem uma lista de clientes autorizados. Em produção, definir a lista e usar
# EQUIPMENT_TEST_MODE=false para limitar o acesso ao contrato correcto.
equipment_test_default = "true" if DATABASE_URL_2 and not CLIENT_ALLOWED_NUMBERS else "false"
EQUIPMENT_TEST_MODE = os.environ.get("EQUIPMENT_TEST_MODE", equipment_test_default).lower() == "true"

if not client_accounts or any(
    not account["email"] or not account["password_hash"]
    for account in client_accounts
):
    raise RuntimeError(
        "Defina CLIENT_EMAIL e CLIENT_PASSWORD_HASH, ou uma lista válida em CLIENT_USER_ACCOUNTS_JSON."
    )

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
    {"id": "DOC-018", "contract": "CTR-2026-001", "title": "Relatório preventivo QGBT", "category": "Manutenção preventiva", "date": "2026-08-14"},
    {"id": "DOC-019", "contract": "CTR-2026-001", "title": "Relatório de ocorrência UPS", "category": "Manutenção corretiva", "date": "2026-08-16"},
    {"id": "DOC-016", "contract": "CTR-2026-001", "title": "Checklist de iluminação de emergência", "category": "Checklist", "date": "2026-07-22"},
    {"id": "DOC-003", "contract": "CTR-2026-001", "title": "Guia de desbloqueio do grupo gerador", "category": "Guia técnico", "date": "2026-06-03"},
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

def load_user_accounts():
    if not DATABASE_URL:
        return client_accounts

    connection = None
    try:
        connection = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        connection.autocommit = False
        cursor = connection.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS portal_users (
                id BIGSERIAL PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'Utilizador',
                status TEXT NOT NULL DEFAULT 'Activo',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS portal_user_clients (
                user_id BIGINT NOT NULL REFERENCES portal_users(id) ON DELETE CASCADE,
                numero_cliente TEXT NOT NULL,
                PRIMARY KEY (user_id, numero_cliente)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS portal_user_contracts (
                user_id BIGINT NOT NULL REFERENCES portal_users(id) ON DELETE CASCADE,
                numero_cliente TEXT NOT NULL,
                numero_contrato TEXT NOT NULL,
                PRIMARY KEY (user_id, numero_cliente, numero_contrato)
            )
            """
        )
        cursor.execute("SELECT COUNT(*) AS total FROM portal_users")
        if cursor.fetchone()["total"] == 0:
            first = client_accounts[0]
            cursor.execute(
                """
                INSERT INTO portal_users (email, password_hash, name, role)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (first["email"], first["password_hash"], first["name"], "Administrador"),
            )
            user_id = cursor.fetchone()["id"]
            for number in first["client_numbers"] or CLIENT_ALLOWED_NUMBERS:
                cursor.execute(
                    "INSERT INTO portal_user_clients (user_id, numero_cliente) VALUES (%s, %s)",
                    (user_id, number),
                )
        cursor.execute(
            """
            SELECT u.id, u.email, u.password_hash, u.name, u.role, u.status,
                   COALESCE(ARRAY_AGG(c.numero_cliente) FILTER (WHERE c.numero_cliente IS NOT NULL), '{}') AS client_numbers
            FROM portal_users u
            LEFT JOIN portal_user_clients c ON c.user_id = u.id
            WHERE u.status = 'Activo'
            GROUP BY u.id
            ORDER BY u.id
            """
        )
        accounts = [dict(row) for row in cursor.fetchall()]
        cursor.execute(
            """
            SELECT user_id, numero_cliente, ARRAY_AGG(numero_contrato ORDER BY numero_contrato) AS contracts
            FROM portal_user_contracts
            GROUP BY user_id, numero_cliente
            """
        )
        for account in accounts:
            account["contracts"] = {}
        for row in cursor.fetchall():
            account = next((item for item in accounts if item["id"] == row["user_id"]), None)
            if account is not None:
                account["contracts"][row["numero_cliente"]] = row["contracts"]
        connection.commit()
        return accounts
    except Exception:
        if connection is not None:
            connection.rollback()
        app.logger.exception("Falha ao consultar a base de dados de utilizadores")
        raise
    finally:
        if connection is not None:
            connection.close()


def current_user():
    accounts = load_user_accounts()
    user_id = session.get("user_id")
    if user_id is not None:
        for account in accounts:
            if str(account.get("id")) == str(user_id):
                return account
    user_index = session.get("user_index", 0)
    if isinstance(user_index, int) and 0 <= user_index < len(accounts):
        return accounts[user_index]
    return accounts[0]


def current_allowed_client_numbers():
    user_numbers = current_user().get("client_numbers", [])
    if user_numbers:
        return user_numbers
    return CLIENT_ALLOWED_NUMBERS


def portal_users_for_page():
    accounts = load_user_accounts()
    current_id = str(session.get("user_id", ""))
    return [
        {
            "name": account["name"],
            "email": account["email"],
            "role": account["role"],
            "status": "Activo",
            "last_access": "Acesso actual" if str(account.get("id", index)) == current_id else "—",
            "client_numbers": account["client_numbers"] or CLIENT_ALLOWED_NUMBERS,
            "contracts": account.get("contracts", {}),
        }
        for index, account in enumerate(accounts)
    ]


def create_portal_user(email, password, name, role, client_numbers, contracts):
    if not DATABASE_URL:
        return False, "Configure DATABASE_URL para guardar novos utilizadores."
    connection = None
    try:
        connection = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        cursor = connection.cursor()
        cursor.execute(
            """
            INSERT INTO portal_users (email, password_hash, name, role)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (email, generate_password_hash(password), name, role),
        )
        user_id = cursor.fetchone()[0]
        for number in client_numbers:
            cursor.execute(
                "INSERT INTO portal_user_clients (user_id, numero_cliente) VALUES (%s, %s)",
                (user_id, number),
            )
            for contract in contracts.get(number, []):
                cursor.execute(
                    "INSERT INTO portal_user_contracts (user_id, numero_cliente, numero_contrato) VALUES (%s, %s, %s)",
                    (user_id, number, contract),
                )
        connection.commit()
        return True, "Utilizador criado e associado aos clientes indicados."
    except psycopg2.errors.UniqueViolation:
        if connection is not None:
            connection.rollback()
        return False, "Já existe um utilizador com esse e-mail."
    except Exception:
        if connection is not None:
            connection.rollback()
        app.logger.exception("Falha ao criar utilizador")
        return False, "Não foi possível guardar o novo utilizador."
    finally:
        if connection is not None:
            connection.close()


def find_client_source(cursor):
    cursor.execute(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND (
            table_name IN ('clientes', 'cliente', 'registo_clientes', 'registo_equipamentos')
            OR table_name ILIKE '%cliente%'
            OR table_name ILIKE '%client%'
          )
        ORDER BY CASE table_name
          WHEN 'clientes' THEN 1
          WHEN 'cliente' THEN 2
          WHEN 'registo_clientes' THEN 3
          ELSE 4
        END
        """
    )
    table_columns = {}
    for row in cursor.fetchall():
        table_columns.setdefault(row["table_name"], set()).add(row["column_name"])
    for table_name, columns in table_columns.items():
        number_column = next(
            (column for column in ("numero_cliente", "numero", "id") if column in columns),
            None,
        )
        name_column = next(
            (
                column
                for column in (
                    "nome_empresa",
                    "nome_da_empresa",
                    "nome_cliente",
                    "cliente_nome",
                    "nome_fantasia",
                    "nome_comercial",
                    "nome_empresa_cliente",
                    "razao_social",
                    "designacao_social",
                    "empresa",
                    "designacao",
                    "descricao",
                    "nome",
                )
                if column in columns
            ),
            None,
        )
        if number_column and name_column:
            return table_name, number_column, name_column
    return None


def add_client_contracts(cursor, rows):
    numbers = [row["number"] for row in rows]
    for row in rows:
        row["contracts"] = []
    if not numbers:
        return rows
    cursor.execute(
        """
        SELECT TRIM(COALESCE(numero_cliente, '')) AS number,
               ARRAY_AGG(DISTINCT TRIM(COALESCE(numero_contrato, '')) ORDER BY TRIM(COALESCE(numero_contrato, ''))) AS contracts
        FROM registo_equipamentos
        WHERE TRIM(COALESCE(numero_cliente, '')) = ANY(%s)
          AND TRIM(COALESCE(numero_contrato, '')) <> ''
        GROUP BY TRIM(COALESCE(numero_cliente, ''))
        """,
        (numbers,),
    )
    contracts_by_number = {item["number"]: item["contracts"] for item in cursor.fetchall()}
    for row in rows:
        row["contracts"] = contracts_by_number.get(row["number"], [])
    return rows


def external_client_rows(numbers):
    numbers = list(numbers)
    if not numbers:
        return []
    if not DATABASE_URL_2:
        return [
            {
                "number": number,
                "name": "Cliente de teste",
                "contracts": sorted({item["numero_contrato"] for item in equipment if item["numero_cliente"] == number}),
            }
            for number in numbers
        ]

    connection = None
    try:
        connection = psycopg2.connect(
            DATABASE_URL_2,
            connect_timeout=5,
            options="-c default_transaction_read_only=on -c statement_timeout=5000",
        )
        connection.set_session(readonly=True, autocommit=False)
        cursor = connection.cursor(cursor_factory=RealDictCursor)
        source = find_client_source(cursor)
        if not source:
            return [{"number": number, "name": "Cliente associado"} for number in numbers]
        table_name, number_column, name_column = source
        name_expression = (
            f"COALESCE(NULLIF(TRIM(CAST({name_column} AS TEXT)), ''), CAST({number_column} AS TEXT))"
            if name_column
            else f"CAST({number_column} AS TEXT)"
        )
        cursor.execute(
            f"""
            SELECT TRIM(CAST({number_column} AS TEXT)) AS number,
                   {name_expression} AS name
            FROM {table_name}
            WHERE TRIM(CAST({number_column} AS TEXT)) = ANY(%s)
            ORDER BY number
            """,
            (numbers,),
        )
        rows = [
            {"number": row["number"], "name": row["name"] or "Cliente associado"}
            for row in cursor.fetchall()
        ]
        rows = add_client_contracts(cursor, rows)
        known_numbers = {row["number"] for row in rows}
        rows.extend(
            {"number": number, "name": "Cliente associado"}
            for number in numbers
            if number not in known_numbers
        )
        return rows
    except Exception:
        app.logger.exception("Falha ao consultar a tabela de clientes")
        return [{"number": number, "name": "Cliente associado"} for number in numbers]
    finally:
        if connection is not None:
            connection.rollback()
            connection.close()


def external_client_search(query):
    query = query.strip()[:100]
    if not DATABASE_URL_2:
        return [
            {
                "number": row["numero_cliente"],
                "name": "Cliente de teste",
                "contracts": [row["numero_contrato"]],
            }
            for row in equipment
            if not query or query.casefold() in row["numero_cliente"].casefold()
        ][:20]

    connection = None
    try:
        connection = psycopg2.connect(
            DATABASE_URL_2,
            connect_timeout=5,
            options="-c default_transaction_read_only=on -c statement_timeout=5000",
        )
        connection.set_session(readonly=True, autocommit=False)
        cursor = connection.cursor(cursor_factory=RealDictCursor)
        source = find_client_source(cursor)
        if not source:
            return []
        table_name, number_column, name_column = source
        number_expression = f"TRIM(CAST({number_column} AS TEXT))"
        name_expression = (
            f"COALESCE(NULLIF(TRIM(CAST({name_column} AS TEXT)), ''), {number_expression})"
            if name_column
            else number_expression
        )
        search = f"%{query}%"
        cursor.execute(
            f"""
            SELECT {number_expression} AS number, {name_expression} AS name
            FROM {table_name}
            WHERE {number_expression} ILIKE %s
               OR {name_expression} ILIKE %s
            ORDER BY name, number
            LIMIT 20
            """,
            (search, search),
        )
        rows = [
            {"number": row["number"], "name": row["name"] or row["number"]}
            for row in cursor.fetchall()
        ]
        return add_client_contracts(cursor, rows)
    except Exception:
        app.logger.exception("Falha na pesquisa de clientes")
        return []
    finally:
        if connection is not None:
            connection.rollback()
            connection.close()


def relevant_contracts():
    user = current_user()
    assigned_contracts = user.get("contracts", {})
    clients = external_client_rows(current_allowed_client_numbers())
    contracts = []
    for client in clients:
        numbers = assigned_contracts.get(client["number"]) or client.get("contracts", [])
        for number in numbers:
            contracts.append(
                {
                    "number": str(number),
                    "client_number": client["number"],
                    "client_name": client["name"],
                }
            )
    if not contracts and not DATABASE_URL_2:
        contracts = [
            {
                "number": "CTR-2026-001",
                "client_number": "TESTE-001",
                "client_name": client_profile["company"],
            }
        ]
    for contract in contracts:
        contract["document_count"] = sum(
            item["contract"] == contract["number"] for item in documents
        )
    return sorted(contracts, key=lambda item: (item["client_name"], item["number"]))


def documents_for_contract(contract_number):
    return [item for item in documents if item["contract"] == contract_number]


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
    rows = [
        {**item, "client_name": client_profile["company"]}
        for item in equipment
    ]
    allowed_numbers = current_allowed_client_numbers()
    selected_client = filters["cliente"] if filters["cliente"] in allowed_numbers else ""
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
    allowed_numbers = current_allowed_client_numbers()
    if not allowed_numbers and not EQUIPMENT_TEST_MODE:
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
        if allowed_numbers:
            scope_clause = "WHERE TRIM(COALESCE(numero_cliente, '')) = ANY(%s)"
            scope_params.append(allowed_numbers)

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
        if allowed_numbers:
            clauses.append("TRIM(COALESCE(numero_cliente, '')) = ANY(%s)")
            params.append(allowed_numbers)
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
        client_names = {
            item["number"]: item["name"]
            for item in external_client_rows({row["numero_cliente"] for row in rows})
        }
        for row in rows:
            row["client_name"] = client_names.get(row["numero_cliente"], "Cliente não identificado")
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
            and item["status"].casefold() not in {"serviço delegado", "servico delegado"}
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
        allowed_numbers = current_allowed_client_numbers()
        if allowed_numbers:
            scope_clause = "WHERE TRIM(COALESCE(numero_cliente, '')) = ANY(%s)"
            scope_params.append(allowed_numbers)

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
              AND LOWER(TRIM(COALESCE(estado, ''))) NOT IN ('serviço delegado', 'servico delegado')
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
                "intervention_type": intervention_type_label(row["tipo_checklist"]),
                "work_done": (row.get("respostas") or {}).get("trabalhos_realizados", "") if isinstance(row.get("respostas"), dict) else "",
            }
            for row in cursor.fetchall()
        ]
        try:
            cursor.execute(
                """
                SELECT
                    id,
                    tipo_servico,
                    titulo,
                    trabalhos_realizados,
                    estado_servico,
                    data_inicio,
                    data_fim,
                    numero_servico,
                    equipamento_1_id,
                    equipamento_2_id,
                    equipamento_3_id
                FROM sharepoint_intervencoes
                WHERE (
                    TRIM(COALESCE(equipamento_1_id, '')) = %s
                    OR TRIM(COALESCE(equipamento_2_id, '')) = %s
                    OR TRIM(COALESCE(equipamento_3_id, '')) = %s
                )
                  AND LOWER(TRIM(COALESCE(estado_servico, ''))) NOT IN ('serviço delegado', 'servico delegado')
                ORDER BY data_fim DESC NULLS LAST, data_inicio DESC NULLS LAST, id DESC
                LIMIT 100
                """,
                (selected_equipment, selected_equipment, selected_equipment),
            )
            for report in cursor.fetchall():
                rows.append(
                    {
                        "id": report["id"],
                        "title": report["titulo"] or report["trabalhos_realizados"] or "Relatório de intervenção",
                        "date": report["data_fim"] or report["data_inicio"] or "-",
                        "contract": report["numero_servico"] or "-",
                        "equipment": selected_equipment,
                        "location": "-",
                        "status": report["estado_servico"] or "-",
                        "technician": "-",
                        "intervention_type": intervention_type_label(report["tipo_servico"]),
                        "work_done": report["trabalhos_realizados"] or "",
                    }
                )
        except Exception:
            app.logger.exception("Falha ao consultar relatórios de intervenção")
            connection.rollback()
        return equipment_numbers, rows
    except Exception:
        app.logger.exception("Falha ao consultar histórico de manutenção")
        return [], "Não foi possível consultar o histórico de manutenção neste momento."
    finally:
        if connection is not None:
            connection.rollback()
            connection.close()


def intervention_type_label(value):
    text = str(value or "").strip()
    normalized = text.casefold()
    if "prevent" in normalized:
        return "Manutenção preventiva"
    if "corret" in normalized or "correc" in normalized:
        return "Manutenção correctiva"
    if "instal" in normalized or "obra" in normalized:
        return "Instalação"
    return text if text and "_" not in text else "Intervenção"


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
        allowed_numbers = current_allowed_client_numbers()
        if allowed_numbers:
            scope_clause = "AND TRIM(COALESCE(e.numero_cliente, '')) = ANY(%s)"
            params.append(allowed_numbers)
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
            cursor.execute(
                f"""
                SELECT s.*, e.tipo_equipamento, e.marca, e.modelo, e.numero_cliente
                FROM sharepoint_intervencoes s
                LEFT JOIN registo_equipamentos e ON
                    TRIM(COALESCE(e.numero_equipamento, '')) = TRIM(COALESCE(s.equipamento_1_id, ''))
                    OR TRIM(COALESCE(e.numero_equipamento, '')) = TRIM(COALESCE(s.equipamento_2_id, ''))
                    OR TRIM(COALESCE(e.numero_equipamento, '')) = TRIM(COALESCE(s.equipamento_3_id, ''))
                WHERE s.id = %s {scope_clause}
                ORDER BY e.id DESC NULLS LAST
                LIMIT 1
                """,
                params,
            )
            detail = cursor.fetchone()
            if not detail:
                return None
            detail["numero_equipamento"] = (
                detail.get("equipamento_1_id")
                or detail.get("equipamento_2_id")
                or detail.get("equipamento_3_id")
                or "-"
            )
            detail["numero_contrato"] = detail.get("numero_servico") or "-"
            detail["intervention_type"] = intervention_type_label(detail.get("tipo_servico"))
            detail["work_done"] = detail.get("trabalhos_realizados") or detail.get("observacoes_internas") or ""
            detail["title"] = detail.get("titulo") or detail["work_done"] or "Detalhe da intervenção"
            payload = detail.get("raw_payload") if isinstance(detail.get("raw_payload"), dict) else {}
            detail["numero_cliente"] = detail.get("cliente") or payload.get("cliente") or detail.get("nif") or "-"
            detail["data_checklist"] = detail.get("data_fim") or detail.get("data_inicio") or payload.get("data_fim") or payload.get("data_inicio")
            detail["estado"] = detail.get("estado_servico") or payload.get("estado_servico") or "-"
            detail["posicao"] = detail.get("posicao") or payload.get("posicao") or payload.get("localizacao") or "-"
            detail["tecnicos"] = detail.get("tecnicos") or payload.get("tecnicos") or "-"
            detail["tipo_equipamento"] = detail.get("equipamento_1_tipo") or payload.get("equipamento_1_tipo") or "-"
            detail["marca"] = detail.get("equipamento_1_marca") or payload.get("equipamento_1_marca") or "-"
            detail["modelo"] = detail.get("equipamento_1_modelo") or payload.get("equipamento_1_modelo") or "-"
            detail["comentarios"] = detail.get("observacoes_internas") or payload.get("observacoes_internas") or ""
            try:
                cursor.execute(
                    """
                    SELECT file_url, original_filename
                    FROM ri_anexos
                    WHERE ri_id = %s
                    ORDER BY id
                    """,
                    (int(intervention_id),),
                )
                detail["photos"] = [
                    {"file_url": row["file_url"], "file_name": row["original_filename"] or "Anexo"}
                    for row in cursor.fetchall()
                ]
            except Exception:
                connection.rollback()
                detail["photos"] = []
            return detail
        detail["intervention_type"] = intervention_type_label(detail.get("tipo_checklist"))
        detail["work_done"] = ""
        try:
            cursor.execute(
                """
                SELECT tipo_servico, trabalhos_realizados, observacoes_internas
                FROM sharepoint_intervencoes
                WHERE id = %s
                LIMIT 1
                """,
                (int(intervention_id),),
            )
            service = cursor.fetchone()
            if service:
                if service["tipo_servico"]:
                    detail["intervention_type"] = intervention_type_label(service["tipo_servico"])
                detail["work_done"] = service["trabalhos_realizados"] or service["observacoes_internas"] or ""
        except Exception:
            app.logger.exception("Falha ao consultar contexto da intervenção")
            connection.rollback()
        if not detail["work_done"] and isinstance(detail.get("respostas"), dict):
            detail["work_done"] = detail["respostas"].get("trabalhos_realizados") or ""
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


def validated_checklists(contract, start_date, end_date):
    if not DATABASE_URL_2 or not contract or not start_date or not end_date:
        return [], None
    connection = None
    try:
        connection = psycopg2.connect(
            DATABASE_URL_2,
            connect_timeout=5,
            options="-c default_transaction_read_only=on -c statement_timeout=5000",
        )
        connection.set_session(readonly=True, autocommit=False)
        cursor = connection.cursor(cursor_factory=RealDictCursor)
        params = [contract, start_date, end_date]
        scope_clause = ""
        allowed_numbers = current_allowed_client_numbers()
        if allowed_numbers:
            scope_clause = """
              AND EXISTS (
                SELECT 1 FROM registo_equipamentos e
                WHERE TRIM(COALESCE(e.numero_equipamento, '')) = TRIM(COALESCE(c.numero_equipamento, ''))
                  AND TRIM(COALESCE(e.numero_cliente, '')) = ANY(%s)
              )
            """
            params.append(allowed_numbers)
        cursor.execute(
            f"""
            SELECT c.id, c.tipo_checklist, c.data_checklist, c.numero_contrato,
                   c.numero_equipamento, c.posicao, c.estado, c.tecnicos, c.criado_por_nome
            FROM checklists_manutencao c
            WHERE TRIM(COALESCE(c.numero_contrato, '')) = %s
              AND c.data_checklist >= %s::date
              AND c.data_checklist <= %s::date
              AND LOWER(TRIM(COALESCE(c.estado, ''))) IN (
                'validado', 'serviço validado', 'servico validado',
                'serviço feito e validado', 'servico feito e validado'
              )
              {scope_clause}
            ORDER BY c.data_checklist DESC NULLS LAST, c.id DESC
            LIMIT 100
            """,
            params,
        )
        return cursor.fetchall(), None
    except Exception:
        app.logger.exception("Falha ao consultar checklists validadas")
        return [], "Não foi possível consultar as checklists neste momento."
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
        accounts = load_user_accounts()
        matching_index = next(
            (
                index
                for index, account in enumerate(accounts)
                if secrets.compare_digest(email, account["email"])
            ),
            -1,
        )
        # Store only the outcome and internal account index, never the email address.
        session["login_email_matches"] = matching_index >= 0
        session["login_user_index"] = matching_index
        return redirect(url_for("login_password"))

    session.pop("login_email_matches", None)
    session.pop("login_user_index", None)
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
        accounts = load_user_accounts()
        user_index = session.get("login_user_index", -1)
        password_hash = (
            accounts[user_index]["password_hash"]
            if isinstance(user_index, int) and 0 <= user_index < len(accounts)
            else accounts[0]["password_hash"]
        )
        password_matches = check_password_hash(password_hash, password)

        if email_matches and password_matches:
            if not is_contract_active():
                flash("O acesso não está disponível: o contrato indicado terminou.", "error")
                return redirect(url_for("login"))
            session.clear()
            session["logged_in"] = True
            session["user_id"] = accounts[user_index].get("id", user_index)
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
    allowed_numbers = current_allowed_client_numbers()
    visible_equipment = [
        item for item in equipment
        if not allowed_numbers or item["numero_cliente"] in allowed_numbers
    ]
    visible_equipment_ids = {item["id"] for item in visible_equipment}
    return render_template(
        "dashboard.html",
        profile=client_profile,
        cards=summary_cards,
        equipment=visible_equipment,
        maintenance=[item for item in maintenance if item["equipment"] in visible_equipment_ids],
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


@app.route("/checklists")
@login_required
def checklists():
    contract = request.args.get("contrato", "").strip()[:100]
    start_date = request.args.get("data_inicio", "").strip()[:10]
    end_date = request.args.get("data_fim", "").strip()[:10]
    rows, error = validated_checklists(contract, start_date, end_date)
    return render_template(
        "checklists.html",
        checklists=rows,
        contract=contract,
        start_date=start_date,
        end_date=end_date,
        error=error,
        searched=bool(contract or start_date or end_date),
    )


@app.route("/documentos")
@login_required
def documentos():
    return render_template("documents.html", contracts=relevant_contracts())


@app.route("/documentos/contrato/<contract_number>")
@login_required
def documentos_contrato(contract_number):
    contract = next(
        (item for item in relevant_contracts() if item["number"] == contract_number),
        None,
    )
    if not contract:
        abort(404)
    return render_template(
        "contract_documents.html",
        contract=contract,
        documents=documents_for_contract(contract_number),
    )


@app.route("/documentos/<document_id>/pdf")
@login_required
def documento_pdf(document_id):
    document = next((item for item in documents if item["id"] == document_id), None)
    allowed_contract_numbers = {item["number"] for item in relevant_contracts()}
    if not document or document["contract"] not in allowed_contract_numbers:
        abort(404)
    return pdf_response(f"{document_id.lower()}.pdf", [document])


@app.route("/documentos/contrato/<contract_number>/bundle.pdf")
@login_required
def documentos_bundle_pdf(contract_number):
    contract = next(
        (item for item in relevant_contracts() if item["number"] == contract_number),
        None,
    )
    if not contract:
        abort(404)
    return pdf_response(
        f"electromatic-{contract_number}.pdf",
        documents_for_contract(contract_number),
    )


@app.route("/documentos/bundle.pdf")
@login_required
def documentos_bundle_legacy():
    contracts = relevant_contracts()
    if not contracts:
        abort(404)
    contract_number = contracts[0]["number"]
    return pdf_response(
        f"electromatic-{contract_number}.pdf",
        documents_for_contract(contract_number),
    )


@app.route("/apoio", methods=["GET", "POST"])
@login_required
def apoio():
    reply = None
    if request.method == "POST":
        verify_csrf()
        message = request.form.get("message", "").strip().lower()
        if any(term in message for term in ("avaria", "urgente", "piquete")):
            reply = "Para uma ocorrência urgente fora do horário normal, contacte a piquete em 914 130 921."
        elif any(term in message for term in ("relatorio", "pdf", "checklist")):
            reply = "Os relatórios e checklists estão disponíveis na área Documentos."
        else:
            reply = "Posso orientar sobre equipamentos, manutenção, documentos ou contacto com o gestor."
    return render_template("support.html", profile=client_profile, guides=guides, reply=reply)


@app.route("/perfil")
@login_required
def perfil():
    return render_template("profile.html", profile=client_profile)


@app.route("/utilizadores", methods=["GET", "POST"])
@login_required
def utilizadores():
    if request.method == "POST":
        verify_csrf()
        if current_user().get("role") != "Administrador":
            abort(403)
        email = request.form.get("email", "").strip().lower()
        name = request.form.get("name", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "Utilizador").strip() or "Utilizador"
        client_numbers = sorted(
            {
                value.strip()
                for value in request.form.get("client_numbers", "").replace(";", ",").split(",")
                if value.strip()
            }
        )
        try:
            selected_contracts = json.loads(request.form.get("client_contracts", "{}"))
        except json.JSONDecodeError:
            selected_contracts = {}
        selected_contracts = {
            number: [str(contract).strip() for contract in selected_contracts.get(number, []) if str(contract).strip()]
            for number in client_numbers
        } if isinstance(selected_contracts, dict) else {}
        if not email or not name or len(password) < 12 or not client_numbers or any(
            not selected_contracts.get(number) for number in client_numbers
        ):
            flash("Indique nome, e-mail, palavra-passe com 12 caracteres e pelo menos um cliente.", "error")
        else:
            created, message = create_portal_user(
                email, password, name, role, client_numbers, selected_contracts
            )
            flash(message, "success" if created else "error")
        return redirect(url_for("utilizadores"))

    users = portal_users_for_page()
    if current_user().get("role") != "Administrador":
        users = [users[session.get("user_index", 0)]]
    associated_numbers = sorted(
        {
            number
            for user in users
            for number in user["client_numbers"]
        }
    )
    return render_template(
        "users.html",
        users=users,
        active_count=sum(user["status"] == "Activo" for user in users),
        clients=external_client_rows(associated_numbers),
        can_manage=current_user().get("role") == "Administrador" and bool(DATABASE_URL),
        current_user_role=current_user().get("role"),
        profile=client_profile,
    )


@app.route("/api/clientes")
@login_required
def api_clientes():
    if current_user().get("role") != "Administrador":
        abort(403)
    query = request.args.get("q", "").strip()
    if len(query) < 2:
        return jsonify([])
    return jsonify(external_client_search(query))


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
