#!/usr/bin/env bash
# install_full_stack.sh — Instalação completa: Zep + Dr. João Holanda Agent
# Execute no console VNC: bash /root/automacao/install_full_stack.sh
set -uo pipefail   # sem -e: erros não fatais tratados individualmente

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

add_env "ZEP_API_KEY"            "zep_$(openssl rand -hex 32)"
add_env "ZEP_API_URL"            "http://localhost:8000"
add_env "ZEP_SESSION_ID"         "edilson_parintins_001"
add_env "ELEVENLABS_API_KEY"     ""
add_env "ELEVENLABS_VOICE_ID"    ""
add_env "OPENAI_API_KEY"         ""
add_env "NVIDIA_NIM_API_KEY"     ""
add_env "EDILSON_PHONE"          ""
add_env "N8N_FAMILY_GROUP_WA_ID" ""

source .env
echo "  .env atualizado."

# ─── PASSO 2: Baixa arquivos do repositório ───────────────────────────────────
# IMPORTANTE: deve vir ANTES da migração do PostgreSQL, senão o
# docker compose usaria o docker-compose.yml antigo (sem pgvector).
echo ""
echo "════ PASSO 2 — Baixando arquivos do repositório ════"
BASE_URL="https://raw.githubusercontent.com/erickcavalcante81-hue/Home-Cabin-USA/claude/multimodal-health-ai-system-dEf2q"

curl -fsSL "${BASE_URL}/infra/docker-compose.yml"              -o docker-compose.yml
echo "  docker-compose.yml OK"

mkdir -p integrations agents
curl -fsSL "${BASE_URL}/integrations/zep_memory.py"            -o integrations/zep_memory.py
touch integrations/__init__.py
echo "  zep_memory.py OK"

curl -fsSL "${BASE_URL}/agents/joao_holanda_service.py"        -o agents/joao_holanda_service.py
curl -fsSL "${BASE_URL}/agents/Dockerfile"                     -o agents/Dockerfile
curl -fsSL "${BASE_URL}/agents/requirements.txt"               -o agents/requirements.txt
curl -fsSL "${BASE_URL}/agents/dr_joao_holanda_prompt.md"      -o agents/dr_joao_holanda_prompt.md
echo "  Arquivos do agente OK"

# ─── PASSO 3: PostgreSQL com pgvector + banco 'zep' ───────────────────────────
echo ""
echo "════ PASSO 3 — PostgreSQL: pgvector + banco 'zep' ════"

# O Zep exige a extensão pgvector, ausente na imagem postgres:16-alpine.
PG_IMAGE=$(docker inspect --format='{{.Config.Image}}' postgres 2>/dev/null || echo "nenhuma")
echo "  Imagem atual: $PG_IMAGE"
if [[ "$PG_IMAGE" != *"pgvector"* ]]; then
  echo "  Sem pgvector — migrando para pgvector/pgvector:pg16..."
  docker compose pull postgres 2>&1 | tail -3
  docker compose up -d --force-recreate postgres
else
  echo "  pgvector já presente na imagem ✓"
fi

echo "  Aguardando PostgreSQL aceitar conexões..."
PG_READY=false
for i in $(seq 1 40); do
  if docker exec postgres pg_isready -U "$POSTGRES_USER" >/dev/null 2>&1; then
    echo "  PostgreSQL pronto ✓ (tentativa $i)"; PG_READY=true; break
  fi
  sleep 3
done

if [ "$PG_READY" = "false" ]; then
  echo "  ERRO: PostgreSQL não respondeu. Logs:"
  docker logs --tail 20 postgres 2>&1 || true
else
  # Cria o banco 'zep' (\gexec não funciona com psql -c, por isso 2 comandos)
  if docker exec postgres psql -U "$POSTGRES_USER" -tAc \
       "SELECT 1 FROM pg_database WHERE datname='zep'" 2>/dev/null | grep -q 1; then
    echo "  Banco 'zep' já existe ✓"
  else
    echo "  Criando banco 'zep'..."
    docker exec postgres psql -U "$POSTGRES_USER" -c "CREATE DATABASE zep" 2>&1 | sed 's/^/    /'
  fi

  # Habilita pgvector dentro do banco zep
  echo "  Habilitando extensão pgvector..."
  docker exec postgres psql -U "$POSTGRES_USER" -d zep \
    -c "CREATE EXTENSION IF NOT EXISTS vector" 2>&1 | sed 's/^/    /'

  # Verificação final — falha aqui significa que o Zep não vai subir
  PGV=$(docker exec postgres psql -U "$POSTGRES_USER" -d zep -tAc \
    "SELECT extversion FROM pg_extension WHERE extname='vector'" 2>/dev/null | tr -d '[:space:]')
  if [ -n "$PGV" ]; then
    echo "  ✓ Banco 'zep' pronto com pgvector v${PGV}"
  else
    echo "  ✗ ERRO CRÍTICO: pgvector não habilitado no banco 'zep'."
    echo "    O Zep não conseguirá iniciar. Bancos existentes:"
    docker exec postgres psql -U "$POSTGRES_USER" -lqt 2>&1 | cut -d'|' -f1 | sed 's/^/      /'
  fi
