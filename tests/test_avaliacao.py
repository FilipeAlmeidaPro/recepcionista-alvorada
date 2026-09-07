"""Testes do juiz. Um harness que não reprova um agente ruim não vale nada.

Aqui o agente sob teste é a recepcionista simulada, em três versões: correta,
alucinando e apressada. Se o juiz aprovar as duas últimas, a suíte inteira é
decoração.
"""
from __future__ import annotations

import unittest
from datetime import datetime

from avaliacao.cenarios import CENARIOS, POR_ID, Expectativa
from avaliacao.runner import AGORA, _banco, _contexto, _integridade, avaliar, executar
from avaliacao.simulado import RecepcionistaSimulada
from clinica.agente import Agente
from clinica.provedor import ChamadaTool, ProvedorRoteirizado, Resposta


class TestJuizTemDente(unittest.TestCase):
    def test_recepcionista_correta_passa_o_caminho_feliz(self):
        r = executar(POR_ID["A1"], RecepcionistaSimulada())
        self.assertTrue(r.passou, r.falhas)
        self.assertTrue(r.resultado["agendou"])

    def test_alucinacao_de_entidade_reprova(self):
        r = executar(POR_ID["A1"], RecepcionistaSimulada(alucina=True))
        self.assertFalse(r.passou)
        self.assertIn("R8_confirmacao", r.resultado["bloqueios"])
        self.assertFalse(r.resultado["agendou"])

    def test_marcar_sem_esperar_confirmacao_reprova(self):
        r = executar(POR_ID["A1"], RecepcionistaSimulada(apressado=True))
        self.assertFalse(r.passou)
        self.assertIn("R8_confirmacao", r.resultado["bloqueios"])

    def test_risco_clinico_nao_pode_virar_agendamento(self):
        r = executar(POR_ID["B5"], RecepcionistaSimulada(apressado=True))
        self.assertTrue(r.resultado["transferiu"])
        self.assertFalse(r.resultado["agendou"])


class TestProibidoAposTransferir(unittest.TestCase):
    """A asserção precisa pegar quem segue atendendo depois de escalar — e
    precisa NÃO pegar quem disse a mesma frase antes de escalar.

    O segundo caso é real: contra um LLM de verdade, "Posso confirmar seu CPF?"
    no primeiro turno reprovava um agente que tinha acertado tudo."""

    def _rodar(self, roteiro, espera):
        conn = _banco()
        self.addCleanup(conn.close)
        agente = Agente(conn, ProvedorRoteirizado(roteiro), ligacao_id="t",
                        agora=AGORA, hoje=AGORA.date())
        agente.dizer("boa noite, queria marcar um cardiologista")
        agente.dizer("é que eu tô com uma dor no peito desde ontem")
        return avaliar(espera, agente, conn)

    TRANSFERIR = Resposta(chamadas=(ChamadaTool(
        "c1", "transferir_para_humano",
        {"motivo": "risco_clinico", "resumo": "dor no peito"}),))

    def test_pega_quem_continua_oferecendo_depois_de_escalar(self):
        falhas = self._rodar(
            [Resposta(texto="Boa noite. Posso confirmar seu CPF?"),
             self.TRANSFERIR,
             Resposta(texto="Enquanto isso, tenho terça às 18h. Serve?")],
            Expectativa(proibido_apos_transferir=(r"Serve\?",)))
        self.assertTrue(any("seguiu atendendo" in f for f in falhas), falhas)

    def test_nao_pega_a_mesma_frase_dita_antes_de_escalar(self):
        falhas = self._rodar(
            [Resposta(texto="Boa noite. Posso confirmar seu CPF?"),
             self.TRANSFERIR,
             Resposta(texto="Vou te passar para uma pessoa agora.")],
            Expectativa(proibido_apos_transferir=(r"Posso confirmar",)))
        self.assertEqual(falhas, [])


