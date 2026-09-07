"""Orquestração multi-agente — com o supervisor em código, não em modelo.

O padrão comum é um LLM supervisor que lê o turno e decide para qual
sub-agente mandar. Aqui isso seria um erro caro: acrescenta **um salto de rede
por turno** num sistema cuja latência já está 3,5× acima do alvo, e mais uma
superfície de alucinação, para tomar uma decisão que um `if` toma melhor e em
zero milissegundos.

Então o supervisor é `Supervisor`, e ele é determinístico. O que é
multi-agente aqui são os **especialistas**, e o desenho que os torna baratos:

* **Guardião de risco** — roda **em paralelo** com o orquestrador, não antes.
  Um trabalho só: "esta frase tem sinal de risco clínico?". Prompt de ~80
  tokens contra os ~3 000 do orquestrador, num modelo mais rápido. Como corre
  junto, **não acrescenta latência ao turno** — só espera se terminar depois,
  o que quase nunca acontece.
* **Escriba** — roda **depois** da ligação, fora do caminho crítico. Escreve o
  resumo para a pessoa que recebe a transferência.

Quando o guardião dispara, ele **preempta**: a resposta do orquestrador é
descartada e a ligação escala. Segurança ganha da conversa, sempre.

O QUE A MEDIÇÃO DISSE — e ela contradiz o que eu tinha escrito aqui
------------------------------------------------------------------

A justificativa original era "o orquestrador está ocupado e vai deixar passar
um sinal enterrado no meio de outra frase". Rodei o benchmark
(`python3 -m avaliacao.risco`) e **a premissa não se sustentou**:

* `gpt-oss-120b` pegou o risco enterrado sozinho, sem guardião;
* `gpt-oss-20b` pegou **4 de 4** sinais de risco, também sozinho.

Em nenhum caso medido o guardião pegou algo que o orquestrador perdeu. Se o
argumento fosse esse, a camada não se pagaria e eu deveria removê-la.

O que a medição mostrou foi outra coisa, menor e verdadeira: **o guardião é
mais preciso que o orquestrador barato**. Nos 7 casos que rodaram, ele acertou
7 — viu os 4 riscos e recusou os 3 falsos. O orquestrador `20b` acertou 6:
escalou uma dor no peito **do ano passado, já investigada**, que o guardião
corretamente classificou como não-risco.

Então o valor demonstrado hoje é: um classificador especializado e barato bate
um generalista ocupado **na precisão**, custando ~600 ms que somem no
paralelismo e ~3% dos tokens do turno. É pouco, e é honesto. O guardião fica
como piso de segurança independente do modelo do orquestrador — a hipótese que
falta testar é se ele segura esse piso quando o orquestrador é ainda mais
barato, ou quando o prompt do orquestrador cresce e a instrução de risco se
dilui.

O que ele **não** faz, de propósito: suprimir escalação. Um guardião que
cancela um alarme do orquestrador seria capaz de silenciar um risco real, e
esse é o único erro que este sistema não pode cometer.
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

from clinica import tools
from clinica.agente import Agente, Turno
from clinica.provedor import Provedor

# Modelo do guardião: o mais rápido disponível. Ele decide uma coisa binária
# com uma frase de contexto; capacidade sobrando aqui é latência desperdiçada.
MODELO_GUARDIAO = "openai/gpt-oss-20b"

SISTEMA_GUARDIAO = """Você lê UMA frase dita por quem ligou para uma clínica e
responde se há sinal de risco que exija atendimento humano imediato.

É risco: dor no peito, aperto no peito, falta de ar, desmaio, sangramento
intenso, dormência súbita de um lado do corpo, fala embolada, confusão mental
súbita, convulsão, pensamento de se machucar.

NÃO é risco: dor comum, dor antiga, dúvida sobre remédio, pedido de
diagnóstico, reclamação, impaciência, sintoma de terceiro que não está na
ligação, ou qualquer coisa dita no passado e já resolvida.

Responda SOMENTE com JSON: {"risco": true|false, "sinal": "o trecho exato"}"""

SISTEMA_ESCRIBA = """Você escreve, em uma frase curta e direta, o que a pessoa
da equipe precisa saber ao receber uma ligação transferida.

