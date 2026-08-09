#!/usr/bin/env bash
# fix_whatsapp_pairing.sh — Corrige "Não foi possível conectar o dispositivo"
#
# Causa (meta-issue EvolutionAPI/evolution-api#2437): o Baileys gera as
# pre-keys enquanto a Meta despeja o histórico da conta. O servidor satura,
# a geração estoura o timeout, vem stream error 515 e o WhatsApp derruba o
# dispositivo recém-pareado.
#
# Este script aplica a correção e refaz o pareamento do zero, porque a
# sessão anterior fica em estado inconsistente após a falha.
#
# Uso: bash /root/automacao/fix_whatsapp_pairing.sh
set -uo pipefail

cd /root/automacao
source .env

INST="${WHATSAPP_INSTANCE_NAME:-edilson}"
EVO="http://localhost:8080"
AUTH=(-H "apikey: $EVOLUTION_API_KEY" -H "Content-Type: application/json")

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║  Repareamento do WhatsApp — Dr. João Holanda        ║"
echo "╚══════════════════════════════════════════════════════╝"

# ─── PASSO 1: baixa o docker-compose corrigido ───────────────────────────────
echo ""
echo "════ PASSO 1 — Aplicando correção do Baileys ════"
BASE_URL="https://raw.githubusercontent.com/erickcavalcante81-hue/Home-Cabin-USA/claude/multimodal-health-ai-system-dEf2q"
curl -fsSL -H 'Cache-Control: no-cache' \
  "${BASE_URL}/infra/docker-compose.yml?cb=$(date +%s)$$" -o docker-compose.yml

if grep -q 'CONFIG_SESSION_PHONE_VERSION' docker-compose.yml; then
  echo "  docker-compose.yml atualizado ✓"
else
  echo "  ⚠  Veio do cache do CDN. Aplicando correção localmente..."
  python3 - <<'PYEOF'
import re
s = open('/root/automacao/docker-compose.yml').read()
s = s.replace('- CACHE_REDIS_ENABLED=true',  '- CACHE_REDIS_ENABLED=false')
s = s.replace('- CACHE_LOCAL_ENABLED=false', '- CACHE_LOCAL_ENABLED=true')
s = s.replace('- DATABASE_SAVE_DATA_CONTACTS=true', '- DATABASE_SAVE_DATA_CONTACTS=false')
s = s.replace('- DATABASE_SAVE_DATA_CHATS=true',    '- DATABASE_SAVE_DATA_CHATS=false')
s = s.replace('- DATABASE_SAVE_DATA_NEW_MESSAGE=true', '- DATABASE_SAVE_DATA_NEW_MESSAGE=false')
if 'CONFIG_SESSION_PHONE_VERSION' not in s:
    s = s.replace('      - LOG_LEVEL=ERROR',
                  '      - DATABASE_SAVE_DATA_HISTORIC=false\n'
                  '      - DATABASE_SAVE_DATA_LABELS=false\n'
                  '      - CONFIG_SESSION_PHONE_VERSION=2.3000.1033773198\n'
                  '      - LOG_LEVEL=ERROR', 1)
open('/root/automacao/docker-compose.yml','w').write(s)
print('  correção aplicada localmente ✓')
PYEOF
fi

# ─── PASSO 2: remove a instância corrompida ──────────────────────────────────
echo ""
echo "════ PASSO 2 — Removendo a sessão corrompida ════"
echo "  Desconectando..."
curl -s -X DELETE "${EVO}/instance/logout/${INST}" "${AUTH[@]}" >/dev/null 2>&1 || true
sleep 2
echo "  Apagando a instância..."
curl -s -X DELETE "${EVO}/instance/delete/${INST}" "${AUTH[@]}" >/dev/null 2>&1 || true
sleep 2

# ─── PASSO 3: recria o container com a nova configuração ─────────────────────
echo ""
echo "════ PASSO 3 — Recriando o Evolution API ════"
docker compose up -d --force-recreate evolution 2>&1 | tail -3

echo "  Aguardando a API responder..."
EVO_OK=false
for i in $(seq 1 24); do
  if [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "${EVO}/")" != "000" ]; then
    echo "  Evolution API no ar ✓ (tentativa $i)"; EVO_OK=true; break
  fi
  sleep 5
done

if [ "$EVO_OK" = "false" ]; then
  echo "  ✗ Evolution API não subiu. Logs:"
  docker logs --tail 25 evolution_api 2>&1 | sed 's/^/    /'
  exit 1
fi

# Limpa resíduos da sessão antiga no Redis (agora fora de uso pelo Evolution)
docker exec redis redis-cli -a "$REDIS_PASSWORD" --no-auth-warning \
  --scan --pattern "evolution*" 2>/dev/null | head -500 | while read -r k; do
  [ -n "$k" ] && docker exec redis redis-cli -a "$REDIS_PASSWORD" --no-auth-warning DEL "$k" >/dev/null 2>&1
done
echo "  Cache antigo da sessão limpo ✓"

# ─── PASSO 4: cria a instância limpa ─────────────────────────────────────────
echo ""
echo "════ PASSO 4 — Criando instância nova ════"
CREATE=$(curl -s -X POST "${EVO}/instance/create" "${AUTH[@]}" \
  -d "{\"instanceName\":\"${INST}\",\"qrcode\":true,\"integration\":\"WHATSAPP-BAILEYS\"}")
echo "  ${CREATE:0:200}"
sleep 6

# ─── PASSO 5: reconfigura o webhook ──────────────────────────────────────────
echo ""
echo "════ PASSO 5 — Reconfigurando o webhook do agente ════"
WH=$(curl -s -X POST "${EVO}/webhook/set/${INST}" "${AUTH[@]}" -d '{
  "webhook": {
    "enabled": true,
    "url": "http://joao_holanda_agent:3000/webhook/whatsapp",
    "byEvents": false,
    "base64": false,
    "events": ["MESSAGES_UPSERT","MESSAGES_UPDATE","CONNECTION_UPDATE"]
  }}')
echo "  ${WH:0:180}"

# ─── PASSO 6: QR Code no terminal ────────────────────────────────────────────
echo ""
echo "════ PASSO 6 — QR Code ════"
command -v qrencode >/dev/null 2>&1 || apt-get install -y -qq qrencode >/dev/null 2>&1

CODE=$(curl -s "${EVO}/instance/connect/${INST}" "${AUTH[@]}" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('code') or (d.get('qrcode') or {}).get('code', ''))
except Exception:
    print('')
")

if [ -n "$CODE" ] && command -v qrencode >/dev/null 2>&1; then
  echo ""
  qrencode -t ANSIUTF8 "$CODE"
  echo ""
  echo "  ⏱  ESCANEIE AGORA — o código expira em ~40 segundos."
  echo "     Se expirar, rode:  bash fix_whatsapp_pairing.sh --qr"
else
  echo "  Não foi possível gerar o QR no terminal."
  echo "  Abra no navegador:"
  echo "    http://${VPS_IP}:${AGENT_HOST_PORT:-3001}/qrcode?token=${AGENT_ACCESS_TOKEN:-}"
fi

echo ""
echo "  Depois de escanear, confirme a conexão com:"
echo "    curl -s ${EVO}/instance/connectionState/${INST} -H \"apikey: \$EVOLUTION_API_KEY\""
echo "  O estado deve ser \"open\"."
echo ""
