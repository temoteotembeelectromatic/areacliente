import os
import re
import unittest

from werkzeug.security import generate_password_hash


os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CLIENT_EMAIL"] = "cliente@smartic.pro"
os.environ["CLIENT_PASSWORD_HASH"] = generate_password_hash("senha-de-teste")
os.environ["SESSION_COOKIE_SECURE"] = "false"
os.environ["CONTROLLER_LEGAL_NAME"] = "Smartic Pro, Lda."
os.environ["CONTROLLER_ADDRESS"] = "Rua Exemplo, 1000-000 Lisboa"
os.environ["PRIVACY_EMAIL"] = "privacidade@smartic.pro"

import app as portal  # noqa: E402


def token_from(response):
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True))
    if not match:
        raise AssertionError("Token CSRF nao encontrado.")
    return match.group(1)


class LoginSecurityTests(unittest.TestCase):
    def setUp(self):
        portal.app.config.update(TESTING=True)
        self.client = portal.app.test_client()

    def test_login_requires_two_steps_and_uses_secure_headers(self):
        first_page = self.client.get("/login")
        self.assertEqual(first_page.status_code, 200)
        self.assertIn("Passo 1 de 2", first_page.get_data(as_text=True))
        self.assertIn("electromatic-logo.png", first_page.get_data(as_text=True))
        self.assertEqual(first_page.headers["X-Frame-Options"], "DENY")
        self.assertIn("default-src 'self'", first_page.headers["Content-Security-Policy"])

        email_step = self.client.post(
            "/login",
            data={"email": "cliente@smartic.pro", "csrf_token": token_from(first_page)},
        )
        self.assertEqual(email_step.status_code, 302)
        self.assertIn("/login/password", email_step.headers["Location"])

        with self.client.session_transaction() as browser_session:
            self.assertNotIn("login_email", browser_session)
            self.assertNotIn("cliente@smartic.pro", browser_session.values())

        password_page = self.client.get("/login/password")
        self.assertIn("Passo 2 de 2", password_page.get_data(as_text=True))
        self.assertNotIn("cliente@smartic.pro", password_page.get_data(as_text=True))

        login = self.client.post(
            "/login/password",
            data={"password": "senha-de-teste", "csrf_token": token_from(password_page)},
        )
        self.assertEqual(login.status_code, 302)
        self.assertIn("/dashboard", login.headers["Location"])

        dashboard = self.client.get("/dashboard")
        self.assertEqual(dashboard.headers["Cache-Control"], "no-store, max-age=0")
        dashboard_html = dashboard.get_data(as_text=True)
        self.assertIn("Equipamentos associados", dashboard_html)
        self.assertIn("Contratos associados", dashboard_html)
        self.assertIn("Últimos relatórios de intervenção", dashboard_html)
        self.assertIn("CTR-2026-001", dashboard_html)

        empty_equipment = self.client.get("/equipamentos")
        self.assertEqual(empty_equipment.status_code, 200)
        self.assertNotIn('class="equipment-card"', empty_equipment.get_data(as_text=True))

        documents = self.client.get("/documentos")
        self.assertIn("Documentos por contrato", documents.get_data(as_text=True))
        self.assertIn("CTR-2026-001", documents.get_data(as_text=True))

        contract_documents = self.client.get("/documentos/contrato/CTR-2026-001")
        self.assertEqual(contract_documents.status_code, 200)
        self.assertIn("Relatório preventivo QGBT", contract_documents.get_data(as_text=True))

        support = self.client.get("/apoio")
        self.assertEqual(support.status_code, 200)
        self.assertIn("Serviço de Piquete", support.get_data(as_text=True))
        self.assertIn("914 130 921", support.get_data(as_text=True))

        maintenance_page = self.client.get("/manutencao")
        self.assertEqual(maintenance_page.status_code, 200)
        self.assertIn("Seleccione um equipamento", maintenance_page.get_data(as_text=True))

        checklist_page = self.client.get("/checklists")
        checklist_html = checklist_page.get_data(as_text=True)
        self.assertEqual(checklist_page.status_code, 200)
        self.assertIn("Manutenções e inspeções", checklist_html)
        self.assertIn("Data inicial", checklist_html)

        users_page = self.client.get("/utilizadores")
        users_html = users_page.get_data(as_text=True)
        self.assertEqual(users_page.status_code, 200)
        self.assertIn("Gestão de utilizadores", users_html)
        self.assertIn("Acessos autorizados", users_html)
        self.assertIn("Clientes associados", users_html)
        self.assertIn("TESTE-001", users_html)
        self.assertIn("Apenas leitura", users_html)

        client_suggestions = self.client.get("/api/clientes?q=TESTE")
        self.assertEqual(client_suggestions.status_code, 200)
        self.assertIn("TESTE-001", client_suggestions.get_data(as_text=True))

        maintenance_history = self.client.get("/manutencao?equipamento=EQ-003")
        self.assertEqual(maintenance_history.status_code, 200)
        self.assertIn("Ocorr", maintenance_history.get_data(as_text=True))

        intervention_detail = self.client.get("/manutencao/MC-2026-007")
        self.assertEqual(intervention_detail.status_code, 200)
        self.assertIn("Detalhe da intervenção", intervention_detail.get_data(as_text=True))

        equipment_page = self.client.get("/equipamentos?q=EQ-003")
        equipment_html = equipment_page.get_data(as_text=True)
        self.assertEqual(equipment_page.status_code, 200)
        self.assertIn("Apenas leitura", equipment_html)
        self.assertIn("app.js", equipment_html)
        self.assertIn("Sistema UPS", equipment_html)
        self.assertIn("Equipamento #EQ-003", equipment_html)
        self.assertIn("Empresa Cliente - Contrato de Manutenção", equipment_html)
        self.assertNotIn("Todos os clientes autorizados", equipment_html)
        self.assertEqual(equipment_html.count('class="equipment-card"'), 1)

        equipment_number_page = self.client.get("/equipamentos?q=EQ-003")
        self.assertEqual(equipment_number_page.status_code, 200)
        self.assertEqual(equipment_number_page.get_data(as_text=True).count('class="equipment-card"'), 1)

        contract_page = self.client.get("/equipamentos?q=CTR-2026-001")
        self.assertEqual(contract_page.status_code, 200)
        self.assertEqual(contract_page.get_data(as_text=True).count('class="equipment-card"'), 4)

        write_attempt = self.client.post("/equipamentos", data={"name": "Alterar"})
        self.assertEqual(write_attempt.status_code, 405)

        bundle = self.client.get("/documentos/bundle.pdf")
        self.assertEqual(bundle.status_code, 200)
        self.assertEqual(bundle.mimetype, "application/pdf")
        self.assertTrue(bundle.data.startswith(b"%PDF"))

    def test_privacy_information_is_available_without_login(self):
        response = self.client.get("/privacidade")
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Smartic Pro, Lda.", page)
        self.assertIn("privacidade@smartic.pro", page)

    def test_maintenance_months_use_a_template_safe_records_key(self):
        months = portal.maintenance_by_month(
            [{"data_checklist": "2026-08-20", "tipo_checklist": "Manutenção preventiva"}]
        )
        self.assertEqual(len(months[0]["records"]), 1)
        self.assertNotIn("items", months[0])

    def test_post_without_csrf_token_is_rejected(self):
        response = self.client.post("/login", data={"email": "cliente@smartic.pro"})
        self.assertEqual(response.status_code, 400)

    def test_expired_contract_blocks_the_dashboard(self):
        original_expiry = portal.contract_valid_until
        portal.contract_valid_until = portal.date(2020, 1, 1)
        try:
            with self.client.session_transaction() as browser_session:
                browser_session["logged_in"] = True
            response = self.client.get("/dashboard")
            self.assertEqual(response.status_code, 302)
            self.assertIn("/login", response.headers["Location"])
        finally:
            portal.contract_valid_until = original_expiry


if __name__ == "__main__":
    unittest.main()
