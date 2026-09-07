"""Testes da camada multi-agente. Provedores roteirizados — sem rede, sem custo.

O que precisa ser verdade para essa camada valer a pena:

1. o guardião **preempta**: o que o orquestrador ia dizer é descartado;
2. ele não escala duas vezes quando o orquestrador já escalou sozinho;
3. o histórico do modelo aprende que a ligação mudou de rumo — senão ele
   continua oferecendo horário no turno seguinte;
4. guardião quebrado não derruba a ligação, mas também não vira um "sem risco"
   silencioso;
5. sem guardião, tudo se comporta exatamente como antes.
"""
from __future__ import annotations

import time
import unittest

from clinica.agente import Agente
from clinica.orquestracao import (Escriba, GuardiaoDeRisco, Supervisor,
                                  VereditoGuardiao)
from clinica.provedor import ChamadaTool, ProvedorRoteirizado, Resposta
from tests.base import AGORA, BaseClinica


def fala(texto):
    return Resposta(texto=texto)


def chama(nome, **args):
    return Resposta(chamadas=(ChamadaTool("c1", nome, args),))


class ProvedorQueQuebra:
    nome = "quebrado"

    def responder(self, mensagens, ferramentas):
        raise RuntimeError("groq: HTTP 429 — cota")


class ProvedorLento:
    """Devolve depois de um tempo, para medir o que o paralelismo economiza."""
    nome = "lento"

    def __init__(self, segundos, texto):
        self.segundos, self.texto = segundos, texto

    def responder(self, mensagens, ferramentas):
        time.sleep(self.segundos)
        return Resposta(texto=self.texto)


class BaseSupervisor(BaseClinica):
    def montar(self, *, roteiro_agente, roteiro_guardiao=None, escriba=None,
               paralelo=True):
        agente = Agente(self.conn, ProvedorRoteirizado(roteiro_agente),
                        ligacao_id="sup", agora=AGORA, hoje=AGORA.date())
        guardiao = (GuardiaoDeRisco(ProvedorRoteirizado(roteiro_guardiao))
                    if roteiro_guardiao is not None else None)
        return Supervisor(agente, guardiao=guardiao, escriba=escriba,
                          paralelo=paralelo)

    @staticmethod
    def viu_risco(sinal="dor no peito"):
        return fala(f'{{"risco": true, "sinal": "{sinal}"}}')

    @staticmethod
    def nao_viu():
        return fala('{"risco": false, "sinal": ""}')


class TestGuardiao(unittest.TestCase):
    def _avaliar(self, resposta, texto="tô com dor no peito"):
        return GuardiaoDeRisco(ProvedorRoteirizado([resposta])).avaliar(texto)

    def test_le_o_json(self):
        v = self._avaliar(fala('{"risco": true, "sinal": "dor no peito"}'))
        self.assertTrue(v.risco)
        self.assertEqual(v.sinal, "dor no peito")

    def test_le_json_embrulhado_em_texto(self):
        v = self._avaliar(fala('Claro!\n```json\n{"risco": true, "sinal": "x"}\n```'))
        self.assertTrue(v.risco)

    def test_resposta_sem_json_nao_vira_risco_silencioso(self):
        v = self._avaliar(fala("acho que sim"))
        self.assertFalse(v.risco)
        self.assertEqual(v.erro, "resposta sem JSON")

    def test_json_invalido_e_reportado(self):
        v = self._avaliar(fala('{"risco": tru'))
        self.assertIn("JSON", v.erro or "")

    def test_provedor_fora_do_ar_nao_derruba_e_registra(self):
        v = GuardiaoDeRisco(ProvedorQueQuebra()).avaliar("dor no peito")
        self.assertFalse(v.risco)
        self.assertIn("RuntimeError", v.erro or "")

    def test_fala_vazia_nao_gasta_chamada(self):
        g = GuardiaoDeRisco(ProvedorRoteirizado([]))
        self.assertFalse(g.avaliar("").risco)
        self.assertEqual(g.chamadas, 0)


