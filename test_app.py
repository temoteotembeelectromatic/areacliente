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

        documents = self.client.get("/documentos")
        self.assertIn("Descarregar PDF conjunto", documents.get_data(as_text=True))

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
