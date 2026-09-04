"""Testes das 5 tools puras. stdlib apenas — `python3 -m unittest`.

O que estes testes protegem, em ordem de importância:
  1. double-booking é impossível (não improvável);
  2. reserva é idempotente — voz repete, rede cai, o paciente confirma duas vezes;
  3. restrição impossível devolve zero e uma alternativa verdadeira,
     em vez de abrir espaço para o modelo inventar horário.
"""
from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime

from clinica import db, tools
from tests.base import AGORA, DATA_BASE, BaseClinica

class TestBuscarPaciente(BaseClinica):
    def test_sem_parametro_recusa(self):
        r = tools.buscar_paciente(self.conn)
        self.assertEqual(r["erro"], "parametro_ausente")

    def test_por_cpf_e_nunca_vaza_o_cpf_inteiro(self):
        p = self.paciente()
        r = tools.buscar_paciente(self.conn, cpf=p["cpf"])
        self.assertTrue(r["encontrado"])
        self.assertEqual(r["paciente"]["id"], p["id"])
        self.assertNotIn(p["cpf"], str(r))          # LGPD: nada de dado cru pro LLM
        self.assertTrue(r["paciente"]["cpf_mascarado"].startswith("***.***."))

    def test_cpf_com_pontuacao_do_stt(self):
        p = self.paciente()
        c = p["cpf"]
        r = tools.buscar_paciente(self.conn, cpf=f"{c[:3]}.{c[3:6]}.{c[6:9]}-{c[9:]}")
        self.assertTrue(r["encontrado"])

    def test_cpf_com_digito_faltando(self):
        r = tools.buscar_paciente(self.conn, cpf="1234567890")
        self.assertEqual(r["erro"], "cpf_invalido")
        self.assertEqual(r["digitos_recebidos"], 10)

    def test_telefone_sem_ddi(self):
        p = self.paciente()
        r = tools.buscar_paciente(self.conn, telefone=p["telefone"][4:])   # o STT come o 55+DDD
        self.assertTrue(r["encontrado"])
        self.assertEqual(r["paciente"]["id"], p["id"])

    def test_nao_encontrado_nao_e_erro(self):
        r = tools.buscar_paciente(self.conn, telefone="11900000000")
        self.assertTrue(r["ok"])
        self.assertFalse(r["encontrado"])


class TestConsultarAgenda(BaseClinica):
    def test_especialidade_inexistente_devolve_o_que_existe(self):
        r = tools.consultar_agenda(self.conn, especialidade="Neurologia", agora=AGORA)
        self.assertEqual(r["erro"], "especialidade_inexistente")
        self.assertIn("Ortopedia", r["especialidades_disponiveis"])

    def test_nome_sem_acento_e_em_caixa_alta(self):
        for entrada in ("ortopedia", "ORTOPEDIA", "Ortopédia"):
            with self.subTest(entrada=entrada):
                r = tools.consultar_agenda(self.conn, especialidade=entrada, agora=AGORA)
                self.assertTrue(r["ok"])
                self.assertTrue(r["slots"])

    def test_restricao_de_hora_e_respeitada(self):
        r = tools.consultar_agenda(self.conn, especialidade="Ortopedia",
                                   hora_min="18:00", agora=AGORA, limite=10)
        self.assertTrue(r["slots"])
        for s in r["slots"]:
            self.assertGreaterEqual(db.parse(s["inicio"]).strftime("%H:%M"), "18:00")
            # só a Dra. Thaís atende à noite — a escassez é real, não decorativa
            self.assertEqual(s["profissional"], "Dra. Thaís Bittencourt")

    def test_limite_padrao_e_teto(self):
        self.assertLessEqual(
            len(tools.consultar_agenda(self.conn, especialidade="Dermatologia",
                                       agora=AGORA)["slots"]), 3)
        self.assertLessEqual(
            len(tools.consultar_agenda(self.conn, especialidade="Dermatologia",
                                       agora=AGORA, limite=999)["slots"]), 10)

    def test_restricao_impossivel_devolve_zero_e_uma_alternativa_verdadeira(self):
        r = tools.consultar_agenda(self.conn, especialidade="Ortopedia",
                                   hora_min="05:00", hora_max="05:30", agora=AGORA)
        self.assertTrue(r["ok"])
        self.assertEqual(r["total"], 0)
        self.assertEqual(r["motivo"], "nenhum_horario_na_restricao")
        self.assertIsNotNone(r["alternativa"])
        self.assertEqual(self.status_slot(r["alternativa"]["slot_id"]), "livre")

    def test_nunca_oferece_horario_no_passado(self):
        corte = datetime(2026, 1, 12, 12, 0)
        r = tools.consultar_agenda(self.conn, especialidade="Fisioterapia",
                                   agora=corte, limite=10)
        for s in r["slots"]:
            self.assertGreater(db.parse(s["inicio"]), corte)

    def test_filtro_por_dia_da_semana(self):
        r = tools.consultar_agenda(self.conn, especialidade="Dermatologia",
                                   dias_semana=[5], agora=AGORA, limite=10)
        for s in r["slots"]:
            self.assertEqual(db.parse(s["inicio"]).weekday(), 5)


