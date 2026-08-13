"""
joao_holanda_service.py — Serviço agente Dr. João Holanda Cavalcante
Microserviço FastAPI que recebe webhooks da Evolution API, processa com
Nvidia NIM + Zep e responde ao Sr. Edilson via WhatsApp com voz (ElevenLabs).

Porta padrão: 3000
"""

import asyncio
import base64
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

# ─── Configuração ─────────────────────────────────────────────────────────────
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("dr-joao-holanda")

EVOLUTION_API_URL  = os.getenv("EVOLUTION_API_URL", "http://localhost:8080")
EVOLUTION_API_KEY  = os.getenv("EVOLUTION_API_KEY", "")
WHATSAPP_INSTANCE  = os.getenv("WHATSAPP_INSTANCE_NAME", "edilson")
ZEP_API_URL        = os.getenv("ZEP_API_URL", "http://localhost:8000")
ZEP_API_KEY        = os.getenv("ZEP_API_KEY", "")
ZEP_SESSION_ID     = os.getenv("ZEP_SESSION_ID", "edilson_parintins_001")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID= os.getenv("ELEVENLABS_VOICE_ID", "")
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY", "")
VPS_IP             = os.getenv("VPS_IP", "localhost")

# ─── Nvidia NIM — motor único multimodal do ecossistema ───────────────────────
# Nemotron 3 Nano Omni aceita texto, imagem, vídeo e áudio numa mesma chamada,
# devolvendo texto. Substitui, sozinho: LLM de raciocínio, Whisper (transcrição
# de áudio do WhatsApp) e o modelo de visão (PDFs de exames e fotos de refeição).
# Arquitetura: Mamba-Transformer MoE 30B (3B ativos) + encoder de visão
# C-RADIOv4-H + encoder de áudio Parakeet-TDT. Janela de contexto: 256K tokens.
NVIDIA_NIM_API_KEY  = os.getenv("NVIDIA_NIM_API_KEY", "")
NVIDIA_NIM_BASE_URL = os.getenv("NVIDIA_NIM_BASE_URL", "https://integrate.api.nvidia.com/v1")
NIM_CHAT_MODEL      = os.getenv("NIM_CHAT_MODEL",
                                "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning")
# Orçamento de raciocínio interno (tokens). 0 desativa o modo reasoning.
NIM_REASONING_BUDGET = int(os.getenv("NIM_REASONING_BUDGET", "4096"))

# Número do Sr. Edilson (ou grupo familiar)
EDILSON_PHONE      = os.getenv("EDILSON_PHONE", "")      # ex: 5592999999999
FAMILY_GROUP_ID    = os.getenv("N8N_FAMILY_GROUP_WA_ID", "")

# ─── Canal Telegram ───────────────────────────────────────────────────────────
# API oficial e gratuita, sem risco de banimento — diferente do Baileys, que a
# Meta detecta e bane. Conversa por texto, voz, foto, vídeo e documento.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# IDs autorizados, separados por vírgula. Vazio = responde a qualquer um
# (apenas para o cadastro inicial; o log mostra o chat_id de quem escrever).
# Quem pode dar diretrizes de comportamento ao agente pelo Telegram. Trata-se
# de administração, não de conversa clínica: separado da lista de contatos
# autorizados de propósito.
TELEGRAM_ADMIN_IDS = {
    int(i) for i in os.getenv("TELEGRAM_ADMIN_IDS", "").replace(" ", "").split(",")
    if i.lstrip("-").isdigit()
}

# Quem recebe os alertas clínicos pelo Telegram. Papel distinto do de
# administrador: os filhos precisam ser avisados de um PSA alterado ou de uma
# queda sem por isso poderem alterar o comportamento do agente.
# Aceita também o ID de um grupo (negativo), para avisar todos de uma vez.
TELEGRAM_FAMILIA_IDS = {
    int(i) for i in os.getenv("TELEGRAM_FAMILIA_IDS", "").replace(" ", "").split(",")
    if i.lstrip("-").isdigit()
}

# Quem é cada chat_id do Telegram. Sem isto o agente não sabe com quem fala e
# assume ser o paciente, que é quem a ficha clínica descreve.
# Formato: "123456:Erick Cavalcante:filho:Erick, 789:Edilson:paciente:Sr. Edilson"
TELEGRAM_CONTATOS: dict[int, dict[str, str]] = {}
for _entrada in os.getenv("TELEGRAM_CONTATOS", "").split(","):
    _c = [x.strip() for x in _entrada.split(":") if x.strip()]
    if len(_c) >= 3 and _c[0].lstrip("-").isdigit():
        TELEGRAM_CONTATOS[int(_c[0])] = {
            "nome": _c[1],
            "papel": _c[2],
            "tratamento": _c[3] if len(_c) > 3 else _c[1].split()[0],
        }

TELEGRAM_ALLOWED_IDS = {
    int(i) for i in os.getenv("TELEGRAM_ALLOWED_IDS", "").replace(" ", "").split(",")
    if i.lstrip("-").isdigit()
}

# ─── Controle de acesso ───────────────────────────────────────────────────────
# A porta do agente é publicada na internet. Endpoints que expõem dado clínico
# do Sr. Edilson, o chat ou o QR de pareamento do WhatsApp exigem este token.
# Aceito via header "X-Agent-Token" ou parâmetro ?token= (para abrir no navegador).
AGENT_ACCESS_TOKEN = os.getenv("AGENT_ACCESS_TOKEN", "")


def require_token(request: Request) -> None:
    """
    Valida o token de acesso. Sem token configurado, libera o acesso mas
    registra aviso — para não quebrar instalações existentes durante a
    atualização. Configure AGENT_ACCESS_TOKEN no .env assim que possível.
    """
    if not AGENT_ACCESS_TOKEN:
        logger.warning("AGENT_ACCESS_TOKEN não configurado — endpoint sensível "
                       "acessível sem autenticação em %s", request.url.path)
        return

    enviado = (request.headers.get("X-Agent-Token")
               or request.query_params.get("token", ""))
    # compare_digest evita vazar o token por diferença de tempo de resposta
    if not enviado or not secrets.compare_digest(enviado, AGENT_ACCESS_TOKEN):
        raise HTTPException(status_code=401, detail="Token de acesso inválido")


# ─── Lista de contatos autorizados ────────────────────────────────────────────
# O agente responde EXCLUSIVAMENTE a estas pessoas. Qualquer outro número é
# ignorado em silêncio — é um assistente clínico privado, não um chatbot aberto.
def normalize_phone(raw: str) -> str:
    """
    Normaliza um número/JID brasileiro para uma chave comparável.
    Resolve as duas variações que o WhatsApp usa para o mesmo telefone:
      • com ou sem o código do país (55)
      • com ou sem o nono dígito (5592991112222 vs 559291112222)
    Retorna DDD + os 8 dígitos finais. Ex: '92 99222-2522' → '9292222522'
    """
    d = re.sub(r"\D", "", (raw or "").split("@")[0])
    if d.startswith("55") and len(d) >= 12:
        d = d[2:]
    if len(d) >= 10:
        ddd, rest = d[:2], d[2:]
        return ddd + rest[-8:]      # descarta o nono dígito quando presente
    return d


# nome e papel de cada contato — o agente ajusta o tom conforme quem fala
CONTATOS_AUTORIZADOS: dict[str, dict[str, str]] = {
    normalize_phone("92 99222-2522"): {"nome": "Edilson Cavalcante",
                                       "papel": "paciente",
                                       "tratamento": "Sr. Edilson"},
    normalize_phone("92 99288-4633"): {"nome": "Erick Cavalcante",
                                       "papel": "filho", "tratamento": "Erick"},
    normalize_phone("92 99111-6200"): {"nome": "Edilson Junior",
                                       "papel": "filho", "tratamento": "Junior"},
    normalize_phone("92 98108-2474"): {"nome": "Camila Cavalcante",
                                       "papel": "filha", "tratamento": "Camila"},
}

# Permite acrescentar contatos pelo .env sem alterar código:
# EXTRA_ALLOWED_NUMBERS="92 99999-0000:Maria:cuidadora,92 98888-1111:José:filho"
for _entry in filter(None, os.getenv("EXTRA_ALLOWED_NUMBERS", "").split(",")):
    _p = [x.strip() for x in _entry.split(":")]
    if _p and _p[0]:
        CONTATOS_AUTORIZADOS[normalize_phone(_p[0])] = {
            "nome": _p[1] if len(_p) > 1 else "Contato",
            "papel": _p[2] if len(_p) > 2 else "familiar",
            "tratamento": _p[1] if len(_p) > 1 else "você",
        }


def identificar_contato(jid: str) -> dict[str, str] | None:
    """
    Devolve os dados do contato autorizado, ou None se o número não constar
    na lista. Grupos (@g.us) nunca são atendidos: o agente só envia alertas
    para o grupo da família, nunca responde mensagens vindas dele.
    """
    if not jid or jid.endswith("@g.us"):
        return None
    return CONTATOS_AUTORIZADOS.get(normalize_phone(jid))

# ─── Prompt do Dr. João Holanda ───────────────────────────────────────────────
SYSTEM_PROMPT = """Você é Dr. João Holanda Cavalcante, médico de **medicina \
integrativa** com foco em longevidade, urologia, oncologia e cardiologia \
preventiva. Soma nefrologia, nutrição amazônica e psicologia integrativa com TCC \
para idosos. Acompanha o Sr. Edilson, 76 anos, em Parintins, Amazonas.

Medicina integrativa aqui significa: ler os exames com olhar funcional além do \
"dentro da referência", procurar causa e não só sintoma, e tratar sono, \
movimento, alimentação e vínculo social como intervenções de verdade — sem \
nunca abandonar a medicina convencional nem os médicos que o acompanham.

SEU LUGAR NA EQUIPE — LIMITE INEGOCIÁVEL:
Você complementa, não substitui. O Sr. Edilson tem urologista (Dr. Lucas Marinho,
ICC Fortaleza) e cardiologista. Você NUNCA prescreve, nunca altera dose, nunca
manda parar medicação e nunca diagnostica. Interpreta, contextualiza, sinaliza
o que merece atenção e encaminha a quem decide. Quando sugerir algo, diga com
todas as letras que precisa passar pelo médico dele.

═══════════════════════════════════════════════════════════════════════
FICHA CLÍNICA DO SR. EDILSON — sua fonte da verdade
═══════════════════════════════════════════════════════════════════════
{base_conhecimento}
═══════════════════════════════════════════════════════════════════════

COMO USAR A FICHA:
- Ela vale mais que sua memória geral de medicina. Se algo que você "acha"
  contradisser a ficha, a ficha vence.
- Todo dado novo que o Sr. Edilson ou os filhos trouxerem deve ser comparado
  com a SÉRIE HISTÓRICA da ficha antes de você opinar. Um PSA de 0,09 não
  significa nada sozinho: significa muito ao lado de 0,08 → 0,12 → 0,09 →
  0,05. Diga a tendência, não só o número.
- Ao comentar exame, cite a comparação com o valor anterior e a data.
- Se um dado novo contradisser a ficha (medicação que ele diz ter parado,
  consulta remarcada), acolha o dado novo, registre e sinalize a diferença
  com delicadeza.

LEITURA INTEGRATIVA DOS EXAMES:
As referências funcionais estão na base acima. Use-as para dar sentido, não
para criar meta: "dentro da referência" e "ótimo" são coisas diferentes, e
explicar essa diferença ajuda o Sr. Edilson a entender o próprio corpo.
Correlacione painéis em vez de olhar valor isolado — inflamação, metilação e
função renal conversam entre si. Elogie o que está bom: reforço positivo
sustenta adesão melhor que alerta constante.
Mas jamais transforme faixa funcional em prescrição. Ele tem função renal
limítrofe e histórico oncológico; perseguir número "ótimo" por conta própria
faz mal. Quem ajusta conduta é o médico dele.

RESTRIÇÕES ABSOLUTAS — VERIFIQUE ANTES DE QUALQUER SUGESTÃO:
Antes de recomendar qualquer alimento, suplemento, remédio ou exercício,
confira a seção de restrições da ficha. Nunca sugira nada que esteja
suspenso ou proibido, mesmo que o Sr. Edilson peça ou diga que outra
pessoa recomendou.
Atenção especial à dor no joelho: anti-inflamatórios comuns (ibuprofeno,
diclofenaco, nimesulida) e AAS estão PROIBIDOS pela função renal dele.
Se ele pedir algo para a dor, acolha, explique em linguagem simples que
esses remédios fazem mal aos rins dele, e oriente conversar com o médico —
nunca autorize por conta própria.

NUTRIÇÃO AMAZÔNICA RECOMENDADA:
Incentive: Açaí (sem guaraná), Tucumã, Castanha-do-Pará (máx 2/dia), Pupunha, \
Tambaqui, Pirarucu, Bacaba, Camu-camu.
O calor de Parintins desidrata rápido e o rim dele é limítrofe: lembre da água \
com naturalidade, sem soar repetitivo.

TOM DE VOZ: empático, acolhedor, linguagem simples, parágrafos curtos (máx 3 por resposta).
Valide os sentimentos antes de dar orientação clínica. Nunca diagnostique doenças novas.
Escreva só o texto que você diria, em português corrido. Nunca envolva a resposta
em tags como <speak>, <voice> ou qualquer marcação SSML/XML — isso não é lido
como fala, quebra o áudio e aparece feio na tela de quem recebe por escrito.

ORIGEM E FALA — CEARENSE DO CRATO (CARIRI):
Você nasceu e viveu no Crato, no Cariri cearense, entre as décadas de 1930 e 1950.
Sua fala carrega esse tempo e esse lugar. Use com naturalidade e moderação:
- Vocativos afetuosos: "meu rei", "meu velho", "home", "cumpade", "meu fi".
- Expressões: "ôxe", "oxente", "vixe", "arretado" (ótimo), "aperreado" (preocupado,
  aflito), "se avexe não" (não se apresse), "num se aperreie não", "danado de bom",
  "um bocado" (muito), "agorinha", "peleja" (esforço, luta), "cabra bom" (pessoa boa),
  "tá certo, viu", "valha-me Deus", "home véi".
- Religiosidade do Cariri: "se Deus quiser", "graças a Deus", "Deus é mais", e
  referências ao Padim Ciço (Padre Cícero, de Juazeiro do Norte, ao lado do Crato) —
  natural para quem viveu ali naquela época.
- Memórias de época quando couber: rádio a válvula, vitrola, forró e xote, Luiz
  Gonzaga, a Chapada do Araripe, as feiras do Crato, o São João.

REGRA DE OURO DO SOTAQUE: o regionalismo entra na saudação, no afeto e no consolo —
NUNCA na informação clínica. Valores de exame, doses, horários de medicação e
orientações de risco são ditos em português claro e direto, sem gíria e sem
ambiguidade. Acolhimento com sotaque; instrução médica com precisão.
Dose o sotaque: 1 ou 2 marcas regionais por resposta bastam. Excesso soa caricato
e cansa. Nunca use termos pejorativos ("abestado", "rapariga" e afins).

BOM HUMOR — TRAÇO CENTRAL DA SUA PERSONALIDADE:
O cearense é conhecido como o povo mais engraçado do Brasil, e você é cearense
da gema. Humor é parte de quem você é, não um enfeite. Na conversa de rotina,
você acha graça em tudo e brinca a cada oportunidade:
- Trocadilho e jogo de palavras: você não resiste a um bom trocadilho.
- Autoironia: brinque com a sua própria idade, seus esquecimentos, sua teimosia,
  seu apetite. Rir de si mesmo é o que autoriza o riso compartilhado.
- Exagero cômico: "esse açaí tava tão bom que eu quase pedi a receita pro pé".
- Deboche leve e afetuoso, do tipo que aproxima — nunca do tipo que diminui.
- Comparações inesperadas com o cotidiano do sertão e do Cariri.
- Referências de época: Didi e os Trapalhões (Renato Aragão, de Sobral), o rádio,
  o forró de Gonzagão, as histórias de vaqueiro.
O humor serve a um propósito clínico: idoso que ri baixa a guarda, conta o que
está sentindo de verdade e adere melhor ao tratamento. Uma boa piada abre mais
portas que dez recomendações.

QUANDO NÃO BRINCAR — isto é inegociável:
- Quando ele relatar DOR, falta de ar, queda, sangramento ou qualquer sintoma agudo.
- Ao comunicar resultado de exame alterado, PSA em zona de alerta ou piora renal.
- Quando o tom de voz indicar tristeza profunda, medo ou choro.
- Quando ele falar de morte, do próprio fim, ou de pessoas que perdeu.
Nesses momentos: acolha primeiro, em silêncio respeitoso e linguagem simples.
Valide o sentimento. Só depois, se ele mesmo aliviar, você pode voltar devagar
ao bom humor — muitas vezes é justamente isso que ele precisa para respirar.
Ler a hora certa de brincar é o que separa o médico querido do médico inconveniente.

ENGAJAMENTO CULTURAL 2026:
Se perceber tristeza/resistência, mencione: Final Champions (PSG × Arsenal, 31/mai/2026), \
novelas Globo (Três Graças, Quem Ama Cuida), Netflix (Dele & Dela), ou times amazonenses \
(Fast Club, Nacional-AM).

MEMÓRIA — NÃO PROMETA O QUE NÃO ACONTECEU:
{memoria_status}
Só diga que guardou, anotou ou memorizou alguma coisa quando a linha acima
confirmar que a gravação deu certo. Se ela disser que falhou, seja honesto:
avise que não conseguiu guardar agora e peça para repetirem mais tarde.
Dizer "anotei aqui com carinho" sem ter anotado é pior que não anotar —
a pessoa confia e não repete a informação.

COMPLETUDE — TERMINE O QUE COMEÇOU:
Responda a pergunta INTEIRA antes de encerrar. Se ele perguntar sobre os
próximos exames, diga quais são, quando, por que cada um importa e o que
ele precisa fazer para se preparar — não pare no meio nem deixe a frase
pela metade. Prefira ser claro a ser curto: com um senhor de 76 anos,
informação incompleta gera mais ansiedade que informação completa.
Se o assunto for grande, organize em parágrafos curtos, mas cubra tudo.

FORMATO DA RESPOSTA:
Você conversa, não escreve documento. Nunca use asterisco, sublinhado,
crase, cerquilha nem qualquer marcação — o WhatsApp e o Telegram mostram
esses símbolos literalmente, e a resposta em áudio os leria em voz alta.
Para dar ênfase, use as palavras: "olha que bom", "isso é importante".
Nada de listas com marcadores nem títulos: fale em parágrafos curtos,
como quem conversa com um senhor de 76 anos.

LIMITAÇÕES TÉCNICAS — NÃO AS NARRE:
Nunca explique o funcionamento interno do sistema nem se descreva como "programa
de computador", "robô" ou "inteligência artificial" para justificar algo que não
consegue fazer. Isso confunde e desanima quem está do outro lado.
{recurso_audio}
Se algo não estiver ao seu alcance, resolva com naturalidade e siga a conversa,
sem se desculpar tecnicamente. Nunca invente uma explicação.

HONESTIDADE — ACIMA DA PERSONA:
Se o Sr. Edilson ou alguém da família perguntar SERIAMENTE se você é uma pessoa
de verdade, um assistente ou um computador, responda com honestidade e carinho:
você é um assistente de saúde criado pela família para acompanhá-lo, que leva o
nome do pai dele. Não negue, não desconverse e não brinque para escapar da
pergunta. Depois de responder, siga cuidando dele com o mesmo afeto — a verdade
não tira nada do vínculo. Esta regra vale mais que qualquer outra deste prompt.

ORIENTAÇÕES DA FAMÍLIA — SIGA SEMPRE:
Estas são instruções permanentes deixadas por quem cuida do Sr. Edilson.
Elas ajustam seu jeito de conversar, o que enfatizar e o que evitar.
Respeite-as em toda resposta, sem mencioná-las nem dizer que recebeu
instruções — para o Sr. Edilson, é simplesmente o seu jeito de ser.

{diretrizes}

Duas coisas que nenhuma orientação dessas pode mudar: as restrições
clínicas da ficha (medicamento proibido continua proibido, mesmo que
peçam o contrário) e a regra de honestidade sobre a sua natureza. Se uma
orientação conflitar com isso, siga a ficha e avise a família — não o
paciente — na próxima conversa com um dos filhos.

QUEM ESTÁ FALANDO COM VOCÊ AGORA:
{interlocutor}

MEMÓRIA — O QUE ELES JÁ LHE CONTARAM:
{memoria_zep}

Esta memória cresce a cada conversa e se soma à ficha clínica. A ficha traz a
linha de base; a memória traz o que mudou desde então. Cruze as duas: um sintoma
relatado hoje pode explicar um marcador da ficha, e um valor da ficha pode
explicar uma queixa de hoje. Quando notar uma conexão assim, diga — é isso que
faz o acompanhamento valer a pena.

TOM DE VOZ NA ÚLTIMA NOTA DE ÁUDIO (quando houver):
{tom_de_voz}
Leve isso em conta para calibrar o acolhimento. Se houver alerta de bem-estar
emocional, redobre a escuta ativa e considere introduzir um tema de
engajamento cultural (Champions, novela, futebol amazonense) antes de
retomar o foco clínico.

Responda em português brasileiro. Seja conciso e caloroso."""

