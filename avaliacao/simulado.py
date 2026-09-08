"""Recepcionista simulada — determinística, baseada em regras, sem LLM.

Existe por dois motivos:

1. **Provar que a suíte de eval funciona antes de gastar um token.** Se o juiz
   não reprova um agente que alucina, ele não vale nada quando o LLM alucinar.
2. **Baseline.** Uma recepcionista burra de 150 linhas passa X dos 41 cenários.
   O LLM tem que bater isso — senão a complexidade não se pagou.

Ela usa o mesmo normalizador e o mesmo validador do agente de verdade. O que
falta nela é exatamente o que se está pagando o LLM para fazer: entender o que
não foi previsto.
"""
from __future__ import annotations

import json

from clinica.normalizador import (cpf_valido, extrair_digitos,
                                  interpretar_restricao)
from clinica.provedor import ChamadaTool, Resposta

ESPECIALIDADES = {"ortoped": "Ortopedia", "dermato": "Dermatologia",
                  "cardio": "Cardiologia", "fisio": "Fisioterapia",
                  "neuro": "Neurologia"}   # neuro entra de propósito: não existe

RISCO = ("dor no peito", "aperto no peito", "falta de ar", "desmai", "sangrament",
         "dormência", "dormencia", "fala embolada", "tontura")
CLINICO = ("acha que pode ser", "você acha", "voce acha", "é hérnia", "e hernia",
           "tendinite", "artrose", "posso tomar", "anti-inflamatório",
           "anti-inflamatorio", "qual dose", "que remédio", "que remedio")
FRUSTRACAO = ("é robô", "e robo", "robô", "robo", "pessoa de verdade",
              "falar com uma pessoa", "atendente", "quero uma pessoa")
AFIRMATIVO = ("pode confirmar", "confirma", "pode marcar", "isso mesmo", "pode ser",
              "esse serve", "beleza", "tá bom", "ta bom", "isso", "sim", "agora sim")


def _mapa(mensagens):
    nomes = {}
    for m in mensagens:
        for tc in (m.get("tool_calls") or []):
            nomes[tc["id"]] = tc["function"]["name"]
    return [(nomes.get(m.get("tool_call_id")), json.loads(m["content"]))
            for m in mensagens if m.get("role") == "tool"]


class RecepcionistaSimulada:
    """Política fixa. `alucina` e `apressado` existem para testar o juiz."""

    def __init__(self, *, alucina: bool = False, apressado: bool = False):
        self.nome = f"simulado{'+alucina' if alucina else ''}{'+apressado' if apressado else ''}"
        self.alucina = alucina
        self.apressado = apressado

    def responder(self, mensagens, _ferramentas) -> Resposta:
        nomes = {tc["id"]: tc["function"]["name"]
                 for m in mensagens for tc in (m.get("tool_calls") or [])}
        usuarios = [m["content"] for m in mensagens if m.get("role") == "user"]
        resultados = _mapa(mensagens)
        usadas = {n for n, _ in resultados}
        tudo = " ".join(usuarios).lower()
        ultima = (usuarios[-1] if usuarios else "").lower()

        def indice(condicao):
            return max((i for i, m in enumerate(mensagens) if condicao(m)), default=-1)

        i_oferta = indice(lambda m: m.get("role") == "assistant" and m.get("content")
                          and "Posso confirmar" in m["content"])
        i_agenda = indice(lambda m: m.get("role") == "tool"
                          and nomes.get(m.get("tool_call_id")) == "consultar_agenda")
        i_fala = indice(lambda m: m.get("role") == "user")

        transferido = "transferir_para_humano" in usadas
        if transferido:
            return Resposta(texto="Vou te passar para um atendente agora. "
                                  "Só um instante, por favor.")
        if not transferido:
            for gatilhos, motivo, resumo in (
                (RISCO, "risco_clinico", "Paciente relatou sintoma de risco na ligação."),
                (CLINICO, "fora_de_escopo", "Paciente pediu avaliação clínica."),
                (FRUSTRACAO, "frustracao", "Paciente pediu atendimento humano."),
            ):
                if any(g in tudo for g in gatilhos):
                    return self._chamar("transferir_para_humano",
                                        motivo=motivo, resumo=resumo)

        achados = [r for n, r in resultados
                   if n == "buscar_paciente" and r.get("encontrado")]
        paciente = achados[-1] if achados else None
        digitos = extrair_digitos(ultima)
        buscados = {json.dumps(tc["function"]["arguments"], sort_keys=True)
                    for m in mensagens for tc in (m.get("tool_calls") or [])
                    if tc["function"]["name"] == "buscar_paciente"}
        if paciente is None and len(digitos) >= 8:
            args = ({"cpf": digitos} if len(digitos) == 11 and cpf_valido(digitos)
                    else {"telefone": digitos})
            if json.dumps(args, sort_keys=True) not in buscados:
                return self._chamar("buscar_paciente", **args)

        especialidade = next((v for k, v in ESPECIALIDADES.items() if k in tudo), None)
        agendas = [r for n, r in resultados if n == "consultar_agenda"]
        agenda = agendas[-1] if agendas else None

        # Reconsultar quando o paciente muda a restrição: a oferta velha ficou
        # obsoleta. Sem isto o agente ofereceria um horário que ele mesmo já
        # sabe que não serve mais — e o validador barraria na R7.
        mudou_restricao = i_agenda < i_fala and not interpretar_restricao(ultima).vazia()
        if especialidade and (agenda is None or mudou_restricao):
            return self._chamar("consultar_agenda", especialidade=especialidade)

        slots = (agenda or {}).get("slots") or []
        ofereceu = i_oferta > i_agenda
        propos = "propor_reserva" in usadas

        if slots and paciente and not propos:
            if self.apressado or (ofereceu and any(a in ultima for a in AFIRMATIVO)):
                return self._chamar("propor_reserva", slot_id=slots[0]["slot_id"])
            if not ofereceu:
                return Resposta(texto=self._oferta(slots[0]))
            return Resposta(texto=f"Esse horário serve? {self._oferta(slots[0])}")
        if agenda is not None and not slots:
            alt = agenda.get("alternativa")
            if agenda.get("erro") == "especialidade_inexistente":
                disponiveis = ", ".join(agenda.get("especialidades_disponiveis", []))
                return Resposta(texto=f"A gente não atende essa especialidade. "
                                      f"Temos {disponiveis}. Alguma dessas ajuda?")
            if alt:
                return Resposta(texto=f"Não tenho nada dentro do que você pediu. "
                                      f"O mais próximo é {alt['profissional']}, "
                                      f"{alt['descricao']}. Serve?")

        if propos:
            return Resposta(texto="Prontinho, está marcado. Mais alguma coisa?")
        if not especialidade:
            return Resposta(texto="Esta ligação é gravada. Qual especialidade você precisa?")
        if paciente is None:
            return Resposta(texto="Me diz seu telefone com DDD, por favor?")
        return Resposta(texto="Como posso ajudar?")

    def _oferta(self, slot) -> str:
        if self.alucina:
            # troca o dia da semana — é isso que a R8 tem que pegar
            return (f"Consegui com {slot['profissional']}, sexta-feira, "
                    f"{slot['descricao'].split(', ', 1)[1]}. Posso confirmar?")
        return (f"Consegui com {slot['profissional']}, {slot['descricao']}. "
                f"Posso confirmar?")

    @staticmethod
    def _chamar(nome, **args) -> Resposta:
        return Resposta(chamadas=(ChamadaTool(f"c-{nome}", nome, args),))
