"""
llm_backend.py — Camada de modelo do Dr. João Holanda Cavalcante

Abstrai o motor de IA para que o resto do sistema não conheça o provedor.
Dois backends multimodais (texto, áudio, imagem, vídeo e documento):

  gemini — Google Gemini via Google AI Studio (generativelanguage.googleapis.com)
           Lê PDF nativamente, sem rasterizar páginas.
  nim    — Nvidia NIM, Nemotron 3 Nano Omni (API compatível com OpenAI)

Escolha por LLM_PROVIDER no .env. Sem definir, usa o Gemini se houver
GEMINI_API_KEY; senão, cai no NIM.

A mídia trafega internamente num formato neutro — {kind, mime, b64} — e
cada backend a serializa no formato que espera.
"""

import base64
import logging
import os
from typing import Any, Optional

import httpx

logger = logging.getLogger("dr-joao-holanda.llm")

# ─── Configuração ─────────────────────────────────────────────────────────────
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL    = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_BASE_URL = os.getenv("GEMINI_BASE_URL",
                            "https://generativelanguage.googleapis.com/v1beta")

NIM_API_KEY   = os.getenv("NVIDIA_NIM_API_KEY", "")
NIM_BASE_URL  = os.getenv("NVIDIA_NIM_BASE_URL", "https://integrate.api.nvidia.com/v1")
NIM_MODEL     = os.getenv("NIM_CHAT_MODEL", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning")
NIM_REASONING_BUDGET = int(os.getenv("NIM_REASONING_BUDGET", "4096"))

_provider_env = os.getenv("LLM_PROVIDER", "").strip().lower()
if _provider_env in ("gemini", "nim"):
    PROVIDER = _provider_env
else:
    PROVIDER = "gemini" if GEMINI_API_KEY else "nim"

MODELO_ATUAL = GEMINI_MODEL if PROVIDER == "gemini" else NIM_MODEL


def configurado() -> bool:
    """Há credencial para o provedor ativo?"""
    return bool(GEMINI_API_KEY) if PROVIDER == "gemini" else bool(NIM_API_KEY)


def suporta_pdf_nativo() -> bool:
    """
    O Gemini lê PDF diretamente. O Nemotron Omni não — para ele, as páginas
    precisam ser rasterizadas como imagem antes do envio.
    """
    return PROVIDER == "gemini"


# ─── Formato neutro de mídia ──────────────────────────────────────────────────
def build_media_part(kind: str, data: bytes, mime: str) -> dict:
    """
    Empacota mídia de forma independente de provedor.
    kind: 'audio' | 'image' | 'video' | 'document'
    """
    return {"kind": kind, "mime": (mime or "").split(";")[0].strip(),
            "b64": base64.b64encode(data).decode()}


def _para_openai(part: dict) -> dict:
    """Serializa no formato de blocos do OpenAI/NIM (data URI)."""
    # O NIM não tem canal de documento: PDF já chega rasterizado como imagem
    kind = part["kind"] if part["kind"] in ("audio", "image", "video") else "image"
    chave = f"{kind}_url"
    return {"type": chave, chave: {"url": f"data:{part['mime']};base64,{part['b64']}"}}


def _para_gemini(part: dict) -> dict:
    """Serializa no formato inline_data do Gemini."""
    return {"inline_data": {"mime_type": part["mime"], "data": part["b64"]}}


# ─── Backend: Google Gemini ───────────────────────────────────────────────────
async def _chamar_gemini(system: str, texto: str, partes: list[dict],
                         max_tokens: int, temperature: float) -> str:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY não configurada")

    conteudo: list[dict] = []
    if texto:
        conteudo.append({"text": texto})
    conteudo.extend(_para_gemini(p) for p in partes)

    payload: dict[str, Any] = {
        "contents": [{"role": "user", "parts": conteudo}],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": temperature,
            "topP": 0.9,
        },
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}

    url = f"{GEMINI_BASE_URL}/models/{GEMINI_MODEL}:generateContent"
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(
            url,
            headers={"x-goog-api-key": GEMINI_API_KEY,
                     "Content-Type": "application/json"},
            json=payload,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Gemini HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        candidatos = data.get("candidates") or []
        if not candidatos:
            # Conteúdo barrado pelos filtros de segurança do Gemini
            motivo = (data.get("promptFeedback") or {}).get("blockReason", "desconhecido")
            raise RuntimeError(f"Gemini não retornou resposta (motivo: {motivo})")

        cand = candidatos[0]
        partes_resp = (cand.get("content") or {}).get("parts") or []
        texto_resp = "".join(p.get("text", "") for p in partes_resp).strip()

        if not texto_resp:
            fim = cand.get("finishReason", "")
            if fim == "MAX_TOKENS":
                raise RuntimeError("Gemini cortou a resposta no limite de tokens")
            raise RuntimeError(f"Gemini devolveu resposta vazia (finishReason={fim})")
        return texto_resp


# ─── Backend: Nvidia NIM ──────────────────────────────────────────────────────
async def _chamar_nim(system: str, texto: str, partes: list[dict],
                      max_tokens: int, temperature: float,
                      reasoning: bool) -> str:
    if not NIM_API_KEY:
        raise RuntimeError("NVIDIA_NIM_API_KEY não configurada")

    if partes:
        conteudo: Any = ([{"type": "text", "text": texto}] if texto else []) + \
                        [_para_openai(p) for p in partes]
    else:
        conteudo = texto

    mensagens = []
    if system:
        mensagens.append({"role": "system", "content": system})
    mensagens.append({"role": "user", "content": conteudo})

    payload: dict[str, Any] = {
        "model": NIM_MODEL,
        "messages": mensagens,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": 0.9,
    }
    if reasoning and NIM_REASONING_BUDGET > 0:
        payload["reasoning_budget"] = NIM_REASONING_BUDGET

    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(
            f"{NIM_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {NIM_API_KEY}",
                     "Content-Type": "application/json"},
            json=payload,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"NIM HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()["choices"][0]["message"]["content"].strip()


# ─── Interface pública ────────────────────────────────────────────────────────
async def chamar_modelo(system: str, texto: str,
                        partes: Optional[list[dict]] = None,
                        max_tokens: int = 1024, temperature: float = 0.6,
                        reasoning: bool = True) -> str:
    """
    Envia prompt e mídia ao provedor ativo e devolve o texto da resposta.
    O rascunho de raciocínio, quando existir, não é exposto.
    """
    partes = partes or []
    if PROVIDER == "gemini":
        return await _chamar_gemini(system, texto, partes, max_tokens, temperature)
    return await _chamar_nim(system, texto, partes, max_tokens, temperature, reasoning)


async def listar_modelos() -> list[str]:
    """
    Lista os modelos que a chave atual pode usar. Útil porque os nomes mudam
    entre gerações e um nome errado devolve 404.
    """
    if PROVIDER != "gemini":
        return [NIM_MODEL]
    if not GEMINI_API_KEY:
        return []
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{GEMINI_BASE_URL}/models",
                                headers={"x-goog-api-key": GEMINI_API_KEY})
        if resp.status_code != 200:
            logger.warning("Falha ao listar modelos: HTTP %s — %s",
                           resp.status_code, resp.text[:200])
            return []
        return [
            m["name"].removeprefix("models/")
            for m in resp.json().get("models", [])
            if "generateContent" in m.get("supportedGenerationMethods", [])
        ]
