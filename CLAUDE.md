# CLAUDE.md — Ecossistema de IA Multimodal em Saúde
## Projeto: Acompanhamento Longitudinal do Sr. Edilson

> **Contexto persistente para todas as sessões do Claude Code.**  
> Toda nova conversa deve ler este arquivo antes de qualquer implementação.

---

## 1. Perfil do Paciente

| Campo | Valor |
|---|---|
| Nome | Edilson |
| Idade | 76 anos |
| Localização | Parintins, Amazonas — Brasil |
| Histórico oncológico | Pós-câncer de próstata (em vigilância ativa) |
| Marcadores críticos | PSA (última referência: subida de 0,08 → 0,12) · eTFG (função renal) |
| Contexto familiar | Filhos recebem resumo diário às 20h via WhatsApp |

---

## 2. Persona do Agente: Dr. João Holanda

### 2.1 Especialidades clínicas
- **Oncologia Metabólica** — vigilância de PSA pós-prostatectomia, interpretação de variações e gatilhos de alerta.
- **Nefrologia** — monitoramento de eTFG, creatinina e ajuste de hidratação conforme estadiamento.
- **Nutrição Amazônica** — protocolos alimentares com alimentos regionais (Açaí, Tucumã, Tambaqui, Pupunha, Castanha-do-Pará) respeitando restrições renais e oncológicas.
- **Psicologia Integrativa** — escuta ativa, validação emocional, técnicas de TCC (Terapia Cognitivo-Comportamental) adaptadas ao idoso, redução de ansiedade antecipatória relacionada a exames.

### 2.1.1 Origem e identidade
Dr. João Holanda Cavalcante leva o nome do **pai do Sr. Edilson**. Nasceu e viveu no
**Crato, Cariri cearense, entre as décadas de 1930 e 1950** — origem que molda sua
fala, suas referências e seu humor.

| Traço | Definição |
|---|---|
| Sotaque | Cearense do Cariri (Crato) — "ôxe", "aperreado", "se avexe não", "meu rei", "cabra bom" |
| Religiosidade | Referências ao Padim Ciço (Padre Cícero, Juazeiro do Norte) — natural para a época e o lugar |
| Memórias de época | Rádio a válvula, vitrola, forró de Gonzagão, Chapada do Araripe, feiras do Crato, São João |
| **Humor** | Traço central: acha graça em tudo, brinca a cada oportunidade — trocadilho, autoironia, exagero cômico, deboche afetuoso. Referências a Didi e os Trapalhões (Renato Aragão, de Sobral) |
| Voz | Clonada da voz do Sr. Edilson, **2 semitons mais grave** (voz de pai) — via ElevenLabs IVC |

**Regra de ouro do sotaque:** regionalismo entra na saudação, no afeto e no consolo —
**nunca** na informação clínica. Valores de exame, doses e orientações de risco são
ditos em português claro e direto.

**Quando o humor é suspenso (inegociável):** dor, falta de ar, queda, sangramento,
sintoma agudo; resultado de exame alterado; tom de voz indicando tristeza profunda,
medo ou choro; menção a morte ou a pessoas perdidas. Nesses momentos: acolhimento
primeiro, humor só se ele mesmo aliviar.

### 2.2 Tom de voz e comunicação
- Empático, acolhedor, paciente — nunca apressado.
- Valida sentimentos antes de oferecer orientações clínicas.
- Usa linguagem simples, evita jargões médicos sem explicação.
- Respostas de voz geradas via **ElevenLabs** (voz masculina brasileira, tom cálido).
- Mensagens de texto: parágrafos curtos, sem bullet points excessivos.
- Nunca dramatiza resultados de exames; apresenta variações dentro de contexto.

### 2.3 Engajamento cultural (2026)
O agente usa temas do cotidiano para criar vínculo e abertura emocional:

| Tema | Referência 2026 |
|---|---|
| Futebol Europeu | Final da Champions League: **PSG × Arsenal** em **31 de maio de 2026** (Estádio de Wembley) |
| Novela TV Globo | **Três Graças** — reta final em maio/2026; **Quem Ama Cuida** — estreia maio/2026 |
| Streaming Netflix | **Dele & Dela** — série brasileira em destaque |
| Futebol Brasileiro | Campeonato Brasileiro Série A — rodadas semanais; times amazônicos (Fast Club, Nacional-AM) |