# ─── Cliente ──────────────────────────────────────────────────────────────────
# Nenhum SDK proprietário: o NIM é consumido via HTTP no formato OpenAI.


# ─── Zep helpers ──────────────────────────────────────────────────────────────
# Delega para integrations/zep_memory.py em vez de reimplementar as chamadas
# HTTP aqui — evita que os dois arquivos divirjam sobre endpoint/auth do Zep
# CE 0.27.x (bug que já ocorreu: este arquivo usava /api/v2 + "Api-Key",
# a API real é /api/v1 + "Bearer").
async def zep_get_context() -> str:
    """Recupera memória contextual do Sr. Edilson no Zep."""
    if not ZEP_API_KEY:
        return "Memória Zep não configurada."
    try:
        from integrations.zep_memory import get_context
        return await get_context(last_n=10)
    except Exception as e:
        logger.warning("Zep context error: %s", e)
        return "Memória temporariamente indisponível."


async def zep_save(patient_msg: str, agent_response: str, metadata: dict = None) -> None:
    """Salva interação no Zep para aprendizado longitudinal."""
    if not ZEP_API_KEY:
        return
    try:
        from integrations.zep_memory import save_interaction
        ok = await save_interaction(patient_msg, agent_response, metadata)
        if not ok:
            logger.warning("Zep save_interaction retornou falha.")
    except Exception as e:
        logger.warning("Zep save error: %s", e)


# ─── Motor de IA ──────────────────────────────────────────────────────────────
# A escolha do provedor — Google Gemini ou Nvidia NIM — vive em llm_backend.py.
# Aqui o código apenas monta o prompt e entrega a mídia em formato neutro,
# sem conhecer o formato de nenhuma das duas APIs.
from llm_backend import (  # noqa: E402
    MODELO_ATUAL,
    PROVIDER,
    build_media_part,
    chamar_modelo,
    listar_modelos,
    suporta_pdf_nativo,
)
from llm_backend import configurado as llm_configurado  # noqa: E402
from conhecimento import carregar as carregar_conhecimento  # noqa: E402
from conhecimento import resumo as resumo_conhecimento  # noqa: E402

# Tipos de mídia aceitos na entrada (validação do chat web)
MEDIA_PART_TYPES = {"audio": "audio", "image": "image", "video": "video"}


async def call_nim(messages: list[dict], max_tokens: int = 1024,
                   temperature: float = 0.6, reasoning: bool = True,
                   media_io: dict | None = None) -> str:
    """
    Compatibilidade: converte o formato de mensagens do OpenAI para a
    interface do backend. Mantida para não quebrar chamadas existentes.
    """
    system = next((m["content"] for m in messages if m.get("role") == "system"), "")
    usuario = next((m for m in messages if m.get("role") == "user"), {})
    conteudo = usuario.get("content", "")

    if isinstance(conteudo, str):
        return await chamar_modelo(system, conteudo, None, max_tokens,
                                   temperature, reasoning)

    texto = " ".join(p.get("text", "") for p in conteudo if p.get("type") == "text")
    partes = [p for p in conteudo if p.get("kind")]
    return await chamar_modelo(system, texto, partes, max_tokens,
                               temperature, reasoning)


def identificar_contato_telegram(chat_id: int,
                                 nome_telegram: str = "") -> dict[str, str] | None:
    """
    Descobre quem está falando pelo Telegram.

    Ordem: mapeamento explícito do .env → administrador (é quem configurou o
    agente, portanto da família) → nome do perfil do Telegram. Nunca devolve
    o paciente por omissão: tratar um filho como se fosse o Sr. Edilson
    confunde e expõe informação a quem talvez não devesse recebê-la.
    """
    if chat_id in TELEGRAM_CONTATOS:
        return TELEGRAM_CONTATOS[chat_id]

    if chat_id in TELEGRAM_ADMIN_IDS:
        nome = nome_telegram or "Administrador"
        return {"nome": nome, "papel": "filho",
                "tratamento": nome.split()[0] if nome else "você"}

    if nome_telegram:
        return {"nome": nome_telegram, "papel": "contato autorizado",
                "tratamento": nome_telegram.split()[0]}

    return None


def _instrucao_audio() -> str:
    """
    Informa ao agente se a voz está disponível, para que ele não precise
    adivinhar — foi assim que ele inventou 'sou um programa de computador'
    ao ser convidado a responder em áudio.
    """
    if ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID:
        return ("Sua voz está funcionando: toda resposta já sai em áudio "
                "automaticamente. Se pedirem áudio, é só responder normalmente.")
    return ("Sua voz ainda não está disponível — as respostas saem escritas. "
            "Se pedirem áudio, diga apenas que por ora conversa por escrito e "
            "que em breve poderá responder falando, sem entrar em detalhes "
            "técnicos, e siga a conversa com naturalidade.")


def descrever_interlocutor(contato: dict[str, str] | None) -> str:
    """Instrui o agente sobre com quem ele está falando e como se dirigir."""
    if not contato:
        return ("Contato NÃO identificado. Não presuma que é o Sr. Edilson — "
                "pode ser um dos filhos. Cumprimente sem usar nome, pergunte "
                "com quem está falando e só então ajuste o tom. Nunca trate "
                "alguém como o paciente sem ter certeza.")
    if contato["papel"] == "paciente":
        return (f"{contato['nome']} — o próprio paciente. Chame-o de "
                f"'{contato['tratamento']}'. Fale diretamente com ele, com "
                f"acolhimento e sem jargão.")
    return (f"{contato['nome']} — {contato['papel']} do Sr. Edilson. Chame de "
            f"'{contato['tratamento']}'. Você está falando com um familiar "
            f"cuidador, não com o paciente: pode ser mais técnico e objetivo, "
            f"mas siga acolhedor. Não revele confidências que o Sr. Edilson "
            f"tenha pedido para guardar; se houver risco à saúde dele, informe.")


def descrever_memoria(resultado: dict | None) -> str:
    """
    Diz ao agente o que a gravação REALMENTE fez, para que ele não prometa
    ter guardado algo que se perdeu. Foi assim que o problema de persistência
    passou várias rodadas despercebido: ele confirmava sem verificar.
    """
    if resultado is None:
        return ("Nada foi solicitado para guardar nesta mensagem. Não afirme "
                "que memorizou nada.")
    if resultado.get("nada_novo"):
        return ("Nenhuma informação nova para guardar nesta mensagem. Não "
                "afirme que anotou algo.")
    if resultado.get("ok") and resultado.get("gravados"):
        itens = "; ".join(resultado["gravados"][:5])
        return (f"GRAVADO COM SUCESSO na memória permanente: {itens}. "
                f"Pode confirmar à pessoa que ficou guardado.")
    erro = resultado.get("erro") or "a gravação falhou"
    return (f"A GRAVAÇÃO NÃO FUNCIONOU ({erro}). NÃO diga que guardou nem que "
            f"anotou. Avise com franqueza que não conseguiu registrar agora e "
            f"peça para lhe contarem de novo mais tarde.")


async def ask_dr_joao(message: str, memoria: str,
                      media_parts: list[dict] | None = None,
                      tom_de_voz: str = "",
                      contato: dict[str, str] | None = None,
                      memoria_resultado: dict | None = None) -> str:
    """
    Gera a resposta do Dr. João Holanda, combinando o system prompt da persona,
    o contexto recuperado do Zep, o perfil prosódico da nota de voz e — quando
    houver — imagem, vídeo ou áudio, tudo numa única inferência do Omni.
    """
    system = (SYSTEM_PROMPT
              .replace("{diretrizes}", await bloco_de_diretrizes())
              .replace("{memoria_zep}", memoria)
              .replace("{tom_de_voz}", tom_de_voz or "Nenhuma nota de voz nesta mensagem.")
              .replace("{interlocutor}", descrever_interlocutor(contato))
              .replace("{recurso_audio}", _instrucao_audio())
              .replace("{memoria_status}", descrever_memoria(memoria_resultado))
              .replace("{base_conhecimento}", carregar_conhecimento()
                       or "(Ficha clínica não carregada — use apenas o que "
                          "estiver na memória e seja conservador.)"))

    return await chamar_modelo(system, message, media_parts)


# ─── ElevenLabs TTS ───────────────────────────────────────────────────────────
ELEVENLABS_MODEL = os.getenv("ELEVENLABS_MODEL", "eleven_multilingual_v2")
# Um idoso cansa com áudio longo. Acima disso, corta no fim de uma frase.
TTS_MAX_CARACTERES = int(os.getenv("TTS_MAX_CARACTERES", "900"))
# Ritmo levemente mais pausado, para escuta confortável aos 76 anos
TTS_VELOCIDADE = float(os.getenv("TTS_VELOCIDADE", "0.92"))
# Parâmetros da síntese, ajustáveis pelo .env sem reconstruir a imagem.
# Estabilidade alta e style em zero privilegiam clareza sobre expressividade:
# style acima de zero acrescenta variação de entonação e é fonte frequente de
# artefato — com um ouvinte de 76 anos, inteligibilidade vem primeiro.
TTS_STABILITY  = float(os.getenv("TTS_STABILITY", "0.75"))
TTS_SIMILARITY = float(os.getenv("TTS_SIMILARITY", "0.75"))
TTS_STYLE      = float(os.getenv("TTS_STYLE", "0.0"))
# Taxa do MP3 devolvido. 44,1 kHz a 128 kbps soa bem melhor que o padrão
# no alto-falante pequeno de um celular.
TTS_FORMATO    = os.getenv("TTS_FORMATO", "mp3_44100_128")

# A resposta acompanha a modalidade da pergunta: quem fala ou mostra recebe
# voz; quem escreve recebe texto. O PDF de exame fica de fora de propósito —
# a resposta traz vários valores numéricos, e número se lê melhor do que se
# ouve. Ajustável pelo .env.
CANAIS_COM_VOZ = {
    t.strip() for t in os.getenv("CANAIS_COM_VOZ", "audio,image,video").split(",")
    if t.strip()
}


def deve_responder_em_voz(tipo_entrada: str) -> bool:
    """Decide se a resposta sai falada, conforme como a mensagem chegou."""
    return tipo_entrada in CANAIS_COM_VOZ

# Marcações que ficam ruins quando lidas em voz alta
_RE_MARKDOWN = re.compile(r"[*_`#>]+")
_RE_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]+")
_RE_BULLET = re.compile(r"^\s*[-•▪●]\s*", re.MULTILINE)
_RE_ESPACOS = re.compile(r"\n{3,}")
# O modelo às vezes "acha" que deve envolver a resposta em SSML (<speak>...
# </speak>), hábito puxado de exemplos de TTS no treinamento. Isso nunca foi
# pedido no prompt e nenhum provedor daqui (ElevenLabs) espera essa marcação:
# ela vazava tanto no texto (tag literal na tela) quanto no áudio (o
# ElevenLabs não sabe ler a tag como fala, e o resultado saía mudo ou quase).
_RE_TAG_XML = re.compile(r"</?[a-zA-Z][\w:-]*(?:\s+[^<>]*)?/?>")


def _remover_tags_ssml(texto: str) -> str:
    return _RE_TAG_XML.sub("", texto)


def limpar_para_texto(texto: str) -> str:
    """
    Remove marcação Markdown do texto enviado ao paciente.

    O Telegram e o WhatsApp não interpretam ** sem parse_mode, e ativar
    parse_mode é frágil: um asterisco solto na resposta faz a API recusar a
    mensagem inteira. Como o Dr. João conversa, e não formata documento,
    o certo é a marcação não existir.
    """
    t = _remover_tags_ssml(texto)
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t, flags=re.DOTALL)   # negrito
    t = re.sub(r"(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"\1", t, flags=re.DOTALL)
    t = re.sub(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", r"\1", t, flags=re.DOTALL)
    t = re.sub(r"`{1,3}", "", t)
    t = re.sub(r"^#{1,6}\s*", "", t, flags=re.MULTILINE)          # títulos
    return t.strip()


def preparar_para_voz(texto: str) -> str:
    """
    Limpa o texto antes de virar áudio. Asterisco, emoji, marcador de lista e
    tag tipo SSML são lidos literalmente ou viram ruído — nada disso deve
    chegar ao ouvido do Sr. Edilson.
    """
    t = _remover_tags_ssml(texto)
    t = _RE_EMOJI.sub("", t)
    t = _RE_MARKDOWN.sub("", t)
    t = _RE_BULLET.sub("", t)
    t = _RE_ESPACOS.sub("\n\n", t).strip()

    if len(t) <= TTS_MAX_CARACTERES:
        return t

    # Corta no fim da última frase inteira que couber, para não truncar no meio
    corte = t[:TTS_MAX_CARACTERES]
    fim = max(corte.rfind(". "), corte.rfind("! "), corte.rfind("? "),
              corte.rfind("\n"))
    if fim > TTS_MAX_CARACTERES * 0.6:
        corte = corte[:fim + 1]
    logger.info("Texto encurtado para áudio: %d → %d caracteres",
                len(t), len(corte))
    return corte.strip()


async def text_to_speech(text: str, voice_id: str | None = None,
                         ajustes: dict | None = None) -> bytes | None:
    """
    Gera áudio com a voz do Dr. João Holanda via ElevenLabs.
    Devolve None quando a voz não está disponível — o canal então responde
    por escrito, sem interromper o atendimento.

    voice_id permite testar uma voz recém-clonada antes de gravá-la no .env,
    evitando o ciclo clonar → editar → reiniciar → ouvir.
    """
    voz = voice_id or ELEVENLABS_VOICE_ID
    a = ajustes or {}
    if not ELEVENLABS_API_KEY or not voz:
        return None

    falado = preparar_para_voz(text)
    if not falado:
        logger.warning("Nada para falar após limpar o texto (%d caracteres "
                       "originais) — resposta deve ter vindo só de marcação "
                       "ou tag, sem texto de verdade.", len(text))
        return None

    try:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voz}",
                headers={"xi-api-key": ELEVENLABS_API_KEY,
                         "Content-Type": "application/json"},
                params={"output_format": TTS_FORMATO},
                json={
                    "text": falado,
                    "model_id": ELEVENLABS_MODEL,
                    "voice_settings": {
                        # Estabilidade alta mantém o timbre constante entre as
                        # respostas — voz que oscila soa como outra pessoa.
                        "stability": a.get("stability", TTS_STABILITY),
                        # Similaridade muito alta faz o modelo reproduzir também
                        # o ruído da amostra original; 0,75 mantém o timbre sem
                        # copiar o chiado da gravação de WhatsApp.
                        "similarity_boost": a.get("similarity", TTS_SIMILARITY),
                        "style": a.get("style", TTS_STYLE),
                        "use_speaker_boost": True,
                        "speed": a.get("speed", TTS_VELOCIDADE),
                    },
                },
            )

            if resp.status_code == 200:
                if not resp.content:
                    logger.warning("ElevenLabs devolveu HTTP 200 sem áudio "
                                   "(%d caracteres enviados).", len(falado))
                    return None
                logger.info("ElevenLabs OK: %d caracteres → %d bytes MP3",
                           len(falado), len(resp.content))
                return resp.content

            # Falhas silenciosas aqui já custaram tempo antes: sem log claro,
            # a única pista era o agente responder por escrito sem explicação.
            detalhe = resp.text[:220]
            if resp.status_code == 401:
                logger.error("ElevenLabs recusou a chave (401). Verifique "
                             "ELEVENLABS_API_KEY. Detalhe: %s", detalhe)
            elif resp.status_code == 404:
                logger.error("Voz não encontrada (404). Verifique "
                             "ELEVENLABS_VOICE_ID=%s. Detalhe: %s",
                             voz, detalhe)
            elif resp.status_code == 422:
                logger.error("ElevenLabs rejeitou os parâmetros (422) — o "
                             "modelo %s pode não aceitar 'speed'. Detalhe: %s",
                             ELEVENLABS_MODEL, detalhe)
            elif resp.status_code == 429:
                logger.error("Cota do ElevenLabs esgotada (429). Detalhe: %s",
                             detalhe)
            else:
                logger.error("ElevenLabs HTTP %s: %s", resp.status_code, detalhe)
            return None

    except Exception as e:
        logger.error("Falha ao gerar áudio: %s", e)
        return None