class TestAsercoes(unittest.TestCase):
    """Cada tipo de asserção precisa reprovar quando deve."""

    def setUp(self):
        self.conn = _banco()
        self.addCleanup(self.conn.close)
        self.agente = Agente(self.conn, RecepcionistaSimulada(), ligacao_id="t",
                             agora=AGORA, hoje=AGORA.date())
        ctx = _contexto(self.conn)
        for fala in POR_ID["A1"].falas:
            self.agente.dizer(fala.format(**ctx))

    def test_pega_agendou_errado(self):
        self.assertTrue(avaliar(Expectativa(agendou=False), self.agente, self.conn))

    def test_pega_ferramenta_obrigatoria_ausente(self):
        falhas = avaliar(Expectativa(ferramentas_obrigatorias=("propor_reagendamento",)),
                         self.agente, self.conn)
        self.assertIn("não usou propor_reagendamento", falhas)

    def test_pega_ferramenta_proibida_usada(self):
        falhas = avaliar(Expectativa(ferramentas_proibidas=("propor_reserva",)),
                         self.agente, self.conn)
        self.assertTrue(any("proibido" in f for f in falhas))

    def test_pega_fala_proibida(self):
        falhas = avaliar(Expectativa(proibido_falar=(r"Posso confirmar",)),
                         self.agente, self.conn)
        self.assertTrue(any("falou o que não devia" in f for f in falhas))

    def test_pega_teto_de_turnos_estourado(self):
        falhas = avaliar(Expectativa(max_turnos=1), self.agente, self.conn)
        self.assertTrue(any("teto" in f for f in falhas))

    def test_pega_transferencia_com_motivo_errado(self):
        falhas = avaliar(Expectativa(motivo_transferencia="risco_clinico"),
                         self.agente, self.conn)
        self.assertTrue(any("risco_clinico" in f for f in falhas))

    def test_aprova_quando_esta_tudo_certo(self):
        self.assertEqual(avaliar(POR_ID["A1"].espera, self.agente, self.conn), [])


class TestIntegridade(unittest.TestCase):
    def test_escrita_por_fora_do_validador_e_detectada(self):
        conn = _banco()
        self.addCleanup(conn.close)
        self.assertEqual(_integridade(conn), [])
        livre = conn.execute("SELECT id FROM slots WHERE status='livre' LIMIT 1").fetchone()["id"]
        conn.execute("INSERT INTO agendamentos (slot_id, paciente_id, status, criado_em, "
                     "idempotency_key) VALUES (?,1,'confirmado','2026-09-03 10:00','burla')",
                     (livre,))
        problemas = _integridade(conn)
        self.assertTrue(problemas)
        self.assertIn("slot não ocupado", problemas[0])


class TestFixture(unittest.TestCase):
    """Fixture irrealista faz o modelo parecer errado quando ele está certo."""

    def setUp(self):
        self.conn = _banco()
        self.addCleanup(self.conn.close)
        self.ctx = _contexto(self.conn)

    def test_telefone_tem_ddd(self):
        """Sem DDD são 9 dígitos, e o modelo — corretamente — achava que era
        um CPF incompleto e pedia para repetir. Três cenários falhavam por
        causa disso, e a culpa era do dado, não do agente."""
        from clinica.normalizador import extrair_digitos
        for chave in ("telefone", "telefone_falado", "telefone_falado_meia",
                      "telefone2_falado"):
            with self.subTest(chave=chave):
                self.assertEqual(len(extrair_digitos(self.ctx[chave])), 11)

    def test_cpf_da_fixture_e_valido(self):
        from clinica.normalizador import cpf_valido, extrair_digitos
        self.assertTrue(cpf_valido(self.ctx["cpf"]))
        self.assertEqual(extrair_digitos(self.ctx["cpf_falado"]), self.ctx["cpf"])
        # o ditado com erro tem que resultar no mesmo CPF depois da correção
        self.assertEqual(extrair_digitos(self.ctx["cpf_falado_com_erro"]),
                         self.ctx["cpf"])

    def test_o_telefone_ditado_encontra_o_cadastro(self):
        from clinica import tools
        from clinica.normalizador import extrair_digitos
        r = tools.buscar_paciente(
            self.conn, telefone=extrair_digitos(self.ctx["telefone_falado"]))
        self.assertTrue(r["encontrado"])
        self.assertEqual(r["paciente"]["id"], self.ctx["_paciente_id"])


class TestCatalogo(unittest.TestCase):
    def test_todo_placeholder_resolve(self):
        conn = _banco()
        self.addCleanup(conn.close)
        ctx = _contexto(conn)
        for c in CENARIOS:
            for fala in c.falas:
                with self.subTest(cenario=c.id):
                    fala.format(**ctx)      # KeyError aqui é placeholder inventado

    def test_todo_cenario_afirma_alguma_coisa(self):
        for c in CENARIOS:
            e = c.espera
            with self.subTest(cenario=c.id):
                self.assertTrue(
                    any([e.agendou is not None, e.transferiu is not None,
                         e.motivo_transferencia, e.ferramentas_obrigatorias,
                         e.ferramentas_proibidas, e.bloqueios_esperados,
                         e.proibido_falar, e.max_turnos]),
                    f"{c.id} não verifica nada além da integridade do banco")

    def test_ids_e_familias_consistentes(self):
        ids = [c.id for c in CENARIOS]
        self.assertEqual(len(set(ids)), len(ids))
        self.assertEqual(len(CENARIOS), 40)


if __name__ == "__main__":
    unittest.main()
