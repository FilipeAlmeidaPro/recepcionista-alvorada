"""Normalização de português falado. Determinístico, sem LLM.

Três problemas que o STT entrega e que ninguém mede:

1. **Restrição de horário e data em linguagem natural** — "depois das seis",
   "quinta que vem", "dia quinze do mês que vem".
2. **Dígitos ditados com hesitação** — CPF e telefone, com correção no meio
   da fala ("quatro, três... não, dois") e o "meia" brasileiro valendo 6.
3. **Nomes próprios brasileiros** — Thaís vira Taís, Wesley vira Uesley,
   Vasconcelos vira Vasconselos.

Nada disso vai para o prompt. Resolver no código é mais barato, é testável, e
o erro fica onde dá para medir. O que o LLM recebe é o resultado já
estruturado — e, quando a interpretação foi um chute, um aviso de que ele
precisa confirmar em voz alta antes de escrever qualquer coisa.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from difflib import SequenceMatcher

from clinica.db import sem_acento
from clinica.idioma import PT, Idioma

_TOKEN = re.compile(r"\d+|[a-z]+")

# As tabelas de língua moram em `idioma.py`. O que fica aqui é o algoritmo —
# ele é um só, e é a razão de o inglês custar uma tabela e não um módulo novo.
# Os nomes abaixo são re-exportados em português por compatibilidade: código
# antigo que importava DIAS_SEMANA continua funcionando, agora explicitamente
# como "as tabelas do idioma padrão".
_EXTENSO = PT.extenso
_CORRECAO = PT.correcao
DIAS_SEMANA = PT.dias_semana
MESES = PT.meses
_MARC_MIN = PT.marc_min
_MARC_MAX = PT.marc_max
_PERIODOS = PT.periodos
_LIGACOES = PT.ligacoes
_PREPOSICOES = PT.preposicoes
MARCAS_DE_PASSADO = PT.marcas_passado
_NOMES_DIA = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]

_COMPOSTOS = {20, 30, 40, 50, 60, 70, 80, 90}


def tokenizar(texto: str) -> list[str]:
    return _TOKEN.findall(sem_acento(texto))


def _ler_numero(tokens: list[str], i: int, idi: Idioma = PT) -> tuple[int | None, int]:
    """Lê um número em dígito ou por extenso, inclusive composto.

    Composto tem duas formas: "vinte e três" liga com "e", "twenty three" não
    liga com nada. As duas casam no mesmo laço porque o que importa é a segunda
    parte valer menos que a dezena."""
    if i >= len(tokens):
        return None, i
    tk = tokens[i]
    if tk.isdigit():
        return int(tk), i + 1
    if tk not in idi.extenso:
        return None, i
    valor = idi.extenso[tk]
    prox = i + 1
    if valor in _COMPOSTOS and prox < len(tokens):
        salto = 1 if tokens[prox] in idi.conector_minuto | {"e"} else 0
        if prox + salto < len(tokens):
            parte, depois = _ler_numero(tokens, prox + salto, idi)
            if parte is not None and parte < valor and (salto or idi.codigo != "pt"):
                return valor + parte, depois
    return valor, prox


# --- 1. dígitos ditados ------------------------------------------------------

def extrair_digitos(texto: str, idi: Idioma = PT) -> str:
    """CPF/telefone ditado, com hesitação e correção.

        "quatro, três... não, dois"  -> "42"
        "meia meia, sete oito"       -> "6678"
        "vinte e três, zero um"      -> "2301"

    A correção descarta o último grupo emitido — que é como as pessoas de fato
    se corrigem ao ditar. Correção de dois grupos de uma vez não é coberta, e
    isso está documentado em vez de escondido.
    """
    limpo = sem_acento(texto).replace("na verdade", "alias")
    tokens = _TOKEN.findall(limpo)
    grupos: list[str] = []
    i = 0
    while i < len(tokens):
        tk = tokens[i]
        if tk in idi.correcao:
            if grupos:
                grupos.pop()
            i += 1
        elif tk == "meia" and idi.codigo == "pt":
            grupos.append("6")
            i += 1
        else:
            valor, prox = _ler_numero(tokens, i, idi)
            if valor is None:
                i += 1
            else:
                grupos.append(str(valor))
                i = prox
    return "".join(grupos)


def cpf_valido(cpf: str) -> bool:
    """Dígitos verificadores. O validador precisa poder recusar um CPF inventado."""
    d = [int(c) for c in cpf if c.isdigit()]
    if len(d) != 11 or len(set(d)) == 1:
        return False
    for tamanho in (9, 10):
        soma = sum(v * p for v, p in zip(d[:tamanho], range(tamanho + 1, 1, -1)))
        resto = 11 - soma % 11
        if d[tamanho] != (0 if resto >= 10 else resto):
            return False
    return True


# --- 2. restrição de horário e data ------------------------------------------

@dataclass(frozen=True)
class Restricao:
    """O que o paciente declarou. É contra isto que o validador confere o slot."""
    hora_min: str | None = None
    hora_max: str | None = None
    dias_semana: tuple[int, ...] | None = None
    data_inicio: str | None = None
    data_fim: str | None = None
    ambigua: bool = False
    interpretacao: str = ""

    def vazia(self) -> bool:
        return not any((self.hora_min, self.hora_max, self.dias_semana,
                        self.data_inicio, self.data_fim))

    def como_filtros(self) -> dict:
        """Argumentos prontos para `consultar_agenda`."""
        return {k: v for k, v in {
            "hora_min": self.hora_min, "hora_max": self.hora_max,
            "dias_semana": list(self.dias_semana) if self.dias_semana else None,
            "data_inicio": self.data_inicio, "data_fim": self.data_fim,
        }.items() if v is not None}


def _periodo_dominante(tokens, idi: Idioma = PT) -> str | None:
    """O período dito em qualquer ponto da frase, não só colado no número.

    "à noite, antes das oito" são oito da NOITE. Sem isto o 8 virava 08:00 e a
    restrição saía 18:00–08:00 — impossível.
    """
    for i, tk in enumerate(tokens):
        if tk in idi.periodos and (i == 0 or tokens[i - 1] in idi.preposicoes):
            return tk
    return None


def _ler_hora(tokens, i, permitir_minutos=True,
              periodo_padrao=None, idi: Idioma = PT
              ) -> tuple[tuple[int, int] | None, int, bool]:
    while i < len(tokens) and tokens[i] in idi.ligacoes:
        i += 1
    if i >= len(tokens):
        return None, i, False
    if _frase_em(tokens, i, idi.meio_dia):
        return (12, 0), i + _tamanho_frase(idi.meio_dia), False
    if _frase_em(tokens, i, idi.meia_noite):
        return (0, 0), i + _tamanho_frase(idi.meia_noite), False

    # "half past six" — o meio vem ANTES da hora em inglês, e depois dela em
    # português ("seis e meia"). É a única diferença de ordem que o parser
    # precisa conhecer.
    meia_antes = False
    if idi.prefixo_meia_hora and tokens[i:i + len(idi.prefixo_meia_hora)] == list(idi.prefixo_meia_hora):
        meia_antes, i = True, i + len(idi.prefixo_meia_hora)
        while i < len(tokens) and tokens[i] in idi.ligacoes:
            i += 1

    valor, prox = _ler_numero(tokens, i, idi)
    if valor is None or valor > 23:
        return None, i, False
    hora, minuto = valor, 30 if meia_antes else 0

    if prox < len(tokens) and tokens[prox] in idi.sufixo_hora:
        prox += 1
    if permitir_minutos and not meia_antes and prox < len(tokens):
        if tokens[prox] in idi.conector_minuto:
            if prox + 1 < len(tokens) and tokens[prox + 1] in idi.meia_hora:
                minuto, prox = 30, prox + 2
            else:
                mv, mprox = _ler_numero(tokens, prox + 1, idi)
                if mv is not None and mv < 60:
                    minuto, prox = mv, mprox
        elif tokens[prox].isdigit() and len(tokens[prox]) == 2 and int(tokens[prox]) < 60:
            minuto, prox = int(tokens[prox]), prox + 1
        elif idi.minuto_sem_conector:
            # "six thirty": em inglês o minuto vem colado, sem conector. Só
            # aceita palavra de número — senão "six doctors" viraria 6:00 e
            # engoliria o substantivo seguinte.
            tk = tokens[prox]
            if tk in idi.extenso and 0 < idi.extenso[tk] < 60 and tk not in idi.dias_semana:
                minuto, prox = idi.extenso[tk], prox + 1

    # "6pm" / "six pm": o sufixo faz o papel que em português cabe ao período.
    periodo = None
    if prox < len(tokens) and tokens[prox] in idi.sufixo_am_pm:
        periodo, prox = idi.sufixo_am_pm[tokens[prox]], prox + 1

    if periodo is None:
        j = prox
        while j < len(tokens) and tokens[j] in idi.prep_periodo:
            j += 1
        if j < len(tokens) and tokens[j] in idi.periodos:
            periodo, prox = tokens[j], j + 1
        elif periodo_padrao:
            periodo = periodo_padrao      # o período da frase vale para esta hora

    ambiguo = False
    if periodo in _TARDINHA and hora < 12:
        hora += 12
    elif periodo in _MANHA and hora == 12:
        hora = 0                          # "12 am" é meia-noite
    elif periodo is None and 1 <= hora <= 6:
        # "depois das seis" em clínica é 18h. É um chute — bom, mas chute.
        # Marcar como ambíguo obriga o agente a confirmar em voz alta.
        hora, ambiguo = hora + 12, True
    return (hora, minuto), prox, ambiguo


# Os períodos que empurram uma hora pequena para a tarde/noite, nos dois
# idiomas. Nomeados por valor e não por língua: é a semântica que decide.
_TARDINHA = {"tarde", "noite", "afternoon", "evening", "night"}
_MANHA = {"manha", "morning"}


def _frase_em(tokens, i, frases: tuple[str, ...]) -> bool:
    for frase in frases:
        partes = frase.split()
        if tokens[i:i + len(partes)] == partes:
            return True
    return False


def _tamanho_frase(frases: tuple[str, ...]) -> int:
    return len(frases[0].split())


def _hhmm(h: int, m: int) -> str:
    return f"{h:02d}:{m:02d}"


def _restricao_horaria(tokens, idi: Idioma = PT
                       ) -> tuple[str | None, str | None, bool, set[int]]:
    """Devolve também os índices consumidos.

    Sem isso, o "da tarde" de "depois das seis da tarde" era usado duas vezes:
    uma para desambiguar a hora (6 → 18) e outra como faixa do período — o que
    produzia a restrição impossível 18:00–17:59, com zero horários possíveis.
    """
    dominante = _periodo_dominante(tokens, idi)
    hora_min = hora_max = None
    ambigua = False
    consumidos: set[int] = set()
    i = 0
    while i < len(tokens):
        tk = tokens[i]
        if tk == idi.entre:
            a, prox, amb_a = _ler_hora(tokens, i + 1, False, dominante, idi)
            if a:
                while prox < len(tokens) and tokens[prox] in idi.entre_conect:
                    prox += 1
                b, prox, amb_b = _ler_hora(tokens, prox, False, dominante, idi)
                if b:
                    hora_min, hora_max = _hhmm(*a), _hhmm(*b)
                    # "entre duas e quatro DA TARDE": o período fecha as duas
                    # pontas, então só é ambíguo quando nenhuma delas tem período.
                    ambigua = ambigua or (amb_a and amb_b)
                    consumidos.update(range(i, prox))
                    i = prox
                    continue
        elif tk in idi.marc_min:
            h, prox, amb = _ler_hora(tokens, i + 1, True, dominante, idi)
            if h:
                hora_min, ambigua = _hhmm(*h), ambigua or amb
                consumidos.update(range(i, prox))
                i = prox
                continue
        elif tk in idi.marc_max:
            h, prox, amb = _ler_hora(tokens, i + 1, True, dominante, idi)
            if h:
                hora_max, ambigua = _hhmm(*h), ambigua or amb
                consumidos.update(range(i, prox))
                i = prox
                continue
        i += 1
    if hora_min or hora_max:
        return hora_min, hora_max, ambigua, consumidos

    # Sem marcador de faixa: horário exato ("às oito da manhã", "at noon").
    for i, _tk in enumerate(tokens):
        if _frase_em(tokens, i, idi.meio_dia):
            return "12:00", "12:00", False, set(range(len(tokens)))
        if _frase_em(tokens, i, idi.meia_noite):
            return "00:00", "00:00", False, set(range(len(tokens)))
    for i, tk in enumerate(tokens):
        if tk in idi.marcador_exato:
            h, prox, amb = _ler_hora(tokens, i + 1, True, dominante, idi)
            if h:
                return _hhmm(*h), _hhmm(*h), amb, set(range(i, prox))

    # Período do dia ("de manhã", "in the morning"). Exige preposição antes —
    # senão "boa noite" e "good evening", que abrem literalmente toda ligação,
    # viravam restrição de horário em silêncio.
    for i, tk in enumerate(tokens):
        if tk in idi.periodos and (i == 0 or tokens[i - 1] in idi.preposicoes):
            return (*idi.periodos[tk], False, {i})
    return None, None, False, set()


def _completar_com_periodo(tokens, hora_min, hora_max, consumidos, idi: Idioma = PT):
    """'de manhã, antes das onze' são duas informações, não uma.

    O limite explícito manda na sua ponta; o período preenche a que sobrou.
    Sem isto a restrição saía como "até as 11h" e o agente ofereceria 7h da
    manhã do dia seguinte como se servisse.
    """
    if hora_min and hora_max:
        return hora_min, hora_max
    for i, tk in enumerate(tokens):
        # Um período já usado para desambiguar a hora não vale de novo como faixa.
        if (tk in idi.periodos and i not in consumidos
                and (i == 0 or tokens[i - 1] in idi.preposicoes)):
            inicio, fim = idi.periodos[tk]
            return hora_min or inicio, hora_max or fim
    return hora_min, hora_max


def _proximo_dia(hoje: date, alvo: int, semana_seguinte: bool) -> date:
    delta = (alvo - hoje.weekday()) % 7 or 7
    d = hoje + timedelta(days=delta)
    if semana_seguinte:
        inicio_prox = hoje + timedelta(days=7 - hoje.weekday())
        if d < inicio_prox:
            d += timedelta(days=7)
    return d


def _restricao_temporal(tokens, hoje: date, idi: Idioma = PT):
    texto = " ".join(tokens)
    proxima = any(marca in texto for marca in idi.marcas_proxima)
    mes_que_vem = any(marca in texto for marca in idi.marcas_mes_que_vem)

    # As relativas vão da mais longa para a mais curta: "day after tomorrow"
    # contém "tomorrow", e testar na ordem errada devolve amanhã.
    for frase, offset in sorted(idi.relativas.items(), key=lambda kv: -len(kv[0])):
        if frase in texto:
            d = hoje + timedelta(days=offset)
            return d.isoformat(), d.isoformat(), None
    if any(frase in texto for frase in idi.semana_que_vem):
        inicio = hoje + timedelta(days=7 - hoje.weekday())
        return inicio.isoformat(), (inicio + timedelta(days=6)).isoformat(), None

    # "dia quinze", "quinze de outubro", "October fifteenth", "the 15th of May"
    for i, tk in enumerate(tokens):
        numero = mes = None
        if tk in idi.marcador_dia:
            numero, prox = _ler_numero(tokens, i + 1, idi)
            if numero is not None:
                j = prox
                while j < len(tokens) and tokens[j] in idi.prep_data:
                    j += 1
                if j < len(tokens) and tokens[j] in idi.meses:
                    mes = idi.meses[tokens[j]]
        elif tk in idi.meses:
            mes = idi.meses[tk]
            for recuo in (1, 2):          # "quinze outubro" e "quinze de outubro"
                if i - recuo >= 0:
                    numero, _ = _ler_numero(tokens, i - recuo, idi)
                    if numero is not None:
                        break
            if numero is None and idi.dia_apos_mes:
                # "October fifteenth": em inglês o dia vem depois do mês.
                numero, _ = _ler_numero(tokens, i + 1, idi)
        if numero is None or not 1 <= numero <= 31:
            continue
        ano, mes_alvo = hoje.year, mes
        if mes_alvo is None:
            mes_alvo = hoje.month + 1 if (mes_que_vem or numero <= hoje.day) else hoje.month
        if mes_alvo > 12:
            mes_alvo, ano = mes_alvo - 12, ano + 1
        elif mes_alvo < hoje.month:
            ano += 1
        try:
            d = date(ano, mes_alvo, numero)
        except ValueError:
            continue
        return d.isoformat(), d.isoformat(), None

    dias = tuple(sorted({idi.dias_semana[t] for t in tokens if t in idi.dias_semana}))
    if not dias:
        return None, None, None
    if proxima and len(dias) == 1:
        d = _proximo_dia(hoje, dias[0], semana_seguinte=True)
        return d.isoformat(), d.isoformat(), None
    return None, None, dias


def interpretar_restricao(texto: str, hoje: date | None = None,
                          idi: Idioma = PT) -> Restricao:
    """Converte a fala do paciente em filtros para `consultar_agenda`."""
    hoje = hoje or date.today()
    tokens = tokenizar(texto)
    hora_min, hora_max, ambigua, consumidos = _restricao_horaria(tokens, idi)
    hora_min, hora_max = _completar_com_periodo(tokens, hora_min, hora_max,
                                                consumidos, idi)
    if idi.marcas_passado & set(tokens):
        # "nasci em quinze de março de oitenta" traz uma data que não é um
        # pedido de agenda — e o extrator de datas empurrava esse 15 de março
        # para o futuro, filtrando a agenda inteira pelo aniversário da pessoa.
        # Ninguém marca consulta dizendo quando nasceu.
        data_inicio = data_fim = dias = None
    else:
        data_inicio, data_fim, dias = _restricao_temporal(tokens, hoje, idi)

    if hora_min and hora_max and hora_min > hora_max:
        # Não deveria acontecer — há um teste de propriedade contra isso. Se
        # acontecer, largar o limite derivado de período é melhor do que
        # entregar ao validador uma restrição com zero horários por definição.
        hora_max, ambigua = None, True

    return Restricao(hora_min, hora_max, dias, data_inicio, data_fim, ambigua,
                     descrever_restricao(hora_min, hora_max, dias,
                                         data_inicio, data_fim, idi))


def descrever_restricao(hora_min, hora_max, dias, data_inicio, data_fim,
                        idi: Idioma = PT) -> str:
    """Frase do que foi entendido, na língua da ligação. O agente lê isto de
    volta em voz alta quando a interpretação foi um chute."""
    en = idi.codigo == "en"
    partes = []
    if hora_min and hora_max and hora_min == hora_max:
        partes.append(f"at {hora_min}" if en else f"às {hora_min}")
    else:
        if hora_min:
            partes.append(f"from {hora_min}" if en else f"a partir das {hora_min}")
        if hora_max:
            partes.append(f"until {hora_max}" if en else f"até as {hora_max}")
    if dias:
        nomes = [idi.nomes_dia[d] for d in dias]
        partes.append((" or " if en else " ou ").join(nomes))
    if data_inicio and data_fim and data_inicio == data_fim:
        partes.append(f"on {data_inicio}" if en else f"em {data_inicio}")
    elif data_inicio:
        partes.append(f"between {data_inicio} and {data_fim}" if en
                      else f"entre {data_inicio} e {data_fim}")
    return ", ".join(partes)


def mesclar_restricoes(atual: Restricao, nova: Restricao,
                       idi: Idioma = PT) -> Restricao:
    """Combina o que o paciente já disse com o que acabou de dizer.

    Por grupo, não por campo: quem menciona horário substitui o horário
    inteiro. É assim que "só depois das 18h... na verdade, de manhã" acaba
    em manhã, e não numa restrição impossível costurada das duas.
    """
    if nova.vazia():
        return atual
    tem_hora = bool(nova.hora_min or nova.hora_max)
    hora_min = nova.hora_min if tem_hora else atual.hora_min
    hora_max = nova.hora_max if tem_hora else atual.hora_max
    dias = nova.dias_semana or atual.dias_semana
    tem_data = bool(nova.data_inicio or nova.data_fim)
    data_inicio = nova.data_inicio if tem_data else atual.data_inicio
    data_fim = nova.data_fim if tem_data else atual.data_fim
    return Restricao(
        hora_min, hora_max, dias, data_inicio, data_fim,
        nova.ambigua if tem_hora else atual.ambigua,
        descrever_restricao(hora_min, hora_max, dias, data_inicio, data_fim, idi))


IDADE_MAXIMA = 120


def normalizar_nascimento(texto: str, hoje: date | None = None,
                          idi: Idioma = PT) -> str | None:
    """Data de nascimento falada → AAAA-MM-DD, ou None se não der para ler.

    Aceita o que as pessoas realmente dizem: "quinze de março de oitenta",
    "15/03/1980", "quinze do três de mil novecentos e oitenta". Devolver None
    é resposta legítima — é melhor o agente pedir de novo do que gravar uma
    data inventada na ficha de alguém.
    """
    hoje = hoje or date.today()
    tokens = tokenizar(texto)
    if not tokens:
        return None

    numeros, mes_nomeado = [], None
    i = 0
    while i < len(tokens):
        if tokens[i] in idi.meses:
            mes_nomeado = idi.meses[tokens[i]]
            i += 1
            continue
        valor, prox = _ler_numero(tokens, i, idi)
        if valor is None:
            i += 1
            continue
        if valor == 0:
            # "zero três de doze" — o zero é o zero à esquerda que a pessoa
            # fala, não um número. Solto ele nunca é dia, mês nem ano.
            i = prox
            continue
        # Ano falado vem em pedaços: "mil novecentos e oitenta" é 1000+900+80,
        # "dois mil e cinco" é 2*1000+5. Junta na ordem em que se fala.
        if numeros and valor == 1000 and 1 <= numeros[-1] <= 9:
            numeros[-1] *= 1000
        elif (numeros and numeros[-1] >= 1000 and valor < 1000
              and numeros[-1] % 100 == 0):
            # Só absorve enquanto o ano está incompleto: 1900 aceita o "oitenta"
            # e vira 1980; 2005 já está fechado, então o "dez" seguinte é o dia.
            numeros[-1] += valor
        elif numeros and 100 <= numeros[-1] < 1000 and valor < 100:
            numeros[-1] += valor
        else:
            numeros.append(valor)
        i = prox

    if mes_nomeado is not None:
        # Por posição, nunca por valor: filtrar "o número diferente do mês"
        # apagava o dia em "primeiro de janeiro", onde dia e mês são 1.
        dia = ano = None
        for n in numeros:
            if ano is None and n > 31:
                ano = n
            elif dia is None and 1 <= n <= 31:
                dia = n
    elif len(numeros) >= 3 and numeros[0] > 31:
        # ISO — "1980-03-15". É o formato que o modelo emite quando preenche um
        # campo de data, e recusá-lo derrubava o cadastro por R11 com todos os
        # dados corretos na mão.
        ano, mes_nomeado, dia = numeros[0], numeros[1], numeros[2]
    elif len(numeros) >= 3:
        dia, mes_nomeado, ano = numeros[0], numeros[1], numeros[2]
    else:
        return None
    if dia is None or mes_nomeado is None or ano is None:
        return None

    if ano < 100:      # "oitenta" é 1980, "cinco" é 2005 — nunca o futuro
        ano += 2000 if ano <= hoje.year % 100 else 1900
    try:
        nascimento = date(int(ano), int(mes_nomeado), int(dia))
    except ValueError:
        return None
    if not (hoje.replace(year=hoje.year - IDADE_MAXIMA) < nascimento < hoje):
        return None
    return nascimento.isoformat()


GRUPOS = {"cpf": (3, 3, 3, 2), "telefone": (2, 5, 4)}


def ler_digitos(digitos: str, formato: str | None = None) -> str:
    """Separa os dígitos para o agente ler de volta, um a um, em voz alta.

    A metade que faltava do §3.3: o normalizador acerta muito, mas não acerta
    sempre. Confirmação dígito-a-dígito é o que transforma "acertou quase
    sempre" em "errou e o paciente corrigiu" — que é um resultado aceitável,
    ao contrário de agendar para o CPF de outra pessoa.
    """
    d = "".join(c for c in digitos if c.isdigit())
    grupos = GRUPOS.get(formato or "")
    if not grupos or sum(grupos) != len(d):
        grupos = (3,) * (len(d) // 3) + ((len(d) % 3,) if len(d) % 3 else ())
    saida, i = [], 0
    for g in grupos:
        saida.append(" ".join(d[i:i + g]))
        i += g
    return ", ".join(p for p in saida if p)


def casar_especialidade(falado: str, catalogo: list[str],
                        idi: Idioma = PT) -> str | None:
    """'ortopedista' é Ortopedia. O paciente fala a profissão, não a área.

    Descoberto no primeiro smoke test contra um modelo de verdade: ele chamou
    consultar_agenda(especialidade="ortopedista") e a busca exata devolveu
    "não atendemos".

    O apelido por idioma veio depois, da eval em inglês: "orthopedist" não casa
    com "Ortopedia" nem por prefixo — "ortho" contra "ortop". As outras três
    passavam por acidente ("derma", "cardi", "neuro" coincidem nas duas
    línguas), o que é pior do que falhar: a regra parecia funcionar.
    """
    alvo = sem_acento(falado)
    for nome in catalogo:
        if sem_acento(nome) == alvo:
            return nome
    apelido = idi.especialidades.get(alvo)
    if apelido:
        for nome in catalogo:
            if sem_acento(nome) == sem_acento(apelido):
                return nome
    if len(alvo) >= 5:
        for nome in catalogo:
            if sem_acento(nome)[:5] == alvo[:5]:
                return nome
    return None


_MARCADOR = re.compile(r"^\s*(?:[-*•‣]|\d+[.)])\s+", re.MULTILINE)
_ENFASE = re.compile(r"(\*\*|__|\*|_|`+)")
_TITULO = re.compile(r"^\s*#{1,6}\s*", re.MULTILINE)
_ESPACO = re.compile(r"[ \t]+")
# O modelo narra o próprio controle de fluxo dentro da fala: "Está correto?
# (aguardando resposta)". No telefone ninguém ouve parêntese — o TTS lê
# "abre parênteses aguardando resposta" e a ligação fica sem sentido.
_RUBRICA = re.compile(
    r"\s*[(\[][^)\]]*\b(aguard|espera|esperando|pausa|sil[êe]ncio|"
    r"wait|waiting|thinking|nota|obs)\w*\b[^)\]]*[)\]]", re.IGNORECASE)


def limpar_para_voz(texto: str) -> str:
    """Tira do texto o que não se fala.

    O prompt manda não usar lista nem markdown. O modelo usa mesmo assim — e
    o TTS lê "- 18h00" como "hífen dezoito". Pedir por prompt e conferir por
    código é a mesma ideia do validador, aplicada à saída: o modelo propõe o
    texto, o código garante que é falável.
    """
    if not texto:
        return ""
    saida = ""
    texto = _RUBRICA.sub("", texto)
    for linha in _TITULO.sub("", _MARCADOR.sub("", texto)).splitlines():
        linha = _ESPACO.sub(" ", _ENFASE.sub("", linha)).strip()
        if not linha:
            continue
        if not saida:
            saida = linha
        elif saida[-1] in ".!?:;,":
            saida += " " + linha
        elif linha[:1].isupper():
            saida += ". " + linha      # frase nova, não item de lista
        else:
            saida += ", " + linha
    return saida


# --- 3. nomes próprios -------------------------------------------------------

_FONEMAS = [("ph", "f"), ("th", "t"), ("ch", "x"), ("sh", "x"), ("lh", "L"),
            ("nh", "N"), ("qu", "k"), ("ss", "s"), ("sc", "s"), ("rr", "r"),
            ("ç", "s"), ("z", "s"), ("q", "k"), ("c", "k"), ("y", "i"),
            ("h", "")]


def chave_fonetica(nome: str) -> str:
    """Reduz o nome ao som. 'Thaís' e 'Taís' colidem; é essa colisão que serve."""
    s = re.sub(r"[^a-z]", "", sem_acento(nome))
    for de, para in _FONEMAS:
        s = s.replace(de, para)
    s = re.sub(r"(.)\1+", r"\1", s)
    return s


def _variantes(nome: str) -> set[str]:
    base = chave_fonetica(nome)
    # 'W' brasileiro é ora /v/ (Wagner), ora /u/ (Wesley). Guardo as duas leituras.
    return {base, base.replace("w", "v"), base.replace("w", "u")}


def casar_nome(falado: str, candidatos: list[str], corte: float = 0.72,
               limite: int = 3) -> dict:
    """Casa um nome ouvido pelo STT contra o cadastro.

    Combina similaridade literal e fonética — a fonética sozinha erra em nome
    japonês e alemão, a literal sozinha erra em Thaís/Taís. Devolve `ambiguo`
    quando os dois primeiros empatam: aí o certo é perguntar, não escolher.
    """
    alvo_literal = sem_acento(falado)
    alvo_fonetico = _variantes(falado)
    marcados = []
    for cand in candidatos:
        literal = SequenceMatcher(None, alvo_literal, sem_acento(cand)).ratio()
        fonetico = max(SequenceMatcher(None, a, b).ratio()
                       for a in alvo_fonetico for b in _variantes(cand))
        marcados.append({"nome": cand, "score": round(max(literal, fonetico), 3)})
    marcados.sort(key=lambda m: -m["score"])

    acima = [m for m in marcados if m["score"] >= corte][:limite]
    ambiguo = len(acima) > 1 and (acima[0]["score"] - acima[1]["score"]) < 0.05
    return {
        "melhor": acima[0]["nome"] if acima else None,
        "score": acima[0]["score"] if acima else 0.0,
        "ambiguo": ambiguo,
        "candidatos": acima,
    }