Diga: quem é (se identificado), o que a pessoa queria, e por que está sendo
transferida. Sem saudação, sem preâmbulo, sem repetir a transcrição. Se houver
sinal de risco clínico, ele vem primeiro na frase."""


@dataclass(frozen=True)
class VereditoGuardiao:
    risco: bool
    sinal: str = ""
    ms: float = 0.0
    erro: str | None = None

    def para_trace(self) -> dict:
        return {"risco": self.risco, "sinal": self.sinal,
                "ms": round(self.ms), "erro": self.erro}


class GuardiaoDeRisco:
    """Um trabalho só, e por isso barato o bastante para rodar todo turno."""

    nome = "guardião de risco"

    def __init__(self, provedor: Provedor):
        self.provedor = provedor
        self.chamadas = 0

    def avaliar(self, fala: str) -> VereditoGuardiao:
        if not (fala or "").strip():
            return VereditoGuardiao(False)
        inicio = time.perf_counter()
        self.chamadas += 1
        try:
            r = self.provedor.responder(
                [{"role": "system", "content": SISTEMA_GUARDIAO},
                 {"role": "user", "content": fala}], [])
        except Exception as e:      # noqa: BLE001
            # O guardião falhando não pode derrubar a ligação. Mas também não
            # pode virar um "sem risco" silencioso: o erro vai para o trace.
            return VereditoGuardiao(False, ms=(time.perf_counter() - inicio) * 1000,
                                    erro=f"{type(e).__name__}: {str(e)[:80]}")
        ms = (time.perf_counter() - inicio) * 1000
        bruto = re.search(r"\{.*\}", r.texto or "", re.S)
        if not bruto:
            return VereditoGuardiao(False, ms=ms, erro="resposta sem JSON")
        try:
            d = json.loads(bruto.group(0))
        except json.JSONDecodeError:
            return VereditoGuardiao(False, ms=ms, erro="JSON inválido")
        return VereditoGuardiao(bool(d.get("risco")), str(d.get("sinal", ""))[:120], ms)


class Escriba:
    """Fora do caminho crítico: escreve depois que a ligação já acabou."""

    nome = "escriba"

    def __init__(self, provedor: Provedor):
        self.provedor = provedor

    def resumir(self, resultado: dict) -> str:
        falas = "\n".join(f"{t['papel']}: {t['texto']}"
                          for t in resultado.get("transcricao", []))
        contexto = (f"motivo do contato: {resultado.get('motivo_contato')}\n"
                    f"agendou: {resultado.get('agendou')}\n"
                    f"transferência: {resultado.get('motivo_transferencia')}\n\n"
                    f"{falas}")
        try:
            r = self.provedor.responder(
                [{"role": "system", "content": SISTEMA_ESCRIBA},
                 {"role": "user", "content": contexto[:4000]}], [])
            return (r.texto or "").strip()
        except Exception:           # noqa: BLE001
            return ""


@dataclass
class TurnoSupervisionado:
    turno: Turno
    guardiao: VereditoGuardiao | None = None
    preemptado: bool = False
    ms_supervisao: float = 0.0

    def para_trace(self) -> dict:
        return {"guardiao": self.guardiao.para_trace() if self.guardiao else None,
                "preemptado": self.preemptado,
                "ms_supervisao": round(self.ms_supervisao)}


class Supervisor:
    """Coordena os especialistas. É código: não pensa, decide.

    A regra é uma só e cabe numa linha: **se o guardião viu risco e a ligação
    ainda não escalou, escala e descarta o que o orquestrador ia dizer.**
    """

    def __init__(self, agente: Agente, *, guardiao: GuardiaoDeRisco | None = None,
                 escriba: Escriba | None = None, paralelo: bool = True):
        self.agente = agente
        self.guardiao = guardiao
        self.escriba = escriba
        self.paralelo = paralelo
        self.turnos: list[TurnoSupervisionado] = []
        self.resumo_handoff: str = ""

    def dizer(self, fala: str) -> TurnoSupervisionado:
        inicio = time.perf_counter()

        if self.guardiao is None:
            saida = TurnoSupervisionado(self.agente.dizer(fala))
        elif self.paralelo:
            # Só o guardião vai para a thread. O orquestrador fica na principal
            # porque a conexão SQLite é presa à thread que a criou — e afrouxar
            # isso com check_same_thread seria trocar um problema real por um
            # silencioso. O guardião não encosta no banco, então não tem o
            # problema. O paralelismo é o mesmo: os dois correm juntos.
            with ThreadPoolExecutor(max_workers=1) as piscina:
                f_guardiao = piscina.submit(self.guardiao.avaliar, fala)
                turno = self.agente.dizer(fala)
                veredito = f_guardiao.result()
            saida = TurnoSupervisionado(turno, veredito)
        else:
            # Modo sequencial, só para medir o que o paralelismo economiza.
            veredito = self.guardiao.avaliar(fala)
            saida = TurnoSupervisionado(self.agente.dizer(fala), veredito)

        if saida.guardiao and saida.guardiao.risco:
            self._preemptar(saida)

        saida.ms_supervisao = (time.perf_counter() - inicio) * 1000
        self.turnos.append(saida)
        return saida

    def _preemptar(self, saida: TurnoSupervisionado) -> None:
        """Segurança ganha da conversa. O que o orquestrador ia dizer não vale."""
        if self.agente.transferencia is not None:
            return                      # o orquestrador já tinha escalado sozinho
        r = tools.transferir_para_humano(
            self.agente.conn, motivo="risco_clinico",
            resumo=f"Guardião detectou sinal de risco: "
                   f"«{saida.guardiao.sinal or 'não especificado'}»",
            paciente_id=self.agente.paciente_id, agora=self.agente.agora)
        if not r.get("ok"):
            return
        self.agente.transferencia = r
        saida.preemptado = True
        saida.turno.fala_agente = ("Entendi. Vou te passar agora para uma pessoa "
                                   "da equipe, um instante.")
        saida.turno.eventos.append(
            {"ferramenta": "transferir_para_humano", "argumentos":
             {"motivo": "risco_clinico", "origem": "guardiao"}, "resultado": r})
        # O histórico do modelo precisa saber que a ligação mudou de rumo, ou
        # ele continua oferecendo horário no turno seguinte.
        self.agente.mensagens.append(
            {"role": "assistant", "content": saida.turno.fala_agente})

    def encerrar(self) -> dict:
        """Fecha a ligação e chama o escriba — depois, nunca durante."""
        resultado = self.agente.resultado()
        if self.escriba is not None and resultado.get("transferiu"):
            self.resumo_handoff = self.escriba.resumir(resultado)
        resultado["resumo_handoff"] = self.resumo_handoff
        resultado["supervisao"] = [t.para_trace() for t in self.turnos]
        resultado["preempcoes"] = sum(t.preemptado for t in self.turnos)
        resultado["ms_guardiao"] = [round(t.guardiao.ms) for t in self.turnos
                                    if t.guardiao]
        return resultado
