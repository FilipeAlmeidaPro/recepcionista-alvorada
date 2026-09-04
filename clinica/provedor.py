"""Interface de LLM. Uma só, para o orquestrador não saber de quem depende.

Duas implementações moram aqui:

* `ProvedorRoteirizado` — determinístico, offline, custo zero. É ele que roda
  a suíte de eval no CI: mesma sequência de tools, mesmo validador, mesmas
  asserções, sem gastar um token. Testa **a máquina**, não o modelo.
* `ProvedorOpenAICompativel` — Gemini Flash e Groq falam o mesmo dialeto de
  tool calling. Uma URL e um nome de modelo separam um do outro.

Trocar de provedor é trocar uma linha. Foi para isso que a interface existe.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Protocol


@dataclass(frozen=True)
class ChamadaTool:
    id: str
    nome: str
    argumentos: dict


@dataclass(frozen=True)
class Resposta:
    texto: str | None = None
    chamadas: tuple[ChamadaTool, ...] = ()
    tokens_entrada: int = 0
    tokens_saida: int = 0
    latencia_ms: float = 0.0
    # Mensagem crua do provedor, para ser devolvida no histórico byte a byte.
    # O Gemini 3.x recusa a próxima chamada se o `thought_signature` que ele
    # emitiu no tool call não voltar: reconstruir a mensagem a partir dos
    # campos que eu entendo apaga os campos que eu não entendo.
    bruto: dict | None = None


class MarcaPasso:
    """Segura a chamada para não estourar o teto de tokens por minuto.

    A primeira versão estimava o custo da próxima chamada e errava: chutava
    900 tokens quando a conversa já custava 3 000. Esta lê o que o servidor
    responde — `x-ratelimit-remaining-tokens` e `x-ratelimit-reset-tokens` —
    e espera o balde encher antes de bater a cabeça no 429.

    Tomar 429 e tentar de novo até funciona, mas desperdiça a chamada e
    contamina a medição de latência com tempo de espera.
    """

    def __init__(self, margem: int = 1500):
        self.margem = margem
        self.restante: int | None = None
        self.reset_s: float = 0.0
        self.espera_total_s = 0.0

    def ler(self, cabecalhos) -> None:
        def num(nome, padrao=None):
            valor = cabecalhos.get(nome)
            if valor is None:
                return padrao
            try:
                return float(re.sub(r"[^\d.]", "", valor.replace("m", "m "))
                             .split()[0]) if "m" in valor else float(
                    valor.rstrip("s"))
            except (ValueError, IndexError):
                return padrao

        bruto = cabecalhos.get("x-ratelimit-remaining-tokens")
        self.restante = int(bruto) if bruto and bruto.isdigit() else None
        reset = cabecalhos.get("x-ratelimit-reset-tokens") or ""
        try:
            self.reset_s = float(reset.rstrip("s")) if reset.endswith("s") and "m" not in reset else 60.0
        except ValueError:
            self.reset_s = 60.0

    def aguardar(self) -> None:
        """Espera se o que sobrou não cobre uma chamada típica."""
        if self.restante is None or self.restante >= self.margem:
            return
        espera = min(max(self.reset_s, 1.0) + 0.5, 65.0)
        time.sleep(espera)
        self.espera_total_s += espera
        self.restante = None      # o balde encheu; o próximo cabeçalho reconta


class OrcamentoEsgotado(RuntimeError):
    """Parou por limite auto-imposto, não por erro. Distinguir importa: um é
    resultado da rodada, o outro é bug."""


class CotaDiariaEsgotada(RuntimeError):
    """Acabou a cota do dia. Não adianta tentar de novo — nem em 5 s, nem em
    5 minutos. Insistir aqui gasta 5 tentativas por cenário e transforma um
    limite conhecido numa lista de 28 falhas que parecem bug."""


class Provedor(Protocol):
    nome: str

    def responder(self, mensagens: list[dict], ferramentas: list[dict]) -> Resposta: ...


# --- offline -----------------------------------------------------------------

class ProvedorRoteirizado:
    """Responde de um roteiro fixo. Cada entrada é uma `Resposta` ou um callable
    que recebe as mensagens até agora e devolve uma — o que permite ao paciente
    de teste reagir ao que o agente falou, sem LLM nenhum no meio."""

    nome = "roteirizado"

    def __init__(self, roteiro: list[Resposta | Callable[[list[dict]], Resposta]]):
        self._roteiro = list(roteiro)
        self.chamadas = 0

    def responder(self, mensagens: list[dict], ferramentas: list[dict]) -> Resposta:
        if self.chamadas >= len(self._roteiro):
            raise AssertionError(
                f"roteiro esgotado na chamada {self.chamadas + 1}; "
                f"o agente pediu mais turnos do que o cenário previu")
        item = self._roteiro[self.chamadas]
        self.chamadas += 1
        return item(mensagens) if callable(item) else item


# --- online ------------------------------------------------------------------

PERFIS = {
    # Free tier de verdade, sem cartão. A interface é a mesma nos dois.
    # Modelo fixado, não alias: eval com alias móvel muda de resultado sozinha.
    # (url, modelo, variável de ambiente, tokens por minuto do free tier)
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
               "gemini-3.5-flash", "GEMINI_API_KEY", None),
    "groq": ("https://api.groq.com/openai/v1/chat/completions",
             "openai/gpt-oss-120b", "GROQ_API_KEY", 8000),   # 8000 TPM
}


_RETENTAR_EM = re.compile(r"try again in ([\d.]+)\s*s", re.IGNORECASE)
# "tokens per day (TPD)" / "requests per day (RPD)": esperar não resolve.
_E_COTA_DIARIA = re.compile(r"per day \((?:TPD|RPD)\)|PerDay", re.IGNORECASE)


def _espera_pedida(detalhe: str) -> float | None:
    """O servidor diz em quanto tempo tentar de novo. Obedecer é mais barato
    que adivinhar: o backoff exponencial chutava 3 s quando pediam 11 s."""
    achado = _RETENTAR_EM.search(detalhe)
    return min(float(achado.group(1)) + 0.5, 65.0) if achado else None


def _contexto_ssl() -> ssl.SSLContext:
    """Encontra o bundle de certificados sem depender de instalação manual.

    O Python do python.org no macOS vem sem CA bundle — quem clonar o projeto
    tomaria CERTIFICATE_VERIFY_FAILED e acharia que a chave está errada.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    for caminho in ("/etc/ssl/cert.pem", "/usr/local/etc/openssl/cert.pem",
                    "/etc/pki/tls/certs/ca-bundle.crt"):
        if os.path.exists(caminho):
            return ssl.create_default_context(cafile=caminho)
    return ssl.create_default_context()


