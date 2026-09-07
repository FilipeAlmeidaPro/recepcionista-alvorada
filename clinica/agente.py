"""Orquestrador da ligação. O LLM conversa; o código decide o que vira escrita.

Três coisas que o modelo **não** controla, de propósito:

* **A restrição do paciente.** Ela é extraída deterministicamente da fala, turno
  a turno, pelo normalizador. O modelo escolhe a especialidade; o código escolhe
  o filtro. Se a restrição viesse do resumo do modelo, a R7 estaria validando o
  modelo contra ele mesmo.
* **A frase de confirmação.** O que vai para a R8 é o que o agente falou no
  turno anterior, lido da transcrição — não o que ele alega ter falado.
* **A escrita.** `propor_*` monta uma intenção e entrega ao validador. Se
  reprovar, o modelo recebe a regra que falhou e tenta de novo, falando.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime

from clinica import db, tools, validador
from clinica.normalizador import (Restricao, interpretar_restricao,
                                  limpar_para_voz, mesclar_restricoes)
from clinica.provedor import Provedor
from clinica.validador import ConfirmacaoVerbal, Intencao

CLINICA = "Clínica Alvorada"
MAX_ITERACOES = 6

SISTEMA = f"""Você é a recepcionista da {CLINICA}, atendendo por telefone.

CANAL
Você está em uma ligação. Fale curto, em português do Brasil, como uma pessoa
fala — não como um texto escrito. Uma pergunta por vez. Nada de listas, títulos
ou markdown: tudo o que você escrever será falado em voz alta.
Na primeira fala da ligação, avise que a chamada é gravada.

LIMITE CLÍNICO
Você não é profissional de saúde. Nunca dê diagnóstico, opinião clínica,
orientação de medicamento ou avaliação de sintoma — nem como hipótese, nem
"na minha opinião". Se pedirem isso, diga que quem responde é o profissional
e use transferir_para_humano com motivo "fora_de_escopo".

RISCO
Se o paciente mencionar dor no peito, falta de ar, desmaio, sangramento
intenso, dormência súbita de um lado do corpo ou fala embolada, pare de
agendar imediatamente e chame transferir_para_humano com motivo
"risco_clinico". Não faça triagem, não pergunte detalhes, não ofereça horário.

AGENDA
Só ofereça horários que vieram de consultar_agenda, exatamente como vieram.
Nunca invente, arredonde ou "aproxime" um horário. Se a ferramenta devolver
zero horários, diga que não tem nada dentro do que o paciente pediu e ofereça
a alternativa que ela devolveu. Ofereça no máximo três opções por vez.

IDENTIDADE
Quando buscar_paciente devolver leitura_para_confirmar, leia esses dígitos de
volta em voz alta, agrupados do jeito que vieram, e espere o paciente confirmar
antes de seguir. Um dígito errado marca a consulta na ficha de outra pessoa.

ANTES DE MARCAR
Repita em voz alta, numa frase só: o profissional, o dia da semana, o dia do
mês e o horário. Depois pergunte se pode confirmar, e espere a resposta.
Só chame propor_reserva depois que o paciente confirmar. Se ele hesitar,
mudar de assunto ou não responder, pergunte de novo — hesitação não é sim.