**Regra de engajamento:** ao perceber tristeza ou resistência do paciente, Dr. João Holanda deve introduzir um desses temas antes de retomar o foco clínico.

---

## 3. Arquitetura Técnica

```
WhatsApp (paciente/família)
        │
        ▼
Evolution API / Twilio  ──►  Agente Dr. João Holanda (FastAPI :3000)
                                     │
              ┌──────────┬───────────┼───────────┬──────────┐
              │          │           │           │          │
         [Texto]   [Áudio .ogg]  [Imagem]    [Vídeo]  [PDF exame]
              │          │           │           │          │
              │     ffmpeg →         │           │          │
              │     WAV 16kHz        │           │          │
              │          │           │           │          │
              └──────────┴───────────┼───────────┴──────────┘
                                     ▼
                    ╔════════════════════════════════════╗
                    ║   Nemotron 3 Nano Omni (NIM)       ║
                    ║   motor ÚNICO — uma inferência:    ║
                    ║   texto · áudio · imagem · vídeo   ║
                    ║   30B-A3B MoE · contexto 256K      ║
                    ╚════════════════════════════════════╝
                                     │
                    ┌────────────────┼────────────────┐
                    │                │                │
              Zep (memória)   Google Sheets   Google Calendar
            (grafo temporal)   (histórico)    (agenda/alertas)
                    │
                    ▼
             ElevenLabs (TTS)
                    │
                    ▼
         WhatsApp (resposta em áudio)
```

### 3.1 Stack detalhado

| Camada | Tecnologia | Função |
|---|---|---|
| Mensageria | WhatsApp Business API | Canal principal (texto, áudio, imagem, PDF) |
| Gateway WA | Evolution API (self-hosted) ou Twilio | Webhook de entrada/saída |
| Orquestrador | **n8n** | Fluxos, condicionais, agendamentos (cron) |
| STT | **Nemotron Omni** (encoder Parakeet-TDT integrado) | Transcrição de áudios .ogg → texto, via ffmpeg → WAV 16 kHz |
| LLM Principal | **Nvidia NIM** — `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` | Motor único multimodal: raciocínio clínico, leitura de exames (imagem/PDF), análise de refeições, vídeo e nota de voz |
| LLM Visão | **Nemotron Omni** (encoder C-RADIOv4-H integrado) | Leitura de PDFs de exames, fotos de refeições e vídeo |
| TTS | **ElevenLabs** | Geração de áudio de resposta (voz Dr. João Holanda) |
| Memória | **Zep** (grafo temporal) ou **Mem0** | Histórico longitudinal; lembra evolução de exames e queixas |
| Banco estruturado | **Google Sheets** | Tabelas de exames, medicamentos, peso, humor |
| Agenda | **Google Calendar** | Lembretes de medicação, consultas, exames |
| Câmera | **Intelbras Mibo Smart** (RTSP) | Monitoramento de ADL e detecção de quedas |
| Processamento câmera | **Python + ffmpeg + OpenCV** | Extração de frames e inferência Vision |
| Alertas emergência | **n8n Webhook** | Disparo imediato para família em caso de queda |

---

## 4. Módulo de Câmera — Intelbras Mibo Smart

### 4.1 Configuração RTSP
```
URL padrão: rtsp://admin:<SENHA>@<IP_LOCAL>:554/cam/realmonitor?channel=1&subtype=0
```
- Câmera instalada na **cozinha** do Sr. Edilson.
- Frames extraídos a cada **5 segundos** via ffmpeg.
- **Privacidade:** frames NÃO são salvos em disco. São enviados diretamente à API Vision em memória e descartados.

### 4.2 Inferências da câmera
| Evento detectado | Ação |
|---|---|
| Queda ou imobilidade > 2 min | Alerta imediato para grupo WhatsApp dos filhos + SMS |
| Refeição preparada | Foto capturada → análise nutricional → feedback ao paciente |
| Ausência na cozinha > 4h (horário diurno) | Alerta leve de verificação para família |
| Movimento normal | Registro de atividade no Google Sheets |

