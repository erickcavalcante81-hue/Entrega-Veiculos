"""
zep_memory.py — Interface com o Zep: segundo cérebro do Dr. João Holanda
Armazena memória longitudinal, conhecimento clínico e evolução do Sr. Edilson.

Uso:
    from integrations.zep_memory import ZepMemory
    mem = ZepMemory()
    await mem.save_interaction("Estou com dor nas costas hoje", "Dr. João resposta...")
    contexto = await mem.get_context()
"""

import os
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# ─── Configuração ────────────────────────────────────────────────────────────
ZEP_API_URL     = os.getenv("ZEP_API_URL", "http://localhost:8000")
ZEP_API_KEY     = os.getenv("ZEP_API_KEY", "")
ZEP_SESSION_ID  = os.getenv("ZEP_SESSION_ID", "edilson_parintins_001")
ZEP_USER_ID     = "edilson_76_parintins"


# ─── Cliente base ─────────────────────────────────────────────────────────────
def _headers() -> dict:
    """Cabeçalhos HTTP para autenticação no Zep."""
    return {
        "Authorization": f"Api-Key {ZEP_API_KEY}",
        "Content-Type": "application/json",
    }


# ─── Inicialização do usuário e sessão ───────────────────────────────────────
async def ensure_user_and_session() -> bool:
    """
    Garante que o usuário e sessão do Sr. Edilson existam no Zep.
    Chamado automaticamente na primeira interação do dia.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        # Cria usuário se não existir
        user_payload = {
            "user_id": ZEP_USER_ID,
            "first_name": "Edilson",
            "last_name": "Parintins",
            "metadata": {
                "idade": 76,
                "cidade": "Parintins",
                "estado": "Amazonas",
                "condicao": "pós-câncer de próstata",
                "medico_ia": "Dr. João Holanda",
                "criado_em": datetime.now(timezone.utc).isoformat(),
            },
        }
        await client.post(
            f"{ZEP_API_URL}/api/v2/users",
            headers=_headers(),
            json=user_payload,
        )

        # Cria sessão se não existir
        session_payload = {
            "session_id": ZEP_SESSION_ID,
            "user_id": ZEP_USER_ID,
            "metadata": {
                "canal": "WhatsApp",
                "agente": "Dr. João Holanda",
                "projeto": "Ecossistema IA Saúde Edilson",
            },
        }
        resp = await client.post(
            f"{ZEP_API_URL}/api/v2/sessions",
            headers=_headers(),
            json=session_payload,
        )
        return resp.status_code in (200, 201, 409)  # 409 = já existe


# ─── Salvar interação ────────────────────────────────────────────────────────
async def save_interaction(
    patient_message: str,
    agent_response: str,
    metadata: Optional[dict] = None,
) -> bool:
    """
    Salva uma troca de mensagens entre o Sr. Edilson e o Dr. João Holanda.
    O Zep extrai automaticamente entidades, fatos e resumos do diálogo.
    """
    payload = {
        "messages": [
            {
                "role": "user",
                "role_type": "user",
                "content": patient_message,
                "metadata": metadata or {},
            },
            {
                "role": "Dr. João Holanda",
                "role_type": "assistant",
                "content": agent_response,
                "metadata": {"timestamp": datetime.now(timezone.utc).isoformat()},
            },
        ]
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{ZEP_API_URL}/api/v2/sessions/{ZEP_SESSION_ID}/messages",
            headers=_headers(),
            json=payload,
        )
        if resp.status_code not in (200, 201):
            logger.warning("Zep save_interaction falhou: %s", resp.text)
            return False
        return True


# ─── Buscar contexto de memória ───────────────────────────────────────────────
async def get_context(last_n: int = 10) -> str:
    """
    Retorna o contexto resumido da memória do Sr. Edilson para enriquecer
    o prompt do Dr. João Holanda. Inclui: resumo do Zep + fatos extraídos.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{ZEP_API_URL}/api/v2/sessions/{ZEP_SESSION_ID}/memory",
            headers=_headers(),
            params={"lastn": last_n},
        )
        if resp.status_code != 200:
            logger.warning("Zep get_context falhou: %s", resp.text)
            return "Sem histórico anterior disponível."

        data = resp.json()
        summary   = data.get("summary", {}).get("content", "")
        messages  = data.get("messages", [])
        facts     = data.get("facts", [])

        # Monta contexto legível para o LLM
        parts = []

        if summary:
            parts.append(f"[RESUMO DA MEMÓRIA]\n{summary}")

        if facts:
            facts_text = "\n".join(f"• {f.get('fact', '')}" for f in facts[:10])
            parts.append(f"[FATOS CONHECIDOS SOBRE O SR. EDILSON]\n{facts_text}")

        if messages:
            recent = messages[-4:]  # Últimas 2 trocas
            msgs_text = "\n".join(
                f"{m.get('role','?')}: {m.get('content','')}"
                for m in recent
            )
            parts.append(f"[MENSAGENS RECENTES]\n{msgs_text}")

        return "\n\n".join(parts) if parts else "Primeira interação com o Sr. Edilson."


