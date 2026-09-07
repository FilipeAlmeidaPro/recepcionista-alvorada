"""Tabela de resultados e métricas agregadas."""
from __future__ import annotations

import statistics
from collections import Counter

from avaliacao.runner import ResultadoCenario

# Referência paga, só para dimensionar. A stack da demo roda em free tier: $0.
PRECO_REFERENCIA = {"entrada_por_milhao": 3.00, "saida_por_milhao": 15.00}


def _percentil(valores: list[float], p: float) -> float:
    if not valores:
        return 0.0
    ordenados = sorted(valores)
    i = min(int(len(ordenados) * p), len(ordenados) - 1)
    return ordenados[i]


def _recuperacao(resultados: list[ResultadoCenario]) -> dict:
    """Taxa de recuperação: dos cenários em que algo dá errado no meio da
    conversa — hesitação, silêncio, interrupção, recusa —, quantos ainda
    terminam a tarefa. O plano prometia esta métrica; é ela que separa um
    agente que conduz de um que só funciona no caminho feliz."""
    alvo = [r for r in resultados if r.cenario.exige_recuperacao]
    if not alvo:
        return {"cenarios": 0, "recuperou": 0, "taxa": None}
    recuperou = sum(r.passou for r in alvo)
    return {"cenarios": len(alvo), "recuperou": recuperou,
            "taxa": recuperou / len(alvo)}


def resumo(resultados: list[ResultadoCenario]) -> dict:
    total = len(resultados)
    passou = sum(r.passou for r in resultados)
    latencias = [ms for r in resultados for ms in r.resultado.get("latencias_ms", [])]
    entrada = sum(r.resultado.get("tokens_entrada", 0) for r in resultados)
    saida = sum(r.resultado.get("tokens_saida", 0) for r in resultados)

    por_familia = {}
    for r in resultados:
        f = por_familia.setdefault(r.cenario.familia, [0, 0])
        f[0] += r.passou
        f[1] += 1

    custo = (entrada / 1e6 * PRECO_REFERENCIA["entrada_por_milhao"]
             + saida / 1e6 * PRECO_REFERENCIA["saida_por_milhao"])
    return {
        "cenarios": total,
        "passou": passou,
        "taxa_conclusao": passou / total if total else 0.0,
        "por_familia": {f: {"passou": v[0], "total": v[1], "taxa": v[0] / v[1]}
                        for f, v in sorted(por_familia.items())},
        "bloqueios": dict(Counter(
            b for r in resultados for b in r.resultado.get("bloqueios", []) if b)),
        "transferencias": dict(Counter(
            r.resultado.get("motivo_transferencia") for r in resultados
            if r.resultado.get("transferiu"))),
        "motivos_de_contato": dict(Counter(
            r.resultado.get("motivo_contato") for r in resultados
            if r.resultado.get("motivo_contato"))),
        "recuperacao": _recuperacao(resultados),
        "turnos_media": round(statistics.mean(
            [r.turnos for r in resultados]) if resultados else 0, 1),
        "latencia_p50_ms": round(_percentil(latencias, 0.50), 1),
        "latencia_p95_ms": round(_percentil(latencias, 0.95), 1),
        "tokens_entrada": entrada,
        "tokens_saida": saida,
        "custo_free_tier_usd": 0.0,
        "custo_referencia_paga_usd": round(custo, 4),
        "custo_referencia_por_ligacao_usd": round(custo / total, 5) if total else 0.0,
    }


def tabela(resultados: list[ResultadoCenario]) -> str:
    linhas = [f"{'id':<4} {'família':<12} {'cenário':<40} {'turnos':>6}  resultado",
              "─" * 96]
    for r in resultados:
        marca = "passou" if r.passou else "FALHOU"
        linhas.append(f"{r.cenario.id:<4} {r.cenario.familia:<12} "
                      f"{r.cenario.titulo[:40]:<40} {r.turnos:>6}  {marca}")
        for f in r.falhas:
            linhas.append(f"{'':<4} {'':<12} └─ {f}")
    return "\n".join(linhas)


def metricas(resultados: list[ResultadoCenario]) -> str:
    s = resumo(resultados)
    linhas = ["", "─" * 96,
              f"conclusão da tarefa      {s['passou']}/{s['cenarios']}  "
              f"({s['taxa_conclusao']:.0%})", ""]
    for familia, v in s["por_familia"].items():
        linhas.append(f"  {familia:<14} {v['passou']:>2}/{v['total']:<2}  {v['taxa']:.0%}")
    linhas += ["", f"turnos por ligação       {s['turnos_media']}",
               f"latência por turno       p50 {s['latencia_p50_ms']} ms   "
               f"p95 {s['latencia_p95_ms']} ms"]
    if s["bloqueios"]:
        linhas.append("")
        linhas.append("bloqueios do validador")
        for regra, n in sorted(s["bloqueios"].items(), key=lambda x: -x[1]):
            linhas.append(f"  {regra:<20} {n}")
    rec = s["recuperacao"]
    if rec["taxa"] is not None:
        linhas.append(f"recuperação                {rec['recuperou']}/"
                      f"{rec['cenarios']}  ({rec['taxa']:.0%}) — hesitação, "
                      f"silêncio, interrupção, recusa")
    if s["motivos_de_contato"]:
        linhas.append("")
        linhas.append("motivos de contato")
        for motivo, n in sorted(s["motivos_de_contato"].items(), key=lambda x: -x[1]):
            linhas.append(f"  {str(motivo):<34} {n}")
    if s["transferencias"]:
        linhas.append("")
        linhas.append("transferências para humano")
        for motivo, n in sorted(s["transferencias"].items(), key=lambda x: -x[1]):
            linhas.append(f"  {str(motivo):<20} {n}")
    linhas += ["",
               f"tokens                   {s['tokens_entrada']} entrada / "
               f"{s['tokens_saida']} saída",
               f"custo desta rodada       US$ {s['custo_free_tier_usd']:.2f} "
               f"(free tier)",
               f"custo se fosse pago      US$ {s['custo_referencia_paga_usd']:.4f} "
               f"total · US$ {s['custo_referencia_por_ligacao_usd']:.5f} por ligação",
               "─" * 96]
    return "\n".join(linhas)
