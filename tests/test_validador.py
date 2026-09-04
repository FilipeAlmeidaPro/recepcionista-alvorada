"""Testes da camada determinística.

O teste que importa mais é `test_agente_confirmou_dia_da_semana_errado`: todos
os argumentos estruturados estão certos, o slot existe e está livre, o paciente
disse "pode ser" — e a escrita é bloqueada mesmo assim, porque o que o agente
falou em voz alta não bate com o que ele ia gravar.
"""
from __future__ import annotations

import unittest

from clinica import db, validador
from clinica.normalizador import Restricao, interpretar_restricao
from clinica.validador import ConfirmacaoVerbal, Intencao
from tests.base import AGORA, BaseClinica


class BaseValidador(BaseClinica):
    def frase(self, slot, **erro):
        """Frase de confirmação do agente. Os kwargs injetam alucinação."""
        inicio = db.parse(slot["inicio"])
        ds = erro.get("dia_semana", inicio.weekday())
        dia = erro.get("dia", inicio.day)
        mes = erro.get("mes", inicio.month)
        h, m = erro.get("hora", inicio.hour), erro.get("minuto", inicio.minute)
        prof = erro.get("profissional", slot["profissional"])
        hora = f"{h}h" if m == 0 else f"{h}h{m:02d}"
        return (f"Então fica assim: {prof}, {db.DIAS[ds]}, dia {dia} de "
                f"{db.MESES[mes - 1]}, às {hora}. Confirma?")

    def intencao(self, slot=None, *, resposta="pode ser", chave="lig-1",
                 erro_na_frase=None, **campos):
        slot = slot or self.slot_livre()
        return slot, Intencao(
            slot_id=campos.pop("slot_id", slot["slot_id"]),
            paciente_id=campos.pop("paciente_id", self.paciente()["id"]),
            idempotency_key=chave,
            especialidade=campos.pop("especialidade", slot["especialidade"]),
            restricao=campos.pop("restricao", None),
            confirmacao=ConfirmacaoVerbal(self.frase(slot, **(erro_na_frase or {})),
                                          resposta),
            **campos)

    def total_agendamentos(self):
        return self.conn.execute(
            "SELECT count(*) FROM agendamentos WHERE origem='voz'").fetchone()[0]


class TestCaminhoFeliz(BaseValidador):
    def test_intencao_completa_e_aprovada_e_escreve(self):
        slot, intencao = self.intencao()
        r = validador.executar_reserva(self.conn, intencao, agora=AGORA)
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["veredito"]["aprovado"])
        self.assertEqual(self.status_slot(slot["slot_id"]), "ocupado")

    def test_veredito_nomeia_todas_as_regras_que_passaram(self):
        _slot, intencao = self.intencao(restricao=interpretar_restricao("de manhã"))
        v = validador.validar(self.conn, intencao, agora=AGORA)
        self.assertIn("R8_confirmacao", v.regras_ok)
        self.assertIn("R6_especialidade", v.regras_ok)
        self.assertIsNone(v.motivo())

    def test_restricao_declarada_e_respeitada_passa(self):
        slot = self.slot_livre("Ortopedia", hora_min="18:00")
        _s, intencao = self.intencao(slot, restricao=interpretar_restricao("depois das 18h"))
        self.assertTrue(validador.validar(self.conn, intencao, agora=AGORA).aprovado)