fi

# ─── PASSO 4: Instala dependências Python (antes de qualquer script Python) ───
echo ""
echo "════ PASSO 4 — Instalando dependências Python no host ════"
_pip_ok=false
if pip3 install httpx python-dotenv 2>&1; then
  _pip_ok=true
  echo "  pip3 install OK ✓"
elif pip3 install --break-system-packages httpx python-dotenv 2>&1; then
  _pip_ok=true
  echo "  pip3 install (--break-system-packages) OK ✓"
elif python3 -m pip install --user httpx python-dotenv 2>&1; then
  _pip_ok=true
  echo "  pip --user install OK ✓"
else
  echo "  AVISO: pip install falhou. Tentando via apt..."
  apt-get install -y python3-httpx python3-dotenv 2>/dev/null \
    && _pip_ok=true \
    || echo "  AVISO: httpx não instalado — init Zep será pulado."
fi

# Confirma importação
python3 -c "import httpx; print('  httpx disponível ✓')" 2>/dev/null \
  || echo "  AVISO: httpx ainda não importável (init Zep será pulado)."

# ─── PASSO 5: Gera arquivo de configuração do Zep ─────────────────────────────
echo ""
echo "════ PASSO 5 — Gerando zep_config.yaml ════"
# Zep CE v0.27.x ignora variáveis de ambiente para store.type e llm;
# é necessário fornecer um config.yaml explícito montado no container.
cat > /root/automacao/zep_config.yaml << ZEOF
server:
  port: 8000

store:
  type: postgres
  postgres:
    dsn: "postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/zep?sslmode=disable"

auth:
  required: true
  secret: "${ZEP_API_KEY}"

llm:
  service: openai
  model: meta/llama-3.1-8b-instruct
  openai_api_key: "${NVIDIA_NIM_API_KEY}"
  openai_endpoint: "https://integrate.api.nvidia.com/v1"

extractors:
  documents:
    embeddings:
      enabled: true
      dimensions: 1024
      service: openai
      model: nvidia/nv-embedqa-e5-v5
  messages:
    embeddings:
      enabled: true
      dimensions: 1024
      service: openai
      model: nvidia/nv-embedqa-e5-v5

log:
  level: info

memory:
  message_window: 12
ZEOF
echo "  zep_config.yaml gerado ✓"
echo "  Store: postgres | LLM: meta/llama-3.1-8b-instruct via NIM"

# ─── PASSO 6: Sobe Zep ────────────────────────────────────────────────────────
echo ""
echo "════ PASSO 6 — Subindo Zep (segundo cérebro) ════"
docker compose up -d zep

echo "  Aguardando Zep inicializar (máx 10 min — baixa modelos NLP na 1ª vez)..."
ZEP_OK=false
for i in $(seq 1 60); do
  HTTP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 \
    "http://localhost:8000/healthz" 2>/dev/null || echo "000")
  if [ "$HTTP" = "200" ]; then
    echo "  Zep OK! ✓  (tentativa $i)"
    ZEP_OK=true
    break
  fi
  # A cada 10 tentativas exibe os últimos logs para diagnóstico
  if (( i % 10 == 0 )); then
    echo "  --- Zep logs (últimas 5 linhas) ---"
    docker logs --tail 5 zep 2>/dev/null || true
    echo "  ---"
  fi
  echo "  Aguardando Zep... ($i/60)"; sleep 10
done

if [ "$ZEP_OK" = "false" ]; then
  echo ""
  echo "  ╔══ DIAGNÓSTICO ZEP ══╗"
  docker logs --tail 30 zep 2>/dev/null || true
  echo "  ╚═══════════════════╝"
  echo "  AVISO: Zep não respondeu em 10 min."
  echo "  Continuando com instalação do agente (Zep pode ainda estar iniciando)."
fi