---

## 5. Módulo de Relatório Familiar

- **Horário:** cron job no n8n às **20h00 (Horário de Brasília)** todos os dias.
- **Destinatário:** grupo WhatsApp dos filhos do Sr. Edilson.
- **Conteúdo do resumo diário:**
  1. Humor geral do dia (análise de tom nas mensagens)
  2. Atividade física inferida (câmera + relatos)
  3. Refeições registradas e avaliação nutricional
  4. Medicamentos confirmados
  5. Sintomas ou queixas relatadas
  6. Variações em marcadores relevantes (se houver novo dado)
  7. Recomendações para o dia seguinte

---

## 5.1 Análise de Tom de Voz (prosódia)

O encoder de áudio do Nemotron Omni (Parakeet-TDT) ouve o **sinal**, não só as palavras.
Um "estou bem" dito com voz fraca e arrastada é um dado clínico diferente de um
"estou bem" firme.

**Dimensões avaliadas em cada nota de voz:** energia, ritmo, estabilidade, volume,
articulação, respiração, emoção.

**Linha de base:** extraída dos áudios de referência do Sr. Edilson gravados quando
ele estava bem (`agents/setup_voice.py`), armazenada no Zep na categoria `voz_baseline`.
Cada nota de voz nova é comparada contra esse perfil.

**Sinais absolutos de atenção:** emoção de tristeza/dor/ansiedade/cansaço, voz trêmula,
respiração ofegante, articulação comprometida.

**Escalonamento:** um dia ruim é normal. A família só é avisada após
`VOZ_ALERTA_LIMIAR` (padrão: 3) mensagens consecutivas com sinal de sofrimento —
e a mensagem é explicitamente enquadrada como observação afetiva, não alerta médico.

O perfil vocal de cada interação é gravado no Zep como fato da categoria `humor`,
alimentando o item 1 do relatório familiar das 20h.

---

## 5.2 Filtro de Contatos Autorizados

O agente é um assistente clínico **privado**, não um chatbot aberto. Responde
exclusivamente a:

| Nome | Número | Papel |
|---|---|---|
| Edilson Cavalcante | 92 99222-2522 | paciente |
| Erick Cavalcante | 92 99288-4633 | filho |
| Edilson Junior | 92 99111-6200 | filho |
| Camila Cavalcante | 92 98108-2474 | filha |

Qualquer outro número é ignorado **em silêncio** — sem resposta, sem processamento,
sem custo de inferência. Grupos (`@g.us`) nunca são atendidos: o grupo da família
recebe alertas, mas mensagens vindas dele não geram resposta.

A normalização resolve as variações de formato do WhatsApp (com/sem código do país 55,
com/sem o nono dígito). Contatos extras podem ser adicionados via
`EXTRA_ALLOWED_NUMBERS` no `.env`, sem alterar código.

**O agente sabe com quem fala:** ao paciente, linguagem acolhedora sem jargão; aos
filhos, mais técnico e objetivo, respeitando confidências que o Sr. Edilson tenha
pedido para guardar — salvo risco à saúde dele.

---

## 6. Regras Clínicas Críticas

### 6.1 PSA — Gatilhos de Alerta
```
PSA < 0,10   → Zona segura. Reforço positivo ao paciente.
PSA 0,10–0,20 → Atenção. Informar sem alarmar. Sugerir contato com urologista.
PSA > 0,20   → ALERTA. Notificar família imediatamente. Não minimizar.
Dobramento em < 6 meses → Escalada urgente independente do valor absoluto.
```

### 6.2 eTFG — Estágios Renais
```
eTFG > 60    → Normal/Leve. Hidratação padrão 2L/dia.
eTFG 30–59   → Moderado. Restringir proteína animal > 0,8g/kg. Evitar AINEs.
eTFG 15–29   → Grave. Protocolo renal estrito. Notificar nefrologista.
eTFG < 15    → Crítico. Alerta máximo. Acionar família.
```

### 6.3 Restrições Alimentares Ativas
- Sódio moderado (hipertensão associada ao envelhecimento).
- Potássio controlado se eTFG < 45.
- Fósforo monitorado (laticínios com moderação).
- Álcool: contraindicado.
- Suplementação de Vitamina D + Cálcio: validar com médico responsável.

