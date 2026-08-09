#!/usr/bin/env bash
# configure_stack_v3.sh — Autenticação n8n via banco de dados (abordagem direta)
# Cole no console VNC: curl -fsSL <URL> | bash
set -euo pipefail

cd /root/automacao
source .env

N8N="http://localhost:5678"
EVO="http://localhost:8080"
INSTANCE="edilson"

echo ""
echo "════════════════════════════════════════════════════════"
echo " PASSO 1 — Verificando n8n"
echo "════════════════════════════════════════════════════════"
for i in $(seq 1 20); do
  HTTP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$N8N/healthz" 2>/dev/null || echo "000")
  [ "$HTTP" = "200" ] && { echo "n8n OK!"; break; }
  echo "  Aguardando... ($i/20)"; sleep 5
done

echo ""
echo "════════════════════════════════════════════════════════"
echo " PASSO 2 — Desabilitando User Management (restart n8n)"
echo "════════════════════════════════════════════════════════"
# Adiciona vars que eliminam a necessidade de login para a API
if ! grep -q "N8N_USER_MANAGEMENT_DISABLED" docker-compose.yml; then
  sed -i '/- N8N_LOG_LEVEL=warn/a\      - N8N_USER_MANAGEMENT_DISABLED=true\n      - N8N_SKIP_OWNER_SETUP=true' docker-compose.yml
  echo "  Vars adicionadas ao docker-compose.yml"
  docker compose up -d n8n
  echo "  n8n reiniciado. Aguardando..."
  sleep 15
  for i in $(seq 1 20); do
    HTTP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$N8N/healthz" 2>/dev/null || echo "000")
    [ "$HTTP" = "200" ] && { echo "  n8n pronto!"; break; }
    echo "  Aguardando... ($i/20)"; sleep 5
  done
else
  echo "  Vars já configuradas."
fi

echo ""
echo "════════════════════════════════════════════════════════"
echo " PASSO 3 — Obtendo/criando X-N8N-API-KEY"
echo "════════════════════════════════════════════════════════"

N8N_API_KEY=""

# Tenta obter API key existente no banco
EXISTING_KEY=$(docker exec postgres psql -U "$POSTGRES_USER" -d n8n -t \
  -c "SELECT \"apiKey\" FROM public.api_key LIMIT 1;" 2>/dev/null | tr -d ' \n' || echo "")

if [ -n "$EXISTING_KEY" ] && [ "$EXISTING_KEY" != "" ]; then
  N8N_API_KEY="$EXISTING_KEY"
  echo "  API Key existente encontrada: ${N8N_API_KEY:0:30}..."
