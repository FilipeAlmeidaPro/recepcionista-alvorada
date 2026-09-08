"""Testes do normalizador PT-BR. Sem banco, sem LLM — só entrada suja e saída limpa."""
from __future__ import annotations

import unittest
from datetime import date

from clinica import db
from clinica.idioma import EN, PT
from clinica.normalizador import (Restricao, casar_especialidade, casar_nome,
                                  cpf_valido, extrair_digitos,
                                  interpretar_restricao, ler_digitos,
                                  limpar_para_voz, normalizar_nascimento)

HOJE = date(2026, 9, 3)   # quinta-feira


class TestDigitosDitados(unittest.TestCase):
    def test_correcao_no_meio_da_fala(self):
        self.assertEqual(extrair_digitos("quatro, três... não, dois"), "42")
        self.assertEqual(extrair_digitos("zero zero, nove, na verdade oito"), "008")

    def test_meia_vale_seis(self):
        self.assertEqual(extrair_digitos("meia meia sete oito"), "6678")

    def test_compostos_e_digitos_misturados(self):
        self.assertEqual(extrair_digitos("vinte e três, zero um"), "2301")
        self.assertEqual(extrair_digitos("5 1 1 9 8 7"), "511987")

    def test_cpf_ditado_inteiro(self):
        falado = ("um um um, quatro quatro quatro, sete sete sete, "
                  "trinta e cinco")
        self.assertEqual(extrair_digitos(falado), "11144477735")
        self.assertTrue(cpf_valido(extrair_digitos(falado)))

    def test_cpf_invalido_e_recusado(self):
        self.assertFalse(cpf_valido("12345678901"))
        self.assertFalse(cpf_valido("11111111111"))
        self.assertFalse(cpf_valido("123"))


class TestRestricaoHoraria(unittest.TestCase):
    def test_depois_das_seis_vira_dezoito_e_se_declara_ambiguo(self):
        r = interpretar_restricao("só consigo depois das seis", HOJE)
        self.assertEqual(r.hora_min, "18:00")
        self.assertTrue(r.ambigua)      # é um chute bom — o agente tem que confirmar

    def test_periodo_explicito_remove_a_ambiguidade(self):
        r = interpretar_restricao("depois das seis da tarde", HOJE)
        self.assertEqual(r.hora_min, "18:00")
        self.assertFalse(r.ambigua)

    def test_manha_nao_vira_tarde(self):
        self.assertEqual(interpretar_restricao("às oito da manhã", HOJE).hora_min, "08:00")

    def test_antes_das_cinco(self):
        r = interpretar_restricao("antes das cinco", HOJE)
        self.assertEqual((r.hora_min, r.hora_max), (None, "17:00"))

    def test_faixa(self):
        r = interpretar_restricao("entre duas e quatro da tarde", HOJE)
        self.assertEqual((r.hora_min, r.hora_max), ("14:00", "16:00"))
        self.assertFalse(r.ambigua)     # o "da tarde" fecha as duas pontas

    def test_periodo_solto(self):
        self.assertEqual(interpretar_restricao("só à noite", HOJE).hora_min, "18:00")
        self.assertEqual(interpretar_restricao("de manhã", HOJE).hora_max, "11:59")

    def test_minutos(self):
        self.assertEqual(interpretar_restricao("depois das seis e meia", HOJE).hora_min, "18:30")
        self.assertEqual(interpretar_restricao("às 18h30", HOJE).hora_min, "18:30")

    def test_periodo_usado_na_hora_nao_vira_faixa_tambem(self):
        """Regressão: o "da tarde" de "depois das seis da tarde" era usado duas
        vezes — para desambiguar 6→18h e, de novo, como faixa do período. Saía
        18:00–17:59: uma restrição impossível, com zero horários por definição,
        e a suíte inteira passando."""
        r = interpretar_restricao("depois das seis da tarde", HOJE)
        self.assertEqual((r.hora_min, r.hora_max), ("18:00", None))
        r = interpretar_restricao("a partir das sete da noite", HOJE)
        self.assertEqual((r.hora_min, r.hora_max), ("19:00", None))

    def test_nenhuma_restricao_falada_pode_ser_impossivel(self):
        """Propriedade, não exemplo: se hora_min > hora_max, a consulta devolve
        zero por construção e o agente fica sem nada verdadeiro para oferecer."""
        falas = [
            "depois das seis da tarde", "a partir das sete da noite",
            "de manhã, antes das onze", "de manhã, depois das nove",
            "só consigo depois das seis", "antes das cinco", "de manhã",
            "só à noite", "entre duas e quatro da tarde", "às oito da manhã",
            "ao meio-dia", "às 18h30", "depois das seis e meia", "até as onze",
            "de tarde, depois das duas", "à noite, antes das oito",
            "pela manhã", "só consigo depois dar seis", "entre nove e onze",
        ]
        for fala in falas:
            with self.subTest(fala=fala):
                r = interpretar_restricao(fala, HOJE)
                if r.hora_min and r.hora_max:
                    self.assertLessEqual(
                        r.hora_min, r.hora_max,
                        f"«{fala}» virou {r.hora_min}–{r.hora_max}, impossível")

    def test_periodo_combina_com_limite_explicito(self):
        """'de manhã, antes das onze' são duas informações, não uma. Sem isto a
        restrição saía só como 'até as 11h' e 7h da manhã do dia seguinte
        passaria como se servisse."""
        r = interpretar_restricao("Prefiro de manhã, antes das onze.", HOJE)
        self.assertEqual((r.hora_min, r.hora_max), ("06:00", "11:00"))
        r = interpretar_restricao("de manhã, depois das nove", HOJE)
        self.assertEqual((r.hora_min, r.hora_max), ("09:00", "11:59"))

    def test_palavra_de_ligacao_trocada_pelo_stt(self):
        """Achado na suíte de áudio: o Whisper transcreveu 'depois DAR seis'.
        Em texto isso nunca aparece."""
        for fala in ("só consigo depois dar seis", "depois dos seis",
                     "a partir dar sete da noite"):
            with self.subTest(fala=fala):
                self.assertIsNotNone(interpretar_restricao(fala, HOJE).hora_min)

    def test_meio_dia(self):
        self.assertEqual(interpretar_restricao("ao meio-dia", HOJE).hora_min, "12:00")


