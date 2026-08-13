"""
zep_memory.py — Interface com o Zep: segundo cérebro do Dr. João Holanda
Armazena memória longitudinal, conhecimento clínico e evolução do Sr. Edilson.

IMPORTANTE — Zep Community Edition v0.27.x (imagem ghcr.io/getzep/zep:latest):
Esta versão é DIFERENTE do Zep Cloud e do Zep CE v1.x (baseado em Graphiti).
Diferenças que já causaram bugs silenciosos neste projeto:
  • Prefixo da API é /api/v1, não /api/v2 (chamadas a /api/v2 dão 404).
  • Autenticação é "Authorization: Bearer <secret>", não "Api-Key <secret>".
  • Não existe conceito de "facts" (isso é do Zep Cloud). Fatos clínicos são
    guardados aqui na metadata da sessão (PATCH /sessions/{id}), que
    sobrevive independente da janela de mensagens.
  • POST .../memory responde "OK" em texto puro, não JSON.
  • GET .../memory devolve 404 quando a sessão está vazia/nova — não é erro,
    é "ainda não há memória".

Uso:
    from integrations import zep_memory as mem
    await mem.save_interaction("Estou com dor nas costas hoje", "Dr. João resposta...")
    contexto = await mem.get_context()
"""

import os
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# ─── Configuração ────────────────────────────────────────────────────────────
ZEP_API_URL     = os.getenv("ZEP_API_URL", "http://localhost:8000").rstrip("/")
ZEP_API_KEY     = os.getenv("ZEP_API_KEY", "")
ZEP_SESSION_ID  = os.getenv("ZEP_SESSION_ID", "edilson_parintins_001")
ZEP_USER_ID     = "edilson_76_parintins"

API = f"{ZEP_API_URL}/api/v1"


# ─── Cliente base ─────────────────────────────────────────────────────────────
def _headers() -> dict:
    """Cabeçalhos HTTP para autenticação no Zep CE 0.27.x (esquema Bearer)."""
    return {
        "Authorization": f"Bearer {ZEP_API_KEY}",
        "Content-Type": "application/json",
    }


# ─── Inicialização do usuário e sessão ───────────────────────────────────────
async def ensure_user_and_session() -> bool:
    """
    Garante que o usuário e a sessão do Sr. Edilson existam no Zep.
    POST /sessions/{id}/memory também cria a sessão automaticamente, então
    isto é uma garantia extra — não é estritamente obrigatório antes de salvar.
    """
    async with httpx.AsyncClient(timeout=15) as client:
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
        resp_user = await client.post(f"{API}/user", headers=_headers(), json=user_payload)
        # 200/201 = criado; 400/409 = já existe (varia por build) — ambos OK.
        # 401/403 NÃO são OK: davam "sessão criada" enquanto toda escrita
        # falhava, escondendo o problema real de autenticação.
        user_ok = resp_user.status_code not in (401, 403) and resp_user.status_code < 500
        if not user_ok:
            logger.warning("Zep ensure_user falhou: HTTP %s — %s",
                           resp_user.status_code, resp_user.text[:200])

        session_payload = {
            "session_id": ZEP_SESSION_ID,
            "user_id": ZEP_USER_ID,
            "metadata": {
                "canal": "WhatsApp",
                "agente": "Dr. João Holanda",
                "projeto": "Ecossistema IA Saúde Edilson",
            },
        }
        resp_session = await client.post(f"{API}/sessions", headers=_headers(), json=session_payload)
        session_ok = (resp_session.status_code not in (401, 403)
                      and resp_session.status_code < 500)
        if not session_ok:
            logger.warning("Zep ensure_session falhou: HTTP %s — %s",
                           resp_session.status_code, resp_session.text[:200])

        return user_ok and session_ok


# ─── Salvar interação ────────────────────────────────────────────────────────
async def save_interaction(
    patient_message: str,
    agent_response: str,
    metadata: Optional[dict] = None,
) -> bool:
    """
    Salva uma troca de mensagens entre o Sr. Edilson e o Dr. João Holanda.
    O Zep extrai automaticamente resumo e embeddings de forma assíncrona
    (exige um LLM configurado em zep_config.yaml → llm.*).
    """
    payload = {
        "messages": [
            {"role": "user", "content": patient_message, "metadata": metadata or {}},
            {"role": "assistant", "content": agent_response,
             "metadata": {"timestamp": datetime.now(timezone.utc).isoformat(),
                          "nome_agente": "Dr. João Holanda"}},
        ]
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{API}/sessions/{ZEP_SESSION_ID}/memory",
            headers=_headers(),
            json=payload,
        )
        # Sucesso é texto puro "OK", não JSON — não tentar resp.json() aqui.
        if resp.status_code != 200:
            logger.warning("Zep save_interaction falhou: HTTP %s — %s",
                           resp.status_code, resp.text[:200])
            return False
        return True


