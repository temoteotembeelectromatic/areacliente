# Área do Cliente

Portal inicial em Flask para uma área reservada de cliente.

## Funcionalidades do MVP

- Acesso em duas fases, disponível apenas enquanto o contrato estiver ativo.
- Lista de equipamentos abrangidos pelo contrato.
- Histórico de manutenção corretiva e preventiva com checklists e relatórios.
- Download de PDFs individuais ou num único ficheiro conjunto.
- Contacto com gestor de contrato e assistente de orientação inicial.

## Arranque local

```bash
pip install -r requirements.txt
python app.py
```

Depois abre:

```text
http://127.0.0.1:5000
```

Em produção, define estas variáveis no Render:

```text
SECRET_KEY
CLIENT_EMAIL
CLIENT_PASSWORD_HASH
CONTROLLER_LEGAL_NAME
CONTROLLER_ADDRESS
PRIVACY_EMAIL
```

## Render

```text
Build Command: pip install -r requirements.txt
Start Command: gunicorn app:app
```

Gere o hash da palavra-passe antes de configurar o Render:

```powershell
python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('A_SUA_PALAVRA_PASSE'))"
```

Cole o resultado em `CLIENT_PASSWORD_HASH`. Nunca use `CLIENT_PASSWORD` nem publique uma palavra-passe no repositório.

Para testes locais em HTTP, defina tambem `SESSION_COOKIE_SECURE=false`.

### Valores de teste do Render

O `render.yaml` inclui dados de exemplo para permitir o primeiro deploy. Usa estas credenciais apenas no ambiente de teste:

```text
Email: cliente.teste@exemplo.pt
```

No painel do Render, introduza `CLIENT_PASSWORD_HASH` com um hash novo gerado localmente. A palavra-passe nunca deve ser colocada no repositório. Antes de produção, substitua também todos os restantes valores de teste do `render.yaml` pelos dados reais do responsável pelo tratamento.

## Privacidade e RGPD

O portal usa apenas o cookie estritamente necessário para a sessão autenticada. Não tem trackers, publicidade nem cookies de análise. Antes de publicar, preencha no Render a designação legal, morada e e-mail de privacidade do responsável pelo tratamento. A política em `/privacidade` deve ser revista e aprovada pelo responsável jurídico ou de proteção de dados da empresa.