class TestRestricaoTemporal(unittest.TestCase):
    def test_relativas(self):
        self.assertEqual(interpretar_restricao("amanhã", HOJE).data_inicio, "2026-09-04")
        self.assertEqual(interpretar_restricao("depois de amanhã", HOJE).data_inicio, "2026-09-05")

    def test_dia_da_semana_que_vem(self):
        # hoje é quinta 03/09; "quinta que vem" é a da semana seguinte, não hoje
        self.assertEqual(interpretar_restricao("quinta que vem", HOJE).data_inicio, "2026-09-10")
        self.assertEqual(interpretar_restricao("terça que vem", HOJE).data_inicio, "2026-09-08")
        self.assertEqual(interpretar_restricao("próxima segunda", HOJE).data_inicio, "2026-09-07")

    def test_semana_que_vem_e_uma_faixa(self):
        r = interpretar_restricao("semana que vem", HOJE)
        self.assertEqual((r.data_inicio, r.data_fim), ("2026-09-07", "2026-09-13"))

    def test_dia_do_mes(self):
        self.assertEqual(interpretar_restricao("dia quinze", HOJE).data_inicio, "2026-09-15")
        self.assertEqual(interpretar_restricao("dia quinze do mês que vem", HOJE).data_inicio,
                         "2026-10-15")
        self.assertEqual(interpretar_restricao("quinze de outubro", HOJE).data_inicio,
                         "2026-10-15")

    def test_dia_ja_passou_vai_para_o_mes_seguinte(self):
        self.assertEqual(interpretar_restricao("dia dois", HOJE).data_inicio, "2026-10-02")

    def test_dias_da_semana_soltos_viram_preferencia(self):
        r = interpretar_restricao("terça ou quinta", HOJE)
        self.assertEqual(r.dias_semana, (1, 3))
        self.assertIsNone(r.data_inicio)

    def test_hora_e_data_juntas(self):
        r = interpretar_restricao("quinta que vem depois das seis", HOJE)
        self.assertEqual((r.hora_min, r.data_inicio), ("18:00", "2026-09-10"))


class TestFiltros(unittest.TestCase):
    def test_como_filtros_omite_o_que_nao_foi_dito(self):
        f = interpretar_restricao("depois das 18h", HOJE).como_filtros()
        self.assertEqual(f, {"hora_min": "18:00"})

    def test_saudacao_nao_e_restricao(self):
        """Toda ligação abre com "boa tarde" ou "boa noite". Nenhuma delas é
        uma restrição de horário — e essa confusão era silenciosa."""
        for saudacao in ("boa noite, tudo bem?", "boa tarde!", "bom dia",
                         "boa tarde, queria marcar uma consulta"):
            with self.subTest(saudacao=saudacao):
                self.assertTrue(interpretar_restricao(saudacao, HOJE).vazia())

    def test_restricao_vazia(self):
        self.assertTrue(Restricao().vazia())