# ─── Buscar contexto de memória ───────────────────────────────────────────────
async def get_context(last_n: int = 10) -> str:
    """
    Retorna o contexto resumido da memória do Sr. Edilson para enriquecer
    o prompt do Dr. João Holanda: fatos clínicos datados (metadata da
    sessão) + última conversa com cada pessoa + resumo do Zep (quando já
    gerado) + mensagens recentes, com quem falou cada uma.
    """
    parts: list[str] = []

    # 1. Fatos clínicos estruturados, guardados na metadata da sessão — COM
    #    DATA. Sem data, o agente não distingue um fato de hoje de um fato
    #    de duas semanas atrás, e acaba repetindo como se fosse novidade.
    facts = await get_facts()
    if facts:
        facts_ordenados = sorted(facts, key=lambda f: f.get("registrado_em", ""))
        facts_text = "\n".join(
            f"• [{f.get('categoria', '?')}] {f.get('fact', '')} "
            f"(registrado em {(f.get('registrado_em') or '?')[:10]})"
            for f in facts_ordenados
        )
        parts.append(f"[FATOS CONHECIDOS SOBRE O SR. EDILSON]\n{facts_text}")

    # 2. Última conversa com CADA PESSOA. A sessão do Zep é única para a
    #    família inteira — sem isto, "conversou com o Sr. Edilson hoje?"
    #    cai numa janela de mensagens recentes que pode estar cheia de
    #    mensagens de outra pessoa, e o agente responde errado por falta
    #    de sinal, não por falta de memória de verdade. Guarda o dia
    #    INTEIRO de trocas por pessoa (não só a última), para responder com
    #    completude "o que conversamos hoje" — inclusive papo sem fato
    #    clínico extraível, que os clinical_facts não guardam de propósito.
    interacoes = await get_ultimas_interacoes()
    if interacoes:
        hoje = datetime.now(timezone.utc).date().isoformat()
        linhas: list[str] = []
        for d in sorted(interacoes.values(),
                        key=lambda d: d.get("ultima_em", ""), reverse=True):
            nome, papel = d.get("nome", "?"), d.get("papel", "?")
            trocas = d.get("trocas_hoje", [])
            if d.get("dia") == hoje and trocas:
                itens = "\n".join(
                    f"    - {(t.get('em') or '?')[11:16]}: \"{t.get('resumo', '')}\""
                    for t in trocas
                )
                linhas.append(f"• {nome} ({papel}) — {len(trocas)} "
                              f"troca(s) hoje:\n{itens}")
            else:
                quando = (d.get("ultima_em") or "?")[:16].replace("T", " ")
                linhas.append(f"• {nome} ({papel}): sem conversa hoje — "
                              f"última foi em {quando}")
        parts.append("[CONVERSAS DE HOJE POR PESSOA]\n" + "\n".join(linhas))

    # 3. Resumo + mensagens recentes, com quem falou cada uma
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{API}/sessions/{ZEP_SESSION_ID}/memory",
            headers=_headers(),
            params={"lastn": last_n},
        )
        if resp.status_code == 404:
            # Sessão nova ou ainda sem mensagens — não é erro.
            pass
        elif resp.status_code != 200:
            logger.warning("Zep get_context falhou: HTTP %s — %s",
                           resp.status_code, resp.text[:200])
        else:
            data = resp.json()
            summary  = (data.get("summary") or {}).get("content", "")
            messages = data.get("messages", [])

            if summary:
                parts.append(f"[RESUMO DA MEMÓRIA]\n{summary}")

            if messages:
                recent = messages[-4:]  # últimas ~2 trocas
                # A sessão é compartilhada por toda a família — sem o nome,
                # "user"/"assistant" não diz QUEM disse o quê.
                def _quem(m: dict) -> str:
                    meta = m.get("metadata") or {}
                    return (meta.get("quem") or meta.get("nome_agente")
                           or m.get("role", "?"))
                msgs_text = "\n".join(
                    f"{_quem(m)}: {m.get('content', '')}" for m in recent
                )
                parts.append(f"[MENSAGENS RECENTES — podem ser de qualquer "
                             f"pessoa da família]\n{msgs_text}")

    return "\n\n".join(parts) if parts else "Primeira interação com o Sr. Edilson."


# ─── Conversas de hoje por pessoa ──────────────────────────────────────────────
# Guardado à parte dos clinical_facts porque isto não é fato clínico — é um
# log leve de "quando" e "com quem", que o item 2 de get_context() usa para
# responder com completude perguntas como "o que conversamos hoje?" sem
# depender da janela de mensagens recentes (compartilhada por toda a
# família, então facilmente sobrescrita por quem está falando agora) nem
# dos clinical_facts (que ignoram cumprimento/papo sem fato novo de propósito).
#
# trocas_hoje acumula TODAS as trocas do dia corrente — zera sozinho quando
# o campo "dia" muda, então não vira um histórico permanente crescendo sem
# fim. Limitada a 20 por pessoa/dia como teto de segurança (uma pessoa muito
# ativa não deve inflar o prompt de toda inferência do dia).
MAX_TROCAS_POR_DIA = 20


