"""Camada de execução determinística. O modelo propõe; o código escreve.

Nenhuma tool de escrita é chamada direto pelo LLM. Ele emite uma **intenção**,
e este módulo confere, em código, regra por regra:

  R1 chave          — tem chave de idempotência?
  R2 paciente       — o cadastro existe?
  R3 slot           — o horário existe na agenda?
  R4 futuro         — ainda não passou?
  R5 disponivel     — continua livre?
  R6 especialidade  — o profissional atende a especialidade pedida?
  R7 restricao      — o horário está dentro do que o paciente declarou?
  R8 confirmacao    — o agente repetiu o slot certo e ouviu um "sim"?
  R9 oferecido      — o slot chegou a ser oferecido ao paciente?

No cadastro de paciente valem outras três, pelo mesmo motivo:

  R11 dados         — nome, telefone, CPF e nascimento estão completos?
  R12 cpf           — os dígitos verificadores fecham?
  R13 duplicado     — esse CPF ou telefone já está na base?

A R8 é a que sustenta a apresentação. O validador não recebe só os argumentos
estruturados: recebe **a frase que o agente falou em voz alta**. Se o agente
consultou terça e confirmou "quinta", as entidades divergem e a escrita é
bloqueada — mesmo com todos os argumentos corretos. É assim que alucinação de
entidade vira uma regra executável em vez de uma métrica de relatório.

Toda reprovação é nomeada. É esse log que aparece na demo quando eu quebro o
agente de propósito.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from clinica import db, tools
from clinica.normalizador import (DIAS_SEMANA, MESES, Restricao, _ler_hora,
                                  _ler_numero, _restricao_horaria, casar_nome,
                                  cpf_valido, extrair_digitos,
                                  normalizar_nascimento, tokenizar)

_AFIRMATIVOS = {"sim", "isso", "confirmo", "confirma", "confirmado", "perfeito",
                "otimo", "beleza", "fechado", "exato", "exatamente", "uhum",
                "aham", "claro", "positivo", "ok", "okay", "ta", "pode",
                "certo", "combinado", "blz", "vamos", "bora", "show"}
_NEGATIVOS = {"nao", "errado", "errada", "espera", "pera", "perai", "calma",
              "muda", "mudar", "outro", "outra", "prefiro", "cancela",
              "engano", "engano", "nada"}
_TITULOS = {"dr", "dra", "doutor", "doutora", "sr", "sra"}
_HORA_CRUA = re.compile(r"(\d{1,2})\s*h\s*(\d{2})?")


# --- estruturas --------------------------------------------------------------

@dataclass(frozen=True)
class ConfirmacaoVerbal:
    """O turno de confirmação, do jeito que aconteceu no áudio."""
    frase_dita: str      # o que o AGENTE falou de volta
    resposta: str        # o que o PACIENTE respondeu


@dataclass(frozen=True)
class Intencao:
    """Proposta do modelo. Nada aqui toca o banco antes de passar pelo validador."""
    slot_id: int
    paciente_id: int
    idempotency_key: str
    especialidade: str | None = None
    restricao: Restricao | None = None
    confirmacao: ConfirmacaoVerbal | None = None
    agendamento_id: int | None = None      # só em reagendamento
    slots_oferecidos: tuple[int, ...] | None = None
    cadastro: dict | None = None           # só em cadastro de paciente


@dataclass(frozen=True)
class Violacao:
    regra: str
    mensagem: str
    detalhe: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Veredito:
    aprovado: bool
    violacoes: tuple[Violacao, ...] = ()
    regras_ok: tuple[str, ...] = ()

    def para_trace(self) -> dict:
        return {
            "aprovado": self.aprovado,
            "regras_ok": list(self.regras_ok),
            "violacoes": [{"regra": v.regra, "mensagem": v.mensagem, **v.detalhe}
                          for v in self.violacoes],
        }

    def motivo(self) -> str | None:
        return self.violacoes[0].regra if self.violacoes else None


# --- leitura da frase falada -------------------------------------------------

def interpretar_resposta(resposta: str) -> str:
    """'sim' / 'nao' / 'indefinido'. Silêncio e hesitação não são consentimento."""
    tokens = set(tokenizar(resposta or ""))
    if tokens & _NEGATIVOS:
        return "nao"
    if tokens & _AFIRMATIVOS:
        return "sim"
    return "indefinido"


def extrair_entidades(frase: str) -> dict:
    """Entidades que o agente disse em voz alta: hora, dia da semana, dia do mês."""
    tokens = tokenizar(frase or "")
    hora_min, hora_max, _amb, _consumidos = _restricao_horaria(tokens)
    hora = hora_min if hora_min and hora_min == hora_max else (hora_min or hora_max)
    if hora is None:
        bruto = _HORA_CRUA.search(frase or "")
        if bruto:
            hora = f"{int(bruto.group(1)):02d}:{int(bruto.group(2) or 0):02d}"

    dia_semana = next((DIAS_SEMANA[t] for t in tokens if t in DIAS_SEMANA), None)
    mes = next((MESES[t] for t in tokens if t in MESES), None)

    dia_mes = None
    for i, tk in enumerate(tokens):
        if tk == "dia":
            valor, _ = _ler_numero(tokens, i + 1)
        elif tk in MESES and i > 0:
            valor, _ = _ler_numero(tokens, i - 1)
            if valor is None and i > 1:
                valor, _ = _ler_numero(tokens, i - 2)
        else:
            continue
        if valor is not None and 1 <= valor <= 31:
            dia_mes = valor
            break

    return {"hora": hora, "dia_semana": dia_semana, "dia_mes": dia_mes, "mes": mes}


def _profissional_dito(frase: str, catalogo: list[str]) -> str | None:
    """Qual profissional do cadastro o agente nomeou — foneticamente, não literal."""
    tokens = [t for t in tokenizar(frase or "") if t not in _TITULOS and len(t) > 3]
    for token in tokens:
        for nome in catalogo:
            partes = [p for p in tokenizar(nome) if p not in _TITULOS and len(p) > 3]
            achado = casar_nome(token, partes, corte=0.85, limite=1)
            if achado["melhor"]:
                return nome
    return None


# --- regras ------------------------------------------------------------------

def _fora_da_restricao(inicio: datetime, r: Restricao) -> list[str]:
    fora = []
    hhmm, dia = inicio.strftime("%H:%M"), inicio.date().isoformat()
    if r.hora_min and hhmm < r.hora_min:
        fora.append(f"começa {hhmm}, antes do limite {r.hora_min}")
    if r.hora_max and hhmm > r.hora_max:
        fora.append(f"começa {hhmm}, depois do limite {r.hora_max}")
    if r.dias_semana and inicio.weekday() not in r.dias_semana:
        fora.append(f"cai em dia da semana fora de {list(r.dias_semana)}")
    if r.data_inicio and dia < r.data_inicio:
        fora.append(f"{dia} é antes de {r.data_inicio}")
    if r.data_fim and dia > r.data_fim:
        fora.append(f"{dia} é depois de {r.data_fim}")
    return fora


def validar(conn, intencao: Intencao, *, agora: datetime | None = None) -> Veredito:
    """Confere a intenção contra o banco e contra o que foi dito. Não escreve nada."""
    agora = agora or datetime.now()
    violacoes: list[Violacao] = []
    ok: list[str] = []

    def falhou(regra, mensagem, **detalhe):
        violacoes.append(Violacao(regra, mensagem, detalhe))

    # R1
    if not intencao.idempotency_key:
        falhou("R1_chave", "Escrita sem chave de idempotência.")
    else:
        ok.append("R1_chave")

    # R2
    paciente = conn.execute("SELECT * FROM pacientes WHERE id = ?",
                            (intencao.paciente_id,)).fetchone()
    if paciente is None:
        falhou("R2_paciente", "Cadastro não encontrado.", paciente_id=intencao.paciente_id)
    else:
        ok.append("R2_paciente")

    # R3
    slot = conn.execute("""
        SELECT s.*, p.nome AS prof_nome, e.nome AS especialidade
        FROM slots s
        JOIN profissionais p ON p.id = s.profissional_id
        JOIN especialidades e ON e.id = p.especialidade_id
        WHERE s.id = ?""", (intencao.slot_id,)).fetchone()
    if slot is None:
        falhou("R3_slot", "Horário não existe na agenda.", slot_id=intencao.slot_id)
        return Veredito(False, tuple(violacoes), tuple(ok))
    ok.append("R3_slot")
    inicio = db.parse(slot["inicio"])

    # R9 — o modelo só pode propor um horário que ele de fato ofereceu.
    # Sem isso, um slot_id inventado que por acaso exista no banco passaria
    # por todas as outras regras.
    if intencao.slots_oferecidos is not None:
        if intencao.slot_id not in intencao.slots_oferecidos:
            falhou("R9_oferecido",
                   "O agente propôs um horário que nunca foi oferecido ao paciente.",
                   slot_id=intencao.slot_id,
                   oferecidos=list(intencao.slots_oferecidos))
        else:
            ok.append("R9_oferecido")

    # R4
    if inicio <= agora:
        falhou("R4_futuro", "Horário já passou.", inicio=slot["inicio"])
    else:
        ok.append("R4_futuro")

    # R5 — em reagendamento o slot de origem já está ocupado por este agendamento
    if slot["status"] != "livre":
        falhou("R5_disponivel", "Horário não está mais livre.", status=slot["status"])
    else:
        ok.append("R5_disponivel")

    # R6
    if intencao.especialidade:
        pedida = db.sem_acento(intencao.especialidade)
        if pedida != db.sem_acento(slot["especialidade"]):
            falhou("R6_especialidade",
                   f"{slot['prof_nome']} atende {slot['especialidade']}, "
                   f"não {intencao.especialidade}.",
                   pedida=intencao.especialidade, do_slot=slot["especialidade"])
        else:
            ok.append("R6_especialidade")

    # R7
    if intencao.restricao and not intencao.restricao.vazia():
        fora = _fora_da_restricao(inicio, intencao.restricao)
        if fora:
            falhou("R7_restricao",
                   "Horário fora da restrição que o paciente declarou.",
                   restricao=intencao.restricao.interpretacao, problemas=fora)
        else:
            ok.append("R7_restricao")

    # R8 — a regra que pega alucinação de entidade
    violacoes.extend(_validar_confirmacao(conn, intencao, slot, inicio, ok))

    return Veredito(not violacoes, tuple(violacoes), tuple(ok))


def _validar_confirmacao(conn, intencao, slot, inicio, ok) -> list[Violacao]:
    conf = intencao.confirmacao
    if conf is None:
        return [Violacao("R8_confirmacao",
                         "Escrita sem confirmação verbal do paciente.", {})]

    resposta = interpretar_resposta(conf.resposta)
    if resposta != "sim":
        return [Violacao("R8_confirmacao",
                         "O paciente não confirmou.",
                         {"resposta": conf.resposta, "leitura": resposta})]

    dito = extrair_entidades(conf.frase_dita)
    divergencias = []
    if dito["hora"] is None:
        divergencias.append("o agente não disse o horário em voz alta")
    elif dito["hora"] != inicio.strftime("%H:%M"):
        divergencias.append(f"disse {dito['hora']}, o slot é {inicio:%H:%M}")
    if dito["dia_semana"] is None and dito["dia_mes"] is None:
        divergencias.append("o agente não disse o dia em voz alta")
    if dito["dia_semana"] is not None and dito["dia_semana"] != inicio.weekday():
        divergencias.append(f"disse dia da semana {dito['dia_semana']}, "
                            f"o slot é {inicio.weekday()}")
    if dito["dia_mes"] is not None and dito["dia_mes"] != inicio.day:
        divergencias.append(f"disse dia {dito['dia_mes']}, o slot é dia {inicio.day}")
    if dito["mes"] is not None and dito["mes"] != inicio.month:
        divergencias.append(f"disse mês {dito['mes']}, o slot é {inicio.month}")

    catalogo = [r["nome"] for r in conn.execute("SELECT nome FROM profissionais")]
    nomeado = _profissional_dito(conf.frase_dita, catalogo)
    if nomeado and nomeado != slot["prof_nome"]:
        divergencias.append(f"disse {nomeado}, o slot é com {slot['prof_nome']}")

    if divergencias:
        return [Violacao("R8_confirmacao",
                         "O que o agente confirmou não bate com o horário reservado.",
                         {"frase_dita": conf.frase_dita, "divergencias": divergencias})]
    ok.append("R8_confirmacao")
    return []


# --- execução ----------------------------------------------------------------

def _reprovado(veredito: Veredito) -> dict:
    return {"ok": False, "erro": "reprovado_pelo_validador",
            "regra": veredito.motivo(),
            "mensagem": veredito.violacoes[0].mensagem,
            "veredito": veredito.para_trace()}


def executar_reserva(conn, intencao: Intencao, *, agora: datetime | None = None,
                     motivo: str | None = None) -> dict:
    """Único caminho de escrita de agendamento. Valida, depois grava."""
    veredito = validar(conn, intencao, agora=agora)
    if not veredito.aprovado:
        return _reprovado(veredito)
    resultado = tools.reservar_horario(
        conn, slot_id=intencao.slot_id, paciente_id=intencao.paciente_id,
        idempotency_key=intencao.idempotency_key, motivo=motivo, agora=agora)
    resultado["veredito"] = veredito.para_trace()
    return resultado


def executar_cadastro(conn, intencao: Intencao, *,
                      agora: datetime | None = None) -> dict:
    """Cria a ficha, se os dados fecharem e o paciente tiver confirmado.

    Um CPF errado aqui não gera um agendamento errado: gera **uma pessoa que
    não existe**, que mais tarde colide com o cadastro verdadeiro de alguém.
    Por isso o dígito verificador é conferido em código — o modelo não tem como
    saber se um CPF é válido, e não deveria tentar adivinhar.
    """
    dados = intencao.cadastro or {}
    violacoes: list[Violacao] = []
    ok: list[str] = []

    if not intencao.idempotency_key:
        violacoes.append(Violacao("R1_chave", "Cadastro sem chave de idempotência.", {}))
    else:
        ok.append("R1_chave")

    nome = (dados.get("nome") or "").strip()
    cpf = db.so_digitos(dados.get("cpf") or "")
    telefone = db.so_digitos(dados.get("telefone") or "")
    nascimento = normalizar_nascimento(dados.get("nascimento") or "",
                                       (agora or datetime.now()).date())

    faltando = [rotulo for rotulo, valor in
                (("nome", len(nome.split()) >= 2), ("CPF", len(cpf) == 11),
                 ("telefone", len(telefone) >= 10), ("nascimento", bool(nascimento)))
                if not valor]
    if faltando:
        violacoes.append(Violacao(
            "R11_dados", f"Faltam dados para o cadastro: {', '.join(faltando)}.",
            {"faltando": faltando}))
    else:
        ok.append("R11_dados")

    if len(cpf) == 11 and not cpf_valido(cpf):
        violacoes.append(Violacao(
            "R12_cpf", "Esse CPF não é válido — os dígitos verificadores não "
                       "fecham. Peça para repetir.", {"digitos": len(cpf)}))
    elif len(cpf) == 11:
        ok.append("R12_cpf")

    ja = conn.execute("SELECT * FROM pacientes WHERE cpf = ?", (cpf,)).fetchone() if cpf else None
    if ja and casar_nome(nome, [ja["nome"]], corte=0.80)["melhor"]:
        # Mesmo CPF e mesmo nome é repetição, não conflito: a rede caiu, ou o
        # paciente confirmou duas vezes. Devolver erro aqui faria o agente
        # dizer "esse CPF já está cadastrado" para a própria pessoa que acabou
        # de ditá-lo.
        return {"ok": True, "idempotente": True,
                "paciente": tools._paciente_publico(ja),
                "mensagem": "Esse cadastro já existe.",
                "veredito": Veredito(True, (), tuple(ok + ["R13_duplicado"])).para_trace()}
    if ja:
        violacoes.append(Violacao(
            "R13_duplicado", "Esse CPF já está cadastrado em outro nome.",
            {"nome_no_cadastro": ja["nome"]}))
    elif telefone and conn.execute(
            "SELECT 1 FROM pacientes WHERE telefone LIKE ?",
            (f"%{telefone[-8:]}",)).fetchone():
        violacoes.append(Violacao(
            "R13_duplicado", "Já existe cadastro com esse telefone.", {}))
    else:
        ok.append("R13_duplicado")

    violacoes.extend(_validar_confirmacao_cadastro(intencao, nome, cpf, ok))

    if violacoes:
        return _reprovado(Veredito(False, tuple(violacoes), tuple(ok)))

    r = tools.cadastrar_paciente(
        conn, nome=nome, telefone=telefone, cpf=cpf, nascimento=nascimento,
        idempotency_key=intencao.idempotency_key, agora=agora)
    r["veredito"] = Veredito(True, (), tuple(ok)).para_trace()
    return r


def _validar_confirmacao_cadastro(intencao, nome, cpf, ok) -> list[Violacao]:
    """O agente tem de ter lido o nome e o CPF de volta, e ouvido um sim.

    É a R8 outra vez, com as entidades do cadastro: se ele leu um CPF diferente
    do que vai gravar, a ficha nasce errada e ninguém percebe até a colisão.
    """
    conf = intencao.confirmacao
    if conf is None:
        return [Violacao("R8_confirmacao",
                         "Cadastro sem confirmação verbal do paciente.", {})]
    if interpretar_resposta(conf.resposta) != "sim":
        return [Violacao("R8_confirmacao", "O paciente não confirmou os dados.",
                         {"resposta": conf.resposta})]

    dito = conf.frase_dita or ""
    divergencias = []
    sobrenome = nome.split()[-1] if nome.split() else ""
    if sobrenome and not casar_nome(sobrenome, tokenizar(dito), corte=0.85)["melhor"]:
        divergencias.append(f"o agente não leu o nome «{nome}» de volta")
    # Contido, não igual: a frase boa lê o CPF **e** a data de nascimento de
    # volta ("CPF 111 444 777 35, nascido em quinze do três de oitenta"), e
    # comparar o total de dígitos reprovava justamente o agente que confirma
    # tudo. Exigir os 11 dígitos em ordem continua pegando o CPF trocado.
    if cpf and cpf not in extrair_digitos(dito):
        divergencias.append("o CPF lido em voz alta não é o que ia ser gravado")
    if divergencias:
        return [Violacao("R8_confirmacao",
                         "O que o agente confirmou não bate com o cadastro.",
                         {"frase_dita": dito[:160], "divergencias": divergencias})]
    ok.append("R8_confirmacao")
    return []


def executar_cancelamento(conn, intencao: Intencao, *,
                          agora: datetime | None = None) -> dict:
    """Cancelar exige a mesma confirmação verbal que marcar.

    Só duas regras se aplicam aqui — o slot de destino não existe. O que
    importa é que o paciente ouviu qual consulta está sendo cancelada e disse
    sim: cancelar a consulta errada é o segundo pesadelo de quem opera clínica.
    """
    if not intencao.idempotency_key:
        return _reprovado(Veredito(False, (Violacao(
            "R1_chave", "Cancelamento sem chave de idempotência.", {}),)))

    # A idempotência vem antes da checagem de status: depois do primeiro
    # cancelamento o agendamento já não está "confirmado", e o segundo pedido
    # com a mesma chave é repetição — não erro.
    ja = conn.execute("SELECT 1 FROM agendamentos WHERE idempotency_key = ?",
                      (intencao.idempotency_key,)).fetchone()
    if ja:
        return tools.cancelar(conn, agendamento_id=intencao.agendamento_id,
                              idempotency_key=intencao.idempotency_key, agora=agora)

    atual = conn.execute("""
        SELECT a.id, a.status, s.inicio, p.nome AS prof_nome, e.nome AS especialidade
        FROM agendamentos a
        JOIN slots s ON s.id = a.slot_id
        JOIN profissionais p ON p.id = s.profissional_id
        JOIN especialidades e ON e.id = p.especialidade_id
        WHERE a.id = ?""", (intencao.agendamento_id,)).fetchone()
    if atual is None or atual["status"] != "confirmado":
        return _reprovado(Veredito(False, (Violacao(
            "R10_origem", "Agendamento não encontrado ou já inativo.",
            {"agendamento_id": intencao.agendamento_id}),)))

    ok: list[str] = ["R1_chave", "R10_origem"]
    faltas = _validar_confirmacao(
        conn, intencao, {"prof_nome": atual["prof_nome"],
                         "especialidade": atual["especialidade"]},
        db.parse(atual["inicio"]), ok)
    if faltas:
        return _reprovado(Veredito(False, tuple(faltas), tuple(ok)))

    veredito = Veredito(True, (), tuple(ok))
    r = tools.cancelar(conn, agendamento_id=intencao.agendamento_id,
                       idempotency_key=intencao.idempotency_key, agora=agora)
    r["veredito"] = veredito.para_trace()
    return r


def executar_reagendamento(conn, intencao: Intencao, *,
                           agora: datetime | None = None) -> dict:
    """Mesmas regras, mais a checagem do agendamento de origem."""
    atual = conn.execute("""
        SELECT a.*, p.especialidade_id, e.nome AS especialidade
        FROM agendamentos a
        JOIN slots s ON s.id = a.slot_id
        JOIN profissionais p ON p.id = s.profissional_id
        JOIN especialidades e ON e.id = p.especialidade_id
        WHERE a.id = ?""", (intencao.agendamento_id,)).fetchone()
    if atual is None:
        return _reprovado(Veredito(False, (Violacao(
            "R10_origem", "Agendamento de origem não encontrado.",
            {"agendamento_id": intencao.agendamento_id}),)))
    if atual["status"] != "confirmado":
        return _reprovado(Veredito(False, (Violacao(
            "R10_origem", "Agendamento de origem não está ativo.",
            {"status": atual["status"]}),)))

    # A especialidade de destino é a da origem — não é o modelo que decide isso.
    proposta = Intencao(
        slot_id=intencao.slot_id, paciente_id=atual["paciente_id"],
        idempotency_key=intencao.idempotency_key,
        especialidade=atual["especialidade"], restricao=intencao.restricao,
        confirmacao=intencao.confirmacao, agendamento_id=intencao.agendamento_id,
        slots_oferecidos=intencao.slots_oferecidos)

    veredito = validar(conn, proposta, agora=agora)
    if not veredito.aprovado:
        return _reprovado(veredito)
    resultado = tools.reagendar(
        conn, agendamento_id=intencao.agendamento_id, novo_slot_id=intencao.slot_id,
        idempotency_key=intencao.idempotency_key, agora=agora)
    resultado["veredito"] = veredito.para_trace()
    return resultado
