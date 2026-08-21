import json
import os
import secrets
import time
from collections import defaultdict, deque
from datetime import date, timedelta
from io import BytesIO
from functools import wraps
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4
from xml.sax.saxutils import escape

import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from PIL import Image as PillowImage
from PIL import ImageOps
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
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
PDF_MAX_JOB_BYTES = int(os.environ.get("PDF_MAX_JOB_BYTES", str(50 * 1024 * 1024)))
PDF_MAX_PHOTO_BYTES = int(os.environ.get("PDF_MAX_PHOTO_BYTES", str(4 * 1024 * 1024)))
PDF_MAX_PHOTO_PIXELS = int(os.environ.get("PDF_MAX_PHOTO_PIXELS", str(12_000_000)))
PDF_MAX_PHOTOS_PER_REPORT = int(os.environ.get("PDF_MAX_PHOTOS_PER_REPORT", "12"))
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
            try:
                connection.rollback()
            except psycopg2.Error:
                pass
            finally:
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


def dashboard_equipment_context():
    contracts = relevant_contracts()
    allowed_numbers = current_allowed_client_numbers()
    allowed_contract_numbers = [item["number"] for item in contracts]
    if not DATABASE_URL_2:
        visible_equipment = [
            item for item in equipment
            if (not allowed_numbers or item["numero_cliente"] in allowed_numbers)
            and (not allowed_contract_numbers or item["numero_contrato"] in allowed_contract_numbers)
        ]
        return visible_equipment, len(visible_equipment), contracts
    if not allowed_numbers and not EQUIPMENT_TEST_MODE:
        return [], 0, contracts

    connection = None
    try:
        connection = psycopg2.connect(
            DATABASE_URL_2,
            connect_timeout=5,
            options="-c default_transaction_read_only=on -c statement_timeout=5000",
        )
        connection.set_session(readonly=True, autocommit=False)
        cursor = connection.cursor(cursor_factory=RealDictCursor)
        clauses = ["TRIM(COALESCE(numero_equipamento, '')) <> ''"]
        params = []
        if allowed_numbers:
            clauses.append("TRIM(COALESCE(numero_cliente, '')) = ANY(%s)")
            params.append(allowed_numbers)
        if allowed_contract_numbers:
            clauses.append("TRIM(COALESCE(numero_contrato, '')) = ANY(%s)")
            params.append(allowed_contract_numbers)
        where_clause = " AND ".join(clauses)
        cursor.execute(
            f"""
            SELECT COUNT(DISTINCT TRIM(COALESCE(numero_equipamento, ''))) AS total
            FROM registo_equipamentos
            WHERE {where_clause}
            """,
            params,
        )
        equipment_total = cursor.fetchone()["total"]
        cursor.execute(
            f"""
            SELECT DISTINCT ON (TRIM(COALESCE(numero_equipamento, '')))
                TRIM(COALESCE(numero_equipamento, '')) AS id,
                COALESCE(NULLIF(TRIM(tipo_equipamento), ''), 'Sem tipo') AS name,
                TRIM(COALESCE(numero_contrato, '')) AS numero_contrato,
                TRIM(COALESCE(numero_cliente, '')) AS numero_cliente
            FROM registo_equipamentos
            WHERE {where_clause}
            ORDER BY TRIM(COALESCE(numero_equipamento, '')), registo_equipamentos.id DESC
            LIMIT 3
            """,
            params,
        )
        return [dict(row) for row in cursor.fetchall()], equipment_total, contracts
    except Exception:
        app.logger.exception("Falha ao consultar o resumo de equipamentos")
        return [], 0, contracts
    finally:
        if connection is not None:
            connection.rollback()
            connection.close()


