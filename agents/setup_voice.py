#!/usr/bin/env python3
"""
setup_voice.py — Configura a voz do Dr. João Holanda Cavalcante

Faz duas coisas a partir dos áudios de referência do Sr. Edilson:

1. CLONA A VOZ (ElevenLabs Instant Voice Cloning)
   A voz do Dr. João Holanda é derivada da do Sr. Edilson e gravada um tom
   ABAIXO — João Holanda era o pai dele, e a voz de um pai costuma ser mais
   grave que a do filho. O rebaixamento é feito com ffmpeg antes do envio.

2. EXTRAI A LINHA DE BASE VOCAL
   Analisa a prosódia habitual do Sr. Edilson (energia, ritmo, estabilidade,
   volume, articulação) com o Nemotron Omni e grava no Zep. É contra esse
   perfil que o agente compara cada nota de voz futura para perceber quando
   algo mudou.

CONSENTIMENTO: só execute com autorização explícita do Sr. Edilson para uso
da voz dele. Os termos da ElevenLabs exigem que você tenha esse direito.

Uso:
    python3 setup_voice.py audio1.ogg audio2.ogg
    python3 setup_voice.py --semitons -2.5 audio1.ogg audio2.ogg
    python3 setup_voice.py --apenas-baseline audio1.ogg
"""

import argparse
import asyncio
import base64
import logging
import os
import subprocess
import sys

import httpx
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("setup-voice")

ELEVENLABS_API_KEY  = os.getenv("ELEVENLABS_API_KEY", "")
NVIDIA_NIM_API_KEY  = os.getenv("NVIDIA_NIM_API_KEY", "")
NVIDIA_NIM_BASE_URL = os.getenv("NVIDIA_NIM_BASE_URL", "https://integrate.api.nvidia.com/v1")
NIM_CHAT_MODEL      = os.getenv("NIM_CHAT_MODEL",
                                "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning")

# Quanto mais grave que a voz do Sr. Edilson. -2 semitons ≈ diferença natural
# entre a voz de um pai e a de um filho, sem soar artificial.
SEMITONS_PADRAO = -2.0


# ─── Processamento de áudio ───────────────────────────────────────────────────
def to_wav(caminho: str, semitons: float = 0.0) -> bytes:
    """
    Converte para WAV 16 kHz mono, opcionalmente transpondo o tom.

    A transposição usa asetrate (que muda tom E duração) seguida de atempo
    (que restaura a duração original) — assim a voz fica mais grave sem
    ficar mais lenta, que é o efeito de "fita tocando devagar".
    """
    fator = 2 ** (semitons / 12.0)          # -2 semitons → ~0.891
    taxa_base = 16000
    if semitons:
        filtro = (f"asetrate={int(taxa_base * fator)},"
                  f"aresample={taxa_base},"
                  f"atempo={1/fator:.6f}")
    else:
        filtro = "aresample=16000"

    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-i", caminho, "-af", filtro,
         "-ar", str(taxa_base), "-ac", "1", "-f", "wav", "pipe:1"],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg falhou em {caminho}: {proc.stderr.decode()[:300]}")
    return proc.stdout


