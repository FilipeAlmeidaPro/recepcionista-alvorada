"""Testes da camada determinística.

O teste que importa mais é `test_agente_confirmou_dia_da_semana_errado`: todos
os argumentos estruturados estão certos, o slot existe e está livre, o paciente
disse "pode ser" — e a escrita é bloqueada mesmo assim, porque o que o agente
falou em voz alta não bate com o que ele ia gravar.
"""
from __future__ import annotations

import unittest

from clinica import db, validador
from clinica.normalizador import (Restricao, interpretar_restricao,
                                  ler_digitos)
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


class TestCadastro(BaseValidador):
    """R11/R12/R13 + a R8 outra vez, agora com as entidades da ficha.

    O teste que importa aqui é `test_agente_leu_um_cpf_diferente_do_que_ia_gravar`:
    os dados estão completos, o CPF é válido, o paciente disse sim — e a escrita
    é bloqueada porque o número lido em voz alta não é o número que ia para o
    banco. Uma ficha nascida assim não dá erro nenhum: só colide, anos depois,
    com o cadastro verdadeiro de outra pessoa."""

    NOME, CPF = "Joana Ribeiro Alves", "11144477735"
    DADOS = dict(nome=NOME, telefone="11987650000", cpf=CPF,
                 nascimento="quinze de março de mil novecentos e oitenta")

    def fala(self, nome=None, cpf=None):
        return (f"Então: {nome or self.NOME}, CPF "
                f"{ler_digitos(cpf or self.CPF, 'cpf')}, nascido em quinze do "
                f"três de oitenta. Confirma?")

    def cadastro(self, *, resposta="isso", chave="lig-1", frase=None, **muda):
        return Intencao(
            slot_id=0, paciente_id=0, idempotency_key=chave,
            confirmacao=ConfirmacaoVerbal(frase if frase is not None else self.fala(),
                                          resposta),
            cadastro={**self.DADOS, **muda})

    def executar(self, **kw):
        return validador.executar_cadastro(self.conn, self.cadastro(**kw), agora=AGORA)

    def recusa(self, regra, **kw):
        r = self.executar(**kw)
        self.assertFalse(r["ok"], f"esperava recusa por {regra}")
        self.assertEqual(r["regra"], regra)
        return r

    # --- caminho feliz -------------------------------------------------------

    def test_dados_completos_e_confirmados_gravam(self):
        r = self.executar()
        self.assertTrue(r["ok"])
        self.assertEqual(r["paciente"]["nome"], self.NOME)

    def test_a_data_falada_chega_normalizada_ao_banco(self):
        """O modelo mandou "quinze de março de mil novecentos e oitenta"; quem
        converteu para data foi o código."""
        self.executar()
        self.assertEqual(self.conn.execute(
            "SELECT nascimento FROM pacientes WHERE cpf=?", (self.CPF,)).fetchone()[0],
            "1980-03-15")

    def test_veredito_nomeia_as_regras_de_cadastro(self):
        v = self.executar()["veredito"]
        self.assertEqual(set(v["regras_ok"]),
                         {"R1_chave", "R11_dados", "R12_cpf", "R13_duplicado",
                          "R8_confirmacao"})

    # --- R8: o que foi dito em voz alta --------------------------------------

    def test_agente_leu_um_cpf_diferente_do_que_ia_gravar(self):
        r = self.recusa("R8_confirmacao", frase=self.fala(cpf="52998224725"))
        self.assertIn("CPF lido em voz alta",
                      " ".join(r["veredito"]["violacoes"][0]["divergencias"]))
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM pacientes WHERE cpf=?", (self.CPF,)).fetchone())

    def test_agente_nao_leu_o_nome_de_volta(self):
        self.recusa("R8_confirmacao", frase=f"Confirma o CPF "
                                            f"{ler_digitos(self.CPF, 'cpf')}?")

    def test_paciente_disse_nao(self):
        self.recusa("R8_confirmacao", resposta="não, tá errado")

    def test_hesitacao_nao_e_consentimento(self):
        self.recusa("R8_confirmacao", resposta="hmm, sei lá")

    def test_silencio_nao_e_consentimento(self):
        self.recusa("R8_confirmacao", resposta="")

    def test_sem_turno_de_confirmacao_nenhum(self):
        i = self.cadastro()
        r = validador.executar_cadastro(
            self.conn, Intencao(slot_id=0, paciente_id=0, idempotency_key="k",
                                confirmacao=None, cadastro=i.cadastro), agora=AGORA)
        self.assertEqual(r["regra"], "R8_confirmacao")

    # --- R11: dados incompletos ----------------------------------------------

    def test_R11_nome_incompleto(self):
        r = self.recusa("R11_dados", nome="Joana")
        self.assertIn("nome", r["veredito"]["violacoes"][0]["faltando"])

    def test_R11_nascimento_ilegivel(self):
        r = self.recusa("R11_dados", nascimento="ah, não lembro")
        self.assertIn("nascimento", r["veredito"]["violacoes"][0]["faltando"])

    def test_R11_telefone_curto(self):
        self.recusa("R11_dados", telefone="9876")

    def test_R11_cpf_incompleto(self):
        self.recusa("R11_dados", cpf="1114447")

    # --- R12: dígito verificador ---------------------------------------------

    def test_R12_cpf_com_digito_invalido(self):
        """O modelo não tem como saber se um CPF fecha, e não deveria tentar."""
        self.recusa("R12_cpf", cpf="11144477700",
                    frase=self.fala(cpf="11144477700"))

    def test_R12_cpf_de_digitos_repetidos(self):
        self.recusa("R12_cpf", cpf="11111111111",
                    frase=self.fala(cpf="11111111111"))

    # --- R13: já existe ------------------------------------------------------

    def test_R13_cpf_de_outra_pessoa(self):
        self.executar()
        r = self.recusa("R13_duplicado", chave="lig-2", nome="Carlos Mendes Souza",
                        frase=self.fala(nome="Carlos Mendes Souza"))
        self.assertEqual(r["veredito"]["violacoes"][0]["nome_no_cadastro"],
                         self.NOME)

    def test_R13_telefone_ja_cadastrado(self):
        tel = self.paciente()["telefone"]
        self.recusa("R13_duplicado", telefone=tel)

    def test_repetir_o_mesmo_cadastro_e_idempotente_e_nao_erro(self):
        """A rede caiu, ou o paciente confirmou duas vezes. Devolver
        "esse CPF já está cadastrado" para a própria pessoa que acabou de
        ditá-lo é um bug, não uma proteção."""
        a = self.executar()
        b = self.executar(chave="lig-2")
        self.assertTrue(b["ok"])
        self.assertTrue(b["idempotente"])
        self.assertEqual(a["paciente"]["id"], b["paciente"]["id"])

    def test_nenhuma_recusa_deixa_ficha_no_banco(self):
        antes = self.conn.execute("SELECT count(*) FROM pacientes").fetchone()[0]
        for kw in ({"nome": "Joana"}, {"cpf": "11144477700",
                                       "frase": self.fala(cpf="11144477700")},
                   {"resposta": "não"}, {"nascimento": "sei lá"},
                   {"frase": "Confirma?"}, {"chave": ""}):
            self.assertFalse(self.executar(**kw)["ok"])
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM pacientes").fetchone()[0], antes)

    # --- R1 ------------------------------------------------------------------

    def test_R1_sem_chave_de_idempotencia(self):
        self.recusa("R1_chave", chave="")