class TestAlucinacaoDeEntidade(BaseValidador):
    """R8 — o agente falou uma coisa e ia gravar outra."""

    def _bloqueado(self, **erro_na_frase):
        slot, intencao = self.intencao(erro_na_frase=erro_na_frase)
        r = validador.executar_reserva(self.conn, intencao, agora=AGORA)
        self.assertFalse(r["ok"])
        self.assertEqual(r["regra"], "R8_confirmacao")
        # e, principalmente: nada foi escrito
        self.assertEqual(self.status_slot(slot["slot_id"]), "livre")
        self.assertEqual(self.total_agendamentos(), 0)
        return r

    def test_agente_confirmou_dia_da_semana_errado(self):
        slot = self.slot_livre()
        outro_dia = (db.parse(slot["inicio"]).weekday() + 2) % 7
        slot2, intencao = self.intencao(slot, erro_na_frase={"dia_semana": outro_dia})
        r = validador.executar_reserva(self.conn, intencao, agora=AGORA)
        self.assertFalse(r["ok"])
        self.assertEqual(r["regra"], "R8_confirmacao")
        self.assertTrue(any("dia da semana" in d
                            for d in r["veredito"]["violacoes"][0]["divergencias"]))
        self.assertEqual(self.status_slot(slot["slot_id"]), "livre")

    def test_agente_confirmou_hora_errada(self):
        r = self._bloqueado(hora=7, minuto=0)
        self.assertTrue(any("o slot é" in d
                            for d in r["veredito"]["violacoes"][0]["divergencias"]))

    def test_agente_confirmou_dia_do_mes_errado(self):
        self._bloqueado(dia=28)

    def test_agente_confirmou_o_profissional_errado(self):
        self._bloqueado(profissional="Dra. Larissa Nakamura")

    def test_agente_nao_disse_o_horario_em_voz_alta(self):
        slot = self.slot_livre()
        _s, intencao = self.intencao(slot)
        muda = Intencao(**{**intencao.__dict__,
                           "confirmacao": ConfirmacaoVerbal(
                               "Consegui uma vaga pra você. Confirma?", "sim")})
        r = validador.executar_reserva(self.conn, muda, agora=AGORA)
        self.assertFalse(r["ok"])
        self.assertEqual(r["regra"], "R8_confirmacao")


class TestConsentimento(BaseValidador):
    def test_paciente_disse_nao(self):
        slot, intencao = self.intencao(resposta="não, espera")
        r = validador.executar_reserva(self.conn, intencao, agora=AGORA)
        self.assertEqual(r["regra"], "R8_confirmacao")
        self.assertEqual(self.status_slot(slot["slot_id"]), "livre")

    def test_hesitacao_nao_e_consentimento(self):
        _slot, intencao = self.intencao(resposta="hmm, sei lá")
        self.assertFalse(validador.validar(self.conn, intencao, agora=AGORA).aprovado)

    def test_silencio_nao_e_consentimento(self):
        _slot, intencao = self.intencao(resposta="")
        self.assertFalse(validador.validar(self.conn, intencao, agora=AGORA).aprovado)

    def test_sem_turno_de_confirmacao_nenhum(self):
        slot = self.slot_livre()
        intencao = Intencao(slot_id=slot["slot_id"], paciente_id=self.paciente()["id"],
                            idempotency_key="lig-x", especialidade="Ortopedia")
        v = validador.validar(self.conn, intencao, agora=AGORA)
        self.assertEqual(v.motivo(), "R8_confirmacao")

    def test_leitura_da_resposta(self):
        for texto, esperado in [("sim", "sim"), ("pode ser", "sim"), ("isso mesmo", "sim"),
                                ("tá bom", "sim"), ("perfeito", "sim"),
                                ("não", "nao"), ("não, espera", "nao"),
                                ("prefiro outro", "nao"),
                                ("hmm", "indefinido"), ("", "indefinido")]:
            with self.subTest(texto=texto):
                self.assertEqual(validador.interpretar_resposta(texto), esperado)