class TestPreempcao(BaseSupervisor):
    ROTEIRO_OFERTA = [fala("Tenho terça às 18h com a Dra. Thaís. Serve?")]

    def test_guardiao_preempta_e_a_resposta_do_agente_e_descartada(self):
        s = self.montar(roteiro_agente=self.ROTEIRO_OFERTA,
                        roteiro_guardiao=[self.viu_risco()])
        t = s.dizer("Pode ser terça, ah, e ando com um aperto no peito.")
        self.assertTrue(t.preemptado)
        self.assertNotIn("Serve?", t.turno.fala_agente)
        self.assertIn("pessoa da equipe", t.turno.fala_agente)
        self.assertTrue(s.agente.resultado()["transferiu"])
        self.assertEqual(s.agente.resultado()["motivo_transferencia"], "risco_clinico")

    def test_o_modelo_aprende_que_a_ligacao_mudou_de_rumo(self):
        """Sem isto, ele volta a oferecer horário no turno seguinte."""
        s = self.montar(roteiro_agente=self.ROTEIRO_OFERTA,
                        roteiro_guardiao=[self.viu_risco()])
        s.dizer("dor no peito")
        ultima = [m for m in s.agente.mensagens if m.get("role") == "assistant"][-1]
        self.assertIn("pessoa da equipe", ultima["content"])

    def test_sem_risco_nao_mexe_em_nada(self):
        s = self.montar(roteiro_agente=self.ROTEIRO_OFERTA,
                        roteiro_guardiao=[self.nao_viu()])
        t = s.dizer("pode ser terça mesmo")
        self.assertFalse(t.preemptado)
        self.assertIn("Serve?", t.turno.fala_agente)
        self.assertFalse(s.agente.resultado()["transferiu"])

    def test_nao_escala_duas_vezes(self):
        """O orquestrador já viu o risco sozinho — o guardião não duplica."""
        s = self.montar(
            roteiro_agente=[chama("transferir_para_humano",
                                  motivo="risco_clinico", resumo="dor no peito"),
                            fala("Vou te passar para um atendente.")],
            roteiro_guardiao=[self.viu_risco()])
        t = s.dizer("tô com dor no peito")
        self.assertFalse(t.preemptado)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM transferencias").fetchone()[0], 1)

    def test_guardiao_quebrado_deixa_a_ligacao_seguir(self):
        agente = Agente(self.conn, ProvedorRoteirizado(self.ROTEIRO_OFERTA),
                        ligacao_id="x", agora=AGORA, hoje=AGORA.date())
        s = Supervisor(agente, guardiao=GuardiaoDeRisco(ProvedorQueQuebra()))
        t = s.dizer("pode ser terça")
        self.assertIn("Serve?", t.turno.fala_agente)
        self.assertIsNotNone(t.guardiao.erro)

    def test_sem_guardiao_se_comporta_como_antes(self):
        s = self.montar(roteiro_agente=self.ROTEIRO_OFERTA)
        t = s.dizer("oi")
        self.assertIsNone(t.guardiao)
        self.assertFalse(t.preemptado)
        self.assertIn("Serve?", t.turno.fala_agente)


class TestParalelismo(BaseClinica):
    def _rodar(self, paralelo):
        agente = Agente(self.conn, ProvedorLento(0.30, "Tenho terça às 18h."),
                        ligacao_id="p", agora=AGORA, hoje=AGORA.date())
        guardiao = GuardiaoDeRisco(
            ProvedorLento(0.20, '{"risco": false, "sinal": ""}'))
        s = Supervisor(agente, guardiao=guardiao, paralelo=paralelo)
        inicio = time.perf_counter()
        s.dizer("boa noite")
        return (time.perf_counter() - inicio) * 1000

    def test_paralelo_custa_o_maior_e_nao_a_soma(self):
        """É a razão de existir do desenho: o guardião não entra na conta do
        turno enquanto for mais rápido que o orquestrador."""
        paralelo = self._rodar(True)
        self.assertLess(paralelo, 450, "deveria custar ~300 ms, não 500")
        self.assertGreater(paralelo, 280)

    def test_sequencial_custa_a_soma(self):
        self.assertGreater(self._rodar(False), 480)


class TestEscriba(BaseSupervisor):
    def test_escreve_so_quando_transfere(self):
        escriba = Escriba(ProvedorRoteirizado([fala("Paciente com dor no peito.")]))
        s = self.montar(roteiro_agente=[fala("Tenho terça às 18h.")],
                        roteiro_guardiao=[self.nao_viu()], escriba=escriba)
        s.dizer("boa noite")
        r = s.encerrar()
        self.assertEqual(r["resumo_handoff"], "")

    def test_escreve_no_handoff(self):
        escriba = Escriba(ProvedorRoteirizado([fala("Paciente relatou dor no peito "
                                                    "ao tentar marcar ortopedia.")]))
        s = self.montar(roteiro_agente=[fala("Tenho terça às 18h.")],
                        roteiro_guardiao=[self.viu_risco()], escriba=escriba)
        s.dizer("dor no peito")
        r = s.encerrar()
        self.assertIn("dor no peito", r["resumo_handoff"])
        self.assertEqual(r["preempcoes"], 1)

    def test_escriba_fora_do_ar_nao_quebra_o_encerramento(self):
        s = self.montar(roteiro_agente=[fala("ok")],
                        roteiro_guardiao=[self.viu_risco()],
                        escriba=Escriba(ProvedorQueQuebra()))
        s.dizer("dor no peito")
        self.assertEqual(s.encerrar()["resumo_handoff"], "")


class TestTrace(BaseSupervisor):
    def test_o_trace_carrega_o_guardiao(self):
        s = self.montar(roteiro_agente=[fala("ok"), fala("ok")],
                        roteiro_guardiao=[self.nao_viu(), self.viu_risco("aperto")])
        s.dizer("boa noite")
        s.dizer("ando com um aperto no peito")
        r = s.encerrar()
        self.assertEqual(len(r["supervisao"]), 2)
        self.assertFalse(r["supervisao"][0]["guardiao"]["risco"])
        self.assertEqual(r["supervisao"][1]["guardiao"]["sinal"], "aperto")
        self.assertEqual(len(r["ms_guardiao"]), 2)


if __name__ == "__main__":
    unittest.main()
