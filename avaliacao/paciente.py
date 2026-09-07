"""O paciente do outro lado da linha.

Dois modos, de propósito:

* **Roteirizado** — falas fixas do cenário. Determinístico, custo zero no lado
  do paciente, roda no CI. É o modo em que uma regressão é sempre culpa do
  agente, nunca do sorteio.
* **Sintético** — um segundo LLM com objetivo privado e personalidade. Mais
  realista, não determinístico. É o que encontra o que o roteiro não previu.

O objetivo do paciente sintético é privado: ele não conta o que quer, do mesmo
jeito que ninguém conta. O agente tem que descobrir conversando.
"""
from __future__ import annotations

from clinica.provedor import Provedor, Resposta

DIGITOS = ["zero", "um", "dois", "três", "quatro", "cinco",
           "seis", "sete", "oito", "nove"]


def ditar(digitos: str, *, meia: bool = False, grupo: int = 3) -> str:
    """Escreve os dígitos como uma pessoa fala ao telefone."""
    ditos = [("meia" if meia and d == "6" else DIGITOS[int(d)]) for d in digitos]
    blocos = [" ".join(ditos[i:i + grupo]) for i in range(0, len(ditos), grupo)]
    return ", ".join(blocos)


def ditar_com_correcao(digitos: str, posicao: int = 4) -> str:
    """Ditado com um engano no meio, corrigido na hora — como acontece."""
    ditos = []
    for i, d in enumerate(digitos):
        if i == posicao:
            errado = DIGITOS[(int(d) + 3) % 10]
            ditos.append(f"{errado}, não, {DIGITOS[int(d)]}")
        else:
            ditos.append(DIGITOS[int(d)])
    return ", ".join(ditos)


class PacienteRoteirizado:
    """Fala o que o cenário mandou, na ordem. Ignora o que o agente disse."""

    modo = "roteirizado"

    def __init__(self, falas: list[str], continuacao: tuple[str, ...] = ()):
        self._falas = list(falas) + list(continuacao)
        self._i = 0

    def falar(self, _ultima_fala_do_agente: str) -> str | None:
        if self._i >= len(self._falas):
            return None
        fala = self._falas[self._i]
        self._i += 1
        return fala


SISTEMA_PACIENTE = """Você é um paciente ligando para a Clínica Alvorada.

SEU OBJETIVO (não conte isso, apenas persiga): {objetivo}
SEU JEITO: {personalidade}

Seus dados, se pedirem:
  nome: {nome}
  telefone: {telefone}
  CPF: {cpf}

Fale como gente fala ao telefone: curto, uma coisa por vez, sem formalidade
de e-mail. Nunca diga que é uma IA nem descreva o que está fazendo. Se a
recepcionista já resolveu o que você queria, ou se ficou claro que não vai
resolver, encerre dizendo apenas: ENCERRAR"""


SENTINELA = "ENCERRAR"


def _sem_sentinela(fala: str) -> str | None:
    """Devolve a fala sem o sentinela, ou None se não sobrou nada a dizer."""
    if SENTINELA not in fala.upper():
        return fala or None
    corte = fala.upper().index(SENTINELA)
    restante = fala[:corte].strip()
    return restante or None


class PacienteSintetico:
    """Um segundo agente conversando com o primeiro."""

    modo = "sintetico"

    def __init__(self, provedor: Provedor, *, objetivo: str, personalidade: str,
                 dados: dict, max_turnos: int = 12):
        self.provedor = provedor
        self.max_turnos = max_turnos
        self.mensagens = [{"role": "system", "content": SISTEMA_PACIENTE.format(
            objetivo=objetivo, personalidade=personalidade, **dados)}]
        self._turnos = 0

    def falar(self, ultima_fala_do_agente: str) -> str | None:
        if self._turnos >= self.max_turnos:
            return None
        if ultima_fala_do_agente:
            self.mensagens.append({"role": "user", "content": ultima_fala_do_agente})
        elif self._turnos == 0:
            self.mensagens.append({"role": "user", "content": "[a ligação foi atendida]"})
        resposta: Resposta = self.provedor.responder(self.mensagens, [])
        fala = (resposta.texto or "").strip()
        self.mensagens.append({"role": "assistant", "content": fala})
        self._turnos += 1
        # O modelo põe ENCERRAR no fim da frase, não no começo: "Obrigada!
        # ENCERRAR". Com startswith a ligação nunca terminava e o agente
        # ficava repetindo a mesma resposta até o teto de turnos.
        return _sem_sentinela(fala)