class TestRegrasDeNegocio(BaseValidador):
    def test_R7_horario_fora_da_restricao_declarada(self):
        slot = self.slot_livre("Ortopedia", hora_max="12:00")
        _s, intencao = self.intencao(slot, restricao=interpretar_restricao("só depois das 18h"))
        v = validador.validar(self.conn, intencao, agora=AGORA)
        self.assertEqual(v.motivo(), "R7_restricao")

    def test_R6_profissional_nao_atende_a_especialidade_pedida(self):
        slot = self.slot_livre("Dermatologia")
        _s, intencao = self.intencao(slot, especialidade="Ortopedia")
        self.assertEqual(validador.validar(self.conn, intencao, agora=AGORA).motivo(),
                         "R6_especialidade")

    def test_R5_slot_ja_ocupado(self):
        slot, intencao = self.intencao()
        validador.executar_reserva(self.conn, intencao, agora=AGORA)
        _s, outra = self.intencao(slot, chave="lig-2")
        self.assertEqual(validador.validar(self.conn, outra, agora=AGORA).motivo(),
                         "R5_disponivel")

    def test_R4_slot_no_passado(self):
        slot, intencao = self.intencao()
        depois = db.parse(slot["inicio"])
        self.assertEqual(validador.validar(self.conn, intencao, agora=depois).motivo(),
                         "R4_futuro")

    def test_R3_slot_inexistente(self):
        _s, intencao = self.intencao(slot_id=999999)
        self.assertEqual(validador.validar(self.conn, intencao, agora=AGORA).motivo(),
                         "R3_slot")

    def test_R2_paciente_inexistente(self):
        _s, intencao = self.intencao(paciente_id=999999)
        self.assertEqual(validador.validar(self.conn, intencao, agora=AGORA).motivo(),
                         "R2_paciente")

    def test_R1_sem_chave_de_idempotencia(self):
        _s, intencao = self.intencao(chave="")
        self.assertEqual(validador.validar(self.conn, intencao, agora=AGORA).motivo(),
                         "R1_chave")


class TestSlotOferecido(BaseValidador):
    """R9 — o modelo não inventa horário nem quando o id existe no banco."""

    def test_slot_que_nunca_foi_oferecido_e_recusado(self):
        oferecido = self.slot_livre()
        outro = self.conn.execute(
            "SELECT id FROM slots WHERE status='livre' AND id != ? LIMIT 1",
            (oferecido["slot_id"],)).fetchone()["id"]
        _s, intencao = self.intencao(oferecido, slot_id=outro,
                                     slots_oferecidos=(oferecido["slot_id"],))
        v = validador.validar(self.conn, intencao, agora=AGORA)
        self.assertEqual(v.motivo(), "R9_oferecido")
        self.assertEqual(self.status_slot(outro), "livre")

    def test_slot_oferecido_passa(self):
        slot, intencao = self.intencao(slots_oferecidos=None)
        oferta = (slot["slot_id"],)
        _s, com_oferta = self.intencao(slot, chave="lig-of", slots_oferecidos=oferta)
        self.assertIn("R9_oferecido",
                      validador.validar(self.conn, com_oferta, agora=AGORA).regras_ok)


