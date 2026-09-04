"""Popula o banco: 4 especialidades, 6 profissionais, 40 pacientes, 3 semanas de agenda.

Determinístico (Random(42)) — rodar duas vezes com a mesma data-base produz o
mesmo banco. Sem isso não existe suíte de eval reproduzível.

Uso:  python3 -m clinica.seed [--data-base AAAA-MM-DD] [--banco caminho.db]
"""
from __future__ import annotations

import argparse
import random
from datetime import date, datetime, timedelta
from pathlib import Path

from clinica import db

# --- catálogo ----------------------------------------------------------------

ESPECIALIDADES = ["Ortopedia", "Dermatologia", "Cardiologia", "Fisioterapia"]

# (nome, registro, especialidade, duração, agenda {dia_semana: (abre, fecha)})
# dia_semana: 0=segunda ... 6=domingo
PROFISSIONAIS = [
    # Wesley não atende à noite. Thaís atende, mas só ter/qui.
    # É essa escassez que faz "ortopedista depois das 18h" ser um problema real.
    ("Dr. Wesley Vasconcelos", "CRM-SP 118432", "Ortopedia", 30,
     {0: ("08:00", "14:00"), 1: ("08:00", "14:00"), 2: ("08:00", "14:00"),
      3: ("08:00", "14:00"), 4: ("08:00", "14:00")}),
    ("Dra. Thaís Bittencourt", "CRM-SP 132907", "Ortopedia", 30,
     {0: ("08:00", "12:00"), 1: ("14:00", "20:00"), 3: ("14:00", "20:00")}),
    ("Dra. Larissa Nakamura", "CRM-SP 145820", "Dermatologia", 30,
     {0: ("09:00", "18:00"), 1: ("09:00", "18:00"), 2: ("09:00", "18:00"),
      3: ("09:00", "18:00"), 4: ("09:00", "18:00"), 5: ("08:00", "12:00")}),
    ("Dr. Otávio Rangel", "CRM-SP 109655", "Cardiologia", 40,
     {0: ("08:00", "17:00"), 2: ("08:00", "17:00"), 4: ("08:00", "17:00")}),
    ("Dra. Marina Sampaio", "CREFITO-3 88214", "Fisioterapia", 50,
     {0: ("07:00", "13:00"), 1: ("07:00", "13:00"), 2: ("07:00", "13:00"),
      3: ("07:00", "13:00"), 4: ("07:00", "13:00")}),
    ("Dr. Caio Peixoto", "CREFITO-3 91077", "Fisioterapia", 50,
     {0: ("13:00", "20:00"), 1: ("13:00", "20:00"), 2: ("13:00", "20:00"),
      3: ("13:00", "20:00"), 4: ("13:00", "20:00")}),
]

ALMOCO = ("12:00", "13:00")   # nenhum slot começa nessa janela
DIAS_DE_AGENDA = 21           # 3 semanas
TAXA_OCUPACAO = 0.30
TAXA_BLOQUEIO = 0.04          # férias, reunião, encaixe manual

# Nomes escolhidos de propósito: Thaís, Wesley, Vasconcelos, Nakamura e
# Kauã são exatamente onde o STT erra. Servem de alvo para o normalizador.
NOMES = [
    "Thaís Vasconcelos", "Wesley Bittencourt", "Larissa Nakamura",
    "Kauã Figueiredo", "Maria Aparecida da Silva", "João Pedro Alencar",
    "Ana Beatriz Rocha", "Rafael Queiroz", "Juliana Meireles",
    "Carlos Eduardo Bastos", "Fernanda Sampaio", "Lucas Andrade",
    "Patrícia Gonçalves", "Rodrigo Tavares", "Camila Xavier",
    "Bruno Assunção", "Letícia Monteiro", "Gustavo Rezende",
    "Vanessa Siqueira", "Márcio D'Ávila", "Renata Albuquerque",
    "Felipe Nogueira", "Aline Cavalcanti", "Diego Marchetti",
    "Sônia Bezerra", "Otávio Paredes", "Débora Antunes",
    "Leandro Vasques", "Priscila Fontoura", "Ricardo Uchôa",
    "Tatiane Moraes", "Vinícius Salgado", "Mônica Ferrari",
    "Anderson Klein", "Bianca Ramalho", "Everton Pacheco",
    "Cristiane Lobo", "Murilo Berteli", "Sabrina Yamada", "Heitor Cruz",
]

DDDS = ["11", "11", "11", "19", "21", "31", "48"]


def _cpf_valido(rnd: random.Random) -> str:
    """CPF com dígitos verificadores corretos — o validador precisa poder recusar."""
    d = [rnd.randint(0, 9) for _ in range(9)]
    for _ in range(2):
        soma = sum(v * p for v, p in zip(d, range(len(d) + 1, 1, -1)))
        resto = 11 - soma % 11
        d.append(0 if resto >= 10 else resto)
    return "".join(map(str, d))


