"""
conhecimento.py — Base de conhecimento clínica do Sr. Edilson

Carrega os documentos de referência (ficha clínica, laudos, protocolo de
medicamentos, restrições) que o Dr. João Holanda precisa ter sempre à mão.

Por que no prompt e não na memória vetorial:
As restrições absolutas — AINEs proibidos, cúrcuma com BioPerine suspensa,
interações medicamentosas — precisam estar presentes em TODA inferência. Uma
busca semântica pode não recuperar a regra certa na hora em que ela importa,
e aqui o custo de errar é clínico. A ficha é pequena o bastante para caber no
contexto inteiro, então ela vai por completo, sempre.

Os arquivos ficam em /app/conhecimento (volume montado), nunca na imagem nem
no repositório: são dados de saúde identificáveis do paciente.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger("dr-joao-holanda.conhecimento")

DIR_CONHECIMENTO = Path(os.getenv("DIR_CONHECIMENTO", "/app/conhecimento"))
LIMITE_CARACTERES = int(os.getenv("CONHECIMENTO_MAX_CHARS", "60000"))

_cache: str | None = None


def carregar(forcar: bool = False) -> str:
    """
    Lê todos os .md do diretório de conhecimento, em ordem alfabética.
    O resultado é mantido em cache — a ficha muda raramente e relê-la a cada
    mensagem seria desperdício.
    """
    global _cache
    if _cache is not None and not forcar:
        return _cache

    if not DIR_CONHECIMENTO.is_dir():
        logger.warning("Diretório de conhecimento ausente: %s", DIR_CONHECIMENTO)
        _cache = ""
        return _cache

    arquivos = sorted(DIR_CONHECIMENTO.glob("*.md"))
    if not arquivos:
        logger.warning("Nenhum documento .md em %s", DIR_CONHECIMENTO)
        _cache = ""
        return _cache

    blocos: list[str] = []
    total = 0
    for arq in arquivos:
        try:
            texto = arq.read_text(encoding="utf-8").strip()
        except Exception as e:
            logger.warning("Falha ao ler %s: %s", arq.name, e)
            continue
        if not texto:
            continue
        if total + len(texto) > LIMITE_CARACTERES:
            logger.warning("Limite de %d caracteres atingido — %s e seguintes "
                           "ficaram de fora.", LIMITE_CARACTERES, arq.name)
            break
        blocos.append(texto)
        total += len(texto)
        logger.info("Conhecimento carregado: %s (%d caracteres)", arq.name, len(texto))

    _cache = "\n\n---\n\n".join(blocos)
    logger.info("Base de conhecimento: %d documento(s), %d caracteres.",
                len(blocos), len(_cache))
    return _cache


def disponivel() -> bool:
    return bool(carregar())


def resumo() -> dict:
    """Estado da base, para o /health."""
    conteudo = carregar()
    arquivos = sorted(p.name for p in DIR_CONHECIMENTO.glob("*.md")) \
        if DIR_CONHECIMENTO.is_dir() else []
    return {"carregada": bool(conteudo), "documentos": arquivos,
            "caracteres": len(conteudo)}
