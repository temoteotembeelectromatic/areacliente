# Area do Cliente

Portal inicial em Flask para uma area reservada de cliente.

## Arranque local

```bash
pip install -r requirements.txt
python app.py
```

Depois abre:

```text
http://127.0.0.1:5000
```

## Credenciais de teste

```text
Email: cliente@smartic.pro
Password: cliente123
```

Em producao, define estas variaveis no Render:

```text
SECRET_KEY
CLIENT_EMAIL
CLIENT_PASSWORD
```

## Render

```text
Build Command: pip install -r requirements.txt
Start Command: gunicorn app:app
```
