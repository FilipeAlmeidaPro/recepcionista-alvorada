"""Uma ligação, ponta a ponta, com o tempo de cada estágio na tela.

    python3 -m clinica.ligar --falas "Boa noite, queria um ortopedista" \\
                                     "Só depois das seis" "Meu telefone é ..."

Cada turno percorre o caminho inteiro: síntese da fala do paciente →
transcrição → normalizador → orquestrador → validador → banco → síntese da
resposta. E imprime quanto cada estágio custou.

É esta tabela que responde "onde o turno demora", e é ela que sustenta dizer
"o gargalo é o STT em lote" com número em vez de opinião. O microfone e o
WebRTC do navegador são a camada que falta; tudo abaixo deles está aqui.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from clinica import db
from clinica.agente import Agente
from clinica.provedor import provedor_padrao
from clinica.voz import SinteseMacOS, TranscricaoGroq

PASTA = Path("audio/ligacao")


def _linha(rotulo: str, ms: float, alvo: float = 800.0) -> str:
    marca = "·" if ms <= alvo else "!"
    barra = "█" * min(int(ms / 120), 40)
    return f"    {marca} {rotulo:<22} {ms:7.0f} ms  {barra}"


def main() -> int:
    p = argparse.ArgumentParser(description="Roda uma ligação de voz completa.")
    p.add_argument("--falas", nargs="+", required=True,
                   help="o que o paciente diz, um turno por argumento")
    # Luciana nos dois lados, ritmos diferentes. As vozes de novidade do
    # macOS (Flo, Rocko, Grandma...) degradam muito o STT — "ortopedista"
    # vira "a morta pedista". Servem como condição adversarial, não padrão.
    p.add_argument("--voz-paciente", default="Luciana")
    p.add_argument("--ritmo-paciente", type=int, default=205)
    p.add_argument("--voz-agente", default="Luciana")
    p.add_argument("--ouvir", action="store_true", help="toca o áudio da resposta")
    p.add_argument("--banco", default=None)
    args = p.parse_args()

    provedor = provedor_padrao()
    if provedor is None:
        print("Sem chave de LLM. Rode: cp exemplo.env .env "
      "e preencha GROQ_API_KEY.", file=sys.stderr)
        return 2

    try:
        paciente_tts = SinteseMacOS(args.voz_paciente, args.ritmo_paciente)
        agente_tts = SinteseMacOS(args.voz_agente)
        stt = TranscricaoGroq()
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 2

    conn = db.conectar(args.banco)
    agora = datetime.now()
    agente = Agente(conn, provedor, ligacao_id=f"voz-{int(time.time())}",
                    agora=agora, hoje=agora.date())

    totais: list[float] = []
    for n, fala in enumerate(args.falas, 1):
        print(f"\n─── turno {n} ─────────────────────────────────────────────")
        entrada = paciente_tts.falar(fala, PASTA / f"t{n}-paciente.wav")
        transcrito = stt.transcrever(entrada.caminho)
        print(f"  paciente disse : {fala}")
        print(f"  o STT ouviu    : {transcrito.texto}")

        t0 = time.perf_counter()
        turno = agente.dizer(transcrito.texto)
        ms_orquestrador = (time.perf_counter() - t0) * 1000

        for e in turno.eventos:
            veredito = e["resultado"].get("veredito")
            selo = ("" if not veredito else
                    "  [validador: aprovado]" if veredito["aprovado"]
                    else f"  [validador: BLOQUEOU {veredito['violacoes'][0]['regra']}]")
            print(f"    ↳ {e['ferramenta']}{selo}")

        print(f"  agente responde: {turno.fala_agente}")
        saida = agente_tts.falar(turno.fala_agente or "...", PASTA / f"t{n}-agente.wav")
        if args.ouvir:
            subprocess.run(["afplay", str(saida.caminho)], check=False)

        total = transcrito.ms_gasto + ms_orquestrador + saida.ms_gasto
        totais.append(total)
        print()
        print(_linha("STT (Whisper/Groq)", transcrito.ms_gasto))
        print(_linha("orquestrador + LLM", ms_orquestrador))
        print(_linha("TTS (say, local)", saida.ms_gasto))
        print(f"    = {'turno completo':<22} {total:7.0f} ms"
              f"   {'dentro' if total <= 800 else 'ACIMA'} do alvo de 800 ms")

    resultado = agente.resultado()
    print("\n─── fim da ligação ──────────────────────────────────────")
    print(f"  agendou: {resultado['agendou']}  ·  "
          f"transferiu: {resultado['transferiu']}  ·  "
          f"bloqueios: {resultado['bloqueios'] or 'nenhum'}")
    print(f"  turno mais lento: {max(totais):.0f} ms  ·  "
          f"mediana: {sorted(totais)[len(totais) // 2]:.0f} ms")
    print(f"  áudio em {PASTA}/")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