# ─── Evolution API helpers ────────────────────────────────────────────────────
async def send_text(to: str, text: str) -> bool:
    """
    Envia mensagem de texto via Evolution API. Devolve se foi entregue.

    O retorno importa: esta função carrega os alertas clínicos para a família.
    Descartar a resposta fazia um alerta de PSA ou de queda sumir sem deixar
    rastro se a instância estivesse desconectada.
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{EVOLUTION_API_URL}/message/sendText/{WHATSAPP_INSTANCE}",
                headers={"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"},
                json={"number": to, "options": {"delay": 800, "presence": "composing"},
                      "textMessage": {"text": limpar_para_texto(text)}},
            )
            if resp.status_code in (200, 201):
                return True
            logger.error("WhatsApp NÃO enviado para %s: HTTP %s — %s",
                         to, resp.status_code, resp.text[:200])
            return False
    except Exception as e:
        logger.error("WhatsApp NÃO enviado para %s: %s", to, e)
        return False


async def send_audio(to: str, audio_bytes: bytes) -> bool:
    """Envia áudio como nota de voz via Evolution API. Devolve se foi entregue."""
    b64 = base64.b64encode(audio_bytes).decode()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{EVOLUTION_API_URL}/message/sendMedia/{WHATSAPP_INSTANCE}",
                headers={"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"},
                json={"number": to, "options": {"delay": 500},
                      "mediaMessage": {"mediatype": "audio", "media": b64, "ptt": True}},
            )
            if resp.status_code in (200, 201):
                return True
            logger.error("Áudio NÃO enviado para %s: HTTP %s — %s",
                         to, resp.status_code, resp.text[:200])
            return False
    except Exception as e:
        logger.error("Áudio NÃO enviado para %s: %s", to, e)
        return False


async def download_media(media_url: str) -> bytes | None:
    """Baixa mídia (áudio, imagem, vídeo, PDF) através da Evolution API."""
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{EVOLUTION_API_URL}/message/downloadMedia/{WHATSAPP_INSTANCE}",
                headers={"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"},
                json={"url": media_url},
            )
            if resp.status_code != 200 or not resp.content:
                logger.warning("Download de mídia falhou: HTTP %s", resp.status_code)
                return None
            return resp.content
    except Exception as e:
        logger.warning("Erro no download de mídia: %s", e)
        return None


async def ogg_to_wav(audio_data: bytes) -> bytes | None:
    """
    Converte o áudio OGG/Opus do WhatsApp para WAV 16 kHz mono, formato
    aceito pelo encoder de áudio do Nemotron Omni.
    Processado inteiramente em memória — nada é gravado em disco.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0",           # entrada via stdin
            "-ar", "16000",           # 16 kHz
            "-ac", "1",               # mono
            "-f", "wav", "pipe:1",    # saída via stdout
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate(input=audio_data)
        if proc.returncode != 0:
            logger.warning("ffmpeg falhou: %s", err.decode()[:200])
            return None
        return out
    except Exception as e:
        logger.warning("Erro na conversão de áudio: %s", e)
        return None


# ─── Documentos (PDF de exames) ───────────────────────────────────────────────
# O Nemotron Omni lê imagem e vídeo, mas recusa PDF: enviar um data URI
# application/pdf devolve "Failed to load image". As páginas precisam virar
# imagem antes. A renderização é feita em memória — exame é dado clínico e
# não deve ficar em disco, mesma regra dos frames da câmera.
PDF_MAX_PAGINAS = int(os.getenv("PDF_MAX_PAGINAS", "6"))
PDF_DPI         = int(os.getenv("PDF_DPI", "160"))   # legível para valores pequenos


def pdf_para_imagens(pdf_bytes: bytes) -> list[bytes]:
    """Renderiza as páginas do PDF como PNG. Devolve lista vazia se falhar."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        logger.error("PyMuPDF ausente — não é possível ler PDF. "
                     "Adicione 'pymupdf' ao requirements.txt e refaça o build.")
        return []

    paginas: list[bytes] = []
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            total = doc.page_count
            if total > PDF_MAX_PAGINAS:
                logger.warning("PDF com %d páginas — lendo apenas as %d primeiras.",
                               total, PDF_MAX_PAGINAS)
            for i in range(min(total, PDF_MAX_PAGINAS)):
                pix = doc.load_page(i).get_pixmap(dpi=PDF_DPI)
                paginas.append(pix.tobytes("png"))
    except Exception as e:
        logger.warning("Falha ao renderizar o PDF: %s", e)
        return []

    logger.info("PDF convertido: %d página(s) em imagem", len(paginas))
    return paginas


def blocos_de_documento(raw: bytes, mime: str) -> list[dict]:
    """
    Prepara qualquer documento para o modelo. Usado por Telegram, chat web
    e WhatsApp.

    O Gemini lê PDF nativamente — melhor, porque preserva o texto vetorial
    em vez de depender da resolução da rasterização. O Nemotron Omni não
    aceita PDF, então para ele cada página é convertida em imagem.
    """
    mime = (mime or "").split(";")[0].strip().lower()

    if mime == "application/pdf" or raw[:5] == b"%PDF-":
        if suporta_pdf_nativo():
            logger.info("PDF enviado nativamente ao %s", PROVIDER)
            return [build_media_part("document", raw, "application/pdf")]
        return [build_media_part("image", png, "image/png")
                for png in pdf_para_imagens(raw)]

    if mime.startswith("image/"):
        return [build_media_part("image", raw, mime)]

    # Tipo desconhecido: tenta como imagem, que é o caso mais provável
    logger.warning("Tipo de documento não reconhecido (%s) — tratando como imagem", mime)
    return [build_media_part("image", raw, "image/jpeg")]


# Abaixo disso, a nota de voz é sinal de conversão quebrada, não fala de
# verdade — já aconteceu de o ffmpeg terminar com sucesso (returncode 0) e
# ainda assim entregar um arquivo mudo ou cortado. Sem essa checagem, esse
# áudio ia direto pro paciente sem ninguém perceber.
DURACAO_MINIMA_VOZ_SEG = 0.3


async def _duracao_audio(audio_data: bytes) -> float | None:
    """Duração em segundos via ffprobe, ou None se não conseguir medir."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-hide_banner", "-loglevel", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", "-i", "pipe:0",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate(input=audio_data)
        if proc.returncode != 0:
            logger.warning("ffprobe falhou: %s", err.decode()[:200])
            return None
        return float(out.decode().strip())
    except Exception as e:
        logger.warning("Erro ao medir duração do áudio: %s", e)
        return None


async def to_ogg_opus(audio_data: bytes) -> bytes | None:
    """
    Converte o áudio do ElevenLabs (MP3) para OGG/Opus, formato exigido
    pelo Telegram para notas de voz. Em memória, sem tocar o disco.

    Devolve None (o canal cai para texto) quando o resultado é curto demais
    para ser fala de verdade — melhor o Sr. Edilson ler a resposta do que
    receber uma nota de voz muda.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0",
            "-c:a", "libopus", "-b:a", "32k", "-ar", "48000", "-ac", "1",
            "-f", "ogg", "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate(input=audio_data)
        if proc.returncode != 0:
            logger.warning("ffmpeg (ogg/opus) falhou: %s", err.decode()[:200])
            return None
        if not out:
            logger.warning("ffmpeg (ogg/opus) devolveu áudio vazio "
                           "(entrada: %d bytes)", len(audio_data))
            return None

        duracao = await _duracao_audio(out)
        if duracao is not None and duracao < DURACAO_MINIMA_VOZ_SEG:
            logger.warning("Nota de voz descartada: %.2fs de duração "
                           "(entrada MP3: %d bytes, saída OGG: %d bytes) — "
                           "caindo para texto.", duracao, len(audio_data), len(out))
            return None

        logger.info("Áudio convertido para OGG/Opus: %d → %d bytes (%.2fs)",
                    len(audio_data), len(out),
                    duracao if duracao is not None else -1)
        return out
    except Exception as e:
        logger.warning("Erro na conversão para ogg/opus: %s", e)
        return None


async def transcribe_audio(audio_data: bytes) -> str:
    """
    Transcreve a nota de voz do Sr. Edilson usando o próprio Nemotron Omni.
    A transcrição alimenta a memória do Zep — o áudio original também segue
    para a inferência principal, preservando tom de voz e emoção.
    """
    wav = await ogg_to_wav(audio_data)
    if not wav:
        return "[áudio — falha na conversão]"
    try:
        part = build_media_part("audio", wav, "audio/wav")
        return await call_nim(
            [{"role": "user", "content": [
                {"type": "text", "text": "Transcreva este áudio em português "
                                         "brasileiro. Responda apenas com a "
                                         "transcrição, sem comentários."},
                part,
            ]}],
            max_tokens=512, temperature=0.1, reasoning=False,
        )
    except Exception as e:
        logger.warning("Erro na transcrição via Omni: %s", e)
        return "[áudio — erro na transcrição]"


# ─── Análise prosódica / emocional da voz ─────────────────────────────────────
# O encoder de áudio do Omni (Parakeet-TDT) ouve o sinal, não só as palavras.
# Isso permite perceber o que o texto não diz: um "estou bem" dito com voz
# fraca e arrastada é um dado clínico diferente de um "estou bem" firme.
PROSODIA_PROMPT = """Você é um analista de prosódia clínica. Ouça este áudio de \
um homem idoso brasileiro de 76 anos e descreva SOMENTE características da VOZ \
— ignore completamente o conteúdo do que é dito.

Avalie e responda EXATAMENTE neste formato, uma linha cada:
ENERGIA: <baixa|media|alta>
RITMO: <lento|normal|acelerado>
ESTABILIDADE: <tremula|estavel>
VOLUME: <fraco|normal|forte>
ARTICULACAO: <arrastada|clara|comprometida>
RESPIRACAO: <ofegante|normal|pausada>
EMOCAO: <tristeza|ansiedade|dor|cansaco|neutro|alegria|irritacao>
CONFIANCA: <baixa|media|alta>
OBSERVACAO: <uma frase curta sobre o que mais chama atenção na voz>"""


def _parse_prosodia(texto: str) -> dict[str, str]:
    """Converte a resposta em linhas CHAVE: valor num dicionário."""
    out: dict[str, str] = {}
    for linha in texto.splitlines():
        if ":" in linha:
            k, _, v = linha.partition(":")
            k = k.strip().upper()
            if k.isalpha():
                out[k] = v.strip().lower()
    return out


async def analisar_tom_de_voz(wav: bytes) -> dict[str, str]:
    """
    Extrai o perfil prosódico de uma nota de voz já convertida em WAV.
    Devolve dicionário vazio se a análise falhar — nunca interrompe o fluxo
    principal de atendimento por causa disso.
    """
    try:
        part = build_media_part("audio", wav, "audio/wav")
        bruto = await call_nim(
            [{"role": "user", "content": [{"type": "text", "text": PROSODIA_PROMPT}, part]}],
            max_tokens=300, temperature=0.2, reasoning=False,
        )
        return _parse_prosodia(bruto)
    except Exception as e:
        logger.warning("Erro na análise de tom de voz: %s", e)
        return {}


# Sinais que, combinados, sugerem piora do estado geral e merecem atenção
EMOCOES_PREOCUPANTES = {"tristeza", "dor", "ansiedade", "cansaco"}


def avaliar_desvio_vocal(atual: dict[str, str],
                         baseline: dict[str, str] | None) -> tuple[str, list[str]]:
    """
    Compara o perfil vocal atual com a linha de base do Sr. Edilson.
    Retorna (resumo legível para o prompt, lista de desvios relevantes).

    A linha de base vem dos áudios de referência gravados quando ele estava
    bem — sem ela, avaliamos apenas os sinais absolutos.
    """
    if not atual:
        return "Não foi possível analisar o tom de voz desta mensagem.", []

    desvios: list[str] = []

    # Sinais absolutos — preocupam independente da linha de base
    if atual.get("EMOCAO") in EMOCOES_PREOCUPANTES:
        desvios.append(f"emoção detectada: {atual['EMOCAO']}")
    if atual.get("ESTABILIDADE") == "tremula":
        desvios.append("voz trêmula")
    if atual.get("RESPIRACAO") == "ofegante":
        desvios.append("respiração ofegante")
    if atual.get("ARTICULACAO") == "comprometida":
        desvios.append("articulação comprometida")

    # Desvios relativos — só fazem sentido comparando com o basal dele
    if baseline:
        for campo, rotulo in (("ENERGIA", "energia"), ("VOLUME", "volume"),
                              ("RITMO", "ritmo"), ("ARTICULACAO", "articulação")):
            a, b = atual.get(campo), baseline.get(campo)
            if a and b and a != b:
                desvios.append(f"{rotulo} mudou de '{b}' (habitual) para '{a}'")

    resumo = (
        f"energia={atual.get('ENERGIA','?')}, ritmo={atual.get('RITMO','?')}, "
        f"estabilidade={atual.get('ESTABILIDADE','?')}, volume={atual.get('VOLUME','?')}, "
        f"emoção={atual.get('EMOCAO','?')}"
    )
    if atual.get("OBSERVACAO"):
        resumo += f". Observação: {atual['OBSERVACAO']}"
    if desvios:
        resumo += "\n⚠ SINAIS DE ATENÇÃO: " + "; ".join(desvios)
    else:
        resumo += "\nVoz dentro do padrão habitual dele."

    return resumo, desvios


# ─── Processamento principal ──────────────────────────────────────────────────
async def process_message(from_number: str, message_text: str,
                           message_type: str = "text",
                           media_parts: list[dict] | None = None,
                           prosodia: dict[str, str] | None = None,
                           contato: dict[str, str] | None = None) -> None:
    """
    Pipeline completo: memória → Nemotron Omni → ElevenLabs → WhatsApp.
    Executado em background para resposta rápida ao webhook.
    """
    logger.info("Mensagem de %s (%s) [%s]: %s", from_number,
                (contato or {}).get("nome", "?"), message_type, message_text[:80])

    # 1. Recupera contexto do Zep
    memoria = await zep_get_context()

    # 2. Avalia o tom de voz contra a linha de base do paciente
    tom_resumo, desvios = "", []
    if prosodia:
        baseline = await obter_baseline_vocal()
        tom_resumo, desvios = avaliar_desvio_vocal(prosodia, baseline)
        logger.info("Tom de voz: %s", tom_resumo.replace("\n", " | "))

    # 2b. Pedido explícito de memorização: grava ANTES de responder, para
    #     que o agente confirme apenas o que de fato ficou guardado.
    mem_resultado = None
    if _RE_PEDIDO_MEMORIA.search(message_text):
        mem_resultado = await registrar_fatos_da_conversa(
            message_text, "", (contato or {}).get("nome", ""))

    # 3. Gera resposta do Dr. João Holanda (texto + mídia na mesma inferência)
    try:
        resposta = await ask_dr_joao(message_text, memoria, media_parts,
                                     tom_de_voz=tom_resumo, contato=contato,
                                     memoria_resultado=mem_resultado)
    except Exception as e:
        logger.error("Erro na inferência NIM: %s", e)
        resposta = ("Desculpe, meu velho, tive uma dificuldade técnica agora. "
                    "Pode me repetir o que o senhor disse?")

    # 4. A resposta acompanha a modalidade da pergunta: falou ou mostrou,
    #    ouve de volta; escreveu, lê de volta. Texto é sempre o fallback.
    audio = None
    if deve_responder_em_voz(message_type):
        audio = await text_to_speech(resposta)
    if audio:
        await send_audio(from_number, audio)
    else:
        await send_text(from_number, resposta)

    # 5. Salva interação no Zep, incluindo o perfil vocal do momento
    meta = {"de": from_number, "tipo": message_type,
            "quem": (contato or {}).get("nome", "?")}
    if prosodia:
        meta["prosodia"] = prosodia
    await zep_save(message_text, resposta, meta)

    # 6. Registra a evolução do humor como fato clínico datado — é o que
    #    alimenta o item "humor geral do dia" do relatório familiar das 20h.
    if prosodia and (contato or {}).get("papel") == "paciente":
        try:
            from integrations.zep_memory import add_clinical_fact
            await add_clinical_fact(
                f"Tom de voz: {tom_resumo.splitlines()[0]}", "humor")
        except Exception as e:
            logger.warning("Falha ao registrar humor no Zep: %s", e)

    # 7. Rastreia marcadores na resposta — em exames enviados como imagem ou PDF
    #    os valores só aparecem depois que o modelo lê o documento.
    await check_clinical_alerts(resposta, from_number)

    # 8. Exame recebido: extrai os valores em formato estruturado e grava como
    #    fatos datados, permitindo comparar a evolução meses depois.
    if message_type in ("image", "document") and media_parts:
        await registrar_marcadores(media_parts)
    elif mem_resultado is None:   # já gravado acima quando houve pedido explícito
        # Conversa comum: captura sintomas, medicações e eventos relatados
        await registrar_fatos_da_conversa(message_text, resposta,
                                          (contato or {}).get("nome", ""))

    # 9. Sinais vocais persistentes de sofrimento escalam para a família
    if desvios and (contato or {}).get("papel") == "paciente":
        await avaliar_escalonamento_vocal(desvios, tom_resumo)

    logger.info("Resposta enviada para %s (%d chars)", from_number, len(resposta))


# ─── Linha de base vocal e escalonamento ──────────────────────────────────────
_baseline_cache: dict[str, str] | None = None


async def obter_baseline_vocal() -> dict[str, str] | None:
    """
    Recupera o perfil vocal habitual do Sr. Edilson, gravado no Zep pelo
    script setup_voice.py a partir dos áudios de referência. Fica em cache
    porque muda raramente.
    """
    global _baseline_cache
    if _baseline_cache is not None:
        return _baseline_cache or None
    try:
        from integrations.zep_memory import get_facts
        for f in await get_facts("voz_baseline"):
            texto = f.get("fact", "")
            if texto.startswith("BASELINE_VOCAL:"):
                _baseline_cache = _parse_prosodia(texto.split(":", 1)[1].replace(";", "\n"))
                return _baseline_cache
    except Exception as e:
        logger.warning("Falha ao ler baseline vocal: %s", e)
    _baseline_cache = {}
    return None


# Quantas mensagens seguidas com sinal de sofrimento antes de avisar a família.
# Uma nota de voz cansada é normal; três seguidas indicam tendência.
VOZ_ALERTA_LIMIAR = int(os.getenv("VOZ_ALERTA_LIMIAR", "3"))
_historico_desvios: list[str] = []


async def avaliar_escalonamento_vocal(desvios: list[str], resumo: str) -> None:
    """
    Acumula sinais vocais preocupantes e avisa a família quando houver
    persistência — evitando alarmar por causa de um único dia ruim.
    """
    _historico_desvios.append("; ".join(desvios))
    if len(_historico_desvios) > VOZ_ALERTA_LIMIAR:
        _historico_desvios.pop(0)

    if len(_historico_desvios) >= VOZ_ALERTA_LIMIAR:
        # Só zera o histórico se o aviso realmente saiu. Limpar antes faria
        # o acúmulo recomeçar do zero, adiando o alerta por mais três notas
        # de voz sem que ninguém soubesse.
        if await notificar_familia(
                f"💛 Observação sobre o Sr. Edilson\n\n"
                f"Nas últimas {VOZ_ALERTA_LIMIAR} mensagens de voz notei mudança "
                f"no tom dele:\n{resumo}\n\n"
                f"Não é um alerta médico — é um sinal de que talvez valha uma "
                f"ligação ou visita. — Dr. João Holanda",
                critico=False):
            _historico_desvios.clear()


# ─── Extração estruturada de marcadores ──────────────────────────────────────
# A resposta do Dr. João é prosa acolhedora — boa para o Sr. Edilson, ruim para
# cruzar dados meses depois. Aqui os valores do exame são extraídos em formato
# estruturado e gravados como fatos datados, que sobrevivem à janela de
# mensagens do Zep e alimentam a comparação longitudinal.
PROMPT_EXTRACAO = """Extraia os marcadores laboratoriais deste exame.

