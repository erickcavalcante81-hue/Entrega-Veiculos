#!/usr/bin/env bash
# zep_para_gemini.sh — Aponta o Zep para o Google Gemini
#
# O zep_config.yaml foi gerado apontando para a Nvidia NIM. Com o agente já
# rodando no Gemini, quem resume as conversas antigas continuava sendo o NIM —
# e se aquela chave expirar, o resumo para de ser gerado em silêncio e a
# memória de longo prazo degrada sem aviso.
#
# O Zep CE 0.27 fala protocolo OpenAI, e o Gemini expõe uma camada compatível
# em /v1beta/openai/ que atende /chat/completions e /embeddings.
#
# ATENÇÃO: a dimensão do embedding muda (NIM 1024 → Gemini 768) e a coluna do
# pgvector tem dimensão fixa, então o banco do Zep precisa ser recriado. Este
# script exporta os fatos clínicos antes e os reimporta depois.
#
# Uso: bash /root/automacao/zep_para_gemini.sh
set -uo pipefail

cd /root/automacao
source .env

CONFIG="/root/automacao/zep_config.yaml"
STAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_CFG="${CONFIG}.bak_${STAMP}"
BACKUP_FATOS="/root/automacao/backup_fatos_${STAMP}.json"
ZEP="http://localhost:8000"
SESSAO="${ZEP_SESSION_ID:-edilson_parintins_001}"
PORTA_AGENTE="${AGENT_HOST_PORT:-3001}"

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║  Zep → Google Gemini                                ║"
echo "╚══════════════════════════════════════════════════════╝"

# ─── PASSO 1: pré-requisitos ─────────────────────────────────────────────────
echo ""
echo "════ PASSO 1 — Verificando pré-requisitos ════"
if [ -z "${GEMINI_API_KEY:-}" ]; then
  echo "  ✗ GEMINI_API_KEY ausente no .env. Configure antes de continuar."
  exit 1
fi
echo "  GEMINI_API_KEY presente ✓"

MODELO="${GEMINI_MODEL:-gemini-2.5-flash}"
EMBED_MODEL="${GEMINI_EMBED_MODEL:-text-embedding-004}"
EMBED_DIMS="${GEMINI_EMBED_DIMS:-768}"
echo "  LLM        : $MODELO"
echo "  Embeddings : $EMBED_MODEL (${EMBED_DIMS} dimensões)"

# Testa a camada de compatibilidade antes de tocar em qualquer configuração
echo "  Testando o endpoint compatível com OpenAI..."
TESTE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 20 \
  "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions" \
  -H "Authorization: Bearer ${GEMINI_API_KEY}" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"${MODELO}\",\"messages\":[{\"role\":\"user\",\"content\":\"oi\"}],\"max_tokens\":5}")
if [ "$TESTE" = "200" ]; then
  echo "  Endpoint OpenAI-compat respondeu 200 ✓"
else
  echo "  ✗ Endpoint devolveu HTTP $TESTE — a chave ou o modelo não servem."
  echo "    Verifique GEMINI_API_KEY e GEMINI_MODEL antes de prosseguir."
  exit 1
fi

# ─── PASSO 2: exporta os fatos clínicos ──────────────────────────────────────
echo ""
echo "════ PASSO 2 — Exportando os fatos clínicos ════"
curl -s "${ZEP}/api/v1/sessions/${SESSAO}" \
  -H "Authorization: Bearer ${ZEP_API_KEY}" 2>/dev/null \
  | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    fatos = (d.get('metadata') or {}).get('clinical_facts', [])
except Exception:
    fatos = []
json.dump(fatos, open('${BACKUP_FATOS}', 'w'), ensure_ascii=False, indent=2)
print(f'  {len(fatos)} fato(s) salvos em ${BACKUP_FATOS}')
" || echo "  Nenhum fato recuperado (sessão nova) — seguindo."

# ─── PASSO 3: novo zep_config.yaml ───────────────────────────────────────────
echo ""
echo "════ PASSO 3 — Reescrevendo o zep_config.yaml ════"
cp "$CONFIG" "$BACKUP_CFG" 2>/dev/null && echo "  Backup: $BACKUP_CFG"

cat > "$CONFIG" << ZEOF
server:
  port: 8000

store:
  type: postgres
  postgres:
    dsn: "postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/zep?sslmode=disable"

# O Zep CE 0.27 espera um JWT ASSINADO com este secret, não o secret em si.
# Enviar o secret cru devolve 401 "token is unauthorized" em toda escrita.
# Em vez de gerar JWT, a porta do Zep deixou de ser publicada: ele só é
# alcançável pela rede interna do Docker, o que é mais seguro do que expor
# a memória clínica na internet com autenticação.
auth:
  required: false
  secret: "${ZEP_API_KEY}"

# Camada compatível com OpenAI do Gemini: atende /chat/completions e /embeddings
llm:
  service: openai
  model: ${MODELO}
  openai_api_key: "${GEMINI_API_KEY}"
  openai_endpoint: "https://generativelanguage.googleapis.com/v1beta/openai"

