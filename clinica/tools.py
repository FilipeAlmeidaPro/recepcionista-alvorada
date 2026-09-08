"""As 5 ferramentas do agente. Puras, testáveis, sem voz.

Contrato único: toda função devolve um dict JSON-serializável com `ok` e,
quando falha, um `erro` de código estável mais uma `mensagem` em PT-BR.
Os códigos de erro são estáveis de propósito — é sobre eles que a suíte de
eval faz asserção, não sobre o texto.

Duas decisões que valem ser ditas em voz alta:

1. LGPD — CPF e telefone saem mascarados. O LLM conduz a conversa inteira
   sem nunca receber o dado completo.
2. `limite` padrão 3 — ninguém lê dez opções em voz alta. Restrição do
   canal virando padrão da API.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from clinica import db
from clinica.normalizador import casar_especialidade, ler_digitos

MOTIVOS_TRANSFERENCIA = ("risco_clinico", "fora_de_escopo", "frustracao",
                         "pedido_do_paciente", "falha_tecnica")
LIMITE_PADRAO = 3
LIMITE_MAXIMO = 10


def _falha(erro: str, mensagem: str, **extra) -> dict:
    return {"ok": False, "erro": erro, "mensagem": mensagem, **extra}


def _agora(agora: datetime | None) -> datetime:
    return agora or datetime.now()


def _paciente_publico(linha: sqlite3.Row) -> dict:
    return {
        "id": linha["id"],
        "nome": linha["nome"],
        "cpf_mascarado": db.mascarar_cpf(linha["cpf"]),
        "telefone_mascarado": db.mascarar_telefone(linha["telefone"]),
        "nascimento": linha["nascimento"],
    }


def _slot_publico(linha: sqlite3.Row) -> dict:
    inicio = db.parse(linha["inicio"])
    return {
        "slot_id": linha["id"],
        "profissional_id": linha["prof_id"],
        "profissional": linha["prof_nome"],
        "especialidade": linha["especialidade"],
        "inicio": linha["inicio"],
        "fim": linha["fim"],
        "descricao": db.descrever(inicio),
    }


def _especialidades(conn) -> dict[str, sqlite3.Row]:
    return {db.sem_acento(r["nome"]): r for r in conn.execute("SELECT id, nome FROM especialidades")}


# --- 1. buscar_paciente ------------------------------------------------------

def buscar_paciente(conn, *, telefone: str | None = None, cpf: str | None = None) -> dict:
    """Identifica o paciente por telefone ou CPF. Aceita entrada suja do STT."""
    tel, doc = db.so_digitos(telefone or ""), db.so_digitos(cpf or "")
    if not tel and not doc:
        return _falha("parametro_ausente", "Preciso do telefone ou do CPF para localizar o cadastro.")

    linha = None
    if doc:
        if len(doc) != 11:
            return _falha("cpf_invalido",
                          "Esse CPF não tem 11 dígitos. Pode repetir devagar?",
                          digitos_recebidos=len(doc))
        linha = conn.execute("SELECT * FROM pacientes WHERE cpf = ?", (doc,)).fetchone()
    if linha is None and tel:
        # O STT costuma comer o DDI. Casa pelo sufixo, que é o que sobrevive.
        linha = conn.execute(
            "SELECT * FROM pacientes WHERE telefone = ? OR telefone LIKE ?",
            (tel, f"%{tel[-8:]}")).fetchone() if len(tel) >= 8 else None

    if linha is None:
        return {"ok": True, "encontrado": False, "paciente": None,
                "leitura_para_confirmar": ler_digitos(doc or tel,
                                                      "cpf" if doc else "telefone"),
                "mensagem": "Não encontrei cadastro. Leia os dígitos de volta para "
                            "conferir antes de desistir."}
    return {"ok": True, "encontrado": True, "paciente": _paciente_publico(linha),
            "leitura_para_confirmar": ler_digitos(doc or tel,
                                                  "cpf" if doc else "telefone"),
            "mensagem": "Leia os dígitos de volta e confirme com o paciente "
                        "antes de seguir."}


# --- 2. consultar_agenda -----------------------------------------------------

def consultar_agenda(conn, *, especialidade: str, hora_min: str | None = None,
                     hora_max: str | None = None, data_inicio: str | None = None,
                     data_fim: str | None = None, dias_semana: list[int] | None = None,
                     profissional_id: int | None = None, limite: int = LIMITE_PADRAO,
                     agora: datetime | None = None) -> dict:
    """Horários livres reais, filtrados pela restrição declarada pelo paciente.

    `hora_min`/`hora_max` são o horário de INÍCIO aceito, inclusive nas duas
    pontas ("depois das 18h" -> hora_min='18:00').

    Devolver zero resultados é uma resposta legítima e vem com `alternativa`:
    o horário livre mais próximo ignorando as restrições de hora e de dia da
    semana. É o que impede o modelo de inventar um horário para agradar.
    """
    momento = _agora(agora)
    catalogo = _especialidades(conn)
    nomes = [r["nome"] for r in catalogo.values()]
    casada = casar_especialidade(especialidade, nomes)
    if casada is None:
        return _falha("especialidade_inexistente",
                      f"A clínica não atende {especialidade}.",
                      especialidades_disponiveis=nomes)
    chave = db.sem_acento(casada)

    limite = max(1, min(int(limite), LIMITE_MAXIMO))
    base = """
        SELECT s.id, s.inicio, s.fim,
               p.id AS prof_id, p.nome AS prof_nome, e.nome AS especialidade
        FROM slots s
        JOIN profissionais p ON p.id = s.profissional_id
        JOIN especialidades e ON e.id = p.especialidade_id
        WHERE s.status = 'livre' AND p.ativo = 1
          AND e.id = ? AND s.inicio > ?
    """
    args: list = [catalogo[chave]["id"], momento.strftime(db.FORMATO)]
    filtros = ""
    if profissional_id is not None:
        filtros += " AND p.id = ?"; args.append(int(profissional_id))
    if data_inicio:
        filtros += " AND date(s.inicio) >= ?"; args.append(data_inicio)
    if data_fim:
        filtros += " AND date(s.inicio) <= ?"; args.append(data_fim)

    hora_args = list(args)
    hora_filtros = filtros
    if hora_min:
        hora_filtros += " AND time(s.inicio) >= ?"; hora_args.append(hora_min)
    if hora_max:
        hora_filtros += " AND time(s.inicio) <= ?"; hora_args.append(hora_max)

    linhas = conn.execute(base + hora_filtros + " ORDER BY s.inicio", hora_args).fetchall()
    if dias_semana:
        alvo = {int(d) for d in dias_semana}
        linhas = [r for r in linhas if db.parse(r["inicio"]).weekday() in alvo]

    if linhas:
        return {"ok": True, "total": len(linhas),
                "slots": [_slot_publico(r) for r in linhas[:limite]]}

    alternativa = conn.execute(base + filtros + " ORDER BY s.inicio LIMIT 1", args).fetchone()
    return {
        "ok": True, "total": 0, "slots": [],
        "motivo": "nenhum_horario_na_restricao",
        "mensagem": "Não tenho nada livre dentro dessa restrição.",
        "alternativa": _slot_publico(alternativa) if alternativa else None,
    }


# --- 3. reservar_horario -----------------------------------------------------

def reservar_horario(conn, *, slot_id: int, paciente_id: int, idempotency_key: str,
                     motivo: str | None = None, origem: str = "voz",
                     agora: datetime | None = None) -> dict:
    """Escrita. Idempotente por chave, atômica por transação.

    Double-booking não é evitado aqui por cuidado — é impossível pelo índice
    único parcial em `agendamentos`. Este código só traduz a violação em uma
    mensagem que o agente sabe falar.
    """
    if not idempotency_key:
        return _falha("chave_ausente", "Reserva sem chave de idempotência foi recusada.")

    ja = conn.execute(
        "SELECT * FROM agendamentos WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
    if ja:
        return {"ok": True, "idempotente": True, **_agendamento_publico(conn, ja["id"])}

    momento = _agora(agora)
    if conn.execute("SELECT 1 FROM pacientes WHERE id = ?", (paciente_id,)).fetchone() is None:
        return _falha("paciente_inexistente", "Não encontrei esse cadastro.")

    slot = conn.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone()
    if slot is None:
        return _falha("slot_inexistente", "Esse horário não existe na agenda.")
    if db.parse(slot["inicio"]) <= momento:
        return _falha("slot_no_passado", "Esse horário já passou.")

    try:
        conn.execute("BEGIN IMMEDIATE")
        alterados = conn.execute(
            "UPDATE slots SET status = 'ocupado' WHERE id = ? AND status = 'livre'",
            (slot_id,)).rowcount
        if alterados != 1:
            conn.execute("ROLLBACK")
            return _falha("slot_indisponivel", "Esse horário acabou de ser ocupado.",
                          status_atual=slot["status"])
        cur = conn.execute(
            "INSERT INTO agendamentos (slot_id, paciente_id, status, motivo, criado_em, "
            "idempotency_key, origem) VALUES (?,?,'confirmado',?,?,?,?)",
            (slot_id, paciente_id, motivo, momento.strftime(db.FORMATO),
             idempotency_key, origem))
        novo_id = cur.lastrowid
        conn.execute("COMMIT")
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK")
        return _falha("slot_indisponivel", "Esse horário acabou de ser ocupado.")

    return {"ok": True, "idempotente": False, **_agendamento_publico(conn, novo_id)}


def _agendamento_publico(conn, agendamento_id: int) -> dict:
    r = conn.execute("""
        SELECT a.id, a.status, a.criado_em, a.idempotency_key,
               s.id AS slot_id, s.inicio, s.fim,
               p.id AS prof_id, p.nome AS prof_nome, e.nome AS especialidade,
               pa.id AS paciente_id, pa.nome AS paciente_nome
        FROM agendamentos a
        JOIN slots s ON s.id = a.slot_id
        JOIN profissionais p ON p.id = s.profissional_id
        JOIN especialidades e ON e.id = p.especialidade_id
        JOIN pacientes pa ON pa.id = a.paciente_id
        WHERE a.id = ?""", (agendamento_id,)).fetchone()
    inicio = db.parse(r["inicio"])
    return {
        "agendamento": {
            "id": r["id"], "status": r["status"], "slot_id": r["slot_id"],
            "paciente_id": r["paciente_id"], "paciente": r["paciente_nome"],
            "profissional": r["prof_nome"], "profissional_id": r["prof_id"],
            "especialidade": r["especialidade"],
            "inicio": r["inicio"], "fim": r["fim"],
        },
        # Frase única que o agente repete de volta. É contra ela que a métrica
        # de alucinação de entidade compara o que saiu no áudio.
        "confirmacao": (f"{r['prof_nome']}, {r['especialidade']}, "
                        f"{db.descrever(inicio)}"),
    }


# --- 4. reagendar ------------------------------------------------------------

def reagendar(conn, *, agendamento_id: int, novo_slot_id: int, idempotency_key: str,
              agora: datetime | None = None) -> dict:
    """Move um agendamento. Libera o slot antigo e ocupa o novo, ou não faz nada."""
    if not idempotency_key:
        return _falha("chave_ausente", "Reagendamento sem chave de idempotência foi recusado.")

    ja = conn.execute(
        "SELECT * FROM agendamentos WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
    if ja:
        return {"ok": True, "idempotente": True, **_agendamento_publico(conn, ja["id"])}

    momento = _agora(agora)
    atual = conn.execute("""
        SELECT a.*, p.especialidade_id
        FROM agendamentos a
        JOIN slots s ON s.id = a.slot_id
        JOIN profissionais p ON p.id = s.profissional_id
        WHERE a.id = ?""", (agendamento_id,)).fetchone()
    if atual is None:
        return _falha("agendamento_inexistente", "Não encontrei esse agendamento.")
    if atual["status"] != "confirmado":
        return _falha("agendamento_nao_confirmado",
                      "Esse agendamento não está ativo.", status_atual=atual["status"])
    if atual["slot_id"] == novo_slot_id:
        return _falha("mesmo_slot", "Esse já é o horário do agendamento.")

    novo = conn.execute("""
        SELECT s.*, p.especialidade_id FROM slots s
        JOIN profissionais p ON p.id = s.profissional_id
        WHERE s.id = ?""", (novo_slot_id,)).fetchone()
    if novo is None:
        return _falha("slot_inexistente", "Esse horário não existe na agenda.")
    if db.parse(novo["inicio"]) <= momento:
        return _falha("slot_no_passado", "Esse horário já passou.")
    if novo["especialidade_id"] != atual["especialidade_id"]:
        return _falha("especialidade_divergente",
                      "Não dá para mover a consulta para outra especialidade. "
                      "Nesse caso é um agendamento novo.")

    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("UPDATE slots SET status='ocupado' WHERE id=? AND status='livre'",
                        (novo_slot_id,)).rowcount != 1:
            conn.execute("ROLLBACK")
            return _falha("slot_indisponivel", "Esse horário acabou de ser ocupado.",
                          status_atual=novo["status"])
        conn.execute("UPDATE agendamentos SET status='cancelado', motivo='reagendado' WHERE id=?",
                     (agendamento_id,))
        conn.execute("UPDATE slots SET status='livre' WHERE id=?", (atual["slot_id"],))
        cur = conn.execute(
            "INSERT INTO agendamentos (slot_id, paciente_id, status, motivo, criado_em, "
            "idempotency_key, origem) VALUES (?,?,'confirmado',?,?,?,'voz')",
            (novo_slot_id, atual["paciente_id"], f"reagendado_de:{agendamento_id}",
             momento.strftime(db.FORMATO), idempotency_key))
        novo_id = cur.lastrowid
        conn.execute("COMMIT")
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK")
        return _falha("slot_indisponivel", "Esse horário acabou de ser ocupado.")

    return {"ok": True, "idempotente": False, "agendamento_anterior": agendamento_id,
            **_agendamento_publico(conn, novo_id)}


# --- 5. cadastrar_paciente ---------------------------------------------------

def cadastrar_paciente(conn, *, nome: str, telefone: str, cpf: str,
                       nascimento: str, idempotency_key: str,
                       agora: datetime | None = None) -> dict:
    """Cria a ficha. É a escrita que cria uma pessoa que não existia.

    Errar aqui é pior que errar um horário: um CPF trocado não gera um
    agendamento errado, gera **um paciente fantasma** que vai colidir com o
    cadastro verdadeiro de alguém mais tarde. Por isso a unicidade do CPF é do
    banco, não desta função — mesma escolha do double-booking.
    """
    if not idempotency_key:
        return _falha("chave_ausente", "Cadastro sem chave de idempotência foi recusado.")

    doc, tel = db.so_digitos(cpf), db.so_digitos(telefone)
    ja = conn.execute("SELECT * FROM pacientes WHERE cpf = ?", (doc,)).fetchone()
    if ja:
        return {"ok": True, "idempotente": True, "paciente": _paciente_publico(ja),
                "mensagem": "Esse cadastro já existe."}

    momento = _agora(agora)
    try:
        cur = conn.execute(
            "INSERT INTO pacientes (nome, cpf, telefone, nascimento, criado_em, "
            "origem) VALUES (?,?,?,?,?,'voz')",
            (nome.strip(), doc, tel, nascimento, momento.strftime(db.FORMATO)))
    except sqlite3.IntegrityError:
        return _falha("cpf_duplicado", "Esse CPF já está cadastrado.")

    linha = conn.execute("SELECT * FROM pacientes WHERE id = ?",
                         (cur.lastrowid,)).fetchone()
    return {"ok": True, "idempotente": False, "paciente": _paciente_publico(linha),
            "mensagem": f"Cadastro criado para {linha['nome']}."}


# --- 6. cancelar -------------------------------------------------------------

def cancelar(conn, *, agendamento_id: int, idempotency_key: str,
             motivo: str | None = None, agora: datetime | None = None) -> dict:
    """Cancela e devolve o horário para a agenda.

    Cancelar é a única operação que não tem desfazer barato: quem cancela por
    engano perde a vaga para o próximo da fila. Por isso passa pelo mesmo
    portão da reserva — validação e chave de idempotência.
    """
    if not idempotency_key:
        return _falha("chave_ausente", "Cancelamento sem chave de idempotência foi recusado.")

    ja = conn.execute(
        "SELECT * FROM agendamentos WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
    if ja:
        return {"ok": True, "idempotente": True, "agendamento_id": ja["slot_id"],
                "mensagem": "Esse cancelamento já tinha sido feito."}

    atual = conn.execute("SELECT * FROM agendamentos WHERE id = ?",
                         (agendamento_id,)).fetchone()
    if atual is None:
        return _falha("agendamento_inexistente", "Não encontrei esse agendamento.")
    if atual["status"] != "confirmado":
        return _falha("agendamento_nao_confirmado", "Esse agendamento não está ativo.",
                      status_atual=atual["status"])

    momento = _agora(agora)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE agendamentos SET status='cancelado', motivo=? WHERE id=?",
                 (motivo or "cancelado_pelo_paciente", agendamento_id))
    conn.execute("UPDATE slots SET status='livre' WHERE id=?", (atual["slot_id"],))
    # Registro do cancelamento, com a chave, para o pedido repetido ser idempotente.
    conn.execute(
        "INSERT INTO agendamentos (slot_id, paciente_id, status, motivo, criado_em, "
        "idempotency_key, origem) VALUES (?,?,'cancelado',?,?,?,'voz')",
        (atual["slot_id"], atual["paciente_id"], f"registro_cancelamento:{agendamento_id}",
         momento.strftime(db.FORMATO), idempotency_key))
    conn.execute("COMMIT")
    return {"ok": True, "idempotente": False, "agendamento_id": agendamento_id,
            "slot_liberado": atual["slot_id"],
            "mensagem": "Consulta cancelada e horário liberado."}


# --- 7. transferir_para_humano ----------------------------------------------

def transferir_para_humano(conn, *, motivo: str, resumo: str,
                           paciente_id: int | None = None, telefone: str | None = None,
                           agora: datetime | None = None) -> dict:
    """Escalonamento. `risco_clinico` entra como prioridade alta, sempre.

    Transferir não é falha do agente — é a resposta certa para dor no peito,
    pedido de diagnóstico e paciente irritado. O que se mede é se ele
    transfere na hora certa, não se transfere pouco.
    """
    if motivo not in MOTIVOS_TRANSFERENCIA:
        return _falha("motivo_invalido", "Motivo de transferência desconhecido.",
                      motivos_validos=list(MOTIVOS_TRANSFERENCIA))
    if not (resumo or "").strip():
        return _falha("resumo_ausente", "A transferência precisa de um resumo para o atendente.")

    prioridade = "alta" if motivo == "risco_clinico" else "normal"
    cur = conn.execute(
        "INSERT INTO transferencias (motivo, prioridade, paciente_id, telefone, resumo, "
        "criado_em) VALUES (?,?,?,?,?,?)",
        (motivo, prioridade, paciente_id, db.so_digitos(telefone or "") or None,
         resumo.strip(), _agora(agora).strftime(db.FORMATO)))
    return {"ok": True, "protocolo": f"TR-{cur.lastrowid:06d}",
            "motivo": motivo, "prioridade": prioridade,
            "mensagem": ("Vou te passar agora para uma pessoa da equipe."
                         if prioridade == "alta"
                         else "Vou te transferir para um atendente.")}
