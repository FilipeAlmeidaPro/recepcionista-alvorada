"""Retenção de transcrição. O item da LGPD que costuma virar slide e não código.

Gravar a ligação é fácil. O que dá trabalho é **apagar**, e é justamente o que
a lei cobra: dado pessoal guardado sem prazo definido é dado guardado por
descuido. Aqui o prazo é código, com teste, não uma promessa no README.

Duas camadas, com prazos diferentes de propósito:

* **Transcrição** — o texto da conversa. É onde mora o dado sensível de saúde,
  mesmo mascarado: "dor no peito" identifica mais do que um CPF. Prazo curto.
* **Metadados** — quando ligou, se agendou, quanto demorou, se transferiu e
  por quê. Não tem conteúdo de saúde e é o que sustenta a operação: taxa de
  conclusão, motivo de contato, custo. Prazo longo.

Apagar a transcrição e manter o metadado é o desenho certo: some o que
identifica, fica o que gerencia. O agendamento em si é outro registro, com
outra base legal, e não é tocado por nada aqui.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from clinica import db

DIAS_TRANSCRICAO = 90
DIAS_METADADOS = 730          # dois anos de estatística operacional
MARCA_PURGADA = "[transcrição removida por política de retenção]"


def politica() -> dict:
    """A política em uma estrutura, para o painel e a auditoria lerem."""
    return {
        "transcricao_dias": DIAS_TRANSCRICAO,
        "metadados_dias": DIAS_METADADOS,
        "mascaramento": "CPF e telefone removidos da transcrição na gravação, "
                        "não na leitura",
        "base": "a transcrição some; o metadado operacional fica; o "
                "agendamento tem registro e base legal próprios",
    }


def registrar(conn, resultado: dict, *, agora: datetime | None = None) -> str:
    """Guarda a ligação encerrada. A transcrição já chega mascarada do agente."""
    momento = (agora or datetime.now()).strftime(db.FORMATO)
    conn.execute(
        "INSERT OR REPLACE INTO ligacoes (id, criado_em, paciente_id, "
        "agendamento_id, motivo_contato, transferencia, turnos, transcricao, trace) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (resultado["ligacao_id"], momento, resultado.get("paciente_id"),
         resultado.get("agendamento_id"), resultado.get("motivo_contato"),
         resultado.get("motivo_transferencia"), resultado.get("turnos", 0),
         json.dumps(resultado.get("transcricao", []), ensure_ascii=False),
         json.dumps({"bloqueios": resultado.get("bloqueios", []),
                     "ferramentas": resultado.get("ferramentas", []),
                     "latencias_ms": resultado.get("latencias_ms", [])},
                    ensure_ascii=False)))
    return resultado["ligacao_id"]


def purgar(conn, *, dias_transcricao: int = DIAS_TRANSCRICAO,
           dias_metadados: int = DIAS_METADADOS,
           agora: datetime | None = None) -> dict:
    """Aplica a política. Devolve o que apagou, para o log de auditoria.

    Idempotente: rodar duas vezes no mesmo dia não apaga nada na segunda.
    """
    momento = agora or datetime.now()
    corte_texto = (momento - timedelta(days=dias_transcricao)).strftime(db.FORMATO)
    corte_tudo = (momento - timedelta(days=dias_metadados)).strftime(db.FORMATO)

    conn.execute("BEGIN IMMEDIATE")
    transcricoes = conn.execute(
        "UPDATE ligacoes SET transcricao = ?, trace = NULL "
        "WHERE criado_em < ? AND transcricao != ?",
        (json.dumps([{"papel": "sistema", "texto": MARCA_PURGADA}],
                    ensure_ascii=False), corte_texto,
         json.dumps([{"papel": "sistema", "texto": MARCA_PURGADA}],
                    ensure_ascii=False))).rowcount
    ligacoes = conn.execute("DELETE FROM ligacoes WHERE criado_em < ?",
                            (corte_tudo,)).rowcount
    conn.execute("COMMIT")
    return {"transcricoes_removidas": transcricoes, "ligacoes_removidas": ligacoes,
            "corte_transcricao": corte_texto, "corte_metadados": corte_tudo}


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(
        description="Aplica a política de retenção de transcrições.")
    p.add_argument("--dias-transcricao", type=int, default=DIAS_TRANSCRICAO)
    p.add_argument("--dias-metadados", type=int, default=DIAS_METADADOS)
    p.add_argument("--banco", default=None)
    p.add_argument("--simular", action="store_true",
                   help="mostra o que seria apagado, sem apagar")
    args = p.parse_args()

    conn = db.conectar(args.banco)
    db.criar_schema(conn)
    total = conn.execute("SELECT count(*) FROM ligacoes").fetchone()[0]
    if args.simular:
        corte = (datetime.now() - timedelta(days=args.dias_transcricao)).strftime(db.FORMATO)
        alvo = conn.execute("SELECT count(*) FROM ligacoes WHERE criado_em < ?",
                            (corte,)).fetchone()[0]
        print(f"{total} ligações guardadas · {alvo} com transcrição a remover "
              f"(anteriores a {corte})")
        return 0

    r = purgar(conn, dias_transcricao=args.dias_transcricao,
               dias_metadados=args.dias_metadados)
    print(f"{total} ligações guardadas")
    print(f"  transcrições removidas  {r['transcricoes_removidas']} "
          f"(anteriores a {r['corte_transcricao']})")
    print(f"  ligações removidas      {r['ligacoes_removidas']} "
          f"(anteriores a {r['corte_metadados']})")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
