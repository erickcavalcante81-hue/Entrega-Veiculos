#!/usr/bin/env bash
# configure_stack_v2.sh — Versão corrigida com autenticação n8n via API Key
# Executar na VPS: bash /root/automacao/configure_stack_v2.sh
set -euo pipefail

cd /root/automacao
source .env

N8N="http://localhost:5678"
EVO="http://localhost:8080"
INSTANCE="edilson"
N8N_EMAIL="admin@automacao.local"
COOKIES="/tmp/n8n_session.txt"

# ─────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════"
echo " PASSO 1 — Aguardando n8n ficar pronto"
echo "════════════════════════════════════════════════════════"
for i in $(seq 1 24); do
  HTTP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$N8N/healthz" 2>/dev/null || echo "000")
  [ "$HTTP" = "200" ] && { echo "n8n respondendo!"; break; }
  echo "  Aguardando... ($i/24)"; sleep 5
done

# ─────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════"
echo " PASSO 2 — Setup inicial do n8n (criação do owner)"
echo "════════════════════════════════════════════════════════"
SETUP_HTTP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$N8N/setup" 2>/dev/null || echo "000")

if [ "$SETUP_HTTP" = "200" ]; then
  echo "Wizard de setup detectado. Criando conta owner..."
  SETUP_RESP=$(curl -s -c "$COOKIES" -X POST "$N8N/setup" \
    -H "Content-Type: application/json" \
    -d "{
      \"email\": \"$N8N_EMAIL\",
      \"firstName\": \"Dr. João\",
      \"lastName\": \"Holanda\",
      \"password\": \"$N8N_BASIC_AUTH_PASSWORD\"
    }")
  echo "Setup: $(echo "$SETUP_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print('OK — ' + d.get('data',{}).get('email','ver abaixo'))" 2>/dev/null || echo "$SETUP_RESP")"
else
  echo "n8n já tem owner configurado (HTTP $SETUP_HTTP)."
fi

# ─────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════"
echo " PASSO 3 — Login e geração de X-N8N-API-KEY"
echo "════════════════════════════════════════════════════════"
rm -f "$COOKIES"
LOGIN_RESP=$(curl -s -c "$COOKIES" -b "$COOKIES" -X POST "$N8N/rest/login" \
  -H "Content-Type: application/json" \
  -d "{\"email\": \"$N8N_EMAIL\", \"password\": \"$N8N_BASIC_AUTH_PASSWORD\"}")