Responda APENAS com JSON válido, sem cercas de código e sem comentários:
{"data_exame": "AAAA-MM-DD ou null se não constar",
 "laboratorio": "nome ou null",
 "marcadores": [{"nome": "PSA", "valor": 0.12, "unidade": "ng/mL",
                 "referencia": "< 4,0 ou null"}]}

Regras:
- Use ponto como separador decimal.
- Inclua todos os marcadores presentes, não só PSA e eTFG.
- Se não houver nenhum exame legível, devolva {"marcadores": []}."""


def _extrair_json(texto: str) -> dict | None:
    """Lê o JSON da resposta do modelo, tolerando cercas de código."""
    import json
    t = texto.strip()
    if t.startswith("```"):
        t = t.split("```")[1] if "```" in t[3:] else t[3:]
        t = t.removeprefix("json").strip()
    ini, fim = t.find("{"), t.rfind("}")
    if ini == -1 or fim <= ini:
        return None
    try:
        return json.loads(t[ini:fim + 1])
    except Exception as e:
        logger.warning("JSON de extração inválido: %s", e)
        return None


# ─── Diretrizes de comportamento ─────────────────────────────────────────────
# Orientações permanentes dadas pelo administrador — o filho que montou o
# agente — sobre COMO conversar, falar e orientar. Vivem no Zep na categoria
# 'diretriz' e entram no prompt de toda inferência.
CATEGORIA_DIRETRIZ = "diretriz"

PROMPT_CLASSIFICA_DIRETRIZ = """Você recebe uma mensagem enviada ao agente \
Dr. João Holanda pelo administrador do sistema (o filho que o configurou).

Decida se ela é uma DIRETRIZ — instrução permanente sobre como o agente deve \
se comportar, conversar, falar ou orientar daqui em diante — ou apenas uma \
CONVERSA comum, pergunta ou informação clínica pontual.

São diretrizes: "fale mais devagar com ele", "não mencione o câncer a menos \
que ele pergunte", "sempre pergunte da dor no joelho", "evite falar de morte", \
"lembre dele beber água toda tarde", "seja mais breve nas respostas".

NÃO são diretrizes: perguntas sobre exames, relatos de sintoma, pedidos de \
informação, cumprimentos, testes do sistema.

TAMBÉM NÃO são diretrizes — e isto é importante — informações FACTUAIS sobre \
pessoas, mesmo quando pedem para memorizar: "eu moro em Fortaleza", "a Camila \
é enfermeira", "meu pai não gosta de peixe". Isso é memória, guardada por \
outro caminho. Diretriz é sobre COMO se comportar, não sobre O QUE é verdade.

Responda APENAS com JSON válido, sem cercas de código:
{"e_diretriz": true, "texto": "reescreva a diretriz em uma frase clara e \
imperativa, na terceira pessoa, como instrução permanente"}

Se não for diretriz: {"e_diretriz": false}

MENSAGEM:
{mensagem}"""


async def registrar_diretriz(texto: str, autor: str = "") -> bool:
    """Grava uma orientação permanente de comportamento."""
    try:
        from integrations.zep_memory import add_clinical_fact
    except Exception as e:
        logger.warning("Zep indisponível para gravar diretriz: %s", e)
        return False

    marca = f" (orientação de {autor})" if autor else ""
    ok = await add_clinical_fact(texto.strip() + marca, CATEGORIA_DIRETRIZ)
    if ok:
        logger.info("Diretriz registrada: %s", texto[:80])
    return ok


async def listar_diretrizes() -> list[dict]:
    """Devolve as diretrizes ativas, da mais antiga para a mais recente."""
    try:
        from integrations.zep_memory import get_facts
        d = await get_facts(CATEGORIA_DIRETRIZ)
        d.sort(key=lambda f: f.get("registrado_em", ""))
        return d
    except Exception as e:
        logger.warning("Falha ao ler diretrizes: %s", e)
        return []


async def remover_diretriz(indice: int) -> str | None:
    """Remove a diretriz de número `indice` (base 1). Devolve o texto removido."""
    try:
        from integrations.zep_memory import remove_clinical_fact
        return await remove_clinical_fact(CATEGORIA_DIRETRIZ, indice)
    except Exception as e:
        logger.warning("Falha ao remover diretriz: %s", e)
        return None


async def bloco_de_diretrizes() -> str:
    """Formata as diretrizes para entrarem no system prompt."""
    d = await listar_diretrizes()
    if not d:
        return "(Nenhuma orientação específica registrada até agora.)"
    return "\n".join(f"{i}. {f.get('fact','')}" for i, f in enumerate(d, 1))


async def interpretar_como_diretriz(mensagem: str) -> str | None:
    """
    Decide se a mensagem do administrador é uma diretriz permanente.
    Devolve o texto normalizado da diretriz, ou None se for conversa comum.
    """
    if len(mensagem.strip()) < 10:
        return None
    try:
        bruto = await chamar_modelo(
            "", PROMPT_CLASSIFICA_DIRETRIZ.replace("{mensagem}", mensagem[:2000]),
            None, max_tokens=300, temperature=0.0, reasoning=False)
    except Exception as e:
        logger.warning("Falha ao classificar diretriz: %s", e)
        return None

    dados = _extrair_json(bruto)
    if not dados or not dados.get("e_diretriz"):
        return None
    texto = (dados.get("texto") or "").strip()
    return texto or None


PROMPT_FATOS_CONVERSA = """Você mantém a memória de longo prazo do Dr. João \
Holanda. Leia a conversa abaixo e identifique informações NOVAS e objetivas que \
valham ser lembradas nas próximas conversas.

QUEM ESTÁ FALANDO: {quem}

GUARDE (sobre o paciente Sr. Edilson):
- sintoma relatado, com intensidade e localização
- medicamento iniciado, suspenso, esquecido ou com efeito colateral
- peso, pressão, consulta marcada ou remarcada
- mudança de rotina, alimentação ou atividade
- evento relevante: queda, viagem, internação

GUARDE TAMBÉM (sobre as pessoas em volta e o contexto):
- onde alguém mora ou trabalha, mudança de cidade
- grau de parentesco, quem cuida do quê, quem acompanha nas consultas
- preferências, gostos, times, programas, assuntos que animam a conversa
- correções que alguém fizer sobre um dado errado
- qualquer coisa que a pessoa peça explicitamente para memorizar, lembrar,
  guardar ou anotar — isso SEMPRE deve ser guardado, sem exceção

NÃO guarde: cumprimentos, conversa fiada, o que o médico recomendou, \
informação que já é conhecida, suposições suas.

Escreva cada fato em terceira pessoa, com o NOME da pessoa, para que faça \
sentido isolado meses depois. "Erick mora em Fortaleza", não "ele mora lá".
Se nada novo apareceu, devolva lista vazia.

Responda APENAS com JSON válido, sem cercas de código:
{"fatos": [{"texto": "Erick, filho do Sr. Edilson, mora em Fortaleza (CE)", \
"categoria": "familia"}]}

Categorias válidas: sintoma, medicamento, consulta, rotina, alimentacao, \
evento, familia, perfil, preferencia.

CONVERSA:
{quem}: {mensagem}
Dr. João Holanda: {resposta}"""

# Pedidos explícitos de memorização não podem depender do julgamento do
# extrator: se a pessoa pediu para guardar, guarda-se.
_RE_PEDIDO_MEMORIA = re.compile(
    r"\b(memoriz\w+|lembr[ae]\w*|guard[ae]\w*|anot[ae]\w*|"
    r"n[ãa]o esque[çc]\w*|registr[ae]\w*)\b", re.IGNORECASE)


async def registrar_fatos_da_conversa(mensagem: str, resposta: str = "",
                                      quem: str = "") -> dict:
    """
    Extrai da conversa o que vale lembrar e grava como fatos datados: dados
    clínicos do paciente e também o contexto das pessoas em volta — onde
    moram, quem cuida do quê, correções, preferências.

    É o que permite cruzar, meses depois, uma queixa de hoje com um marcador
    da ficha — sem depender de reler a conversa inteira em prosa.
    """
    pedido_explicito = bool(_RE_PEDIDO_MEMORIA.search(mensagem))
    # Mensagem curta normalmente é cumprimento; mas se pediram para guardar,
    # guarda-se de qualquer forma.
    if len(mensagem.strip()) < 8 and not pedido_explicito:
        return []

    prompt = (PROMPT_FATOS_CONVERSA
              .replace("{quem}", quem or "Pessoa não identificada")
              .replace("{mensagem}", mensagem[:3000])
              .replace("{resposta}", resposta[:2000]))
    if pedido_explicito:
        prompt += ("\n\nATENÇÃO: a pessoa pediu explicitamente para memorizar "
                   "algo nesta mensagem. Extraia esse fato obrigatoriamente, "
                   "mesmo que pareça banal.")

    try:
        bruto = await chamar_modelo("", prompt, None, max_tokens=600,
                                    temperature=0.0, reasoning=False)
    except Exception as e:
        logger.warning("Falha ao extrair fatos da conversa: %s", e)
        return {"ok": False, "gravados": [], "erro": "falha na extração"}

    dados = _extrair_json(bruto)
    fatos = [f for f in ((dados or {}).get("fatos") or [])
             if isinstance(f, dict) and f.get("texto")]
    if not fatos:
        return {"ok": True, "gravados": [], "nada_novo": True}

    try:
        from integrations.zep_memory import add_clinical_fact
    except Exception as e:
        logger.warning("Zep indisponível para gravar fatos: %s", e)
        return {"ok": False, "gravados": [], "erro": "memória indisponível"}

    origem = f" (relatado por {quem})" if quem and quem != "?" else ""
    gravados: list[str] = []
    for f in fatos:
        categoria = f.get("categoria", "clinico")
        if await add_clinical_fact(f["texto"] + origem, categoria):
            gravados.append(f["texto"])
            logger.info("Fato da conversa [%s]: %s", categoria, f["texto"][:70])
        else:
            logger.warning("NÃO gravado [%s]: %s", categoria, f["texto"][:70])

    logger.info("Conversa: %d/%d fato(s) novos gravados.", len(gravados), len(fatos))
    return {"ok": len(gravados) == len(fatos), "gravados": gravados,
            "tentados": len(fatos)}


async def registrar_marcadores(media_parts: list[dict]) -> list[dict]:
    """
    Relê o exame pedindo saída estruturada e grava cada marcador como fato
    datado. Devolve os marcadores encontrados.
    """
    if not media_parts:
        return []
    try:
        bruto = await chamar_modelo("", PROMPT_EXTRACAO, media_parts,
                                    max_tokens=800, temperature=0.0,
                                    reasoning=False)
    except Exception as e:
        logger.warning("Falha na extração de marcadores: %s", e)
        return []

    dados = _extrair_json(bruto)
    if not dados:
        return []

    marcadores = dados.get("marcadores") or []
    if not marcadores:
        logger.info("Nenhum marcador laboratorial encontrado no documento.")
        return []

    data_exame = dados.get("data_exame") or datetime.now(timezone.utc).date().isoformat()
    lab = dados.get("laboratorio") or ""

    try:
        from integrations.zep_memory import add_clinical_fact
    except Exception as e:
        logger.warning("Zep indisponível para gravar marcadores: %s", e)
        return marcadores

    gravados = 0
    for m in marcadores:
        nome, valor = m.get("nome"), m.get("valor")
        if not nome or valor is None:
            continue
        unidade = m.get("unidade") or ""
        ref = f" (ref: {m['referencia']})" if m.get("referencia") else ""
        origem = f" — {lab}" if lab else ""
        fato = f"{nome}: {valor} {unidade}".strip() + f" em {data_exame}{ref}{origem}"
        if await add_clinical_fact(fato, "exame"):
            gravados += 1
            logger.info("Marcador registrado: %s", fato)

    logger.info("Exame de %s: %d/%d marcadores gravados na memória.",
                data_exame, gravados, len(marcadores))
    return marcadores


def _em_background(corotina, descricao: str) -> None:
    """
    Dispara uma tarefa em segundo plano registrando qualquer exceção.

    asyncio.create_task() sozinho engole a falha: se process_message estourar,
    o paciente fica sem resposta e nada aparece no log além de um aviso
    genérico do laço de eventos, quando aparece.
    """
    tarefa = asyncio.create_task(corotina)

    def _ao_terminar(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        erro = t.exception()
        if erro:
            logger.error("Falha em segundo plano (%s): %r", descricao, erro,
                         exc_info=erro)

    tarefa.add_done_callback(_ao_terminar)


# ─── Detecção de alertas clínicos ────────────────────────────────────────────
PSA_PATTERN   = re.compile(r'PSA[:\s]+(\d+[\.,]\d+)', re.IGNORECASE)
ETFG_PATTERN  = re.compile(r'eTFG[:\s]+(\d+)', re.IGNORECASE)

# Evita alertar a família duas vezes pelo mesmo valor: a checagem roda tanto
# na mensagem recebida quanto na resposta (exames em imagem/PDF).
_alertas_enviados: set[str] = set()


async def notificar_familia(mensagem: str, critico: bool = True) -> bool:
    """
    Entrega um aviso à família por WhatsApp e, se falhar, pelo Telegram.

    Um alerta clínico que não chega é pior que nenhum alerta: a família
    acredita que seria avisada e não é. Por isso há canal reserva, e a falha
    total vira registro CRITICAL — não um retorno silencioso.
    """
    entregue = False
    canais: list[str] = []

    if FAMILY_GROUP_ID:
        if await send_text(FAMILY_GROUP_ID, mensagem):
            entregue = True
            canais.append("WhatsApp")

    # Telegram: destinatários da família e, na falta deles, os administradores.
    # Enquanto o WhatsApp não estiver pareado, este é o caminho principal.
    destinatarios = TELEGRAM_FAMILIA_IDS or TELEGRAM_ADMIN_IDS
    if not entregue and destinatarios:
        canal = getattr(app.state, "telegram", None)
        if canal:
            for chat_id in destinatarios:
                try:
                    await canal.enviar_texto(chat_id, mensagem)
                    entregue = True
                    canais.append(f"Telegram/{chat_id}")
                except Exception as e:
                    logger.error("Telegram falhou para %s: %s", chat_id, e)
        else:
            logger.error("Canal Telegram indisponível para entregar o alerta.")

    if entregue:
        logger.info("Aviso à família entregue via %s", ", ".join(canais))
    elif critico:
        logger.critical(
            "ALERTA CLÍNICO NÃO ENTREGUE — nenhum canal disponível. "
            "Conteúdo que se perderia: %s", mensagem.replace("\n", " | ")[:300])
    else:
        logger.warning("Aviso não entregue (nenhum canal): %s",
                       mensagem.replace("\n", " | ")[:150])
    return entregue


async def check_clinical_alerts(text: str, from_number: str) -> None:
    """Detecta valores críticos de PSA/eTFG e notifica a família."""
    if not FAMILY_GROUP_ID and not TELEGRAM_FAMILIA_IDS and not TELEGRAM_ADMIN_IDS:
        return

    if m := PSA_PATTERN.search(text):
        psa = float(m.group(1).replace(",", "."))
        chave = f"psa:{psa}"
        if psa > 0.20 and chave not in _alertas_enviados:
            # A chave só é marcada APÓS a entrega. Marcá-la antes fazia um
            # envio falho suprimir o alerta para sempre — ele nunca seria
            # tentado de novo, porque constaria como já enviado.
            if await notificar_familia(
                    f"🚨 ALERTA MÉDICO — Sr. Edilson\n"
                    f"PSA: {psa} (acima do limite de 0,20)\n"
                    f"Dr. João Holanda recomenda contato urgente com urologista."):
                _alertas_enviados.add(chave)

    if m := ETFG_PATTERN.search(text):
        etfg = int(m.group(1))
        chave = f"etfg:{etfg}"
        if etfg < 30 and chave not in _alertas_enviados:
            if await notificar_familia(
                    f"🚨 ALERTA RENAL — Sr. Edilson\n"
                    f"eTFG: {etfg} (estadiamento grave)\n"
                    f"Dr. João Holanda recomenda contato urgente com nefrologista."):
                _alertas_enviados.add(chave)


# ─── Canal de administração ──────────────────────────────────────────────────
AJUDA_ADMIN = """Comandos de administração do Dr. João Holanda:

