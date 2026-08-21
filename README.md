# Área do Cliente

Portal inicial em Flask para uma área reservada de cliente.

## Funcionalidades do MVP

- Acesso em duas fases, disponível apenas enquanto o contrato estiver ativo.
- Lista de equipamentos abrangidos pelo contrato.
- Histórico de manutenção corretiva e preventiva com checklists e relatórios.
- Exportação de relatórios PDF por tarefa em segundo plano, com progresso visível.
- Contacto com gestor de contrato e assistente de orientação inicial.
- Gestão de utilizadores com associação explícita a números de cliente.

## Base de dados externa de equipamentos

A página `/equipamentos` lê a tabela `registo_equipamentos` através de `DATABASE_URL_2`. A ligação abre com transações somente de leitura, timeout de cinco segundos e limite de 500 resultados. A rota aceita apenas pedidos `GET`.

No Render, configure:

```text
DATABASE_URL_2=postgresql://...
EQUIPMENT_TEST_MODE=true
```

Em modo de teste, todos os números de cliente existentes ficam disponíveis no seletor. Antes de produção, defina `EQUIPMENT_TEST_MODE=false` e configure `CLIENT_USER_ACCOUNTS_JSON` para limitar cada utilizador aos clientes autorizados. `CLIENT_ALLOWED_NUMBERS` continua disponível para a conta única legada. Use na base externa uma credencial PostgreSQL que tenha apenas permissão `SELECT`.

No serviço Render já existente, estas variáveis têm de ser adicionadas manualmente em `Environment`; o `render.yaml` não altera automaticamente as variáveis de um serviço criado anteriormente.

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
CLIENT_USER_ACCOUNTS_JSON
DATABASE_URL
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

### Worker para PDFs

As exportações de relatórios são colocadas numa fila na `DATABASE_URL` e processadas pelo serviço Render `area-cliente-pdf-worker`. A página mostra o progresso enquanto o ficheiro é preparado e disponibiliza a descarga quando termina. Os PDFs ficam guardados temporariamente durante 48 horas e são depois eliminados pelo worker.

No worker, configure as mesmas variáveis `SECRET_KEY`, `CLIENT_EMAIL`, `CLIENT_PASSWORD_HASH`, `CLIENT_USER_ACCOUNTS_JSON`, `DATABASE_URL`, `DATABASE_URL_2` e `EQUIPMENT_TEST_MODE` usadas no serviço web. `DATABASE_URL` é a única base onde a fila e os ficheiros temporários são gravados; `DATABASE_URL_2` continua a ser usada apenas em transações de leitura para consultar os relatórios.

Pode ajustar o limite temporário de cada ficheiro com `PDF_MAX_JOB_BYTES` (por omissão, 50 MB). Para serviços Render já existentes, crie o worker no painel ou sincronize o Blueprint para aplicar a definição do `render.yaml`.

Para associar vários utilizadores aos respectivos clientes, configure `CLIENT_USER_ACCOUNTS_JSON` no Render. Cada conta deve conter um hash seguro e pelo menos um número de cliente:

```json
[{"email":"utilizador@cliente.pt","password_hash":"HASH_GERADO_COM_WERKZEUG","name":"Nome do utilizador","role":"Utilizador","client_numbers":["6917","7024"]}]
```

O utilizador autenticado só consulta equipamentos, intervenções e checklists cujo `numero_cliente` esteja na sua lista. A página `/utilizadores` permite ao administrador criar acessos e associar clientes, gravando apenas na `DATABASE_URL`. A `DATABASE_URL_2` mantém-se exclusivamente de leitura.

Para testes locais em HTTP, defina tambem `SESSION_COOKIE_SECURE=false`.

### Valores de teste do Render

O `render.yaml` inclui dados de exemplo para permitir o primeiro deploy. Usa estas credenciais apenas no ambiente de teste:

```text
Email: cliente.teste@exemplo.pt
```

No painel do Render, introduza `CLIENT_PASSWORD_HASH` com um hash novo gerado localmente. A palavra-passe nunca deve ser colocada no repositório. Antes de produção, substitua também todos os restantes valores de teste do `render.yaml` pelos dados reais do responsável pelo tratamento.

## Privacidade e RGPD

O portal usa apenas o cookie estritamente necessário para a sessão autenticada. Não tem trackers, publicidade nem cookies de análise. Antes de publicar, preencha no Render a designação legal, morada e e-mail de privacidade do responsável pelo tratamento. A política em `/privacidade` deve ser revista e aprovada pelo responsável jurídico ou de proteção de dados da empresa.
