"""As tabelas que mudam com a língua — e só elas.

O normalizador tem um algoritmo só. O que muda entre português e inglês não é
como se lê "depois das seis", é **quais palavras** marcam um piso de horário.
Separar as duas coisas é o que evita duplicar 667 linhas de parser para ganhar
um idioma.

A decisão que vale explicar: o prompt do sistema **não** é traduzido. As regras
de operação ("nunca invente um horário", "leia o CPF de volta") são as mesmas
nos dois idiomas, e manter duas versões garante que elas divirjam na primeira
correção feita só de um lado. O que entra no prompt é uma diretriz de idioma e
as palavras que o agente precisa falar — saudação, dias, meses.

O que **não** dá para deixar em português é a validação. A R8 confere o que o
agente falou em voz alta contra o que ele ia gravar. Medido: uma frase inglesa
correta lida com as tabelas em português devolve `{hora: None, dia_semana:
None, dia_mes: None, mes: None}` — nenhuma entidade. A regra então falha
**fechada**: ela reprova toda ligação em inglês por "o agente não disse o
horário em voz alta", inclusive quando ele disse. Não é um buraco de segurança,
é a impossibilidade de atender em inglês. Por isso `Idioma` atravessa o
normalizador e o validador, e não só o prompt.

O mesmo vale para o consentimento, e ali a colisão é pior: "no" é negação em
inglês e preposição em português. Uma lista só, somando as duas línguas, leria
"pode ser no dia quinze" como recusa.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Idioma:
    codigo: str
    # --- números e correção de ditado ---
    extenso: dict[str, int]
    correcao: set[str]
    # --- calendário ---
    dias_semana: dict[str, int]
    meses: dict[str, int]
    nomes_dia: list[str]                 # como se fala, para o agente ler
    nomes_mes: list[str]
    # --- restrição de horário ---
    marc_min: set[str]                   # "depois das" / "after"
    marc_max: set[str]                   # "antes das" / "before"
    periodos: dict[str, tuple[str, str]]
    ligacoes: set[str]                   # palavras puladas ao ler uma hora
    preposicoes: set[str]                # o que pode preceder um período
    sufixo_hora: set[str]                # "h", "horas" / "oclock"
    prep_periodo: set[str]               # "da tarde" / "in the afternoon"
    marcador_exato: set[str]             # "às" / "at"
    entre: str
    entre_conect: set[str]
    meio_dia: tuple[str, ...]
    meia_noite: tuple[str, ...]
    conector_minuto: set[str]            # "seis E meia"; vazio em inglês
    minuto_sem_conector: bool            # "six thirty" — só faz sentido em inglês
    meia_hora: set[str]                  # "meia" / "thirty"
    prefixo_meia_hora: tuple[str, ...]   # ("half","past") em inglês
    sufixo_am_pm: dict[str, str]         # {"pm": "tarde"} — inexistente em pt
    # --- datas relativas ---
    relativas: dict[str, int]            # "amanhã" → +1 dia
    semana_que_vem: tuple[str, ...]
    marcas_proxima: tuple[str, ...]      # "que vem" / "next"
    marcas_mes_que_vem: tuple[str, ...]
    marcador_dia: set[str]               # "dia 15" / "the 15th"
    prep_data: set[str]                  # "quinze DE outubro" / "fifteenth OF October"
    dia_apos_mes: bool                   # "October fifteenth" — só em inglês
    # --- catálogo falado ---
    # O paciente fala a profissão, não a área — e em inglês nem a raiz bate:
    # "orthopedist" contra "Ortopedia" não casa nem por prefixo.
    especialidades: dict[str, str]
    # --- consentimento ---
    afirmativos: set[str]
    negativos: set[str]
    # --- outros ---
    marcas_passado: set[str]             # "nasci" / "born" — não é pedido de agenda
    saudacoes: tuple[str, str, str]      # manhã, tarde, noite
    voz_tts: str                         # voz do `say` no macOS
    codigo_stt: str                      # idioma passado ao Whisper
    diretriz: str                        # a linha que entra no prompt do sistema
    rotulo: str                          # nome do idioma, para trace e painel

    def descrever(self, quando) -> str:
        """Data e hora ditas em voz alta — a âncora que a R8 compara."""
        if self.codigo == "en":
            h = quando.hour % 12 or 12
            sufixo = "am" if quando.hour < 12 else "pm"
            hora = f"{h} {sufixo}" if quando.minute == 0 else f"{h}:{quando.minute:02d} {sufixo}"
            return (f"{self.nomes_dia[quando.weekday()]}, "
                    f"{self.nomes_mes[quando.month - 1]} {quando.day}, at {hora}")
        hora = (f"{quando.hour}h" if quando.minute == 0
                else f"{quando.hour}h{quando.minute:02d}")
        return (f"{self.nomes_dia[quando.weekday()]}, {quando.day} de "
                f"{self.nomes_mes[quando.month - 1]}, às {hora}")

    def saudacao(self, quando) -> str:
        h = quando.hour
        return self.saudacoes[0] if 5 <= h < 12 else \
               self.saudacoes[1] if 12 <= h < 18 else self.saudacoes[2]


# --- português do Brasil -----------------------------------------------------

_EXTENSO_PT = {
    "zero": 0, "um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "quatro": 4,
    "cinco": 5, "seis": 6, "sete": 7, "oito": 8, "nove": 9, "dez": 10,
    "onze": 11, "doze": 12, "treze": 13, "quatorze": 14, "catorze": 14,
    "quinze": 15, "dezesseis": 16, "dezasseis": 16, "dezessete": 17,
    "dezoito": 18, "dezenove": 19, "vinte": 20, "trinta": 30, "quarenta": 40,
    "cinquenta": 50, "sessenta": 60, "setenta": 70, "oitenta": 80,
    "noventa": 90, "cem": 100, "cento": 100,
    # Centenas e milhar: só aparecem em ano de nascimento, mas sem elas
    # "mil novecentos e oitenta" só dava 1980 por acidente — "mil" e
    # "novecentos" eram ignorados e sobrava o "oitenta".
    "duzentos": 200, "trezentos": 300, "quatrocentos": 400,
    "quinhentos": 500, "seiscentos": 600, "setecentos": 700,
    "oitocentos": 800, "novecentos": 900, "mil": 1000,
    "primeiro": 1,          # "primeiro de janeiro"
}

PT = Idioma(
    codigo="pt",
    extenso=_EXTENSO_PT,
    # "meia" fica de fora do dicionário de propósito: em ditado de dígitos vale
    # 6, em expressão de hora vale 30 minutos. São dois modos, não um.
    correcao={"nao", "desculpa", "desculpe", "perdao", "opa", "ops", "errei",
              "corrige", "corrigindo", "alias", "menti"},
    dias_semana={"segunda": 0, "terca": 1, "quarta": 2, "quinta": 3,
                 "sexta": 4, "sabado": 5, "domingo": 6},
    meses={"janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5,
           "junho": 6, "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10,
           "novembro": 11, "dezembro": 12},
    nomes_dia=["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
               "sexta-feira", "sábado", "domingo"],
    nomes_mes=["janeiro", "fevereiro", "março", "abril", "maio", "junho",
               "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"],
    marc_min={"depois", "partir", "apos"},
    marc_max={"antes", "ate"},
    periodos={"manha": ("06:00", "11:59"), "tarde": ("12:00", "17:59"),
              "noite": ("18:00", "21:00")},
    ligacoes={"das", "de", "do", "da", "a", "as", "o", "aos", "na", "no",
              "pela", "pelo", "hora", "horas",
              # O STT troca palavra de ligação por parecida: "depois DAR seis"
              # em vez de "depois DAS seis". Achado na suíte de áudio — em
              # texto isso nunca aparece.
              "dar", "der", "dás", "dos", "ao"},
    preposicoes={"de", "da", "do", "pela", "pelo", "a", "ao", "na", "no", "em"},
    sufixo_hora={"h", "hora", "horas"},
    prep_periodo={"da", "do", "de", "pela", "pelo"},
    marcador_exato={"as", "a", "ao", "aos"},
    entre="entre",
    entre_conect={"e", "a", "as", "ate"},
    meio_dia=("meio dia",),
    meia_noite=("meia noite",),
    conector_minuto={"e"},
    minuto_sem_conector=False,
    meia_hora={"meia"},
    prefixo_meia_hora=(),
    sufixo_am_pm={},
    relativas={"depois de amanha": 2, "amanha": 1, "hoje": 0},
    semana_que_vem=("semana que vem", "proxima semana"),
    marcas_proxima=("que vem", "proxima", "proximo"),
    marcas_mes_que_vem=("mes que vem", "proximo mes"),
    marcador_dia={"dia"},
    prep_data={"de", "do", "da"},
    dia_apos_mes=False,
    especialidades={
        "ortopedista": "Ortopedia", "ortopedico": "Ortopedia",
        "dermatologista": "Dermatologia", "dermato": "Dermatologia",
        "cardiologista": "Cardiologia", "cardio": "Cardiologia",
        "neurologista": "Neurologia", "neuro": "Neurologia",
        "clinico geral": "Clínica Geral", "clinico": "Clínica Geral",
    },
    afirmativos={"sim", "isso", "confirmo", "confirma", "confirmado", "perfeito",
                 "otimo", "beleza", "fechado", "exato", "exatamente", "uhum",
                 "aham", "claro", "positivo", "ok", "okay", "ta", "pode",
                 "certo", "combinado", "blz", "vamos", "bora", "show"},
    negativos={"nao", "errado", "errada", "espera", "pera", "perai", "calma",
               "muda", "mudar", "outro", "outra", "prefiro", "cancela",
               "engano", "nada"},
    marcas_passado={"nasci", "nascida", "nascido", "nascimento", "aniversario"},
    saudacoes=("Bom dia", "Boa tarde", "Boa noite"),
    voz_tts="Luciana",
    codigo_stt="pt",
    diretriz=("Fale português do Brasil. Números, datas e horários no formato "
              "falado do Brasil."),
    rotulo="português",
)


# --- inglês ------------------------------------------------------------------

_EXTENSO_EN = {
    "zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    "hundred": 100, "thousand": 1000,
    # Ordinais: em inglês a data se fala assim — "March fifteenth", "the first
    # of June". Sem eles o dia simplesmente não existe na frase.
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11,
    "twelfth": 12, "thirteenth": 13, "fourteenth": 14, "fifteenth": 15,
    "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
    "twentieth": 20, "thirtieth": 30,
}

EN = Idioma(
    codigo="en",
    extenso=_EXTENSO_EN,
    correcao={"no", "sorry", "oops", "wrong", "actually", "scratch", "correction",
              "mistake", "mistyped", "misspoke"},
    dias_semana={"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                 "friday": 4, "saturday": 5, "sunday": 6,
                 "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3,
                 "thurs": 3, "fri": 4, "sat": 5, "sun": 6},
    meses={"january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
           "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
           "november": 11, "december": 12,
           "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
           "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12},
    nomes_dia=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
               "Saturday", "Sunday"],
    nomes_mes=["January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December"],
    # "past" fica fora: "half past six" é hora exata, não piso. Deixá-lo aqui
    # fazia "half past six" virar "depois das seis" e perder os 30 minutos.
    marc_min={"after", "from", "starting"},
    marc_max={"before", "until", "till", "by", "earlier"},
    periodos={"morning": ("06:00", "11:59"), "afternoon": ("12:00", "17:59"),
              "evening": ("18:00", "21:00"), "night": ("18:00", "21:00")},
    ligacoes={"the", "at", "of", "a", "an", "on", "in", "oclock", "clock", "o",
              "around", "about", "day", "hours", "hour"},
    preposicoes={"the", "in", "at", "of", "on", "this", "some"},
    sufixo_hora={"oclock", "clock", "hours"},
    prep_periodo={"in", "the", "of", "at"},
    marcador_exato={"at"},
    entre="between",
    entre_conect={"and", "to"},
    meio_dia=("noon", "midday", "mid day"),
    meia_noite=("midnight", "mid night"),
    conector_minuto=set(),
    minuto_sem_conector=True,
    meia_hora={"thirty", "half"},
    prefixo_meia_hora=("half", "past"),
    sufixo_am_pm={"pm": "afternoon", "am": "morning"},
    relativas={"day after tomorrow": 2, "tomorrow": 1, "today": 0},
    semana_que_vem=("next week",),
    marcas_proxima=("next",),
    marcas_mes_que_vem=("next month",),
    marcador_dia={"the", "on"},
    prep_data={"of", "the"},
    especialidades={
        "orthopedist": "Ortopedia", "orthopaedist": "Ortopedia",
        "orthopedics": "Ortopedia", "orthopaedics": "Ortopedia",
        "orthopedic": "Ortopedia", "bone doctor": "Ortopedia",
        "dermatologist": "Dermatologia", "dermatology": "Dermatologia",
        "skin doctor": "Dermatologia",
        "cardiologist": "Cardiologia", "cardiology": "Cardiologia",
        "heart doctor": "Cardiologia",
        "neurologist": "Neurologia", "neurology": "Neurologia",
        "general practitioner": "Clínica Geral", "gp": "Clínica Geral",
        "family doctor": "Clínica Geral",
    },
    # "October fifteenth" põe o dia depois do mês; "quinze de outubro" põe
    # antes. As duas ordens convivem em inglês ("the fifteenth of October"),
    # então o parser procura dos dois lados.
    dia_apos_mes=True,
    afirmativos={"yes", "yeah", "yep", "yup", "sure", "correct", "right",
                 "ok", "okay", "confirm", "confirmed", "please", "perfect",
                 "great", "exactly", "absolutely", "affirmative", "book",
                 "go", "sounds", "works", "fine", "good"},
    negativos={"no", "nope", "wrong", "incorrect", "wait", "hold", "cancel",
               "change", "different", "another", "prefer", "mistake", "not"},
    marcas_passado={"born", "birth", "birthday", "birthdate"},
    saudacoes=("Good morning", "Good afternoon", "Good evening"),
    # Samantha vem instalada em qualquer macOS; Luciana não fala inglês, e o
    # `say` com voz errada lê inglês com fonética portuguesa.
    voz_tts="Samantha",
    codigo_stt="en",
    diretriz=("Speak English. Say numbers, dates and times the way they are "
              "spoken in English."),
    rotulo="English",
)


IDIOMAS = {"pt": PT, "en": EN}
PADRAO = PT


# --- detecção ----------------------------------------------------------------

# Palavras funcionais: aparecem em quase toda frase da língua e quase nunca na
# outra. Contar conteúdo (nomes de mês, números) seria pior — "March" e "cinco"
# aparecem nos dois lados de uma ligação bilíngue.
_MARCAS = {
    "en": {"i", "the", "a", "to", "for", "with", "my", "is", "im", "id",
           "you", "your", "can", "could", "would", "want", "need", "have",
           "hi", "hello", "please", "thanks", "thank", "appointment", "book",
           "doctor", "morning", "afternoon", "evening", "yes", "no", "and",
           "on", "at", "in", "of", "me", "we", "it", "that", "this", "do",
           "does", "am", "are", "was", "were", "get", "make", "see", "there"},
    "pt": {"eu", "o", "a", "os", "as", "um", "uma", "de", "da", "do", "para",
           "com", "meu", "minha", "e", "voce", "vc", "queria", "quero",
           "gostaria", "preciso", "tenho", "oi", "ola", "bom", "boa", "por",
           "favor", "obrigado", "obrigada", "consulta", "marcar", "agendar",
           "medico", "medica", "manha", "tarde", "noite", "sim", "nao",
           "que", "em", "na", "no", "isso", "aqui", "ai", "ser", "esta"},
}
# "a", "o", "no", "e", "am", "in", "on", "of", "at", "as", "me", "do", "da"
# existem nas duas listas ou colidem entre elas. Contá-las faria o detector
# decidir pela língua errada em frases curtas — que são justamente as do
# começo da ligação, onde a decisão é tomada.
_AMBIGUAS = _MARCAS["en"] & _MARCAS["pt"] | {
    "a", "o", "e", "no", "na", "do", "da", "as", "am", "in", "on", "of",
    "at", "me", "as", "so", "sem", "ate", "to", "ok"}
_SO_EN = _MARCAS["en"] - _AMBIGUAS
_SO_PT = _MARCAS["pt"] - _AMBIGUAS


def detectar(texto: str, atual: Idioma | None = None) -> Idioma:
    """A língua da ligação, pela fala do paciente. Determinístico, não LLM.

    Devolve `atual` no empate — inclusive quando a frase não tem marca nenhuma
    ("sim", "pode ser", "ok"). Trocar de idioma no meio da ligação porque o
    paciente respondeu uma monossílaba é pior do que errar a primeira frase:
    o agente muda de língua sozinho e a pessoa desliga.
    """
    from clinica.normalizador import tokenizar

    tokens = set(tokenizar(texto or ""))
    en, pt = len(tokens & _SO_EN), len(tokens & _SO_PT)
    if en == pt:
        return atual or PADRAO
    return EN if en > pt else PT