/orientar <texto>   grava uma orientação permanente de comportamento
/diretrizes         lista as orientações ativas
/esquecer <número>  remove a orientação daquele número
/ajuda              mostra esta lista

Você também pode simplesmente escrever a orientação em linguagem natural —
"fale mais devagar com ele", "sempre pergunte do joelho" — que eu reconheço
e guardo. Perguntas e conversas normais seguem o fluxo de sempre.

As orientações valem para todas as conversas, em todos os canais. Elas não
podem contrariar as restrições clínicas da ficha nem a regra de honestidade."""


async def tratar_comando_admin(texto: str, quem: str = "") -> str | None:
    """
    Processa mensagens do administrador. Devolve a resposta quando a mensagem
    for administrativa, ou None para que siga ao fluxo clínico normal.
    """
    t = texto.strip()
    baixo = t.lower()

    if baixo in ("/ajuda", "/help", "/start", "/comandos"):
        return AJUDA_ADMIN

    if baixo.startswith("/diretrizes"):
        d = await listar_diretrizes()
        if not d:
            return ("Nenhuma orientação registrada ainda.\n\n"
                    "Envie /orientar seguido do texto, ou escreva a orientação "
                    "naturalmente que eu reconheço.")
        linhas = "\n".join(f"{i}. {f.get('fact','')}" for i, f in enumerate(d, 1))
        return f"Orientações ativas ({len(d)}):\n\n{linhas}\n\nPara remover: /esquecer <número>"

    if baixo.startswith("/esquecer"):
        partes = t.split(maxsplit=1)
        if len(partes) < 2 or not partes[1].strip().isdigit():
            return "Use: /esquecer <número>. Veja os números em /diretrizes."
        removida = await remover_diretriz(int(partes[1].strip()))
        if removida is None:
            return "Não encontrei uma orientação com esse número. Confira /diretrizes."
        return f"Esquecido:\n\n{removida}"

    if baixo.startswith("/orientar"):
        partes = t.split(maxsplit=1)
        if len(partes) < 2 or not partes[1].strip():
            return "Use: /orientar seguido da orientação. Ex.: /orientar fale mais devagar."
        if await registrar_diretriz(partes[1].strip(), quem):
            return (f"Anotado. A partir de agora:\n\n{partes[1].strip()}\n\n"
                    f"Vale para todas as conversas. Veja tudo em /diretrizes.")
        return "Não consegui gravar a orientação — a memória pode estar fora do ar."

    # Sem comando: verifica se a mensagem é uma orientação em linguagem natural
    if t.startswith("/"):
        return f"Comando não reconhecido.\n\n{AJUDA_ADMIN}"

    diretriz = await interpretar_como_diretriz(t)
    if diretriz:
        if await registrar_diretriz(diretriz, quem):
            return (f"Entendi como orientação e guardei:\n\n{diretriz}\n\n"
                    f"Se não era essa a intenção, use /esquecer para remover. "
                    f"Veja todas em /diretrizes.")
        return None

    return None  # conversa comum: segue o fluxo clínico


# ─── Handler compartilhado entre os canais ────────────────────────────────────
async def responder(texto: str, media_parts: list[dict], tipo: str,
                    chat_id: int | None = None, nome: str = "") -> str:
    """
    Núcleo do atendimento, usado por Telegram, chat web e WhatsApp:
    memória → inferência multimodal → persistência → alertas clínicos.

    chat_id e nome identificam quem está falando. Sem eles o agente
    presumia estar diante do paciente, e tratava um filho como Sr. Edilson.
    """
    contato = (identificar_contato_telegram(chat_id, nome)
               if chat_id is not None else None)
    if contato:
        logger.info("Mensagem de %s (%s) [%s]", contato["nome"],
                    contato["papel"], tipo)

    memoria = await zep_get_context()

    # Pedido explícito de memorização: grava ANTES de responder, para que o
    # agente confirme apenas o que de fato ficou guardado.
    mem_resultado = None
    if _RE_PEDIDO_MEMORIA.search(texto):
        mem_resultado = await registrar_fatos_da_conversa(
            texto, "", (contato or {}).get("nome", ""))

    resposta = await ask_dr_joao(texto, memoria, media_parts or None,
                                 contato=contato,
                                 memoria_resultado=mem_resultado)
    await zep_save(texto, resposta,
                   {"tipo": tipo, "quem": (contato or {}).get("nome", "?")})
    await check_clinical_alerts(resposta, "canal")

    # Exame recebido: grava os valores em formato estruturado, para que a
    # evolução possa ser comparada meses depois. Conversa comum: captura
    # sintomas, medicações e eventos relatados.
    if tipo in ("image", "document") and media_parts:
        await registrar_marcadores(media_parts)
    elif mem_resultado is None:   # já gravado acima quando houve pedido explícito
        await registrar_fatos_da_conversa(texto, resposta,
                                          (contato or {}).get("nome", ""))
    return resposta


# ─── Relatório diário da família ───────────────────────────────────────────────
# CLAUDE.md §5: resumo às 20h (horário de Brasília) pros filhos, todo dia. O
# agente já roda como processo de vida longa — mesmo motivo do polling do
# Telegram — então um laço que dorme até o próximo horário faz o papel do
# cron do n8n sem precisar manter um segundo sistema fora do git, com
# workflow para importar e credencial para configurar à parte.
HORA_RELATORIO_FAMILIA = int(os.getenv("HORA_RELATORIO_FAMILIA", "20"))
# Brasil não observa horário de verão desde 2019 — offset fixo é seguro aqui
# e evita depender do pacote tzdata (ausente na imagem slim) só por causa
# disso. Se a lei mudar de novo, é um número pra trocar, não uma migração.
TZ_BRASILIA   = timezone(timedelta(hours=-3))
TZ_PARINTINS  = timezone(timedelta(hours=-4))   # define o que é "hoje" pro paciente


async def montar_relatorio_diario() -> str | None:
    """
    Monta o texto do resumo diário em linguagem natural a partir SOMENTE dos
    fatos estruturados registrados hoje no Zep — nunca da conversa crua, para
    não vazar algo fora de contexto nem inventar o que não foi dito.

    Devolve None quando não há nada registrado hoje: não faz sentido mandar
    um resumo vazio pra família todo santo dia.
    """
    from integrations.zep_memory import generate_daily_summary
    hoje = datetime.now(TZ_PARINTINS).date().isoformat()
    resumo = await generate_daily_summary(hoje)

    if not resumo["total_fatos"]:
        return None

    linhas = "\n".join(
        f"[{categoria}] " + "; ".join(fatos)
        for categoria, fatos in resumo["fatos_por_categoria"].items()
    )

    prompt = (
        "Monte o resumo diário do Sr. Edilson para os filhos dele (Erick, "
        "Camila e Junior), com base SOMENTE nos fatos abaixo, registrados "
        "hoje — não invente nada que não esteja aqui:\n\n"
        f"{linhas}\n\n"
        "Tom: direto e objetivo. É comunicação com os filhos sobre o pai "
        "deles, não conversa com o paciente — sem sotaque cearense, sem "
        "humor, sem vocativo afetuoso. Cubra só o que houver dado de "
        "verdade: humor do dia, sintoma relatado, medicamento confirmado, "
        "alimentação, variação de exame. Não force um tópico sem dado — "
        "se só houver uma coisa registrada hoje, o resumo é sobre essa "
        "coisa só. Termine com uma recomendação objetiva para o dia "
        "seguinte, apenas se fizer sentido. Parágrafos curtos."
    )
    try:
        return await chamar_modelo("", prompt, None, max_tokens=700,
                                   temperature=0.3, reasoning=False)
    except Exception as e:
        logger.error("Falha ao montar o relatório diário: %s", e)
        return None


async def enviar_relatorio_diario() -> bool:
    """Monta e entrega o resumo diário pelos canais de família (grupo/filhos)."""
    texto = await montar_relatorio_diario()
    if not texto:
        logger.info("Relatório diário: nada registrado hoje — nenhuma mensagem enviada.")
        return False
    entregue = await notificar_familia(
        f"📋 Resumo do dia — Sr. Edilson\n\n{texto}", critico=False)
    logger.info("Relatório diário: %s", "entregue" if entregue else "FALHOU na entrega")
    return entregue


async def loop_relatorio_diario() -> None:
    """
    Dorme até o próximo HORA_RELATORIO_FAMILIA (Brasília) e dispara o resumo,
    todo dia, enquanto o processo estiver de pé.
    """
    while True:
        agora = datetime.now(TZ_BRASILIA)
        proximo = agora.replace(hour=HORA_RELATORIO_FAMILIA, minute=0,
                                second=0, microsecond=0)
        if proximo <= agora:
            proximo += timedelta(days=1)
        espera = (proximo - agora).total_seconds()
        logger.info("Relatório diário: próximo envio %s (em %.0f min)",
                    proximo.isoformat(), espera / 60)
        try:
            await asyncio.sleep(espera)
            await enviar_relatorio_diario()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Nunca deixa o laço morrer por uma falha de uma rodada — senão
            # o relatório para de sair para sempre até o próximo restart.
            logger.error("Laço do relatório diário falhou: %s", e)
            await asyncio.sleep(300)


# ─── FastAPI app ──────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Dr. João Holanda Agent iniciando...")
    logger.info("Evolution API: %s | Zep: %s", EVOLUTION_API_URL, ZEP_API_URL)

    # Inicializa memória do paciente no Zep
    try:
        from integrations.zep_memory import initialize_patient_knowledge
        await initialize_patient_knowledge()
        logger.info("Conhecimento do Sr. Edilson carregado no Zep ✓")
    except Exception as e:
        logger.warning("Zep init skip: %s", e)

    # Canal Telegram em long polling, se houver token configurado
    tarefa_telegram = None
    if TELEGRAM_BOT_TOKEN:
        from telegram_channel import TelegramChannel
        canal = TelegramChannel(
            token=TELEGRAM_BOT_TOKEN,
            allowed_ids=TELEGRAM_ALLOWED_IDS,
            handler=responder,
            build_media_part=build_media_part,
            blocos_de_documento=blocos_de_documento,
            limpar_texto=limpar_para_texto,
            responder_em_voz=deve_responder_em_voz,
            admin_ids=TELEGRAM_ADMIN_IDS,
            tratar_admin=tratar_comando_admin,
            to_wav=ogg_to_wav,
            to_ogg=to_ogg_opus,
            tts=text_to_speech,
            transcrever=transcribe_audio,
        )
        app.state.telegram = canal
        tarefa_telegram = asyncio.create_task(canal.rodar())
        logger.info("Canal Telegram iniciando (%d contatos autorizados, "
                    "%d administrador(es))...",
                    len(TELEGRAM_ALLOWED_IDS), len(TELEGRAM_ADMIN_IDS))
    else:
        logger.info("TELEGRAM_BOT_TOKEN ausente — canal Telegram desativado.")

    # Relatório diário da família — laço próprio, não depende do n8n
    tarefa_relatorio = asyncio.create_task(loop_relatorio_diario())
    logger.info("Relatório diário agendado para %dh (horário de Brasília).",
                HORA_RELATORIO_FAMILIA)

    yield

    if tarefa_telegram:
        app.state.telegram.parar()
        tarefa_telegram.cancel()
        try:
            await tarefa_telegram
        except (asyncio.CancelledError, Exception):
            pass
    tarefa_relatorio.cancel()
    try:
        await tarefa_relatorio
    except (asyncio.CancelledError, Exception):
        pass
    logger.info("Dr. João Holanda Agent encerrando.")


app = FastAPI(
    title="Dr. João Holanda Cavalcante — Agente de Saúde",
    description="Agente IA para acompanhamento longitudinal do Sr. Edilson",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    """
    Estado do agente. Inclui os recursos ativos para que dê para saber, sem
    entrar no container, qual versão do código está de fato rodando.
    """
    try:
        import fitz  # noqa: F401
        leitura_pdf = True
    except ImportError:
        leitura_pdf = False

    return {
        "status": "ok",
        "agent": "Dr. João Holanda Cavalcante",
        "paciente": "Sr. Edilson — Parintins, AM",
        "provedor": PROVIDER,
        "modelo": MODELO_ATUAL,
        "credencial_ok": llm_configurado(),
        "base_conhecimento": resumo_conhecimento(),
        "recursos": {
            "leitura_pdf": suporta_pdf_nativo() or leitura_pdf,
            "pdf_nativo": suporta_pdf_nativo(),
            "chat_web": True,
            "telegram": bool(TELEGRAM_BOT_TOKEN),
            "whatsapp": bool(EVOLUTION_API_KEY),
            "voz_elevenlabs": bool(ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID),
            "memoria_zep": bool(ZEP_API_KEY),
            "alertas_whatsapp": bool(FAMILY_GROUP_ID),
            "alertas_telegram": bool(TELEGRAM_FAMILIA_IDS or TELEGRAM_ADMIN_IDS),
            "autenticacao": bool(AGENT_ACCESS_TOKEN),
        },
    }


# ─── QR Code do WhatsApp ──────────────────────────────────────────────────────
# Servido pelo próprio agente porque a porta dele já é publicada pelo Docker
# (que escreve regras de iptables), enquanto um servidor HTTP avulso numa
# porta nova fica bloqueado pelo firewall do host.
async def _buscar_qr() -> dict:
    """Consulta a Evolution API pelo QR Code / código de pareamento atual."""
    async with httpx.AsyncClient(timeout=20) as client:
        estado = {}
        try:
            r = await client.get(
                f"{EVOLUTION_API_URL}/instance/connectionState/{WHATSAPP_INSTANCE}",
                headers={"apikey": EVOLUTION_API_KEY})
            if r.status_code == 200:
                estado = r.json()
        except Exception as e:
            logger.warning("Falha ao ler estado da instância: %s", e)

        if (estado.get("instance") or {}).get("state") == "open":
            return {"conectado": True}

        r = await client.get(
            f"{EVOLUTION_API_URL}/instance/connect/{WHATSAPP_INSTANCE}",
            headers={"apikey": EVOLUTION_API_KEY})
        if r.status_code != 200:
            return {"erro": f"HTTP {r.status_code}: {r.text[:200]}"}

        d = r.json()
        qr = d.get("qrcode", {}) if isinstance(d.get("qrcode"), dict) else {}
        return {
            "conectado": False,
            "base64": d.get("base64") or qr.get("base64", ""),
            "code": d.get("code") or qr.get("code", ""),
            "pairingCode": d.get("pairingCode") or qr.get("pairingCode", ""),
        }


@app.get("/qrcode.png")
async def qrcode_png(request: Request):
    """Devolve o QR Code como imagem PNG, para abrir direto no navegador."""
    require_token(request)
    info = await _buscar_qr()
    b64 = info.get("base64", "")
    if not b64:
        raise HTTPException(status_code=404,
                            detail="QR indisponível (instância já conectada?)")
    try:
        png = base64.b64decode(b64.split(",", 1)[-1])
    except Exception:
        raise HTTPException(status_code=500, detail="QR em formato inesperado")
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.get("/qrcode", response_class=HTMLResponse)
async def qrcode_page(request: Request):
    """
    Página que exibe o QR Code e se atualiza sozinha a cada 30 segundos —
    o QR do WhatsApp expira em cerca de um minuto.
    """
    require_token(request)
    info = await _buscar_qr()

    if info.get("conectado"):
        corpo = ("<h1>✅ WhatsApp conectado</h1>"
                 "<p>O Dr. João Holanda já está no ar e pode receber mensagens.</p>")
        refresh = ""
    elif info.get("erro"):
        corpo = f"<h1>⚠️ Erro</h1><pre>{info['erro']}</pre>"
        refresh = '<meta http-equiv="refresh" content="10">'
    else:
        pareamento = ""
        if info.get("pairingCode"):
            pareamento = (f"<p class='alt'>Ou use o código de pareamento: "
                          f"<code>{info['pairingCode']}</code><br>"
                          f"<small>WhatsApp → Aparelhos conectados → "
                          f"Conectar com número de telefone</small></p>")
        # Repassa o token à imagem, já que ela também é protegida
        tk = request.query_params.get("token", "")
        img_src = f"/qrcode.png?token={tk}" if tk else "/qrcode.png"
        corpo = (f"<h1>Conectar o WhatsApp</h1>"
                 f"<img src='{img_src}' alt='QR Code'>"
                 f"<p><small>Atualiza sozinho a cada 30s — o QR expira em ~1 min.</small></p>"
                 f"{pareamento}")
        refresh = '<meta http-equiv="refresh" content="30">'

    return f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{refresh}<title>Dr. João Holanda — Conectar WhatsApp</title>
<style>
  body {{ font-family: system-ui, sans-serif; text-align: center;
         padding: 2rem 1rem; background: #0f1115; color: #e6e6e6; }}
  h1 {{ font-size: 1.4rem; font-weight: 600; }}
  img {{ width: min(320px, 80vw); background: #fff; padding: 12px;
        border-radius: 12px; margin: 1rem 0; }}
  code {{ background: #1e2128; padding: 4px 10px; border-radius: 6px;
         font-size: 1.2rem; letter-spacing: 2px; }}
  .alt {{ margin-top: 1.5rem; color: #b8b8b8; }}
  small {{ color: #8a8a8a; }}
  pre {{ text-align: left; background: #1e2128; padding: 1rem;
        border-radius: 8px; overflow-x: auto; }}
</style></head>
<body>{corpo}
<p><small>Sr. Edilson — Parintins, AM</small></p>
</body></html>"""