class TestReservarHorario(BaseClinica):
    def test_reserva_ocupa_o_slot_e_devolve_frase_de_confirmacao(self):
        slot, p = self.slot_livre(), self.paciente()
        r = tools.reservar_horario(self.conn, slot_id=slot["slot_id"], paciente_id=p["id"],
                                   idempotency_key="lig-1", agora=AGORA)
        self.assertTrue(r["ok"])
        self.assertFalse(r["idempotente"])
        self.assertEqual(self.status_slot(slot["slot_id"]), "ocupado")
        self.assertIn(slot["profissional"], r["confirmacao"])
        self.assertIn("Ortopedia", r["confirmacao"])

    def test_mesma_chave_nao_cria_segundo_agendamento(self):
        slot, p = self.slot_livre(), self.paciente()
        a = tools.reservar_horario(self.conn, slot_id=slot["slot_id"], paciente_id=p["id"],
                                   idempotency_key="lig-2", agora=AGORA)
        b = tools.reservar_horario(self.conn, slot_id=slot["slot_id"], paciente_id=p["id"],
                                   idempotency_key="lig-2", agora=AGORA)
        self.assertTrue(b["idempotente"])
        self.assertEqual(a["agendamento"]["id"], b["agendamento"]["id"])
        total = self.conn.execute("SELECT count(*) FROM agendamentos WHERE origem='voz'").fetchone()[0]
        self.assertEqual(total, 1)

    def test_slot_ocupado_e_recusado_com_codigo_estavel(self):
        slot = self.slot_livre()
        p1, p2 = self.conn.execute("SELECT * FROM pacientes LIMIT 2").fetchall()
        tools.reservar_horario(self.conn, slot_id=slot["slot_id"], paciente_id=p1["id"],
                               idempotency_key="lig-3", agora=AGORA)
        r = tools.reservar_horario(self.conn, slot_id=slot["slot_id"], paciente_id=p2["id"],
                                   idempotency_key="lig-4", agora=AGORA)
        self.assertFalse(r["ok"])
        self.assertEqual(r["erro"], "slot_indisponivel")

    def test_double_booking_e_impossivel_no_banco(self):
        """A regra não mora no prompt nem na tool: mora no índice único parcial."""
        slot, p = self.slot_livre(), self.paciente()
        tools.reservar_horario(self.conn, slot_id=slot["slot_id"], paciente_id=p["id"],
                               idempotency_key="lig-5", agora=AGORA)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO agendamentos (slot_id, paciente_id, status, criado_em, "
                "idempotency_key) VALUES (?,?,'confirmado','2026-01-05 08:00','burlar')",
                (slot["slot_id"], p["id"]))

    def test_slot_no_passado(self):
        slot, p = self.slot_livre(), self.paciente()
        depois = db.parse(slot["inicio"])
        r = tools.reservar_horario(self.conn, slot_id=slot["slot_id"], paciente_id=p["id"],
                                   idempotency_key="lig-6", agora=depois)
        self.assertEqual(r["erro"], "slot_no_passado")

    def test_paciente_inexistente(self):
        r = tools.reservar_horario(self.conn, slot_id=self.slot_livre()["slot_id"],
                                   paciente_id=99999, idempotency_key="lig-7", agora=AGORA)
        self.assertEqual(r["erro"], "paciente_inexistente")

    def test_sem_chave_de_idempotencia_nao_escreve(self):
        r = tools.reservar_horario(self.conn, slot_id=self.slot_livre()["slot_id"],
                                   paciente_id=self.paciente()["id"],
                                   idempotency_key="", agora=AGORA)
        self.assertEqual(r["erro"], "chave_ausente")


