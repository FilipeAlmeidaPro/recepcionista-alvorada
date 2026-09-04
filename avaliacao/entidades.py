"""Taxa de erro em entidades: antes e depois do normalizador (§3.3).

Três métodos, o mesmo conjunto rotulado de 50 falas:

* **regex ingênuo** — o que sai de uma implementação de primeira tentativa.
* **LLM cru** — a fala vai direto para o modelo, que devolve a entidade. É o
  "jogado no colo do LLM" que o plano diz que não foi feito. O modelo recebe
  exatamente o mesmo contexto que o código determinístico tem (data de hoje,
  catálogo de especialidades, cadastro de nomes) — senão a comparação seria
  desonesta.
* **normalizador determinístico** — `clinica/normalizador.py`.

    python3 -m avaliacao.entidades              # os três métodos
    python3 -m avaliacao.entidades --sem-llm    # só os dois offline, custo zero
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import date

from clinica.normalizador import (casar_especialidade, casar_nome,
                                  extrair_digitos, interpretar_restricao)

HOJE = date(2026, 9, 3)          # quinta-feira
NADA = "·"

ESPECIALIDADES = ["Ortopedia", "Dermatologia", "Cardiologia", "Fisioterapia"]
CADASTRO = ["Thaís Vasconcelos", "Wesley Bittencourt", "Larissa Nakamura",
            "Kauã Figueiredo", "Heitor Cruz", "Márcio D'Ávila", "Sabrina Yamada",
            "Priscila Fontoura", "Everton Pacheco", "Bianca Ramalho"]


@dataclass(frozen=True)
class Caso:
    tipo: str
    falado: str
    esperado: str


CASOS: list[Caso] = [
    # --- dígitos ditados (12) ---
    Caso("digitos", "quatro, três... não, dois", "42"),
    Caso("digitos", "meia meia sete oito", "6678"),
    Caso("digitos", "vinte e três, zero um", "2301"),
    Caso("digitos", "um um um, quatro quatro quatro, sete sete sete, trinta e cinco",
         "11144477735"),
    Caso("digitos", "onze, nove oito sete seis cinco, quatro três dois um",
         "11987654321"),
    Caso("digitos", "zero zero, nove, na verdade oito", "008"),
    Caso("digitos", "cinco cinco, onze, nove nove nove, oito oito, sete sete",
         "55119998877"),
    Caso("digitos", "meia, meia, meia, dois, dois, dois, um, um, um, zero, zero",
         "66622211100"),
    Caso("digitos", "é o dois um, nove nove, meia meia, cinco quatro, três dois",
         "2199665432"),
    Caso("digitos", "nove, oito, sete, seis, cinco, quatro, três, dois, um, zero, nove",
         "98765432109"),
    Caso("digitos", "trinta e um, quarenta e dois, cinquenta e três", "314253"),
    Caso("digitos", "um dois três, desculpa, quatro", "124"),

    # --- restrição de horário (12) — formato "min-max" ---
    Caso("horario", "só consigo depois das seis", f"18:00-{NADA}"),
    Caso("horario", "depois das seis da tarde", f"18:00-{NADA}"),
    Caso("horario", "às oito da manhã", "08:00-08:00"),
    Caso("horario", "antes das cinco", f"{NADA}-17:00"),
    Caso("horario", "entre duas e quatro da tarde", "14:00-16:00"),
    Caso("horario", "de manhã", "06:00-11:59"),
    Caso("horario", "só à noite", "18:00-21:00"),
    Caso("horario", "depois das seis e meia", f"18:30-{NADA}"),
    Caso("horario", "ao meio-dia", "12:00-12:00"),
    Caso("horario", "às 18h30", "18:30-18:30"),
    Caso("horario", "a partir das sete da noite", f"19:00-{NADA}"),
    Caso("horario", "até as onze", f"{NADA}-11:00"),

    # --- data relativa (10) ---
    Caso("data", "amanhã", "2026-09-04"),
    Caso("data", "depois de amanhã", "2026-09-05"),
    Caso("data", "quinta que vem", "2026-09-10"),
    Caso("data", "terça que vem", "2026-09-08"),
    Caso("data", "próxima segunda", "2026-09-07"),
    Caso("data", "dia quinze", "2026-09-15"),
    Caso("data", "dia quinze do mês que vem", "2026-10-15"),
    Caso("data", "quinze de outubro", "2026-10-15"),
    Caso("data", "hoje", "2026-09-03"),
    Caso("data", "dia dois", "2026-10-02"),

    # --- nome próprio saído do STT (8) ---
    Caso("nome", "Taís Vasconselos", "Thaís Vasconcelos"),
    Caso("nome", "Uesley Bitencourt", "Wesley Bittencourt"),
    Caso("nome", "Larisa Nakamura", "Larissa Nakamura"),
    Caso("nome", "Cauã Figueredo", "Kauã Figueiredo"),
    Caso("nome", "Eitor Cruz", "Heitor Cruz"),
    Caso("nome", "Marcio Davila", "Márcio D'Ávila"),
    Caso("nome", "Sabrina Iamada", "Sabrina Yamada"),
    Caso("nome", "Roberto Alves", NADA),

    # --- especialidade (8) ---
    Caso("especialidade", "ortopedista", "Ortopedia"),
    Caso("especialidade", "dermatologista", "Dermatologia"),
    Caso("especialidade", "fisioterapeuta", "Fisioterapia"),
    Caso("especialidade", "cardiologista", "Cardiologia"),
    Caso("especialidade", "ortopedia", "Ortopedia"),
    Caso("especialidade", "neurologista", NADA),
    Caso("especialidade", "psiquiatra", NADA),
    Caso("especialidade", "fisio", "Fisioterapia"),
]


# --- método 1: regex ingênuo -------------------------------------------------

def _ingenuo(caso: Caso) -> str:
    t = caso.falado
    if caso.tipo == "digitos":
        return "".join(re.findall(r"\d", t)) or NADA
    if caso.tipo == "horario":
        m = re.search(r"(\d{1,2})\s*[h:](\d{2})?", t)
        if not m:
            return f"{NADA}-{NADA}"
        hh = f"{int(m.group(1)):02d}:{int(m.group(2) or 0):02d}"
        return f"{hh}-{hh}"
    if caso.tipo == "data":
        m = re.search(r"\d{4}-\d{2}-\d{2}", t)
        return m.group(0) if m else NADA
    if caso.tipo == "nome":
        return t if t in CADASTRO else NADA
    return t if t in ESPECIALIDADES else NADA


# --- método 2: normalizador determinístico -----------------------------------

def _deterministico(caso: Caso) -> str:
    t = caso.falado
    if caso.tipo == "digitos":
        return extrair_digitos(t) or NADA
    if caso.tipo in ("horario", "data"):
        r = interpretar_restricao(t, HOJE)
        if caso.tipo == "horario":
            return f"{r.hora_min or NADA}-{r.hora_max or NADA}"
        return r.data_inicio or NADA
    if caso.tipo == "nome":
        return casar_nome(t, CADASTRO)["melhor"] or NADA
    return casar_especialidade(t, ESPECIALIDADES) or NADA


# --- método 3: LLM cru -------------------------------------------------------

INSTRUCAO = {
    "digitos": "Devolva só os dígitos que a pessoa ditou, sem espaço nem "
               "pontuação. Ela pode ter se corrigido no meio ('quatro, não, "
               "dois' = '2'); 'meia' vale 6.",
    "horario": "Devolva a restrição de horário como 'HH:MM-HH:MM' (início "
               f"mínimo e máximo aceitos). Use '{NADA}' na ponta que ela não "
               "declarou. A clínica funciona das 07h às 20h.",
    "data": f"Hoje é {HOJE.isoformat()}, uma quinta-feira. Devolva a data que "
            "a pessoa quis dizer, no formato AAAA-MM-DD.",
    "nome": f"Devolva o nome do cadastro que a pessoa quis dizer, exatamente "
            f"como está escrito na lista. Se não houver, devolva '{NADA}'. "
            f"Cadastro: {CADASTRO}",
    "especialidade": f"Devolva a especialidade da lista {ESPECIALIDADES} que a "
                     f"pessoa quis dizer, exatamente como escrita. Se a clínica "
                     f"não atender, devolva '{NADA}'.",
}


def _llm(caso: Caso, provedor) -> str:
    mensagens = [
        {"role": "system",
         "content": "Você extrai uma entidade de uma fala transcrita em "
                    "português do Brasil. Responda SOMENTE com JSON "
                    '{"valor": "..."} e nada mais.'},
        {"role": "user",
         "content": f"{INSTRUCAO[caso.tipo]}\n\nFala: «{caso.falado}»"},
    ]
    for tentativa in range(3):
        try:
            texto = (provedor.responder(mensagens, []).texto or "").strip()
            break
        except RuntimeError as e:
            if "429" not in str(e) or tentativa == 2:
                return f"<erro: {str(e)[:40]}>"
            time.sleep(4 * (tentativa + 1))
    else:
        return "<erro>"
    bruto = re.search(r"\{.*\}", texto, re.S)
    if not bruto:
        return texto[:40] or NADA
    try:
        return str(json.loads(bruto.group(0)).get("valor", NADA)).strip()
    except json.JSONDecodeError:
        return texto[:40]


# --- relatório ---------------------------------------------------------------

def amostra_estratificada(casos: list[Caso], n: int) -> list[Caso]:
    """Reduz o conjunto mantendo a proporção entre tipos.

    Existe por causa da cota: 50 casos não cabem em 20 chamadas por dia. O
    número menor é dito em voz alta no relatório em vez de escondido.
    """
    tipos: dict[str, list[Caso]] = {}
    for c in casos:
        tipos.setdefault(c.tipo, []).append(c)
    por_tipo = max(1, n // len(tipos))
    escolhidos = [c for grupo in tipos.values() for c in grupo[:por_tipo]]
    return escolhidos[:n]


def medir(provedor=None, casos: list[Caso] | None = None) -> dict:
    metodos = {"regex ingênuo": _ingenuo, "normalizador": _deterministico}
    if provedor is not None:
        metodos["LLM cru"] = lambda c: _llm(c, provedor)

    casos = casos if casos is not None else CASOS
    ordem = [c.tipo for c in CASOS]
    tipos = sorted({c.tipo for c in casos}, key=ordem.index)
    placar = {m: {t: [0, 0] for t in tipos} for m in metodos}
    erros = {m: [] for m in metodos}
    for caso in casos:
        for nome, fn in metodos.items():
            obtido = fn(caso)
            acertou = obtido == caso.esperado
            placar[nome][caso.tipo][0] += acertou
            placar[nome][caso.tipo][1] += 1
            if not acertou:
                erros[nome].append((caso, obtido))
    return {"tipos": tipos, "placar": placar, "erros": erros,
            "total": len(casos), "metodos": list(metodos)}


def tabela(m: dict) -> str:
    largura = max(len(x) for x in m["metodos"]) + 2
    cab = f"{'método':<{largura}}" + "".join(f"{t:>15}" for t in m["tipos"]) + f"{'TOTAL':>10}"
    linhas = [cab, "─" * len(cab)]
    for metodo in m["metodos"]:
        acertos = total = 0
        celulas = ""
        for tipo in m["tipos"]:
            a, n = m["placar"][metodo][tipo]
            acertos, total = acertos + a, total + n
            celulas += f"{f'{a}/{n}  {a / n:.0%}':>15}"
        linhas.append(f"{metodo:<{largura}}{celulas}"
                      f"{f'{acertos / total:.0%}':>10}")
    return "\n".join(linhas)


def main() -> int:
    p = argparse.ArgumentParser(description="Taxa de erro em entidades.")
    p.add_argument("--sem-llm", action="store_true",
                   help="só os métodos offline (custo zero)")
    p.add_argument("--erros", action="store_true", help="lista o que cada método errou")
    p.add_argument("--amostra", type=int, metavar="N",
                   help="amostra estratificada de N casos (cota do free tier)")
    p.add_argument("--modelo", help="modelo específico para o método LLM")
    args = p.parse_args()

    casos = amostra_estratificada(CASOS, args.amostra) if args.amostra else CASOS

    provedor = None
    if not args.sem_llm:
        from clinica.provedor import ProvedorOpenAICompativel, provedor_padrao
        provedor = (ProvedorOpenAICompativel("gemini", modelo=args.modelo,
                                             orcamento=len(casos))
                    if args.modelo else provedor_padrao())
        if provedor is None:
            print("sem chave de LLM — rodando só os métodos offline\n", file=sys.stderr)

    inicio = time.perf_counter()
    m = medir(provedor, casos)
    amostrado = "" if len(casos) == len(CASOS) else f" (amostra de {len(CASOS)})"
    print(f"{m['total']} falas rotuladas{amostrado} · {len(m['metodos'])} métodos "
          f"· modelo {getattr(provedor, 'nome', '—')} "
          f"· {time.perf_counter() - inicio:.1f}s\n")
    print(tabela(m))

    if args.erros:
        for metodo in m["metodos"]:
            if not m["erros"][metodo]:
                continue
            print(f"\nerros de «{metodo}»")
            for caso, obtido in m["erros"][metodo]:
                print(f"  [{caso.tipo}] «{caso.falado[:44]}»")
                print(f"       esperado {caso.esperado!r}   obtido {obtido!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
