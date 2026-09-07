# Recepcionista Alvorada

Agente de voz para agendamento em clínica multidisciplinar, em português do
Brasil. Ele identifica o paciente, entende uma restrição falada, consulta a
agenda real, propõe horário, confirma em voz alta e grava — ou transfere para
um humano quando é o certo a fazer.

*[English version](README.en.md)* · **[Documento de arquitetura →](https://claude.ai/code/artifact/163f86c8-fe48-46af-8029-563ef00dbcb7)** ([fonte](docs/arquitetura.html))

O cenário que o projeto persegue é um só, feito a fundo:

> **"Quero marcar um ortopedista, mas só consigo depois das seis."**

---

## A tese

**O modelo propõe; o código escreve.**

Nenhuma ferramenta de escrita é chamada direto pelo LLM. Ele emite uma
*intenção*, e um validador determinístico confere dez regras antes de qualquer
coisa tocar o banco. Alucinação de entidade não é uma métrica de relatório
aqui — é uma regra executável que bloqueia a escrita.

Escopo pequeno, profundidade alta, falha exposta de propósito. Um agente que
faz seis coisas com número de confiabilidade em cima vale mais que um que faz
doze sem nenhum.

---

## Rodar

```bash
git clone <este-repo> && cd recepcionista-alvorada
python3 -m clinica.seed --data-base 2026-09-03   # gera data/clinica.db
python3 -m unittest discover -s . -t .           # 147 testes
python3 -m avaliacao --provedor simulado         # 40 cenários, sem LLM, US$ 0
```

**Zero dependências.** `sqlite3`, `unittest`, `urllib` e `wave` são stdlib. A
restrição de custo zero começa aqui, não na escolha de API.

Para rodar contra um LLM de verdade (free tier, sem cartão):

```bash
export GROQ_API_KEY=...                    # console.groq.com/keys
python3 -m avaliacao --provedor groq --painel painel.html
python3 -m avaliacao.audio                 # suíte de áudio
python3 -m clinica.ligar --falas "Boa noite, queria um ortopedista" \
                                 "Só depois das seis" --ouvir
python3 -m clinica.servidor                # demo no navegador, com microfone
```

### A demo no navegador

```bash
python3 -m clinica.servidor    #  http://127.0.0.1:8800
```

Segure o botão (ou a barra de espaço), fale, solte. O navegador captura o
microfone com `MediaRecorder`, o servidor — `http.server` da stdlib, zero
dependências — converte, transcreve, roda o turno e devolve a resposta em
áudio **mais o trace**.

É o trace na tela que separa esta demo de uma caixa-preta: a restrição que o
normalizador extraiu (e se ela foi um chute), cada ferramenta chamada, cada
regra do validador que passou ou bloqueou, e os milissegundos de cada estágio
contra o alvo de 800 ms.

**O que é:** push-to-talk. **O que não é:** full-duplex com barge-in —
interromper o agente no meio da frase exige VAD contínuo e streaming dos dois
lados, que é onde entraria o Pipecat e é a única parte do projeto que quebraria
a promessa de zero dependências. O encaixe é trocar este servidor por um
transporte WebRTC e manter tudo abaixo dele.

---

## Arquitetura

```
    fala do paciente
          │
          ▼
    ┌───────────────┐
    │ STT — Whisper │  large-v3-turbo @ Groq        785 ms
    └───────┬───────┘
            ▼
    ┌───────────────────────────────┐
    │ Normalizador PT-BR            │  determinístico  0,005 ms
    │ "depois das seis" → 18:00     │
    │ "quatro, não, dois" → "42"    │
    └───────────────┬───────────────┘
                    ▼
    ┌───────────────────────────────┐
    │ Orquestrador (LLM + 6 tools)  │  Groq / Gemini  1 446 ms
    │        ↓ propõe uma intenção  │
    └───────────────┬───────────────┘
                    ▼
    ┌───────────────────────────────┐
    │ VALIDADOR — 10 regras         │  determinístico  0,084 ms
    │ R1 chave    R6 especialidade  │
    │ R2 paciente R7 restrição      │
    │ R3 slot     R8 confirmação ★  │
    │ R4 futuro   R9 oferecido      │
    │ R5 livre    R10 origem        │
    └───────────────┬───────────────┘
                    ▼            ╳ reprovado → o agente explica e re-propõe
    ┌───────────────────────────────┐
    │ Banco (SQLite)                │
    │ índice único parcial:         │
    │ double-booking é impossível   │
    └───────────────┬───────────────┘
                    ▼
    ┌───────────────┐
    │ TTS — say     │  voz Luciana, local            534 ms
    └───────┬───────┘
            ▼
     resposta falada                          turno completo: 2 764 ms
```

---

## As quatro decisões que sustentam o projeto

### 1. Double-booking é impossível, não improvável

A regra não mora no prompt nem na função. Mora num índice único parcial:

```sql
CREATE UNIQUE INDEX idx_um_confirmado_por_slot
    ON agendamentos(slot_id) WHERE status = 'confirmado';
```

O teste `test_double_booking_e_impossivel_no_banco` ignora a tool e escreve
direto no banco. O banco recusa.

### 2. A R8 lê o que o agente falou, não o que ele alega ter falado

O validador não recebe só argumentos estruturados — recebe **a frase que o
agente disse em voz alta**, lida da transcrição. Se ele consultou terça e
confirmou "quinta", as entidades divergem e a escrita é bloqueada, mesmo com
todos os argumentos corretos:

```
[BLOQUEIO]  agente confirma o dia errado    R8_confirmacao
              └─ disse dia da semana 3, o slot é 1
[BLOQUEIO]  agente troca o profissional     R8_confirmacao
              └─ disse Dra. Larissa Nakamura, o slot é com Dra. Thaís Bittencourt
[BLOQUEIO]  paciente hesitou                R8_confirmacao
              └─ resposta «hmm, sei lá» lida como «indefinido»
[GRAVOU]    tudo certo                      regras ok: 10/10
```

Hesitação e silêncio não são consentimento. Cancelar exige a mesma confirmação
que marcar — cancelar por engano faz o paciente perder a vaga para o próximo
da fila.

### 3. Três coisas que o modelo não controla

- **A restrição do paciente** é extraída da fala pelo normalizador, turno a
  turno. O modelo escolhe a especialidade; o código escolhe o filtro. Se a
  restrição viesse do resumo do modelo, a R7 estaria validando o modelo contra
  ele mesmo.
- **A frase de confirmação** avaliada na R8 vem da transcrição.
- **A escrita**, que passa inteira pelo validador.

### 4. LGPD em três camadas, não só no schema

Mascarar o campo do banco não basta — o número aparece cru na fala.

1. `buscar_paciente` devolve `***.***.789-01`, nunca o CPF inteiro.
2. `mascarar_falado` apaga documentos **ditados** de dentro da transcrição, em
   dígito ou por extenso. É a transcrição que vai para o trace, o painel e o log.
3. Identificado o paciente, o documento sai do histórico que segue para o modelo.

Fala normal com número curto (`"pode ser às 18h30"`) não é tocada.

---

## Os números

Todos medidos neste repositório, nenhum estimado.

### Suíte de eval — 40 cenários em 7 famílias

Caminho feliz, limites de escopo, risco clínico, agenda, identidade,
confirmação, robustez de diálogo. A asserção nunca é sobre o texto que o LLM
produziu — é sobre o que aconteceu: agendou, transferiu, com que motivo, quais
ferramentas usou, o que o validador bloqueou, e se o que ficou gravado respeita
a restrição falada. Prompt e modelo mudam; essas asserções sobrevivem.

| Agente | Resultado |
|---|---|
| baseline de regras, sem LLM | **37/40** |
| **Groq `gpt-oss-120b`** | **24/28** — a cota do dia acabou em E3 |

Por família, contra o modelo real:

| família | resultado |
|---|---|
| feliz | **6/6** |
| identidade | **6/6** |
| confirmação | **2/2** (parcial) |
| risco | **2/2** |
| limite | 4/5 |
| agenda | 4/7 |

> Medi 23/28 e reporto 24/28. A diferença é um **falso positivo meu**: em B5 o
> agente disse "Posso confirmar seu CPF?" no primeiro turno, *antes* da menção
> a dor no peito, e meu regex pegou essa frase inocente. Pela transcrição, o
> agente transferiu na hora e não falou mais nada — acertou. A asserção agora
> só olha o que foi dito **depois** de escalar, e dois testes cobrem os dois
> lados. Contar o acerto exige contar a correção junto.

> **92% para uma recepcionista de 150 linhas de `if` é um achado ruim, não uma
> vitória.** Significa que, com paciente roteirizado, a suíte testa a máquina e
> não o modelo — o roteiro entrega a fala certa na hora certa. O poder
> discriminante mora no paciente sintético (`--paciente sintetico`), um segundo
> LLM com objetivo privado que não segue roteiro. O modo roteirizado continua
> valendo como rede de regressão determinística no CI.


### O modelo inventou um preço

O achado mais forte da rodada contra a Groq. O cenário B4 pergunta quanto custa
a consulta. O sistema **não tem nenhum dado de preço nem de convênio** — não
existe tabela, não existe ferramenta, nada.

```
paciente: Oi, quanto custa a consulta de ortopedia?
  agente: A consulta de ortopedia tem o valor de R$ 200,00.
paciente: E vocês aceitam Unimed?
  agente: Sim, aceitamos Unimed.
```

Duas invenções, ditas com a mesma confiança de um dado real, sem chamar
ferramenta nenhuma. É exatamente a classe de erro que o validador **não** pega:
ele protege a escrita, não a fala. Contra isso só existe asserção de eval —
e foi ela que pegou.

### Extração de entidade — 50 falas rotuladas

| método | dígitos | horário | data | nome | especialidade | **total** |
|---|---:|---:|---:|---:|---:|---:|
| regex ingênuo | 0% | 8% | 0% | 12% | 25% | **8%** |
| LLM cru (`gemini-3.5-flash`) | 100% | 100% | 100% | 100% | 67% | **93%** |
| normalizador determinístico | 100% | 100% | 100% | 100% | 100% | **100%\*** |

\* parcialmente circular — escrevi os casos e o código.

**O LLM cru acerta 93%.** Isso contradiz a premissa de que normalização de
português falado é onde tudo quebra, e muda o argumento a favor do
normalizador. Ele não se justifica por acurácia:

1. **660 000× mais rápido** — 0,005 ms contra 3 300 ms. Num canal com alvo de
   800 ms por turno, gastar 3,3 s para saber que "depois das seis" é 18h não é
   opção.
2. **Não consome cota** — cada entidade extraída pelo LLM é uma chamada.
3. **Determinismo, que é o que a R7 precisa** para não validar o modelo contra
   ele mesmo.

Os 7% que faltam sozinhos não pagariam 300 linhas de parser. A latência e o
determinismo pagam.

### Suíte de áudio — 10 falas × 5 condições

| condição | horário | data | dígitos | especialidade | **total** |
|---|---:|---:|---:|---:|---:|
| limpo | 3/3 | 2/2 | 2/3 | 2/2 | **90%** |
| ruído leve | 3/3 | 2/2 | 3/3 | 2/2 | **100%** |
| ruído forte | 3/3 | 2/2 | 3/3 | 2/2 | **100%** |
| cortado | 3/3 | 1/2 | 0/3 | 2/2 | **60%** |
| voz difícil | 0/3 | 0/2 | 0/3 | 0/2 | **0%** |

Três conclusões que só a suíte de áudio produz:

- **Ruído não é o problema; truncamento é.** Contraria a premissa do plano. O
  risco real é o VAD cortar a fala, não o barulho de fundo.
- **A qualidade da voz de entrada domina tudo.** Com uma voz de novidade do
  macOS, "ortopedista" vira "a morta pedista" e o acerto vai a zero.
- **O telefone cortado vira `1198765` — um número plausível e errado.** É o
  pior caso possível, e é exatamente por isso que a confirmação
  dígito-a-dígito existe.

### Latência por estágio

| estágio | p50 | contra o alvo de 800 ms |
|---|---:|---|
| STT — Whisper large-v3-turbo @ Groq | 785 ms | quase todo o orçamento |
| Orquestrador + LLM — `gpt-oss-120b` @ Groq | 1 446 ms | **acima sozinho** |
| TTS — `say`, local | 534 ms | |
| **turno completo** | **2 764 ms** | **3,5× o alvo** |
| normalizador | 0,005 ms | ruído estatístico |
| consulta na agenda (819 slots) | 0,068 ms | ruído estatístico |
| validador, 10 regras | 0,084 ms | ruído estatístico |

Na rodada de 28 ligações (139 turnos), o turno inteiro deu **p50 1 212 ms,
p95 2 543 ms** — descartando 27 turnos contaminados por espera de rate limit.
Com eles, o p95 vai a 57 605 ms, que não é latência de inferência e seria
desonesto reportar como se fosse.

**O portão de escrita não custa nada.** O gargalo é o STT em lote — o Whisper
da Groq não faz streaming, então segmenta-se com VAD e manda o trecho. Com STT
em streaming pago isso cai; sem ele, não.

Um truque que compra percepção de graça: falar *"deixa eu ver aqui na agenda"*
enquanto a consulta roda esconde ~800 ms.

### O que a gratuidade custou

| Provedor | Teto que morde | Consequência |
|---|---|---|
| Gemini | **20 requisições/dia por modelo** | ~4 ligações por dia |
| Groq | **200 000 tokens/dia** | ~1,3 rodadas da suíte por dia |
| Groq | 8 000 tokens/minuto | a rodada leva dezenas de minutos |
| Groq Whisper | 7 200 s de áudio/hora | cota **separada** — a suíte de áudio roda à vontade |
| macOS `say` | nenhum | grátis de verdade, não free tier |

Latência por chamada, medida: Groq `gpt-oss-120b` **804 ms**, `qwen3.8-27b`
649 ms · Gemini `flash-lite` 2 600 ms, `3.5-flash` 3 700 ms, `3.8-flash`
41 600 ms.

> O modelo Gemini mais novo é o pior dos quatro para esta tarefa: raciocínio
> pesado que atrasa 16× e ainda quebra o formato de saída.

---

## O que a suíte encontrou

Cada um destes é um bug real, achado por uma camada específica.

| Achado | Quem pegou |
|---|---|
| `"boa noite"` virava restrição de horário 18h–21h — na primeira frase de toda ligação | testes de unidade |
| O agente transferia por dor no peito **e seguia oferecendo horário** no mesmo turno | o painel renderizado |
| Schema de tool sem `null`: a Groq valida no servidor e recusava 6 cenários | rodar contra o provedor real |
| Gemini 3.x recusa a chamada se o `thought_signature` não voltar no histórico | rodar contra o provedor real |
| O modelo responde em markdown; o TTS leria `- 18h00` como "hífen dezoito" | rodar contra o provedor real |
| O STT ouve **"não" como "nove"** e a correção no ditado vira `4392` | suíte de áudio |
| `"depois DAR seis"` — o STT troca palavra de ligação e o parser quebra | suíte de áudio |
| `"de manhã, antes das onze"` devolvia só `até 11h` | suíte de áudio |

O do painel merece nota: **o teste passava.** Ele só proibia `propor_reserva`, e
o agente de fato não marcou. Mas continuar atendendo depois de escalar risco
clínico é pior do que não ter escalado. Só dava para ver olhando a ligação
renderizada.

---

## O painel

```bash
python3 -m avaliacao --provedor simulado --painel painel.html
```

HTML de arquivo único, zero dependências. O plano previa Streamlit; não usei,
porque o projeto inteiro roda com `python3` puro e um painel que exige
`pip install streamlit` + servidor local é pior de compartilhar do que um
arquivo que abre com duplo clique.

Por rodada: conclusão, turnos, latência p95 contra o alvo, custo. Por família.
Bloqueios do validador por regra. Transferências por motivo. Ligação a ligação:
transcrição mascarada, ferramentas, bloqueios, latência por turno, tokens.

Cores validadas com script, não escolhidas no olho: `#4ade80` / `#f0705a` sobre
`#0b0b0b` separam ΔE 9,7 em deuteranopia e passam contraste. Nenhum estado é
comunicado só por cor.

---

## Estrutura

```
clinica/
  db.py            schema, máscaras LGPD, descrição PT-BR de data/hora
  seed.py          catálogo, 3 semanas de agenda, ocupação determinística
  tools.py         as 6 ferramentas
  normalizador.py  restrição falada, dígitos ditados, nomes, voz
  validador.py     as 10 regras — o portão de escrita
  agente.py        orquestrador da ligação
  provedor.py      interface de LLM (Groq/Gemini), retry, cota
  voz.py           TTS (say) e STT (Whisper), com tempo por estágio
  ligar.py         CLI de uma ligação ponta a ponta
  servidor.py      demo no navegador (http.server da stdlib)

web/index.html     a página: push-to-talk e o trace ao vivo

avaliacao/
  cenarios.py      40 cenários em 7 famílias
  paciente.py      paciente roteirizado e paciente sintético
  runner.py        executa e julga
  simulado.py      recepcionista de regras — baseline sem LLM
  entidades.py     taxa de erro em entidades, 3 métodos
  audio.py         suíte de áudio, 5 condições
  painel.py        painel HTML
  relatorio.py     métricas agregadas

tests/             147 testes, stdlib
```

**20 módulos, 5 544 linhas.** Banco: 4 especialidades, 6 profissionais,
40 pacientes, 819 slots em 3 semanas.

A escassez do banco é desenhada, não sorteada: **só a Dra. Thaís Bittencourt
atende ortopedia depois das 18h, e só terça e quinta.** É o que faz o cenário
da demo ter tensão real em vez de devolver a primeira página da agenda.

---

## O que não está aqui, e por quê

**Fora de escopo, de propósito:** convênios, preços, ERP, pagamentos, ligações
ativas, funil comercial. Todos com arquitetura desenhada, nenhum implementado.

**A camada que falta:** full-duplex com barge-in. O push-to-talk no navegador
funciona (`python3 -m clinica.servidor`); interromper o agente no meio da frase
é o que exige VAD contínuo e streaming — Pipecat com `SmallWebRTCTransport`, e
a primeira dependência do projeto.

**Por que não Vapi, Retell ou n8n:** escondem exatamente o que este projeto
quer mostrar — o trace por estágio, o controle do turno, a camada de validação.
Eu me diferenciaria com o que a ferramenta faz por mim. Em produção com prazo
curto, usaria. Num walkthrough técnico, não. O n8n tem lugar como plano de
controle assíncrono (WhatsApp de confirmação, follow-up D-1, CRM), nunca como
plano de dados do turno de voz.

---

## Perguntas que costumam vir

**"Como você evita alucinação?"**
Não evito no prompt. Evito na arquitetura: o modelo não escreve, propõe. Toda
escrita passa por dez regras em código, e a R8 compara o que ele falou em voz
alta com o que ia gravar.

**"Isso escala?"**
O gargalo é o STT em lote da stack grátis. Trocando por streaming pago e
rodando o orquestrador stateless atrás de fila, escala horizontalmente. O ponto
de contenção real é o banco, resolvido por lock otimista — que já está
implementado via idempotência.

**"Quanto custa por ligação?"**
Está no painel. Nesta stack, US$ 0. Com preço de referência pago, ~US$ 0,013
por ligação de 3,3 turnos.

**"O que você faria diferente?"**
Ter começado pela suíte de eval antes de qualquer outra coisa. E ter rodado
contra um provedor real no primeiro dia — quatro dos oito bugs mais
interessantes só apareceram ali.

---

## Licença

MIT. Nenhum dado real: os 40 pacientes, os CPFs (com dígito verificador
válido) e a agenda inteira são gerados por `Random(42)` e reconstruídos com um
comando.
