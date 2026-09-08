"""Conexão, schema e helpers de banco.

SQLite por decisão: zero dependência, zero custo, zero Docker. O schema
carrega as regras que não podem depender do prompt — em especial o índice
único parcial em `agendamentos`, que torna double-booking impossível por
construção, não improvável por sorte.
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
CAMINHO_BANCO = RAIZ / "data" / "clinica.db"

FORMATO = "%Y-%m-%d %H:%M"

SCHEMA = """
CREATE TABLE IF NOT EXISTS especialidades (
    id    INTEGER PRIMARY KEY,
    nome  TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS profissionais (
    id                INTEGER PRIMARY KEY,
    nome              TEXT NOT NULL,
    registro          TEXT NOT NULL UNIQUE,
    especialidade_id  INTEGER NOT NULL REFERENCES especialidades(id),
    duracao_min       INTEGER NOT NULL DEFAULT 30,
    ativo             INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS pacientes (
    id          INTEGER PRIMARY KEY,
    nome        TEXT NOT NULL,
    cpf         TEXT UNIQUE,            -- 11 dígitos; NULL em cadastro por voz
    telefone    TEXT NOT NULL,          -- 55DDNNNNNNNNN, sem símbolos
    nascimento  TEXT,                   -- YYYY-MM-DD; opcional
    criado_em   TEXT,                   -- NULL nos semeados; data no cadastro por voz
    origem      TEXT                    -- 'seed' | 'voz'
);
CREATE INDEX IF NOT EXISTS idx_pacientes_telefone ON pacientes(telefone);

CREATE TABLE IF NOT EXISTS slots (
    id               INTEGER PRIMARY KEY,
    profissional_id  INTEGER NOT NULL REFERENCES profissionais(id),
    inicio           TEXT NOT NULL,     -- 'YYYY-MM-DD HH:MM'
    fim              TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('livre','ocupado','bloqueado')),
    UNIQUE (profissional_id, inicio)
);
CREATE INDEX IF NOT EXISTS idx_slots_busca ON slots(status, inicio);

CREATE TABLE IF NOT EXISTS agendamentos (
    id               INTEGER PRIMARY KEY,
    slot_id          INTEGER NOT NULL REFERENCES slots(id),
    paciente_id      INTEGER NOT NULL REFERENCES pacientes(id),
    status           TEXT NOT NULL CHECK (status IN ('confirmado','cancelado')),
    motivo           TEXT,
    criado_em        TEXT NOT NULL,
    idempotency_key  TEXT NOT NULL UNIQUE,
    origem           TEXT NOT NULL DEFAULT 'voz'
);

-- A regra mais importante do sistema mora aqui, não no prompt:
-- um slot só pode ter UM agendamento confirmado. Ponto.
CREATE UNIQUE INDEX IF NOT EXISTS idx_um_confirmado_por_slot
    ON agendamentos(slot_id) WHERE status = 'confirmado';

-- Ligações encerradas. A transcrição aqui já vem mascarada, e tem prazo de
-- validade: ver clinica/retencao.py. Dado pessoal guardado sem prazo definido
-- é dado guardado por descuido.
CREATE TABLE IF NOT EXISTS ligacoes (
    id              TEXT PRIMARY KEY,
    criado_em       TEXT NOT NULL,
    paciente_id     INTEGER REFERENCES pacientes(id),
    agendamento_id  INTEGER REFERENCES agendamentos(id),
    motivo_contato  TEXT,
    transferencia   TEXT,
    turnos          INTEGER NOT NULL DEFAULT 0,
    transcricao     TEXT NOT NULL,
    trace           TEXT
);
CREATE INDEX IF NOT EXISTS idx_ligacoes_data ON ligacoes(criado_em);

CREATE TABLE IF NOT EXISTS transferencias (
    id           INTEGER PRIMARY KEY,
    motivo       TEXT NOT NULL CHECK (motivo IN
                     ('risco_clinico','fora_de_escopo','frustracao',
                      'pedido_do_paciente','falha_tecnica')),
    prioridade   TEXT NOT NULL CHECK (prioridade IN ('alta','normal')),
    paciente_id  INTEGER REFERENCES pacientes(id),
    telefone     TEXT,
    resumo       TEXT NOT NULL,
    criado_em    TEXT NOT NULL
);
"""


def conectar(caminho: Path | str | None = None) -> sqlite3.Connection:
    """Abre conexão com foreign keys ligadas e linhas acessíveis por nome."""
    alvo = ":memory:" if caminho == ":memory:" else Path(caminho or CAMINHO_BANCO)
    if alvo != ":memory:":
        alvo.parent.mkdir(parents=True, exist_ok=True)
        alvo = str(alvo)
    conn = sqlite3.connect(alvo, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL") if alvo != ":memory:" else None
    return conn


# Coluna nova em tabela que já existe não entra por CREATE TABLE IF NOT EXISTS.
# Banco semeado antes do cadastro por voz ficaria sem `criado_em` e a inserção
# quebraria em silêncio — na produção de alguém, não aqui.
MIGRACOES = {"pacientes": {"criado_em": "TEXT", "origem": "TEXT"}}


def criar_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for tabela, colunas in MIGRACOES.items():
        existentes = {r["name"] for r in conn.execute(f"PRAGMA table_info({tabela})")}
        for coluna, tipo in colunas.items():
            if coluna not in existentes:
                conn.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {tipo}")
    _afrouxar_pacientes(conn)


def _afrouxar_pacientes(conn: sqlite3.Connection) -> None:
    """Tira o NOT NULL de `cpf` e `nascimento` num banco que já existe.

    `CREATE TABLE IF NOT EXISTS` não altera tabela, e o SQLite não sabe soltar
    um NOT NULL com ALTER — só reconstruindo. Quem liga de fora não tem CPF, e
    o cadastro por voz passou a pedir só nome e telefone; um banco semeado
    antes disso continuaria recusando a escrita por uma restrição que já não
    é regra."""
    colunas = {r["name"]: r for r in conn.execute("PRAGMA table_info(pacientes)")}
    if not colunas or not (colunas["cpf"]["notnull"] or colunas["nascimento"]["notnull"]):
        return
    conn.executescript("""
        PRAGMA foreign_keys = OFF;
        BEGIN;
        CREATE TABLE pacientes_novo (
            id          INTEGER PRIMARY KEY,
            nome        TEXT NOT NULL,
            cpf         TEXT UNIQUE,
            telefone    TEXT NOT NULL,
            nascimento  TEXT,
            criado_em   TEXT,
            origem      TEXT
        );
        INSERT INTO pacientes_novo (id, nome, cpf, telefone, nascimento, criado_em, origem)
            SELECT id, nome, NULLIF(cpf, ''), telefone, NULLIF(nascimento, ''),
                   criado_em, origem FROM pacientes;
        DROP TABLE pacientes;
        ALTER TABLE pacientes_novo RENAME TO pacientes;
        CREATE INDEX IF NOT EXISTS idx_pacientes_telefone ON pacientes(telefone);
        COMMIT;
        PRAGMA foreign_keys = ON;
    """)


# --- helpers -----------------------------------------------------------------

def sem_acento(texto: str) -> str:
    """Normaliza para comparação: sem acento, minúsculo, sem espaço nas pontas."""
    decomposto = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in decomposto if not unicodedata.combining(c)).strip().lower()


def so_digitos(texto: str) -> str:
    return "".join(c for c in (texto or "") if c.isdigit())


def mascarar_cpf(cpf: str) -> str:
    """LGPD: o LLM nunca precisa do CPF inteiro para conduzir a conversa."""
    d = so_digitos(cpf)
    return f"***.***.{d[6:9]}-{d[9:]}" if len(d) == 11 else "***"


def mascarar_telefone(telefone: str) -> str:
    d = so_digitos(telefone)
    return f"(**) ****-{d[-4:]}" if len(d) >= 4 else "***"


_NUMERO_FALADO = (r"(?:zero|uma?|dois|duas|tr[êe]s|quatro|cinco|seis|meia|sete|oito|"
                  r"nove|dez|onze|doze|treze|qu?atorze|quinze|dezesseis|dezessete|"
                  r"dezoito|dezenove|vinte|trinta|quarenta|cinquenta|sessenta|"
                  r"setenta|oitenta|noventa|e|n[ãa]o|\d+)")
# \b em cada ponta: sem isso o "e" da lista casava dentro de "está", e a
# máscara comia meia palavra junto com os dígitos.
_SEQUENCIA = re.compile(rf"\b{_NUMERO_FALADO}\b(?:[\s,.\-]+\b{_NUMERO_FALADO}\b)*",
                        re.IGNORECASE)
MASCARA = "«dígitos omitidos»"


def mascarar_falado(texto: str, minimo: int = 8) -> str:
    """Apaga CPF e telefone **ditados** de dentro de uma transcrição.

    Mascarar o campo do banco não basta: o número aparece cru na transcrição,
    que é justamente o que vai para o trace, para o painel e para o log. Esta
    função acha sequências de números — em dígito ou por extenso — e some com
    as que tiverem tamanho de documento.
    """
    from clinica.normalizador import extrair_digitos

    def trocar(m):
        trecho = m.group(0)
        return MASCARA if len(extrair_digitos(trecho)) >= minimo else trecho

    return _SEQUENCIA.sub(trocar, texto or "")


DIAS = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
        "sexta-feira", "sábado", "domingo"]
MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
         "agosto", "setembro", "outubro", "novembro", "dezembro"]


def descrever(quando: datetime, idi=None) -> str:
    """Texto pronto para TTS — e âncora para medir alucinação de entidade.

    Na língua da ligação: é esta string que o agente lê de volta em voz alta e
    que a R8 confere. Entregá-la em português numa ligação em inglês obriga o
    modelo a traduzir, e é a tradução — não o dado — que a R8 passa a validar.
    """
    from clinica.idioma import PT
    return (idi or PT).descrever(quando)


def saudacao(quando: datetime, idi=None) -> str:
    """Bom dia, boa tarde ou boa noite — pela hora, não por chute.

    Estava fixo em "boa noite" no código do servidor, e o prompt do modelo
    nunca dizia que horas eram. Às 16h a clínica dava boa noite.
    """
    from clinica.idioma import PT
    return (idi or PT).saudacao(quando)


def parse(momento: str) -> datetime:
    return datetime.strptime(momento, FORMATO)
