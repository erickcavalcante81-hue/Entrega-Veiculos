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
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

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

# ─── Prompt do Dr. João Holanda ───────────────────────────────────────────────
SYSTEM_PROMPT = """Você é Dr. João Holanda Cavalcante, médico especialista em oncologia \
metabólica, nefrologia e nutrição amazônica, com formação em psicologia integrativa e TCC \
para idosos. Você acompanha o Sr. Edilson, 76 anos, residente em Parintins, Amazonas.

HISTÓRICO DO PACIENTE:
- Pós-câncer de próstata (em vigilância ativa)
- PSA mais recente: 0,12 (subiu de 0,08 — zona de atenção moderada)
- Monitoramento de eTFG renal
- Restrição de sódio, sem álcool

REGRAS CLÍNICAS:
- PSA < 0,10: zona segura — reforço positivo
- PSA 0,10-0,20: atenção — não alarmar, sugerir urologista
- PSA > 0,20: ALERTA — notificar família imediatamente
- eTFG < 30: protocolo renal grave — notificar nefrologista

NUTRIÇÃO AMAZÔNICA RECOMENDADA:
Incentive: Açaí (sem guaraná), Tucumã, Castanha-do-Pará (máx 2/dia), Pupunha, Tambaqui, \
Bacaba, Camu-camu.

TOM DE VOZ: empático, acolhedor, linguagem simples, parágrafos curtos (máx 3 por resposta).
Valide os sentimentos antes de dar orientação clínica. Nunca diagnostique doenças novas.

ENGAJAMENTO CULTURAL 2026:
Se perceber tristeza/resistência, mencione: Final Champions (PSG × Arsenal, 31/mai/2026), \
novelas Globo (Três Graças, Quem Ama Cuida), Netflix (Dele & Dela), ou times amazonenses \
(Fast Club, Nacional-AM).

MEMÓRIA (Zep):
{memoria_zep}

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


# ─── Nvidia NIM — motor multimodal ────────────────────────────────────────────
# Mapeia o mimetype da mídia para o tipo de conteúdo esperado pela API do Omni.
MEDIA_PART_TYPES = {
    "audio": "audio_url",
    "image": "image_url",
    "video": "video_url",
}


def build_media_part(kind: str, data: bytes, mime: str) -> dict:
    """
    Monta um bloco de conteúdo multimodal no formato data URI base64.
    kind: 'audio' | 'image' | 'video'
    """
    key = MEDIA_PART_TYPES[kind]
    b64 = base64.b64encode(data).decode()
    return {"type": key, key: {"url": f"data:{mime};base64,{b64}"}}


async def call_nim(messages: list[dict], max_tokens: int = 1024,
                   temperature: float = 0.6, reasoning: bool = True,
                   media_io: dict | None = None) -> str:
    """
    Chamada genérica ao Nemotron Omni via API compatível com OpenAI.
    Retorna apenas o conteúdo final — o rascunho de raciocínio (campo
    'reasoning') é descartado, pois não deve chegar ao Sr. Edilson.
    """
    if not NVIDIA_NIM_API_KEY:
        raise RuntimeError("NVIDIA_NIM_API_KEY não configurada")

    payload: dict[str, Any] = {
        "model": NIM_CHAT_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": 0.9,
    }
    if reasoning and NIM_REASONING_BUDGET > 0:
        payload["reasoning_budget"] = NIM_REASONING_BUDGET
    if media_io:
        # Controla amostragem de vídeo (fps / num_frames) na inferência
        payload["media_io_kwargs"] = media_io

    # Mídia em base64 deixa o corpo grande; timeout generoso para vídeo/áudio longo
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(
            f"{NVIDIA_NIM_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {NVIDIA_NIM_API_KEY}",
                     "Content-Type": "application/json"},
            json=payload,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"NIM HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()["choices"][0]["message"]["content"].strip()


async def ask_dr_joao(message: str, memoria: str,
                      media_parts: list[dict] | None = None) -> str:
    """
    Gera a resposta do Dr. João Holanda, combinando o system prompt da persona,
    o contexto recuperado do Zep e — quando houver — imagem, vídeo ou áudio
    enviados pelo Sr. Edilson, tudo numa única inferência do Nemotron Omni.
    """
    system = SYSTEM_PROMPT.replace("{memoria_zep}", memoria)

    # Sem mídia, o conteúdo é texto puro; com mídia, vira lista de blocos
    if media_parts:
        content: Any = [{"type": "text", "text": message}] + media_parts
    else:
        content = message

    return await call_nim(
        [{"role": "system", "content": system},
         {"role": "user",   "content": content}],
        max_tokens=1024,
    )


# ─── ElevenLabs TTS ───────────────────────────────────────────────────────────
async def text_to_speech(text: str) -> bytes | None:
    """Gera áudio com a voz do Dr. João Holanda via ElevenLabs."""
    if not ELEVENLABS_API_KEY or not ELEVENLABS_VOICE_ID:
        return None
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
                headers={"xi-api-key": ELEVENLABS_API_KEY,
                         "Content-Type": "application/json"},
                json={
                    "text": text,
                    "model_id": "eleven_multilingual_v2",
                    "voice_settings": {"stability": 0.6, "similarity_boost": 0.8,
                                       "style": 0.2, "use_speaker_boost": True},
                },
            )
            return resp.content if resp.status_code == 200 else None
    except Exception as e:
        logger.warning("ElevenLabs error: %s", e)
        return None


# ─── Evolution API helpers ────────────────────────────────────────────────────
async def send_text(to: str, text: str) -> None:
    """Envia mensagem de texto via Evolution API."""
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"{EVOLUTION_API_URL}/message/sendText/{WHATSAPP_INSTANCE}",
            headers={"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"},
            json={"number": to, "options": {"delay": 800, "presence": "composing"},
                  "textMessage": {"text": text}},
        )


async def send_audio(to: str, audio_bytes: bytes) -> None:
    """Envia áudio (mp3/ogg) via Evolution API como PTT (nota de voz)."""
    b64 = base64.b64encode(audio_bytes).decode()
    async with httpx.AsyncClient(timeout=30) as client:
        await client.post(
            f"{EVOLUTION_API_URL}/message/sendMedia/{WHATSAPP_INSTANCE}",
            headers={"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"},
            json={"number": to, "options": {"delay": 500},
                  "mediaMessage": {"mediatype": "audio", "media": b64, "ptt": True}},
        )


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


# ─── Processamento principal ──────────────────────────────────────────────────
async def process_message(from_number: str, message_text: str,
                           message_type: str = "text",
                           media_parts: list[dict] | None = None) -> None:
    """
    Pipeline completo: memória → Nemotron Omni → ElevenLabs → WhatsApp.
    Executado em background para resposta rápida ao webhook.
    """
    logger.info("Mensagem de %s [%s]: %s", from_number, message_type,
                message_text[:80])

    # 1. Recupera contexto do Zep
    memoria = await zep_get_context()

    # 2. Gera resposta do Dr. João Holanda (texto + mídia na mesma inferência)
    try:
        resposta = await ask_dr_joao(message_text, memoria, media_parts)
    except Exception as e:
        logger.error("Erro na inferência NIM: %s", e)
        resposta = ("Desculpe Sr. Edilson, tive uma dificuldade técnica agora. "
                    "Pode me repetir o que disse?")

    # 3. Envia áudio (ElevenLabs) se disponível, texto como fallback
    audio = await text_to_speech(resposta)
    if audio:
        await send_audio(from_number, audio)
    else:
        await send_text(from_number, resposta)

    # 4. Salva interação no Zep para aprendizado
    await zep_save(message_text, resposta, {"de": from_number, "tipo": message_type})

    # 5. Rastreia marcadores na resposta — em exames enviados como imagem ou PDF
    #    os valores só aparecem depois que o modelo lê o documento.
    await check_clinical_alerts(resposta, from_number)

    logger.info("Resposta enviada para %s (%d chars)", from_number, len(resposta))


# ─── Detecção de alertas clínicos ────────────────────────────────────────────
PSA_PATTERN   = re.compile(r'PSA[:\s]+(\d+[\.,]\d+)', re.IGNORECASE)
ETFG_PATTERN  = re.compile(r'eTFG[:\s]+(\d+)', re.IGNORECASE)

# Evita alertar a família duas vezes pelo mesmo valor: a checagem roda tanto
# na mensagem recebida quanto na resposta (exames em imagem/PDF).
_alertas_enviados: set[str] = set()


async def check_clinical_alerts(text: str, from_number: str) -> None:
    """Detecta valores críticos de PSA/eTFG e notifica família se necessário."""
    if not FAMILY_GROUP_ID:
        return

    if m := PSA_PATTERN.search(text):
        psa = float(m.group(1).replace(",", "."))
        chave = f"psa:{psa}"
        if psa > 0.20 and chave not in _alertas_enviados:
            _alertas_enviados.add(chave)
            await send_text(FAMILY_GROUP_ID,
                            f"🚨 ALERTA MÉDICO — Sr. Edilson\n"
                            f"PSA: {psa} (acima do limite de 0,20)\n"
                            f"Dr. João Holanda recomenda contato urgente com urologista.")

    if m := ETFG_PATTERN.search(text):
        etfg = int(m.group(1))
        chave = f"etfg:{etfg}"
        if etfg < 30 and chave not in _alertas_enviados:
            _alertas_enviados.add(chave)
            await send_text(FAMILY_GROUP_ID,
                            f"🚨 ALERTA RENAL — Sr. Edilson\n"
                            f"eTFG: {etfg} (estadiamento grave)\n"
                            f"Dr. João Holanda recomenda contato urgente com nefrologista.")


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
    yield
    logger.info("Dr. João Holanda Agent encerrando.")


app = FastAPI(
    title="Dr. João Holanda Cavalcante — Agente de Saúde",
    description="Agente IA para acompanhamento longitudinal do Sr. Edilson",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    return {"status": "ok", "agent": "Dr. João Holanda Cavalcante",
            "paciente": "Sr. Edilson — Parintins, AM"}


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

    message_obj = msg.get("message", {})
    message_type = "text"
    message_text = ""
    media_parts: list[dict] = []

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

        message_text = await transcribe_audio(raw)
        logger.info("Áudio transcrito: %s", message_text[:80])

        wav = await ogg_to_wav(raw)
        if wav:
            media_parts.append(build_media_part("audio", wav, "audio/wav"))

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
        # O Omni lê PDF e imagem pelo mesmo canal visual
        media_parts.append(build_media_part("image", raw, mime))
        message_text = (f"O Sr. Edilson enviou o documento '{caption}'. "
                        f"Leia o conteúdo. Se for um exame laboratorial, extraia "
                        f"os marcadores (PSA, eTFG, creatinina) com seus valores e "
                        f"interprete-os segundo as regras clínicas.")
    else:
        return JSONResponse({"status": "unsupported_type"})

    if not message_text.strip():
        return JSONResponse({"status": "empty_message"})

    # Verifica alertas clínicos
    asyncio.create_task(check_clinical_alerts(message_text, from_number))

    # Processa em background (resposta imediata ao webhook)
    asyncio.create_task(
        process_message(from_number, message_text, message_type, media_parts))

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

    if event_type in ("fall_detected", "immobility") and FAMILY_GROUP_ID:
        alerta = (f"🚨 ALERTA DE SEGURANÇA — Sr. Edilson\n"
                  f"Câmera cozinha detectou: {description}\n"
                  f"Por favor, verifiquem imediatamente.")
        await send_text(FAMILY_GROUP_ID, alerta)
        await zep_save(f"[CÂMERA] {description}",
                       "Alerta enviado à família.", {"tipo": "emergencia"})

    return JSONResponse({"status": "ok", "event": event_type})


@app.post("/memoria/fato")
async def add_fact(request: Request):
    """Adiciona fato clínico diretamente à memória do Sr. Edilson."""
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


@app.get("/memoria/contexto")
async def get_memory_context():
    """Retorna o contexto atual da memória do Sr. Edilson."""
    memoria = await zep_get_context()
    return JSONResponse({"contexto": memoria})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("joao_holanda_service:app", host="0.0.0.0", port=3000,
                reload=False, log_level="info")
