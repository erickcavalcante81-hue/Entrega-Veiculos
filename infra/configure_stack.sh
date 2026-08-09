#!/usr/bin/env bash
# configure_stack.sh — Configura n8n + Evolution API após o deploy
# Execute diretamente na VPS: bash /root/automacao/configure_stack.sh
set -euo pipefail

cd /root/automacao
source .env

N8N="http://localhost:5678"
EVO="http://localhost:8080"
AUTH=$(echo -n "admin:${N8N_BASIC_AUTH_PASSWORD}" | base64)
INSTANCE="edilson"

echo ""
echo "════════════════════════════════════════════════════════"
echo " PASSO 1 — Aguardando n8n ficar pronto"
echo "════════════════════════════════════════════════════════"
for i in $(seq 1 24); do
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$N8N/healthz" || echo "000")
  if [ "$STATUS" = "200" ]; then
    echo "n8n respondendo!"
    break
  fi
  echo "Aguardando n8n... ($i/24)"
  sleep 5
done

echo ""
echo "════════════════════════════════════════════════════════"
echo " PASSO 2 — Importando workflow Dr. João Holanda no n8n"
echo "════════════════════════════════════════════════════════"

# Download do workflow do GitHub
curl -fsSL \
  "https://raw.githubusercontent.com/erickcavalcante81-hue/Home-Cabin-USA/claude/multimodal-health-ai-system-dEf2q/whatsapp_webhook_n8n.json" \
  -o /tmp/workflow_raw.json

# Remove campos que causam conflito no import via API
python3 - <<'PYEOF'
import json, sys

with open("/tmp/workflow_raw.json") as f:
    wf = json.load(f)

# Remove campos que o n8n recusa no POST
for field in ["id", "versionId", "meta", "pinData", "staticData"]:
    wf.pop(field, None)

# Garante active=False no import (ativa depois)
wf["active"] = False

with open("/tmp/workflow_import.json", "w") as f:
    json.dump(wf, f)
print("Workflow preparado para import.")
PYEOF

IMPORT_RESP=$(curl -s -X POST "$N8N/api/v1/workflows" \
  -H "Authorization: Basic $AUTH" \
  -H "Content-Type: application/json" \
  -d @/tmp/workflow_import.json)

WORKFLOW_ID=$(echo "$IMPORT_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('id','ERRO'))" 2>/dev/null || echo "ERRO")

if [ "$WORKFLOW_ID" = "ERRO" ] || [ -z "$WORKFLOW_ID" ]; then
  echo "AVISO: Falha no import via API. Resposta:"
  echo "$IMPORT_RESP" | python3 -m json.tool 2>/dev/null || echo "$IMPORT_RESP"
else
  echo "Workflow importado com ID: $WORKFLOW_ID"
  # Ativa o workflow
  curl -s -X PATCH "$N8N/api/v1/workflows/$WORKFLOW_ID" \
    -H "Authorization: Basic $AUTH" \
    -H "Content-Type: application/json" \
    -d '{"active": true}' > /dev/null
  echo "Workflow ativado!"
fi

echo ""
echo "════════════════════════════════════════════════════════"
echo " PASSO 3 — Registrando credencial Anthropic no n8n"
echo "════════════════════════════════════════════════════════"

ANTHROPIC_KEY="${ANTHROPIC_API_KEY:-INSIRA_SUA_CHAVE_ANTHROPIC_AQUI}"

CRED_RESP=$(curl -s -X POST "$N8N/api/v1/credentials" \
  -H "Authorization: Basic $AUTH" \
  -H "Content-Type: application/json" \
  -d "{
    \"name\": \"Anthropic — Dr. João Holanda\",
    \"type\": \"anthropicApi\",
    \"data\": {
      \"apiKey\": \"$ANTHROPIC_KEY\"
    }
  }")

CRED_ID=$(echo "$CRED_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('id','ERRO'))" 2>/dev/null || echo "ERRO")
if [ "$CRED_ID" != "ERRO" ] && [ -n "$CRED_ID" ]; then
  echo "Credencial Anthropic criada com ID: $CRED_ID"
else
  echo "AVISO: Credencial Anthropic — adicione manualmente no painel n8n."
fi

echo ""
echo "════════════════════════════════════════════════════════"
echo " PASSO 4 — Criando instância WhatsApp na Evolution API"
echo "════════════════════════════════════════════════════════"

# Cria instância
CREATE_RESP=$(curl -s -X POST "$EVO/instance/create" \
  -H "apikey: $EVOLUTION_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"instanceName\": \"$INSTANCE\",
    \"qrcode\": true,
    \"integration\": \"WHATSAPP-BAILEYS\"
  }")

echo "Instância criada:"
echo "$CREATE_RESP" | python3 -m json.tool 2>/dev/null || echo "$CREATE_RESP"

# Configura webhook Evolution API → n8n
WEBHOOK_RESP=$(curl -s -X POST "$EVO/webhook/set/$INSTANCE" \
  -H "apikey: $EVOLUTION_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"url\": \"http://localhost:5678/webhook/whatsapp-webhook\",
    \"webhook_by_events\": false,
    \"webhook_base64\": false,
    \"events\": [
      \"MESSAGES_UPSERT\",
      \"MESSAGES_UPDATE\",
      \"SEND_MESSAGE\",
      \"CONNECTION_UPDATE\",
      \"QRCODE_UPDATED\"
    ]
  }")

echo "Webhook configurado:"
echo "$WEBHOOK_RESP" | python3 -m json.tool 2>/dev/null || echo "$WEBHOOK_RESP"

echo ""
echo "════════════════════════════════════════════════════════"
echo " PASSO 5 — Gerando QR Code para conectar WhatsApp"
echo "════════════════════════════════════════════════════════"
sleep 3
QR_RESP=$(curl -s "$EVO/instance/connect/$INSTANCE" \
  -H "apikey: $EVOLUTION_API_KEY")

QR_CODE=$(echo "$QR_RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
qr = d.get('qrcode', {})
print(qr.get('base64', qr.get('pairingCode', 'QR não disponível ainda')))
" 2>/dev/null || echo "QR não disponível ainda")

echo ""
echo "════════════════════════════════════════════════════════"
echo " ✔  CONFIGURAÇÃO CONCLUÍDA"
echo "════════════════════════════════════════════════════════"
echo ""
echo "  n8n             → http://${VPS_IP}:5678"
echo "  Evolution API   → http://${VPS_IP}:8080"
echo ""
echo "  QR Code (base64) para conectar WhatsApp do Sr. Edilson:"
echo "  $QR_CODE" | head -c 200
echo ""
echo "  Para ver o QR Code como imagem, cole o base64 em:"
echo "  https://base64.guru/converter/decode/image"
echo ""
echo "  Credenciais pendentes de adicionar manualmente no n8n:"
echo "  - OpenAI API Key (Whisper + GPT-4o Vision)"
echo "  - ElevenLabs API Key + Voice ID"
echo "  - Google Sheets OAuth2"
echo "  - Zep API Key"
echo ""