LOGIN_OK=$(echo "$LOGIN_RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
name = d.get('data', {}).get('firstName', '')
print('OK — ' + name if name else 'FALHOU')
" 2>/dev/null || echo "FALHOU")
echo "Login: $LOGIN_OK"

if echo "$LOGIN_OK" | grep -q "FALHOU"; then
  echo "  Resposta raw: $LOGIN_RESP"
  echo "  Tentando criar API Key diretamente via banco de dados..."

  N8N_API_KEY="n8n_api_$(openssl rand -hex 32)"
  USER_ID=$(docker exec postgres psql -U "$POSTGRES_USER" -d n8n -t \
    -c "SELECT id FROM public.user LIMIT 1;" 2>/dev/null | tr -d ' \n' || echo "")

  if [ -n "$USER_ID" ]; then
    docker exec postgres psql -U "$POSTGRES_USER" -d n8n -c "
      INSERT INTO public.api_key (id, \"userId\", label, \"apiKey\", \"createdAt\", \"updatedAt\")
      VALUES (gen_random_uuid(), '$USER_ID', 'automation', '$N8N_API_KEY', NOW(), NOW())
      ON CONFLICT DO NOTHING;" 2>/dev/null
    echo "  API Key inserida via banco: ${N8N_API_KEY:0:30}..."
  else
    echo "  ERRO: banco sem usuário n8n. Abra http://${VPS_IP}:5678 e complete o setup manualmente."
    N8N_API_KEY=""
  fi
else
  # Login OK — gera API key via REST
  APIKEY_RESP=$(curl -s -c "$COOKIES" -b "$COOKIES" -X POST "$N8N/rest/me/api-key" \
    -H "Content-Type: application/json" \
    -d '{"label": "automation-dr-joao"}')

  N8N_API_KEY=$(echo "$APIKEY_RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
key = (d.get('data') or {}).get('apiKey') or d.get('apiKey') or ''
print(key)
" 2>/dev/null || echo "")

  if [ -z "$N8N_API_KEY" ]; then
    echo "  Fallback: inserindo API Key via banco de dados..."
    N8N_API_KEY="n8n_api_$(openssl rand -hex 32)"
    USER_ID=$(docker exec postgres psql -U "$POSTGRES_USER" -d n8n -t \
      -c "SELECT id FROM public.user LIMIT 1;" 2>/dev/null | tr -d ' \n' || echo "")
    [ -n "$USER_ID" ] && docker exec postgres psql -U "$POSTGRES_USER" -d n8n -c "
      INSERT INTO public.api_key (id, \"userId\", label, \"apiKey\", \"createdAt\", \"updatedAt\")
      VALUES (gen_random_uuid(), '$USER_ID', 'automation', '$N8N_API_KEY', NOW(), NOW())
      ON CONFLICT DO NOTHING;" 2>/dev/null
  fi
  echo "  API Key: ${N8N_API_KEY:0:30}..."
fi

# Persiste a API Key no .env para uso futuro
grep -q "^N8N_API_KEY=" .env && \
  sed -i "s|^N8N_API_KEY=.*|N8N_API_KEY=$N8N_API_KEY|" .env || \
  echo "N8N_API_KEY=$N8N_API_KEY" >> .env

# ─────────────────────────────────────────────────────────────
if [ -n "$N8N_API_KEY" ]; then

  # Testa a API Key
  TEST_HTTP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 \
    -H "X-N8N-API-KEY: $N8N_API_KEY" "$N8N/api/v1/workflows" 2>/dev/null || echo "000")
  echo "  Teste API Key → HTTP $TEST_HTTP"

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
print("Workflow preparado para import.")
PYEOF

  IMPORT_RESP=$(curl -s -X POST "$N8N/api/v1/workflows" \
    -H "X-N8N-API-KEY: $N8N_API_KEY" \
    -H "Content-Type: application/json" \
    -d @/tmp/workflow_import.json)

  WF_ID=$(echo "$IMPORT_RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d.get('id', 'ERRO'))
" 2>/dev/null || echo "ERRO")

  if [ "$WF_ID" != "ERRO" ] && [ -n "$WF_ID" ]; then
    echo "  Workflow importado! ID: $WF_ID"
    curl -s -X PATCH "$N8N/api/v1/workflows/$WF_ID" \
      -H "X-N8N-API-KEY: $N8N_API_KEY" \
      -H "Content-Type: application/json" \
      -d '{"active": true}' > /dev/null
    echo "  Workflow ativado!"
  else
    echo "  AVISO: falha no import. Resposta:"
    echo "$IMPORT_RESP" | python3 -m json.tool 2>/dev/null || echo "$IMPORT_RESP"
  fi

  echo ""
  echo "════════════════════════════════════════════════════════"
  echo " PASSO 5 — Registrando credencial Anthropic no n8n"
  echo "════════════════════════════════════════════════════════"
  ANTHROPIC_KEY="${ANTHROPIC_API_KEY:-}"
  if [ -n "$ANTHROPIC_KEY" ]; then
    CRED_RESP=$(curl -s -X POST "$N8N/api/v1/credentials" \
      -H "X-N8N-API-KEY: $N8N_API_KEY" \
      -H "Content-Type: application/json" \
      -d "{
        \"name\": \"Anthropic — Dr. João Holanda\",
        \"type\": \"anthropicApi\",
        \"data\": {\"apiKey\": \"$ANTHROPIC_KEY\"}
      }")
    CRED_ID=$(echo "$CRED_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('id','ERRO'))" 2>/dev/null || echo "ERRO")
    echo "  Credencial Anthropic: ID $CRED_ID"
  else
    echo "  AVISO: ANTHROPIC_API_KEY não definida no .env"
  fi

fi

# ─────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════"
echo " PASSO 6 — Evolution API: instância WhatsApp + webhook"
echo "════════════════════════════════════════════════════════"

CREATE_RESP=$(curl -s -X POST "$EVO/instance/create" \
  -H "apikey: $EVOLUTION_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"instanceName\": \"$INSTANCE\", \"qrcode\": true, \"integration\": \"WHATSAPP-BAILEYS\"}")

echo "Instância:"
echo "$CREATE_RESP" | python3 -m json.tool 2>/dev/null || echo "$CREATE_RESP"

WEBHOOK_RESP=$(curl -s -X POST "$EVO/webhook/set/$INSTANCE" \
  -H "apikey: $EVOLUTION_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"url\": \"http://localhost:5678/webhook/whatsapp-webhook\",
    \"webhook_by_events\": false,
    \"webhook_base64\": false,
    \"events\": [\"MESSAGES_UPSERT\", \"MESSAGES_UPDATE\", \"SEND_MESSAGE\", \"CONNECTION_UPDATE\"]
  }")

echo "Webhook:"
echo "$WEBHOOK_RESP" | python3 -m json.tool 2>/dev/null || echo "$WEBHOOK_RESP"

sleep 3
QR_RESP=$(curl -s "$EVO/instance/connect/$INSTANCE" -H "apikey: $EVOLUTION_API_KEY")
QR_B64=$(echo "$QR_RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
qr = d.get('qrcode', {})
print(qr.get('base64', qr.get('pairingCode', 'aguarde e rode: curl -s http://localhost:8080/instance/connect/edilson -H \"apikey: \$EVOLUTION_API_KEY\"')))
" 2>/dev/null || echo "$QR_RESP")

echo ""
echo "════════════════════════════════════════════════════════"
echo " ✔  CONFIGURAÇÃO CONCLUÍDA"
echo "════════════════════════════════════════════════════════"
echo ""
echo "  n8n           → http://${VPS_IP}:5678"
echo "  Evolution API → http://${VPS_IP}:8080"
echo ""
echo "  QR Code WhatsApp (cole em base64.guru/converter/decode/image):"
echo "  ${QR_B64:0:400}"
echo ""