def _horarios(abre: str, fecha: str, duracao: int, dia: date) -> list[datetime]:
    inicio = datetime.combine(dia, datetime.strptime(abre, "%H:%M").time())
    limite = datetime.combine(dia, datetime.strptime(fecha, "%H:%M").time())
    almoco_i = datetime.combine(dia, datetime.strptime(ALMOCO[0], "%H:%M").time())
    almoco_f = datetime.combine(dia, datetime.strptime(ALMOCO[1], "%H:%M").time())
    out, atual = [], inicio
    while atual + timedelta(minutes=duracao) <= limite:
        if not (almoco_i <= atual < almoco_f):
            out.append(atual)
        atual += timedelta(minutes=duracao)
    return out


def semear(conn, data_base: date, rnd: random.Random | None = None) -> dict:
    rnd = rnd or random.Random(42)
    cur = conn.cursor()
    cur.execute("BEGIN")

    for nome in ESPECIALIDADES:
        cur.execute("INSERT INTO especialidades (nome) VALUES (?)", (nome,))
    esp_id = {r["nome"]: r["id"] for r in cur.execute("SELECT id, nome FROM especialidades")}

    for nome, registro, esp, duracao, _agenda in PROFISSIONAIS:
        cur.execute(
            "INSERT INTO profissionais (nome, registro, especialidade_id, duracao_min) "
            "VALUES (?,?,?,?)", (nome, registro, esp_id[esp], duracao))
    prof_id = {r["nome"]: r["id"] for r in cur.execute("SELECT id, nome FROM profissionais")}

    cpfs = set()
    for nome in NOMES:
        cpf = _cpf_valido(rnd)
        while cpf in cpfs:
            cpf = _cpf_valido(rnd)
        cpfs.add(cpf)
        telefone = f"55{rnd.choice(DDDS)}9{rnd.randint(10_000_000, 99_999_999)}"
        nascimento = date(rnd.randint(1950, 2006), rnd.randint(1, 12), rnd.randint(1, 28))
        cur.execute(
            "INSERT INTO pacientes (nome, cpf, telefone, nascimento) VALUES (?,?,?,?)",
            (nome, cpf, telefone, nascimento.isoformat()))
    pacientes = [r["id"] for r in cur.execute("SELECT id FROM pacientes")]

    # --- agenda ---
    total_slots = 0
    for nome, _registro, _esp, duracao, agenda in PROFISSIONAIS:
        pid = prof_id[nome]
        for offset in range(1, DIAS_DE_AGENDA + 1):
            dia = data_base + timedelta(days=offset)
            janela = agenda.get(dia.weekday())
            if not janela:
                continue
            for inicio in _horarios(*janela, duracao, dia):
                fim = inicio + timedelta(minutes=duracao)
                sorte = rnd.random()
                status = ("ocupado" if sorte < TAXA_OCUPACAO
                          else "bloqueado" if sorte < TAXA_OCUPACAO + TAXA_BLOQUEIO
                          else "livre")
                cur.execute(
                    "INSERT INTO slots (profissional_id, inicio, fim, status) VALUES (?,?,?,?)",
                    (pid, inicio.strftime(db.FORMATO), fim.strftime(db.FORMATO), status))
                total_slots += 1

    # Todo slot 'ocupado' ganha um agendamento real — banco sem estado órfão.
    ocupados = [r["id"] for r in cur.execute("SELECT id FROM slots WHERE status='ocupado'")]
    agora = datetime.combine(data_base, datetime.min.time()).strftime(db.FORMATO)
    for n, slot_id in enumerate(ocupados):
        cur.execute(
            "INSERT INTO agendamentos (slot_id, paciente_id, status, motivo, criado_em, "
            "idempotency_key, origem) VALUES (?,?,'confirmado',?,?,?,'seed')",
            (slot_id, rnd.choice(pacientes), "consulta", agora, f"seed-{n:05d}"))

    cur.execute("COMMIT")
    return {
        "especialidades": len(ESPECIALIDADES),
        "profissionais": len(PROFISSIONAIS),
        "pacientes": len(NOMES),
        "slots": total_slots,
        "ocupados": len(ocupados),
        "data_base": data_base.isoformat(),
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Popula o banco da clínica.")
    p.add_argument("--data-base", default=date.today().isoformat(),
                   help="dia a partir do qual a agenda é gerada (AAAA-MM-DD)")
    p.add_argument("--banco", default=str(db.CAMINHO_BANCO))
    args = p.parse_args()

    caminho = Path(args.banco)
    if caminho.exists():
        caminho.unlink()
    for extra in (caminho.with_suffix(caminho.suffix + "-wal"),
                  caminho.with_suffix(caminho.suffix + "-shm")):
        extra.unlink(missing_ok=True)

    conn = db.conectar(caminho)
    db.criar_schema(conn)
    resumo = semear(conn, date.fromisoformat(args.data_base))
    conn.close()

    print(f"banco: {caminho}")
    for chave, valor in resumo.items():
        print(f"  {chave:<16} {valor}")


if __name__ == "__main__":
    main()