class TestReagendar(BaseClinica):
    def _agendar(self, chave="orig"):
        slot, p = self.slot_livre(), self.paciente()
        r = tools.reservar_horario(self.conn, slot_id=slot["slot_id"], paciente_id=p["id"],
                                   idempotency_key=chave, agora=AGORA)
        return r["agendamento"], slot

    def test_move_liberando_o_slot_antigo(self):
        agend, antigo = self._agendar()
        novo = tools.consultar_agenda(self.conn, especialidade="Ortopedia",
                                      agora=AGORA, limite=2)["slots"][-1]
        r = tools.reagendar(self.conn, agendamento_id=agend["id"],
                            novo_slot_id=novo["slot_id"], idempotency_key="rea-1", agora=AGORA)
        self.assertTrue(r["ok"])
        self.assertEqual(self.status_slot(antigo["slot_id"]), "livre")
        self.assertEqual(self.status_slot(novo["slot_id"]), "ocupado")
        antigo_status = self.conn.execute(
            "SELECT status FROM agendamentos WHERE id=?", (agend["id"],)).fetchone()[0]
        self.assertEqual(antigo_status, "cancelado")

    def test_nao_atravessa_especialidade(self):
        agend, _ = self._agendar()
        outro = self.slot_livre("Dermatologia")
        r = tools.reagendar(self.conn, agendamento_id=agend["id"],
                            novo_slot_id=outro["slot_id"], idempotency_key="rea-2", agora=AGORA)
        self.assertEqual(r["erro"], "especialidade_divergente")

    def test_mesmo_slot(self):
        agend, antigo = self._agendar()
        r = tools.reagendar(self.conn, agendamento_id=agend["id"],
                            novo_slot_id=antigo["slot_id"], idempotency_key="rea-3", agora=AGORA)
        self.assertEqual(r["erro"], "mesmo_slot")

    def test_idempotente(self):
        agend, _ = self._agendar()
        novo = tools.consultar_agenda(self.conn, especialidade="Ortopedia",
                                      agora=AGORA, limite=2)["slots"][-1]
        a = tools.reagendar(self.conn, agendamento_id=agend["id"], novo_slot_id=novo["slot_id"],
                            idempotency_key="rea-4", agora=AGORA)
        b = tools.reagendar(self.conn, agendamento_id=agend["id"], novo_slot_id=novo["slot_id"],
                            idempotency_key="rea-4", agora=AGORA)
        self.assertTrue(b["idempotente"])
        self.assertEqual(a["agendamento"]["id"], b["agendamento"]["id"])

    def test_agendamento_inexistente(self):
        r = tools.reagendar(self.conn, agendamento_id=99999, novo_slot_id=1,
                            idempotency_key="rea-5", agora=AGORA)
        self.assertEqual(r["erro"], "agendamento_inexistente")


class TestTransferirParaHumano(BaseClinica):
    def test_risco_clinico_entra_como_alta(self):
        r = tools.transferir_para_humano(self.conn, motivo="risco_clinico",
                                         resumo="Paciente relatou dor no peito.", agora=AGORA)
        self.assertTrue(r["ok"])
        self.assertEqual(r["prioridade"], "alta")
        self.assertTrue(r["protocolo"].startswith("TR-"))

    def test_demais_motivos_sao_normais(self):
        r = tools.transferir_para_humano(self.conn, motivo="fora_de_escopo",
                                         resumo="Pediu diagnóstico.", agora=AGORA)
        self.assertEqual(r["prioridade"], "normal")

    def test_motivo_invalido(self):
        r = tools.transferir_para_humano(self.conn, motivo="tanto_faz",
                                         resumo="x", agora=AGORA)
        self.assertEqual(r["erro"], "motivo_invalido")

    def test_resumo_vazio(self):
        r = tools.transferir_para_humano(self.conn, motivo="frustracao",
                                         resumo="   ", agora=AGORA)
        self.assertEqual(r["erro"], "resumo_ausente")


if __name__ == "__main__":
    unittest.main()
