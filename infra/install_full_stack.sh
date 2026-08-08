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
add_env "NIM_CHAT_MODEL"         "meta/llama-3.3-70b-instruct"
add_env "EDILSON_PHONE"          ""
add_env "N8N_FAMILY_GROUP_WA_ID" ""

source .env
echo "  .env atualizado."

# O NIM é o motor único: sem essa chave nada funciona
if [ -z "${NVIDIA_NIM_API_KEY:-}" ]; then
  echo "  ⚠  NVIDIA_NIM_API_KEY vazia — o Dr. João Holanda não responderá"
  echo "     e a memória do Zep ficará inativa. Configure antes de continuar:"
  echo "     echo 'NVIDIA_NIM_API_KEY=nvapi-...' >> /root/automacao/.env"
else
  echo "  Motor de IA: Nvidia NIM (${NIM_CHAT_MODEL})"
fi
[ -z "${ELEVENLABS_API_KEY:-}" ] && \
  echo "  ℹ  ELEVENLABS_API_KEY vazia — respostas serão em texto, não em áudio."

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
curl -fsSL "${BASE_URL}/integrations/__init__.py"              -o integrations/__init__.py \
  || touch integrations/__init__.py
echo "  zep_memory.py OK"

# Verifica que todos os arquivos necessários ao build chegaram
MISSING=""
for f in agents/Dockerfile agents/requirements.txt agents/joao_holanda_service.py \
         integrations/zep_memory.py integrations/__init__.py; do
  [ -s "$f" ] || MISSING="$MISSING $f"
done

curl -fsSL "${BASE_URL}/agents/joao_holanda_service.py"        -o agents/joao_holanda_service.py
curl -fsSL "${BASE_URL}/agents/Dockerfile"                     -o agents/Dockerfile
curl -fsSL "${BASE_URL}/agents/requirements.txt"               -o agents/requirements.txt
curl -fsSL "${BASE_URL}/agents/dr_joao_holanda_prompt.md"      -o agents/dr_joao_holanda_prompt.md

if [ -n "$MISSING" ]; then
  echo "  ✗ ERRO: arquivos ausentes ou vazios:$MISSING"
  echo "    O build do agente vai falhar. Verifique a conexão com o GitHub."
else
  echo "  Arquivos do agente OK ✓ (todos verificados)"
fi

# Espaço em disco — build do agente precisa de ~1,5 GB
echo "  Espaço livre em disco:"
df -h / | tail -1 | sed 's/^/    /'

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
BUILD_LOG=$(docker compose build joao_holanda 2>&1)
if echo "$BUILD_LOG" | grep -qiE 'ERROR|failed to (solve|compute)'; then
  echo "  ✗ BUILD FALHOU — saída completa:"
  echo "$BUILD_LOG" | tail -40 | sed 's/^/    /'
else
  echo "$BUILD_LOG" | tail -5 | sed 's/^/    /'
  echo "  Build OK ✓"
fi
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

# ─── PASSO 9: Cria instância WhatsApp e configura webhook ────────────────────
# A instância deve existir ANTES de configurar o webhook, senão retorna 404.
echo ""
echo "════ PASSO 9 — Instância WhatsApp + webhook → Dr. João Holanda ════"

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

if [ "$EVO_OK" = "false" ]; then
  echo "  AVISO: Evolution API indisponível. Logs:"
  docker logs --tail 10 evolution_api 2>/dev/null || true
