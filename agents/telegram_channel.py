"""
telegram_channel.py — Canal Telegram do Dr. João Holanda Cavalcante

Alternativa ao WhatsApp: a Meta bane números que conectam via Baileys
(engenharia reversa do WhatsApp Web). O Telegram tem API oficial e
gratuita para bots, sem risco de banimento e sem precisar de chip.

Usa LONG POLLING (getUpdates), não webhook: o Telegram exige HTTPS com
certificado válido para webhooks, e o polling dispensa domínio, TLS e
abertura de porta — o agente é quem inicia a conexão de saída.

Modalidades suportadas, todas encaminhadas ao Nemotron Omni:
  • texto
  • nota de voz (OGG/Opus, mesmo formato do WhatsApp)
  • foto (refeição, exame impresso)
  • vídeo
  • documento (PDF de exame)
"""

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

import httpx

logger = logging.getLogger("dr-joao-holanda.telegram")

API_BASE = "https://api.telegram.org"
# Limite de download da Bot API do Telegram
MAX_FILE_BYTES = 20 * 1024 * 1024


class TelegramChannel:
    """
    Canal de conversa via Telegram.

    handler: async (texto, media_parts, tipo, chat_id, nome) -> resposta em texto
    to_wav:  async (bytes) -> WAV 16 kHz (conversão do áudio recebido)
    to_ogg:  async (bytes) -> OGG/Opus (conversão da resposta em voz)
    tts:     async (texto) -> bytes de áudio, ou None
    """

    def __init__(
        self,
        token: str,
        allowed_ids: set[int],
        handler: Callable[..., Awaitable[str]],
        build_media_part: Callable[[str, bytes, str], dict],
        blocos_de_documento: Optional[Callable[[bytes, str], list[dict]]] = None,
        limpar_texto: Optional[Callable[[str], str]] = None,
        responder_em_voz: Optional[Callable[[str], bool]] = None,
        admin_ids: Optional[set] = None,
        tratar_admin: Optional[Callable[[str, str], Awaitable[Optional[str]]]] = None,
        to_wav: Optional[Callable[[bytes], Awaitable[Optional[bytes]]]] = None,
        to_ogg: Optional[Callable[[bytes], Awaitable[Optional[bytes]]]] = None,
        tts: Optional[Callable[[str], Awaitable[Optional[bytes]]]] = None,
        transcrever: Optional[Callable[[bytes], Awaitable[str]]] = None,
    ):
        self.token = token
        self.allowed_ids = allowed_ids
        self.handler = handler
        self.build_media_part = build_media_part
        # Sem conversor de documento, trata tudo como imagem (PDF falharia)
        self.blocos_de_documento = blocos_de_documento or (
            lambda raw, mime: [build_media_part("image", raw, mime)])
        self.limpar_texto = limpar_texto
        self.responder_em_voz = responder_em_voz
        self.admin_ids = admin_ids or set()
        self.tratar_admin = tratar_admin
        self.to_wav = to_wav
        self.to_ogg = to_ogg
        self.tts = tts
        self.transcrever = transcrever
        self._offset = 0
        self._parar = False

    # ─── Chamadas à Bot API ──────────────────────────────────────────────────
    def _url(self, metodo: str) -> str:
        return f"{API_BASE}/bot{self.token}/{metodo}"

    async def _api(self, metodo: str, **params) -> dict:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(self._url(metodo), json=params)
            if r.status_code != 200:
                logger.warning("Telegram %s falhou: HTTP %s — %s",
                               metodo, r.status_code, r.text[:200])
                return {}
            return r.json().get("result", {})

    @staticmethod
    def _registrar_falha(tarefa: "asyncio.Task") -> None:
        """Traz à tona exceções de tarefas em segundo plano."""
        if tarefa.cancelled():
            return
        erro = tarefa.exception()
        if erro:
            logger.error("Falha ao tratar mensagem: %r", erro, exc_info=erro)

    async def enviar_texto(self, chat_id: int, texto: str) -> None:
        # Sem parse_mode, o Telegram mostra ** e _ literalmente; com ele, um
        # asterisco solto faz a API recusar a mensagem. A marcação é removida.
        limpo = self.limpar_texto(texto) if self.limpar_texto else texto
        # O Telegram corta mensagens acima de 4096 caracteres
        for i in range(0, len(limpo), 4000):
            await self._api("sendMessage", chat_id=chat_id, text=limpo[i:i + 4000])

    async def enviar_voz(self, chat_id: int, audio: bytes) -> bool:
        """Envia como nota de voz. Exige OGG/Opus — converte se necessário."""
        ogg = await self.to_ogg(audio) if self.to_ogg else None
        if not ogg:
            return False
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(
                self._url("sendVoice"),
                data={"chat_id": str(chat_id)},
                files={"voice": ("resposta.ogg", ogg, "audio/ogg")},
            )
            if r.status_code != 200:
                logger.warning("Telegram sendVoice falhou: %s", r.text[:200])
                return False
            return True

    async def _baixar_arquivo(self, file_id: str) -> Optional[bytes]:
        """Resolve o file_id e baixa o conteúdo."""
        info = await self._api("getFile", file_id=file_id)
        caminho = info.get("file_path")
        if not caminho:
            return None
        if info.get("file_size", 0) > MAX_FILE_BYTES:
            logger.warning("Arquivo acima do limite da Bot API (20 MB)")
            return None

        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.get(f"{API_BASE}/file/bot{self.token}/{caminho}")
            return r.content if r.status_code == 200 else None

    # ─── Interpretação da mensagem ───────────────────────────────────────────
    async def _extrair(self, msg: dict) -> tuple[str, list[dict], str]:
        """Devolve (texto, blocos_de_midia, tipo) a partir de uma mensagem."""
        partes: list[dict] = []

        if "text" in msg:
            return msg["text"], partes, "texto"

        legenda = msg.get("caption", "")

        # Nota de voz ou arquivo de áudio
        if "voice" in msg or "audio" in msg:
            origem = msg.get("voice") or msg["audio"]
            bruto = await self._baixar_arquivo(origem["file_id"])
            if not bruto:
                return "", partes, "audio"

            texto = legenda
            if self.transcrever and not texto:
                texto = await self.transcrever(bruto)

            if self.to_wav:
                wav = await self.to_wav(bruto)
                if wav:
                    partes.append(self.build_media_part("audio", wav, "audio/wav"))
            return texto, partes, "audio"

        # Foto — o Telegram manda várias resoluções; a última é a maior
        if "photo" in msg:
            bruto = await self._baixar_arquivo(msg["photo"][-1]["file_id"])
            if not bruto:
                return "", partes, "image"
            partes.append(self.build_media_part("image", bruto, "image/jpeg"))
            texto = legenda or (
                "O Sr. Edilson enviou uma foto. Analise a imagem: se for uma "
                "refeição, avalie do ponto de vista nutricional considerando as "
                "restrições renais e oncológicas dele; se for um exame, leia os "
                "valores e interprete-os."
            )
            return texto, partes, "image"

        if "video" in msg:
            bruto = await self._baixar_arquivo(msg["video"]["file_id"])
            if not bruto:
                return "", partes, "video"
            mime = msg["video"].get("mime_type", "video/mp4").split(";")[0]
            partes.append(self.build_media_part("video", bruto, mime))
            return (legenda or "O Sr. Edilson enviou um vídeo. Observe o que "
                    "acontece e comente com acolhimento."), partes, "video"

        # Documento — PDF de exame chega por aqui
        if "document" in msg:
            doc = msg["document"]
            bruto = await self._baixar_arquivo(doc["file_id"])
            if not bruto:
                return "", partes, "document"
            mime = doc.get("mime_type", "application/pdf").split(";")[0]
            # PDF vira uma imagem por página — o Omni não aceita PDF direto
            partes.extend(self.blocos_de_documento(bruto, mime))
            if not partes:
                return "", partes, "document"
            nome = doc.get("file_name", "documento")
            texto = legenda or (
                f"O Sr. Edilson enviou o documento '{nome}' ({len(partes)} página(s)). "
                f"Leia o conteúdo. Se for um exame laboratorial, extraia os "
                f"marcadores (PSA, eTFG, creatinina) com seus valores e "
                f"interprete-os segundo as regras clínicas."
            )
            return texto, partes, "document"

        return "", partes, "desconhecido"

    async def _tratar(self, update: dict) -> None:
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return

        chat_id = msg.get("chat", {}).get("id")
        if chat_id is None:
            return

        # Filtro de contatos: assistente clínico privado, não chatbot aberto.
        # Sem lista configurada, registra o ID para facilitar o cadastro inicial.
        if self.allowed_ids and chat_id not in self.allowed_ids:
            quem = msg.get("from", {})
            logger.warning("Telegram: mensagem ignorada de chat_id=%s (%s %s)",
                           chat_id, quem.get("first_name", ""), quem.get("username", ""))
            return
        if not self.allowed_ids:
            logger.warning("TELEGRAM_ALLOWED_IDS vazio — respondendo a "
                           "chat_id=%s. Adicione-o ao .env para restringir.", chat_id)

        await self._api("sendChatAction", chat_id=chat_id, action="typing")

        texto, partes, tipo = await self._extrair(msg)

        # Canal de administração: quem configurou o agente pode deixar
        # orientações permanentes de comportamento. Se a mensagem for tratada
        # como administrativa, ela não segue para o fluxo clínico.
        if chat_id in self.admin_ids and self.tratar_admin and tipo == "texto":
            try:
                _q = msg.get("from", {})
                _nome = " ".join(
                    x for x in (_q.get("first_name"), _q.get("last_name")) if x)
                resposta_admin = await self.tratar_admin(texto, _nome or str(chat_id))
            except Exception as e:
                logger.error("Erro no comando administrativo: %s", e)
                resposta_admin = f"Não consegui processar: {e}"
            if resposta_admin:
                await self.enviar_texto(chat_id, resposta_admin)
                return
        if not texto and not partes:
            await self.enviar_texto(
                chat_id, "Não consegui ler essa mensagem, Sr. Edilson. "
                         "Pode mandar de novo?")
            return

        try:
            quem = msg.get("from", {})
            nome_perfil = " ".join(
                x for x in (quem.get("first_name"), quem.get("last_name")) if x)
            resposta = await self.handler(texto, partes, tipo,
                                          chat_id, nome_perfil)
        except Exception as e:
            logger.error("Erro no processamento (Telegram): %s", e)
            await self.enviar_texto(
                chat_id, "Desculpe, tive uma dificuldade técnica agora. "
                         "Pode me repetir o que disse?")
            return

        # A resposta acompanha a modalidade da pergunta: quem manda áudio,
        # foto ou vídeo ouve de volta; quem escreve, lê de volta. Se a voz
        # falhar, o texto entra no lugar para a resposta não se perder.
        enviou_voz = False
        if self.tts and (self.responder_em_voz is None
                         or self.responder_em_voz(tipo)):
            audio = await self.tts(resposta)
            if audio:
                enviou_voz = await self.enviar_voz(chat_id, audio)
        if not enviou_voz:
            await self.enviar_texto(chat_id, resposta)

    # ─── Laço de long polling ────────────────────────────────────────────────
    async def rodar(self) -> None:
        """
        Consome atualizações continuamente. Cada ciclo espera até 30 s no
        servidor do Telegram (long polling), então não há busy-wait.
        """
        eu = await self._api("getMe")
        if not eu:
            logger.error("Telegram: token inválido — canal não iniciado.")
            return
        logger.info("Telegram ativo: @%s (%s)", eu.get("username"), eu.get("first_name"))

        # Descarta webhook eventualmente configurado, que bloquearia o getUpdates
        await self._api("deleteWebhook", drop_pending_updates=False)

        while not self._parar:
            try:
                updates = await self._api(
                    "getUpdates", offset=self._offset, timeout=30,
                    allowed_updates=["message", "edited_message"],
                )
                for u in updates or []:
                    self._offset = u["update_id"] + 1
                    # Uma tarefa por mensagem: uma inferência lenta não
                    # segura a fila das demais. O callback registra falhas —
                    # sem ele, uma exceção aqui some e a pessoa fica sem
                    # resposta, sem nada no log.
                    tarefa = asyncio.create_task(self._tratar(u))
                    tarefa.add_done_callback(self._registrar_falha)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Telegram polling: %s — nova tentativa em 5 s", e)
                await asyncio.sleep(5)

    def parar(self) -> None:
        self._parar = True
