"""Atendimento em inglês. O teste que carrega a decisão de projeto é
`TestR8EmIngles`: sem a camada de idioma, uma frase inglesa correta é lida com
as tabelas em português e devolve zero entidades — a R8 então reprova toda
ligação em inglês, inclusive as certas. Não é buraco de segurança, é a
impossibilidade de atender no idioma.

O segundo teste que importa é `test_no_em_portugues_nao_e_recusa`: "no" é
negação em inglês e preposição em português. Uma lista de consentimento só,
somando as duas línguas, leria "pode ser no dia quinze" como recusa.
"""
from __future__ import annotations

import unittest
from datetime import date, datetime

from clinica import db, tools, validador
from clinica.agente import Agente
from clinica.idioma import EN, PT, detectar
from clinica.normalizador import (extrair_digitos, interpretar_restricao,
                                  normalizar_nascimento)
from clinica.provedor import ChamadaTool, ProvedorRoteirizado, Resposta
from clinica.validador import (ConfirmacaoVerbal, Intencao, extrair_entidades,
                               interpretar_resposta)
from tests.base import AGORA, BaseClinica

HOJE = date(2026, 9, 3)          # quinta-feira


class TestDeteccao(unittest.TestCase):
    def test_frase_em_portugues(self):
        self.assertIs(detectar("Oi, queria marcar um ortopedista"), PT)

    def test_frase_em_ingles(self):
        self.assertIs(detectar("Hi, I would like to book an appointment"), EN)

    def test_monossilabo_nao_troca_de_idioma(self):
        """"ok" existe nas duas línguas. Trocar de idioma porque o paciente
        respondeu uma monossílaba é pior do que errar a primeira frase: o
        agente muda de língua sozinho no meio da ligação."""
        self.assertIs(detectar("ok", atual=EN), EN)
        self.assertIs(detectar("ok", atual=PT), PT)

    def test_sem_marca_nenhuma_mantem_o_atual(self):
        self.assertIs(detectar("15 03 1980", atual=EN), EN)
        self.assertIs(detectar("", atual=PT), PT)

    def test_troca_de_verdade_e_respeitada(self):
        self.assertIs(detectar("actually I would prefer the morning", atual=PT), EN)
        self.assertIs(detectar("na verdade eu prefiro de manhã", atual=EN), PT)


class TestRestricaoEmIngles(unittest.TestCase):
    def r(self, texto):
        return interpretar_restricao(texto, HOJE, EN)

    def test_after_six_usa_a_mesma_heuristica_de_clinica(self):
        """"depois das seis" e "after six" são o mesmo chute: 18h, e marcado
        como ambíguo para o agente confirmar em voz alta."""
        r = self.r("I can only do after six")
        self.assertEqual(r.hora_min, "18:00")
        self.assertTrue(r.ambigua)

    def test_pm_remove_a_ambiguidade(self):
        r = self.r("only after 6pm")
        self.assertEqual(r.hora_min, "18:00")
        self.assertFalse(r.ambigua)

    def test_periodo_do_dia(self):
        r = self.r("in the morning")
        self.assertEqual((r.hora_min, r.hora_max), ("06:00", "11:59"))

    def test_periodo_mais_limite_sao_duas_informacoes(self):
        r = self.r("in the morning, before eleven")
        self.assertEqual((r.hora_min, r.hora_max), ("06:00", "11:00"))

    def test_entre(self):
        r = self.r("between two and four in the afternoon")
        self.assertEqual((r.hora_min, r.hora_max), ("14:00", "16:00"))

    def test_half_past(self):
        """A ordem inverte: "seis e meia" põe o meio depois, "half past six"
        põe antes."""
        self.assertEqual(self.r("at half past six").hora_min, "18:30")

    def test_hora_com_minuto_colado(self):
        self.assertEqual(self.r("at six thirty").hora_min, "18:30")

    def test_meio_dia(self):
        self.assertEqual(self.r("at noon").hora_min, "12:00")

    def test_dia_da_semana(self):
        self.assertEqual(self.r("next Tuesday").data_inicio, "2026-09-08")

    def test_amanha(self):
        self.assertEqual(self.r("tomorrow").data_inicio, "2026-09-04")

    def test_depois_de_amanha_nao_vira_amanha(self):
        """"day after tomorrow" contém "tomorrow" — a ordem de teste importa."""
        self.assertEqual(self.r("the day after tomorrow").data_inicio, "2026-09-05")

    def test_data_nas_duas_ordens(self):
        self.assertEqual(self.r("October fifteenth").data_inicio, "2026-10-15")
        self.assertEqual(self.r("the fifteenth of October").data_inicio, "2026-10-15")

    def test_good_evening_nao_e_restricao(self):
        """Mesmo bug do "boa noite", na outra língua: abre toda ligação."""
        self.assertIsNone(self.r("good evening").hora_min)

    def test_nascimento_nao_vira_filtro_de_agenda(self):
        self.assertIsNone(self.r("I was born on March fifteenth").data_inicio)

    def test_a_interpretacao_e_escrita_em_ingles(self):
        """O agente lê esta frase de volta em voz alta quando a leitura foi um
        chute. Em português, numa ligação em inglês, ela não serve."""
        self.assertEqual(self.r("in the morning").interpretacao,
                         "from 06:00, until 11:59")