def recent_intervention_reports(contracts, limit=3):
    if not DATABASE_URL_2:
        return [
            {
                "id": item["id"],
                "title": item["title"],
                "contract": "CTR-2026-001",
                "status": item["status"],
            }
            for item in maintenance[:limit]
        ]

    allowed_numbers = current_allowed_client_numbers()
    allowed_contract_numbers = [item["number"] for item in contracts]
    if not allowed_numbers and not EQUIPMENT_TEST_MODE:
        return []

    connection = None
    try:
        connection = psycopg2.connect(
            DATABASE_URL_2,
            connect_timeout=5,
            options="-c default_transaction_read_only=on -c statement_timeout=5000",
        )
        connection.set_session(readonly=True, autocommit=False)
        cursor = connection.cursor(cursor_factory=RealDictCursor)
        clauses = [
            """
            LOWER(TRIM(COALESCE(s.estado_servico, ''))) NOT IN (
                'serviço delegado', 'servico delegado',
                'checklist por validar', 'checklist pendente de validação',
                'checklist pendente de validacao'
            )
            """
        ]
        params = []
        if allowed_numbers or allowed_contract_numbers:
            equipment_scope = [
                """
                TRIM(COALESCE(e.numero_equipamento, '')) = TRIM(COALESCE(s.equipamento_1_id, ''))
                OR TRIM(COALESCE(e.numero_equipamento, '')) = TRIM(COALESCE(s.equipamento_2_id, ''))
                OR TRIM(COALESCE(e.numero_equipamento, '')) = TRIM(COALESCE(s.equipamento_3_id, ''))
                """
            ]
            if allowed_numbers:
                equipment_scope.append("TRIM(COALESCE(e.numero_cliente, '')) = ANY(%s)")
                params.append(allowed_numbers)
            if allowed_contract_numbers:
                equipment_scope.append("TRIM(COALESCE(e.numero_contrato, '')) = ANY(%s)")
                params.append(allowed_contract_numbers)
            clauses.append(
                "EXISTS (SELECT 1 FROM registo_equipamentos e WHERE "
                + " AND ".join(f"({condition.strip()})" for condition in equipment_scope)
                + ")"
            )
        params.append(limit)
        cursor.execute(
            f"""
            SELECT s.id,
                   COALESCE(NULLIF(TRIM(s.titulo), ''), NULLIF(TRIM(s.trabalhos_realizados), ''), 'Relatório de intervenção') AS title,
                   COALESCE(s.data_fim, s.data_inicio) AS date,
                   COALESCE(NULLIF(TRIM(s.estado_servico), ''), '-') AS status,
                   COALESCE(NULLIF(TRIM(s.numero_servico), ''), '-') AS contract
            FROM sharepoint_intervencoes s
            WHERE {' AND '.join(clauses)}
            ORDER BY s.data_fim DESC NULLS LAST, s.data_inicio DESC NULLS LAST, s.id DESC
            LIMIT %s
            """,
            params,
        )
        return [dict(row) for row in cursor.fetchall()]
    except Exception:
        app.logger.exception("Falha ao consultar as RIs recentes")
        return []
    finally:
        if connection is not None:
            try:
                connection.rollback()
            except psycopg2.Error:
                pass
            finally:
                connection.close()


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


def pdf_text(value):
    return escape(str(value or "-")).replace("\n", "<br/>")


def pdf_photo(url, caption):
    parsed_url = urlparse(str(url or ""))
    if parsed_url.scheme not in {"http", "https"}:
        return None
    try:
        photo_request = Request(str(url), headers={"User-Agent": "Electromatic-AreaCliente/1.0"})
        with urlopen(photo_request, timeout=6) as response:
            content_length = int(response.headers.get("Content-Length", "0") or 0)
            if content_length > PDF_MAX_PHOTO_BYTES:
                return None
            photo_data = response.read(PDF_MAX_PHOTO_BYTES + 1)
        if len(photo_data) > PDF_MAX_PHOTO_BYTES:
            return None
        with PillowImage.open(BytesIO(photo_data)) as source:
            if source.width * source.height > PDF_MAX_PHOTO_PIXELS:
                return None
            image_source = ImageOps.exif_transpose(source)
            if image_source.mode not in {"RGB", "L"}:
                image_source = image_source.convert("RGB")
            image_source.thumbnail((1280, 960), PillowImage.Resampling.LANCZOS)
            compressed_photo = BytesIO()
            image_source.save(compressed_photo, format="JPEG", quality=72, optimize=True)
        compressed_photo.seek(0)
        image = Image(compressed_photo)
        # Mantém o buffer comprimido disponível até o ReportLab terminar o documento.
        image._compressed_source = compressed_photo
        image._restrictSize(82 * mm, 62 * mm)
        return [image, Spacer(1, 2 * mm), Paragraph(pdf_text(caption), PDF_STYLES["photo_caption"])]
    except Exception:
        app.logger.warning("Não foi possível incluir uma fotografia no PDF")
        return None


def checklist_pdf_header(pdf_canvas, document):
    page_width, page_height = A4
    pdf_canvas.saveState()
    pdf_canvas.setFillColor(colors.HexColor("#171717"))
    pdf_canvas.rect(0, page_height - 22 * mm, page_width, 22 * mm, fill=1, stroke=0)
    pdf_canvas.setFillColor(colors.HexColor("#f58200"))
    pdf_canvas.rect(0, page_height - 22 * mm, 7 * mm, 22 * mm, fill=1, stroke=0)
    pdf_canvas.setFillColor(colors.white)
    pdf_canvas.setFont("Helvetica-Bold", 9)
    pdf_canvas.drawString(12 * mm, page_height - 13 * mm, "ELECTROMATIC")
    pdf_canvas.setFont("Helvetica", 8)
    pdf_canvas.drawRightString(page_width - 12 * mm, page_height - 13 * mm, "Área de cliente | Manutenções e inspeções")
    pdf_canvas.setStrokeColor(colors.HexColor("#dedbd5"))
    pdf_canvas.line(12 * mm, 12 * mm, page_width - 12 * mm, 12 * mm)
    pdf_canvas.setFillColor(colors.HexColor("#656565"))
    pdf_canvas.setFont("Helvetica", 7)
    pdf_canvas.drawString(12 * mm, 7 * mm, "Relatório gerado pela Área de Cliente Electromatic")
    pdf_canvas.drawRightString(page_width - 12 * mm, 7 * mm, f"Página {document.page}")
    pdf_canvas.restoreState()


