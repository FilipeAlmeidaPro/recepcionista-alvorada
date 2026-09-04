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

_TOKEN = re.compile(r"\d+|[a-z]+")

_EXTENSO = {
    "zero": 0, "um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "quatro": 4,
    "cinco": 5, "seis": 6, "sete": 7, "oito": 8, "nove": 9, "dez": 10,
    "onze": 11, "doze": 12, "treze": 13, "quatorze": 14, "catorze": 14,
    "quinze": 15, "dezesseis": 16, "dezasseis": 16, "dezessete": 17,
    "dezoito": 18, "dezenove": 19, "vinte": 20, "trinta": 30, "quarenta": 40,
    "cinquenta": 50, "sessenta": 60, "setenta": 70, "oitenta": 80,
    "noventa": 90, "cem": 100, "cento": 100,
}
_COMPOSTOS = {20, 30, 40, 50, 60, 70, 80, 90, 100}

# "meia" fica de fora do dicionário de propósito: em ditado de dígitos vale 6,
# em expressão de hora vale 30 minutos. São dois modos, não um.
_CORRECAO = {"nao", "desculpa", "desculpe", "perdao", "opa", "ops", "errei",
             "corrige", "corrigindo", "alias", "menti"}

DIAS_SEMANA = {"segunda": 0, "terca": 1, "quarta": 2, "quinta": 3,
               "sexta": 4, "sabado": 5, "domingo": 6}
MESES = {"janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5,
         "junho": 6, "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10,
         "novembro": 11, "dezembro": 12}

_MARC_MIN = {"depois", "partir", "apos"}
_MARC_MAX = {"antes", "ate"}
_PERIODOS = {"manha": ("06:00", "11:59"), "tarde": ("12:00", "17:59"),
             "noite": ("18:00", "21:00")}
_LIGACOES = {"das", "de", "do", "da", "a", "as", "o", "aos", "na", "no",
             "pela", "pelo", "hora", "horas",
             # O STT troca palavra de ligação por parecida: "depois DAR seis"
             # em vez de "depois DAS seis". Achado na suíte de áudio — em texto
             # isso nunca aparece.
             "dar", "der", "dás", "dos", "ao"}
_PREPOSICOES = {"de", "da", "do", "pela", "pelo", "a", "ao", "na", "no", "em"}


def tokenizar(texto: str) -> list[str]:
    return _TOKEN.findall(sem_acento(texto))


def _ler_numero(tokens: list[str], i: int) -> tuple[int | None, int]:
    """Lê um número em dígito ou por extenso, inclusive composto ('vinte e três')."""
    if i >= len(tokens):
        return None, i
    tk = tokens[i]
    if tk.isdigit():
        return int(tk), i + 1
    if tk not in _EXTENSO:
        return None, i
    valor = _EXTENSO[tk]
    prox = i + 1
    if valor in _COMPOSTOS and prox + 1 < len(tokens) and tokens[prox] == "e":
        parte, depois = _ler_numero(tokens, prox + 1)
        if parte is not None and parte < valor:
            return valor + parte, depois
    return valor, prox


# --- 1. dígitos ditados ------------------------------------------------------

def extrair_digitos(texto: str) -> str:
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
        if tk in _CORRECAO:
            if grupos:
                grupos.pop()
            i += 1
        elif tk == "meia":
            grupos.append("6")
            i += 1
        else:
            valor, prox = _ler_numero(tokens, i)
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


