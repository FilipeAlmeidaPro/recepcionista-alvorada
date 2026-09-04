"""Suíte de áudio: o que só existe no canal de voz (§3.1 e §6 do plano).

A suíte de texto testa lógica, regra de negócio e uso de ferramenta. Esta testa
o que a outra não alcança: **a entidade sobrevive à ida e volta pelo áudio?**

O método é o mesmo em todas as condições — sintetiza a fala com a voz nativa
do macOS, degrada o áudio quando o cenário pede, transcreve com o Whisper e
extrai a entidade da transcrição com o mesmo normalizador do agente. O que se
compara é a **entidade**, não o texto: "dezoito horas" e "18h" transcritos
diferente dão o mesmo resultado, e é o resultado que agenda a consulta.

    python3 -m avaliacao.audio                    # todas as condições
    python3 -m avaliacao.audio --condicao limpo   # só uma
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from clinica.normalizador import (casar_especialidade, extrair_digitos,
                                  interpretar_restricao)
from clinica.voz import (VOZ_PADRAO, SinteseMacOS, TranscricaoGroq,
                         adicionar_ruido, cortar)

HOJE = date(2026, 9, 3)
PASTA = Path("audio")
NADA = "·"
ESPECIALIDADES = ["Ortopedia", "Dermatologia", "Cardiologia", "Fisioterapia"]

# Contexto passado ao Whisper: nomes próprios que ele erraria sozinho.
CONTEXTO_STT = ("Clínica Alvorada. Ortopedia, Dermatologia, Cardiologia, "
                "Fisioterapia. Dra. Thaís Bittencourt, Dr. Wesley Vasconcelos, "
                "Dra. Larissa Nakamura, Dr. Otávio Rangel.")


@dataclass(frozen=True)
class CasoAudio:
    id: str
    tipo: str
    fala: str
    esperado: str
    testa: str


CASOS = [
    CasoAudio("V1", "horario", "Só consigo depois das seis, por causa do trabalho.",
              "18:00", "hora relativa falada sobrevive ao STT"),
    CasoAudio("V2", "horario", "Prefiro de manhã, antes das onze.",
              "06:00", "período do dia + limite superior"),
    CasoAudio("V3", "horario", "Pode ser às seis e meia da tarde?",
              "18:30", "minutos ditados por extenso"),
    CasoAudio("V4", "data", "Queria marcar para quinta que vem.",
              "2026-09-10", "data relativa falada"),
    CasoAudio("V5", "data", "Dia quinze do mês que vem serve.",
              "2026-10-15", "data composta"),
    CasoAudio("V6", "digitos", "Meu telefone é onze, nove oito sete seis cinco, "
              "quatro três dois um.", "11987654321", "telefone ditado inteiro"),
    CasoAudio("V7", "digitos", "É meia, meia, sete, oito, dois, dois, um, um.",
              "66782211", "o 'meia' brasileiro valendo seis"),
    CasoAudio("V8", "digitos", "Quatro, três, não, dois.", "42",
              "correção no meio do ditado"),
    CasoAudio("V9", "especialidade", "Eu queria marcar com um ortopedista.",
              "Ortopedia", "profissão falada vira especialidade"),
    CasoAudio("V10", "especialidade", "Preciso de um fisioterapeuta.",
              "Fisioterapia", "idem, outra área"),
]

CONDICOES = {
    "limpo": {},
    # As vozes de novidade do macOS degradam muito a transcrição — "ortopedista"
    # vira "a morta pedista". Não é a voz de um paciente de verdade, mas serve
    # de piso adversarial: se a entidade sobrevive a isto, sobrevive a um
    # telefone ruim.
    "voz_dificil": {"voz": "Rocko"},
    "ruido_leve": {"ruido": 0.045},
    "ruido_forte": {"ruido": 0.10},
    "cortado": {"corte": 0.72},          # paciente interrompido a 72% da fala
}


def _extrair(tipo: str, texto: str) -> str:
    if tipo == "digitos":
        return extrair_digitos(texto) or NADA
    if tipo == "especialidade":
        for palavra in texto.replace(",", " ").replace(".", " ").split():
            achado = casar_especialidade(palavra, ESPECIALIDADES)
            if achado:
                return achado
        return NADA
    r = interpretar_restricao(texto, HOJE)
    if tipo == "horario":
        return r.hora_min or r.hora_max or NADA
    return r.data_inicio or NADA


@dataclass
class Medida:
    caso: CasoAudio
    condicao: str
    transcricao: str
    obtido: str
    acertou: bool
    ms_tts: float
    ms_stt: float
    duracao_s: float


def medir(condicoes: list[str], *, pasta: Path = PASTA) -> list[Medida]:
    stt = TranscricaoGroq()
    vozes = {v: SinteseMacOS(v) for v in
             {CONDICOES[c].get("voz", VOZ_PADRAO) for c in condicoes}}
    saida: list[Medida] = []
    for caso in CASOS:
        for condicao in condicoes:
            opcoes = CONDICOES[condicao]
            tts = vozes[opcoes.get("voz", VOZ_PADRAO)]
            limpo = tts.falar(caso.fala,
                              pasta / f"{caso.id}-{tts.voz}.wav")
            arquivo = limpo.caminho
            if "ruido" in opcoes:
                arquivo = adicionar_ruido(limpo.caminho,
                                          pasta / f"{caso.id}-{condicao}.wav",
                                          volume=opcoes["ruido"])
            elif "corte" in opcoes:
                arquivo = cortar(limpo.caminho, pasta / f"{caso.id}-{condicao}.wav",
                                 ate_s=limpo.duracao_s * opcoes["corte"])
            t = stt.transcrever(arquivo, contexto=CONTEXTO_STT)
            obtido = _extrair(caso.tipo, t.texto)
            saida.append(Medida(caso, condicao, t.texto, obtido,
                                obtido == caso.esperado, limpo.ms_gasto,
                                t.ms_gasto, t.duracao_audio_s))
    return saida


def tabela(medidas: list[Medida], condicoes: list[str]) -> str:
    tipos = ["horario", "data", "digitos", "especialidade"]
    largura = max(len(c) for c in condicoes) + 2
    cab = f"{'condição':<{largura}}" + "".join(f"{t:>15}" for t in tipos) + f"{'TOTAL':>10}"
    linhas = [cab, "─" * len(cab)]
    for condicao in condicoes:
        do_grupo = [m for m in medidas if m.condicao == condicao]
        celulas, acertos = "", 0
        for tipo in tipos:
            do_tipo = [m for m in do_grupo if m.caso.tipo == tipo]
            a = sum(m.acertou for m in do_tipo)
            acertos += a
            celulas += f"{f'{a}/{len(do_tipo)}':>15}" if do_tipo else f"{'—':>15}"
        linhas.append(f"{condicao:<{largura}}{celulas}"
                      f"{f'{acertos / len(do_grupo):.0%}':>10}")
    return "\n".join(linhas)


def latencias(medidas: list[Medida]) -> str:
    stt = sorted(m.ms_stt for m in medidas)
    tts = sorted({m.caso.id: m.ms_tts for m in medidas}.values())
    audio = statistics.mean({m.caso.id: m.duracao_s for m in medidas}.values())
    p = lambda v, q: v[min(int(len(v) * q), len(v) - 1)]  # noqa: E731
    return "\n".join([
        "", f"áudio médio por fala      {audio:.2f} s",
        f"síntese (say, local)      p50 {p(tts, .5):6.0f} ms   p95 {p(tts, .95):6.0f} ms",
        f"transcrição (Whisper)     p50 {p(stt, .5):6.0f} ms   p95 {p(stt, .95):6.0f} ms",
        "",
        "Ambos são por fala inteira, não streaming. O Whisper da Groq não é",
        "streaming: segmenta-se com VAD e manda o trecho. É esse o gargalo do",
        "turno, e é ele que some com um STT pago em streaming.",
    ])


def main() -> int:
    p = argparse.ArgumentParser(description="Suíte de áudio.")
    p.add_argument("--condicao", choices=list(CONDICOES), action="append")
    p.add_argument("--erros", action="store_true")
    args = p.parse_args()
    condicoes = args.condicao or list(CONDICOES)

    try:
        inicio = time.perf_counter()
        medidas = medir(condicoes)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 2

    print(f"{len(CASOS)} falas × {len(condicoes)} condições = {len(medidas)} "
          f"transcrições · {time.perf_counter() - inicio:.0f}s\n")
    print(tabela(medidas, condicoes))
    print(latencias(medidas))

    if args.erros:
        for m in medidas:
            if not m.acertou:
                print(f"\n  [{m.condicao}/{m.caso.id}] {m.caso.testa}")
                print(f"    falou     «{m.caso.fala}»")
                print(f"    transcreveu «{m.transcricao}»")
                print(f"    esperado {m.caso.esperado!r}  obtido {m.obtido!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