class TestConfirmacaoDigitoADigito(unittest.TestCase):
    """§3.3 — o normalizador acerta muito, não acerta sempre. A leitura de
    volta é o que faz um erro virar correção em vez de consulta na ficha
    de outra pessoa."""

    def test_agrupamento_de_cpf_e_telefone(self):
        self.assertEqual(ler_digitos("11144477735", "cpf"), "1 1 1, 4 4 4, 7 7 7, 3 5")
        self.assertEqual(ler_digitos("11987654321", "telefone"), "1 1, 9 8 7 6 5, 4 3 2 1")

    def test_formato_desconhecido_cai_em_grupos_de_tres(self):
        self.assertEqual(ler_digitos("123456"), "1 2 3, 4 5 6")

    def test_ignora_pontuacao(self):
        self.assertEqual(ler_digitos("111.444.777-35", "cpf"), "1 1 1, 4 4 4, 7 7 7, 3 5")

    def test_ida_e_volta(self):
        falado = "um um um, quatro quatro quatro, sete sete sete, trinta e cinco"
        self.assertEqual(ler_digitos(extrair_digitos(falado), "cpf"),
                         "1 1 1, 4 4 4, 7 7 7, 3 5")


class TestEspecialidadeFalada(unittest.TestCase):
    CATALOGO = ["Ortopedia", "Dermatologia", "Cardiologia", "Fisioterapia"]

    def test_paciente_fala_a_profissao_nao_a_area(self):
        for falado, esperado in [("ortopedista", "Ortopedia"),
                                 ("dermatologista", "Dermatologia"),
                                 ("cardiologista", "Cardiologia"),
                                 ("fisioterapeuta", "Fisioterapia"),
                                 ("ORTOPEDIA", "Ortopedia")]:
            with self.subTest(falado=falado):
                self.assertEqual(casar_especialidade(falado, self.CATALOGO), esperado)

    def test_o_que_a_clinica_nao_tem_continua_nao_tendo(self):
        for falado in ("neurologista", "clínico geral", "psiquiatra", "ok"):
            with self.subTest(falado=falado):
                self.assertIsNone(casar_especialidade(falado, self.CATALOGO))

    def test_ingles_precisa_de_apelido_e_nao_de_prefixo(self):
        """Achado na eval em inglês: "orthopedist" não casa com "Ortopedia"
        nem por prefixo — "ortho" contra "ortop". As outras três passavam por
        acidente ("derma", "cardi", "neuro" coincidem nas duas línguas), o que
        é pior do que falhar: a regra parecia funcionar."""
        for falado in ("orthopedist", "orthopaedics", "bone doctor"):
            with self.subTest(falado=falado):
                self.assertEqual(
                    casar_especialidade(falado, self.CATALOGO, EN), "Ortopedia")

    def test_o_apelido_ingles_nao_vale_numa_ligacao_em_portugues(self):
        self.assertIsNone(casar_especialidade("bone doctor", self.CATALOGO, PT))

    def test_portugues_segue_igual(self):
        self.assertEqual(casar_especialidade("ortopedista", self.CATALOGO, PT),
                         "Ortopedia")


