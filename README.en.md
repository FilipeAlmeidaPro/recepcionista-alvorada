# Voice Agent Assistant

A Brazilian-Portuguese voice agent that books appointments for a
multi-specialty clinic. The demo clinic — **Clínica Alvorada** — is fictional,
and the 40 patients, their ID numbers and the whole calendar come from
`Random(42)`. It identifies the caller, understands a spoken
constraint, queries a real calendar, offers a slot, confirms out loud, and
writes — or escalates to a human when that's the right call.

*[Versão em português](README.md)* · **[Architecture →](https://claude.ai/code/artifact/8e4cad9f-5b25-4a44-b1da-aa158f092b54)** · **[Who checks whom →](https://claude.ai/code/artifact/07ffe974-f74d-48f3-80cc-4c072503a993)** ([sources](docs/))

The project chases a single scenario, in depth:

> **"I need an orthopedist, but I'm only free after six."**

---

## The thesis

**The model proposes; the code writes.**

No write tool is ever called directly by the LLM. It emits an *intent*, and a
deterministic validator checks ten rules before anything touches the database.
Entity hallucination isn't a metric in a report here — it's an executable rule
that blocks the write.

Small scope, high depth, failure exposed on purpose. An agent that does six
things with reliability numbers attached beats one that does twelve with none.

---

## Run it

```bash
git clone <this-repo> && cd voice-agent-assistant
python3 -m clinica.seed --data-base 2026-09-03   # builds data/clinica.db
python3 -m unittest discover -s . -t .           # 210 tests
python3 -m avaliacao --provedor simulado         # 40 scenarios, no LLM, US$ 0
```

**Zero dependencies.** `sqlite3`, `unittest`, `urllib` and `wave` are stdlib.
The zero-cost constraint starts here, not at the API choice.

To run against a real LLM (free tier, no credit card):

```bash
cp exemplo.env .env    # then fill GROQ_API_KEY — console.groq.com/keys
                       # .env is gitignored; an exported var always wins
python3 -m avaliacao --provedor groq --painel panel.html
python3 -m avaliacao.audio                 # audio suite
python3 -m clinica.ligar --falas "Good evening, I need an orthopedist" \
                                 "Only after six" --ouvir
python3 -m clinica.servidor                # browser demo, with a microphone
```

### The browser demo

```bash
python3 -m clinica.servidor    #  http://127.0.0.1:8800
```

Hold the button (or the space bar), speak, release. The browser captures the
mic with `MediaRecorder`; the server — stdlib `http.server`, zero dependencies
— converts, transcribes, runs the turn and returns the spoken reply **plus the
trace**.

The on-screen trace is what separates this from a black box: the constraint the
normalizer extracted (and whether it was a guess), every tool called, every
validator rule that passed or blocked, and the milliseconds of each stage
against the 800 ms target.

### Hands-free and barge-in

The "hands-free" checkbox turns on a **browser-side VAD**: it opens the turn
when you start speaking, closes it when you stop, and **cuts the agent off
mid-sentence if you talk over it**. A live meter shows the level and the
threshold — you can watch the VAD decide.

Rethinking the problem: barge-in is deciding *when to stop playing audio*, and
turn detection is VAD. Both belong on the client. The plan called for Silero;
Silero is better, and it's a model plus PyTorch — the difference between
`git clone && python3` and twenty minutes of installation. Energy with
hysteresis and a noise floor measured from the recording itself is enough for a
phone call.

`clinica/vad.py` is the same logic in Python, with **14 tests** over
code-generated audio: digital silence, a quiet room, a short pause that must
not end the turn, a click that must not open one, and a noisy background. The
browser mirrors the parameters. It also trims silence before sending to
Whisper — two seconds of silence cost quota and add no letters.

**What's still missing:** *streaming* transcription. Without it the agent only
starts thinking once you finish speaking. That needs paid streaming STT — the
same conclusion the latency table had already reached from another direction.

`silencio_final_s` is the most expensive parameter in the system: too short and
it truncates speech, and the audio suite measured the price — entity extraction
drops from **90% to 60%**.

> The codebase, prompts and CLI are in Portuguese — the product is a
> Brazilian clinic receptionist, and mixing languages in domain code makes it
> worse, not more accessible. This document is the English entry point.

---

## Architecture

```
    caller's speech
          │
          ▼
    ┌───────────────┐
    │ STT — Whisper │  large-v3-turbo @ Groq        785 ms
    └───────┬───────┘
            ▼
    ┌───────────────────────────────┐
    │ PT-BR normalizer              │  deterministic   0.005 ms
    │ "depois das seis" → 18:00     │
    │ "quatro, não, dois" → "42"    │
    └───────────────┬───────────────┘
                    ▼
    ┌───────────────────────────────┐
    │ Orchestrator (LLM + 6 tools)  │  Groq / Gemini  1,446 ms
    │        ↓ proposes an intent   │
    └───────────────┬───────────────┘
                    ▼
    ┌───────────────────────────────┐
    │ VALIDATOR — 10 rules          │  deterministic   0.084 ms
    │ R1 key       R6 specialty     │
    │ R2 patient   R7 constraint    │
    │ R3 slot      R8 confirmation ★│
    │ R4 future    R9 was offered   │
    │ R5 free      R10 origin       │
    └───────────────┬───────────────┘
                    ▼            ╳ rejected → the agent explains and re-proposes
    ┌───────────────────────────────┐
    │ Database (SQLite)             │
    │ partial unique index:         │
    │ double-booking is impossible  │
    └───────────────┬───────────────┘
                    ▼
    ┌───────────────┐
    │ TTS — say     │  Luciana voice, local          534 ms
    └───────┬───────┘
            ▼
     spoken reply                                full turn: 2,764 ms
```

---

## The four decisions that carry the project

### 1. Double-booking is impossible, not unlikely

The rule doesn't live in the prompt or in the function. It lives in a partial
unique index:

```sql
CREATE UNIQUE INDEX idx_um_confirmado_por_slot
    ON agendamentos(slot_id) WHERE status = 'confirmado';
```

The test `test_double_booking_e_impossivel_no_banco` bypasses the tool and
writes straight to the database. The database refuses.

### 2. R8 reads what the agent actually said, not what it claims it said

The validator doesn't only receive structured arguments — it receives **the
sentence the agent spoke out loud**, read from the transcript. If it queried
Tuesday and confirmed "Thursday", the entities diverge and the write is
blocked, even with every argument correct:

```
[BLOCKED]  agent confirmed the wrong day    R8_confirmacao
             └─ said weekday 3, the slot is 1
[BLOCKED]  agent named the wrong doctor     R8_confirmacao
             └─ said Dra. Larissa Nakamura, the slot is Dra. Thaís Bittencourt
[BLOCKED]  caller hesitated                 R8_confirmacao
             └─ reply «hmm, sei lá» read as «undetermined»
[WROTE]    everything checks out            rules passed: 10/10
```

Hesitation and silence are not consent. Cancelling requires the same
confirmation as booking — a mistaken cancellation costs the caller their slot
to whoever is next in line.

### 3. Three things the model does not control

- **The caller's constraint** is extracted from speech by the normalizer, turn
  by turn. The model picks the specialty; the code picks the filter. If the
  constraint came from the model's own summary, R7 would be validating the
  model against itself.
- **The confirmation sentence** evaluated by R8 comes from the transcript.
- **The write**, which goes entirely through the validator.

### 4. Privacy in three layers, not just in the schema

Masking the database column isn't enough — the number shows up raw in speech.

1. `buscar_paciente` returns `***.***.789-01`, never the full document number.
2. `mascarar_falado` erases **dictated** documents from inside the transcript,
   as digits or spelled out. The transcript is what reaches the trace, the
   panel and the logs.
3. Once the caller is identified, the document leaves the history sent to the
   model.

Ordinary speech with short numbers (`"pode ser às 18h30"`) is left alone.

### Retention: deleting is the hard part

Recording a call is easy. What the law asks for is **deletion**, and personal
data kept without a defined window is data kept by neglect.

| layer | window | why |
|---|---:|---|
| **transcript** | **90 days** | where the health data lives — "chest pain" identifies more than an ID number |
| operational metadata | 730 days | when they called, whether it booked, contact reason; runs the operation |
| the appointment itself | untouched | separate record, separate legal basis |

What identifies goes; what manages stays.

```bash
python3 -m clinica.retencao --simular   # shows what would go
python3 -m clinica.retencao             # applies it
```

Ten tests cover this, including idempotency and the guarantee that purging
never touches an appointment.


---

## Multi-agent orchestration — and what it failed to prove

```
                     ┌──────────────────────────┐
     caller's turn ──┤  Supervisor (code)       │
                     └───────┬──────────┬───────┘
                             │ parallel │
              ┌──────────────┘          └──────────────┐
              ▼                                        ▼
   ┌──────────────────────┐              ┌──────────────────────┐
   │ Orchestrator         │              │ Risk guardian        │
   │ 6 tools · ~3,000 tok │              │ 1 question · ~80 tok │
   │ 1,000–1,500 ms       │              │ 615 ms · disappears  │
   └──────────┬───────────┘              └──────────┬───────────┘
              │                                     │ risk?
              └──────────────┬──────────────────────┘
                             ▼
                   preempt: discard the orchestrator's
                   reply and escalate
```

**The supervisor is code, not a model.** An LLM supervisor routing between
sub-agents would add a network hop per turn to a system already 3.5× over its
latency target, plus one more surface for hallucination, to decide something an
`if` decides better.

What *is* multi-agent are the **specialists**: a risk guardian running **in
parallel** (not in front) and a scribe writing the handoff summary **after**
the call. Neither sits on the turn's critical path.

### My hypothesis was wrong

I wrote that the guardian existed because "the orchestrator is busy and will
miss a signal buried mid-sentence." I measured it
(`python3 -m avaliacao.risco`) and **that isn't true**:

| | risk signals caught | false positives |
|---|---|---|
| Guardian (`gpt-oss-20b`) | **4/4** | **0/3** |
| Orchestrator (`gpt-oss-20b`) | **4/4** | 1/3 |
| Orchestrator (`gpt-oss-120b`) | caught the buried case alone | — |

**In no measured case did the guardian catch something the orchestrator
missed.** If that were the argument, the layer wouldn't pay for itself.

What the measurement did show is smaller and true: **a specialised classifier
beats a busy generalist on precision.** The guardian scored 7/7; the cheap
orchestrator scored 6/7 — it escalated a chest pain *from last year, already
investigated*, which the guardian correctly declined.

So the demonstrated value is: better precision, ~600 ms that vanish into the
parallelism, ~3% of the turn's tokens. The guardian stays as a **safety floor
independent of the orchestrator's model** — and that part is still unproven,
because the daily quota ran out before I could test an even cheaper
orchestrator.

One thing it deliberately does **not** do: suppress an escalation. A guardian
that cancels the orchestrator's alarm could silence a real risk, and that's the
one error this system cannot make.

**18 tests** cover the layer, including preemption, non-duplication when the
orchestrator already escalated on its own, and proof that running in parallel
costs the larger of the two times rather than their sum.

## The numbers

All measured in this repository, none estimated.

### Eval suite — 40 scenarios in 7 families

Happy path, scope limits, clinical risk, calendar, identity, confirmation,
dialogue robustness. The assertion is never about the text the LLM produced —
it's about what happened: did it book, did it escalate, for what reason, which
tools it called, what the validator blocked, and whether what got written
respects the spoken constraint. Prompts and models change; these assertions
survive.

| Agent | Scenarios | Result |
|---|---|---|
| rule-based baseline, no LLM | 40 | **37/40** |
| Groq `gpt-oss-120b` | A1–E2 (28) | **24/28** |
| Groq `gpt-oss-20b` | E3–F7 (12) | **8/12** |

**All 40 scenarios have now run against a real LLM.** But across two different
models, because the daily quota doesn't fit 40 in one go — which is why I
**don't add the two numbers up**. 24/28 and 8/12 measure different models;
writing "32/40" would invent a run that never happened.

Beyond task completion, the suite reports a **recovery rate**: of the 16
scenarios where something goes wrong mid-conversation — the caller hesitates,
corrects themselves, goes quiet, interrupts, changes their mind or drifts off
topic — how many still finish the task. That's what separates an agent that
leads a conversation from one that only works on the happy path. The
deterministic baseline scores **14/16 (88%)**.

By family, on `gpt-oss-120b`:

| family | result |
|---|---|
| happy path | **6/6** |
| identity | **6/6** |
| confirmation | **2/2** (partial) |
| clinical risk | **2/2** |
| scope limits | 4/5 |
| calendar | 4/7 |

> I measured 23/28 and report 24/28. The difference is a **false positive of my
> own**: in B5 the agent said "may I confirm your ID?" on the first turn,
> *before* chest pain was mentioned, and my regex caught that innocent line.
> The transcript shows the agent escalated immediately and said nothing else —
> it was right. The assertion now only looks at what was said **after**
> escalating, and two tests cover both sides. Claiming the score means claiming
> the correction with it.

> **92% for a receptionist made of 150 lines of `if` is a bad finding, not a
> win.** It means that with a scripted caller the suite tests the machine, not
> the model — the script hands over the right line at the right moment. The
> discriminating power lives in the synthetic caller (`--paciente sintetico`),
> a second LLM with a private goal that follows no script. Scripted mode stays
> useful as a deterministic regression net in CI.


### The model invented a price

The strongest finding of the Groq run. Scenario B4 asks what a consultation
costs. The system **has no pricing or insurance data at all** — no table, no
tool, nothing.

```
caller: How much does an orthopedics appointment cost?
 agent: An orthopedics appointment costs R$ 200.00.
caller: And do you take Unimed?
 agent: Yes, we accept Unimed.
```

Two inventions, delivered with the confidence of real data, without calling a
single tool. This is exactly the class of error the validator does **not**
catch: it guards the write, not the speech. The only defence is an eval
assertion — and that's what caught it.


### When two models disagree with your label, the label is what's wrong

Scenario F5 expected escalation with reason `frustracao`. Both models
classified it as `pedido_do_paciente` — and they were right: *"I want to speak
to a person"* **is literally a request**. The assertion now accepts both, since
what matters is escalating, and escalating fast, not the taxonomy.

And three scenarios were failing on bad data, not on the agent:

```
caller: «dictated phone number»
 agent: Sorry, the ID number you gave has only nine digits.
        Could you repeat it, please?
```

My fixture built the phone number without the area code — nine digits, which
**no Brazilian would ever say**. The model assumed an incomplete ID and asked
again. Right once more. Three tests now guard fixture realism, because
unrealistic data makes a correct model look wrong.

### The synthetic caller ran for the first time — and tested my harness

The claim above (*"the discriminating power lives in the synthetic caller"*)
went a long time without proof. I ran it, and the first run **never got to test
the agent**: it exposed two defects of my own first.

```
caller: Hello! This is Thaís. I'd like to book an appointment.
 agent: Which specialty do you need?
caller: Dermatology, please.              ← scenario A1 is ORTHOPEDICS
   …
caller: That works, please confirm. Thanks! ENCERRAR
 agent: I have nothing within what you asked for…   ← and seven more times
```

1. **The caller's goal was the scenario title** — "Booking with a time
   constraint" — which doesn't name the specialty. The caller invented its own.
   The `objetivo` field had existed from the start and had never been exercised.
   The script is now a **briefing**: the caller pursues the same goal in its own
   words.
2. **The `ENCERRAR` sentinel was only detected at the start of a line**, and the
   model writes `"Thanks! ENCERRAR"`. The call never ended.
3. And one that's the agent's, not mine: **the baseline loops**, repeating the
   same reply. The runner now ends the call on repetition — a loop is not a test
   result.

**The claim still has no proof.** What changed is that synthetic mode now works,
with six tests covering both defects. Running it for real costs an entire daily
quota, and that quota ran out on both models mid-attempt.

### Entity extraction — 50 labelled utterances

| method | digits | time | date | name | specialty | **total** |
|---|---:|---:|---:|---:|---:|---:|
| naive regex | 0% | 8% | 0% | 12% | 25% | **8%** |
| raw LLM (`gemini-3.5-flash`) | 100% | 100% | 100% | 100% | 67% | **93%** |
| deterministic normalizer | 100% | 100% | 100% | 100% | 100% | **100%\*** |

\* partly circular — I wrote both the cases and the code.

**The raw LLM gets 93%.** That contradicts the premise that spoken-Portuguese
normalization is where everything breaks, and it changes the argument for the
normalizer. It isn't justified by accuracy:

1. **660,000× faster** — 0.005 ms against 3,300 ms. On a channel targeting
   800 ms per turn, spending 3.3 s to learn that "after six" means 18:00 is not
   an option.
2. **It consumes no quota** — every entity the LLM extracts is an API call.
3. **Determinism, which is what R7 needs** in order not to validate the model
   against itself.

The missing 7% alone wouldn't pay for 300 lines of parser. Latency and
determinism do.

### Audio suite — 10 utterances × 5 conditions

| condition | time | date | digits | specialty | **total** |
|---|---:|---:|---:|---:|---:|
| clean | 3/3 | 2/2 | 2/3 | 2/2 | **90%** |
| light noise | 3/3 | 2/2 | 3/3 | 2/2 | **100%** |
| heavy noise | 3/3 | 2/2 | 3/3 | 2/2 | **100%** |
| truncated | 3/3 | 1/2 | 0/3 | 2/2 | **60%** |
| hard voice | 0/3 | 0/2 | 0/3 | 0/2 | **0%** |

Three conclusions only the audio suite can produce:

- **Noise isn't the problem; truncation is.** This contradicts the original
  plan. The real risk is the VAD cutting speech short, not background noise.
- **Input voice quality dominates everything.** With one of the macOS novelty
  voices, "ortopedista" becomes "a morta pedista" and accuracy hits zero.
- **A truncated phone number becomes `1198765` — plausible and wrong.** That's
  the worst possible failure, and it's exactly why digit-by-digit confirmation
  exists.

### Latency per stage

| stage | p50 | against the 800 ms target |
|---|---:|---|
| STT — Whisper large-v3-turbo @ Groq | 785 ms | nearly the whole budget |
| Orchestrator + LLM — `gpt-oss-120b` @ Groq | 1,446 ms | **over the target alone** |
| TTS — `say`, local | 534 ms | |
| **full turn** | **2,764 ms** | **3.5× the target** |
| normalizer | 0.005 ms | statistical noise |
| calendar query (819 slots) | 0.068 ms | statistical noise |
| validator, 10 rules | 0.084 ms | statistical noise |

Across the 28-call run (139 turns), the full turn measured **p50 1,212 ms,
p95 2,543 ms** — discarding 27 turns polluted by rate-limit waiting. With them
included the p95 jumps to 57,605 ms, which isn't inference latency and would be
dishonest to report as such.

**The write gate costs nothing.** The bottleneck is batch STT — Groq's Whisper
doesn't stream, so you segment with VAD and send the chunk. Paid streaming STT
removes it; nothing else does.

One trick that buys perceived speed for free: saying *"let me check the
calendar"* while the query runs hides ~800 ms.

### What free actually cost

| Provider | Binding limit | Consequence |
|---|---|---|
| Gemini | **20 requests/day per model** | ~4 calls per day |
| Groq | **200,000 tokens/day** | ~1.3 suite runs per day |
| Groq | 8,000 tokens/minute | a run takes tens of minutes |
| Groq Whisper | 7,200 audio-seconds/hour | **separate** quota — audio suite runs freely |
| macOS `say` | none | actually free, not free tier |

Measured per-call latency: Groq `gpt-oss-120b` **804 ms**, `qwen3.8-27b`
649 ms · Gemini `flash-lite` 2,600 ms, `3.5-flash` 3,700 ms, `3.8-flash`
41,600 ms.

> The newest Gemini model is the worst of the four for this task: heavy
> reasoning that costs 16× the latency and still breaks the output format.

---

## What the suite caught

Each of these is a real bug, caught by a specific layer.

| Finding | Which layer caught it |
|---|---|
| `"boa noite"` (good evening) parsed as an 18:00–21:00 constraint — on the opening line of every call | unit tests |
| The agent escalated for chest pain **and kept offering slots** in the same turn | the rendered panel |
| Tool schema without `null`: Groq validates server-side and rejected 6 scenarios | running against the real provider |
| Gemini 3.x rejects the next call if `thought_signature` isn't echoed back | running against the real provider |
| The model replies in markdown; TTS would read `- 18h00` as "hyphen eighteen" | running against the real provider |
| STT hears **"não" as "nove"** and mid-dictation correction becomes `4392` | audio suite |
| `"depois DAR seis"` — STT swaps a function word and the parser breaks | audio suite |
| `"de manhã, antes das onze"` returned only `until 11:00` | audio suite |

The panel one deserves a note: **the test was passing.** It only forbade
`propor_reserva`, and the agent indeed didn't book. But continuing to serve
after escalating clinical risk is worse than never escalating. The only way to
see it was to look at the rendered call.

---

## The panel

```bash
python3 -m avaliacao --provedor simulado --painel panel.html
```

Single-file HTML, zero dependencies. The original plan called for Streamlit; I
didn't use it, because the whole project runs on bare `python3` and a panel
that needs `pip install streamlit` plus a local server is worse to share than a
file you open by double-clicking.

Per run: task completion, turns, p95 latency against target, cost. Per family.
Validator blocks by rule. Escalations by reason. Call by call: masked
transcript, tools, blocks, per-turn latency, tokens.

Colors validated with a script, not picked by eye: `#4ade80` / `#f0705a` on
`#0b0b0b` separate at ΔE 9.7 under deuteranopia and pass contrast. No state is
communicated by color alone.

---

## Layout

```
clinica/
  db.py            schema, privacy masking, PT-BR date/time rendering
  seed.py          catalogue, 3 weeks of calendar, deterministic occupancy
  tools.py         the 6 tools
  normalizador.py  spoken constraints, dictated digits, names, voice output
  validador.py     the 10 rules — the write gate
  agente.py        call orchestrator
  provedor.py      LLM interface (Groq/Gemini), retry, quota handling
  voz.py           TTS (say) and STT (Whisper), with per-stage timing
  ligar.py         end-to-end call CLI
  servidor.py      browser demo (stdlib http.server)
  orquestracao.py  deterministic supervisor, risk guardian, scribe
  vad.py           energy-based speech detection with an adaptive floor
  retencao.py      transcript retention policy, with a CLI

web/index.html     the page: push-to-talk and the live trace

avaliacao/
  cenarios.py      40 scenarios in 7 families
  paciente.py      scripted caller and synthetic caller
  runner.py        executes and judges
  simulado.py      rule-based receptionist — baseline without an LLM
  entidades.py     entity error rate, 3 methods
  audio.py         audio suite, 5 conditions
  painel.py        HTML panel
  relatorio.py     aggregate metrics

tests/             210 tests, stdlib
```

**25 modules, 7,631 lines.** Database: 4 specialties, 6 professionals,
40 patients, 819 slots across 3 weeks.

Scarcity in the seed is designed, not random: **only Dra. Thaís Bittencourt
covers orthopedics after 18:00, and only on Tuesdays and Thursdays.** That's
what gives the demo scenario real tension instead of returning the first page
of an empty calendar.

---

## What isn't here, and why

**Deliberately out of scope:** insurance plans, pricing, ERP, payments,
outbound calls, sales funnel. Architecture sketched for all of them, none
implemented.

**The missing layer:** full-duplex with barge-in. Browser push-to-talk works
(`python3 -m clinica.servidor`); interrupting the agent mid-sentence is what
needs continuous VAD and streaming — Pipecat with `SmallWebRTCTransport`, and
the project's first dependency.

**Why not Vapi, Retell or n8n:** they hide exactly what this project sets out
to show — the per-stage trace, turn control, the validation layer. I'd be
differentiating on what the tool does for me. On a short deadline in
production, I'd use one. In a technical walkthrough, no. n8n has a place as an
asynchronous control plane (WhatsApp confirmations, D-1 follow-up, CRM), never
as the data plane of a voice turn.

---

## Questions that usually come up

**"How do you prevent hallucination?"**
Not in the prompt. In the architecture: the model doesn't write, it proposes.
Every write passes ten rules in code, and R8 compares what it said out loud
against what it was about to store.

**"Does this scale?"**
The bottleneck is the free stack's batch STT. Swap in paid streaming STT and
run the orchestrator stateless behind a queue, and it scales horizontally. The
real contention point is the database, handled by optimistic locking — already
implemented via idempotency.

**"What does a call cost?"**
It's in the panel. On this stack, US$ 0. At paid reference pricing, ~US$ 0.013
for a 3.3-turn call.

**"What would you do differently?"**
Start with the eval suite before anything else. And run against a real provider
on day one — four of the eight most interesting bugs only surfaced there.

---

## License

MIT. No real data: the 40 patients, their document numbers (with valid check
digits) and the entire calendar are generated by `Random(42)` and rebuilt with
one command.
