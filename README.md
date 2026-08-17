# Area do Cliente

Portal inicial em Flask para uma area reservada de cliente.

## Funcionalidades do MVP

- Acesso em duas fases, disponivel apenas enquanto o contrato estiver ativo.
- Lista de equipamentos abrangidos pelo contrato.
- Historico de manutencao corretiva e preventiva com checklists e relatorios.
- Download de PDFs individuais ou num unico ficheiro conjunto.
- Contacto com gestor de contrato e assistente de orientacao inicial.

## Arranque local

```bash
pip install -r requirements.txt
python app.py
```

Depois abre:

```text
http://127.0.0.1:5000
```

Em producao, define estas variaveis no Render:

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

Cole o resultado em `CLIENT_PASSWORD_HASH`. Nunca use `CLIENT_PASSWORD` nem publique uma palavra-passe no repositorio.

Para testes locais em HTTP, defina tambem `SESSION_COOKIE_SECURE=false`.

### Valores de teste do Render

O `render.yaml` inclui dados de exemplo para permitir o primeiro deploy. Usa estas credenciais apenas no ambiente de teste:

```text
Email: cliente.teste@exemplo.pt
```

No painel do Render, introduza `CLIENT_PASSWORD_HASH` com um hash novo gerado localmente. A palavra-passe nunca deve ser colocada no repositorio. Antes de producao, substitua tambem todos os restantes valores de teste do `render.yaml` pelos dados reais do responsavel pelo tratamento.

## Privacidade e RGPD

O portal usa apenas o cookie estritamente necessario para a sessao autenticada. Nao tem trackers, publicidade nem cookies de analise. Antes de publicar, preencha no Render a designacao legal, morada e email de privacidade do responsavel pelo tratamento. A politica em `/privacidade` deve ser revista e aprovada pelo responsavel juridico ou de protecao de dados da empresa.