class TestMascaramentoDeTranscricao(unittest.TestCase):
    """§8 — mascarar o campo do banco não basta; o número aparece cru na fala."""

    def test_cpf_ditado_por_extenso_some(self):
        t = db.mascarar_falado("Meu CPF é um um um, quatro quatro quatro, "
                               "sete sete sete, trinta e cinco")
        self.assertIn(db.MASCARA, t)
        self.assertNotIn("quatro quatro", t)

    def test_cpf_em_digito_some(self):
        self.assertEqual(db.mascarar_falado("É 111.444.777-35, tá?"),
                         f"É {db.MASCARA}, tá?")

    def test_telefone_ditado_some(self):
        self.assertIn(db.MASCARA,
                      db.mascarar_falado("onze nove oito sete seis cinco quatro três dois um"))

    def test_mascara_nao_come_palavra_vizinha(self):
        """Achado rodando contra um LLM real: o 'e' da lista de números casava
        dentro de 'está' e a máscara levava meia palavra junto."""
        t = db.mascarar_falado("o telefone termina em 1 1, 9 8 7 6 5, 4 3 2 1, "
                               "está correto?")
        self.assertIn("está correto?", t)
        self.assertIn(db.MASCARA, t)

    def test_palavras_que_contem_numero_por_dentro_ficam_intactas(self):
        for fala in ("Seu número está correto?", "Ele está esperando, e não sei",
                     "seiscentos e cinquenta reais não", "dezenove de manhã"):
            with self.subTest(fala=fala):
                self.assertNotIn(db.MASCARA, db.mascarar_falado(fala))

    def test_fala_normal_com_numeros_curtos_nao_e_tocada(self):
        for fala in ("Não, prefiro terça às duas da tarde",
                     "dia quinze do mês que vem",
                     "pode ser às 18h30"):
            with self.subTest(fala=fala):
                self.assertEqual(db.mascarar_falado(fala), fala)


class TestLimparParaVoz(unittest.TestCase):
    """O prompt manda não usar lista nem markdown. O modelo usa mesmo assim,
    e o TTS lê "- 18h00" como "hífen dezoito"."""

    def test_lista_markdown_vira_frase(self):
        self.assertEqual(
            limpar_para_voz("Temos três opções:\n\n- 18h00\n- 18h30\n\nQual prefere?"),
            "Temos três opções: 18h00, 18h30. Qual prefere?")

    def test_lista_numerada(self):
        self.assertEqual(limpar_para_voz("1. terça\n2. quinta"), "terça, quinta")

    def test_enfase_e_titulo_somem(self):
        self.assertEqual(limpar_para_voz("## Pronto\n**Confirmado!** Ficou para *terça*."),
                         "Pronto. Confirmado! Ficou para terça.")

    def test_texto_ja_falavel_nao_muda(self):
        for t in ("Boa noite. Como posso ajudar?",
                  "Consegui com a Dra. Thaís, terça-feira, às 18h. Confirma?"):
            with self.subTest(t=t):
                self.assertEqual(limpar_para_voz(t), t)

    def test_vazio(self):
        self.assertEqual(limpar_para_voz(""), "")
        self.assertEqual(limpar_para_voz(None), "")

    def test_entidades_sobrevivem(self):
        """A R8 lê esta frase. Limpar não pode apagar o que ela confere."""
        limpa = limpar_para_voz("**Dra. Thaís Bittencourt**\n- terça-feira, "
                                "dia 8 de setembro, às 18h\nConfirma?")
        for entidade in ("Thaís Bittencourt", "terça-feira", "8 de setembro", "18h"):
            self.assertIn(entidade, limpa)


    def test_rubrica_de_espera_nao_e_falada(self):
        """Visto na eval: "Está tudo correto? (aguardando resposta)". O modelo
        narrando o próprio controle de fluxo — o TTS lê o parêntese em voz alta."""
        self.assertEqual(limpar_para_voz("Está tudo correto? (aguardando resposta)"),
                         "Está tudo correto?")

    def test_rubrica_em_ingles_tambem_cai(self):
        self.assertEqual(limpar_para_voz("Vou verificar. [waiting for user]"),
                         "Vou verificar.")

    def test_parentese_com_informacao_de_verdade_fica(self):
        """A regra é contra rubrica, não contra parêntese: apagar tudo tiraria
        conteúdo que o paciente precisa ouvir."""
        dito = "A consulta é na sexta (dia 4 de setembro), às 9h."
        self.assertEqual(limpar_para_voz(dito), dito)

class TestNomes(unittest.TestCase):
    CADASTRO = ["Thaís Vasconcelos", "Wesley Bittencourt", "Larissa Nakamura",
                "Kauã Figueiredo", "Heitor Cruz"]

    def test_erros_tipicos_do_stt_em_nome_brasileiro(self):
        for falado, esperado in [("Taís Vasconselos", "Thaís Vasconcelos"),
                                 ("Uesley Bitencourt", "Wesley Bittencourt"),
                                 ("Larisa Nakamura", "Larissa Nakamura"),
                                 ("Cauã Figueredo", "Kauã Figueiredo")]:
            with self.subTest(falado=falado):
                self.assertEqual(casar_nome(falado, self.CADASTRO)["melhor"], esperado)

    def test_nome_de_fora_nao_e_forcado(self):
        self.assertIsNone(casar_nome("Roberto Alves", self.CADASTRO)["melhor"])

    def test_empate_e_declarado_em_vez_de_escolhido(self):
        r = casar_nome("Ana Silva", ["Ana Silvia", "Ana Silvia"], corte=0.5)
        self.assertTrue(r["ambiguo"])