def build_checklist_pdf(rows, start_date, end_date, equipment_number, allowed_numbers=None, progress_callback=None):
    def report_progress(progress, stage):
        if progress_callback is not None:
            progress_callback(progress, stage)

    report_progress(8, "A preparar o documento")
    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        rightMargin=12 * mm,
        leftMargin=12 * mm,
        topMargin=30 * mm,
        bottomMargin=18 * mm,
        title="Manutenções e inspeções Electromatic",
        author="Electromatic",
    )
    story = [
        Paragraph("Manutenções e inspeções", PDF_STYLES["title"]),
        Paragraph("Relatórios validados", PDF_STYLES["subtitle"]),
        Spacer(1, 5 * mm),
    ]
    scope_label = f"Equipamento #{equipment_number}" if equipment_number else "Todos os equipamentos autorizados"
    summary = Table(
        [["Período", "Registos validados", "Âmbito"], [f"{start_date} a {end_date}", str(len(rows)), scope_label]],
        colWidths=[48 * mm, 38 * mm, 90 * mm],
    )
    summary.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f0ede9")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#4a4a4a")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#dedbd5")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.extend([summary, Spacer(1, 7 * mm)])

    details = []
    total_rows = len(rows)
    for index, row in enumerate(rows, start=1):
        detail = maintenance_detail(row["id"], allowed_numbers=allowed_numbers)
        if detail:
            details.append(detail)
        if total_rows:
            report_progress(10 + int(35 * index / total_rows), "A recolher relatórios e fotografias")
    details = [detail for detail in details if detail]
    if not details:
        story.append(Paragraph("Não existem relatórios validados para o período selecionado.", PDF_STYLES["body"]))

    for index, detail in enumerate(details):
        if details:
            report_progress(45 + int(45 * (index + 1) / len(details)), "A compor o PDF")
        if index:
            story.append(PageBreak())
        equipment_name = detail.get("tipo_equipamento") or "Equipamento"
        detail_equipment_number = detail.get("numero_equipamento") or "-"
        contract_number = detail.get("numero_contrato") or "-"
        title = Table(
            [["", Paragraph(f"<b>{pdf_text(equipment_name)} #{pdf_text(detail_equipment_number)}</b><br/><font size='7'>Contrato {pdf_text(contract_number)}</font>", PDF_STYLES["report_header"]) ]],
            colWidths=[7 * mm, 169 * mm],
        )
        title.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#f58200")),
            ("BACKGROUND", (1, 0), (1, 0), colors.HexColor("#171717")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ("LEFTPADDING", (1, 0), (1, 0), 8),
        ]))
        story.extend([title, Spacer(1, 4 * mm)])

        detail_table = Table([
            ["Data", "Contrato", "Tipo de equipamento", "Técnico(s)"],
            [pdf_text(detail.get("data_checklist")), pdf_text(contract_number), pdf_text(equipment_name), pdf_text(detail.get("tecnicos") or detail.get("criado_por_nome"))],
            ["Posição", "Estado", "Cliente", "Criado em"],
            [pdf_text(detail.get("posicao")), pdf_text(detail.get("estado")), pdf_text(detail.get("numero_cliente")), pdf_text(detail.get("created_at"))],
        ], colWidths=[44 * mm, 38 * mm, 47 * mm, 47 * mm])
        detail_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f0ede9")),
            ("BACKGROUND", (0, 2), (-1, 2), colors.HexColor("#f0ede9")),
            ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTNAME", (0, 2), (-1, 2), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#dedbd5")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.extend([detail_table, Spacer(1, 5 * mm)])

        responses = detail.get("respostas") if isinstance(detail.get("respostas"), dict) else {}
        for section in responses.get("secoes") or []:
            story.append(Paragraph(pdf_text(section.get("titulo") or "Checklist"), PDF_STYLES["section"]))
            question_rows = [[Paragraph("Pergunta", PDF_STYLES["table_head"]), Paragraph("Resposta", PDF_STYLES["table_head"])]]
            for question in section.get("perguntas") or []:
                question_title = question.get("pergunta") or question.get("codigo") or "Pergunta"
                description = question.get("descricao")
                question_copy = f"<b>{pdf_text(question_title)}</b>"
                if description:
                    question_copy += f"<br/><font size='6'>{pdf_text(description)}</font>"
                question_rows.append([
                    Paragraph(question_copy, PDF_STYLES["table_body"]),
                    Paragraph(pdf_text(question.get("resposta")), PDF_STYLES["table_body"]),
                ])
            if len(question_rows) > 1:
                checklist_table = Table(question_rows, colWidths=[138 * mm, 38 * mm], repeatRows=1)
                checklist_table.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#171717")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#dedbd5")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ]))
                story.extend([checklist_table, Spacer(1, 5 * mm)])

        for label, value in (("Trabalhos realizados", detail.get("work_done")), ("Comentários", detail.get("comentarios"))):
            if value:
                story.extend([
                    Paragraph(label, PDF_STYLES["section"]),
                    Paragraph(pdf_text(value), PDF_STYLES["body"]),
                    Spacer(1, 4 * mm),
                ])

        report_photos = (detail.get("photos") or [])[:PDF_MAX_PHOTOS_PER_REPORT]
        photo_cells = [pdf_photo(photo.get("file_url"), photo.get("file_name")) for photo in report_photos]
        photo_cells = [cell for cell in photo_cells if cell]
        if photo_cells:
            story.append(Paragraph("Fotografias", PDF_STYLES["section"]))
            photo_rows = [photo_cells[position:position + 2] for position in range(0, len(photo_cells), 2)]
            if photo_rows and len(photo_rows[-1]) == 1:
                photo_rows[-1].append("")
            photos_table = Table(photo_rows, colWidths=[88 * mm, 88 * mm])
            photos_table.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))
            story.append(photos_table)

    report_progress(94, "A finalizar o ficheiro")
    document.build(story, onFirstPage=checklist_pdf_header, onLaterPages=checklist_pdf_header)
    output.seek(0)
    suffix = f"-{equipment_number}" if equipment_number else ""
    filename = f"manutencoes{suffix}-{start_date}-{end_date}.pdf"
    report_progress(98, "A guardar o ficheiro")
    return output, filename