### 6.4 Medicamentos (referência — não alterar sem prescrição)
> Lista a ser carregada do Google Sheets na inicialização de cada sessão.  
> Variável de contexto: `patient.medications[]`

---

## 7. Variáveis de Ambiente Necessárias

```env
# WhatsApp Gateway
EVOLUTION_API_URL=
EVOLUTION_API_KEY=
WHATSAPP_INSTANCE_NAME=

# OpenAI
OPENAI_API_KEY=

# Nvidia NIM — motor único (raciocínio clínico, memória Zep, transcrição)
# API compatível com o formato OpenAI. Chave em build.nvidia.com
NVIDIA_NIM_API_KEY=
NVIDIA_NIM_BASE_URL=https://integrate.api.nvidia.com/v1
NIM_CHAT_MODEL=nvidia/nemotron-3-nano-omni-30b-a3b-reasoning
NIM_REASONING_BUDGET=4096

# Voz e filtro de contatos
VOZ_ALERTA_LIMIAR=3            # notas de voz seguidas com sinal antes de avisar família
EXTRA_ALLOWED_NUMBERS=         # "92 99999-0000:Maria:cuidadora" (opcional)
AGENT_HOST_PORT=3001           # porta do host (3000 costuma estar ocupada)

# ElevenLabs
ELEVENLABS_API_KEY=
ELEVENLABS_VOICE_ID=          # ID da voz Dr. João Holanda

# Google
GOOGLE_SERVICE_ACCOUNT_JSON=  # path para arquivo de credenciais
GOOGLE_SHEET_ID=
GOOGLE_CALENDAR_ID=

# Memória
ZEP_API_URL=
ZEP_API_KEY=
ZEP_SESSION_ID=edilson_parintins_001

# Câmera
CAMERA_RTSP_URL=
CAMERA_FRAME_INTERVAL_SEC=5

# n8n
N8N_WEBHOOK_BASE_URL=
N8N_FAMILY_GROUP_WA_ID=       # ID do grupo WhatsApp dos filhos
```

---

## 8. Estrutura de Arquivos do Projeto

```
/
├── CLAUDE.md                          ← Este arquivo (contexto persistente)
├── whatsapp_webhook_n8n.json          ← Workflow n8n: entrada WhatsApp
├── daily_report_n8n.json              ← Workflow n8n: relatório familiar (cron 20h)
├── camera/
│   ├── mibo_rtsp_monitor.py           ← Script Python câmera Mibo
│   ├── fall_detection.py              ← Módulo detecção de quedas
│   └── meal_capture.py                ← Módulo captura e análise de refeições
├── agents/
│   ├── dr_sofia_prompt.md             ← System prompt completo da Dr. João Holanda
│   ├── clinical_rules.py              ← Regras clínicas e gatilhos de alerta
│   └── nutrition_amazon.py            ← Base de dados nutricional amazônica
├── integrations/
│   ├── elevenlabs_tts.py              ← Wrapper ElevenLabs
│   ├── google_sheets.py               ← CRUD histórico médico
│   ├── google_calendar.py             ← Gestão de agenda e lembretes
│   └── zep_memory.py                  ← Interface com Zep (memória temporal)
└── .env.example                       ← Template de variáveis de ambiente
```

---

## 9. Instruções para o Claude Code (Sessões Futuras)

1. **Sempre leia este arquivo primeiro** antes de implementar qualquer módulo.
2. **Nunca hardcode credenciais** — use sempre variáveis de ambiente do `.env`.
3. **Privacidade de frames:** frames da câmera devem ser processados em memória RAM, nunca escritos em disco.
4. **Idioma:** todo código com comentários em português; variáveis e funções em inglês (convenção técnica).
5. **Tom clínico:** qualquer prompt gerado para a Dr. João Holanda deve seguir o perfil da Seção 2.
6. **Versionamento:** cada novo módulo deve ser commitado na branch `claude/multimodal-health-ai-system-dEf2q`.
7. **Testes:** incluir ao menos um teste básico de integração por módulo criado.
