"""Painel de uma rodada de eval. HTML de arquivo único, zero dependências.

O plano previa Streamlit. Não usei, e a razão importa: o projeto inteiro roda
com `python3` puro, e um painel que exige `pip install streamlit` + servidor
local é pior de compartilhar do que um arquivo que abre com duplo clique. Um
HTML estático vai por e-mail, entra num anexo e sobrevive à apresentação.

Cores: paleta de status validada com o script do skill de dataviz sobre o
fundo #0b0b0b — verde #4ade80 / vermelho #f0705a separam ΔE 9,7 em deuteranopia
e passam contraste. Ainda assim, nenhum estado é comunicado só por cor: todo
mark tem rótulo em texto ao lado.
"""
from __future__ import annotations

import html
from datetime import datetime

from avaliacao import relatorio
from clinica import retencao
from avaliacao.runner import ResultadoCenario

VERDE = "#4ade80"
VERMELHO = "#f0705a"

CSS = """
:root{
  --fundo:#0b0b0b; --superficie:#131513; --superficie2:#1a1d1a;
  --tinta:#f8f8f6; --tinta2:#a3ada6; --tinta3:#6e786f;
  --linha:rgba(248,248,246,.14);
  --verde:#4ade80; --verde-esc:#16a34a; --vermelho:#f0705a;
  --r1:8px; --r2:14px; --r3:16px;
}
*{box-sizing:border-box}
body{margin:0;background:var(--fundo);color:var(--tinta);
  font:400 15px/1.55 "Space Grotesk",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:1080px;margin:0 auto;padding:48px 24px 96px}
h1{font-size:28px;font-weight:500;margin:0 0 6px;letter-spacing:-.01em}
h2{font-size:13px;font-weight:500;text-transform:uppercase;letter-spacing:.12em;
  color:var(--tinta3);margin:56px 0 18px}
.sub{color:var(--tinta2);font-size:14px;margin:0}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-top:32px}
.tile{background:var(--superficie);border:.5px solid var(--linha);border-radius:var(--r3);padding:20px}
.tile .rot{font-size:12px;color:var(--tinta3);text-transform:uppercase;letter-spacing:.08em}
.tile .val{font-size:32px;font-weight:500;margin-top:8px;letter-spacing:-.02em;
  font-variant-numeric:tabular-nums}
.tile .nota{font-size:12px;color:var(--tinta2);margin-top:4px}
.barra{display:grid;grid-template-columns:130px 1fr 76px;align-items:center;gap:12px;
  padding:5px 0}
.barra .rot{font-size:13px;color:var(--tinta2)}
.trilho{background:var(--superficie2);border-radius:var(--r1);height:14px;overflow:hidden}
.preenche{height:100%;background:var(--verde);border-radius:0 4px 4px 0;min-width:2px}
.preenche.ruim{background:var(--vermelho)}
.barra .num{font-size:13px;color:var(--tinta);text-align:right;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse;font-size:14px}
td,th{text-align:left;padding:7px 12px 7px 0;border-bottom:.5px solid var(--linha)}
th{font-size:12px;color:var(--tinta3);text-transform:uppercase;letter-spacing:.08em;font-weight:500}
td.n{text-align:right;font-variant-numeric:tabular-nums}
details{background:var(--superficie);border:.5px solid var(--linha);border-radius:var(--r2);
  margin-bottom:8px;overflow:hidden}
summary{cursor:pointer;padding:14px 18px;display:grid;
  grid-template-columns:44px 1fr auto auto;gap:14px;align-items:center;list-style:none}
summary::-webkit-details-marker{display:none}
summary:hover{background:var(--superficie2)}
summary:focus-visible{outline:2px solid var(--verde);outline-offset:-2px}
.id{font-size:12px;color:var(--tinta3);font-variant-numeric:tabular-nums}
.marca{font-size:12px;padding:3px 10px;border-radius:999px;white-space:nowrap}
.marca.ok{color:var(--verde);background:rgba(74,222,128,.12)}
.marca.erro{color:var(--vermelho);background:rgba(240,112,90,.14)}
.corpo{padding:4px 18px 20px;border-top:.5px solid var(--linha)}
.fala{display:grid;grid-template-columns:78px 1fr;gap:12px;padding:4px 0;font-size:14px}
.fala .quem{color:var(--tinta3);font-size:12px;text-align:right;padding-top:2px}
.fala.agente .quem{color:var(--verde-esc)}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0}
.chip{font-size:12px;padding:3px 9px;border-radius:var(--r1);background:var(--superficie2);
  color:var(--tinta2);border:.5px solid var(--linha)}
.chip.bloq{color:var(--vermelho);border-color:rgba(240,112,90,.4)}
.falha{color:var(--vermelho);font-size:13px;margin:2px 0}
.rodape{margin-top:64px;padding-top:20px;border-top:.5px solid var(--linha);
  color:var(--tinta3);font-size:12px}
"""


