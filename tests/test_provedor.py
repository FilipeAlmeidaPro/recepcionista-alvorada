"""Testes do provedor: leitura de .env, orçamento e tradução de erros.

O `.env` existe porque a mensagem de erro mandava criar um e o código nunca
lia — seguir a instrução não resolvia nada. É o tipo de bug que só aparece
quando outra pessoa tenta rodar.
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from clinica import provedor
from clinica.provedor import (MarcaPasso, OrcamentoEsgotado,
                              ProvedorRoteirizado, Resposta, _espera_pedida,
                              carregar_env)


class TestCarregarEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.arquivo = Path(self.tmp.name) / ".env"
        self.antes = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self.antes)))

    def test_le_pares_simples(self):
        self.arquivo.write_text("GROQ_API_KEY=abc123\nGEMINI_API_KEY=xyz\n")
        os.environ.pop("GROQ_API_KEY", None)
        os.environ.pop("GEMINI_API_KEY", None)
        lidas = carregar_env(self.arquivo)
        self.assertEqual(lidas["GROQ_API_KEY"], "abc123")
        self.assertEqual(os.environ["GEMINI_API_KEY"], "xyz")

    def test_ignora_comentario_linha_vazia_e_lixo(self):
        self.arquivo.write_text(
            "# comentário\n\n  \nSEM_IGUAL\nVAZIA=\n=SEM_CHAVE\nBOA=1\n")
        self.assertEqual(carregar_env(self.arquivo), {"BOA": "1"})

    def test_tira_aspas(self):
        self.arquivo.write_text("A='um'\nB=\"dois\"\n")
        self.assertEqual(carregar_env(self.arquivo), {"A": "um", "B": "dois"})

    def test_ambiente_explicito_vence_o_arquivo(self):
        """Quem exportou na mão quis aquilo."""
        os.environ["GROQ_API_KEY"] = "do-ambiente"
        self.arquivo.write_text("GROQ_API_KEY=do-arquivo\n")
        carregar_env(self.arquivo)
        self.assertEqual(os.environ["GROQ_API_KEY"], "do-ambiente")

    def test_arquivo_ausente_nao_quebra(self):
        self.assertEqual(carregar_env(Path(self.tmp.name) / "nao-existe"), {})


class TestOrcamento(unittest.TestCase):
    def test_roteirizado_avisa_quando_o_roteiro_acaba(self):
        p = ProvedorRoteirizado([Resposta(texto="um")])
        p.responder([], [])
        with self.assertRaises(AssertionError):
            p.responder([], [])


class TestMarcaPasso(unittest.TestCase):
    def test_le_o_que_o_servidor_informa(self):
        passo = MarcaPasso()
        passo.ler({"x-ratelimit-remaining-tokens": "4200",
                   "x-ratelimit-reset-tokens": "3.5s"})
        self.assertEqual(passo.restante, 4200)
        self.assertAlmostEqual(passo.reset_s, 3.5)

    def test_sem_cabecalho_nao_espera(self):
        passo = MarcaPasso()
        passo.ler({})
        passo.aguardar()
        self.assertEqual(passo.espera_total_s, 0.0)

    def test_folga_grande_nao_espera(self):
        passo = MarcaPasso(margem=1000)
        passo.ler({"x-ratelimit-remaining-tokens": "7000",
                   "x-ratelimit-reset-tokens": "2s"})
        passo.aguardar()
        self.assertEqual(passo.espera_total_s, 0.0)


class TestTraducaoDeErro(unittest.TestCase):
    def test_obedece_o_tempo_que_o_servidor_pede(self):
        """Backoff exponencial chutava 3 s quando o servidor pedia 11."""
        self.assertAlmostEqual(
            _espera_pedida("Rate limit reached. Please try again in 10.965s."),
            11.465, places=2)

    def test_sem_indicacao_devolve_none(self):
        self.assertIsNone(_espera_pedida("erro qualquer"))

    def test_teto_de_espera(self):
        self.assertLessEqual(_espera_pedida("try again in 9999s"), 65.0)


if __name__ == "__main__":
    unittest.main()
