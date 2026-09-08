"""CLI da suíte de eval.

    python3 -m avaliacao                      # todos os 41 cenários
    python3 -m avaliacao --familia risco      # só uma família
    python3 -m avaliacao --cenarios B5 C1     # cenários específicos
    python3 -m avaliacao --paciente sintetico # paciente também é LLM
"""
from __future__ import annotations

import argparse
import json
import sys

from avaliacao import relatorio
from avaliacao.cenarios import CENARIOS, FAMILIAS, POR_ID
from avaliacao.runner import rodar
from avaliacao.simulado import RecepcionistaSimulada
from clinica.provedor import PERFIS, ProvedorOpenAICompativel, provedor_padrao


def main() -> int:
    p = argparse.ArgumentParser(description="Suíte de eval do agente de voz.")
    p.add_argument("--cenarios", nargs="*", metavar="ID")
    p.add_argument("--familia", choices=sorted(FAMILIAS))
    p.add_argument("--provedor", choices=sorted(PERFIS) + ["simulado"],
                   help="simulado = recepcionista de regras, sem LLM e sem custo")
    p.add_argument("--modelo", help="sobrescreve o modelo do provedor")
    p.add_argument("--paciente", choices=("roteirizado", "sintetico"),
                   default="roteirizado")
    p.add_argument("--modelo-paciente",
                   help="modelo do paciente sintético (padrão: o mesmo do agente)")
    p.add_argument("--json", metavar="ARQUIVO")
    p.add_argument("--painel", metavar="ARQUIVO.html",
                   help="gera o painel da rodada (HTML de arquivo único)")
    args = p.parse_args()

    try:
        if args.provedor == "simulado":
            agente = RecepcionistaSimulada()
        elif args.provedor:
            agente = ProvedorOpenAICompativel(args.provedor, modelo=args.modelo)
        else:
            agente = provedor_padrao()
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 2
    if agente is None:
        print("Nenhuma chave de LLM no ambiente.\n\n"
              "A suíte precisa de um modelo para o papel de recepcionista. Free\n"
              "tier, sem cartão:\n"
              "  export GEMINI_API_KEY=...   # aistudio.google.com/apikey\n"
              "  export GROQ_API_KEY=...     # console.groq.com/keys\n\n"
              "Sem chave, o que roda é a suíte de unidade — que testa a máquina\n"
              "(tools, normalizador, validador, orquestrador) sem gastar token:\n"
              "  python3 -m unittest discover -s . -t .\n\n"
              "E o harness inteiro roda contra a recepcionista de regras:\n"
              "  python3 -m avaliacao --provedor simulado", file=sys.stderr)
        return 2

    escolhidos = CENARIOS
    if args.familia:
        escolhidos = [c for c in escolhidos if c.familia == args.familia]
    if args.cenarios:
        escolhidos = [POR_ID[i] for i in args.cenarios if i in POR_ID]
    if not escolhidos:
        print("nenhum cenário selecionado", file=sys.stderr)
        return 2

    print(f"provedor: {agente.nome} · paciente: {args.paciente} · "
          f"{len(escolhidos)} cenários\n")
    # O paciente sintético precisa de um LLM próprio. Sem isto, rodar o
    # baseline de regras contra ele daria à recepcionista de `if` o papel de
    # paciente — e o teste que interessa é exatamente esse: um agente burro
    # contra alguém imprevisível.
    fabrica_paciente = None
    if args.paciente == "sintetico":
        try:
            voz_do_paciente = ProvedorOpenAICompativel(
                args.provedor if args.provedor in PERFIS else "groq",
                modelo=args.modelo_paciente or args.modelo)
        except RuntimeError as e:
            print(f"paciente sintético precisa de chave: {e}", file=sys.stderr)
            return 2
        print(f"  paciente sintético em {voz_do_paciente.nome}\n")
        fabrica_paciente = lambda _c: voz_do_paciente      # noqa: E731

    resultados = rodar(escolhidos, lambda _c: agente,
                       fabrica_paciente=fabrica_paciente)

    print(relatorio.tabela(resultados))
    print(relatorio.metricas(resultados))

    if args.painel:
        from avaliacao.painel import gerar
        gerar(resultados, args.painel, provedor=agente.nome,
              paciente=args.paciente)
        print(f"\npainel em {args.painel}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"resumo": relatorio.resumo(resultados),
                       "cenarios": [{"id": r.cenario.id, "passou": r.passou,
                                     "falhas": r.falhas, **r.resultado}
                                    for r in resultados]},
                      f, ensure_ascii=False, indent=2)
        print(f"\ntrace completo em {args.json}")

    return 0 if all(r.passou for r in resultados) else 1


if __name__ == "__main__":
    raise SystemExit(main())