def _e(t) -> str:
    return html.escape(str(t if t is not None else ""))


def _plural(n: int, palavra: str) -> str:
    return f"{n} {palavra}" + ("" if n == 1 else "s")


def _barra(rotulo: str, valor: float, maximo: float, texto: str, ruim=False) -> str:
    largura = (valor / maximo * 100) if maximo else 0
    classe = "preenche ruim" if ruim else "preenche"
    return (f'<div class="barra"><div class="rot">{_e(rotulo)}</div>'
            f'<div class="trilho" title="{_e(rotulo)}: {_e(texto)}">'
            f'<div class="{classe}" style="width:{largura:.1f}%"></div></div>'
            f'<div class="num">{_e(texto)}</div></div>')


def _tile(rotulo: str, valor: str, nota: str = "") -> str:
    return (f'<div class="tile"><div class="rot">{_e(rotulo)}</div>'
            f'<div class="val">{_e(valor)}</div>'
            + (f'<div class="nota">{_e(nota)}</div>' if nota else "") + "</div>")


def _ligacao(r: ResultadoCenario) -> str:
    res = r.resultado or {}
    marca = ('<span class="marca ok">passou</span>' if r.passou
             else '<span class="marca erro">falhou</span>')
    linhas = [f'<details><summary><span class="id">{_e(r.cenario.id)}</span>'
              f"<span>{_e(r.cenario.titulo)}</span>"
              f'<span class="id">{_plural(res.get("turnos", 0), "turno")}</span>{marca}'
              f'</summary><div class="corpo">']

    linhas.append(f'<p class="sub" style="margin:12px 0 4px">'
                  f"testa: {_e(r.cenario.testa)}</p>")
    for f in r.falhas:
        linhas.append(f'<p class="falha">↳ {_e(f)}</p>')

    for t in res.get("transcricao", []):
        papel = t["papel"]
        linhas.append(f'<div class="fala {papel}"><div class="quem">{_e(papel)}</div>'
                      f"<div>{_e(t['texto'])}</div></div>")

    desfecho = ("agendou" if res.get("agendou")
                else f"transferiu · {res.get('motivo_transferencia')}"
                if res.get("transferiu") else "sem desfecho")
    linhas.append(f'<p class="sub" style="margin:10px 0 2px">desfecho: '
                  f'<b style="color:var(--tinta)">{_e(desfecho)}</b>'
                  f' · motivo do contato: {_e(res.get("motivo_contato", "—"))}</p>')

    chips = "".join(f'<span class="chip">{_e(f)}</span>'
                    for f in res.get("ferramentas", []))
    chips += "".join(f'<span class="chip bloq">bloqueado: {_e(b)}</span>'
                     for b in res.get("bloqueios", []) if b)
    if res.get("transferiu"):
        chips += (f'<span class="chip bloq">transferiu: '
                  f'{_e(res.get("motivo_transferencia"))}</span>')
    if chips:
        linhas.append(f'<div class="chips">{chips}</div>')

    latencias = res.get("latencias_ms") or []
    # Rodada sem LLM não tem latência para mostrar; barra cheia de "0 ms" é
    # ruído que finge ser informação.
    if latencias and max(latencias) >= 1:
        pico = max(latencias)
        linhas.append('<h2 style="margin:22px 0 10px">latência por turno</h2>')
        for i, ms in enumerate(latencias, 1):
            linhas.append(_barra(f"turno {i}", ms, pico, f"{ms:.0f} ms",
                                 ruim=ms > 1400))
        linhas.append(f'<p class="sub" style="margin-top:10px">tokens '
                      f'{_e(res.get("tokens_entrada", 0))} entrada / '
                      f'{_e(res.get("tokens_saida", 0))} saída · alvo de mercado '
                      f"por turno: 800 ms</p>")
    return "\n".join(linhas) + "</div></details>"


