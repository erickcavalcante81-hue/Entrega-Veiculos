#!/usr/bin/env bash
# setup_vps.sh — Provisionamento completo da VPS Ubuntu para o ecossistema Dr. João Holanda
# Executado remotamente via SSH a partir do Claude Code
set -euo pipefail

VPS_IP="$1"

echo ""
echo "════════════════════════════════════════════════════════"
echo " PASSO 1 — Atualização do sistema"
echo "════════════════════════════════════════════════════════"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq curl wget git ufw

echo ""
echo "════════════════════════════════════════════════════════"
echo " PASSO 2 — Instalação do Docker (script oficial)"
echo "════════════════════════════════════════════════════════"
if ! command -v docker &>/dev/null; then
  curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
  sh /tmp/get-docker.sh
  rm /tmp/get-docker.sh
  systemctl enable docker
  systemctl start docker
else
  echo "Docker já instalado: $(docker --version)"
fi

# Docker Compose v2 (plugin nativo)
if ! docker compose version &>/dev/null; then
  apt-get install -y -qq docker-compose-plugin
fi
echo "Docker Compose: $(docker compose version)"

echo ""
echo "════════════════════════════════════════════════════════"
echo " PASSO 3 — Criação do diretório /root/automacao"
echo "════════════════════════════════════════════════════════"
mkdir -p /root/automacao
cd /root/automacao

echo ""
echo "════════════════════════════════════════════════════════"
echo " PASSO 4 — Geração de senhas fortes e criação do .env"
echo "════════════════════════════════════════════════════════"
POSTGRES_PASSWORD=$(openssl rand -hex 24)
REDIS_PASSWORD=$(openssl rand -hex 24)
N8N_BASIC_AUTH_PASSWORD=$(openssl rand -hex 16)
N8N_ENCRYPTION_KEY=$(openssl rand -hex 32)
EVOLUTION_API_KEY=$(openssl rand -hex 32)

cat > /root/automacao/.env <<ENV
VPS_IP=${VPS_IP}

POSTGRES_USER=automacao
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
POSTGRES_DB=automacao
N8N_DB_NAME=n8n
EVOLUTION_DB_NAME=evolution

REDIS_PASSWORD=${REDIS_PASSWORD}

N8N_BASIC_AUTH_USER=admin
N8N_BASIC_AUTH_PASSWORD=${N8N_BASIC_AUTH_PASSWORD}
N8N_ENCRYPTION_KEY=${N8N_ENCRYPTION_KEY}

EVOLUTION_API_KEY=${EVOLUTION_API_KEY}
ENV
chmod 600 /root/automacao/.env

echo ""
echo "════════════════════════════════════════════════════════"
echo " PASSO 5 — Criação do init-db.sql"
echo "════════════════════════════════════════════════════════"
cat > /root/automacao/init-db.sql <<'SQL'
SELECT 'CREATE DATABASE n8n'
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'n8n')\gexec

SELECT 'CREATE DATABASE evolution'
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'evolution')\gexec
SQL