class ProvedorOpenAICompativel:
    """Cliente mínimo de chat completions com tool calling, via stdlib.

    Sem SDK de propósito: uma dependência a menos para instalar, e o corpo da
    requisição fica visível — o que importa quando o assunto é medir latência
    e custo por estágio.
    """

    def __init__(self, perfil: str = "gemini", *, modelo: str | None = None,
                 chave: str | None = None, timeout: float = 90.0,
                 temperatura: float = 0.2, tentativas: int = 5,
                 orcamento: int | None = None):
        if perfil not in PERFIS:
            raise ValueError(f"perfil desconhecido: {perfil}. Use {list(PERFIS)}.")
        url, modelo_padrao, env, _tpm = PERFIS[perfil]
        self.nome = f"{perfil}:{modelo or modelo_padrao}"
        self._url = url
        self._modelo = modelo or modelo_padrao
        self._chave = chave or os.environ.get(env)
        self._timeout = timeout
        self._temperatura = temperatura
        self._ssl = _contexto_ssl()
        self._tentativas = tentativas
        # Free tier do Gemini: 20 chamadas por dia, por modelo. Um laço de eval
        # que não sabe disso queima a cota do dia inteiro em 40 segundos —
        # e foi exatamente o que aconteceu aqui.
        self.orcamento = orcamento
        self.chamadas = 0
        self._passo = MarcaPasso()
        if not self._chave:
            raise RuntimeError(
                f"Falta a variável de ambiente {env}. "
                f"A suíte de eval roda sem chave com ProvedorRoteirizado.")

    @property
    def espera_total_s(self) -> float:
        """Segundos gastos esperando rate limit. Não é latência de inferência —
        misturar os dois seria mentir com dado real."""
        return self._passo.espera_total_s

    def responder(self, mensagens: list[dict], ferramentas: list[dict]) -> Resposta:
        if self.orcamento is not None and self.chamadas >= self.orcamento:
            raise OrcamentoEsgotado(
                f"{self.nome}: orçamento de {self.orcamento} chamadas esgotado")
        self.chamadas += 1
        self._passo.aguardar()
        corpo = json.dumps({
            "model": self._modelo,
            "messages": mensagens,
            "tools": ferramentas,
            "tool_choice": "auto",
            "temperature": self._temperatura,
        }).encode()
        req = urllib.request.Request(
            self._url, data=corpo,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self._chave}",
                     # Sem isto a Groq devolve 403 (Cloudflare 1010): o
                     # User-Agent padrão do urllib está na lista de bloqueio.
                     "User-Agent": "recepcionista-alvorada/1.0"})
        inicio = time.perf_counter()
        # Free tier tem 429 e a rede tem timeout. Ambos são recuperáveis e
        # nenhum dos dois pode derrubar uma rodada de eval de 40 cenários.
        for tentativa in range(self._tentativas):
            try:
                with urllib.request.urlopen(req, timeout=self._timeout,
                                            context=self._ssl) as r:
                    dados = json.loads(r.read())
                    self._passo.ler(r.headers)
                break
            except urllib.error.HTTPError as e:
                detalhe = " ".join(e.read()[:400].decode(errors="replace").split())
                if e.code == 429 and _E_COTA_DIARIA.search(detalhe):
                    raise CotaDiariaEsgotada(f"{self.nome}: {detalhe[:200]}") from e
                if e.code in (429, 500, 502, 503) and tentativa < self._tentativas - 1:
                    time.sleep(_espera_pedida(detalhe) or 2 ** tentativa * 3)
                    continue
                raise RuntimeError(f"{self.nome}: HTTP {e.code} — {detalhe}") from e
            except (urllib.error.URLError, TimeoutError, OSError) as e:  # noqa: PERF203
                if tentativa < self._tentativas - 1:
                    time.sleep(2 ** tentativa * 2)
                    continue
                raise RuntimeError(f"{self.nome}: falha de rede — {e}") from e
        latencia = (time.perf_counter() - inicio) * 1000

        escolha = dados["choices"][0]["message"]
        chamadas = tuple(
            ChamadaTool(id=c.get("id") or f"c{i}", nome=c["function"]["name"],
                        argumentos=json.loads(c["function"].get("arguments") or "{}"))
            for i, c in enumerate(escolha.get("tool_calls") or []))
        uso = dados.get("usage") or {}
        return Resposta(texto=escolha.get("content"), chamadas=chamadas,
                        tokens_entrada=uso.get("prompt_tokens", 0),
                        tokens_saida=uso.get("completion_tokens", 0),
                        latencia_ms=latencia, bruto=escolha)


def provedor_padrao() -> Provedor | None:
    """O provedor que houver chave para. `None` quando não há nenhuma — e aí a
    suíte roda roteirizada em vez de falhar."""
    for perfil, (_url, _modelo, env, _tpm) in PERFIS.items():
        if os.environ.get(env):
            return ProvedorOpenAICompativel(perfil)
    return None
