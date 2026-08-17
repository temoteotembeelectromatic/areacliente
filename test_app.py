import os
import re
import unittest

from werkzeug.security import generate_password_hash


os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CLIENT_EMAIL"] = "cliente@smartic.pro"
os.environ["CLIENT_PASSWORD_HASH"] = generate_password_hash("senha-de-teste")
os.environ["SESSION_COOKIE_SECURE"] = "false"

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
        self.assertEqual(first_page.headers["X-Frame-Options"], "DENY")
        self.assertIn("default-src 'self'", first_page.headers["Content-Security-Policy"])

        email_step = self.client.post(
            "/login",
            data={"email": "cliente@smartic.pro", "csrf_token": token_from(first_page)},
        )
        self.assertEqual(email_step.status_code, 302)
        self.assertIn("/login/password", email_step.headers["Location"])

        password_page = self.client.get("/login/password")
        self.assertIn("Passo 2 de 2", password_page.get_data(as_text=True))

        login = self.client.post(
            "/login/password",
            data={"password": "senha-de-teste", "csrf_token": token_from(password_page)},
        )
        self.assertEqual(login.status_code, 302)
        self.assertIn("/dashboard", login.headers["Location"])

    def test_post_without_csrf_token_is_rejected(self):
        response = self.client.post("/login", data={"email": "cliente@smartic.pro"})
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