class TestDigitosEDatas(unittest.TestCase):
    def test_telefone_ditado_em_ingles(self):
        self.assertEqual(
            extrair_digitos("one one nine six one two three zero zero zero zero", EN),
            "11961230000")

    def test_correcao_no_meio_do_ditado(self):
        self.assertEqual(extrair_digitos("four three, no, two", EN), "42")

    def test_nascimento_em_ingles(self):
        self.assertEqual(normalizar_nascimento("March fifteenth, nineteen eighty",
                                               HOJE, EN), "1980-03-15")

    def test_nascimento_iso_nos_dois_idiomas(self):
        self.assertEqual(normalizar_nascimento("1980-03-15", HOJE, EN), "1980-03-15")
        self.assertEqual(normalizar_nascimento("1980-03-15", HOJE, PT), "1980-03-15")


class TestConsentimento(unittest.TestCase):
    def test_sim_e_nao_em_ingles(self):
        self.assertEqual(interpretar_resposta("yes, please", EN), "sim")
        self.assertEqual(interpretar_resposta("no, not that one", EN), "nao")

    def test_hesitacao_nao_e_consentimento(self):
        self.assertEqual(interpretar_resposta("hmm, I'm not sure", EN), "nao")
        self.assertEqual(interpretar_resposta("well...", EN), "indefinido")

    def test_no_em_portugues_nao_e_recusa(self):
        """A colisão que obriga a separar as listas: "no" é negação em inglês
        e preposição em português."""
        self.assertEqual(interpretar_resposta("pode ser no dia quinze", PT), "sim")
        self.assertEqual(interpretar_resposta("no", EN), "nao")


class TestR8EmIngles(BaseClinica):
    """A R8 confere o que o agente falou em voz alta. Em inglês, com as
    tabelas em inglês."""

    def setUp(self):
        super().setUp()
        self.slot = self.slot_livre()
        self.inicio = db.parse(self.slot["inicio"])
        self.certa = (f"So that's {self.slot['profissional']}, "
                      f"{EN.descrever(self.inicio)}. Shall I confirm?")

    def julgar(self, frase, resposta="yes", idi=EN):
        i = Intencao(slot_id=self.slot["slot_id"], paciente_id=self.paciente()["id"],
                     idempotency_key="k", especialidade=self.slot["especialidade"],
                     slots_oferecidos=(self.slot["slot_id"],), idioma=idi,
                     confirmacao=ConfirmacaoVerbal(frase, resposta))
        return validador.validar(self.conn, i, agora=AGORA)

    def test_a_frase_certa_passa(self):
        self.assertTrue(self.julgar(self.certa).aprovado)

    def test_dia_da_semana_errado_e_bloqueado(self):
        errada = self.certa.replace(EN.nomes_dia[self.inicio.weekday()], "Monday")
        v = self.julgar(errada)
        self.assertFalse(v.aprovado)
        self.assertEqual(v.motivo(), "R8_confirmacao")

    def test_mes_errado_e_bloqueado(self):
        errada = self.certa.replace(EN.nomes_mes[self.inicio.month - 1], "December")
        self.assertFalse(self.julgar(errada).aprovado)

    def test_dia_do_mes_errado_e_bloqueado(self):
        errada = self.certa.replace(f"{EN.nomes_mes[self.inicio.month - 1]} "
                                    f"{self.inicio.day}",
                                    f"{EN.nomes_mes[self.inicio.month - 1]} "
                                    f"{self.inicio.day + 7}")
        self.assertFalse(self.julgar(errada).aprovado)

    def test_nao_disse_o_horario(self):
        muda = f"That's {self.slot['profissional']}, " \
               f"{EN.nomes_dia[self.inicio.weekday()]}. Confirm?"
        self.assertFalse(self.julgar(muda).aprovado)

    def test_paciente_recusou(self):
        self.assertFalse(self.julgar(self.certa, resposta="no, not that one").aprovado)

    def test_sem_a_camada_de_idioma_a_regra_falha_fechada(self):
        """A medida que justifica o projeto: a mesma frase correta, lida com as
        tabelas em português, devolve zero entidades — e a R8 reprova a
        ligação certa. Falha fechada, não aberta."""
        lido_em_pt = extrair_entidades(self.certa, PT)
        self.assertEqual(lido_em_pt,
                         {"hora": None, "dia_semana": None, "dia_mes": None,
                          "mes": None})
        self.assertFalse(self.julgar(self.certa, idi=PT).aprovado)

    def test_com_a_camada_as_entidades_aparecem(self):
        lido = extrair_entidades(self.certa, EN)
        self.assertEqual(lido["hora"], self.inicio.strftime("%H:%M"))
        self.assertEqual(lido["dia_semana"], self.inicio.weekday())
        self.assertEqual(lido["dia_mes"], self.inicio.day)
        self.assertEqual(lido["mes"], self.inicio.month)