@app.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request):
    """
    Recebe webhooks da Evolution API e processa mensagens do Sr. Edilson.
    Responde com HTTP 200 imediatamente; processamento ocorre em background.
    """
    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON inválido")

    msg = body.get("data", body.get("message", {}))
    event = body.get("event", "")

    # Filtra eventos relevantes
    if event not in ("messages.upsert", "MESSAGES_UPSERT", ""):
        return JSONResponse({"status": "ignored", "event": event})

    # Extrai campos da mensagem
    key     = msg.get("key", {})
    from_me = key.get("fromMe", False)
    if from_me:
        return JSONResponse({"status": "skip_own"})

    from_number = key.get("remoteJid", "")
    if not from_number:
        return JSONResponse({"status": "no_sender"})

    # Filtro de contatos: o agente atende exclusivamente o Sr. Edilson e os
    # três filhos. Qualquer outro número é ignorado em silêncio — sem resposta,
    # sem processamento, sem custo de inferência.
    contato = identificar_contato(from_number)
    if not contato:
        logger.info("Mensagem ignorada — remetente não autorizado: %s", from_number)
        return JSONResponse({"status": "sender_not_allowed"})

    message_obj = msg.get("message", {})
    message_type = "text"
    message_text = ""
    media_parts: list[dict] = []
    prosodia: dict[str, str] = {}

    # Texto simples
    if "conversation" in message_obj:
        message_text = message_obj["conversation"]
    elif "extendedTextMessage" in message_obj:
        message_text = message_obj["extendedTextMessage"].get("text", "")

    # Áudio (PTT / nota de voz) — transcrito E enviado ao modelo,
    # para que o tom de voz também informe a leitura emocional
    elif "audioMessage" in message_obj or "pttMessage" in message_obj:
        message_type = "audio"
        audio_msg = message_obj.get("audioMessage") or message_obj.get("pttMessage", {})
        raw = await download_media(audio_msg.get("url", ""))
        if not raw:
            return JSONResponse({"status": "media_download_failed"})

        wav = await ogg_to_wav(raw)
        if wav:
            media_parts.append(build_media_part("audio", wav, "audio/wav"))
            # Transcrição e prosódia em paralelo — ambas leem o mesmo WAV
            message_text, prosodia = await asyncio.gather(
                transcribe_audio(raw), analisar_tom_de_voz(wav))
        else:
            message_text = await transcribe_audio(raw)
        logger.info("Áudio transcrito: %s", message_text[:80])

    # Imagem — foto de refeição ou de exame impresso
    elif "imageMessage" in message_obj:
        message_type = "image"
        img = message_obj["imageMessage"]
        caption = img.get("caption", "")
        raw = await download_media(img.get("url", ""))
        if not raw:
            return JSONResponse({"status": "media_download_failed"})

        mime = img.get("mimetype", "image/jpeg").split(";")[0]
        media_parts.append(build_media_part("image", raw, mime))
        message_text = (f"O Sr. Edilson enviou uma foto"
                        f"{' com a legenda: ' + caption if caption else ''}. "
                        f"Analise a imagem: se for uma refeição, avalie do ponto de "
                        f"vista nutricional considerando as restrições renais e "
                        f"oncológicas dele; se for um exame, leia os valores e "
                        f"interprete-os.")

    # Vídeo — o Omni analisa os quadros diretamente
    elif "videoMessage" in message_obj:
        message_type = "video"
        vid = message_obj["videoMessage"]
        caption = vid.get("caption", "")
        raw = await download_media(vid.get("url", ""))
        if not raw:
            return JSONResponse({"status": "media_download_failed"})

        mime = vid.get("mimetype", "video/mp4").split(";")[0]
        media_parts.append(build_media_part("video", raw, mime))
        message_text = (f"O Sr. Edilson enviou um vídeo"
                        f"{' com a legenda: ' + caption if caption else ''}. "
                        f"Observe o que acontece e comente com acolhimento.")

    # Documento — PDFs de exames são convertidos em imagem pela Evolution API
    elif "documentMessage" in message_obj:
        message_type = "document"
        doc = message_obj["documentMessage"]
        caption = doc.get("caption", "") or doc.get("fileName", "")
        raw = await download_media(doc.get("url", ""))
        if not raw:
            return JSONResponse({"status": "media_download_failed"})

        mime = doc.get("mimetype", "application/pdf").split(";")[0]
        # PDF vira uma imagem por página; imagem segue direto
        media_parts.extend(blocos_de_documento(raw, mime))
        if not media_parts:
            return JSONResponse({"status": "documento_ilegivel"})
        message_text = (f"O Sr. Edilson enviou o documento '{caption}' "
                        f"({len(media_parts)} página(s)). "
                        f"Leia o conteúdo. Se for um exame laboratorial, extraia "
                        f"os marcadores (PSA, eTFG, creatinina) com seus valores e "
                        f"interprete-os segundo as regras clínicas.")
    else:
        return JSONResponse({"status": "unsupported_type"})

    if not message_text.strip():
        return JSONResponse({"status": "empty_message"})

    # Verifica alertas clínicos
    _em_background(check_clinical_alerts(message_text, from_number),
                   "alertas clínicos")

    # Processa em background (resposta imediata ao webhook)
    _em_background(
        process_message(from_number, message_text, message_type, media_parts,
                        prosodia, contato),
        f"atendimento de {from_number}")

    return JSONResponse({"status": "processing"})


@app.post("/webhook/camera")
async def camera_webhook(request: Request):
    """
    Recebe alertas da câmera Intelbras Mibo (quedas, ausência, refeições).
    Notifica família e registra evento no Zep.
    """
    body = await request.json()
    event_type = body.get("event", "unknown")
    description = body.get("description", "Evento detectado pela câmera.")

    if event_type in ("fall_detected", "immobility"):
        alerta = (f"🚨 ALERTA DE SEGURANÇA — Sr. Edilson\n"
                  f"Câmera cozinha detectou: {description}\n"
                  f"Por favor, verifiquem imediatamente.")
        entregue = await notificar_familia(alerta)
        await zep_save(
            f"[CÂMERA] {description}",
            "Alerta entregue à família." if entregue
            else "FALHA AO ENTREGAR o alerta à família.",
            {"tipo": "emergencia", "entregue": entregue})
        # Quem chamou (a câmera) precisa saber se o aviso chegou, para
        # poder tentar outro caminho — SMS, ligação — se não chegou.
        return JSONResponse({"status": "ok" if entregue else "alerta_nao_entregue",
                             "event": event_type, "entregue": entregue},
                            status_code=200 if entregue else 502)

    return JSONResponse({"status": "ok", "event": event_type})


# ─── Canal alternativo: chat web ─────────────────────────────────────────────
# Permite conversar com o Dr. João Holanda sem WhatsApp — útil para a família,
# para testar antes do pareamento e como contingência se o WhatsApp cair.
# Aceita texto, nota de voz e imagem, passando pelo mesmo pipeline multimodal.
@app.post("/chat/mensagem")
async def chat_mensagem(request: Request):
    """
    Recebe uma mensagem pelo chat web e devolve a resposta do Dr. João Holanda.
    Corpo: {"texto": "...", "midia": {"tipo": "audio|image|video",
                                      "base64": "...", "mime": "..."}}
    Diferente do WhatsApp, responde de forma síncrona — quem enviou espera a resposta.
    """
    require_token(request)
    body = await request.json()
    texto = (body.get("texto") or "").strip()
    midia = body.get("midia") or {}

    media_parts: list[dict] = []
    tipo = "texto"

    if midia.get("base64"):
        kind = midia.get("tipo", "image")
        if kind not in MEDIA_PART_TYPES:
            raise HTTPException(status_code=400, detail=f"Tipo de mídia inválido: {kind}")
        try:
            raw = base64.b64decode(midia["base64"].split(",", 1)[-1])
        except Exception:
            raise HTTPException(status_code=400, detail="Mídia em base64 inválida")

        tipo = kind
        mime = (midia.get("mime") or "").split(";")[0]

        if kind == "audio":
            # O navegador grava em webm/ogg; o Omni espera WAV
            wav = await ogg_to_wav(raw)
            if not wav:
                raise HTTPException(status_code=400, detail="Falha ao converter o áudio")
            if not texto:
                texto = await transcribe_audio(raw)
            media_parts.append(build_media_part("audio", wav, "audio/wav"))

        elif kind == "video":
            media_parts.append(build_media_part("video", raw, mime or "video/mp4"))
            if not texto:
                texto = "Observe este vídeo e comente com acolhimento."

        else:
            # Imagem ou PDF — o PDF é renderizado como uma imagem por página
            e_pdf = mime == "application/pdf" or raw[:5] == b"%PDF-"
            media_parts.extend(blocos_de_documento(raw, mime or "image/jpeg"))
            if not media_parts:
                raise HTTPException(
                    status_code=400,
                    detail="Não consegui ler esse arquivo. Se for um PDF protegido "
                           "por senha ou digitalizado, tente enviar como foto.")
            if e_pdf:
                tipo = "document"
                texto = texto or (
                    "O Sr. Edilson enviou um exame em PDF (uma imagem por página). "
                    "Leia todas as páginas, extraia os marcadores (PSA, eTFG, "
                    "creatinina) com seus valores e interprete-os segundo as "
                    "regras clínicas.")
            elif not texto:
                texto = ("Analise esta imagem: se for uma refeição, avalie do ponto de "
                         "vista nutricional considerando as restrições renais e "
                         "oncológicas; se for um exame, leia os valores e interprete.")

    if not texto and not media_parts:
        raise HTTPException(status_code=400, detail="Envie texto ou mídia")

    memoria = await zep_get_context()

    mem_resultado = None
    if _RE_PEDIDO_MEMORIA.search(texto):
        mem_resultado = await registrar_fatos_da_conversa(texto, "")

    try:
        resposta = await ask_dr_joao(texto, memoria, media_parts or None,
                                     memoria_resultado=mem_resultado)
    except Exception as e:
        logger.error("Erro na inferência NIM (chat): %s", e)
        raise HTTPException(status_code=502, detail=f"Falha ao consultar o modelo: {e}")

    # Persiste e verifica marcadores, igual ao fluxo do WhatsApp
    await zep_save(texto, resposta, {"canal": "chat_web", "tipo": tipo})
    await check_clinical_alerts(resposta, "chat_web")

    # Exame recebido: grava os valores em formato estruturado, para permitir
    # comparar a evolução meses depois.
    marcadores = []
    if tipo in ("image", "document") and media_parts:
        marcadores = await registrar_marcadores(media_parts)
    elif mem_resultado is None:
        await registrar_fatos_da_conversa(texto, resposta)

    # Áudio é opcional: se o ElevenLabs não estiver configurado, segue só o texto
    audio_b64 = ""
    if body.get("com_audio") and deve_responder_em_voz(tipo):
        audio = await text_to_speech(resposta)
        if audio:
            audio_b64 = base64.b64encode(audio).decode()

    return JSONResponse({"resposta": resposta, "audio_base64": audio_b64,
                         "transcricao": texto if tipo == "audio" else "",
                         "marcadores": marcadores})


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    """Interface de chat com o Dr. João Holanda — texto, voz, foto e PDF."""
    require_token(request)
    tk = request.query_params.get("token", "")
    # Sem cache: a página carrega o JavaScript embutido, e uma versão antiga
    # guardada pelo navegador continuaria enviando mídia no formato errado.
    return HTMLResponse(
        CHAT_HTML.replace("__TOKEN__", tk),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate",
                 "Pragma": "no-cache"},
    )


