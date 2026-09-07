"""Testes da política de retenção.

Gravar é fácil; o que a LGPD cobra é **apagar**. Estes testes existem para que
"retenção definida" seja uma coisa que roda, não uma frase no README.
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta

from clinica import retencao
from clinica.retencao import DIAS_METADADOS, DIAS_TRANSCRICAO, purgar, registrar
from tests.base import BaseClinica

AGORA = datetime(2026, 9, 7, 12, 0)


class BaseRetencao(BaseClinica):
    def gravar(self, ident: str, *, dias_atras: int, texto="dor no joelho há um mês"):
        registrar(self.conn, {
            "ligacao_id": ident,
            "paciente_id": self.paciente()["id"],
            "agendamento_id": None,
            "motivo_contato": "agendamento: Ortopedia",
            "motivo_transferencia": None,
            "turnos": 4,
            "transcricao": [{"papel": "paciente", "texto": texto}],
            "bloqueios": [], "ferramentas": ["consultar_agenda"],
            "latencias_ms": [910.0],
        }, agora=AGORA - timedelta(days=dias_atras))

    def linha(self, ident):
        return self.conn.execute("SELECT * FROM ligacoes WHERE id = ?",
                                 (ident,)).fetchone()


class TestRegistro(BaseRetencao):
    def test_guarda_o_que_o_painel_precisa(self):
        self.gravar("a", dias_atras=0)
        r = self.linha("a")
        self.assertEqual(r["motivo_contato"], "agendamento: Ortopedia")
        self.assertEqual(r["turnos"], 4)
        self.assertIn("joelho", r["transcricao"])
        self.assertEqual(json.loads(r["trace"])["ferramentas"], ["consultar_agenda"])

    def test_regravar_a_mesma_ligacao_nao_duplica(self):
        self.gravar("a", dias_atras=0)
        self.gravar("a", dias_atras=0, texto="atualizado")
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM ligacoes").fetchone()[0], 1)
        self.assertIn("atualizado", self.linha("a")["transcricao"])


class TestPurga(BaseRetencao):
    def test_ligacao_recente_fica_intacta(self):
        self.gravar("nova", dias_atras=1)
        purgar(self.conn, agora=AGORA)
        self.assertIn("joelho", self.linha("nova")["transcricao"])

    def test_transcricao_velha_some_e_o_metadado_fica(self):
        """O desenho todo em um teste: some o que identifica, fica o que gerencia."""
        self.gravar("media", dias_atras=DIAS_TRANSCRICAO + 10)
        purgar(self.conn, agora=AGORA)
        r = self.linha("media")
        self.assertIsNotNone(r, "o metadado não pode ser apagado junto")
        self.assertNotIn("joelho", r["transcricao"])
        self.assertIn(retencao.MARCA_PURGADA, r["transcricao"])
        self.assertEqual(r["motivo_contato"], "agendamento: Ortopedia")
        self.assertEqual(r["turnos"], 4)
        self.assertIsNone(r["trace"])

    def test_ligacao_muito_velha_some_inteira(self):
        self.gravar("antiga", dias_atras=DIAS_METADADOS + 10)
        purgar(self.conn, agora=AGORA)
        self.assertIsNone(self.linha("antiga"))

    def test_purga_e_idempotente(self):
        self.gravar("media", dias_atras=DIAS_TRANSCRICAO + 10)
        primeira = purgar(self.conn, agora=AGORA)
        segunda = purgar(self.conn, agora=AGORA)
        self.assertEqual(primeira["transcricoes_removidas"], 1)
        self.assertEqual(segunda["transcricoes_removidas"], 0)

    def test_relata_o_que_apagou(self):
        self.gravar("a", dias_atras=DIAS_TRANSCRICAO + 5)
        self.gravar("b", dias_atras=DIAS_METADADOS + 5)
        self.gravar("c", dias_atras=1)
        r = purgar(self.conn, agora=AGORA)
        self.assertEqual(r["ligacoes_removidas"], 1)
        self.assertGreaterEqual(r["transcricoes_removidas"], 1)
        self.assertIn("corte_transcricao", r)

    def test_prazo_customizado(self):
        self.gravar("a", dias_atras=10)
        purgar(self.conn, dias_transcricao=5, agora=AGORA)
        self.assertNotIn("joelho", self.linha("a")["transcricao"])

    def test_agendamento_nao_e_tocado_pela_purga(self):
        """A consulta marcada tem base legal própria e outro prazo."""
        antes = self.conn.execute(
            "SELECT count(*) FROM agendamentos").fetchone()[0]
        self.gravar("velha", dias_atras=DIAS_METADADOS + 100)
        purgar(self.conn, agora=AGORA)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM agendamentos").fetchone()[0],
            antes)


class TestPolitica(unittest.TestCase):
    def test_politica_e_legivel_por_codigo(self):
        p = retencao.politica()
        self.assertEqual(p["transcricao_dias"], DIAS_TRANSCRICAO)
        self.assertLess(p["transcricao_dias"], p["metadados_dias"],
                        "a transcrição tem que sair antes do metadado")


if __name__ == "__main__":
    unittest.main()