def checklist_pdf_response(rows, start_date, end_date, equipment_number):
    output, filename = build_checklist_pdf(rows, start_date, end_date, equipment_number)
    return send_file(
        output,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )


PDF_STYLES = {
    "title": ParagraphStyle("PdfTitle", fontName="Helvetica-Bold", fontSize=19, leading=23, textColor=colors.HexColor("#171717")),
    "subtitle": ParagraphStyle("PdfSubtitle", fontName="Helvetica", fontSize=9, leading=12, textColor=colors.HexColor("#656565")),
    "report_header": ParagraphStyle("PdfReportHeader", fontName="Helvetica", fontSize=10, leading=13, textColor=colors.white),
    "section": ParagraphStyle("PdfSection", fontName="Helvetica-Bold", fontSize=10, leading=13, textColor=colors.HexColor("#171717"), spaceBefore=2 * mm, spaceAfter=2 * mm),
    "table_head": ParagraphStyle("PdfTableHead", fontName="Helvetica-Bold", fontSize=7, leading=9, textColor=colors.white),
    "table_body": ParagraphStyle("PdfTableBody", fontName="Helvetica", fontSize=7, leading=9, textColor=colors.HexColor("#171717")),
    "body": ParagraphStyle("PdfBody", fontName="Helvetica", fontSize=8, leading=11, textColor=colors.HexColor("#171717")),
    "photo_caption": ParagraphStyle("PdfPhotoCaption", fontName="Helvetica", fontSize=6, leading=8, textColor=colors.HexColor("#656565")),
}


PDF_JOB_LABELS = {
    "queued": "Em fila",
    "processing": "Em preparação",
    "completed": "PDF pronto",
    "failed": "Não foi possível gerar",
}


def ensure_pdf_jobs_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS portal_pdf_jobs (
            id TEXT PRIMARY KEY,
            requested_by TEXT NOT NULL,
            start_date DATE NOT NULL,
            end_date DATE NOT NULL,
            equipment_number TEXT NOT NULL DEFAULT '',
            access_scope JSONB NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            progress SMALLINT NOT NULL DEFAULT 0,
            stage TEXT NOT NULL DEFAULT 'Em fila',
            file_name TEXT,
            pdf_data BYTEA,
            error_message TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            started_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS portal_pdf_jobs_pending_idx "
        "ON portal_pdf_jobs (status, created_at)"
    )


def current_pdf_access_scope():
    return {
        "numbers": [str(number) for number in current_allowed_client_numbers()],
        "contracts": [str(item["number"]) for item in relevant_contracts()],
    }