async def registrar_interacao(chave: str, nome: str, papel: str, resumo: str) -> bool:
    """Acrescenta uma troca de hoje ao histórico do dia desta pessoa."""
    meta = await _get_session_metadata()
    interacoes = meta.get("ultimas_interacoes", {})
    agora = datetime.now(timezone.utc)
    hoje = agora.date().isoformat()

    registro = interacoes.get(chave, {})
    trocas_hoje = registro.get("trocas_hoje", []) if registro.get("dia") == hoje else []
    trocas_hoje.append({"em": agora.isoformat(), "resumo": (resumo or "").strip()[:140]})
    trocas_hoje = trocas_hoje[-MAX_TROCAS_POR_DIA:]

    interacoes[chave] = {
        "nome": nome,
        "papel": papel,
        "ultima_em": agora.isoformat(),
        "dia": hoje,
        "trocas_hoje": trocas_hoje,
    }
    meta["ultimas_interacoes"] = interacoes
    ok, _status, _corpo = await _patch_session_metadata(meta)
    return ok


async def get_ultimas_interacoes() -> dict:
    """Última conversa registrada com cada pessoa, chaveada por chat_id/número."""
    meta = await _get_session_metadata()
    return meta.get("ultimas_interacoes", {})


async def get_trocas_do_dia(dia: str) -> dict:
    """
    Trocas de hoje por pessoa, só para o dia informado — descarta quem já
    virou o dia (campo "dia" desatualizado) sem apagar nada, porque
    registrar_interacao() só zera o "trocas_hoje" de alguém na PRÓXIMA vez
    que essa pessoa escrever, não no relógio. Usado pela consolidação de
    fim de dia para ler exatamente o que aconteceu hoje, antes de virar.
    """
    interacoes = await get_ultimas_interacoes()
    return {chave: d for chave, d in interacoes.items() if d.get("dia") == dia}


# ─── Fatos clínicos (armazenados na metadata da sessão) ───────────────────────
# O Zep CE 0.27.x não tem um endpoint de "facts" (isso é do Zep Cloud).
# Guardamos aqui na metadata da sessão via PATCH, que sobrevive independente
# da janela de mensagens/resumo. Leitura-modificação-escrita: seguro neste
# projeto porque há um único processo gravando (o agente do Sr. Edilson).
async def _get_session_metadata() -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{API}/sessions/{ZEP_SESSION_ID}", headers=_headers())
        if resp.status_code != 200:
            logger.warning("Zep leitura da sessão falhou: HTTP %s — %s",
                           resp.status_code, resp.text[:200])
            return {}
        return resp.json().get("metadata") or {}