# ─── 1. Clonagem da voz no ElevenLabs ─────────────────────────────────────────
def clonar_voz_detalhado(arquivos: list[str], semitons: float,
                         nome: str = "Dr. João Holanda Cavalcante") -> dict:
    """
    Cria a voz do Dr. João Holanda no ElevenLabs a partir das amostras,
    rebaixadas em `semitons`.

    Devolve sempre um dicionário com o resultado COMPLETO, incluindo o corpo
    da resposta em caso de erro. Engolir a mensagem do ElevenLabs e substituí-la
    por um palpite já custou uma rodada de diagnóstico: o plano estava correto
    e a causa real era outra.
    """
    if not ELEVENLABS_API_KEY:
        return {"ok": False, "status": 0,
                "erro": "ELEVENLABS_API_KEY não configurada no .env"}

    logger.info("Preparando %d amostra(s) com %+.1f semitons...", len(arquivos), semitons)
    files = []
    detalhes = []
    for i, caminho in enumerate(arquivos):
        try:
            wav = to_wav(caminho, semitons)
        except Exception as e:
            return {"ok": False, "status": 0,
                    "erro": f"Falha ao converter {os.path.basename(caminho)}: {e}"}
        if len(wav) < 16000:  # menos de ~0,5 s de áudio
            return {"ok": False, "status": 0,
                    "erro": f"{os.path.basename(caminho)} ficou curto demais "
                            f"após a conversão ({len(wav)} bytes). A amostra "
                            f"precisa ter pelo menos alguns segundos de fala."}
        files.append(("files", (f"amostra_{i+1}.wav", wav, "audio/wav")))
        detalhes.append(f"{os.path.basename(caminho)} → {len(wav)//1024} KB")
        logger.info("  %s", detalhes[-1])

    data = {
        "name": nome,
        "description": ("Voz masculina brasileira, idosa, tom grave e cálido. "
                        "Sotaque cearense do Cariri (Crato). Médico acolhedor."),
        "labels": '{"idioma":"pt-BR","genero":"masculino","idade":"idoso",'
                  '"sotaque":"cearense-cariri","uso":"assistente-clinico"}',
    }

    try:
        resp = httpx.post(
            "https://api.elevenlabs.io/v1/voices/add",
            headers={"xi-api-key": ELEVENLABS_API_KEY},
            data=data, files=files, timeout=300,
        )
    except Exception as e:
        return {"ok": False, "status": 0, "erro": f"Erro de rede: {e}",
                "amostras": detalhes}

    if resp.status_code not in (200, 201):
        corpo = resp.text[:600]
        logger.error("Clonagem falhou: HTTP %s — %s", resp.status_code, corpo)
        return {"ok": False, "status": resp.status_code, "erro": corpo,
                "amostras": detalhes, "dica": _dica_para_erro(resp.status_code, corpo)}

    voice_id = resp.json().get("voice_id")
    logger.info("Voz criada ✓  voice_id=%s", voice_id)
    return {"ok": True, "status": resp.status_code, "voice_id": voice_id,
            "amostras": detalhes}


def _dica_para_erro(status: int, corpo: str) -> str:
    """Traduz a resposta do ElevenLabs em uma orientação prática."""
    c = corpo.lower()
    if "missing_permissions" in c or "voices_write" in c:
        return ("A chave de API não tem permissão de escrita em vozes. No "
                "ElevenLabs, em API Keys, edite a chave e marque 'Voices: "
                "Write' (ou use uma chave com acesso total).")
    if status == 401:
        return ("Chave recusada. Confirme ELEVENLABS_API_KEY e se ela pertence "
                "à conta do plano Starter.")
    if "can_not_use_instant_voice_cloning" in c or "subscription" in c:
        return ("A conta não liberou clonagem instantânea. Se o plano acabou "
                "de ser assinado, aguarde alguns minutos e tente de novo.")
    if "verification" in c or "captcha" in c:
        return ("O ElevenLabs pede verificação de identidade para clonar voz. "
                "Acesse elevenlabs.io, vá em Voices → Add Voice e conclua a "
                "verificação uma vez pelo site.")
    if status == 422:
        return ("O ElevenLabs recusou as amostras. Verifique se têm ao menos "
                "30 segundos de fala clara, sem música nem ruído.")
    if status == 429:
        return "Limite de requisições atingido. Aguarde um minuto."
    return "Veja o corpo da resposta acima para o motivo exato."


def clonar_voz(arquivos: list[str], semitons: float) -> str | None:
    """Compatibilidade: devolve apenas o voice_id, ou None em caso de falha."""
    r = clonar_voz_detalhado(arquivos, semitons)
    return r.get("voice_id") if r.get("ok") else None


# ─── 2. Linha de base vocal do Sr. Edilson ────────────────────────────────────
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


