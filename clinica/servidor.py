"""Servidor da demo no navegador. `http.server` da stdlib — zero dependências.

    python3 -m clinica.servidor          # http://127.0.0.1:8800

O navegador captura o microfone com `MediaRecorder`, manda o trecho por POST, e
recebe de volta a resposta em áudio mais **o trace de cada estágio**. É esse
trace na tela que separa esta demo de uma caixa-preta: dá para ver o STT, o
normalizador, cada ferramenta chamada, cada regra do validador e quantos
milissegundos cada coisa custou.

**O que isto é:** push-to-talk. O paciente segura o botão, fala, solta.

**O que isto não é:** full-duplex com barge-in. Interromper o agente no meio da
frase exige VAD contínuo e streaming dos dois lados — que é onde entra o
Pipecat com `SmallWebRTCTransport`, e é a única parte do projeto que quebraria
a promessa de zero dependências. A arquitetura está desenhada; o encaixe é
trocar este servidor por um transporte WebRTC e manter tudo abaixo dele.
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from clinica import db
from clinica.agente import Agente
from clinica.provedor import provedor_padrao
from clinica.voz import TAXA, SinteseMacOS, TranscricaoGroq, duracao

RAIZ = Path(__file__).resolve().parent.parent
PAGINA = RAIZ / "web" / "index.html"
LIMITE_CORPO = 12 * 1024 * 1024        # ~2 minutos de áudio do navegador

_ligacoes: dict[str, Agente] = {}
_trava = threading.Lock()


def _converter(bruto: bytes, destino: Path) -> Path:
    """webm/opus do navegador → wav 16 kHz mono, que é o que o Whisper quer."""
    entrada = destino.with_suffix(".webm")
    entrada.write_bytes(bruto)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(entrada),
         "-ar", str(TAXA), "-ac", "1", "-c:a", "pcm_s16le", str(destino)],
        check=True, capture_output=True)
    entrada.unlink(missing_ok=True)
    return destino


class Ligacao(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    servidor_voz: dict = {}

    def log_message(self, formato, *args):      # silencia o log padrão
        pass

    # --- utilidades ---

    def _responder(self, codigo: int, corpo: bytes, tipo: str):
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(corpo)

    def _json(self, dados: dict, codigo: int = 200):
        self._responder(codigo, json.dumps(dados, ensure_ascii=False).encode(),
                        "application/json; charset=utf-8")

    # --- rotas ---

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            if not PAGINA.exists():
                return self._responder(500, b"web/index.html nao encontrado", "text/plain")
            return self._responder(200, PAGINA.read_bytes(), "text/html; charset=utf-8")
        self._responder(404, b"nao encontrado", "text/plain")

    def do_POST(self):
        if self.path.startswith("/nova"):
            return self._nova()
        if self.path.startswith("/turno"):
            return self._turno()
        self._responder(404, b"nao encontrado", "text/plain")

    def _nova(self):
        ligacao_id = f"web-{uuid.uuid4().hex[:8]}"
        agora = datetime.now()
        with _trava:
            _ligacoes[ligacao_id] = Agente(
                db.conectar(), self.servidor_voz["provedor"],
                ligacao_id=ligacao_id, agora=agora, hoje=agora.date())
        abertura = ("Clínica Alvorada, boa noite. Esta chamada é gravada. "
                    "Em que posso ajudar?")
        audio = self.servidor_voz["tts"].falar(
            abertura, Path(tempfile.gettempdir()) / f"{ligacao_id}-abertura.wav")
        self._json({"ligacao": ligacao_id, "fala": abertura,
                    "audio": base64.b64encode(audio.caminho.read_bytes()).decode()})

    def _turno(self):
        ligacao_id = self.headers.get("X-Ligacao", "")
        with _trava:
            agente = _ligacoes.get(ligacao_id)
        if agente is None:
            return self._json({"erro": "ligacao_desconhecida"}, 400)

        tamanho = int(self.headers.get("Content-Length", 0))
        if not 0 < tamanho <= LIMITE_CORPO:
            return self._json({"erro": "audio_invalido", "bytes": tamanho}, 400)
        bruto = self.rfile.read(tamanho)

        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "turno.wav"
            try:
                _converter(bruto, wav)
            except subprocess.CalledProcessError:
                return self._json({"erro": "audio_ilegivel"}, 400)
            if duracao(wav) < 0.35:
                return self._json({"erro": "audio_curto_demais"}, 400)

            t = self.servidor_voz["stt"].transcrever(wav)
            t0 = time.perf_counter()
            turno = agente.dizer(t.texto)
            ms_orquestrador = (time.perf_counter() - t0) * 1000
            saida = Path(tmp) / "resposta.wav"
            fala = self.servidor_voz["tts"].falar(turno.fala_agente or "...", saida)
            audio_b64 = base64.b64encode(saida.read_bytes()).decode()

        resultado = agente.resultado()
        self._json({
            "transcricao": t.texto,
            "fala": turno.fala_agente,
            "audio": audio_b64,
            "restricao": agente.restricao.interpretacao,
            "restricao_ambigua": agente.restricao.ambigua,
            "eventos": [{
                "ferramenta": e["ferramenta"],
                "erro": e["resultado"].get("erro"),
                "regra": e["resultado"].get("regra"),
                "veredito": e["resultado"].get("veredito"),
            } for e in turno.eventos],
            "estagios": {
                "stt": round(t.ms_gasto),
                "orquestrador": round(ms_orquestrador),
                "tts": round(fala.ms_gasto),
                "total": round(t.ms_gasto + ms_orquestrador + fala.ms_gasto),
            },
            "resultado": {"agendou": resultado["agendou"],
                          "transferiu": resultado["transferiu"],
                          "motivo": resultado["motivo_transferencia"],
                          "bloqueios": resultado["bloqueios"]},
        })


def main() -> int:
    p = argparse.ArgumentParser(description="Demo da recepcionista no navegador.")
    p.add_argument("--porta", type=int, default=8800)
    p.add_argument("--voz", default="Luciana")
    args = p.parse_args()

    provedor = provedor_padrao()
    if provedor is None:
        print("Sem chave de LLM.\n\n"
              "  cp exemplo.env .env      e preencha GROQ_API_KEY\n"
              "  console.groq.com/keys    free tier, sem cartão\n\n"
              "O .env fica no .gitignore.", file=sys.stderr)
        return 2
    try:
        Ligacao.servidor_voz = {"provedor": provedor, "tts": SinteseMacOS(args.voz),
                                "stt": TranscricaoGroq()}
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 2

    servidor = ThreadingHTTPServer(("127.0.0.1", args.porta), Ligacao)
    # flush explícito: sem ele o banner só aparece quando o processo morre,
    # e quem roda fica sem saber se subiu.
    print(f"Recepcionista Alvorada · {provedor.nome} · voz {args.voz}", flush=True)
    print(f"  http://127.0.0.1:{args.porta}\n  ctrl+c para parar", flush=True)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nencerrado")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
