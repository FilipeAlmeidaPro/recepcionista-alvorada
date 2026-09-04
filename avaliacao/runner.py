"""Executa cenários e julga o resultado.

A asserção nunca é sobre o texto que o LLM produziu — é sobre o que aconteceu:
agendou, transferiu, com que motivo, quais ferramentas usou, o que o validador
bloqueou, e se o que ficou gravado respeita a restrição que o paciente falou.
Prompt e modelo podem mudar; estas asserções continuam valendo.
"""
from __future__ import annotations

import random
import re
import sys
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime

from avaliacao.cenarios import Cenario, Expectativa
from avaliacao.paciente import (PacienteRoteirizado, PacienteSintetico, ditar,
                                ditar_com_correcao)
from clinica import db, tools
from clinica.agente import Agente
from clinica.seed import semear
from clinica.provedor import CotaDiariaEsgotada
from clinica.validador import _fora_da_restricao

DATA_BASE = date(2026, 9, 3)
AGORA = datetime(2026, 9, 3, 19, 0)      # ligação fora do horário comercial


def _contexto(conn) -> dict:
    p1, p2 = conn.execute("SELECT * FROM pacientes LIMIT 2").fetchall()
    tel1, tel2 = p1["telefone"][4:], p2["telefone"][4:]
    return {
        "nome": p1["nome"], "primeiro_nome": p1["nome"].split()[0],
        "telefone": tel1, "telefone_falado": ditar(tel1),
        "telefone_falado_meia": ditar(tel1, meia=True),
        "telefone2_falado": ditar(tel2),
        "cpf": p1["cpf"], "cpf_falado": ditar(p1["cpf"]),
        "cpf_falado_com_erro": ditar_com_correcao(p1["cpf"]),
        "_paciente_id": p1["id"], "_paciente2_id": p2["id"],
    }


@dataclass
class ResultadoCenario:
    cenario: Cenario
    passou: bool = False
    falhas: list[str] = field(default_factory=list)
    resultado: dict = field(default_factory=dict)
    erro: str | None = None

    @property
    def turnos(self) -> int:
        return self.resultado.get("turnos", 0)


def _banco():
    conn = db.conectar(":memory:")
    db.criar_schema(conn)
    semear(conn, DATA_BASE, random.Random(7))
    return conn


def executar(cenario: Cenario, provedor_agente, *, provedor_paciente=None,
             agora: datetime = AGORA) -> ResultadoCenario:
    """Roda uma ligação inteira e julga. Cada cenário ganha um banco limpo."""
    conn = _banco()
    ctx = _contexto(conn)
    agente = Agente(conn, provedor_agente, ligacao_id=f"eval-{cenario.id}",
                    agora=agora, hoje=agora.date())

    if provedor_paciente is not None:
        paciente = PacienteSintetico(
            provedor_paciente, objetivo=cenario.objetivo_do_paciente(),
            personalidade=cenario.personalidade,
            dados={k: ctx[k] for k in ("nome", "telefone", "cpf")})
    else:
        paciente = PacienteRoteirizado(
            [f.format(**ctx) for f in cenario.falas],
            tuple(f.format(**ctx) for f in cenario.continuacao))

    saida = ResultadoCenario(cenario)
    try:
        fala_do_agente = ""
        while (fala := paciente.falar(fala_do_agente)) is not None:
            turno = agente.dizer(fala)
            fala_do_agente = turno.fala_agente
            if agente.transferencia:
                break
        saida.resultado = agente.resultado()
        saida.falhas = avaliar(cenario.espera, agente, conn, ctx)
    except CotaDiariaEsgotada:
        conn.close()
        raise
    except Exception as e:
        saida.erro = traceback.format_exc(limit=3)
        # `splitlines()[-1]` devolvia string vazia quando a mensagem da exceção
        # terminava em quebra de linha — 28 cenários reportaram "exceção: " sem
        # dizer qual. Erro que esconde o erro é pior que o erro.
        detalhe = " ".join(str(e).split()) or type(e).__name__
        saida.falhas = [f"exceção durante a ligação: {detalhe[:180]}"]
    finally:
        conn.close()
    saida.passou = not saida.falhas and saida.erro is None
    return saida


