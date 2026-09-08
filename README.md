# Voice Agent Assistant

Agente de voz para agendamento em clínica multidisciplinar, **em português do
Brasil e em inglês**. A clínica da demo — **Clínica Alvorada** — é fictícia, e
os 40 pacientes, os CPFs e a agenda inteira são gerados por `Random(42)`. Ele
identifica o paciente — ou abre a ficha na hora, se for a primeira ligação —,
entende uma restrição falada, consulta a agenda real, propõe horário, confirma
em voz alta e grava. Ou transfere para um humano, quando é o certo a fazer.

*[English version](README.en.md)* · **[Arquitetura →](https://claude.ai/code/artifact/163f86c8-fe48-46af-8029-563ef00dbcb7)** · **[Quem confere quem →](https://claude.ai/code/artifact/ba4ef3ef-7ceb-4c8b-8c12-b49c76225e7f)** ([fontes](docs/)) · *[in English](https://claude.ai/code/artifact/8e4cad9f-5b25-4a44-b1da-aa158f092b54)*

O cenário que o projeto persegue é um só, feito a fundo:

> **"Quero marcar um ortopedista, mas só consigo depois das seis."**
>
> **"I need an orthopedist, but I can only do after six."**

---

## A tese

**O modelo propõe; o código escreve.**

Nenhuma ferramenta de escrita é chamada direto pelo LLM. Ele emite uma
*intenção*, e um validador determinístico confere treze regras antes de qualquer
coisa tocar o banco. Alucinação de entidade não é uma métrica de relatório
aqui — é uma regra executável que bloqueia a escrita.

Escopo pequeno, profundidade alta, falha exposta de propósito. Um agente que
faz seis coisas com número de confiabilidade em cima vale mais que um que faz
doze sem nenhum.

---

## Rodar

```bash
git clone <este-repo> && cd voice-agent-assistant
python3 -m clinica.seed --data-base 2026-09-03   # gera data/clinica.db
python3 -m unittest discover -s . -t .           # 324 testes
python3 -m avaliacao --provedor simulado         # 46 cenários, sem LLM, US$ 0
```

**Zero dependências.** `sqlite3`, `unittest`, `urllib` e `wave` são stdlib. A
restrição de custo zero começa aqui, não na escolha de API.

Para rodar contra um LLM de verdade (free tier, sem cartão):

```bash
cp exemplo.env .env    # e preencha GROQ_API_KEY — console.groq.com/keys
                       # o .env está no .gitignore; variável exportada na mão vence
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

### Mãos livres e barge-in

A caixa "mãos livres" liga um **VAD no navegador**: ele abre o turno quando
você começa a falar, fecha quando você para, e **cala o agente no meio da frase
se você falar por cima**. Um medidor mostra o nível e o limiar ao vivo — dá
para ver o VAD decidindo.

Repensando o problema: barge-in é decidir *quando parar de tocar o áudio*, e
detecção de turno é VAD. Os dois cabem no cliente. O plano previa Silero;
Silero é melhor, e é um modelo mais PyTorch — a diferença entre
`git clone && python3` e vinte minutos de instalação. Energia com histerese e
piso de ruído medido na própria gravação resolve numa ligação telefônica.

`clinica/vad.py` é a mesma lógica em Python, com **14 testes** sobre áudio
gerado por código: silêncio digital, sala quieta, pausa curta que não pode
encerrar o turno, estalo que não pode abrir, e fundo ruidoso. O navegador
espelha os parâmetros. Ele também serve para recortar silêncio antes de mandar
ao Whisper — dois segundos de silêncio custam cota e não acrescentam uma letra.

**O que ainda falta:** transcrição em *streaming*. Sem ela o agente só começa a
pensar quando você termina de falar. Isso exige STT em streaming pago — é a
mesma conclusão a que a tabela de latência já tinha chegado por outro caminho.

O `silencio_final_s` é o parâmetro mais caro do sistema: curto demais trunca a
fala, e a suíte de áudio mediu o preço — extração de entidade cai de **90% para
60%**.

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
    │ Orquestrador (LLM + 7 tools)  │  Groq / Gemini  1 446 ms
    │        ↓ propõe uma intenção  │
    └───────────────┬───────────────┘
                    ▼
    ┌───────────────────────────────┐
    │ VALIDADOR — 13 regras         │  determinístico  0,084 ms
    │ R1 chave    R8 confirmação ★  │
    │ R2 paciente R9 oferecido      │
    │ R3 slot     R10 origem        │
    │ R4 futuro   R11 dados         │
    │ R5 livre    R12 CPF           │
    │ R6 especial.R13 duplicado     │
    │ R7 restrição                  │
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

## As sete ferramentas

O orquestrador é o único que segura ferramenta — e **nenhuma delas escreve no
banco por conta própria**. As quatro que começam com `propor_` levam o nome
literalmente: montam uma *intenção* e entregam ao validador, que decide. As
outras três leem, ou passam a ligação adiante.

| Ferramenta | O que faz | Escreve? | Regras que atravessa |
|---|---|:--:|---|
| `buscar_paciente` | acha a ficha por telefone ou CPF; devolve o documento mascarado | lê | — |
| `consultar_agenda` | horários livres da especialidade, já filtrados pela restrição que o normalizador extraiu da fala | lê | — |
| `propor_cadastro` | abre a ficha de quem nunca ligou — nome e telefone | **propõe** | R1, R8, R11, R12, R13 |
| `propor_reserva` | marca um horário já oferecido e já confirmado em voz alta | **propõe** | R1–R9 |
| `propor_reagendamento` | move um agendamento ativo para outro horário | **propõe** | R1–R9, mais R10 na origem |
| `propor_cancelamento` | desmarca; exige a mesma confirmação verbal que marcar | **propõe** | R1, R8, R10 |
| `transferir_para_humano` | encerra e passa adiante, com motivo e resumo | registra | — |

A R7 só entra quando o paciente declarou uma restrição — sem restrição não há o
que violar. A R12 só entra quando há CPF: documento ausente não é documento
inválido.

**O modelo nunca viu as funções de escrita.** Ele não conhece
`reservar_horario`, `cadastrar_paciente`, `reagendar` nem `cancelar` — só as
versões `propor_`, que passam pelo portão. A tese inteira cabe numa convenção
de nomes.

Detalhe de cada uma, com o desenho do portão: **[Arquitetura →](https://claude.ai/code/artifact/163f86c8-fe48-46af-8029-563ef00dbcb7)**

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

### Retenção: apagar é a parte difícil

Gravar a ligação é fácil. O que a LGPD cobra é **apagar**, e dado pessoal
guardado sem prazo definido é dado guardado por descuido.

| camada | prazo | por quê |
|---|---:|---|
| **transcrição** | **90 dias** | é onde mora o dado de saúde — "dor no peito" identifica mais que um CPF |
| metadados operacionais | 730 dias | quando ligou, se agendou, motivo do contato; sustenta a operação |
| o agendamento em si | intocado | outro registro, outra base legal |

Some o que identifica, fica o que gerencia.

```bash
python3 -m clinica.retencao --simular   # mostra o que sairia
python3 -m clinica.retencao             # aplica
```

Dez testes cobrem isso, incluindo a idempotência e a garantia de que a purga
não encosta no agendamento.


---

## Cadastro pela voz — nome e telefone

Um paciente novo não fica de fora: o agente abre a ficha na própria ligação e
agenda em seguida. Ele pede **duas coisas**: nome completo e telefone.

Não pede CPF. Quem liga de fora do país não tem um, e exigir documento para
marcar consulta transforma um cadastro de dois campos numa entrevista. Se o
paciente oferecer o CPF, ele é aceito — e conferido.

A escrita é uma **proposta**, igual à reserva:

| Regra | O que ela impede |
|---|---|
| **R11 dados** | ficha nascida sem nome completo ou sem telefone |
| **R12 CPF** | documento cujos dígitos verificadores não fecham — só dispara quando há CPF |
| **R13 duplicado** | o mesmo telefone (ou o mesmo CPF) cadastrado em dois nomes |

Mais a R8 outra vez, com as entidades da ficha: o agente tem de ter lido o nome
e o telefone **em voz alta** e ouvido um sim. Sem CPF, **o telefone é a
identidade**: se ele leu um número e gravou outro, a pessoa fica com um
cadastro que nunca mais vai encontrar — e nada nisso dá erro.

A R12 é código e não prompt por um motivo simples: o modelo não calcula dígito
verificador. Um CPF inventado não dá erro nenhum; ele só existe, e colide anos
depois com o cadastro verdadeiro de outra pessoa.

Repetir o mesmo cadastro é idempotente, não conflito. A rede cai, o paciente
confirma duas vezes; responder *"esse telefone já está cadastrado"* para quem
acabou de se cadastrar seria um bug com cara de proteção.

No banco, `cpf` e `nascimento` viraram opcionais — e o `UNIQUE` do CPF
continua valendo, porque no SQLite `NULL` não colide com `NULL`. Bancos
semeados antes disso são migrados por reconstrução da tabela: `CREATE TABLE IF
NOT EXISTS` não altera nada, e o SQLite não solta um `NOT NULL` com `ALTER`.

### O que o cenário de cadastro encontrou

Escrever o cenário A2 custou três bugs, todos reais:

| Achado | Consequência |
|---|---|
| `1980-03-15` era recusado pelo normalizador de datas | o formato que o **modelo** emite num campo de data derrubava o cadastro por "faltam dados", com todos os dados corretos na mão |
| `"nasci em quinze de março de oitenta"` virava restrição de agenda | toda ligação de cadastro filtrava a agenda pelo **aniversário** de quem ligou, e não achava horário nenhum |
| `"Está correto? (aguardando resposta)"` ia inteiro para o TTS | o modelo narrando o próprio controle de fluxo, lido em voz alta como "abre parênteses aguardando resposta" |

O terceiro é a mesma ideia do validador aplicada à saída: pedir por prompt e
**conferir por código**.

Na eval, o modelo ainda tentou cadastrar antes de ter os dados na mão — e o
portão barrou. Ele se recuperou, pediu de novo e concluiu a ligação. É
exatamente o comportamento que o portão existe para produzir: o modelo erra, o
código segura, a ligação continua.

---

## Atendimento em inglês

O agente atende nas duas línguas. O que tornou isso barato não foi um
normalizador maior — foi separar **o algoritmo** das **palavras**. O parser de
restrição é um só (707 linhas); o que muda com o idioma é uma tabela de 338, em
`clinica/idioma.py`.

```
"depois das seis"          →  18:00, ambíguo
"after six"                →  18:00, ambíguo      (mesma heurística de clínica)
"only after 6pm"           →  18:00              (o "pm" desfaz a ambiguidade)
"de manhã, antes das onze" →  06:00 – 11:00
"in the morning, before eleven" → 06:00 – 11:00
"seis e meia"              →  18:30
"half past six"            →  18:30              (a ordem inverte)
```

A detecção é determinística — palavras funcionais, não LLM — e **empata para o
idioma corrente**: `"ok"` existe nas duas línguas, e trocar de idioma porque o
paciente respondeu uma monossílaba é pior do que ter começado na língua errada.

### Por que o idioma desce até o validador

Essa é a parte que não era óbvia. A R8 confere o que o agente falou em voz
alta. Medido:

```
"Friday, September 4, at 8 am"  lido com as tabelas em português
  → {hora: None, dia_semana: None, dia_mes: None, mes: None}
```

Nenhuma entidade. A regra então falha **fechada**: reprova toda ligação em
inglês por *"o agente não disse o horário em voz alta"*, inclusive quando ele
disse. Não é um buraco de segurança — é a impossibilidade de atender no idioma.
Por isso `Idioma` atravessa o normalizador e o validador, e não só o prompt.

No consentimento a colisão é pior: **`"no"` é negação em inglês e preposição em
português.** Uma lista só, somando as duas línguas, leria *"pode ser no dia
quinze"* como recusa.

A voz também troca: `say` com a Luciana lendo inglês produz inglês com fonética
portuguesa. Em inglês a demo usa Samantha, e o Whisper recebe `language=en`.

### O que ainda não foi medido

O prompt do sistema **não** é traduzido — as regras de operação são as mesmas, e
duas versões divergem na primeira correção feita só de um lado. Entra uma
diretriz de idioma e as palavras que o agente fala. **Essa decisão não foi
comparada com a alternativa**, porque a cota diária dos dois provedores gratuitos
acabou antes.

O que foi medido, e vale exatamente o que diz:

| Rodada | Resultado |
|---|---|
| Groq `gpt-oss-120b`, 4 cenários em inglês, **antes** da correção de especialidade | **1/4** — só o de risco clínico passou |
| Gemini `3.5-flash`, cenário G2 (paciente novo em inglês), **depois** da correção | **passou** |

O bug que a primeira rodada encontrou está corrigido e coberto por teste:
**`"orthopedist"` não casava com `Ortopedia` nem por prefixo** — "ortho" contra
"ortop". As outras três especialidades passavam por acidente ("derma", "cardi",
"neuro" coincidem nas duas línguas), o que é pior do que falhar: a regra
*parecia* funcionar. Agora cada idioma tem uma tabela de apelidos explícita.

O que a rodada da Groq também mostrou, e não está resolvido: o modelo entrou
num laço relendo o telefone de volta, e numa ligação afirmou ter marcado sem ter
marcado — o prompt proíbe isso explicitamente. Não sei ainda se o prompt em
português numa ligação em inglês contribui; é a medição que falta.

---

## Orquestração multi-agente — e o que ela não provou

```
                     ┌──────────────────────────┐
   fala do paciente ─┤  Supervisor (código)     │
                     └───────┬──────────┬───────┘
                             │ paralelo │
              ┌──────────────┘          └──────────────┐
              ▼                                        ▼
   ┌──────────────────────┐              ┌──────────────────────┐
   │ Orquestrador         │              │ Guardião de risco    │
   │ 7 tools · ~3 000 tok │              │ 1 pergunta · ~80 tok │
   │ 1 000–1 500 ms       │              │ 615 ms · some        │
   └──────────┬───────────┘              └──────────┬───────────┘
              │                                     │ viu risco?
              └──────────────┬──────────────────────┘
                             ▼
                   preempta: descarta a resposta
                   do orquestrador e escala
```

**O supervisor é código, não um modelo.** Um LLM supervisor roteando entre
sub-agentes acrescentaria um salto de rede por turno num sistema que já está
3,5× acima do alvo de latência, e mais uma superfície de alucinação, para
decidir o que um `if` decide melhor.

O que é multi-agente são os **especialistas**: um guardião de risco que roda
**em paralelo** (não antes) e um escriba que escreve o resumo do handoff
**depois** da ligação. Nenhum dos dois entra no caminho crítico do turno.

### A hipótese que eu tinha estava errada

Escrevi que o guardião existia porque "o orquestrador está ocupado e deixa
passar sinal enterrado no meio de outra frase". Medi
(`python3 -m avaliacao.risco`) e **não é verdade**:

| | sinais de risco pegos | falsos positivos |
|---|---|---|
| Guardião (`gpt-oss-20b`) | **4/4** | **0/3** |
| Orquestrador (`gpt-oss-20b`) | **4/4** | 1/3 |
| Orquestrador (`gpt-oss-120b`) | pegou o caso enterrado sozinho | — |

**Em nenhum caso medido o guardião pegou algo que o orquestrador perdeu.** Se
o argumento fosse esse, a camada não se pagaria.

O que a medição mostrou é menor e verdadeiro: **um classificador especializado
bate um generalista ocupado na precisão.** O guardião acertou 7/7; o
orquestrador barato acertou 6/7 — escalou uma dor no peito *do ano passado, já
investigada*, que o guardião corretamente recusou.

Então o valor demonstrado é: precisão maior, ~600 ms que somem no paralelismo,
~3% dos tokens do turno. O guardião fica como **piso de segurança independente
do modelo do orquestrador** — e essa parte ainda não foi provada, porque a cota
diária acabou antes de eu testar com um orquestrador ainda mais barato.

Uma coisa que ele **não** faz, de propósito: suprimir escalação. Um guardião
que cancela o alarme do orquestrador poderia silenciar um risco real, e esse é
o único erro que este sistema não pode cometer.

**18 testes** cobrem a camada, incluindo a preempção, a não-duplicação quando o
orquestrador já escalou sozinho, e a prova de que o paralelo custa o maior dos
dois tempos e não a soma.

## Os números

Todos medidos neste repositório, nenhum estimado.

### Suíte de eval — 46 cenários em 8 famílias

Caminho feliz, limites de escopo, risco clínico, agenda, identidade,
confirmação, robustez de diálogo, **atendimento em inglês**. A asserção nunca é
sobre o texto que o LLM produziu — é sobre o que aconteceu: agendou,
transferiu, com que motivo, quais ferramentas usou, o que o validador bloqueou,
e se o que ficou gravado respeita a restrição falada. Prompt e modelo mudam;
essas asserções sobrevivem.

| Agente | Cenários | Resultado |
|---|---|---|
| baseline de regras, sem LLM | 46 | **36/46** |
| Groq `gpt-oss-120b` | A1–E2 (28) | **24/28** |
| Groq `gpt-oss-20b` | E3–F7 (12) | **8/12** |
| Groq `gpt-oss-120b` | G1–G4, inglês | **1/4** |
| Gemini `3.5-flash` | G2, depois da correção | **passou** |

**Cada linha é uma rodada separada, e elas não se somam.** A cota diária
gratuita não comporta os 46 cenários de uma vez, então modelos diferentes
cobriram faixas diferentes. Escrever "32/40" a partir de 24/28 e 8/12 seria
inventar uma rodada que nunca existiu.

As duas últimas linhas são a família nova e merecem a ressalva completa: a
rodada 1/4 aconteceu **antes** da correção do apelido de especialidade que ela
mesma encontrou, e a única rodada depois da correção cobriu **um** cenário, em
outro modelo. A família em inglês ainda não foi medida inteira num estado
estável. O baseline de regras faz 0/4 nela — ele não fala inglês, e é assim que
deve ser.

Além da taxa de conclusão, a suíte reporta a **taxa de recuperação**: dos
16 cenários em que algo dá errado no meio — o paciente hesita, se corrige, se
cala, interrompe, muda de ideia ou sai do assunto —, quantos ainda terminam a
tarefa. É ela que separa um agente que conduz de um que só funciona no caminho
feliz. O baseline determinístico faz **14/16 (88%)**.

Por família, no `gpt-oss-120b`:

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


### Quando dois modelos discordam do seu rótulo, o rótulo é que está errado

O cenário F5 esperava transferência por `frustracao`. Os dois modelos
classificaram como `pedido_do_paciente` — e estavam certos: *"quero falar com
uma pessoa"* **é literalmente um pedido**. A asserção agora aceita os dois
motivos, porque o que importa é escalar, e rápido, não a taxonomia.

E três cenários falhavam por causa de um dado ruim, não do agente:

```
paciente: «telefone ditado»
  agente: Desculpe, mas o CPF que você digitou tem apenas nove números.
          Poderia repetir, por favor?
```

Minha fixture montava o telefone tirando o DDD — nove dígitos, que **nenhum
brasileiro dita**. O modelo achou que era um CPF incompleto e pediu para
repetir. Estava certo de novo. Três testes agora garantem que a fixture é
realista, porque dado irrealista faz o modelo parecer errado quando ele
acertou.

### O paciente sintético rodou pela primeira vez — e testou o meu harness

A afirmação acima (*"o poder discriminante mora no paciente sintético"*) ficou
muito tempo sem prova. Rodei, e a primeira rodada **não conseguiu testar o
agente**: expôs dois defeitos meus antes disso.

```
paciente: Alô, boa tarde! Aqui é a Thaís. Gostaria de marcar uma consulta.
  agente: Qual especialidade você precisa?
paciente: Dermatologia, por favor.          ← o cenário A1 é de ORTOPEDIA
   …
paciente: Serve, pode confirmar. Obrigada! ENCERRAR
  agente: Não tenho nada dentro do que você pediu…   ← e mais sete vezes
```

1. **O objetivo do paciente era o título do cenário** — "Agendamento com
   restrição de horário" —, que não diz a especialidade. O paciente inventou a
   dele. O campo `objetivo` existia desde o início e nunca tinha sido
   exercitado. Agora o roteiro vira **briefing**: o paciente persegue o mesmo
   objetivo com as palavras dele.
2. **O sentinela `ENCERRAR` só era detectado no início da frase**, e o modelo
   escreve `"Obrigada! ENCERRAR"`. A ligação nunca terminava.
3. E um do agente, não meu: **o baseline entra em laço**, repetindo a mesma
   resposta. O runner agora encerra ao detectar repetição — laço não é
   resultado de teste.

**A afirmação continua sem prova.** O que mudou é que o modo sintético agora
funciona, com seis testes cobrindo os dois defeitos. Rodar de verdade custa uma
cota diária inteira, e ela acabou nos dois modelos no meio da tentativa.

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
| validador, 13 regras | 0,084 ms | ruído estatístico |

Na rodada de 28 ligações (139 turnos), o turno inteiro deu **p50 1 212 ms,
p95 2 543 ms** — descartando 27 turnos contaminados por espera de rate limit.
Com eles, o p95 vai a 57 605 ms, que não é latência de inferência e seria
desonesto reportar como se fosse.


> **Primeira ligação com voz humana de verdade**, medida no navegador:
> STT **1 688 ms** + orquestrador **1 056 ms** + TTS **508 ms** = **3 251 ms**.
> O STT é o dobro do que eu media com fala sintetizada (785 ms) — voz humana é
> mais longa e mais hesitante que a do `say`, e o Whisper cobra por segundo de
> áudio. **Toda medição minha de latência com TTS sintético subestimava o STT.**

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

Mais três, todos achados ao escrever o cenário de cadastro — data em ISO
recusada, data de nascimento virando filtro de agenda, e rubrica de palco indo
para o TTS: [Cadastro pela voz](#cadastro-pela-voz).

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
  tools.py         as 7 ferramentas
  idioma.py        tabelas de pt-BR e inglês; detecção determinística
  normalizador.py  restrição falada, dígitos ditados, nomes, voz
  validador.py     as 13 regras — o portão de escrita
  agente.py        orquestrador da ligação
  provedor.py      interface de LLM (Groq/Gemini), retry, cota
  voz.py           TTS (say) e STT (Whisper), com tempo por estágio
  ligar.py         CLI de uma ligação ponta a ponta
  servidor.py      demo no navegador (http.server da stdlib)
  orquestracao.py  supervisor determinístico, guardião de risco, escriba
  vad.py           detecção de fala por energia, com piso adaptativo
  retencao.py      política de retenção de transcrição, com CLI

web/index.html     a página: push-to-talk e o trace ao vivo

avaliacao/
  cenarios.py      46 cenários em 8 famílias
  paciente.py      paciente roteirizado e paciente sintético
  runner.py        executa e julga
  simulado.py      recepcionista de regras — baseline sem LLM
  entidades.py     taxa de erro em entidades, 3 métodos
  audio.py         suíte de áudio, 5 condições
  painel.py        painel HTML
  relatorio.py     métricas agregadas

tests/             324 testes, stdlib
```

**37 módulos, 10 021 linhas**, testes incluídos. Banco: 4 especialidades, 6 profissionais,
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
escrita passa por treze regras em código, e a R8 compara o que ele falou em voz
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
