"""Detecção de fala por energia. Sem modelo, sem download, sem dependência.

O plano previa Silero. Silero é melhor — e é um modelo de 1,8 MB mais PyTorch,
que é a diferença entre `git clone && python3` e vinte minutos de instalação.
Para separar fala de silêncio numa ligação telefônica, energia com histerese
resolve; o que ela erra é ruído estacionário alto, e é justamente aí que a
suíte de áudio mostrou que o Whisper aguenta bem.

Três parâmetros importam, e são os mesmos aqui e no navegador:

* `margem_db` — quanto acima do piso de ruído conta como fala. O piso é medido
  na própria gravação, não fixado: cada microfone e cada sala têm o seu.
* `min_fala_s` — abaixo disso é estalo, tosse, batida de mesa. Não abre turno.
* `silencio_final_s` — quanto de silêncio fecha o turno. É o parâmetro que
  decide se o agente parece atento ou apressado, e é o que corta a fala do
  paciente no meio quando fica curto demais. A suíte de áudio mediu o custo
  disso: truncar derruba a extração de entidade de 90% para 60%.
"""
from __future__ import annotations

import array
import math
import wave
from dataclasses import dataclass
from pathlib import Path

JANELA_S = 0.02          # 20 ms, o padrão de VAD telefônico
MARGEM_DB = 12.0
MIN_FALA_S = 0.25
SILENCIO_FINAL_S = 0.60
PISO_MINIMO_DB = -65.0   # abaixo disso é silêncio digital, não sala quieta


@dataclass(frozen=True)
class Segmento:
    inicio_s: float
    fim_s: float

    @property
    def duracao_s(self) -> float:
        return self.fim_s - self.inicio_s


def _quadros_db(caminho: Path, janela_s: float) -> tuple[list[float], float]:
    """Energia RMS de cada janela, em dBFS."""
    with wave.open(str(caminho), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError("esperado PCM 16 bits")
        taxa, canais = w.getframerate(), w.getnchannels()
        amostras = array.array("h", w.readframes(w.getnframes()))
    if canais > 1:
        amostras = array.array("h", amostras[::canais])

    por_janela = max(1, int(taxa * janela_s))
    dbs: list[float] = []
    for i in range(0, len(amostras) - por_janela + 1, por_janela):
        bloco = amostras[i:i + por_janela]
        soma = sum(float(v) * v for v in bloco)
        rms = math.sqrt(soma / len(bloco)) / 32768.0
        dbs.append(20 * math.log10(rms) if rms > 1e-9 else -120.0)
    return dbs, por_janela / taxa


def piso_de_ruido(dbs: list[float]) -> float:
    """O piso é medido na gravação: o percentil 20 das janelas mais quietas.

    Fixar um limiar absoluto é o erro clássico — funciona na mesa do
    desenvolvedor e falha no celular do paciente dentro do carro.
    """
    if not dbs:
        return PISO_MINIMO_DB
    ordenados = sorted(dbs)
    piso = ordenados[max(0, int(len(ordenados) * 0.20) - 1)]
    return max(piso, PISO_MINIMO_DB)


def detectar_fala(caminho: Path, *, margem_db: float = MARGEM_DB,
                  min_fala_s: float = MIN_FALA_S,
                  silencio_final_s: float = SILENCIO_FINAL_S,
                  janela_s: float = JANELA_S) -> list[Segmento]:
    """Segmentos de fala do áudio, com histerese e tolerância a pausa curta."""
    dbs, passo = _quadros_db(Path(caminho), janela_s)
    if not dbs:
        return []
    limiar = piso_de_ruido(dbs) + margem_db
    tolerancia = max(1, int(silencio_final_s / passo))

    segmentos: list[Segmento] = []
    inicio: int | None = None
    calados = 0
    for i, db in enumerate(dbs):
        if db >= limiar:
            if inicio is None:
                inicio = i
            calados = 0
        elif inicio is not None:
            calados += 1
            if calados >= tolerancia:
                fim = i - calados + 1
                if (fim - inicio) * passo >= min_fala_s:
                    segmentos.append(Segmento(inicio * passo, fim * passo))
                inicio, calados = None, 0
    if inicio is not None and (len(dbs) - inicio) * passo >= min_fala_s:
        segmentos.append(Segmento(inicio * passo, len(dbs) * passo))
    return segmentos


def tem_fala(caminho: Path, **opcoes) -> bool:
    """Basta uma pergunta na maior parte dos casos: alguém falou?"""
    return bool(detectar_fala(caminho, **opcoes))


def recortar_fala(caminho: Path, destino: Path, *, folga_s: float = 0.15,
                  **opcoes) -> Path | None:
    """Escreve só o trecho falado, com uma folga nas pontas.

    Mandar dois segundos de silêncio para o Whisper custa segundos de cota de
    áudio e não acrescenta uma letra à transcrição.
    """
    segmentos = detectar_fala(caminho, **opcoes)
    if not segmentos:
        return None
    with wave.open(str(caminho), "rb") as w:
        taxa, canais, largura = w.getframerate(), w.getnchannels(), w.getsampwidth()
        total = w.getnframes()
        quadros = w.readframes(total)

    inicio = max(0, int((segmentos[0].inicio_s - folga_s) * taxa))
    fim = min(total, int((segmentos[-1].fim_s + folga_s) * taxa))
    bytes_por_quadro = canais * largura
    destino.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(destino), "wb") as w:
        w.setnchannels(canais)
        w.setsampwidth(largura)
        w.setframerate(taxa)
        w.writeframes(quadros[inicio * bytes_por_quadro:fim * bytes_por_quadro])
    return destino