def enqueue_checklist_pdf_job(requested_by, start_date, end_date, equipment_number, access_scope):
    if not DATABASE_URL:
        return None
    connection = None
    try:
        connection = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        cursor = connection.cursor()
        ensure_pdf_jobs_table(cursor)
        cursor.execute(
            """
            SELECT id
            FROM portal_pdf_jobs
            WHERE requested_by = %s
              AND start_date = %s::date
              AND end_date = %s::date
              AND equipment_number = %s
              AND status IN ('queued', 'processing')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (requested_by, start_date, end_date, equipment_number),
        )
        existing_job = cursor.fetchone()
        if existing_job:
            connection.commit()
            return existing_job[0]
        job_id = uuid4().hex
        cursor.execute(
            """
            INSERT INTO portal_pdf_jobs (
                id, requested_by, start_date, end_date, equipment_number, access_scope
            ) VALUES (%s, %s, %s::date, %s::date, %s, %s::jsonb)
            """,
            (job_id, requested_by, start_date, end_date, equipment_number, json.dumps(access_scope)),
        )
        connection.commit()
        return job_id
    except Exception:
        if connection is not None:
            connection.rollback()
        app.logger.exception("Falha ao criar a tarefa de PDF")
        return None
    finally:
        if connection is not None:
            connection.close()


def pdf_jobs_for_user(requested_by, limit=5):
    if not DATABASE_URL:
        return []
    connection = None
    try:
        connection = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        cursor = connection.cursor(cursor_factory=RealDictCursor)
        ensure_pdf_jobs_table(cursor)
        cursor.execute(
            """
            SELECT id, start_date, end_date, equipment_number, status, progress, stage,
                   file_name, error_message, created_at
            FROM portal_pdf_jobs
            WHERE requested_by = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (requested_by, limit),
        )
        jobs = [dict(row) for row in cursor.fetchall()]
        connection.commit()
        for job in jobs:
            job["status_label"] = PDF_JOB_LABELS.get(job["status"], "Em preparação")
            job["progress"] = max(0, min(100, int(job["progress"] or 0)))
        return jobs
    except Exception:
        if connection is not None:
            connection.rollback()
        app.logger.exception("Falha ao consultar tarefas de PDF")
        return []
    finally:
        if connection is not None:
            connection.close()


def pdf_job_for_user(job_id, requested_by, include_file=False):
    if not DATABASE_URL or not job_id:
        return None
    connection = None
    try:
        connection = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        cursor = connection.cursor(cursor_factory=RealDictCursor)
        ensure_pdf_jobs_table(cursor)
        file_column = ", pdf_data" if include_file else ""
        cursor.execute(
            f"""
            SELECT id, start_date, end_date, equipment_number, status, progress, stage,
                   file_name, error_message, created_at {file_column}
            FROM portal_pdf_jobs
            WHERE id = %s AND requested_by = %s
            """,
            (job_id, requested_by),
        )
        job = cursor.fetchone()
        connection.commit()
        return dict(job) if job else None
    except Exception:
        if connection is not None:
            connection.rollback()
        app.logger.exception("Falha ao consultar tarefa de PDF")
        return None
    finally:
        if connection is not None:
            connection.close()


def update_pdf_job(job_id, progress, stage):
    connection = None
    try:
        connection = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        cursor = connection.cursor()
        cursor.execute(
            """
            UPDATE portal_pdf_jobs
            SET progress = GREATEST(progress, %s), stage = %s
            WHERE id = %s AND status = 'processing'
            """,
            (max(0, min(99, int(progress))), stage, job_id),
        )
        connection.commit()
    except Exception:
        if connection is not None:
            connection.rollback()
        app.logger.exception("Falha ao actualizar progresso do PDF")
    finally:
        if connection is not None:
            connection.close()


def claim_next_pdf_job():
    if not DATABASE_URL:
        return None
    connection = None
    try:
        connection = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        cursor = connection.cursor(cursor_factory=RealDictCursor)
        ensure_pdf_jobs_table(cursor)
        cursor.execute(
            """
            UPDATE portal_pdf_jobs
            SET status = 'queued', progress = 0, stage = 'Em fila', started_at = NULL
            WHERE status = 'processing' AND started_at < NOW() - INTERVAL '10 minutes'
            """
        )
        cursor.execute(
            """
            WITH next_job AS (
                SELECT id
                FROM portal_pdf_jobs
                WHERE status = 'queued'
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE portal_pdf_jobs job
            SET status = 'processing', progress = 5, stage = 'A preparar relatórios', started_at = NOW()
            FROM next_job
            WHERE job.id = next_job.id
            RETURNING job.*
            """
        )
        job = cursor.fetchone()
        connection.commit()
        return dict(job) if job else None
    except Exception:
        if connection is not None:
            connection.rollback()
        app.logger.exception("Falha ao obter tarefa de PDF")
        return None
    finally:
        if connection is not None:
            connection.close()


def finish_pdf_job(job_id, output, file_name):
    payload = output.getvalue()
    if len(payload) > PDF_MAX_JOB_BYTES:
        raise ValueError("O ficheiro excede o limite de armazenamento temporário.")
    connection = None
    try:
        connection = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        cursor = connection.cursor()
        cursor.execute(
            """
            UPDATE portal_pdf_jobs
            SET status = 'completed', progress = 100, stage = 'PDF pronto', file_name = %s,
                pdf_data = %s, error_message = NULL, completed_at = NOW()
            WHERE id = %s AND status = 'processing'
            """,
            (file_name, psycopg2.Binary(payload), job_id),
        )
        connection.commit()
    finally:
        if connection is not None:
            connection.close()