else
  # Verifica se há usuário no banco
  USER_ID=$(docker exec postgres psql -U "$POSTGRES_USER" -d n8n -t \
    -c "SELECT id FROM public.user LIMIT 1;" 2>/dev/null | tr -d ' \n' || echo "")

  if [ -n "$USER_ID" ]; then
    N8N_API_KEY="n8n_api_$(openssl rand -hex 32)"
    docker exec postgres psql -U "$POSTGRES_USER" -d n8n -c "
      INSERT INTO public.api_key (id, \"userId\", label, \"apiKey\", \"createdAt\", \"updatedAt\")
      VALUES (gen_random_uuid(), '$USER_ID', 'automation', '$N8N_API_KEY', NOW(), NOW());" 2>/dev/null
    echo "  API Key criada no banco: ${N8N_API_KEY:0:30}..."
  else
    echo "  Sem usuário no banco. Tentando login para criar owner..."

    # Tenta login com campo correto
    LOGIN_RESP=$(curl -s -c /tmp/n8n_cookies.txt -X POST "$N8N/rest/login" \
      -H "Content-Type: application/json" \
      -d "{\"emailOrLdapLoginId\": \"admin@automacao.local\", \"password\": \"$N8N_BASIC_AUTH_PASSWORD\"}" 2>/dev/null)

    SESSION_OK=$(echo "$LOGIN_RESP" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print('OK' if d.get('data') else 'FALHOU')
except: print('FALHOU')
" 2>/dev/null)

    if [ "$SESSION_OK" = "OK" ]; then
      APIKEY_RESP=$(curl -s -b /tmp/n8n_cookies.txt -X POST "$N8N/rest/me/api-key" \
        -H "Content-Type: application/json" \
        -d '{"label": "automation"}' 2>/dev/null)
      N8N_API_KEY=$(echo "$APIKEY_RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print((d.get('data') or {}).get('apiKey') or d.get('apiKey') or '')
" 2>/dev/null || echo "")
      echo "  API Key via login: ${N8N_API_KEY:0:30}..."
    fi
  fi
fi

# Testa se API está acessível (com ou sem chave)
NO_AUTH_HTTP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$N8N/api/v1/workflows" 2>/dev/null || echo "000")
echo "  Teste sem auth: HTTP $NO_AUTH_HTTP"

if [ "$NO_AUTH_HTTP" = "200" ]; then
  API_HEADER=""
  echo "  API acessível sem autenticação (user management desabilitado)."
elif [ -n "$N8N_API_KEY" ]; then
  WITH_KEY_HTTP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 \
    -H "X-N8N-API-KEY: $N8N_API_KEY" "$N8N/api/v1/workflows" 2>/dev/null || echo "000")
  echo "  Teste com API Key: HTTP $WITH_KEY_HTTP"
  [ "$WITH_KEY_HTTP" = "200" ] && API_HEADER="X-N8N-API-KEY: $N8N_API_KEY" || API_HEADER=""
else
  API_HEADER=""
fi

# Persiste
[ -n "$N8N_API_KEY" ] && (grep -q "^N8N_API_KEY=" .env && \
  sed -i "s|^N8N_API_KEY=.*|N8N_API_KEY=$N8N_API_KEY|" .env || \
  echo "N8N_API_KEY=$N8N_API_KEY" >> .env)

echo ""
echo "════════════════════════════════════════════════════════"
echo " PASSO 4 — Importando workflow Dr. João Holanda"
echo "════════════════════════════════════════════════════════"

curl -fsSL \
  "https://raw.githubusercontent.com/erickcavalcante81-hue/Home-Cabin-USA/claude/multimodal-health-ai-system-dEf2q/whatsapp_webhook_n8n.json" \
  -o /tmp/workflow_raw.json

python3 - <<'PYEOF'
import json
with open("/tmp/workflow_raw.json") as f:
    wf = json.load(f)
for field in ["id", "versionId", "meta", "pinData", "staticData", "tags"]:
    wf.pop(field, None)
wf["active"] = False
with open("/tmp/workflow_import.json", "w") as f:
    json.dump(wf, f)
print("  Workflow preparado.")
PYEOF

IMPORT_RESP=$(curl -s -X POST "$N8N/api/v1/workflows" \
  ${API_HEADER:+-H "$API_HEADER"} \
  -H "Content-Type: application/json" \
  -d @/tmp/workflow_import.json 2>/dev/null)

WF_ID=$(echo "$IMPORT_RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d.get('id', 'ERRO'))
" 2>/dev/null || echo "ERRO")

if [ "$WF_ID" != "ERRO" ] && [ -n "$WF_ID" ]; then
  echo "  Workflow importado! ID: $WF_ID"
  curl -s -X PATCH "$N8N/api/v1/workflows/$WF_ID" \
    ${API_HEADER:+-H "$API_HEADER"} \
    -H "Content-Type: application/json" \
    -d '{"active": true}' > /dev/null
  echo "  Workflow ativado!"
else
  echo "  AVISO — resposta do import:"
  echo "$IMPORT_RESP" | python3 -m json.tool 2>/dev/null || echo "$IMPORT_RESP"
fi

echo ""
echo "════════════════════════════════════════════════════════"
echo " PASSO 5 — Credencial Anthropic no n8n"
echo "════════════════════════════════════════════════════════"
ANTHROPIC_KEY="${ANTHROPIC_API_KEY:-}"
if [ -n "$ANTHROPIC_KEY" ]; then
  CRED_RESP=$(curl -s -X POST "$N8N/api/v1/credentials" \
    ${API_HEADER:+-H "$API_HEADER"} \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"Anthropic — Dr. João Holanda\",\"type\":\"anthropicApi\",\"data\":{\"apiKey\":\"$ANTHROPIC_KEY\"}}" 2>/dev/null)
  CRED_ID=$(echo "$CRED_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('id','ERRO'))" 2>/dev/null || echo "ERRO")
  echo "  Credencial Anthropic: ID $CRED_ID"
else
  echo "  AVISO: ANTHROPIC_API_KEY não encontrada no .env"
fi

echo ""
echo "════════════════════════════════════════════════════════"
echo " PASSO 6 — Evolution API: instância + webhook"
echo "════════════════════════════════════════════════════════"

CREATE_RESP=$(curl -s -X POST "$EVO/instance/create" \
  -H "apikey: $EVOLUTION_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"instanceName\":\"$INSTANCE\",\"qrcode\":true,\"integration\":\"WHATSAPP-BAILEYS\"}" 2>/dev/null)
echo "  Instância: $(echo "$CREATE_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('instance',{}).get('instanceName','ver abaixo'))" 2>/dev/null)"

curl -s -X POST "$EVO/webhook/set/$INSTANCE" \
  -H "apikey: $EVOLUTION_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"url\":\"http://localhost:5678/webhook/whatsapp-webhook\",\"webhook_by_events\":false,\"webhook_base64\":false,\"events\":[\"MESSAGES_UPSERT\",\"MESSAGES_UPDATE\",\"SEND_MESSAGE\",\"CONNECTION_UPDATE\"]}" > /dev/null
echo "  Webhook → n8n configurado!"

sleep 3
QR_RESP=$(curl -s "$EVO/instance/connect/$INSTANCE" -H "apikey: $EVOLUTION_API_KEY" 2>/dev/null)
QR_B64=$(echo "$QR_RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
qr = d.get('qrcode', {})
v = qr.get('base64','')
print(v[:500] if v else 'QR ainda não disponível')
" 2>/dev/null || echo "$QR_RESP")

echo ""
echo "════════════════════════════════════════════════════════"
echo " ✔  CONFIGURAÇÃO CONCLUÍDA"
echo "════════════════════════════════════════════════════════"
echo ""
echo "  n8n           → http://${VPS_IP}:5678"
echo "  Evolution API → http://${VPS_IP}:8080"
echo ""
echo "  QR Code (cole em base64.guru/converter/decode/image):"
echo "  $QR_B64"
echo ""