async def _patch_session_metadata(meta: dict) -> tuple[bool, int, str]:
    """
    Grava a metadata da sessão. Devolve (sucesso, status, corpo) para que o
    diagnóstico possa mostrar o motivo exato de uma falha, em vez de só um
    booleano — foi o que travou a investigação da memória.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.patch(
            f"{API}/sessions/{ZEP_SESSION_ID}",
            headers=_headers(),
            json={"metadata": meta},
        )
        ok = resp.status_code in (200, 201)
        if not ok:
            logger.warning("Zep gravação da sessão falhou: HTTP %s — %s",
                           resp.status_code, resp.text[:300])
        return ok, resp.status_code, resp.text[:300]


async def add_clinical_fact(fact: str, category: str = "clinico") -> bool:
    """
    Adiciona um fato clínico estruturado à memória do Sr. Edilson.
    Ex: add_clinical_fact("PSA medido em 0.12 em maio/2026", "exame")
    """
    await ensure_user_and_session()
    meta = await _get_session_metadata()
    facts = meta.get("clinical_facts", [])
    facts.append({
        "fact": fact,
        "categoria": category,
        "registrado_em": datetime.now(timezone.utc).isoformat(),
        "fonte": "Dr. João Holanda",
    })
    meta["clinical_facts"] = facts

    ok, _status, _corpo = await _patch_session_metadata(meta)
    return ok


async def remove_clinical_fact(category: str, indice: int) -> Optional[str]:
    """
    Remove o fato de número `indice` (base 1) dentro de uma categoria,
    ordenado por data de registro. Devolve o texto removido, ou None.
    """
    meta = await _get_session_metadata()
    fatos = meta.get("clinical_facts", [])
    da_categoria = sorted((f for f in fatos if f.get("categoria") == category),
                          key=lambda f: f.get("registrado_em", ""))

    if not 1 <= indice <= len(da_categoria):
        return None

    alvo = da_categoria[indice - 1]
    meta["clinical_facts"] = [f for f in fatos if f is not alvo]

    ok, _status, _corpo = await _patch_session_metadata(meta)
    if not ok:
        return None

    logger.info("Fato removido [%s]: %s", category, alvo.get("fact", "")[:70])
    return alvo.get("fact", "")


async def get_facts(category: Optional[str] = None) -> list[dict]:
    """Recupera fatos conhecidos sobre o Sr. Edilson, opcionalmente filtrados."""
    meta = await _get_session_metadata()
    facts = meta.get("clinical_facts", [])
    if category:
        facts = [f for f in facts if f.get("categoria") == category]
    return facts


# ─── Busca semântica na memória ───────────────────────────────────────────────
async def search_memory(query: str, limit: int = 5) -> list[dict]:
    """
    Busca semântica no histórico de mensagens da sessão.
    Ex: search_memory("histórico de PSA") → mensagens relevantes por embedding.
    """
    payload = {"text": query, "search_scope": "messages", "search_type": "mmr", "mmr_lambda": 0.5}

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{API}/sessions/{ZEP_SESSION_ID}/search",
            headers=_headers(),
            params={"limit": limit},
            json=payload,
        )
        if resp.status_code != 200:
            logger.warning("Zep search_memory falhou: HTTP %s — %s",
                           resp.status_code, resp.text[:200])
            return []
        return resp.json() or []


# ─── Gerar resumo diário ──────────────────────────────────────────────────────
async def generate_daily_summary(dia: Optional[str] = None) -> dict:
    """
    Reúne os fatos clínicos registrados NUM DIA ESPECÍFICO (padrão: hoje,
    UTC) para montar o resumo diário enviado à família às 20h.

    Filtra por data de propósito: sem isso, o resumo repetiria o histórico
    inteiro todos os dias — o objetivo aqui é "o que aconteceu hoje", não
    "tudo que já se sabe sobre o Sr. Edilson" (isso já é o papel de
    get_context()/get_facts() sem filtro, usado no prompt de cada conversa).
    """
    dia = dia or datetime.now(timezone.utc).date().isoformat()
    todos = await get_facts()
    # registrado_em é ISO 8601 UTC (ex: "2026-08-12T23:10:00+00:00") — os 10
    # primeiros caracteres são a data.
    de_hoje = [f for f in todos if (f.get("registrado_em") or "")[:10] == dia]

    por_categoria: dict[str, list[str]] = {}
    for f in de_hoje:
        por_categoria.setdefault(f.get("categoria", "outro"), []).append(f.get("fact", ""))

    return {
        "data": dia,
        "paciente": "Sr. Edilson",
        "cidade": "Parintins, AM",
        "fatos_por_categoria": por_categoria,
        "total_fatos": len(de_hoje),
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
    Idempotente: verifica na metadata da sessão se os fatos já foram gravados
    antes de reescrever — evita duplicar a cada reinício do container.
    """
    await ensure_user_and_session()
    existing = await get_facts()
    if len(existing) >= len(HEALTH_SNAPSHOT):
        logger.info("Conhecimento do paciente já inicializado no Zep (%d fatos).", len(existing))
        return

    ok_count = 0
    for fact, category in HEALTH_SNAPSHOT:
        if await add_clinical_fact(fact, category):
            ok_count += 1
            logger.info("Fato adicionado: [%s] %s", category, fact[:60])
        else:
            logger.warning("Falha ao adicionar fato: [%s] %s", category, fact[:60])

    if ok_count == len(HEALTH_SNAPSHOT):
        logger.info("Conhecimento inicial do Sr. Edilson carregado no Zep ✓ (%d/%d)",
                    ok_count, len(HEALTH_SNAPSHOT))
    else:
        logger.warning("Conhecimento parcialmente carregado no Zep: %d/%d fatos.",
                       ok_count, len(HEALTH_SNAPSHOT))


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
    saved = await save_interaction(
        patient_message="Bom dia doutor, tomei todos os remédios hoje.",
        agent_response="Bom dia, Sr. Edilson! Fico feliz em saber que tomou todos os remédios. "
                       "Como está se sentindo hoje? Dormiu bem?",
        metadata={"hora": "08:00", "tipo": "rotina"},
    )
    print(f"Interação salva: {saved}")

    print("Buscando contexto...")
    ctx = await get_context()
    print(f"Contexto:\n{ctx[:500]}")

    print("\n✓ Zep funcionando corretamente!" if saved else "\n✗ Falha ao salvar — revise ZEP_API_KEY/URL.")


if __name__ == "__main__":
    import asyncio
    asyncio.run(_test())