def fail_pdf_job(job_id):
    connection = None
    try:
        connection = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        cursor = connection.cursor()
        cursor.execute(
            """
            UPDATE portal_pdf_jobs
            SET status = 'failed', progress = 100, stage = 'Não foi possível gerar o PDF',
                error_message = 'Tente novamente dentro de alguns minutos.', completed_at = NOW()
            WHERE id = %s
            """,
            (job_id,),
        )
        connection.commit()
    except Exception:
        if connection is not None:
            connection.rollback()
        app.logger.exception("Falha ao terminar tarefa de PDF")
    finally:
        if connection is not None:
            connection.close()


def cleanup_expired_pdf_jobs():
    if not DATABASE_URL:
        return
    connection = None
    try:
        connection = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        cursor = connection.cursor()
        ensure_pdf_jobs_table(cursor)
        cursor.execute(
            """
            DELETE FROM portal_pdf_jobs
            WHERE status IN ('completed', 'failed')
              AND completed_at < NOW() - INTERVAL '48 hours'
            """
        )
        connection.commit()
    except Exception:
        if connection is not None:
            connection.rollback()
        app.logger.exception("Falha ao limpar tarefas de PDF expiradas")
    finally:
        if connection is not None:
            connection.close()


def process_next_pdf_job():
    job = claim_next_pdf_job()
    if not job:
        return False
    try:
        scope = job.get("access_scope") or {}
        if isinstance(scope, str):
            scope = json.loads(scope)
        allowed_numbers = [str(value) for value in scope.get("numbers", [])]
        allowed_contract_numbers = [str(value) for value in scope.get("contracts", [])]
        update_pdf_job(job["id"], 10, "A consultar registos validados")
        rows, error = validated_checklists(
            "",
            str(job["start_date"]),
            str(job["end_date"]),
            job.get("equipment_number") or "",
            limit=None,
            allowed_numbers=allowed_numbers,
            allowed_contract_numbers=allowed_contract_numbers,
        )
        if error:
            raise RuntimeError(error)
        output, file_name = build_checklist_pdf(
            rows,
            str(job["start_date"]),
            str(job["end_date"]),
            job.get("equipment_number") or "",
            allowed_numbers=allowed_numbers,
            progress_callback=lambda progress, stage: update_pdf_job(job["id"], progress, stage),
        )
        finish_pdf_job(job["id"], output, file_name)
    except Exception:
        app.logger.exception("Falha ao gerar PDF em segundo plano")
        fail_pdf_job(job["id"])
    return True


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
        allowed_numbers = current_allowed_client_numbers()
        equipment_options = [
            {
                "number": row["id"],
                "type": row["type"],
                "contract": row["numero_contrato"],
                "location": row["location"],
            }
            for row in equipment
            if not allowed_numbers or row["numero_cliente"] in allowed_numbers
        ]
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
        return equipment_options, rows

    connection = None
    try:
        connection = psycopg2.connect(
            DATABASE_URL_2,
            connect_timeout=5,
            options="-c default_transaction_read_only=on -c statement_timeout=5000",
        )
        connection.set_session(readonly=True, autocommit=False)
        cursor = connection.cursor(cursor_factory=RealDictCursor)

        allowed_numbers = current_allowed_client_numbers()
        scope_clause = "WHERE TRIM(COALESCE(numero_equipamento, '')) <> ''"
        scope_params = []
        if allowed_numbers:
            scope_clause += " AND TRIM(COALESCE(numero_cliente, '')) = ANY(%s)"
            scope_params.append(allowed_numbers)

        cursor.execute(
            f"""
            SELECT DISTINCT ON (TRIM(COALESCE(numero_equipamento, '')))
                TRIM(COALESCE(numero_equipamento, '')) AS numero_equipamento,
                COALESCE(NULLIF(TRIM(tipo_equipamento), ''), 'Sem tipo') AS tipo_equipamento,
                COALESCE(NULLIF(TRIM(numero_contrato), ''), '-') AS numero_contrato,
                COALESCE(NULLIF(TRIM(posicao), ''), '-') AS posicao
            FROM registo_equipamentos
            {scope_clause}
            ORDER BY TRIM(COALESCE(numero_equipamento, '')), registo_equipamentos.id DESC
            LIMIT 500
            """,
            scope_params,
        )
        equipment_options = [
            {
                "number": row["numero_equipamento"],
                "type": row["tipo_equipamento"],
                "contract": row["numero_contrato"],
                "location": row["posicao"],
            }
            for row in cursor.fetchall()
        ]
        equipment_numbers = [row["number"] for row in equipment_options]
        if not selected_equipment:
            return equipment_options, None
        if selected_equipment not in equipment_numbers:
            return equipment_options, "O equipamento seleccionado não está disponível para esta conta."

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
              AND LOWER(TRIM(COALESCE(estado, ''))) IN (
                'validado', 'serviço validado', 'servico validado',
                'serviço feito e validado', 'servico feito e validado'
              )
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
                  AND LOWER(TRIM(COALESCE(estado_servico, ''))) NOT IN (
                    'serviço delegado', 'servico delegado',
                    'checklist por validar', 'checklist pendente de validação',
                    'checklist pendente de validacao'
                  )
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
        return equipment_options, rows
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