def _ler_hora(tokens, i, permitir_minutos=True) -> tuple[tuple[int, int] | None, int, bool]:
    while i < len(tokens) and tokens[i] in _LIGACOES:
        i += 1
    if i >= len(tokens):
        return None, i, False
    if tokens[i] == "meio" and i + 1 < len(tokens) and tokens[i + 1] == "dia":
        return (12, 0), i + 2, False
    if tokens[i] == "meia" and i + 1 < len(tokens) and tokens[i + 1] == "noite":
        return (0, 0), i + 2, False

    valor, prox = _ler_numero(tokens, i)
    if valor is None or valor > 23:
        return None, i, False
    hora, minuto = valor, 0

    if prox < len(tokens) and tokens[prox] in {"h", "hora", "horas"}:
        prox += 1
    if permitir_minutos and prox < len(tokens):
        if tokens[prox] == "e":
            if prox + 1 < len(tokens) and tokens[prox + 1] == "meia":
                minuto, prox = 30, prox + 2
            else:
                mv, mprox = _ler_numero(tokens, prox + 1)
                if mv is not None and mv < 60:
                    minuto, prox = mv, mprox
        elif tokens[prox].isdigit() and len(tokens[prox]) == 2 and int(tokens[prox]) < 60:
            minuto, prox = int(tokens[prox]), prox + 1

    j = prox
    while j < len(tokens) and tokens[j] in {"da", "do", "de", "pela", "pelo"}:
        j += 1
    periodo = tokens[j] if j < len(tokens) and tokens[j] in _PERIODOS else None
    if periodo:
        prox = j + 1

    ambiguo = False
    if periodo in ("tarde", "noite") and hora < 12:
        hora += 12
    elif periodo is None and 1 <= hora <= 6:
        # "depois das seis" em clínica é 18h. É um chute — bom, mas chute.
        # Marcar como ambíguo obriga o agente a confirmar em voz alta.
        hora, ambiguo = hora + 12, True
    return (hora, minuto), prox, ambiguo


def _hhmm(h: int, m: int) -> str:
    return f"{h:02d}:{m:02d}"


def _restricao_horaria(tokens) -> tuple[str | None, str | None, bool]:
    texto = " ".join(tokens)
    hora_min = hora_max = None
    ambigua = False
    i = 0
    while i < len(tokens):
        tk = tokens[i]
        if tk == "entre":
            a, prox, amb_a = _ler_hora(tokens, i + 1, permitir_minutos=False)
            if a:
                while prox < len(tokens) and tokens[prox] in {"e", "a", "as", "ate"}:
                    prox += 1
                b, prox, amb_b = _ler_hora(tokens, prox, permitir_minutos=False)
                if b:
                    hora_min, hora_max = _hhmm(*a), _hhmm(*b)
                    # "entre duas e quatro DA TARDE": o período fecha as duas
                    # pontas, então só é ambíguo quando nenhuma delas tem período.
                    ambigua = ambigua or (amb_a and amb_b)
                    i = prox
                    continue
        elif tk in _MARC_MIN:
            h, prox, amb = _ler_hora(tokens, i + 1)
            if h:
                hora_min, ambigua, i = _hhmm(*h), ambigua or amb, prox
                continue
        elif tk in _MARC_MAX:
            h, prox, amb = _ler_hora(tokens, i + 1)
            if h:
                hora_max, ambigua, i = _hhmm(*h), ambigua or amb, prox
                continue
        i += 1
    if hora_min or hora_max:
        return hora_min, hora_max, ambigua

    # Sem marcador de faixa: horário exato ("às oito da manhã", "ao meio-dia").
    if "meio dia" in texto:
        return "12:00", "12:00", False
    if "meia noite" in texto:
        return "00:00", "00:00", False
    for i, tk in enumerate(tokens):
        if tk in {"as", "a", "ao", "aos"}:
            h, _prox, amb = _ler_hora(tokens, i + 1)
            if h:
                return _hhmm(*h), _hhmm(*h), amb

    # Período do dia ("de manhã", "só à noite"). Exige preposição antes —
    # senão "boa noite" e "boa tarde", que abrem literalmente toda ligação,
    # viravam restrição de horário em silêncio.
    for i, tk in enumerate(tokens):
        if tk in _PERIODOS and (i == 0 or tokens[i - 1] in _PREPOSICOES):
            return (*_PERIODOS[tk], False)
    return None, None, False


def _completar_com_periodo(tokens, hora_min, hora_max):
    """'de manhã, antes das onze' são duas informações, não uma.

    O limite explícito manda na sua ponta; o período preenche a que sobrou.
    Sem isto a restrição saía como "até as 11h" e o agente ofereceria 7h da
    manhã do dia seguinte como se servisse.
    """
    if hora_min and hora_max:
        return hora_min, hora_max
    for i, tk in enumerate(tokens):
        if tk in _PERIODOS and (i == 0 or tokens[i - 1] in _PREPOSICOES):
            inicio, fim = _PERIODOS[tk]
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