echo ""
echo "════════════════════════════════════════════════════════"
echo " PASSO 6 — Criação do docker-compose.yml"
echo "════════════════════════════════════════════════════════"
cat > /root/automacao/docker-compose.yml <<'COMPOSE'
services:

  postgres:
    image: postgres:16-alpine
    container_name: postgres
    restart: always
    environment:
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB}
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./init-db.sql:/docker-entrypoint-initdb.d/init-db.sql:ro
    networks:
      - automacao_net
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DB}"]
      interval: 10s
      timeout: 5s
      retries: 10
      start_period: 30s

  redis:
    image: redis:7-alpine
    container_name: redis
    restart: always
    command: redis-server --requirepass ${REDIS_PASSWORD} --maxmemory 256mb --maxmemory-policy allkeys-lru
    volumes:
      - redis_data:/data
    networks:
      - automacao_net
    healthcheck:
      test: ["CMD", "redis-cli", "-a", "${REDIS_PASSWORD}", "ping"]
      interval: 10s
      timeout: 5s
      retries: 10
      start_period: 10s

  n8n:
    image: docker.n8n.io/n8nio/n8n:latest
    container_name: n8n
    restart: always
    ports:
      - "5678:5678"
    environment:
      - DB_TYPE=postgresdb
      - DB_POSTGRESDB_HOST=postgres
      - DB_POSTGRESDB_PORT=5432
      - DB_POSTGRESDB_DATABASE=${N8N_DB_NAME}
      - DB_POSTGRESDB_USER=${POSTGRES_USER}
      - DB_POSTGRESDB_PASSWORD=${POSTGRES_PASSWORD}
      - N8N_BASIC_AUTH_ACTIVE=true
      - N8N_BASIC_AUTH_USER=${N8N_BASIC_AUTH_USER}
      - N8N_BASIC_AUTH_PASSWORD=${N8N_BASIC_AUTH_PASSWORD}
      - N8N_ENCRYPTION_KEY=${N8N_ENCRYPTION_KEY}
      - WEBHOOK_URL=http://${VPS_IP}:5678/
      - GENERIC_TIMEZONE=America/Manaus
      - N8N_LOG_LEVEL=warn
      - EXECUTIONS_DATA_PRUNE=true
      - EXECUTIONS_DATA_MAX_AGE=168
    volumes:
      - n8n_data:/home/node/.n8n
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    networks:
      - automacao_net

  evolution:
    image: atendai/evolution-api:latest
    container_name: evolution_api
    restart: always
    ports:
      - "8080:8080"
    environment:
      - SERVER_URL=http://${VPS_IP}:8080
      - AUTHENTICATION_TYPE=apikey
      - AUTHENTICATION_API_KEY=${EVOLUTION_API_KEY}
      - AUTHENTICATION_EXPOSE_IN_FETCH_INSTANCES=true
      - DATABASE_ENABLED=true
      - DATABASE_CONNECTION_URI=postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${EVOLUTION_DB_NAME}
      - DATABASE_CONNECTION_CLIENT_NAME=evolution
      - DATABASE_SAVE_DATA_INSTANCE=true
      - DATABASE_SAVE_DATA_NEW_MESSAGE=true
      - DATABASE_SAVE_MESSAGE_UPDATE=true
      - DATABASE_SAVE_DATA_CONTACTS=true
      - DATABASE_SAVE_DATA_CHATS=true
      - CACHE_REDIS_ENABLED=true
      - CACHE_REDIS_URI=redis://:${REDIS_PASSWORD}@redis:6379/6
      - CACHE_REDIS_PREFIX_KEY=evolution
      - CACHE_REDIS_SAVE_INSTANCES=true
      - CACHE_LOCAL_ENABLED=false
      - LOG_LEVEL=ERROR
      - LOG_COLOR=false
      - DEL_INSTANCE=false
      - QRCODE_LIMIT=30
    volumes:
      - evolution_instances:/evolution/instances
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    networks:
      - automacao_net

volumes:
  postgres_data:
  redis_data:
  n8n_data:
  evolution_instances:

networks:
  automacao_net:
    driver: bridge
COMPOSE

echo ""
echo "════════════════════════════════════════════════════════"
echo " PASSO 7 — Configuração do Firewall (UFW)"
echo "════════════════════════════════════════════════════════"
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw allow 5678/tcp   # n8n
ufw allow 8080/tcp   # Evolution API
ufw --force enable

echo ""
echo "════════════════════════════════════════════════════════"
echo " PASSO 8 — Deploy: docker compose up -d"
echo "════════════════════════════════════════════════════════"
cd /root/automacao
docker compose up -d

echo ""
echo "════════════════════════════════════════════════════════"
echo " PASSO 9 — Aguardando containers ficarem saudáveis..."
echo "════════════════════════════════════════════════════════"
sleep 20
docker compose ps

echo ""
echo "════════════════════════════════════════════════════════"
echo " ✔  INSTALAÇÃO CONCLUÍDA"
echo "════════════════════════════════════════════════════════"
echo ""
echo "  n8n           → http://${VPS_IP}:5678"
echo "  Evolution API → http://${VPS_IP}:8080"
echo ""
echo "  Credenciais salvas em: /root/automacao/.env"
echo ""
echo "  n8n login:"
echo "    Usuário: admin"
echo "    Senha:   ${N8N_BASIC_AUTH_PASSWORD}"
echo ""
echo "  Evolution API Key: ${EVOLUTION_API_KEY}"
echo ""