if __name__ == "__main__":
    unittest.main()


class TestPassadoNaoEAgenda(unittest.TestCase):
    """Achado pelo cenário de cadastro: a fala que informa a data de nascimento
    passava pelo extrator de datas, que empurrava o 15 de março para o futuro e
    filtrava a agenda inteira pelo aniversário de quem ligou."""

    HOJE = date(2026, 9, 3)

    def r(self, texto):
        return interpretar_restricao(texto, self.HOJE)

    def test_nascimento_nao_vira_data_de_consulta(self):
        self.assertIsNone(self.r("nasci em quinze de março de oitenta").data_inicio)

    def test_a_palavra_nascimento_tambem_protege(self):
        self.assertIsNone(
            self.r("minha data de nascimento é quinze de março de oitenta").data_inicio)

    def test_a_restricao_de_hora_na_mesma_frase_sobrevive(self):
        """Só a data cai. "Só consigo depois das seis" continua valendo."""
        r = self.r("só consigo depois das seis, e nasci em março de oitenta")
        self.assertEqual(r.hora_min, "18:00")
        self.assertIsNone(r.data_inicio)

    def test_data_de_consulta_de_verdade_continua_passando(self):
        self.assertEqual(self.r("pode ser dia quinze de março").data_inicio,
                         "2027-03-15")


class TestNascimento(unittest.TestCase):
    """Data de nascimento é ditada por extenso, com sotaque e sem pontuação.

    Devolver None é resposta legítima e desejável: uma data inventada na ficha
    de alguém não dá erro nenhum — só some, e reaparece anos depois."""

    HOJE = date(2026, 9, 8)

    def ler(self, texto):
        return normalizar_nascimento(texto, self.HOJE)

    def test_por_extenso_completo(self):
        self.assertEqual(self.ler("quinze de março de mil novecentos e oitenta"),
                         "1980-03-15")

    def test_ano_abreviado_vira_o_seculo_certo(self):
        self.assertEqual(self.ler("quinze de março de oitenta"), "1980-03-15")

    def test_primeiro_e_dia_um_e_nao_mes_um(self):
        """`primeiro` vale 1 nos dois campos — filtrar por valor comia o dia."""
        self.assertEqual(self.ler("primeiro de janeiro de dois mil"), "2000-01-01")

    def test_barra(self):
        self.assertEqual(self.ler("15/03/1980"), "1980-03-15")

    def test_digitos_ditados_seguidos(self):
        self.assertEqual(self.ler("zero três de doze de setenta e sete"),
                         "1977-12-03")

    def test_mes_dito_como_numero(self):
        self.assertEqual(self.ler("quinze do três de oitenta"), "1980-03-15")

    def test_ano_de_dois_mil_nao_engole_o_dia(self):
        """"dois mil e cinco" acumula; o dia que vem depois não faz parte."""
        self.assertEqual(self.ler("dez de junho de dois mil e cinco"),
                         "2005-06-10")

    def test_iso_do_modelo(self):
        """O formato que o LLM emite num campo de data. Recusá-lo derrubava o
        cadastro por "faltam dados" com todos os dados corretos na mão."""
        self.assertEqual(self.ler("1980-03-15"), "1980-03-15")

    def test_iso_com_barra(self):
        self.assertEqual(self.ler("1980/03/15"), "1980-03-15")

    def test_iso_impossivel_continua_recusado(self):
        self.assertIsNone(self.ler("1980-13-45"))

    def test_data_impossivel_e_recusada(self):
        self.assertIsNone(self.ler("trinta e um de fevereiro de oitenta"))

    def test_futuro_e_recusado(self):
        self.assertIsNone(self.ler("dez de junho de dois mil e trinta"))

    def test_idade_implausivel_e_recusada(self):
        self.assertIsNone(self.ler("dez de junho de mil oitocentos e vinte"))

    def test_frase_sem_data_devolve_none(self):
        self.assertIsNone(self.ler("ah, não lembro agora"))

    def test_so_o_ano_nao_basta(self):
        self.assertIsNone(self.ler("mil novecentos e oitenta"))