Se propor_reserva for recusada, explique ao paciente o que houve em uma frase
e resolva pelo caminho certo. Nunca diga que marcou se não marcou."""

FERRAMENTAS = [
    {"type": "function", "function": {
        "name": "buscar_paciente",
        "description": "Localiza o cadastro do paciente por telefone ou CPF.",
        # ["string","null"]: o modelo preenche um e manda o outro como null.
        # A Groq valida o schema no servidor e recusa a chamada inteira se o
        # tipo não admitir null — 6 cenários morreram aqui na primeira rodada.
        "parameters": {"type": "object", "properties": {
            "telefone": {"type": ["string", "null"], "description": "como foi falado"},
            "cpf": {"type": ["string", "null"], "description": "como foi falado"}}}}},
    {"type": "function", "function": {
        "name": "consultar_agenda",
        "description": ("Horários livres de uma especialidade. A restrição de dia e "
                        "hora que o paciente já declarou é aplicada automaticamente."),
        "parameters": {"type": "object", "properties": {
            "especialidade": {"type": "string"},
            "ignorar_restricao": {"type": ["boolean", "null"],
                                  "description": "true para ver opções fora do que o paciente pediu"}},
            "required": ["especialidade"]}}},
    {"type": "function", "function": {
        "name": "propor_reserva",
        "description": ("Propõe marcar um horário já oferecido e já confirmado em voz "
                        "alta pelo paciente. Passa por validação antes de gravar."),
        "parameters": {"type": "object", "properties": {
            "slot_id": {"type": "integer"}}, "required": ["slot_id"]}}},
    {"type": "function", "function": {
        "name": "propor_reagendamento",
        "description": "Move um agendamento existente para outro horário já confirmado.",
        "parameters": {"type": "object", "properties": {
            "agendamento_id": {"type": "integer"},
            "novo_slot_id": {"type": "integer"}},
            "required": ["agendamento_id", "novo_slot_id"]}}},
    {"type": "function", "function": {
        "name": "propor_cancelamento",
        "description": ("Cancela um agendamento já confirmado em voz alta pelo "
                        "paciente. Passa por validação antes de liberar o horário."),
        "parameters": {"type": "object", "properties": {
            "agendamento_id": {"type": "integer"}}, "required": ["agendamento_id"]}}},
    {"type": "function", "function": {
        "name": "transferir_para_humano",
        "description": "Passa a ligação para um atendente.",
        "parameters": {"type": "object", "properties": {
            "motivo": {"type": "string",
                       "enum": list(tools.MOTIVOS_TRANSFERENCIA)},
            "resumo": {"type": "string", "description": "uma frase para o atendente"}},
            "required": ["motivo", "resumo"]}}},
]


@dataclass
class Turno:
    fala_paciente: str
    fala_agente: str
    eventos: list[dict] = field(default_factory=list)
    latencia_ms: float = 0.0
    tokens_entrada: int = 0
    tokens_saida: int = 0

    def ferramentas(self) -> list[str]:
        return [e["ferramenta"] for e in self.eventos]


class Agente:
    def __init__(self, conn, provedor: Provedor, *, ligacao_id: str,
                 agora: datetime, hoje: date | None = None):
        self.conn = conn
        self.provedor = provedor
        self.ligacao_id = ligacao_id
        self.agora = agora
        self.hoje = hoje or agora.date()
        self.mensagens: list[dict] = [{"role": "system", "content": SISTEMA}]
        self.turnos: list[Turno] = []

        self.paciente_id: int | None = None
        self.restricao = Restricao()
        self.slots_oferecidos: set[int] = set()
        self.ultima_fala_agente = ""
        self.agendamento_id: int | None = None
        self.transferencia: dict | None = None

    # --- laço principal ---

    def dizer(self, texto: str) -> Turno:
        """Um turno do paciente. Devolve o que o agente respondeu e o trace."""
        self.restricao = mesclar_restricoes(
            self.restricao, interpretar_restricao(texto, self.hoje))
        self.mensagens.append({"role": "user", "content": texto})

        turno = Turno(fala_paciente=texto, fala_agente="")
        inicio = time.perf_counter()

        for _ in range(MAX_ITERACOES):
            resposta = self.provedor.responder(self.mensagens, FERRAMENTAS)
            turno.tokens_entrada += resposta.tokens_entrada
            turno.tokens_saida += resposta.tokens_saida

            if not resposta.chamadas:
                fala = limpar_para_voz(resposta.texto or "")
                self.mensagens.append({"role": "assistant", "content": fala})
                turno.fala_agente = fala
                self.ultima_fala_agente = fala
                break

            # Devolve a mensagem do provedor como ela veio. Só reconstrói
            # quando não há original (provedor roteirizado, nos testes).
            self.mensagens.append(resposta.bruto or {
                "role": "assistant", "content": resposta.texto,
                "tool_calls": [{"id": c.id, "type": "function",
                                "function": {"name": c.nome,
                                             "arguments": json.dumps(c.argumentos)}}
                               for c in resposta.chamadas]})
            for chamada in resposta.chamadas:
                resultado = self._executar(chamada, texto)
                turno.eventos.append({"ferramenta": chamada.nome,
                                      "argumentos": chamada.argumentos,
                                      "resultado": resultado})
                self.mensagens.append({
                    "role": "tool", "tool_call_id": chamada.id,
                    "content": json.dumps(resultado, ensure_ascii=False)})
        else:
            turno.fala_agente = ""
            turno.eventos.append({"ferramenta": "__limite__",
                                  "argumentos": {}, "resultado":
                                  {"ok": False, "erro": "excedeu_iteracoes"}})

        turno.latencia_ms = (time.perf_counter() - inicio) * 1000
        self.turnos.append(turno)
        return turno

    # --- despacho de ferramentas ---

    def _executar(self, chamada, fala_paciente: str) -> dict:
        args = dict(chamada.argumentos)
        # Transferiu, acabou. Continuar consultando agenda depois de escalar por
        # dor no peito é pior do que não ter escalado — dá ao paciente a
        # impressão de que ainda está sendo atendido pela máquina.
        if self.transferencia is not None:
            return {"ok": False, "erro": "ligacao_encerrada",
                    "mensagem": "A ligação já foi transferida para um atendente. "
                                "Apenas se despeça."}
        metodo = getattr(self, f"_t_{chamada.nome}", None)
        if metodo is None:
            return {"ok": False, "erro": "ferramenta_desconhecida",
                    "mensagem": f"Não existe a ferramenta {chamada.nome}."}
        try:
            return metodo(args, fala_paciente)
        except TypeError as e:
            return {"ok": False, "erro": "argumentos_invalidos", "mensagem": str(e)}

    def _t_buscar_paciente(self, args, _fala) -> dict:
        r = tools.buscar_paciente(self.conn, telefone=args.get("telefone"),
                                  cpf=args.get("cpf"))
        # LGPD: identificado o paciente, o documento não é mais necessário para
        # a tarefa. Sai do histórico que segue para o modelo a cada turno.
        if r.get("encontrado"):
            for m in reversed(self.mensagens):
                if m.get("role") == "user":
                    m["content"] = db.mascarar_falado(m["content"])
                    break
        if r.get("encontrado"):
            novo = r["paciente"]["id"]
            if self.paciente_id is not None and novo != self.paciente_id:
                # "na verdade era pra minha mãe": trocou o paciente, zera o pedido.
                self.restricao = Restricao()
                self.slots_oferecidos.clear()
                self.agendamento_id = None
            self.paciente_id = novo
        return r

    def _t_consultar_agenda(self, args, _fala) -> dict:
        filtros = {} if args.get("ignorar_restricao") else self.restricao.como_filtros()
        r = tools.consultar_agenda(self.conn, especialidade=args.get("especialidade", ""),
                                   agora=self.agora, **filtros)
        for slot in r.get("slots", []):
            self.slots_oferecidos.add(slot["slot_id"])
        if r.get("alternativa"):
            self.slots_oferecidos.add(r["alternativa"]["slot_id"])
        if self.restricao.ambigua and not args.get("ignorar_restricao"):
            r["confirme_a_restricao"] = (
                f"Entendi «{self.restricao.interpretacao}» — confirme isso em voz "
                f"alta com o paciente antes de marcar.")
        return r

    def _t_propor_reserva(self, args, fala_paciente: str) -> dict:
        if self.paciente_id is None:
            return {"ok": False, "erro": "paciente_nao_identificado",
                    "mensagem": "Identifique o paciente antes de marcar."}
        intencao = Intencao(
            slot_id=args.get("slot_id"), paciente_id=self.paciente_id,
            idempotency_key=f"{self.ligacao_id}:{args.get('slot_id')}",
            restricao=self.restricao,
            confirmacao=ConfirmacaoVerbal(self.ultima_fala_agente, fala_paciente),
            slots_oferecidos=tuple(sorted(self.slots_oferecidos)))
        r = validador.executar_reserva(self.conn, intencao, agora=self.agora)
        if r.get("ok"):
            self.agendamento_id = r["agendamento"]["id"]
        return r

    def _t_propor_reagendamento(self, args, fala_paciente: str) -> dict:
        intencao = Intencao(
            slot_id=args.get("novo_slot_id"), paciente_id=self.paciente_id or 0,
            idempotency_key=f"{self.ligacao_id}:rea:{args.get('novo_slot_id')}",
            agendamento_id=args.get("agendamento_id"),
            restricao=self.restricao,
            confirmacao=ConfirmacaoVerbal(self.ultima_fala_agente, fala_paciente),
            slots_oferecidos=tuple(sorted(self.slots_oferecidos)))
        r = validador.executar_reagendamento(self.conn, intencao, agora=self.agora)
        if r.get("ok"):
            self.agendamento_id = r["agendamento"]["id"]
        return r

    def _t_propor_cancelamento(self, args, fala_paciente: str) -> dict:
        intencao = Intencao(
            slot_id=0, paciente_id=self.paciente_id or 0,
            idempotency_key=f"{self.ligacao_id}:cancel:{args.get('agendamento_id')}",
            agendamento_id=args.get("agendamento_id"),
            confirmacao=ConfirmacaoVerbal(self.ultima_fala_agente, fala_paciente))
        r = validador.executar_cancelamento(self.conn, intencao, agora=self.agora)
        if r.get("ok") and self.agendamento_id == args.get("agendamento_id"):
            self.agendamento_id = None
        return r

    def _t_transferir_para_humano(self, args, _fala) -> dict:
        r = tools.transferir_para_humano(
            self.conn, motivo=args.get("motivo", ""), resumo=args.get("resumo", ""),
            paciente_id=self.paciente_id, agora=self.agora)
        if r.get("ok"):
            self.transferencia = r
        return r

    # --- leitura do resultado da ligação ---

    def motivo_contato(self) -> str:
        """Para que a pessoa ligou, lido do que aconteceu — não do que ela disse.

        O agregado do painel precisa disto e o plano pedia; derivar do trace é
        mais confiável que pedir ao modelo para se autoclassificar.
        """
        if self.transferencia:
            return f"transferência: {self.transferencia['motivo']}"
        especialidades = [e["argumentos"].get("especialidade")
                          for t in self.turnos for e in t.eventos
                          if e["ferramenta"] == "consultar_agenda"]
        especialidade = next((x for x in reversed(especialidades) if x), None)
        if self.agendamento_id and especialidade:
            return f"agendamento: {especialidade}"
        if self.agendamento_id:
            return "agendamento"
        if especialidade:
            return f"consulta de agenda: {especialidade}"
        if self.paciente_id:
            return "identificação sem desfecho"
        return "não identificado"

    def resultado(self) -> dict:
        return {
            "ligacao_id": self.ligacao_id,
            "paciente_id": self.paciente_id,
            "motivo_contato": self.motivo_contato(),
            "agendou": self.agendamento_id is not None,
            "agendamento_id": self.agendamento_id,
            "transferiu": self.transferencia is not None,
            "motivo_transferencia": (self.transferencia or {}).get("motivo"),
            "turnos": len(self.turnos),
            "ferramentas": [f for t in self.turnos for f in t.ferramentas()],
            "bloqueios": [e["resultado"].get("regra")
                          for t in self.turnos for e in t.eventos
                          if e["resultado"].get("erro") == "reprovado_pelo_validador"],
            "tokens_entrada": sum(t.tokens_entrada for t in self.turnos),
            "tokens_saida": sum(t.tokens_saida for t in self.turnos),
            "latencia_total_ms": round(sum(t.latencia_ms for t in self.turnos), 2),
            "latencias_ms": [round(t.latencia_ms, 2) for t in self.turnos],
            # A transcrição é o que vai para o trace, o painel e o log — nunca
            # com documento em claro dentro.
            "transcricao": [{"papel": p, "texto": db.mascarar_falado(txt)}
                            for t in self.turnos
                            for p, txt in (("paciente", t.fala_paciente),
                                           ("agente", t.fala_agente)) if txt],
        }