class TestCancelamento(BaseValidador):
    """Cancelar não tem desfazer barato: quem cancela por engano perde a vaga
    para o próximo da fila. Mesmo portão da reserva."""

    def _reservar(self, chave="orig"):
        slot, intencao = self.intencao(chave=chave)
        validador.executar_reserva(self.conn, intencao, agora=AGORA)
        agend = self.conn.execute(
            "SELECT id FROM agendamentos WHERE idempotency_key=?", (chave,)).fetchone()["id"]
        return slot, agend

    def _intencao_cancelar(self, slot, agend, *, chave="can-1", resposta="isso, pode cancelar",
                           erro_na_frase=None):
        frase = self.frase(slot, **(erro_na_frase or {})).replace(
            "Então fica assim", "Vou cancelar sua consulta com")
        return Intencao(slot_id=0, paciente_id=self.paciente()["id"],
                        idempotency_key=chave, agendamento_id=agend,
                        confirmacao=ConfirmacaoVerbal(frase, resposta))

    def test_cancela_e_libera_o_horario(self):
        slot, agend = self._reservar()
        r = validador.executar_cancelamento(
            self.conn, self._intencao_cancelar(slot, agend), agora=AGORA)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.status_slot(slot["slot_id"]), "livre")
        self.assertEqual(self.conn.execute(
            "SELECT status FROM agendamentos WHERE id=?", (agend,)).fetchone()[0],
            "cancelado")

    def test_sem_confirmacao_verbal_nao_cancela(self):
        slot, agend = self._reservar()
        intencao = Intencao(slot_id=0, paciente_id=self.paciente()["id"],
                            idempotency_key="can-2", agendamento_id=agend)
        r = validador.executar_cancelamento(self.conn, intencao, agora=AGORA)
        self.assertEqual(r["regra"], "R8_confirmacao")
        self.assertEqual(self.status_slot(slot["slot_id"]), "ocupado")

    def test_confirmar_a_consulta_errada_nao_cancela(self):
        slot, agend = self._reservar()
        r = validador.executar_cancelamento(
            self.conn, self._intencao_cancelar(slot, agend, erro_na_frase={"hora": 7}),
            agora=AGORA)
        self.assertEqual(r["regra"], "R8_confirmacao")
        self.assertEqual(self.status_slot(slot["slot_id"]), "ocupado")

    def test_paciente_disse_nao(self):
        slot, agend = self._reservar()
        r = validador.executar_cancelamento(
            self.conn, self._intencao_cancelar(slot, agend, resposta="não, espera"),
            agora=AGORA)
        self.assertEqual(r["regra"], "R8_confirmacao")
        self.assertEqual(self.status_slot(slot["slot_id"]), "ocupado")

    def test_idempotente(self):
        slot, agend = self._reservar()
        i = self._intencao_cancelar(slot, agend, chave="can-3")
        a = validador.executar_cancelamento(self.conn, i, agora=AGORA)
        b = validador.executar_cancelamento(self.conn, i, agora=AGORA)
        self.assertTrue(a["ok"] and b["ok"])
        self.assertTrue(b["idempotente"])

    def test_agendamento_inexistente(self):
        r = validador.executar_cancelamento(
            self.conn, Intencao(slot_id=0, paciente_id=1, idempotency_key="can-4",
                                agendamento_id=99999), agora=AGORA)
        self.assertEqual(r["regra"], "R10_origem")


class TestReagendamento(BaseValidador):
    def _reservar(self):
        slot, intencao = self.intencao(chave="orig")
        validador.executar_reserva(self.conn, intencao, agora=AGORA)
        agend = self.conn.execute(
            "SELECT id FROM agendamentos WHERE idempotency_key='orig'").fetchone()["id"]
        return slot, agend

    def test_move_quando_a_confirmacao_bate(self):
        antigo, agend = self._reservar()
        destino = self.slot_livre()
        intencao = Intencao(slot_id=destino["slot_id"], paciente_id=0,
                            idempotency_key="rea-1", agendamento_id=agend,
                            confirmacao=ConfirmacaoVerbal(self.frase(destino), "isso"))
        r = validador.executar_reagendamento(self.conn, intencao, agora=AGORA)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.status_slot(antigo["slot_id"]), "livre")
        self.assertEqual(self.status_slot(destino["slot_id"]), "ocupado")

    def test_confirmacao_divergente_tambem_bloqueia_reagendamento(self):
        antigo, agend = self._reservar()
        destino = self.slot_livre()
        intencao = Intencao(slot_id=destino["slot_id"], paciente_id=0,
                            idempotency_key="rea-2", agendamento_id=agend,
                            confirmacao=ConfirmacaoVerbal(
                                self.frase(destino, hora=7, minuto=0), "isso"))
        r = validador.executar_reagendamento(self.conn, intencao, agora=AGORA)
        self.assertFalse(r["ok"])
        self.assertEqual(r["regra"], "R8_confirmacao")
        self.assertEqual(self.status_slot(antigo["slot_id"]), "ocupado")   # nada se moveu

    def test_origem_inexistente(self):
        destino = self.slot_livre()
        intencao = Intencao(slot_id=destino["slot_id"], paciente_id=0,
                            idempotency_key="rea-3", agendamento_id=99999)
        r = validador.executar_reagendamento(self.conn, intencao, agora=AGORA)
        self.assertEqual(r["regra"], "R10_origem")


if __name__ == "__main__":
    unittest.main()