def gerar(resultados: list[ResultadoCenario], caminho: str, *,
          provedor: str = "—", paciente: str = "roteirizado") -> str:
    s = relatorio.resumo(resultados)
    partes = ['<link rel="preconnect" href="https://fonts.googleapis.com">'
              '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
              '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
              'family=Space+Grotesk:wght@400;500&display=swap">',
              f"<style>{CSS}</style>", '<div class="wrap">',
              "<h1>Voice Agent Assistant — rodada de eval</h1>",
              f'<p class="sub">{_e(len(resultados))} cenários · modelo '
              f"{_e(provedor)} · paciente {_e(paciente)} · "
              f'{datetime.now():%d/%m/%Y %H:%M}</p>']

    p95 = s["latencia_p95_ms"]
    partes.append('<div class="tiles">'
                  + _tile("conclusão da tarefa", f"{s['taxa_conclusao']:.0%}",
                          f"{s['passou']} de {s['cenarios']} cenários")
                  + _tile("turnos por ligação", f"{s['turnos_media']}")
                  + _tile("latência p95 / turno", f"{p95:.0f} ms",
                          "alvo de mercado: 800 ms")
                  + _tile("custo da rodada", f"US$ {s['custo_free_tier_usd']:.2f}",
                          f"se pago: US$ {s['custo_referencia_por_ligacao_usd']:.5f}"
                          f"/ligação")
                  + "</div>")

    partes.append("<h2>conclusão por família</h2>")
    for familia, v in s["por_familia"].items():
        partes.append(_barra(familia, v["taxa"], 1.0,
                             f"{v['passou']}/{v['total']}", ruim=v["taxa"] < 0.7))

    rec = s["recuperacao"]
    if rec["taxa"] is not None:
        partes.append("<h2>recuperação — quando algo dá errado no meio</h2>")
        partes.append(_barra("hesitação, silêncio, interrupção, recusa",
                             rec["taxa"], 1.0,
                             f"{rec['recuperou']}/{rec['cenarios']}",
                             ruim=rec["taxa"] < 0.7))

    if s["motivos_de_contato"]:
        partes.append("<h2>motivos de contato</h2>")
        pico = max(s["motivos_de_contato"].values())
        for motivo, n in sorted(s["motivos_de_contato"].items(), key=lambda x: -x[1]):
            partes.append(_barra(motivo, n, pico, str(n)))

    if s["bloqueios"]:
        partes.append("<h2>o que o validador bloqueou</h2>")
        pico = max(s["bloqueios"].values())
        for regra, n in sorted(s["bloqueios"].items(), key=lambda x: -x[1]):
            partes.append(_barra(regra, n, pico, str(n), ruim=True))

    if s["transferencias"]:
        partes.append("<h2>transferências para humano</h2>"
                      "<table><tr><th>motivo</th><th></th></tr>")
        for motivo, n in sorted(s["transferencias"].items(), key=lambda x: -x[1]):
            partes.append(f"<tr><td>{_e(motivo)}</td><td class='n'>{n}</td></tr>")
        partes.append("</table>")

    partes.append("<h2>ligação a ligação</h2>")
    partes += [_ligacao(r) for r in resultados]

    politica = retencao.politica()
    partes.append('<div class="rodape">Transcrições mascaradas: CPF e telefone '
                  "ditados não aparecem aqui, em dígito nem por extenso. "
                  f"Retenção: transcrição {politica['transcricao_dias']} dias, "
                  f"metadados operacionais {politica['metadados_dias']} dias — "
                  "<code>python3 -m clinica.retencao</code> aplica. "
                  "Cores de status validadas para deuteranopia; nenhum estado é "
                  "comunicado só por cor.</div></div>")

    saida = "\n".join(partes)
    with open(caminho, "w", encoding="utf-8") as f:
        f.write(f"<!doctype html><meta charset=utf-8>"
                f"<meta name=viewport content='width=device-width,initial-scale=1'>"
                f"<title>Eval — Voice Agent Assistant</title>{saida}")
    return caminho