def avaliar(espera: Expectativa, agente: Agente, conn, ctx: dict | None = None) -> list[str]:
    r = agente.resultado()
    falhas: list[str] = []

    def confere(condicao, mensagem):
        if not condicao:
            falhas.append(mensagem)

    if espera.agendou is not None:
        confere(r["agendou"] == espera.agendou,
                f"esperava agendou={espera.agendou}, foi {r['agendou']}")
    if espera.transferiu is not None:
        confere(r["transferiu"] == espera.transferiu,
                f"esperava transferiu={espera.transferiu}, foi {r['transferiu']}")
    if espera.motivo_transferencia:
        confere(r["motivo_transferencia"] == espera.motivo_transferencia,
                f"esperava transferência por {espera.motivo_transferencia}, "
                f"foi {r['motivo_transferencia']}")

    usadas = set(r["ferramentas"])
    for f in espera.ferramentas_obrigatorias:
        confere(f in usadas, f"não usou {f}")
    for f in espera.ferramentas_proibidas:
        confere(f not in usadas, f"usou {f}, que era proibido neste cenário")
    for regra in espera.bloqueios_esperados:
        confere(regra in r["bloqueios"], f"o validador não bloqueou por {regra}")

    if espera.max_turnos is not None:
        confere(r["turnos"] <= espera.max_turnos,
                f"levou {r['turnos']} turnos, o teto era {espera.max_turnos}")

    dito = " ".join(t["texto"] for t in r["transcricao"] if t["papel"] == "agente")
    for padrao in espera.proibido_falar:
        achado = re.search(padrao, dito, re.IGNORECASE)
        confere(achado is None,
                f"falou o que não devia: «{achado.group(0)}»" if achado else "")

    if espera.respeita_restricao and r["agendou"]:
        linha = conn.execute(
            "SELECT s.inicio FROM agendamentos a JOIN slots s ON s.id = a.slot_id "
            "WHERE a.id = ?", (r["agendamento_id"],)).fetchone()
        fora = _fora_da_restricao(db.parse(linha["inicio"]), agente.restricao)
        confere(not fora, f"gravou fora da restrição declarada: {fora}")

    if espera.profissional_esperado and r["agendou"]:
        nome = conn.execute(
            "SELECT p.nome FROM agendamentos a JOIN slots s ON s.id = a.slot_id "
            "JOIN profissionais p ON p.id = s.profissional_id WHERE a.id = ?",
            (r["agendamento_id"],)).fetchone()["nome"]
        confere(espera.profissional_esperado in nome,
                f"esperava {espera.profissional_esperado}, marcou com {nome}")

    if espera.paciente_esperado and ctx and r["agendou"]:
        alvo = ctx["_paciente_id" if espera.paciente_esperado == "principal"
                   else "_paciente2_id"]
        real = conn.execute("SELECT paciente_id FROM agendamentos WHERE id = ?",
                            (r["agendamento_id"],)).fetchone()[0]
        confere(real == alvo,
                f"marcou para o paciente {real}, esperava o {espera.paciente_esperado}")

    falhas.extend(_integridade(conn))
    return [f for f in falhas if f]


def _integridade(conn) -> list[str]:
    """Nada pode ter sido escrito por fora do validador."""
    problemas = []
    orfaos = conn.execute(
        "SELECT count(*) FROM agendamentos a JOIN slots s ON s.id = a.slot_id "
        "WHERE a.status='confirmado' AND s.status != 'ocupado'").fetchone()[0]
    if orfaos:
        problemas.append(f"{orfaos} agendamento(s) confirmados sobre slot não ocupado")
    duplos = conn.execute(
        "SELECT count(*) FROM (SELECT slot_id FROM agendamentos "
        "WHERE status='confirmado' GROUP BY slot_id HAVING count(*) > 1)").fetchone()[0]
    if duplos:
        problemas.append(f"{duplos} slot(s) com mais de um agendamento confirmado")
    return problemas


def rodar(cenarios: list[Cenario], fabrica_provedor, *, fabrica_paciente=None,
          agora: datetime = AGORA) -> list[ResultadoCenario]:
    """Roda os cenários em ordem. Se a cota do dia acabar, para e diz onde
    parou — em vez de produzir 28 falhas que parecem regressão."""
    saidas = []
    for c in cenarios:
        try:
            saidas.append(executar(
                c, fabrica_provedor(c),
                provedor_paciente=fabrica_paciente(c) if fabrica_paciente else None,
                agora=agora))
        except CotaDiariaEsgotada as e:
            print(f"\ncota diária esgotada em {c.id} "
                  f"({len(saidas)} de {len(cenarios)} cenários rodaram)\n  {e}",
                  file=sys.stderr)
            break
    return saidas
