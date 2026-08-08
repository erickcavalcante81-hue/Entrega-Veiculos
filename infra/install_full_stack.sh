#!/usr/bin/env bash
# install_full_stack.sh — Instalação completa: Zep + Dr. João Holanda Agent
# Execute no console VNC: bash /root/automacao/install_full_stack.sh
set -euo pipefail

cd /root/automacao
source .env

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║  INSTALAÇÃO COMPLETA — Dr. João Holanda Cavalcante  ║"
echo "║  Segundo Cérebro Zep + Agente WhatsApp              ║"
echo "╚══════════════════════════════════════════════════════╝"

# ─── PASSO 1: Gera chaves faltantes ───────────────────────────────────────────
echo ""
echo "════ PASSO 1 — Gerando variáveis faltantes ════"

add_env() {
  local key="$1" val="$2"
  grep -q "^${key}=" .env || echo "${key}=${val}" >> .env
}

add_env "ZEP_API_KEY"       "zep_$(openssl rand -hex 32)"
add_env "ZEP_API_URL"       "http://localhost:8000"
add_env "ZEP_SESSION_ID"    "edilson_parintins_001"
add_env "ELEVENLABS_API_KEY" ""
add_env "ELEVENLABS_VOICE_ID" ""
add_env "OPENAI_API_KEY"    ""
add_env "EDILSON_PHONE"     ""
add_env "N8N_FAMILY_GROUP_WA_ID" ""

source .env
echo "  .env atualizado."

# ─── PASSO 2: Cria banco Zep no PostgreSQL ────────────────────────────────────
echo ""
echo "════ PASSO 2 — Criando banco de dados 'zep' ════"
docker exec postgres psql -U "$POSTGRES_USER" -c \
  "SELECT 'CREATE DATABASE zep' WHERE NOT EXISTS \
   (SELECT FROM pg_database WHERE datname = 'zep')\gexec" 2>/dev/null && \
  echo "  Banco 'zep' OK."

# ─── PASSO 3: Baixa arquivos do repositório ───────────────────────────────────
echo ""
echo "════ PASSO 3 — Baixando arquivos do repositório ════"
BASE_URL="https://raw.githubusercontent.com/erickcavalcante81-hue/Home-Cabin-USA/claude/multimodal-health-ai-system-dEf2q"

# docker-compose.yml atualizado (com Zep + Agent)
curl -fsSL "${BASE_URL}/infra/docker-compose.yml" -o docker-compose.yml
echo "  docker-compose.yml OK"

# Módulo de memória
mkdir -p integrations agents
curl -fsSL "${BASE_URL}/integrations/zep_memory.py" -o integrations/zep_memory.py
touch integrations/__init__.py
echo "  zep_memory.py OK"

# Agente Dr. João Holanda
curl -fsSL "${BASE_URL}/agents/joao_holanda_service.py" -o agents/joao_holanda_service.py
curl -fsSL "${BASE_URL}/agents/Dockerfile"              -o agents/Dockerfile
curl -fsSL "${BASE_URL}/agents/requirements.txt"        -o agents/requirements.txt
curl -fsSL "${BASE_URL}/agents/dr_joao_holanda_prompt.md" -o agents/dr_joao_holanda_prompt.md
echo "  Arquivos do agente OK"

# ─── PASSO 4: Sobe Zep ────────────────────────────────────────────────────────
echo ""
echo "════ PASSO 4 — Subindo Zep (segundo cérebro) ════"
docker compose up -d zep
echo "  Aguardando Zep inicializar..."
for i in $(seq 1 30); do
  HTTP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 \
    "http://localhost:8000/healthz" 2>/dev/null || echo "000")
  if [ "$HTTP" = "200" ]; then
    echo "  Zep OK! ✓"
    break
  fi
  echo "  Aguardando Zep... ($i/30)"; sleep 5
done