class TestAgenteBilingue(BaseClinica):
    def montar(self, roteiro, **kw):
        return Agente(self.conn, ProvedorRoteirizado(roteiro), ligacao_id="b",
                      agora=AGORA, hoje=AGORA.date(), **kw)

    def test_troca_para_ingles_no_primeiro_turno(self):
        a = self.montar([Resposta(texto="Sure, one moment.")])
        self.assertIs(a.idioma, PT)
        a.dizer("Hi, I need to book an appointment with an orthopedist")
        self.assertIs(a.idioma, EN)

    def test_o_prompt_do_sistema_e_reescrito_e_nao_acrescentado(self):
        """Duas diretrizes de idioma contraditórias no mesmo histórico produzem
        resposta misturada — pior do que ter começado na língua errada."""
        a = self.montar([Resposta(texto="ok")])
        a.dizer("Hi, I would like to book an appointment")
        sistemas = [m for m in a.mensagens if m["role"] == "system"]
        self.assertEqual(len(sistemas), 1)
        self.assertIn("Speak English", sistemas[0]["content"])
        self.assertNotIn("Fale português", sistemas[0]["content"])

    def test_a_restricao_e_zerada_ao_trocar_de_idioma(self):
        """Ela foi lida com as tabelas da língua anterior. Mantê-la filtra a
        agenda por uma interpretação que o paciente nunca fez."""
        a = self.montar([Resposta(texto="ok"), Resposta(texto="ok")])
        a.dizer("só consigo depois das seis")
        self.assertEqual(a.restricao.hora_min, "18:00")
        a.dizer("actually, could you do it in the morning instead?")
        self.assertIs(a.idioma, EN)
        self.assertEqual((a.restricao.hora_min, a.restricao.hora_max),
                         ("06:00", "11:59"))

    def test_deteccao_desligavel(self):
        a = self.montar([Resposta(texto="ok")], detectar_idioma=False)
        a.dizer("Hi, I would like to book an appointment")
        self.assertIs(a.idioma, PT)

    def test_a_agenda_chega_ao_modelo_na_lingua_da_ligacao(self):
        """`descricao` é o que o agente lê de volta e a R8 confere. Entregá-la
        em português obriga o modelo a traduzir — e é a tradução, não o dado,
        que a R8 passa a validar."""
        a = self.montar([
            Resposta(chamadas=(ChamadaTool("c1", "consultar_agenda",
                                           {"especialidade": "Ortopedia"}),)),
            Resposta(texto="I have a few options.")])
        a.dizer("Hello, I need an orthopedist please")
        r = tools.consultar_agenda(self.conn, especialidade="Ortopedia",
                                   agora=AGORA, limite=1, idi=a.idioma)
        self.assertRegex(r["slots"][0]["descricao"],
                         r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), ")

    def test_saudacao_e_hora_no_prompt_saem_em_ingles(self):
        a = self.montar([Resposta(texto="ok")], idioma=EN)
        sistema = a.mensagens[0]["content"]
        self.assertIn("Good ", sistema)
        self.assertNotIn("Bom dia", sistema)


class TestCadastroEmIngles(BaseClinica):
    """Quem liga de fora não tem CPF — é a razão de o cadastro pedir só nome e
    telefone."""

    def test_cadastra_sem_documento(self):
        i = Intencao(slot_id=0, paciente_id=0, idempotency_key="en-1", idioma=EN,
                     confirmacao=ConfirmacaoVerbal(
                         "So that's Sarah Chen Miller, phone one one, nine six one "
                         "two three, zero zero zero zero. Is that right?", "yes"),
                     cadastro={"nome": "Sarah Chen Miller",
                               "telefone": "11961230000"})
        r = validador.executar_cadastro(self.conn, i, agora=AGORA)
        self.assertTrue(r["ok"], r.get("mensagem"))
        self.assertEqual(r["paciente"]["nome"], "Sarah Chen Miller")

    def test_a_recusa_em_ingles_tambem_bloqueia(self):
        i = Intencao(slot_id=0, paciente_id=0, idempotency_key="en-2", idioma=EN,
                     confirmacao=ConfirmacaoVerbal(
                         "So that's Sarah Chen Miller, phone one one nine six one "
                         "two three zero zero zero zero. Is that right?",
                         "no, that's wrong"),
                     cadastro={"nome": "Sarah Chen Miller",
                               "telefone": "11961230000"})
        r = validador.executar_cadastro(self.conn, i, agora=AGORA)
        self.assertFalse(r["ok"])
        self.assertEqual(r["regra"], "R8_confirmacao")


if __name__ == "__main__":
    unittest.main()