def maintenance_detail(intervention_id, allowed_numbers=None):
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
        if allowed_numbers is None:
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
              AND LOWER(TRIM(COALESCE(c.estado, ''))) IN (
                'validado', 'serviço validado', 'servico validado',
                'serviço feito e validado', 'servico feito e validado'
              )
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
                  AND LOWER(TRIM(COALESCE(s.estado_servico, ''))) NOT IN (
                    'serviço delegado', 'servico delegado',
                    'checklist por validar', 'checklist pendente de validação',
                    'checklist pendente de validacao'
                  )
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


def maintenance_category(value):
    normalized = str(value or "").casefold()
    if "inspe" in normalized:
        return "Inspeção", "inspection"
    if "corret" in normalized or "correc" in normalized:
        return "Corretiva", "corrective"
    return "Manutenção", "maintenance"


def maintenance_by_month(rows):
    month_names = (
        "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
        "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
    )
    groups = {}
    for row in rows:
        date_value = row.get("data_checklist")
        try:
            parsed_date = date.fromisoformat(str(date_value)[:10])
            month_key = f"{parsed_date.year:04d}-{parsed_date.month:02d}"
            month_label = f"{month_names[parsed_date.month - 1]} {parsed_date.year}"
        except (TypeError, ValueError):
            month_key = "0000-00"
            month_label = "Data não disponível"
        category, tone = maintenance_category(row.get("tipo_checklist"))
        item = dict(row)
        item["category"] = category
        item["tone"] = tone
        groups.setdefault(month_key, {"label": month_label, "records": []})["records"].append(item)
    return [groups[key] for key in sorted(groups, reverse=True)]