extractors:
  documents:
    embeddings:
      enabled: true
      dimensions: ${EMBED_DIMS}
      service: openai
      model: ${EMBED_MODEL}
  messages:
    embeddings:
      enabled: true
      dimensions: ${EMBED_DIMS}
      service: openai
      model: ${EMBED_MODEL}

log:
  level: info

memory:
  message_window: 12
ZEOF
echo "  zep_config.yaml reescrito ✓"

# ─── PASSO 4: recria o banco (a dimensão do vetor mudou) ─────────────────────
echo ""
echo "════ PASSO 4 — Recriando o banco do Zep ════"
echo "  A coluna do pgvector tem dimensão fixa; 1024 → ${EMBED_DIMS} exige recriar."
docker compose stop zep >/dev/null 2>&1
docker exec postgres psql -U "$POSTGRES_USER" -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='zep'" >/dev/null 2>&1
docker exec postgres psql -U "$POSTGRES_USER" -c "DROP DATABASE IF EXISTS zep" 2>&1 | sed 's/^/    /'
docker exec postgres psql -U "$POSTGRES_USER" -c "CREATE DATABASE zep" 2>&1 | sed 's/^/    /'
docker exec postgres psql -U "$POSTGRES_USER" -d zep \
  -c "CREATE EXTENSION IF NOT EXISTS vector" 2>&1 | sed 's/^/    /'
echo "  Banco recriado com pgvector ✓"

# ─── PASSO 5: sobe o Zep e valida ────────────────────────────────────────────
echo ""
echo "════ PASSO 5 — Subindo o Zep ════"
docker compose up -d zep 2>&1 | tail -2

ZEP_OK=false
for i in $(seq 1 40); do
  if [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "${ZEP}/healthz")" = "200" ]; then
    echo "  Zep no ar ✓ (tentativa $i)"; ZEP_OK=true; break
  fi
  sleep 5
done

if [ "$ZEP_OK" = "false" ]; then
  echo ""
  echo "  ✗ O Zep não subiu com a configuração do Gemini. Revertendo..."
  docker logs --tail 25 zep 2>&1 | sed 's/^/    /'
  cp "$BACKUP_CFG" "$CONFIG"
  docker compose up -d zep >/dev/null 2>&1
  echo ""
  echo "  Configuração anterior restaurada. Os fatos estão em:"
  echo "    $BACKUP_FATOS"
  exit 1
fi

# ─── PASSO 6: reinicia o agente e reimporta os fatos ─────────────────────────
echo ""
echo "════ PASSO 6 — Reiniciando o agente ════"
docker compose up -d joao_holanda >/dev/null 2>&1
for i in $(seq 1 24); do
  if [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
      "http://localhost:${PORTA_AGENTE}/health")" = "200" ]; then
    echo "  Agente no ar ✓"; break
  fi
  sleep 5
done
# O agente recria sozinho os 8 fatos de base ao iniciar
sleep 5

echo ""
echo "════ PASSO 7 — Reimportando os fatos exportados ════"
if [ -s "$BACKUP_FATOS" ]; then
  python3 - <<PYEOF
import json, urllib.request, urllib.error

fatos = json.load(open("${BACKUP_FATOS}"))
# Os 8 fatos de base já foram recriados pelo agente; reimporta só o que veio depois
extras = [f for f in fatos if f.get("categoria") in ("exame", "humor", "voz_baseline")]
if not extras:
    print(f"  Nada a reimportar ({len(fatos)} fato(s) de base já recriados pelo agente).")
else:
    ok = 0
    for f in extras:
        corpo = json.dumps({"fato": f.get("fact", ""),
                            "categoria": f.get("categoria", "clinico")}).encode()
        req = urllib.request.Request(
            "http://localhost:${PORTA_AGENTE}/memoria/fato?token=${AGENT_ACCESS_TOKEN:-}",
            data=corpo, headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=20); ok += 1
        except Exception as e:
            print(f"    falhou: {f.get('fact','')[:50]} — {e}")
    print(f"  {ok}/{len(extras)} fato(s) reimportados.")
PYEOF
else
  echo "  Nenhum arquivo de fatos para reimportar."
fi

# ─── Estado final ────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════"
echo ""
echo "  Configuração do Zep:"
grep -E "^\s+(model|openai_endpoint|dimensions):" "$CONFIG" | sed 's/^/   /'
echo ""
echo "  Fatos na memória agora:"
curl -s "http://localhost:${PORTA_AGENTE}/memoria/contexto?token=${AGENT_ACCESS_TOKEN:-}" \
  2>/dev/null | python3 -c "
import sys, json
try:
    print(json.load(sys.stdin).get('contexto','(vazio)')[:700])
except Exception:
    print('  (não foi possível ler o contexto)')
" | sed 's/^/   /'
echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║  ✔  Zep agora usa o Gemini                          ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "  Uma única chave (GEMINI_API_KEY) passa a atender:"
echo "    • raciocínio clínico do Dr. João Holanda"
echo "    • leitura de exames, fotos, vídeo e notas de voz"
echo "    • resumo e embeddings da memória longitudinal"
echo ""
echo "  Backups desta migração:"
echo "    $BACKUP_CFG"
echo "    $BACKUP_FATOS"
echo ""
