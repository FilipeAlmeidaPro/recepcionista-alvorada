"""Testes do servidor da demo. Sem rede, sem microfone, sem chave.

O que dá para testar sem um navegador de verdade é justamente o que costuma
quebrar em silêncio: contrato das rotas, limite de tamanho de corpo, áudio
curto demais, ligação expirada, e o formato do trace que a página desenha.
O microfone e a permissão são do usuário; o resto é nosso.
"""
from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
import wave
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from clinica import servidor
from clinica.provedor import ChamadaTool, ProvedorRoteirizado, Resposta
from clinica.voz import TAXA, Audio, Transcricao


def _wav_silencioso(caminho: Path, segundos: float = 1.0) -> Path:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(caminho), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(TAXA)
        w.writeframes(b"\x00\x00" * int(TAXA * segundos))
    return caminho


class TTSFalso:
    nome = "falso"

    def __init__(self, pasta: Path):
        self.pasta = pasta
        self.falas: list[str] = []

    def falar(self, texto: str, destino: Path) -> Audio:
        self.falas.append(texto)
        _wav_silencioso(destino, 0.5)
        return Audio(destino, 0.5, 12.0)


class STTFalso:
    nome = "falso"

    def __init__(self, texto="queria marcar um ortopedista depois das seis"):
        self.texto = texto

    def transcrever(self, caminho: Path, **_kw) -> Transcricao:
        return Transcricao(self.texto, 42.0, 1.0)


class BaseServidor(unittest.TestCase):
    ROTEIRO = [Resposta(texto="Boa noite, qual especialidade?")]

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        pasta = Path(self.tmp.name)

        servidor.Ligacao.servidor_voz = {
            "provedor": ProvedorRoteirizado(self.ROTEIRO * 8),
            "tts": TTSFalso(pasta),
            "stt": STTFalso(),
        }
        servidor._ligacoes.clear()

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), servidor.Ligacao)
        self.porta = self.http.server_address[1]
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(self.http.server_close)   # sem isto o socket vaza
        self.addCleanup(self.http.shutdown)
        self.audio = _wav_silencioso(pasta / "fala.wav", 1.2).read_bytes()

    # --- helpers ---
    def _url(self, rota):
        return f"http://127.0.0.1:{self.porta}{rota}"

    def _pedir(self, rota, *, dados=None, cabecalhos=None, metodo=None):
        req = urllib.request.Request(self._url(rota), data=dados,
                                     headers=cabecalhos or {}, method=metodo)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def _abrir(self):
        _c, corpo = self._pedir("/nova", dados=b"", metodo="POST")
        return json.loads(corpo)["ligacao"]


class TestRotas(BaseServidor):
    def test_pagina_e_servida(self):
        codigo, corpo = self._pedir("/")
        self.assertEqual(codigo, 200)
        self.assertIn(b"Recepcionista Alvorada", corpo)
        self.assertIn(b"MediaRecorder", corpo)

    def test_rota_desconhecida(self):
        self.assertEqual(self._pedir("/nao-existe")[0], 404)
        self.assertEqual(self._pedir("/nao-existe", dados=b"x", metodo="POST")[0], 404)

    def test_nova_ligacao_avisa_da_gravacao(self):
        codigo, corpo = self._pedir("/nova", dados=b"", metodo="POST")
        self.assertEqual(codigo, 200)
        d = json.loads(corpo)
        self.assertTrue(d["ligacao"].startswith("web-"))
        self.assertIn("gravada", d["fala"])       # exigência de LGPD, na abertura
        self.assertTrue(d["audio"])

    def test_cada_ligacao_tem_estado_proprio(self):
        a, b = self._abrir(), self._abrir()
        self.assertNotEqual(a, b)
        self.assertEqual(len(servidor._ligacoes), 2)


class TestTurno(BaseServidor):
    def test_turno_completo_devolve_trace(self):
        ligacao = self._abrir()
        codigo, corpo = self._pedir("/turno", dados=self.audio,
                                    cabecalhos={"X-Ligacao": ligacao}, metodo="POST")
        self.assertEqual(codigo, 200)
        d = json.loads(corpo)
        self.assertIn("ortopedista", d["transcricao"])
        self.assertTrue(d["fala"])
        self.assertTrue(d["audio"])
        # o trace é o que a página desenha; o contrato precisa estar completo
        for chave in ("stt", "orquestrador", "tts", "total"):
            self.assertIn(chave, d["estagios"])
        self.assertEqual(d["estagios"]["total"],
                         round(sum(d["estagios"][k] for k in ("stt", "orquestrador", "tts"))))
        for chave in ("agendou", "transferiu", "bloqueios"):
            self.assertIn(chave, d["resultado"])

    def test_restricao_falada_chega_no_trace(self):
        ligacao = self._abrir()
        _c, corpo = self._pedir("/turno", dados=self.audio,
                                cabecalhos={"X-Ligacao": ligacao}, metodo="POST")
        d = json.loads(corpo)
        self.assertEqual(d["restricao"], "a partir das 18:00")
        self.assertTrue(d["restricao_ambigua"])   # "seis" virando 18h é chute

    def test_ligacao_desconhecida(self):
        codigo, corpo = self._pedir("/turno", dados=self.audio,
                                    cabecalhos={"X-Ligacao": "nao-existe"}, metodo="POST")
        self.assertEqual(codigo, 400)
        self.assertEqual(json.loads(corpo)["erro"], "ligacao_desconhecida")

    def test_audio_curto_demais_e_recusado(self):
        """Soltar o botão rápido demais não pode virar um turno vazio."""
        ligacao = self._abrir()
        curto = _wav_silencioso(Path(self.tmp.name) / "curto.wav", 0.15).read_bytes()
        codigo, corpo = self._pedir("/turno", dados=curto,
                                    cabecalhos={"X-Ligacao": ligacao}, metodo="POST")
        self.assertEqual(codigo, 400)
        self.assertEqual(json.loads(corpo)["erro"], "audio_curto_demais")

    def test_corpo_vazio_e_recusado(self):
        ligacao = self._abrir()
        codigo, corpo = self._pedir("/turno", dados=b"",
                                    cabecalhos={"X-Ligacao": ligacao}, metodo="POST")
        self.assertEqual(codigo, 400)
        self.assertEqual(json.loads(corpo)["erro"], "audio_invalido")

    def test_audio_ilegivel(self):
        ligacao = self._abrir()
        codigo, corpo = self._pedir("/turno", dados=b"isto nao e audio" * 40,
                                    cabecalhos={"X-Ligacao": ligacao}, metodo="POST")
        self.assertEqual(codigo, 400)
        self.assertEqual(json.loads(corpo)["erro"], "audio_ilegivel")


class TestTraceDoValidador(BaseServidor):
    """A página precisa poder pintar um bloqueio. Se o contrato mudar sem o
    trace acompanhar, a demo mostra sucesso onde houve recusa."""

    ROTEIRO = [
        Resposta(chamadas=(ChamadaTool("c1", "propor_reserva", {"slot_id": 1}),)),
        Resposta(texto="Preciso te identificar antes."),
    ]

    def test_bloqueio_aparece_no_trace(self):
        ligacao = self._abrir()
        _c, corpo = self._pedir("/turno", dados=self.audio,
                                cabecalhos={"X-Ligacao": ligacao}, metodo="POST")
        eventos = json.loads(corpo)["eventos"]
        self.assertEqual(eventos[0]["ferramenta"], "propor_reserva")
        self.assertEqual(eventos[0]["erro"], "paciente_nao_identificado")


if __name__ == "__main__":
    unittest.main()