def validated_checklists(
    contract,
    start_date,
    end_date,
    equipment_number="",
    limit=100,
    allowed_numbers=None,
    allowed_contract_numbers=None,
):
    if not DATABASE_URL_2 or not start_date or not end_date:
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
        params = [start_date, end_date]
        contract_clause = ""
        if allowed_contract_numbers is None:
            allowed_contract_numbers = [item["number"] for item in relevant_contracts()]
        if allowed_contract_numbers:
            contract_clause = "AND TRIM(COALESCE(c.numero_contrato, '')) = ANY(%s)"
            params.append(allowed_contract_numbers)
        elif contract:
            contract_clause = "AND TRIM(COALESCE(c.numero_contrato, '')) = %s"
            params.append(contract)
        equipment_clause = ""
        if equipment_number:
            equipment_clause = "AND TRIM(COALESCE(c.numero_equipamento, '')) = %s"
            params.append(equipment_number)
        scope_clause = ""
        if allowed_numbers is None:
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
        limit_clause = ""
        if limit:
            limit_clause = "LIMIT %s"
            params.append(int(limit))
        cursor.execute(
            f"""
            SELECT c.id, c.tipo_checklist, c.data_checklist, c.numero_contrato,
                   c.numero_equipamento, c.posicao, c.estado, c.tecnicos, c.criado_por_nome,
                   COALESCE(NULLIF(TRIM(e.tipo_equipamento), ''), 'Equipamento') AS equipment_name
            FROM checklists_manutencao c
            LEFT JOIN LATERAL (
                SELECT tipo_equipamento
                FROM registo_equipamentos
                WHERE TRIM(COALESCE(numero_equipamento, '')) = TRIM(COALESCE(c.numero_equipamento, ''))
                ORDER BY id DESC
                LIMIT 1
            ) e ON TRUE
            WHERE c.data_checklist >= %s::date
              AND c.data_checklist <= %s::date
              {contract_clause}
              {equipment_clause}
              AND LOWER(TRIM(COALESCE(c.estado, ''))) IN (
                'validado', 'serviço validado', 'servico validado',
                'serviço feito e validado', 'servico feito e validado'
              )
              {scope_clause}
            ORDER BY c.data_checklist DESC NULLS LAST, c.id DESC
            {limit_clause}
            """,
            params,
        )
        return cursor.fetchall(), None
    except Exception:
        app.logger.exception("Falha ao consultar checklists validadas")
        return [], "Não foi possível consultar as checklists neste momento."
    finally:
        if connection is not None:
            try:
                connection.rollback()
            except psycopg2.Error:
                pass
            finally:
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
    visible_equipment, equipment_total, contracts = dashboard_equipment_context()
    client_total = len({item["client_number"] for item in contracts})
    dashboard_profile = dict(client_profile)
    if len(contracts) == 1:
        dashboard_profile["company"] = contracts[0]["client_name"]
    elif len(contracts) > 1:
        dashboard_profile["company"] = "Área de cliente Electromatic"
    cards = [
        {"label": "Equipamentos associados", "value": equipment_total, "tone": "neutral"},
        {"label": "Contratos associados", "value": len(contracts), "tone": "success"},
        {"label": "Clientes associados", "value": client_total, "tone": "warning"},
        {"label": "Acesso válido até", "value": CONTRACT_VALID_UNTIL, "tone": "neutral"},
    ]
    return render_template(
        "dashboard.html",
        profile=dashboard_profile,
        cards=cards,
        contracts=contracts,
        equipment=visible_equipment,
        maintenance=recent_intervention_reports(contracts),
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
    equipment_options, history = external_maintenance_history(selected_equipment)
    error = history if isinstance(history, str) else None
    rows = [] if error else (history or [])
    selected_option = next(
        (item for item in equipment_options if item["number"] == selected_equipment),
        None,
    )
    return render_template(
        "maintenance.html",
        maintenance=rows,
        equipment_options=equipment_options,
        selected_option=selected_option,
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
    start_date = request.args.get("data_inicio", "").strip()[:10]
    end_date = request.args.get("data_fim", "").strip()[:10]
    selected_equipment = request.args.get("equipamento", "").strip()[:100]
    today = date.today()
    if not start_date:
        start_date = (today - timedelta(days=365)).isoformat()
    if not end_date:
        end_date = today.isoformat()
    try:
        rows, error = validated_checklists("", start_date, end_date, selected_equipment)
        maintenance_months = maintenance_by_month(rows)
    except Exception:
        app.logger.exception("Falha ao carregar o histórico de checklists")
        maintenance_months = []
        error = "Não foi possível apresentar as manutenções neste momento."
    pdf_jobs = pdf_jobs_for_user(current_user()["email"])
    return render_template(
        "checklists.html",
        maintenance_months=maintenance_months,
        start_date=start_date,
        end_date=end_date,
        selected_equipment=selected_equipment,
        error=error,
        searched=bool(start_date and end_date),
        pdf_jobs=pdf_jobs,
        pdf_jobs_pending=any(job["status"] in {"queued", "processing"} for job in pdf_jobs),
    )


@app.route("/checklists/pdf", methods=["GET"])
@login_required
def checklists_pdf():
    # Mantém a exportação directa para demonstração local, onde não existe fila.
    if DATABASE_URL:
        abort(405)
    start_date = request.args.get("data_inicio", "").strip()[:10]
    end_date = request.args.get("data_fim", "").strip()[:10]
    selected_equipment = request.args.get("equipamento", "").strip()[:100]
    today = date.today()
    if not start_date:
        start_date = (today - timedelta(days=365)).isoformat()
    if not end_date:
        end_date = today.isoformat()
    rows, error = validated_checklists(
        "", start_date, end_date, selected_equipment, limit=None
    )
    if error:
        abort(503)
    return checklist_pdf_response(rows, start_date, end_date, selected_equipment)


@app.route("/checklists/pdf", methods=["POST"])
@login_required
def enqueue_checklists_pdf():
    verify_csrf()
    start_date = request.form.get("data_inicio", "").strip()[:10]
    end_date = request.form.get("data_fim", "").strip()[:10]
    selected_equipment = request.form.get("equipamento", "").strip()[:100]
    today = date.today()
    if not start_date:
        start_date = (today - timedelta(days=365)).isoformat()
    if not end_date:
        end_date = today.isoformat()
    if start_date > end_date:
        flash("A data inicial tem de ser anterior à data final.", "error")
    else:
        job_id = enqueue_checklist_pdf_job(
            current_user()["email"],
            start_date,
            end_date,
            selected_equipment,
            current_pdf_access_scope(),
        )
        if job_id:
            flash("A exportação foi colocada em preparação.", "success")
        else:
            flash("Não foi possível preparar a exportação PDF.", "error")
    return redirect(url_for(
        "checklists",
        data_inicio=start_date,
        data_fim=end_date,
        equipamento=selected_equipment,
    ))


@app.route("/api/checklists/pdf/<job_id>")
@login_required
def checklist_pdf_status(job_id):
    job = pdf_job_for_user(job_id, current_user()["email"])
    if not job:
        abort(404)
    status = job["status"]
    return jsonify({
        "status": status,
        "status_label": PDF_JOB_LABELS.get(status, "Em preparação"),
        "progress": max(0, min(100, int(job["progress"] or 0))),
        "stage": job["stage"],
        "download_url": url_for("download_checklists_pdf", job_id=job_id) if status == "completed" else None,
    })


@app.route("/checklists/pdf/<job_id>")
@login_required
def download_checklists_pdf(job_id):
    job = pdf_job_for_user(job_id, current_user()["email"], include_file=True)
    if not job:
        abort(404)
    if job["status"] != "completed" or not job.get("pdf_data"):
        abort(409)
    payload = bytes(job["pdf_data"])
    return send_file(
        BytesIO(payload),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=job.get("file_name") or "manutencoes.pdf",
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
    user = current_user()
    clients = external_client_rows(current_allowed_client_numbers())
    contracts = relevant_contracts()
    profile = dict(client_profile)
    profile["name"] = user.get("name") or client_profile["name"]
    profile["email"] = user.get("email") or client_profile["email"]
    return render_template(
        "profile.html",
        profile=profile,
        user_role=user.get("role", "Utilizador"),
        clients=clients,
        contracts=contracts,
    )


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