# ─── PASSO 7: Inicializa conhecimento do paciente no Zep ──────────────────────
echo ""
echo "════ PASSO 7 — Inicializando conhecimento do Sr. Edilson no Zep ════"
if [ "$ZEP_OK" = "true" ] && python3 -c "import httpx" 2>/dev/null; then
  python3 - <<PYEOF
import asyncio, sys, os
sys.path.insert(0, '/root/automacao')
for k,v in {'ZEP_API_URL': 'http://localhost:8000',
            'ZEP_API_KEY': '${ZEP_API_KEY}',
            'ZEP_SESSION_ID': 'edilson_parintins_001'}.items():
    os.environ[k] = v
from integrations.zep_memory import ensure_user_and_session, initialize_patient_knowledge

async def main():
    ok = await ensure_user_and_session()
    print(f"  Sessão Zep: {'criada ✓' if ok else 'FALHOU'}")
    if ok:
        await initialize_patient_knowledge()
        print("  Conhecimento clínico do Sr. Edilson carregado ✓")

asyncio.run(main())
PYEOF
  echo "  Init Zep concluído."
else
  echo "  PULADO: Zep não está pronto ou httpx ausente."
  echo "  Execute depois: cd /root/automacao && python3 -c \""
  echo "    import asyncio, sys, os; sys.path.insert(0,'.'); os.environ.update({'ZEP_API_URL':'http://localhost:8000','ZEP_API_KEY':'${ZEP_API_KEY}','ZEP_SESSION_ID':'edilson_parintins_001'})"
  echo "    from integrations.zep_memory import ensure_user_and_session, initialize_patient_knowledge"
  echo "    asyncio.run(initialize_patient_knowledge())\""
fi

# ─── PASSO 8: Build e sobe o agente Dr. João Holanda ─────────────────────────
echo ""
echo "════ PASSO 8 — Build do agente Dr. João Holanda ════"
echo "  (pode demorar 2-3 min no primeiro build)"
docker compose build joao_holanda 2>&1 | tail -10
docker compose up -d joao_holanda
echo ""
echo "  Aguardando agente inicializar..."
AGENT_OK=false
for i in $(seq 1 24); do
  HTTP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 \
    "http://localhost:3000/health" 2>/dev/null || echo "000")
  if [ "$HTTP" = "200" ]; then
    echo "  Agente Dr. João Holanda OK! ✓"
    AGENT_OK=true
    break
  fi
  echo "  Aguardando agente... ($i/24)"; sleep 5
done
if [ "$AGENT_OK" = "false" ]; then
  echo "  AVISO: agente não respondeu. Logs:"
  docker logs --tail 20 joao_holanda_agent 2>/dev/null || true
fi

# ─── PASSO 9: Configura webhook Evolution API → Agente ────────────────────────
echo ""
echo "════ PASSO 9 — Configurando webhook Evolution API → Dr. João Holanda ════"

# Aguarda Evolution API estar no ar (pode estar reiniciando)
EVO_OK=false
for i in $(seq 1 12); do
  HTTP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 \
    "http://localhost:8080/" 2>/dev/null || echo "000")
  if [ "$HTTP" != "000" ]; then
    EVO_OK=true; break
  fi
  echo "  Aguardando Evolution API... ($i/12)"; sleep 10
done

if [ "$EVO_OK" = "true" ]; then
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
else
  echo "  AVISO: Evolution API indisponível. Webhook não configurado."
  echo "  Logs Evolution:"
  docker logs --tail 10 evolution_api 2>/dev/null || true
fi

# ─── PASSO 10: QR Code WhatsApp ────────────────────────────────────────────────
echo ""
echo "════ PASSO 10 — Gerando QR Code do WhatsApp ════"

if [ "$EVO_OK" = "true" ]; then
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
except Exception as e:
    print('Erro ao parsear QR:', str(e))
" 2>/dev/null || echo "$QR")
else
  QR_B64="Evolution API indisponível — execute depois: curl -s http://localhost:8080/instance/connect/edilson -H 'apikey: \$EVOLUTION_API_KEY'"
fi

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
if [ "$ZEP_OK" = "false" ]; then
  echo "  ⚠  Zep ainda iniciando — aguarde ~5 min e verifique:"
  echo "     curl http://localhost:8000/healthz"
  echo "     docker logs zep"
fi
echo ""
echo "  Após escanear o QR Code, o Sr. Edilson pode enviar mensagem e"
echo "  o Dr. João Holanda responderá com voz e memória longitudinal."
echo ""
