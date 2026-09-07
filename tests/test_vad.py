"""Testes do VAD. Áudio gerado por código — rodam em qualquer máquina.

O VAD decide duas coisas que o resto do sistema não consegue consertar depois:
quando o turno do paciente acabou, e se houve fala. Errar para o lado curto
trunca a fala — e a suíte de áudio mediu o preço disso: extração de entidade
cai de 90% para 60%. Errar para o lado longo faz o agente parecer lerdo.
"""
from __future__ import annotations

import array
import math
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory

from clinica.vad import (MARGEM_DB, Segmento, detectar_fala, piso_de_ruido,
                         recortar_fala, tem_fala)

TAXA = 16_000


def _escrever(caminho: Path, amostras: array.array, taxa: int = TAXA) -> Path:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(caminho), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(taxa)
        w.writeframes(amostras.tobytes())
    return caminho


def _silencio(segundos: float, amplitude: int = 0) -> array.array:
    n = int(TAXA * segundos)
    if amplitude == 0:
        return array.array("h", [0] * n)
    # ruído determinístico e fraco, para simular sala e não silêncio digital
    return array.array("h", [(i * 7919 % (2 * amplitude)) - amplitude for i in range(n)])


def _tom(segundos: float, amplitude: int = 9000, hz: float = 180.0) -> array.array:
    n = int(TAXA * segundos)
    return array.array("h", [int(amplitude * math.sin(2 * math.pi * hz * i / TAXA))
                             for i in range(n)])


def _juntar(*partes: array.array) -> array.array:
    saida = array.array("h")
    for p in partes:
        saida.extend(p)
    return saida


class BaseVAD(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pasta = Path(self.tmp.name)

    def arquivo(self, nome, amostras):
        return _escrever(self.pasta / nome, amostras)


class TestDeteccao(BaseVAD):
    def test_silencio_digital_nao_e_fala(self):
        self.assertEqual(detectar_fala(self.arquivo("mudo.wav", _silencio(2.0))), [])
        self.assertFalse(tem_fala(self.arquivo("mudo2.wav", _silencio(2.0))))

    def test_sala_quieta_nao_e_fala(self):
        """Ruído de fundo fraco não pode abrir turno sozinho."""
        self.assertEqual(
            detectar_fala(self.arquivo("sala.wav", _silencio(2.0, amplitude=60))), [])

    def test_uma_fala_entre_silencios(self):
        audio = _juntar(_silencio(0.8, 60), _tom(1.2), _silencio(0.8, 60))
        [seg] = detectar_fala(self.arquivo("uma.wav", audio))
        self.assertAlmostEqual(seg.inicio_s, 0.8, delta=0.10)
        self.assertAlmostEqual(seg.fim_s, 2.0, delta=0.10)

    def test_pausa_longa_separa_dois_turnos(self):
        audio = _juntar(_silencio(0.4, 60), _tom(0.8), _silencio(1.2, 60),
                        _tom(0.8), _silencio(0.4, 60))
        segmentos = detectar_fala(self.arquivo("dois.wav", audio))
        self.assertEqual(len(segmentos), 2)

    def test_pausa_curta_nao_separa(self):
        """Respirar no meio da frase não pode encerrar o turno."""
        audio = _juntar(_silencio(0.4, 60), _tom(0.7), _silencio(0.25, 60),
                        _tom(0.7), _silencio(0.8, 60))
        self.assertEqual(len(detectar_fala(self.arquivo("respira.wav", audio))), 1)

    def test_estalo_curto_nao_abre_turno(self):
        """Batida de mesa, tosse, clique de fone."""
        audio = _juntar(_silencio(0.6, 60), _tom(0.08), _silencio(1.0, 60))
        self.assertEqual(detectar_fala(self.arquivo("estalo.wav", audio)), [])

    def test_silencio_final_mais_curto_encerra_antes(self):
        audio = _juntar(_silencio(0.3, 60), _tom(0.6), _silencio(0.45, 60),
                        _tom(0.6), _silencio(0.5, 60))
        self.assertEqual(len(detectar_fala(audio_p := self.arquivo("a.wav", audio))), 1)
        self.assertEqual(len(detectar_fala(audio_p, silencio_final_s=0.25)), 2)

    def test_audio_vazio(self):
        self.assertEqual(detectar_fala(self.arquivo("nada.wav", array.array("h"))), [])


class TestPisoDeRuido(BaseVAD):
    def test_piso_acompanha_a_gravacao(self):
        """Limiar absoluto funciona na mesa do dev e falha no celular do
        paciente dentro do carro."""
        quieto, _ = _quadros(self.arquivo("q.wav", _juntar(_silencio(1.0, 40), _tom(0.8))))
        ruidoso, _ = _quadros(self.arquivo("r.wav", _juntar(_silencio(1.0, 900), _tom(0.8))))
        self.assertLess(piso_de_ruido(quieto), piso_de_ruido(ruidoso))

    def test_fala_sobrevive_a_fundo_ruidoso(self):
        audio = _juntar(_silencio(0.6, 700), _tom(1.0, amplitude=12000),
                        _silencio(0.8, 700))
        self.assertTrue(tem_fala(self.arquivo("ruidoso.wav", audio)))

    def test_margem_maior_exige_fala_mais_forte(self):
        audio = _juntar(_silencio(0.6, 400), _tom(1.0, amplitude=1400),
                        _silencio(0.8, 400))
        caminho = self.arquivo("fraca.wav", audio)
        self.assertTrue(tem_fala(caminho, margem_db=MARGEM_DB))
        self.assertFalse(tem_fala(caminho, margem_db=40.0))


class TestRecorte(BaseVAD):
    def test_recorte_tira_o_silencio_das_pontas(self):
        audio = _juntar(_silencio(1.5, 60), _tom(1.0), _silencio(1.5, 60))
        origem = self.arquivo("longo.wav", audio)
        destino = recortar_fala(origem, self.pasta / "curto.wav")
        self.assertIsNotNone(destino)
        with wave.open(str(destino)) as w:
            duracao = w.getnframes() / w.getframerate()
        self.assertLess(duracao, 2.0)      # 4,0 s viram ~1,3 s
        self.assertGreater(duracao, 0.9)   # mas a fala inteira continua lá

    def test_recorte_de_silencio_devolve_nada(self):
        self.assertIsNone(
            recortar_fala(self.arquivo("mudo.wav", _silencio(2.0)),
                          self.pasta / "saida.wav"))

    def test_recorte_preserva_taxa_e_canais(self):
        audio = _juntar(_silencio(0.8, 60), _tom(1.0), _silencio(0.8, 60))
        destino = recortar_fala(self.arquivo("o.wav", audio), self.pasta / "d.wav")
        with wave.open(str(destino)) as w:
            self.assertEqual((w.getframerate(), w.getnchannels(), w.getsampwidth()),
                             (TAXA, 1, 2))


def _quadros(caminho):
    from clinica.vad import _quadros_db
    return _quadros_db(caminho, 0.02)


if __name__ == "__main__":
    unittest.main()