async def extrair_baseline(arquivos: list[str]) -> dict[str, str]:
    """Analisa as amostras (sem transposição) e consolida o perfil habitual."""
    if not NVIDIA_NIM_API_KEY:
        logger.error("NVIDIA_NIM_API_KEY não configurada — baseline ignorado.")
        return {}

    perfis: list[dict[str, str]] = []
    async with httpx.AsyncClient(timeout=180) as client:
        for caminho in arquivos:
            wav = to_wav(caminho, 0.0)      # voz original, sem alterar o tom
            b64 = base64.b64encode(wav).decode()
            resp = await client.post(
                f"{NVIDIA_NIM_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {NVIDIA_NIM_API_KEY}",
                         "Content-Type": "application/json"},
                json={
                    "model": NIM_CHAT_MODEL,
                    "messages": [{"role": "user", "content": [
                        {"type": "text", "text": PROSODIA_PROMPT},
                        {"type": "audio_url",
                         "audio_url": {"url": f"data:audio/wav;base64,{b64}"}},
                    ]}],
                    "max_tokens": 300, "temperature": 0.2,
                },
            )
            if resp.status_code != 200:
                logger.warning("Análise falhou para %s: HTTP %s — %s",
                               os.path.basename(caminho), resp.status_code,
                               resp.text[:200])
                continue

            texto = resp.json()["choices"][0]["message"]["content"]
            perfil = {}
            for linha in texto.splitlines():
                if ":" in linha:
                    k, _, v = linha.partition(":")
                    if k.strip().isalpha():
                        perfil[k.strip().upper()] = v.strip().lower()
            perfis.append(perfil)
            logger.info("Perfil de %s: %s", os.path.basename(caminho), perfil)

    if not perfis:
        return {}

    # Consolida por moda: o valor mais frequente em cada dimensão
    consolidado: dict[str, str] = {}
    for campo in ("ENERGIA", "RITMO", "ESTABILIDADE", "VOLUME",
                  "ARTICULACAO", "RESPIRACAO"):
        valores = [p[campo] for p in perfis if p.get(campo)]
        if valores:
            consolidado[campo] = max(set(valores), key=valores.count)
    return consolidado


async def gravar_baseline_no_zep(baseline: dict[str, str]) -> bool:
    """Persiste a linha de base como fato clínico na categoria voz_baseline."""
    try:
        from integrations.zep_memory import add_clinical_fact
        texto = "BASELINE_VOCAL:" + ";".join(f"{k}: {v}" for k, v in baseline.items())
        ok = await add_clinical_fact(texto, "voz_baseline")
        logger.info("Baseline gravado no Zep ✓" if ok else "Falha ao gravar baseline no Zep")
        return ok
    except Exception as e:
        logger.error("Erro ao gravar baseline: %s", e)
        return False


# ─── Entrada ──────────────────────────────────────────────────────────────────
async def main() -> int:
    ap = argparse.ArgumentParser(description="Configura a voz do Dr. João Holanda")
    ap.add_argument("audios", nargs="+", help="Arquivos .ogg/.mp3/.wav do Sr. Edilson")
    ap.add_argument("--semitons", type=float, default=SEMITONS_PADRAO,
                    help=f"Transposição do tom (padrão {SEMITONS_PADRAO}, mais grave)")
    ap.add_argument("--apenas-baseline", action="store_true",
                    help="Só extrai o perfil vocal, sem clonar a voz")
    ap.add_argument("--apenas-clone", action="store_true",
                    help="Só clona a voz, sem extrair o perfil")
    args = ap.parse_args()

    faltando = [a for a in args.audios if not os.path.isfile(a)]
    if faltando:
        logger.error("Arquivo(s) não encontrado(s): %s", ", ".join(faltando))
        return 1

    print()
    print("═" * 62)
    print("  Configuração da voz — Dr. João Holanda Cavalcante")
    print("═" * 62)

    voice_id = None
    if not args.apenas_baseline:
        print(f"\n▸ Clonando voz ({args.semitons:+.1f} semitons — mais grave)...")
        voice_id = clonar_voz(args.audios, args.semitons)

    baseline = {}
    if not args.apenas_clone:
        print("\n▸ Extraindo linha de base vocal do Sr. Edilson...")
        baseline = await extrair_baseline(args.audios)
        if baseline:
            print(f"  Perfil habitual: {baseline}")
            await gravar_baseline_no_zep(baseline)

    print()
    print("═" * 62)
    if voice_id:
        print(f"  ELEVENLABS_VOICE_ID={voice_id}")
        print()
        print("  Grave no .env e reinicie o agente:")
        print(f"    sed -i '/^ELEVENLABS_VOICE_ID=/d' /root/automacao/.env")
        print(f"    echo 'ELEVENLABS_VOICE_ID={voice_id}' >> /root/automacao/.env")
        print( "    docker compose restart joao_holanda")
    if baseline:
        print(f"\n  Linha de base vocal registrada: {len(baseline)} dimensões")
    print("═" * 62)
    print()
    return 0 if (voice_id or baseline) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
