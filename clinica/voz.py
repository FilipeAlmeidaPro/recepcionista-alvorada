"""Camada de voz: síntese, transcrição e o tempo de cada estágio.

Duas decisões que se pagam:

* **TTS pelo `say` do macOS.** O plano previa Piper, cujo pacote PT-BR só tem
  vozes masculinas — problema real para uma recepcionista. O macOS traz dez
  vozes brasileiras nativas, `Luciana` entre elas, sem instalar nada e sem
  baixar modelo. Custo zero de verdade, não free tier.
* **STT pelo Whisper na Groq.** Cota separada da do chat: 7 200 segundos de
  áudio por hora. Rodar a suíte de áudio não come a cota do orquestrador.

Todo estágio devolve o próprio tempo. Sem isso não dá para dizer onde o turno
demora, e "o gargalo é o STT" vira opinião em vez de medida.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path

from clinica.provedor import (CotaDiariaEsgotada, _contexto_ssl,
                              _espera_pedida, _garantir_env)

VOZ_PADRAO = "Luciana"          # feminina, pt_BR, nativa do macOS
TAXA = 16_000                   # o Whisper reamostra para 16 kHz de qualquer jeito
URL_STT = "https://api.groq.com/openai/v1/audio/transcriptions"
MODELO_STT = "whisper-large-v3-turbo"


@dataclass(frozen=True)
class Audio:
    caminho: Path
    duracao_s: float
    ms_gasto: float

    def existe(self) -> bool:
        return self.caminho.exists()


@dataclass(frozen=True)
class Transcricao:
    texto: str
    ms_gasto: float
    duracao_audio_s: float

    @property
    def fator_tempo_real(self) -> float:
        """Quantas vezes mais rápido que tempo real. Abaixo de 1 é inviável."""
        return (self.duracao_audio_s * 1000 / self.ms_gasto) if self.ms_gasto else 0.0


def duracao(caminho: Path) -> float:
    with wave.open(str(caminho), "rb") as w:
        return w.getnframes() / float(w.getframerate())


# --- síntese -----------------------------------------------------------------

class SinteseMacOS:
    """`say` nativo. Sem download, sem servidor, sem chave."""

    nome = "macos:say"

    def __init__(self, voz: str = VOZ_PADRAO, palavras_por_minuto: int = 190):
        if not shutil.which("say"):
            raise RuntimeError("`say` não encontrado — síntese nativa só no macOS.")
        self.voz = voz
        self.ritmo = palavras_por_minuto

    def falar(self, texto: str, destino: Path) -> Audio:
        destino.parent.mkdir(parents=True, exist_ok=True)
        inicio = time.perf_counter()
        subprocess.run(
            ["say", "-v", self.voz, "-r", str(self.ritmo),
             "--data-format=LEI16@16000", "-o", str(destino), texto],
            check=True, capture_output=True)
        ms = (time.perf_counter() - inicio) * 1000
        return Audio(destino, duracao(destino), ms)


# --- transcrição -------------------------------------------------------------

class TranscricaoGroq:
    """Whisper large-v3-turbo. Não é streaming: segmenta-se e manda o trecho."""

    nome = f"groq:{MODELO_STT}"

    def __init__(self, chave: str | None = None, *, modelo: str = MODELO_STT,
                 timeout: float = 60.0, tentativas: int = 5):
        _garantir_env()
        self.chave = chave or os.environ.get("GROQ_API_KEY")
        if not self.chave:
            raise RuntimeError("Falta GROQ_API_KEY para a transcrição.")
        self.modelo = modelo
        self.timeout = timeout
        self.tentativas = tentativas
        self.espera_total_s = 0.0
        self._ssl = _contexto_ssl()

    def transcrever(self, caminho: Path, *, idioma: str = "pt",
                    contexto: str | None = None) -> Transcricao:
        limite = uuid.uuid4().hex
        campos = {"model": self.modelo, "language": idioma,
                  "response_format": "json", "temperature": "0"}
        if contexto:
            # O `prompt` do Whisper enviesa a saída: passar os nomes dos
            # profissionais reduz "Taís" virando "Thais" e vice-versa.
            campos["prompt"] = contexto[:800]

        partes = []
        for chave, valor in campos.items():
            partes.append(f"--{limite}\r\nContent-Disposition: form-data; "
                          f'name="{chave}"\r\n\r\n{valor}\r\n'.encode())
        partes.append(f"--{limite}\r\nContent-Disposition: form-data; "
                      f'name="file"; filename="{caminho.name}"\r\n'
                      f"Content-Type: audio/wav\r\n\r\n".encode())
        partes.append(caminho.read_bytes())
        partes.append(f"\r\n--{limite}--\r\n".encode())
        corpo = b"".join(partes)

        req = urllib.request.Request(
            URL_STT, data=corpo,
            headers={"Authorization": f"Bearer {self.chave}",
                     "Content-Type": f"multipart/form-data; boundary={limite}",
                     "User-Agent": "recepcionista-alvorada/1.0"})
        # Mesmo tratamento do cliente de chat: o teto do Whisper é de segundos
        # de áudio por hora, e uma rodada de 40 transcrições encosta nele.
        # A espera de rate limit fica fora da latência reportada — senão o
        # número de "quanto demora o STT" vira ficção.
        for tentativa in range(self.tentativas):
            inicio = time.perf_counter()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout,
                                            context=self._ssl) as r:
                    dados = json.loads(r.read())
                ms = (time.perf_counter() - inicio) * 1000
                break
            except urllib.error.HTTPError as e:
                detalhe = " ".join(e.read()[:400].decode(errors="replace").split())
                if e.code == 429 and ("per day" in detalhe.lower() or "PerDay" in detalhe):
                    raise CotaDiariaEsgotada(f"{self.nome}: {detalhe[:200]}") from e
                if e.code in (429, 500, 502, 503) and tentativa < self.tentativas - 1:
                    espera = _espera_pedida(detalhe) or 2 ** tentativa * 3
                    self.espera_total_s += espera
                    time.sleep(espera)
                    continue
                raise RuntimeError(f"{self.nome}: HTTP {e.code} — {detalhe[:200]}") from e
        return Transcricao((dados.get("text") or "").strip(), ms, duracao(caminho))


# --- degradação do áudio, para os cenários de robustez ----------------------

def adicionar_ruido(entrada: Path, saida: Path, *, tipo: str = "pink",
                    volume: float = 0.06) -> Path:
    """Mistura ruído de fundo. O plano cita `sox`; uso `ffmpeg`, que já está aqui."""
    saida.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(entrada),
         "-f", "lavfi", "-i", f"anoisesrc=c={tipo}:r={TAXA}:a={volume}",
         "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=first[out]",
         "-map", "[out]", "-ar", str(TAXA), "-ac", "1", "-c:a", "pcm_s16le",
         str(saida)], check=True, capture_output=True)
    return saida


def cortar(entrada: Path, saida: Path, *, ate_s: float) -> Path:
    """Corta o áudio no meio — é o que acontece quando o paciente interrompe."""
    saida.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(entrada),
         "-t", str(ate_s), "-ar", str(TAXA), "-ac", "1", "-c:a", "pcm_s16le",
         str(saida)], check=True, capture_output=True)
    return saida