def _restricao_temporal(tokens, hoje: date):
    texto = " ".join(tokens)
    proxima = ("que vem" in texto) or ("proxima" in texto) or ("proximo" in texto)
    mes_que_vem = "mes que vem" in texto or "proximo mes" in texto

    if "depois de amanha" in texto:
        d = hoje + timedelta(days=2)
        return d.isoformat(), d.isoformat(), None
    if "amanha" in texto:
        d = hoje + timedelta(days=1)
        return d.isoformat(), d.isoformat(), None
    if "hoje" in texto:
        return hoje.isoformat(), hoje.isoformat(), None
    if "semana que vem" in texto or "proxima semana" in texto:
        inicio = hoje + timedelta(days=7 - hoje.weekday())
        return inicio.isoformat(), (inicio + timedelta(days=6)).isoformat(), None

    # "dia quinze", "dia 15 do mês que vem", "quinze de outubro"
    for i, tk in enumerate(tokens):
        numero = mes = None
        if tk == "dia":
            numero, prox = _ler_numero(tokens, i + 1)
            if numero is not None:
                j = prox
                while j < len(tokens) and tokens[j] in {"de", "do", "da"}:
                    j += 1
                if j < len(tokens) and tokens[j] in MESES:
                    mes = MESES[tokens[j]]
        elif tk in MESES:
            mes = MESES[tk]
            for recuo in (1, 2):          # "quinze outubro" e "quinze de outubro"
                if i - recuo >= 0:
                    numero, _ = _ler_numero(tokens, i - recuo)
                    if numero is not None:
                        break
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

    dias = tuple(sorted({DIAS_SEMANA[t] for t in tokens if t in DIAS_SEMANA}))
    if not dias:
        return None, None, None
    if proxima and len(dias) == 1:
        d = _proximo_dia(hoje, dias[0], semana_seguinte=True)
        return d.isoformat(), d.isoformat(), None
    return None, None, dias


_NOMES_DIA = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]


def interpretar_restricao(texto: str, hoje: date | None = None) -> Restricao:
    """Converte a fala do paciente em filtros para `consultar_agenda`."""
    hoje = hoje or date.today()
    tokens = tokenizar(texto)
    hora_min, hora_max, ambigua = _restricao_horaria(tokens)
    hora_min, hora_max = _completar_com_periodo(tokens, hora_min, hora_max)
    data_inicio, data_fim, dias = _restricao_temporal(tokens, hoje)

    return Restricao(hora_min, hora_max, dias, data_inicio, data_fim, ambigua,
                     descrever_restricao(hora_min, hora_max, dias,
                                         data_inicio, data_fim))


def descrever_restricao(hora_min, hora_max, dias, data_inicio, data_fim) -> str:
    """Frase PT-BR do que foi entendido. O agente lê isto de volta em voz alta
    quando a interpretação foi um chute."""
    partes = []
    if hora_min and hora_max and hora_min == hora_max:
        partes.append(f"às {hora_min}")
    else:
        if hora_min:
            partes.append(f"a partir das {hora_min}")
        if hora_max:
            partes.append(f"até as {hora_max}")
    if dias:
        partes.append(" ou ".join(_NOMES_DIA[d] for d in dias))
    if data_inicio and data_fim and data_inicio == data_fim:
        partes.append(f"em {data_inicio}")
    elif data_inicio:
        partes.append(f"entre {data_inicio} e {data_fim}")
    return ", ".join(partes)


def mesclar_restricoes(atual: Restricao, nova: Restricao) -> Restricao:
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
        descrever_restricao(hora_min, hora_max, dias, data_inicio, data_fim))


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


def casar_especialidade(falado: str, catalogo: list[str]) -> str | None:
    """'ortopedista' é Ortopedia. O paciente fala a profissão, não a área.

    Descoberto no primeiro smoke test contra um modelo de verdade: ele chamou
    consultar_agenda(especialidade="ortopedista") e a busca exata devolveu
    "não atendemos".
    """
    alvo = sem_acento(falado)
    for nome in catalogo:
        if sem_acento(nome) == alvo:
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