# ─── PASSO 5: Instala Python e inicializa conhecimento do paciente ────────────
echo ""
echo "════ PASSO 5 — Inicializando conhecimento do Sr. Edilson no Zep ════"
pip3 install -q httpx python-dotenv 2>/dev/null || true

python3 - <<PYEOF
import asyncio, sys, os
sys.path.insert(0, '/root/automacao')
for k,v in {'ZEP_API_URL':'http://localhost:8000',
            'ZEP_API_KEY':'${ZEP_API_KEY}',
            'ZEP_SESSION_ID':'edilson_parintins_001'}.items():
    os.environ[k] = v
from integrations.zep_memory import ensure_user_and_session, initialize_patient_knowledge

async def main():
    ok = await ensure_user_and_session()
    print(f"  Sessão Zep: {'criada' if ok else 'falhou'}")
    await initialize_patient_knowledge()
    print("  Conhecimento clínico do Sr. Edilson carregado ✓")

asyncio.run(main())
PYEOF

# ─── PASSO 6: Build e sobe o agente Dr. João Holanda ─────────────────────────
echo ""
echo "════ PASSO 6 — Build do agente Dr. João Holanda ════"
echo "  (pode demorar 2-3 min no primeiro build)"
docker compose build joao_holanda 2>&1 | tail -5
docker compose up -d joao_holanda
echo ""
echo "  Aguardando agente inicializar..."
for i in $(seq 1 20); do
  HTTP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 \
    "http://localhost:3000/health" 2>/dev/null || echo "000")
  if [ "$HTTP" = "200" ]; then
    echo "  Agente OK! ✓"
    break
  fi
  echo "  Aguardando agente... ($i/20)"; sleep 5
done

# ─── PASSO 7: Configura webhook Evolution API → Agente ────────────────────────
echo ""
echo "════ PASSO 7 — Configurando webhook Evolution API → Dr. João Holanda ════"
WH=$(curl -s -X POST "http://localhost:8080/webhook/set/edilson" \
  -H "apikey: $EVOLUTION_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "http://joao_holanda_agent:3000/webhook/whatsapp",
    "webhook_by_events": false,
    "webhook_base64": false,
    "events": ["MESSAGES_UPSERT","MESSAGES_UPDATE","CONNECTION_UPDATE"]
  }' 2>/dev/null)
echo "  Webhook: $WH"

# ─── PASSO 8: QR Code WhatsApp ────────────────────────────────────────────────
echo ""
echo "════ PASSO 8 — Gerando QR Code do WhatsApp ════"

# Cria instância se não existir
curl -s -X POST "http://localhost:8080/instance/create" \
  -H "apikey: $EVOLUTION_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"instanceName":"edilson","qrcode":true,"integration":"WHATSAPP-BAILEYS"}' \
  > /dev/null 2>&1 || true

sleep 5
QR=$(curl -s "http://localhost:8080/instance/connect/edilson" \
  -H "apikey: $EVOLUTION_API_KEY" 2>/dev/null)
QR_B64=$(echo "$QR" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    qr = d.get('qrcode', {})
    print(qr.get('base64', 'QR indisponível')[:400])
except:
    print('$QR'[:200])
" 2>/dev/null || echo "$QR")

# ─── Status final ─────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════"
docker compose ps
echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║  ✔  INSTALAÇÃO CONCLUÍDA                            ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "  Dr. João Holanda Agent → http://${VPS_IP}:3000"
echo "  Zep (segundo cérebro)  → http://${VPS_IP}:8000"
echo "  Evolution API           → http://${VPS_IP}:8080"
echo "  n8n                     → http://${VPS_IP}:5678"
echo ""
echo "  QR Code WhatsApp (cole em base64.guru/converter/decode/image):"
echo "  $QR_B64"
echo ""
echo "  Após escanear o QR Code, o Sr. Edilson pode enviar mensagem e"
echo "  o Dr. João Holanda responderá com voz e memória longitudinal."
echo ""
