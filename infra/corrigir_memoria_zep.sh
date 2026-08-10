#!/usr/bin/env bash
# corrigir_memoria_zep.sh — Corrige o 401 "token is unauthorized" nas gravações
#
# Diagnóstico que levou até aqui:
#   "escrita": {"ok": false, "http": 401,
#               "resposta_do_zep": "token is unauthorized"}
#
# O Zep CE 0.27 com auth.required=true espera um JWT ASSINADO com o secret,
# não o secret em si — que é o que o agente enviava. Toda leitura pública
# funcionava (/healthz) e toda escrita falhava.
#
# Em vez de gerar e renovar JWT, a porta 8000 deixa de ser publicada: o Zep
# passa a ser alcançável apenas pela rede interna do Docker. Isso resolve a
# autenticação e, de quebra, tira a memória clínica do Sr. Edilson da
# internet — onde ela nunca deveria ter estado.
#
# Uso: bash /root/automacao/corrigir_memoria_zep.sh
set -uo pipefail

cd /root/automacao
source .env

PORTA="${AGENT_HOST_PORT:-3001}"

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║  Correção da memória — Dr. João Holanda             ║"
echo "╚══════════════════════════════════════════════════════╝"

# ─── PASSO 1: baixa os arquivos corrigidos ───────────────────────────────────
echo ""
echo "════ PASSO 1 — Baixando correções ════"
B="https://raw.githubusercontent.com/erickcavalcante81-hue/Home-Cabin-USA/claude/multimodal-health-ai-system-dEf2q"
CB="$(date +%s)$$"
for f in agents/joao_holanda_service.py integrations/zep_memory.py; do
  curl -fsSL -H 'Cache-Control: no-cache' "$B/$f?cb=$CB" -o "$f" && echo "  $f"
done
curl -fsSL -H 'Cache-Control: no-cache' "$B/infra/docker-compose.yml?cb=$CB" \
  -o docker-compose.yml && echo "  docker-compose.yml"

# ─── PASSO 2: desliga a autenticação do Zep ──────────────────────────────────
echo ""
echo "════ PASSO 2 — Ajustando o zep_config.yaml ════"
cp zep_config.yaml "zep_config.yaml.bak_$(date +%Y%m%d_%H%M%S)" 2>/dev/null

python3 - <<'PYEOF'
import re
caminho = "/root/automacao/zep_config.yaml"
try:
    s = open(caminho).read()
except FileNotFoundError:
    print("  ✗ zep_config.yaml não encontrado."); raise SystemExit(1)

antes = s
s = re.sub(r"(auth:\s*\n\s*required:\s*)true", r"\1false", s)
if s == antes and "required: false" not in s:
    print("  ⚠ Não encontrei 'auth.required: true' — verifique o arquivo.")
else:
    open(caminho, "w").write(s)
    print("  auth.required = false ✓")
PYEOF

grep -A2 "^auth:" zep_config.yaml | sed 's/^/    /'

# ─── PASSO 3: confirma que a porta do Zep saiu do compose ────────────────────
echo ""
echo "════ PASSO 3 — Verificando exposição da porta ════"
if grep -qE '^\s+- "8000:8000"' docker-compose.yml; then
  echo "  ⚠ O compose ainda publica a 8000 (cache do CDN). Corrigindo..."
  python3 - <<'PYEOF'
s = open("/root/automacao/docker-compose.yml").read()
s = s.replace('    ports:\n      - "8000:8000"\n', '    expose:\n      - "8000"\n', 1)
open("/root/automacao/docker-compose.yml", "w").write(s)
print("  porta 8000 despublicada ✓")
PYEOF
else
  echo "  Porta 8000 não está publicada ✓ (memória fora da internet)"
fi

# ─── PASSO 4: recria Zep e agente ────────────────────────────────────────────
echo ""
echo "════ PASSO 4 — Recriando os containers ════"
docker compose up -d --force-recreate zep 2>&1 | tail -3

echo "  Aguardando o Zep..."
for i in $(seq 1 30); do
  if docker exec zep wget -qO- http://localhost:8000/healthz >/dev/null 2>&1; then
    echo "  Zep no ar ✓ (tentativa $i)"; break
  fi
  sleep 3
done

docker compose build joao_holanda 2>&1 | tail -3
docker compose up -d joao_holanda 2>&1 | tail -2
echo "  Aguardando o agente..."
sleep 15

# ─── PASSO 5: valida a gravação ──────────────────────────────────────────────
echo ""
echo "════ PASSO 5 — Testando a memória ════"
RESULTADO=$(curl -s "http://localhost:${PORTA}/memoria/diagnostico?token=${AGENT_ACCESS_TOKEN}")
echo "$RESULTADO" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print('  Não foi possível ler o diagnóstico:'); print(sys.stdin.read()[:300]); raise SystemExit(1)

esc, lei = d.get('escrita', {}), d.get('leitura', {})
print(f\"  escrita  : ok={esc.get('ok')} http={esc.get('http')}\")
if esc.get('resposta_do_zep'):
    print(f\"             {esc['resposta_do_zep'][:90]}\")
print(f\"  leitura  : ok={lei.get('ok')}\")
print(f\"  fatos    : {d.get('fatos_existentes', {}).get('total')}\")
print(f\"  contatos : {d.get('contatos_telegram', {}).get('mapeados')}\")
print()
if esc.get('ok') and lei.get('ok'):
    print('  ✔ MEMÓRIA FUNCIONANDO — grava e relê.')
else:
    print('  ✗ Ainda falhando. Envie esta saída para análise.')
"

echo ""
echo "  Se a memória estiver OK, o conhecimento inicial será recarregado"
echo "  no próximo start do agente. Para forçar agora:"
echo "    docker compose restart joao_holanda"
echo ""