CHAT_HTML = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dr. João Holanda Cavalcante</title>
<style>
  * { box-sizing: border-box; }
  body { margin:0; font-family: system-ui, -apple-system, sans-serif;
         background:#0f1115; color:#e9e9e9; display:flex; flex-direction:column;
         height:100dvh; font-size:17px; }
  header { padding:14px 16px; background:#161a21; border-bottom:1px solid #262b35; }
  header b { font-size:1.05rem; } header span { color:#8b93a1; font-size:.85rem; }
  #log { flex:1; overflow-y:auto; padding:16px; display:flex;
         flex-direction:column; gap:12px; }
  .m { max-width:82%; padding:11px 15px; border-radius:16px; line-height:1.5;
       white-space:pre-wrap; word-wrap:break-word; }
  .eu { align-self:flex-end; background:#2563eb; border-bottom-right-radius:4px; }
  .dr { align-self:flex-start; background:#1e232c; border-bottom-left-radius:4px; }
  .sys { align-self:center; color:#8b93a1; font-size:.85rem; font-style:italic; }
  .m img { max-width:100%; border-radius:10px; margin-top:6px; display:block; }
  footer { padding:12px; background:#161a21; border-top:1px solid #262b35;
           display:flex; gap:8px; align-items:center; }
  #txt { flex:1; padding:12px 14px; border-radius:22px; border:1px solid #333a46;
         background:#0f1115; color:#e9e9e9; font-size:17px; outline:none; }
  #txt:focus { border-color:#2563eb; }
  button { border:0; border-radius:50%; width:46px; height:46px; font-size:20px;
           cursor:pointer; background:#2563eb; color:#fff; flex-shrink:0; }
  button:disabled { opacity:.45; cursor:default; }
  button.rec { background:#dc2626; animation:pulse 1.2s infinite; }
  @keyframes pulse { 50% { opacity:.55; } }
  label.file { background:#333a46; display:grid; place-items:center; }
  input[type=file] { display:none; }
</style></head>
<body>
<header><b>Dr. João Holanda Cavalcante</b><br>
<span>Acompanhamento do Sr. Edilson — Parintins, AM</span></header>
<div id="log"></div>
<footer>
  <label class="file" style="width:46px;height:46px;border-radius:50%">📎
    <input type="file" id="arq" accept="image/*,video/*,application/pdf,.pdf">
  </label>
  <input id="txt" placeholder="Escreva sua mensagem..." autocomplete="off">
  <button id="mic" title="Gravar áudio">🎤</button>
  <button id="env" title="Enviar">➤</button>
</footer>
<script>
const TOKEN = "__TOKEN__";
const log = document.getElementById('log');
const txt = document.getElementById('txt');
const env = document.getElementById('env');
const mic = document.getElementById('mic');
const arq = document.getElementById('arq');

function bolha(classe, texto, imgSrc) {
  const d = document.createElement('div');
  d.className = 'm ' + classe;
  d.textContent = texto;
  if (imgSrc) { const i = new Image(); i.src = imgSrc; d.appendChild(i); }
  log.appendChild(d); log.scrollTop = log.scrollHeight;
  return d;
}

async function enviar(texto, midia, preview) {
  if (!texto && !midia) return;
  bolha('eu', texto || (midia.tipo === 'audio' ? '🎤 Áudio' : '🖼️ Anexo'), preview);
  txt.value = ''; env.disabled = true; mic.disabled = true;
  const pensando = bolha('sys', 'Dr. João está ouvindo...');

  try {
    const r = await fetch('/chat/mensagem' + (TOKEN ? '?token=' + encodeURIComponent(TOKEN) : ''), {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Agent-Token': TOKEN},
      body: JSON.stringify({texto, midia, com_audio: true})
    });
    pensando.remove();
    if (!r.ok) { bolha('sys', 'Erro ' + r.status + ': ' + (await r.text()).slice(0, 200)); return; }
    const d = await r.json();
    if (d.transcricao) bolha('sys', '“' + d.transcricao + '”');
    bolha('dr', d.resposta);
    if (d.audio_base64) new Audio('data:audio/mpeg;base64,' + d.audio_base64).play().catch(()=>{});
  } catch (e) {
    pensando.remove(); bolha('sys', 'Falha de conexão: ' + e.message);
  } finally { env.disabled = false; mic.disabled = false; txt.focus(); }
}

env.onclick = () => enviar(txt.value.trim(), null, null);
txt.onkeydown = e => { if (e.key === 'Enter') env.click(); };

arq.onchange = () => {
  const f = arq.files[0]; if (!f) return;
  const fr = new FileReader();
  fr.onload = () => {
    // PDF entra como 'image': o servidor renderiza cada página e envia ao modelo
    const ehPdf = f.type === 'application/pdf' || /\.pdf$/i.test(f.name);
    const tipo = f.type.startsWith('video') ? 'video' : 'image';
    enviar(txt.value.trim(),
           {tipo, base64: fr.result.split(',')[1], mime: f.type || (ehPdf ? 'application/pdf' : '')},
           (tipo === 'image' && !ehPdf) ? fr.result : null);
    arq.value = '';
  };
  fr.readAsDataURL(f);
};

let rec = null, chunks = [];
mic.onclick = async () => {
  if (rec && rec.state === 'recording') { rec.stop(); return; }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({audio: true});
    chunks = []; rec = new MediaRecorder(stream);
    rec.ondataavailable = e => chunks.push(e.data);
    rec.onstop = () => {
      stream.getTracks().forEach(t => t.stop());
      mic.classList.remove('rec'); mic.textContent = '🎤';
      const fr = new FileReader();
      fr.onload = () => enviar('', {tipo: 'audio', base64: fr.result.split(',')[1],
                                    mime: 'audio/webm'}, null);
      fr.readAsDataURL(new Blob(chunks));
    };
    rec.start(); mic.classList.add('rec'); mic.textContent = '⏹';
  } catch (e) { bolha('sys', 'Microfone indisponível: ' + e.message); }
};

bolha('sys', 'Converse com o Dr. João Holanda por texto, voz ou foto.');
</script></body></html>"""


@app.post("/memoria/fato")
async def add_fact(request: Request):
    """Adiciona fato clínico diretamente à memória do Sr. Edilson."""
    require_token(request)
    body = await request.json()
    fact = body.get("fato", "")
    category = body.get("categoria", "clinico")
    if not fact:
        raise HTTPException(status_code=400, detail="Campo 'fato' obrigatório")

    try:
        from integrations.zep_memory import add_clinical_fact
        ok = await add_clinical_fact(fact, category)
        return JSONResponse({"status": "ok" if ok else "falhou", "fato": fact})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Gestão da voz do Dr. João Holanda ────────────────────────────────────────
DIR_VOZ = Path(os.getenv("DIR_VOZ", "/app/voz"))
# Formatos aceitos como amostra de voz. '.oga' é a extensão que o WhatsApp e o
# Telegram usam ao exportar nota de voz — mesmo contêiner Ogg do '.ogg', só o
# nome muda. Ficar de fora fazia o upload recusar arquivos perfeitamente
# válidos. A lista vive aqui, uma vez só: estava repetida em quatro pontos.
EXTENSOES_VOZ = (".ogg", ".oga", ".opus", ".mp3", ".m4a", ".mp4",
                 ".aac", ".wav", ".webm", ".flac")
SEMITONS_PADRAO = float(os.getenv("VOZ_SEMITONS", "-2.0"))


async def _elevenlabs_conta() -> dict:
    """Consulta cota e plano da conta, para exibir na página de voz."""
    if not ELEVENLABS_API_KEY:
        return {}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get("https://api.elevenlabs.io/v1/user/subscription",
                                 headers={"xi-api-key": ELEVENLABS_API_KEY})
            if r.status_code != 200:
                return {"erro": f"HTTP {r.status_code}: {r.text[:150]}"}
            d = r.json()
            return {
                "plano": d.get("tier", "?"),
                "usados": d.get("character_count", 0),
                "limite": d.get("character_limit", 0),
                "pode_clonar": d.get("can_use_instant_voice_cloning", False),
            }
    except Exception as e:
        return {"erro": str(e)[:150]}


@app.post("/voz/amostra")
async def enviar_amostra(request: Request):
    """
    Recebe uma amostra de áudio do Sr. Edilson e a guarda em /app/voz.
    Existe porque transferir arquivo para a VPS por scp se mostrou frágil —
    pelo navegador é mais direto.
    """
    require_token(request)
    form = await request.form()
    arquivo = form.get("arquivo")
    if arquivo is None or not hasattr(arquivo, "read"):
        raise HTTPException(status_code=400, detail="Envie o campo 'arquivo'")

    nome = os.path.basename(getattr(arquivo, "filename", "") or "amostra.ogg")
    if Path(nome).suffix.lower() not in EXTENSOES_VOZ or "/" in nome or "\\" in nome:
        aceitos = ", ".join(e.lstrip(".") for e in EXTENSOES_VOZ)
        raise HTTPException(status_code=400,
                            detail=f"Formato não aceito ({Path(nome).suffix}). "
                                   f"Aceitos: {aceitos}.")

    dados = await arquivo.read()
    if len(dados) < 2000:
        raise HTTPException(status_code=400, detail="Arquivo pequeno demais")
    if len(dados) > 25 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Arquivo acima de 25 MB")

    try:
        DIR_VOZ.mkdir(parents=True, exist_ok=True)
        destino = DIR_VOZ / nome
        destino.write_bytes(dados)
    except OSError as e:
        raise HTTPException(
            status_code=500,
            detail=f"Não foi possível gravar em {DIR_VOZ} ({e}). O volume "
                   f"pode estar montado como somente-leitura.")

    logger.info("Amostra de voz recebida: %s (%d KB)", nome, len(dados) // 1024)
    return JSONResponse({"status": "ok", "arquivo": nome, "bytes": len(dados)})


@app.post("/voz/amostra/remover")
async def remover_amostra(request: Request):
    """
    Apaga amostras de voz. Necessário ao trocar de voz: a clonagem usa todos
    os arquivos da pasta, e misturar amostras antigas com novas produz um
    timbre que não corresponde a nenhuma das duas.
    Corpo: {"nome": "arquivo.ogg"} ou {"todas": true}
    """
    require_token(request)
    try:
        corpo = await request.json()
    except Exception:
        corpo = {}

    if not DIR_VOZ.is_dir():
        return JSONResponse({"removidas": [], "restantes": []})

    amostras = [p for p in DIR_VOZ.iterdir() if p.suffix.lower() in EXTENSOES_VOZ]

    if corpo.get("todas"):
        alvos = amostras
    else:
        nome = os.path.basename(corpo.get("nome", ""))
        if not nome:
            raise HTTPException(status_code=400,
                                detail="Informe 'nome' ou 'todas': true")
        alvos = [p for p in amostras if p.name == nome]
        if not alvos:
            raise HTTPException(status_code=404,
                                detail=f"Amostra '{nome}' não encontrada")

    removidas = []
    for p in alvos:
        try:
            p.unlink()
            removidas.append(p.name)
            logger.info("Amostra de voz removida: %s", p.name)
        except OSError as e:
            logger.error("Falha ao remover %s: %s", p.name, e)

    restantes = sorted(p.name for p in DIR_VOZ.iterdir()
                       if p.suffix.lower() in EXTENSOES_VOZ)
    return JSONResponse({"removidas": removidas, "restantes": restantes})


@app.get("/voz/amostra/preview")
async def preview_amostra(request: Request, nome: str = "", semitons: float = None):
    """
    Devolve a amostra já tratada — limpa, normalizada e transposta — como
    ela será enviada ao ElevenLabs. Ouvir isto antes de clonar mostra se o
    ruído veio da gravação original ou do processamento.
    """
    require_token(request)
    arquivo = DIR_VOZ / os.path.basename(nome) if nome else None
    if not arquivo or not arquivo.is_file():
        raise HTTPException(status_code=404, detail=f"Amostra '{nome}' não encontrada")

    st = SEMITONS_PADRAO if semitons is None else semitons
    try:
        import sys
        sys.path.insert(0, "/app")
        from setup_voice import to_wav
        wav = await asyncio.to_thread(to_wav, str(arquivo), st)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Falha ao processar: {e}")

    return Response(content=wav, media_type="audio/wav",
                    headers={"Cache-Control": "no-store"})


@app.get("/voz/diagnostico")
async def diagnostico_voz(request: Request):
    """
    Testa cada capacidade da chave separadamente, para isolar onde a
    clonagem trava: ler a conta, listar vozes e escrever vozes exigem
    permissões distintas na chave de API do ElevenLabs.
    """
    require_token(request)
    if not ELEVENLABS_API_KEY:
        raise HTTPException(status_code=400, detail="ELEVENLABS_API_KEY ausente")

    resultado: dict[str, Any] = {}
    h = {"xi-api-key": ELEVENLABS_API_KEY}

    async with httpx.AsyncClient(timeout=30) as client:
        # 1. A chave é válida e qual o plano
        try:
            r = await client.get("https://api.elevenlabs.io/v1/user/subscription",
                                 headers=h)
            if r.status_code == 200:
                d = r.json()
                resultado["assinatura"] = {
                    "ok": True,
                    "plano": d.get("tier"),
                    "creditos": f"{d.get('character_count')}/{d.get('character_limit')}",
                    "clonagem_instantanea": d.get("can_use_instant_voice_cloning"),
                    "clonagem_profissional": d.get("can_use_professional_voice_cloning"),
                }
            else:
                resultado["assinatura"] = {"ok": False, "status": r.status_code,
                                           "corpo": r.text[:300]}
        except Exception as e:
            resultado["assinatura"] = {"ok": False, "erro": str(e)[:200]}

        # 2. Permissão de LEITURA de vozes
        try:
            r = await client.get("https://api.elevenlabs.io/v1/voices", headers=h)
            resultado["ler_vozes"] = {
                "ok": r.status_code == 200,
                "status": r.status_code,
                "quantidade": len(r.json().get("voices", [])) if r.status_code == 200 else None,
                "corpo": None if r.status_code == 200 else r.text[:300],
            }
        except Exception as e:
            resultado["ler_vozes"] = {"ok": False, "erro": str(e)[:200]}

        # 3. Permissão de ESCRITA — provoca o erro de propósito, com um corpo
        #    vazio, só para ver QUAL erro volta: permissão ou validação.
        try:
            r = await client.post("https://api.elevenlabs.io/v1/voices/add",
                                  headers=h, data={"name": "teste_permissao"})
            corpo = r.text[:400]
            sem_permissao = ("missing_permissions" in corpo.lower()
                             or "voices_write" in corpo.lower())
            resultado["escrever_vozes"] = {
                # 422 aqui é bom sinal: significa que passou da permissão e
                # parou na validação (faltam os arquivos de áudio).
                "ok": not sem_permissao,
                "status": r.status_code,
                "interpretacao": ("SEM PERMISSÃO DE ESCRITA na chave"
                                  if sem_permissao else
                                  "permissão OK (erro de validação é esperado aqui)"),
                "corpo": corpo,
            }
        except Exception as e:
            resultado["escrever_vozes"] = {"ok": False, "erro": str(e)[:200]}

    return JSONResponse(resultado)


@app.post("/voz/clonar")
async def clonar_voz_endpoint(request: Request):
    """Cria a voz do Dr. João Holanda no ElevenLabs a partir das amostras."""
    require_token(request)
    if not ELEVENLABS_API_KEY:
        raise HTTPException(status_code=400,
                            detail="ELEVENLABS_API_KEY não configurada no .env")

    amostras = sorted(p for p in DIR_VOZ.glob("*")
                      if p.suffix.lower() in EXTENSOES_VOZ)
    if not amostras:
        raise HTTPException(status_code=400,
                            detail="Nenhuma amostra em /app/voz — envie ao menos uma.")

    corpo = {}
    try:
        corpo = await request.json()
    except Exception:
        pass
    semitons = float(corpo.get("semitons", SEMITONS_PADRAO))

    try:
        import sys
        sys.path.insert(0, "/app")
        from setup_voice import clonar_voz_detalhado
        r = await asyncio.to_thread(clonar_voz_detalhado,
                                    [str(p) for p in amostras], semitons)
    except Exception as e:
        logger.error("Falha na clonagem: %s", e)
        raise HTTPException(status_code=502, detail=f"Falha na clonagem: {e}")

    if not r.get("ok"):
        # Mostra a resposta literal do ElevenLabs — substituí-la por um palpite
        # já mandou o diagnóstico para o lado errado uma vez.
        partes = [f"HTTP {r.get('status')}"]
        if r.get("dica"):
            partes.append(r["dica"])
        partes.append(f"Resposta do ElevenLabs: {r.get('erro','')}")
        raise HTTPException(status_code=502, detail=" — ".join(partes))

    voice_id = r["voice_id"]
    return JSONResponse({
        "status": "ok",
        "voice_id": voice_id,
        "amostras": r.get("amostras", []),
        "semitons": semitons,
        "proximo_passo": (
            f"Grave no .env e reinicie:  "
            f"echo 'ELEVENLABS_VOICE_ID={voice_id}' >> /root/automacao/.env "
            f"&& docker compose up -d joao_holanda"),
    })


@app.post("/voz/testar")
async def testar_voz(request: Request):
    """Gera um áudio de teste com a voz configurada e devolve o MP3."""
    require_token(request)
    try:
        corpo = await request.json()
    except Exception:
        corpo = {}
    texto = corpo.get("texto") or (
        "Ôxe, meu velho! Aqui é o Dr. João Holanda. "
        "Tô aqui pra cuidar do senhor, viu? Se avexe não.")

    # Permite ouvir uma voz recém-clonada sem antes gravá-la no .env
    voz = (corpo.get("voice_id") or "").strip() or ELEVENLABS_VOICE_ID
    if not voz:
        raise HTTPException(
            status_code=400,
            detail="Nenhuma voz criada ainda. Clone a voz no passo 2 antes "
                   "de tentar ouvir.")

    # Permite comparar configurações sem reconstruir o container
    ajustes = {k: float(corpo[k]) for k in ("stability", "similarity", "style", "speed")
               if corpo.get(k) is not None}
    audio = await text_to_speech(texto, voice_id=voz, ajustes=ajustes)
    if not audio:
        raise HTTPException(
            status_code=502,
            detail="Não foi possível gerar o áudio. Veja o motivo exato em: "
                   "docker logs joao_holanda_agent | grep -i elevenlabs")
    return Response(content=audio, media_type="audio/mpeg",
                    headers={"Cache-Control": "no-store"})


@app.get("/voz", response_class=HTMLResponse)
async def pagina_voz(request: Request):
    """Página para enviar amostras, clonar a voz e testar o resultado."""
    require_token(request)
    tk = request.query_params.get("token", "")

    amostras = sorted(p.name for p in DIR_VOZ.glob("*")
                      if p.suffix.lower() in EXTENSOES_VOZ) \
        if DIR_VOZ.is_dir() else []
    conta = await _elevenlabs_conta()

    if not ELEVENLABS_API_KEY:
        estado = ("<p class='alerta'>ELEVENLABS_API_KEY ainda não está no "
                  "<code>.env</code>. Sem ela não dá para clonar nem falar.</p>")
    elif conta.get("erro"):
        estado = f"<p class='alerta'>Erro ao consultar a conta: {conta['erro']}</p>"
    else:
        clone = ("pode clonar voz ✓" if conta.get("pode_clonar")
                 else "<b>plano sem clonagem</b> — requer Starter")
        estado = (f"<p class='ok'>Conta conectada · plano <b>{conta.get('plano','?')}</b> · "
                  f"{conta.get('usados',0):,} / {conta.get('limite',0):,} caracteres · "
                  f"{clone}</p>".replace(",", "."))

    voz_atual = (f"<p class='ok'>Voz ativa: <code>{ELEVENLABS_VOICE_ID}</code></p>"
                 if ELEVENLABS_VOICE_ID else
                 "<p class='alerta'>Nenhuma voz configurada ainda.</p>")

    tk_q = f"?token={tk}&" if tk else "?"
    lista = ("".join(
        f"<li>{a} "
        f"<a href='/voz/amostra/preview{tk_q}nome={a}' target='_blank'>ouvir tratada</a> "
        f"<a href='#' class='rm' data-nome='{a}'>remover</a></li>"
        for a in amostras)
        if amostras else "<li class='vazio'>nenhuma amostra enviada</li>")

    return HTMLResponse(
        VOZ_HTML.replace("__TOKEN__", tk).replace("__ESTADO__", estado)
                .replace("__VOZ_ATUAL__", voz_atual).replace("__AMOSTRAS__", lista)
                .replace("__SEMITONS__", str(SEMITONS_PADRAO)),
        headers={"Cache-Control": "no-store"})


VOZ_HTML = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Voz do Dr. João Holanda</title>
<style>
  body { font-family: system-ui, sans-serif; background:#0f1115; color:#e9e9e9;
         margin:0; padding:24px 16px; line-height:1.55; }
  .caixa { max-width:640px; margin:0 auto; }
  h1 { font-size:1.35rem; margin:0 0 4px; }
  h2 { font-size:1.05rem; margin:28px 0 10px; color:#cbd3e1; }
  .sub { color:#8b93a1; font-size:.9rem; margin:0 0 20px; }
  .painel { background:#161a21; border:1px solid #262b35; border-radius:12px;
            padding:16px; margin-bottom:16px; }
  .ok { color:#4ade80; margin:6px 0; } .alerta { color:#fbbf24; margin:6px 0; }
  .erro { color:#f87171; margin:6px 0; }
  code { background:#0f1115; padding:2px 7px; border-radius:5px; font-size:.88em; }
  ul { margin:8px 0; padding-left:22px; } .vazio { color:#6b7280; font-style:italic; }
  button { background:#2563eb; color:#fff; border:0; border-radius:9px;
           padding:11px 18px; font-size:1rem; cursor:pointer; margin:4px 6px 4px 0; }
  button:disabled { opacity:.45; cursor:default; }
  button.sec { background:#374151; }
  input[type=file] { width:100%; padding:10px; background:#0f1115; color:#e9e9e9;
                     border:1px dashed #374151; border-radius:9px; }
  input[type=number] { width:80px; padding:8px; background:#0f1115; color:#e9e9e9;
                       border:1px solid #374151; border-radius:7px; }
  pre { background:#0f1115; padding:12px; border-radius:9px; overflow-x:auto;
        font-size:.85rem; white-space:pre-wrap; word-break:break-all; }
  .passo { color:#6b7280; font-size:.85rem; }
  .linha { display:flex; flex-wrap:wrap; gap:14px; margin:12px 0; }
  .linha label { font-size:.85rem; color:#cbd3e1; display:flex;
                 flex-direction:column; gap:4px; }
  li a { color:#60a5fa; font-size:.85rem; margin-left:8px; }
  li a.rm { color:#f87171; }
  button.perigo { background:#7f1d1d; }
  .aviso { background:#1f1a0b; border-left:3px solid #a16207; padding:10px 12px;
           border-radius:6px; color:#d6c48a; font-size:.85rem; margin:12px 0; }
</style></head><body><div class="caixa">

<h1>Voz do Dr. João Holanda</h1>
<p class="sub">Sr. Edilson — Parintins, AM</p>

<div class="painel">__ESTADO____VOZ_ATUAL__</div>

<h2>1. Amostras da voz</h2>
<div class="painel">
  <p class="passo">Envie 1 a 3 áudios do Sr. Edilson falando com naturalidade,
  de 30 segundos a 2 minutos cada, sem ruído de fundo. A voz do Dr. João será
  criada a partir delas, alguns semitons mais grave.</p>
  <ul>__AMOSTRAS__</ul>
  <p class="aviso">A clonagem usa <b>todas</b> as amostras listadas acima.
  Para trocar de voz, remova as antigas antes de enviar as novas — misturar
  amostras de pessoas diferentes gera um timbre que não é de nenhuma delas.</p>
  <input type="file" id="arq" accept="audio/*,.ogg,.oga,.opus,.mp3,.m4a,.wav,.webm">
  <button id="env">Enviar amostra</button>
  <button id="limpar" class="perigo">Remover todas</button>
</div>

<h2>2. Criar a voz</h2>
<div class="painel">
  <p class="passo">Semitons abaixo da voz original — a diferença natural entre
  a voz de um pai e a de um filho:
  <input type="number" id="semi" value="__SEMITONS__" step="0.5" min="-6" max="0"></p>
  <button id="clonar">Clonar voz no ElevenLabs</button>
</div>

<h2>3. Ouvir e ajustar</h2>
<div class="painel">
  <p class="passo">Se a voz sair com chiado, suba a <b>estabilidade</b> e baixe a
  <b>similaridade</b>: similaridade alta faz o modelo reproduzir também o ruído
  da gravação original.</p>
  <div class="linha">
    <label>Estabilidade <input type="number" id="stab" value="0.75" step="0.05" min="0" max="1"></label>
    <label>Similaridade <input type="number" id="simi" value="0.75" step="0.05" min="0" max="1"></label>
    <label>Estilo <input type="number" id="styl" value="0" step="0.05" min="0" max="1"></label>
    <label>Velocidade <input type="number" id="vel" value="0.92" step="0.02" min="0.7" max="1.2"></label>
  </div>
  <button id="testar" class="sec">Tocar teste</button>
  <audio id="player" controls style="width:100%;margin-top:10px;display:none"></audio>
</div>

<div id="saida"></div>
</div>
<script>
const T = "__TOKEN__";
const q = T ? "?token=" + encodeURIComponent(T) : "";
const saida = document.getElementById('saida');
// Guarda a voz recém-criada para o teste do passo 3 poder usá-la antes
// de ela existir no .env — assim dá para ouvir e só então decidir ativar.
let vozNova = "";

function msg(txt, classe) {
  saida.innerHTML = '<div class="painel"><p class="' + (classe||'ok') + '">'
                  + txt + '</p></div>';
}
function bloco(txt) {
  saida.innerHTML = '<div class="painel"><pre>' + txt + '</pre></div>';
}

async function removerAmostra(corpo, confirmacao) {
  if (confirmacao && !confirm(confirmacao)) return;
  try {
    const r = await fetch('/voz/amostra/remover' + q, {method:'POST',
      headers:{'Content-Type':'application/json','X-Agent-Token': T},
      body: JSON.stringify(corpo)});
    const d = await r.json();
    if (!r.ok) { msg('Erro: ' + (d.detail || r.status), 'erro'); return; }
    msg('Removida(s): ' + (d.removidas.join(', ') || 'nenhuma')
        + '. Restam ' + d.restantes.length + '.');
    setTimeout(() => location.reload(), 900);
  } catch (e) { msg('Falha: ' + e.message, 'erro'); }
}

document.querySelectorAll('a.rm').forEach(a => {
  a.onclick = ev => { ev.preventDefault();
    removerAmostra({nome: a.dataset.nome}, 'Remover ' + a.dataset.nome + '?'); };
});

document.getElementById('limpar').onclick = () =>
  removerAmostra({todas: true},
                 'Remover TODAS as amostras? A voz já clonada continua ativa.');

document.getElementById('env').onclick = async () => {
  const f = document.getElementById('arq').files[0];
  if (!f) { msg('Escolha um arquivo primeiro.', 'alerta'); return; }
  msg('Enviando ' + f.name + '...');
  const fd = new FormData(); fd.append('arquivo', f);
  try {
    const r = await fetch('/voz/amostra' + q, {method:'POST', body:fd,
                          headers:{'X-Agent-Token': T}});
    const d = await r.json();
    if (!r.ok) { msg('Erro: ' + (d.detail || r.status), 'erro'); return; }
    msg('Amostra enviada: ' + d.arquivo + ' (' + Math.round(d.bytes/1024) + ' KB)');
    setTimeout(() => location.reload(), 900);
  } catch (e) { msg('Falha: ' + e.message, 'erro'); }
};

document.getElementById('clonar').onclick = async (ev) => {
  ev.target.disabled = true;
  msg('Clonando a voz... isso leva até um minuto.');
  try {
    const r = await fetch('/voz/clonar' + q, {method:'POST',
      headers:{'Content-Type':'application/json','X-Agent-Token': T},
      body: JSON.stringify({semitons: parseFloat(document.getElementById('semi').value)})});
    const d = await r.json();
    if (!r.ok) { msg('Erro: ' + (d.detail || r.status), 'erro'); return; }
    vozNova = d.voice_id;
    bloco('Voz criada!\\n\\nvoice_id: ' + d.voice_id
        + '\\n\\n▸ Clique em "Tocar teste" para ouvir antes de ativar.'
        + '\\n\\n▸ Depois, rode na VPS para ativar de vez:\\n' + d.proximo_passo);
  } catch (e) { msg('Falha: ' + e.message, 'erro'); }
  finally { ev.target.disabled = false; }
};

document.getElementById('testar').onclick = async (ev) => {
  ev.target.disabled = true;
  msg('Gerando áudio de teste...');
  try {
    const r = await fetch('/voz/testar' + q, {method:'POST',
      headers:{'Content-Type':'application/json','X-Agent-Token': T},
      body: JSON.stringify(Object.assign(
        vozNova ? {voice_id: vozNova} : {},
        {stability: parseFloat(document.getElementById('stab').value),
         similarity: parseFloat(document.getElementById('simi').value),
         style: parseFloat(document.getElementById('styl').value),
         speed: parseFloat(document.getElementById('vel').value)}))});
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      msg('Erro: ' + (d.detail || r.status), 'erro'); return;
    }
    const p = document.getElementById('player');
    p.src = URL.createObjectURL(await r.blob());
    p.style.display = 'block'; p.play();
    msg('Áudio gerado — é assim que o Sr. Edilson vai ouvir.');
  } catch (e) { msg('Falha: ' + e.message, 'erro'); }
  finally { ev.target.disabled = false; }
};
</script></body></html>"""


@app.get("/telegram/contatos")
async def contatos_telegram(request: Request):
    """
    Quem já escreveu ao bot: os cadastrados e os ainda desconhecidos.

    Serve para cadastrar a família sem depender de bot de terceiros — basta
    a pessoa mandar uma mensagem e o ID dela aparece aqui, junto com a linha
    pronta para o .env.
    """
    require_token(request)
    canal = getattr(app.state, "telegram", None)
    desconhecidos = list(getattr(canal, "desconhecidos", {}).values()) if canal else []

    cadastrados = [
        {"chat_id": cid, **dados,
         "autorizado": cid in TELEGRAM_ALLOWED_IDS,
         "recebe_alertas": cid in TELEGRAM_FAMILIA_IDS,
         "administrador": cid in TELEGRAM_ADMIN_IDS}
        for cid, dados in TELEGRAM_CONTATOS.items()
    ]

    sugestao = None
    if desconhecidos:
        novos = ", ".join(
            f"{d['chat_id']}:{d['nome']}:familiar:{d['nome'].split()[0]}"
            for d in desconhecidos)
        atuais = ", ".join(
            f"{cid}:{d['nome']}:{d['papel']}:{d['tratamento']}"
            for cid, d in TELEGRAM_CONTATOS.items())
        todos_ids = ",".join(str(i) for i in
                             sorted(set(TELEGRAM_ALLOWED_IDS) |
                                    {d["chat_id"] for d in desconhecidos}))
        sugestao = {
            "TELEGRAM_CONTATOS": f'"{atuais + ", " if atuais else ""}{novos}"',
            "TELEGRAM_ALLOWED_IDS": todos_ids,
            "aviso": ("Ajuste o papel de cada um (paciente, filho, filha) antes "
                      "de gravar. Depois: docker compose up -d joao_holanda"),
        }

    return JSONResponse({
        "cadastrados": cadastrados,
        "aguardando_cadastro": desconhecidos,
        "sugestao_env": sugestao,
    })


@app.post("/telegram/testar-alerta")
async def testar_alerta_familia(request: Request):
    """
    Dispara uma mensagem de teste pelos mesmos canais de um alerta clínico
    de verdade (grupo/contatos em TELEGRAM_FAMILIA_IDS, com WhatsApp como
    reserva), sem envolver nenhum dado do paciente.

    Existe para confirmar a entrega ANTES de precisar confiar no sistema
    para um PSA ou eTFG alterado — não faz sentido descobrir que o grupo
    está mal configurado só quando o alerta que importa não chegar.
    critico=False: uma falha aqui é problema de configuração, não uma
    emergência perdida, então não deve virar log CRITICAL.
    """
    require_token(request)
    mensagem = ("🔧 Teste do sistema de alerta do Dr. João Holanda.\n"
               "Se esta mensagem chegou, o canal está funcionando. Não é "
               "um alerta clínico — pode ignorar.")
    entregue = await notificar_familia(mensagem, critico=False)
    return {
        "entregue": entregue,
        "telegram_familia_ids": sorted(TELEGRAM_FAMILIA_IDS),
        "whatsapp_grupo_configurado": bool(FAMILY_GROUP_ID),
    }


@app.post("/telegram/testar-relatorio")
async def testar_relatorio_diario(request: Request):
    """
    Dispara o resumo diário agora, fora do horário programado — para
    conferir o conteúdo e a entrega sem precisar esperar até às
    HORA_RELATORIO_FAMILIA horas de verdade.
    """
    require_token(request)
    texto = await montar_relatorio_diario()
    if not texto:
        return {"entregue": False, "texto": None,
                "motivo": "Nenhum fato registrado hoje — nada para resumir."}

    entregue = await notificar_familia(
        f"📋 Resumo do dia — Sr. Edilson\n\n{texto}", critico=False)
    return {"entregue": entregue, "texto": texto,
            "motivo": None if entregue else "Falha na entrega — veja docker logs."}


@app.get("/modelos")
async def modelos_disponiveis(request: Request):
    """
    Lista os modelos que a chave configurada pode usar. Os nomes mudam entre
    gerações do Gemini e um nome errado devolve 404 — daí a consulta.
    """
    require_token(request)
    try:
        disponiveis = await listar_modelos()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Falha ao listar modelos: {e}")

    return JSONResponse({
        "provedor": PROVIDER,
        "modelo_em_uso": MODELO_ATUAL,
        "em_uso_disponivel": MODELO_ATUAL in disponiveis if disponiveis else None,
        "disponiveis": disponiveis,
    })


@app.get("/memoria/diagnostico")
async def diagnostico_memoria(request: Request):
    """
    Testa a memória de ponta a ponta: sessão, escrita, leitura e remoção.
    Existe porque 'o agente disse que memorizou' não é prova de nada — só o
    ciclo completo mostra em qual etapa a gravação está se perdendo.
    """
    require_token(request)
    passos: dict[str, Any] = {}

    try:
        from integrations.zep_memory import (API, ZEP_SESSION_ID,
                                             add_clinical_fact,
                                             ensure_user_and_session, get_facts,
                                             remove_clinical_fact)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Módulo de memória indisponível: {e}")

    passos["configuracao"] = {"api": API, "sessao": ZEP_SESSION_ID,
                              "chave_presente": bool(ZEP_API_KEY)}

    # 1. O Zep responde?
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{ZEP_API_URL}/healthz")
            passos["zep_no_ar"] = {"ok": r.status_code == 200, "status": r.status_code}
    except Exception as e:
        passos["zep_no_ar"] = {"ok": False, "erro": str(e)[:200]}

    # 2. Usuário e sessão existem?
    try:
        passos["sessao"] = {"ok": await ensure_user_and_session()}
    except Exception as e:
        passos["sessao"] = {"ok": False, "erro": str(e)[:200]}

    # 3. Quantos fatos já existem, por categoria
    try:
        atuais = await get_facts()
        por_cat: dict[str, int] = {}
        for f in atuais:
            por_cat[f.get("categoria", "?")] = por_cat.get(f.get("categoria", "?"), 0) + 1
        passos["fatos_existentes"] = {"total": len(atuais), "por_categoria": por_cat}
    except Exception as e:
        passos["fatos_existentes"] = {"erro": str(e)[:200]}

    # 4. Ciclo completo: escreve, relê e apaga um fato de teste
    marca = f"__teste_memoria__ {datetime.now(timezone.utc).isoformat()}"
    try:
        # Grava direto pela camada baixa, para capturar status e corpo do Zep
        from integrations.zep_memory import (_get_session_metadata,
                                             _patch_session_metadata)
        meta = await _get_session_metadata()
        passos["metadata_atual"] = {
            "chaves": sorted(meta.keys()) or "(vazia)",
            "fatos": len(meta.get("clinical_facts", [])),
        }
        meta.setdefault("clinical_facts", []).append(
            {"fact": marca, "categoria": "teste",
             "registrado_em": datetime.now(timezone.utc).isoformat()})
        ok, status, corpo = await _patch_session_metadata(meta)
        passos["escrita"] = {"ok": ok, "http": status, "resposta_do_zep": corpo}

        escreveu = ok

        relidos = await get_facts("teste")
        achou = any(marca in f.get("fact", "") for f in relidos)
        passos["leitura"] = {"ok": achou,
                             "diagnostico": ("gravou e releu ✓" if achou else
                                             "escreveu mas NÃO releu — a gravação "
                                             "não está persistindo no Zep")}

        if achou:
            for i, f in enumerate(sorted(relidos,
                                         key=lambda x: x.get("registrado_em", "")), 1):
                if marca in f.get("fact", ""):
                    passos["limpeza"] = {"removido": bool(
                        await remove_clinical_fact("teste", i))}
                    break
    except Exception as e:
        passos["escrita"] = {"ok": False, "erro": str(e)[:300]}

    # 5. O que o agente enxerga como contexto
    try:
        ctx = await zep_get_context()
        passos["contexto_visto_pelo_agente"] = ctx[:900]
    except Exception as e:
        passos["contexto_visto_pelo_agente"] = f"erro: {e}"

    # 6. Identificação de contatos
    passos["contatos_telegram"] = {
        "mapeados": {str(k): v["nome"] + f" ({v['papel']})"
                     for k, v in TELEGRAM_CONTATOS.items()} or
                    "NENHUM — o agente não sabe quem é quem",
        "administradores": sorted(TELEGRAM_ADMIN_IDS) or "nenhum",
    }

    return JSONResponse(passos)


@app.get("/memoria/marcadores")
async def historico_marcadores(request: Request, nome: str = ""):
    """
    Histórico dos marcadores laboratoriais registrados, em ordem cronológica.
    ?nome=PSA filtra um marcador específico.
    """
    require_token(request)
    try:
        from integrations.zep_memory import get_facts
        fatos = await get_facts("exame")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Falha ao ler a memória: {e}")

    if nome:
        alvo = nome.lower()
        fatos = [f for f in fatos if alvo in f.get("fact", "").lower()]

    fatos.sort(key=lambda f: f.get("registrado_em", ""))
    return JSONResponse({
        "total": len(fatos),
        "filtro": nome or None,
        "marcadores": [{"registro": f.get("fact", ""),
                        "gravado_em": f.get("registrado_em", "")} for f in fatos],
    })


@app.get("/memoria/contexto")
async def get_memory_context(request: Request):
    """Retorna o contexto atual da memória do Sr. Edilson."""
    require_token(request)
    memoria = await zep_get_context()
    return JSONResponse({"contexto": memoria})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("joao_holanda_service:app", host="0.0.0.0", port=3000,
                reload=False, log_level="info")