else
  # 9a. Verifica se a instância 'edilson' já existe
  INST=$(curl -s "http://localhost:8080/instance/fetchInstances" \
    -H "apikey: $EVOLUTION_API_KEY" 2>/dev/null)
  if echo "$INST" | grep -q '"edilson"'; then
    echo "  Instância 'edilson' já existe ✓"
  else
    echo "  Criando instância 'edilson'..."
    CREATE=$(curl -s -X POST "http://localhost:8080/instance/create" \
      -H "apikey: $EVOLUTION_API_KEY" \
      -H "Content-Type: application/json" \
      -d '{"instanceName":"edilson","qrcode":true,"integration":"WHATSAPP-BAILEYS"}' 2>&1)
    echo "    ${CREATE:0:300}"
    sleep 5
  fi

  # 9b. Configura o webhook — Evolution v2.x usa payload aninhado em "webhook"
  echo "  Configurando webhook..."
  WH=$(curl -s -X POST "http://localhost:8080/webhook/set/edilson" \
    -H "apikey: $EVOLUTION_API_KEY" \
    -H "Content-Type: application/json" \
    -d '{
      "webhook": {
        "enabled": true,
        "url": "http://joao_holanda_agent:3000/webhook/whatsapp",
        "byEvents": false,
        "base64": false,
        "events": ["MESSAGES_UPSERT","MESSAGES_UPDATE","CONNECTION_UPDATE"]
      }
    }' 2>&1)

  # Fallback para o formato plano da v1.x, caso a v2 rejeite
  if echo "$WH" | grep -qi '"error"\|"status":4'; then
    echo "    Formato v2 rejeitado, tentando v1..."
    WH=$(curl -s -X POST "http://localhost:8080/webhook/set/edilson" \
      -H "apikey: $EVOLUTION_API_KEY" \
      -H "Content-Type: application/json" \
      -d '{
        "url": "http://joao_holanda_agent:3000/webhook/whatsapp",
        "webhook_by_events": false,
        "webhook_base64": false,
        "enabled": true,
        "events": ["MESSAGES_UPSERT","MESSAGES_UPDATE","CONNECTION_UPDATE"]
      }' 2>&1)
  fi
  echo "    ${WH:0:300}"
fi

# ─── PASSO 10: QR Code WhatsApp ────────────────────────────────────────────────
echo ""
echo "════ PASSO 10 — Gerando QR Code do WhatsApp ════"

if [ "$EVO_OK" = "true" ]; then
  QR=$(curl -s "http://localhost:8080/instance/connect/edilson" \
    -H "apikey: $EVOLUTION_API_KEY" 2>/dev/null)

  # Salva o QR como PNG para abrir direto no navegador
  echo "$QR" | python3 -c "
import sys, json, base64
try:
    d = json.load(sys.stdin)
except Exception:
    print('  Resposta não-JSON da Evolution API:'); print('  ' + sys.stdin.read()[:200]); raise SystemExit

b64 = d.get('base64') or d.get('qrcode', {}).get('base64', '')
if b64:
    raw = b64.split(',', 1)[-1]          # remove prefixo data:image/png;base64,
    with open('/root/automacao/qrcode.png', 'wb') as f:
        f.write(base64.b64decode(raw))
    print('  QR Code salvo em /root/automacao/qrcode.png ✓')
    print('  Base64 (cole em base64.guru/converter/decode/image):')
    print('  ' + b64[:300] + '...')
elif d.get('pairingCode'):
    print('  Código de pareamento: ' + str(d['pairingCode']))
elif d.get('instance', {}).get('state') == 'open':
    print('  WhatsApp JÁ CONECTADO ✓ — não é necessário escanear.')
else:
    print('  QR indisponível. Resposta: ' + json.dumps(d)[:250])
" 2>&1 || echo "  Erro ao processar QR: ${QR:0:200}"
else
  echo "  Evolution API indisponível — rode depois:"
  echo "    curl -s http://localhost:8080/instance/connect/edilson -H \"apikey: \$EVOLUTION_API_KEY\""
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

if [ "$ZEP_OK" = "false" ]; then
  echo "  ⚠  Zep não respondeu — verifique:"
  echo "     curl http://localhost:8000/healthz  &&  docker logs zep"
  echo ""
fi
if [ "$AGENT_OK" = "false" ]; then
  echo "  ⚠  Agente Dr. João Holanda não subiu — verifique:"
  echo "     docker logs joao_holanda_agent"
  echo "     docker compose build joao_holanda"
  echo ""
fi

if [ -f /root/automacao/qrcode.png ]; then
  echo "  📱 QR Code salvo em: /root/automacao/qrcode.png"
  echo "     Baixe com:  scp root@${VPS_IP}:/root/automacao/qrcode.png ."
  echo ""
fi

echo "  Após escanear o QR Code, o Sr. Edilson pode enviar mensagem e"
echo "  o Dr. João Holanda responderá com voz e memória longitudinal."
echo ""
