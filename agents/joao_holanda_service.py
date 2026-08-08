"""
joao_holanda_service.py — Serviço agente Dr. João Holanda Cavalcante
Microserviço FastAPI que recebe webhooks da Evolution API, processa com
Claude + Zep e responde ao Sr. Edilson via WhatsApp com voz (ElevenLabs).

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
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

# ─── Configuração ─────────────────────────────────────────────────────────────
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("dr-joao-holanda")

ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
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

# ─── Nvidia NIM (backend OpenAI-compatível para Whisper / transcrição) ─────────
# NIM é preferido sobre OpenAI quando OPENAI_API_KEY não está configurado.
NVIDIA_NIM_API_KEY  = os.getenv("NVIDIA_NIM_API_KEY", "")
NVIDIA_NIM_BASE_URL = os.getenv("NVIDIA_NIM_BASE_URL", "https://integrate.api.nvidia.com/v1")

# Resolve credenciais e endpoint de transcrição em tempo de inicialização
_whisper_key   = OPENAI_API_KEY or NVIDIA_NIM_API_KEY
_whisper_url   = (
    "https://api.openai.com/v1" if OPENAI_API_KEY
    else NVIDIA_NIM_BASE_URL   if NVIDIA_NIM_API_KEY
    else ""
)
# whisper-1 = OpenAI  |  nvidia/canary-1b = NIM (ambos aceitam /audio/transcriptions)
_whisper_model = "whisper-1" if OPENAI_API_KEY else "nvidia/canary-1b"

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

# ─── Clientes ─────────────────────────────────────────────────────────────────
claude_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


# ─── Zep helpers ──────────────────────────────────────────────────────────────
async def zep_get_context() -> str:
    """Recupera memória contextual do Sr. Edilson no Zep."""
    if not ZEP_API_KEY:
        return "Memória Zep não configurada."
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{ZEP_API_URL}/api/v2/sessions/{ZEP_SESSION_ID}/memory",
                headers={"Authorization": f"Api-Key {ZEP_API_KEY}"},
                params={"lastn": 10},
            )
            if resp.status_code != 200:
                return "Sem histórico anterior."
            data = resp.json()
            parts = []
            if summary := data.get("summary", {}).get("content"):
                parts.append(f"RESUMO: {summary}")
            if facts := data.get("facts", []):
                parts.append("FATOS: " + " | ".join(f.get("fact", "") for f in facts[:8]))
            return "\n".join(parts) or "Primeira interação do dia."
    except Exception as e:
        logger.warning("Zep context error: %s", e)
        return "Memória temporariamente indisponível."


async def zep_save(patient_msg: str, agent_response: str, metadata: dict = None) -> None:
    """Salva interação no Zep para aprendizado longitudinal."""
    if not ZEP_API_KEY:
        return
    try:
        payload = {
            "messages": [
                {"role": "user", "role_type": "user", "content": patient_msg,
                 "metadata": metadata or {}},
                {"role": "Dr. João Holanda", "role_type": "assistant",
                 "content": agent_response,
                 "metadata": {"ts": datetime.now(timezone.utc).isoformat()}},
            ]
        }
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{ZEP_API_URL}/api/v2/sessions/{ZEP_SESSION_ID}/messages",
                headers={"Authorization": f"Api-Key {ZEP_API_KEY}",
                         "Content-Type": "application/json"},
                json=payload,
            )
    except Exception as e:
        logger.warning("Zep save error: %s", e)


# ─── Claude ───────────────────────────────────────────────────────────────────
async def ask_dr_joao(message: str, memoria: str) -> str:
    """Chama o Claude com o persona do Dr. João Holanda + contexto Zep."""
    system = SYSTEM_PROMPT.replace("{memoria_zep}", memoria)
    resp = await claude_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": message}],
    )
    return resp.content[0].text


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


async def transcribe_audio_url(media_url: str) -> str:
    """
    Baixa áudio da Evolution API e transcreve via Whisper.
    Usa OpenAI ou Nvidia NIM (OpenAI-compatível) conforme disponibilidade.
    """
    if not _whisper_key:
        return "[áudio — transcrição indisponível: configure OPENAI_API_KEY ou NVIDIA_NIM_API_KEY]"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # Download do áudio via Evolution API
            dl_resp = await client.post(
                f"{EVOLUTION_API_URL}/message/downloadMedia/{WHATSAPP_INSTANCE}",
                headers={"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"},
                json={"url": media_url},
            )
            audio_data = dl_resp.content

            # Transcrição: whisper-1 (OpenAI) ou nvidia/canary-1b (NIM)
            files = {"file": ("audio.ogg", audio_data, "audio/ogg")}
            data  = {"model": _whisper_model, "language": "pt"}
            wh_resp = await client.post(
                f"{_whisper_url}/audio/transcriptions",
                headers={"Authorization": f"Bearer {_whisper_key}"},
                files=files, data=data,
            )
            return wh_resp.json().get("text", "[transcrição vazia]")
    except Exception as e:
        logger.warning("Whisper/NIM transcription error: %s", e)
        return "[áudio — erro na transcrição]"


# ─── Processamento principal ──────────────────────────────────────────────────
async def process_message(from_number: str, message_text: str,
                           message_type: str = "text") -> None:
    """
    Pipeline completo: memória → Claude → ElevenLabs → WhatsApp.
    Executado em background para resposta rápida ao webhook.
    """
    logger.info("Mensagem de %s [%s]: %s", from_number, message_type,
                message_text[:80])

    # 1. Recupera contexto do Zep
    memoria = await zep_get_context()

    # 2. Gera resposta do Dr. João Holanda
    try:
        resposta = await ask_dr_joao(message_text, memoria)
    except Exception as e:
        logger.error("Claude error: %s", e)
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

    logger.info("Resposta enviada para %s (%d chars)", from_number, len(resposta))


# ─── Detecção de alertas clínicos ────────────────────────────────────────────
PSA_PATTERN   = re.compile(r'PSA[:\s]+(\d+[\.,]\d+)', re.IGNORECASE)
ETFG_PATTERN  = re.compile(r'eTFG[:\s]+(\d+)', re.IGNORECASE)

async def check_clinical_alerts(text: str, from_number: str) -> None:
    """Detecta valores críticos de PSA/eTFG e notifica família se necessário."""
    if m := PSA_PATTERN.search(text):
        psa = float(m.group(1).replace(",", "."))
        if psa > 0.20 and FAMILY_GROUP_ID:
            alerta = (f"🚨 ALERTA MÉDICO — Sr. Edilson\n"
                      f"PSA: {psa} (acima do limite de 0,20)\n"
                      f"Dr. João Holanda recomenda contato urgente com urologista.")
            await send_text(FAMILY_GROUP_ID, alerta)

    if m := ETFG_PATTERN.search(text):
        etfg = int(m.group(1))
        if etfg < 30 and FAMILY_GROUP_ID:
            alerta = (f"🚨 ALERTA RENAL — Sr. Edilson\n"
                      f"eTFG: {etfg} (estadiamento grave)\n"
                      f"Dr. João Holanda recomenda contato urgente com nefrologista.")
            await send_text(FAMILY_GROUP_ID, alerta)


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

    # Texto simples
    if "conversation" in message_obj:
        message_text = message_obj["conversation"]
    elif "extendedTextMessage" in message_obj:
        message_text = message_obj["extendedTextMessage"].get("text", "")

    # Áudio (PTT / nota de voz)
    elif "audioMessage" in message_obj or "pttMessage" in message_obj:
        message_type = "audio"
        audio_url = (message_obj.get("audioMessage") or
                     message_obj.get("pttMessage", {})).get("url", "")
        message_text = await transcribe_audio_url(audio_url)
        logger.info("Áudio transcrito: %s", message_text[:80])

    # Imagem/PDF — análise nutricional ou de exames
    elif "imageMessage" in message_obj or "documentMessage" in message_obj:
        message_type = "media"
        caption = (message_obj.get("imageMessage") or
                   message_obj.get("documentMessage", {})).get("caption", "")
        message_text = (f"[O Sr. Edilson enviou uma {'imagem' if 'imageMessage' in message_obj else 'documento'}]"
                        f"{' com legenda: ' + caption if caption else ''}")
    else:
        return JSONResponse({"status": "unsupported_type"})

    if not message_text.strip():
        return JSONResponse({"status": "empty_message"})

    # Verifica alertas clínicos
    asyncio.create_task(check_clinical_alerts(message_text, from_number))

    # Processa em background (resposta imediata ao webhook)
    asyncio.create_task(process_message(from_number, message_text, message_type))

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