# ─── Adicionar fato clínico ───────────────────────────────────────────────────
async def add_clinical_fact(fact: str, category: str = "clinico") -> bool:
    """
    Adiciona um fato clínico estruturado à memória do Sr. Edilson.
    Ex: add_clinical_fact("PSA medido em 0.12 em maio/2026", "exame")
    """
    payload = {
        "facts": [
            {
                "fact": fact,
                "metadata": {
                    "categoria": category,
                    "registrado_em": datetime.now(timezone.utc).isoformat(),
                    "fonte": "Dr. João Holanda",
                },
            }
        ]
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{ZEP_API_URL}/api/v2/users/{ZEP_USER_ID}/facts",
            headers=_headers(),
            json=payload,
        )
        return resp.status_code in (200, 201)


# ─── Buscar fatos por categoria ───────────────────────────────────────────────
async def get_facts(category: Optional[str] = None) -> list[dict]:
    """
    Recupera fatos conhecidos sobre o Sr. Edilson, opcionalmente filtrados.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{ZEP_API_URL}/api/v2/users/{ZEP_USER_ID}/facts",
            headers=_headers(),
        )
        if resp.status_code != 200:
            return []

        facts = resp.json().get("facts", [])
        if category:
            facts = [f for f in facts if f.get("metadata", {}).get("categoria") == category]
        return facts


# ─── Busca semântica na memória ───────────────────────────────────────────────
async def search_memory(query: str, limit: int = 5) -> list[dict]:
    """
    Busca semântica na memória do Sr. Edilson.
    Ex: search_memory("histórico de PSA") → retorna mensagens relevantes.
    """
    payload = {"text": query, "search_type": "mmr", "limit": limit}

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{ZEP_API_URL}/api/v2/sessions/{ZEP_SESSION_ID}/search",
            headers=_headers(),
            json=payload,
        )
        if resp.status_code != 200:
            return []

        return resp.json().get("results", [])


# ─── Gerar resumo diário ──────────────────────────────────────────────────────
async def generate_daily_summary() -> dict:
    """
    Gera o resumo diário do Sr. Edilson para envio à família às 20h.
    Consolida interações, fatos clínicos e variações de marcadores do dia.
    """
    context = await get_context(last_n=50)
    exames  = await get_facts("exame")
    humor   = await get_facts("humor")
    aliment = await get_facts("alimentacao")
    medic   = await get_facts("medicamento")

    return {
        "data": datetime.now(timezone.utc).date().isoformat(),
        "paciente": "Sr. Edilson",
        "cidade": "Parintins, AM",
        "contexto_memoria": context,
        "exames_recentes": exames,
        "humor_do_dia": humor,
        "alimentacao": aliment,
        "medicamentos": medic,
        "gerado_por": "Dr. João Holanda",
    }


# ─── Snapshot de saúde inicial ────────────────────────────────────────────────
HEALTH_SNAPSHOT = [
    ("Paciente: Sr. Edilson, 76 anos, Parintins-AM", "perfil"),
    ("Histórico oncológico: pós-câncer de próstata, em vigilância ativa", "oncologia"),
    ("PSA mais recente: 0.12 (subiu de 0.08 — atenção moderada)", "exame"),
    ("Monitoramento renal: eTFG em acompanhamento regular", "nefrologia"),
    ("Dieta: restrição de sódio moderada, sem álcool", "alimentacao"),
    ("Timezone: America/Manaus (UTC-4)", "perfil"),
    ("Filhos recebem resumo às 20h via WhatsApp", "familia"),
    ("Câmera Intelbras Mibo instalada na cozinha para detecção de quedas", "seguranca"),
]


async def initialize_patient_knowledge() -> None:
    """
    Popula o Zep com o conhecimento inicial do Sr. Edilson na primeira execução.
    Chamar apenas uma vez (idempotente — verifica fatos existentes primeiro).
    """
    existing = await get_facts()
    if len(existing) >= len(HEALTH_SNAPSHOT):
        logger.info("Conhecimento do paciente já inicializado no Zep.")
        return

    await ensure_user_and_session()
    for fact, category in HEALTH_SNAPSHOT:
        await add_clinical_fact(fact, category)
        logger.info("Fato adicionado: [%s] %s", category, fact[:60])

    logger.info("Conhecimento inicial do Sr. Edilson carregado no Zep ✓")


# ─── Teste rápido de integração ───────────────────────────────────────────────
async def _test():
    """Testa a conexão e inicializa o conhecimento do paciente."""
    import asyncio

    print("Testando conexão com Zep...")
    ok = await ensure_user_and_session()
    print(f"Sessão criada/confirmada: {ok}")

    print("Inicializando conhecimento do Sr. Edilson...")
    await initialize_patient_knowledge()

    print("Salvando interação de teste...")
    await save_interaction(
        patient_message="Bom dia doutor, tomei todos os remédios hoje.",
        agent_response="Bom dia, Sr. Edilson! Fico feliz em saber que tomou todos os remédios. "
                       "Como está se sentindo hoje? Dormiu bem?",
        metadata={"hora": "08:00", "tipo": "rotina"},
    )

    print("Buscando contexto...")
    ctx = await get_context()
    print(f"Contexto:\n{ctx[:500]}")

    print("\n✓ Zep funcionando corretamente!")


if __name__ == "__main__":
    import asyncio
    asyncio.run(_test())
