"""Testes do orquestrador com provedor roteirizado — sem LLM, sem rede, sem custo.

O que se testa aqui é a **máquina**: se a restrição chega na consulta sem passar
pelo modelo, se a frase de confirmação lida na R8 é mesmo a que foi falada, e se
uma proposta reprovada não escreve nada.
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime

from avaliacao.paciente import ditar
from clinica import db
from clinica.agente import Agente
from clinica.provedor import ChamadaTool, ProvedorRoteirizado, Resposta
from tests.base import AGORA, BaseClinica

LIGACAO = datetime(2026, 1, 5, 19, 0)   # fora do horário comercial


def falar(texto):
    return Resposta(texto=texto, tokens_entrada=120, tokens_saida=30)


def chamar(nome, **args):
    return Resposta(chamadas=(ChamadaTool("c1", nome, args),),
                    tokens_entrada=120, tokens_saida=20)


def _resultados(mensagens):
    for m in reversed(mensagens):
        if m.get("role") == "tool":
            yield json.loads(m["content"])


def slots_oferecidos(mensagens):
    for r in _resultados(mensagens):
        if r.get("slots"):
            return r["slots"]
    return []


class BaseAgente(BaseClinica):
    def agente(self, roteiro):
        return Agente(self.conn, ProvedorRoteirizado(roteiro), ligacao_id="lig-teste",
                      agora=LIGACAO, hoje=LIGACAO.date())

    def confirmar_primeiro_slot(self, mensagens):
        s = slots_oferecidos(mensagens)[0]
        return falar(f"Consegui com {s['profissional']}, {s['descricao']}. "
                     f"Posso confirmar?")

    def propor_primeiro_slot(self, mensagens):
        return chamar("propor_reserva", slot_id=slots_oferecidos(mensagens)[0]["slot_id"])


class TestLigacaoCompleta(BaseAgente):
    def roteiro_feliz(self):
        telefone = self.conn.execute(
            "SELECT telefone FROM pacientes LIMIT 1").fetchone()[0][4:]
        return [
            falar("Boa noite! Esta ligação é gravada. Me diz seu telefone com DDD?"),
            chamar("buscar_paciente", telefone=telefone),
            chamar("consultar_agenda", especialidade="Ortopedia"),
            self.confirmar_primeiro_slot,
            self.propor_primeiro_slot,
            falar("Marcado. Até lá!"),
        ]

    def test_ligacao_do_inicio_ao_agendamento(self):
        pac = self.conn.execute("SELECT * FROM pacientes LIMIT 1").fetchone()
        a = self.agente(self.roteiro_feliz())
        a.dizer("Boa noite, queria marcar um ortopedista, mas só consigo depois das seis")
        a.dizer(f"É {pac['telefone'][4:]}")
        a.dizer("pode confirmar")

        r = a.resultado()
        self.assertTrue(r["agendou"], r["bloqueios"])
        self.assertEqual(r["bloqueios"], [])
        self.assertEqual(r["ferramentas"],
                         ["buscar_paciente", "consultar_agenda", "propor_reserva"])

    def test_a_restricao_falada_chega_na_consulta_sem_passar_pelo_modelo(self):
        pac = self.conn.execute("SELECT * FROM pacientes LIMIT 1").fetchone()
        a = self.agente(self.roteiro_feliz())
        a.dizer("Boa noite, queria um ortopedista, mas só consigo depois das seis")
        a.dizer(f"É {pac['telefone'][4:]}")

        self.assertEqual(a.restricao.hora_min, "18:00")
        oferta = slots_oferecidos(a.mensagens)
        self.assertTrue(oferta)
        for s in oferta:
            self.assertGreaterEqual(db.parse(s["inicio"]).strftime("%H:%M"), "18:00")

    def test_restricao_ambigua_pede_confirmacao_em_voz_alta(self):
        pac = self.conn.execute("SELECT * FROM pacientes LIMIT 1").fetchone()
        a = self.agente(self.roteiro_feliz())
        a.dizer("queria um ortopedista, só depois das seis")
        a.dizer(f"É {pac['telefone'][4:]}")
        agenda = next(r for r in _resultados(a.mensagens) if "slots" in r)
        self.assertIn("confirme_a_restricao", agenda)


class TestPortaoDeEscrita(BaseAgente):
    def _telefone(self):
        return self.conn.execute("SELECT telefone FROM pacientes LIMIT 1").fetchone()[0][4:]

    def _ate_a_oferta(self, ultima):
        return [
            falar("Boa noite. A ligação é gravada. Seu telefone?"),
            chamar("buscar_paciente", telefone=self._telefone()),
            chamar("consultar_agenda", especialidade="Ortopedia"),
            self.confirmar_primeiro_slot,
            ultima,
            falar("Certo."),
        ]

    def _rodar(self, ultima, resposta_do_paciente="pode confirmar"):
        a = self.agente(self._ate_a_oferta(ultima))
        a.dizer("queria um ortopedista depois das 18h")
        a.dizer(self._telefone())
        a.dizer(resposta_do_paciente)
        return a

    def test_slot_que_nunca_foi_oferecido_e_bloqueado(self):
        def propor_outro(mensagens):
            oferecidos = {s["slot_id"] for s in slots_oferecidos(mensagens)}
            intruso = self.conn.execute(
                "SELECT id FROM slots WHERE status='livre' AND id NOT IN "
                f"({','.join('?' * len(oferecidos))}) LIMIT 1",
                tuple(oferecidos)).fetchone()["id"]
            return chamar("propor_reserva", slot_id=intruso)

        a = self._rodar(propor_outro)
        self.assertFalse(a.resultado()["agendou"])
        self.assertEqual(a.resultado()["bloqueios"], ["R9_oferecido"])

    def test_paciente_hesitou_e_nada_e_escrito(self):
        a = self._rodar(self.propor_primeiro_slot, resposta_do_paciente="ahn, sei lá")
        self.assertFalse(a.resultado()["agendou"])
        self.assertEqual(a.resultado()["bloqueios"], ["R8_confirmacao"])
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM agendamentos WHERE origem='voz'"
                              ).fetchone()[0], 0)

    def test_a_frase_avaliada_na_R8_e_a_que_o_agente_falou(self):
        """Não é o que o modelo alega ter falado: é o texto do turno anterior."""
        def confirmar_errado(mensagens):
            s = slots_oferecidos(mensagens)[0]
            return falar(f"Consegui com {s['profissional']}, sexta-feira, "
                         f"dia 30 de dezembro, às 9h. Posso confirmar?")

        a = self.agente([
            falar("Boa noite. Seu telefone?"),
            chamar("buscar_paciente", telefone=self._telefone()),
            chamar("consultar_agenda", especialidade="Ortopedia"),
            confirmar_errado,
            self.propor_primeiro_slot,
            falar("Certo."),
        ])
        a.dizer("queria um ortopedista depois das 18h")
        a.dizer(self._telefone())
        a.dizer("pode confirmar")
        self.assertEqual(a.resultado()["bloqueios"], ["R8_confirmacao"])

    def test_sem_paciente_identificado_nao_marca(self):
        a = self.agente([chamar("propor_reserva", slot_id=1), falar("...")])
        a.dizer("marca aí")
        self.assertEqual(a.turnos[0].eventos[0]["resultado"]["erro"],
                         "paciente_nao_identificado")


class TestLGPD(BaseAgente):
    """§8 — o documento não pode sobreviver nem no trace nem no histórico."""

    def test_cpf_ditado_nao_aparece_no_trace(self):
        pac = self.conn.execute("SELECT * FROM pacientes LIMIT 1").fetchone()
        falado = ditar(pac["cpf"])
        a = self.agente([chamar("buscar_paciente", cpf=pac["cpf"]),
                         falar("Achei seu cadastro.")])
        a.dizer(f"Meu CPF é {falado}")
        trace = str(a.resultado()["transcricao"])
        self.assertIn(db.MASCARA, trace)
        self.assertNotIn(pac["cpf"], trace)
        self.assertNotIn("quatro quatro", trace)

    def test_cpf_sai_do_historico_que_vai_para_o_modelo(self):
        pac = self.conn.execute("SELECT * FROM pacientes LIMIT 1").fetchone()
        a = self.agente([chamar("buscar_paciente", cpf=pac["cpf"]),
                         falar("Achei.")])
        a.dizer(f"É {ditar(pac['cpf'])}")
        historico = " ".join(str(m.get("content")) for m in a.mensagens
                             if m.get("role") == "user")
        self.assertIn(db.MASCARA, historico)

    def test_paciente_nao_encontrado_mantem_a_fala_para_o_agente_reconferir(self):
        a = self.agente([chamar("buscar_paciente", cpf="12345678901"),
                         falar("Não achei, pode repetir?")])
        turno = a.dizer("É um dois três quatro cinco seis sete oito nove zero um")
        self.assertIn("leitura_para_confirmar", turno.eventos[0]["resultado"])


class TestEstadoDaLigacao(BaseAgente):
    def test_trocar_de_paciente_zera_o_pedido(self):
        """'na verdade era pra minha mãe' — a restrição do primeiro não vale
        para o segundo."""
        p1, p2 = self.conn.execute("SELECT * FROM pacientes LIMIT 2").fetchall()
        a = self.agente([
            chamar("buscar_paciente", telefone=p1["telefone"][4:]),
            falar("Achei seu cadastro."),
            chamar("buscar_paciente", telefone=p2["telefone"][4:]),
            falar("Achei o cadastro dela."),
        ])
        a.dizer(f"aqui é do {p1['telefone'][4:]}, quero ortopedista depois das 18h")
        self.assertEqual(a.restricao.hora_min, "18:00")
        a.dizer(f"na verdade era pra minha mãe, o telefone dela é {p2['telefone'][4:]}")
        self.assertEqual(a.paciente_id, p2["id"])
        self.assertTrue(a.restricao.vazia())

    def test_transferencia_por_risco(self):
        a = self.agente([
            chamar("transferir_para_humano", motivo="risco_clinico",
                   resumo="Paciente relatou dor no peito durante o agendamento."),
            falar("Vou te passar agora para uma pessoa da equipe."),
        ])
        a.dizer("tô com uma dor no peito forte desde ontem")
        r = a.resultado()
        self.assertTrue(r["transferiu"])
        self.assertEqual(r["motivo_transferencia"], "risco_clinico")
        self.assertFalse(r["agendou"])

    def test_limite_de_iteracoes_nao_trava_a_ligacao(self):
        a = self.agente([chamar("consultar_agenda", especialidade="Ortopedia")] * 8)
        turno = a.dizer("oi")
        self.assertEqual(turno.eventos[-1]["resultado"]["erro"], "excedeu_iteracoes")

    def test_ferramenta_desconhecida_vira_erro_e_nao_excecao(self):
        a = self.agente([chamar("cancelar_tudo"), falar("Desculpa, não consigo isso.")])
        turno = a.dizer("cancela tudo")
        self.assertEqual(turno.eventos[0]["resultado"]["erro"], "ferramenta_desconhecida")
        self.assertEqual(turno.fala_agente, "Desculpa, não consigo isso.")

    def test_resultado_traz_transcricao_e_custo(self):
        a = self.agente([falar("Boa noite!")])
        a.dizer("oi")
        r = a.resultado()
        self.assertEqual(r["transcricao"],
                         [{"papel": "paciente", "texto": "oi"},
                          {"papel": "agente", "texto": "Boa noite!"}])
        self.assertEqual(r["tokens_saida"], 30)


if __name__ == "__main__":
    unittest.main()
