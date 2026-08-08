#!/usr/bin/env bash
# install_zep.sh — Instala o Zep (segundo cérebro) na VPS e inicializa
# conhecimento clínico do Sr. Edilson
# Execute no console VNC: bash /root/automacao/install_zep.sh
set -euo pipefail

cd /root/automacao
source .env

echo ""
echo "════════════════════════════════════════════════════════"
echo " INSTALANDO ZEP — Segundo Cérebro do Dr. João Holanda"
echo "════════════════════════════════════════════════════════"

# ─── Gera chave Zep se não existir ────────────────────────────────────────────
if ! grep -q "^ZEP_API_KEY=" .env; then
  ZEP_KEY="zep_$(openssl rand -hex 32)"
  echo "ZEP_API_KEY=$ZEP_KEY" >> .env
  echo "ZEP_API_URL=http://localhost:8000" >> .env
  echo "ZEP_SESSION_ID=edilson_parintins_001" >> .env
  echo "  Chave Zep gerada: ${ZEP_KEY:0:20}..."
else
  ZEP_KEY=$(grep "^ZEP_API_KEY=" .env | cut -d= -f2)
  echo "  Chave Zep já existe."
fi

# ─── Cria banco zep no PostgreSQL ─────────────────────────────────────────────
echo ""
echo "Criando banco de dados 'zep' no PostgreSQL..."
docker exec postgres psql -U "$POSTGRES_USER" -c \
  "SELECT 'CREATE DATABASE zep' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'zep')\gexec" 2>/dev/null
echo "  Banco 'zep' OK."

# ─── Baixa docker-compose.yml atualizado do repositório ───────────────────────
echo ""
echo "Atualizando docker-compose.yml com o serviço Zep..."
curl -fsSL \
  "https://raw.githubusercontent.com/erickcavalcante81-hue/Home-Cabin-USA/claude/multimodal-health-ai-system-dEf2q/infra/docker-compose.yml" \
  -o docker-compose.yml
echo "  docker-compose.yml atualizado."

# ─── Sobe o Zep ───────────────────────────────────────────────────────────────
echo ""
echo "Subindo container Zep..."
docker compose up -d zep

echo ""
echo "Aguardando Zep inicializar (pode demorar até 60s na primeira vez)..."
for i in $(seq 1 24); do
  HTTP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 \
    "http://localhost:8000/healthz" 2>/dev/null || echo "000")
  if [ "$HTTP" = "200" ]; then
    echo "  Zep respondendo!"
    break
  fi
  echo "  Aguardando... ($i/24)"
  sleep 5
done

# ─── Instala dependências Python ───────────────────────────────────────────────
echo ""
echo "Instalando dependências Python (httpx, python-dotenv)..."
pip3 install -q httpx python-dotenv 2>/dev/null || \
  apt-get install -y -qq python3-pip && pip3 install -q httpx python-dotenv

# ─── Baixa módulo zep_memory.py ───────────────────────────────────────────────
echo ""
echo "Baixando módulo zep_memory.py..."
mkdir -p /root/automacao/integrations
curl -fsSL \
  "https://raw.githubusercontent.com/erickcavalcante81-hue/Home-Cabin-USA/claude/multimodal-health-ai-system-dEf2q/integrations/zep_memory.py" \
  -o /root/automacao/integrations/zep_memory.py
touch /root/automacao/integrations/__init__.py

# ─── Inicializa conhecimento do Sr. Edilson ────────────────────────────────────
echo ""
echo "Inicializando conhecimento clínico do Sr. Edilson no Zep..."
cd /root/automacao
python3 - <<PYEOF
import asyncio, sys, os
sys.path.insert(0, '/root/automacao')

# Configura env vars inline para o script
os.environ['ZEP_API_URL'] = 'http://localhost:8000'
os.environ['ZEP_SESSION_ID'] = 'edilson_parintins_001'

# Lê a chave do .env
with open('/root/automacao/.env') as f:
    for line in f:
        if line.startswith('ZEP_API_KEY='):
            os.environ['ZEP_API_KEY'] = line.strip().split('=', 1)[1]
            break

from integrations.zep_memory import (
    ensure_user_and_session,
    initialize_patient_knowledge,
    save_interaction,
    get_context,
)

async def main():
    print("  Criando usuário e sessão do Sr. Edilson...")
    ok = await ensure_user_and_session()
    print(f"  Sessão: {'OK' if ok else 'AVISO — verifique o Zep'}")

    print("  Carregando fatos clínicos iniciais...")
    await initialize_patient_knowledge()

    print("  Salvando primeira interação de boas-vindas...")
    await save_interaction(
        patient_message="[SISTEMA] Primeiro contato — Dr. João Holanda iniciando acompanhamento do Sr. Edilson.",
        agent_response="Olá Sr. Edilson! Sou o Dr. João Holanda, seu médico de acompanhamento. "
                       "Estou aqui para ajudá-lo a cuidar bem da sua saúde. "
                       "Pode me chamar pelo WhatsApp sempre que precisar!",
        metadata={"tipo": "boas_vindas", "sistema": True},
    )

    print("  Verificando contexto recuperado...")
    ctx = await get_context()
    print(f"\n  CONTEXTO INICIAL:\n{ctx[:600]}")

asyncio.run(main())
PYEOF

echo ""
echo "════════════════════════════════════════════════════════"
echo " ✔  ZEP INSTALADO E INICIALIZADO"
echo "════════════════════════════════════════════════════════"
echo ""
echo "  Zep API    → http://${VPS_IP}:8000"
echo "  Session ID → edilson_parintins_001"
echo "  ZEP_API_KEY salva em /root/automacao/.env"
echo ""
echo "  Próximo passo: corrigir Evolution API (crash-loop)"
echo "  Execute: docker logs evolution_api --tail 40"
echo ""
