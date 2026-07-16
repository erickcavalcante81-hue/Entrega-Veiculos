# Funcionalidades do Sistema — GAE / Braga Veículos

Documento de referência das **funcionalidades desenvolvidas** no app de Gestão
de Preparação e Entrega de veículos zero km (Chevrolet · GAE / Automania Group).

- **App (produção):** https://entrega-braga.netlify.app
- **Plataforma:** PWA (instalável no celular), *offline-first*, dark mode.
- **Base:** Fluxograma **v5.1** · sincronização em tempo real (Firebase).

---

## 1. Acesso e perfis (9 — por cargo, sem nomes pessoais)

| Perfil | Foco |
|--------|------|
| 📊 Gerente / Dashboard | Visão geral, métricas e alertas |
| 🗼 Torre de Controle | Monitora todo o fluxo, comunicação e atribuição |
| 📋 Programador | Entrada das planilhas, pipeline e agendamentos |
| 👨‍💼 Co-Programador | Recebe pedidos, cadastra veículos, atualiza listas |
| 🔧 Colaborador de Pátio | Busca o veículo, foto do VIN SERIAL, preparação |
| 🛠️ Instalação de Acessórios | Instala e confirma acessórios e películas |
| ✨ Lavador / Tapete | Lavagem, acabamento e tapete (inclui Via Rápida) |
| 🏁 Equipe Técnica | Conferência final e placa |
| 🚗 Entregador Técnico | Entrega ao cliente, fotos finais e Planilha 4 |

Cada perfil enxerga todas as telas, mas foca nas suas tarefas. O botão **＋**
(cadastrar veículo) aparece para Gerente, Torre, Programador e Co-Programador.

---

## 2. Pipeline operacional (5 etapas)

| # | Etapa | Responsável | Destaque |
|---|-------|-------------|----------|
| 0 | 🔧 Preparação no Estoque | Colaborador de Pátio | Foto do **VIN SERIAL** conferida (OCR × grade) libera a etapa; acessórios; lavagem + tapete |
| 1 | 📄 Pré-Entrega | Programador | Validação, liberação financeira, docs e **agendamento** (48 h antes) |
| 2 | 🏁 Conferência Final | Equipe Técnica | Confere tudo; placa instalada; foto da placa |
| 3 | 🚗 Entrega ao Cliente | Entregador Técnico | Conferência com o cliente; fotos finais; Planilha 4 |
| 4 | ✅ Entregue | — | Encerrado no app |

**Fluxos transversais:**
- ⚡ **Via Rápida (Frota/Locadora):** veículos sem acessório — só lavagem +
  tapete, com prioridade (botão no detalhe + selo na lista).
- 🔁 **Reagendamento:** cliente não compareceu → volta para a Pré-Entrega com
  nova data (motivo no histórico + selo).
- 🔒 **Portão do chassi:** a etapa 0 só é liberada quando a foto do VIN SERIAL
  **confere** com a grade (com liberação manual registrada como exceção).

Avançar/voltar etapa mostra a linha do tempo; ao concluir, o card fica
**"🎉 Entregue!"**.

---

## 3. Entrada de dados

### 3.1 Importação das 4 planilhas de programação (lote)
Aba **Entrada → Planilhas de programação**. Escolhe-se **qual** planilha e
**sobe-se o PDF** (extração automática via PDF.js) **ou** cola-se o texto. A
aplicação é **automática**, deduplicada pelo **chassi**:

| Planilha | Regra |
|----------|-------|
| 1 · Preparação | Cadastra novos na etapa Preparação; atualiza existentes |
| 2 · Entrega (agendar) | Define data/horário/entregador → leva à Pré-Entrega |
| 3 · Reagendamento | Marca 🔁 Reagendado → volta à Pré-Entrega |
| 4 · Entregues | Marca como ✅ Entregue |

- **Novos** → cadastrados direto. **Existentes com mudança** → sobrescritos pela
  última versão + tag **🔔 Alterado** (some ao abrir). **Idênticos** → ignorados
  (não duplica). Ao final, um **resumo** (tipo · novos · atualizados · sem mudança).
- Normalização de modelos, cores e acessórios (corrige erros de digitação).

### 3.2 Pedido individual (WhatsApp / e-mail / PDF / imagem)
Parser que extrai modelo/cor/cliente/acessórios. **WhatsApp** exige
**remetente autorizado** (Adriana · Adriano · Junior Leão · Adriano Junior).

---

## 4. Preparação, checklists e qualidade

- ✅ **Checklists por etapa** — o responsável marca item a item, com barra de
  progresso; itens com 📸 lembram das fotos obrigatórias.
- 🛠️ **Acessórios** — instalação confirmada item a item (grava data); dá para
  adicionar acessórios que faltaram na lista.
- ⚠️ **Defeitos / avarias** — registro por tipo, local, gravidade e descrição,
  com acompanhamento até a resolução.

---

## 5. Fotos da vistoria + verificação do VIN SERIAL (IA/OCR)

- 📷 **Captura pela câmera** do celular (VIN SERIAL, avaria, placa, outras),
  com **compressão** no aparelho; imagens em coleção separada (fora do limite
  de 1 MB do documento) + cache local.
- 🔢 **OCR do VIN SERIAL** cruzado com a grade — aprova pelo **VIN SERIAL de 6
  dígitos** (adesivo GM) **ou** pelo **chassi cheio (17)**, tolerando erros de
  leitura. É o que **libera a etapa 0**.
- ☁️ **OCR cloud-first:** usa a função serverless (Google Vision **ou** Claude,
  chave no servidor) e **cai para o OCR no aparelho** (Tesseract.js) se a nuvem
  não estiver configurada/estiver offline. O selo mostra a fonte (**nuvem** /
  **aparelho**).

---

## 6. Torre de Controle · comunicação · atribuição

- 🗼 **Torre de Controle (Dashboard):** painel de **pendências** — chassi/VIN
  SERIAL não conferido, comunicação pendente, sem responsável, Pré-Entrega sem
  data e atrasados — cada uma com contagem e atalho para o veículo.
- 📲 **Comunicação obrigatória por etapa:** ao concluir uma etapa, o app monta
  a mensagem (veículo, chassi, etapa, responsável, entrega) e
  **compartilha/copia para o WhatsApp** (Equipe · Vendedor · Gerência · Torre),
  registrando no histórico.
- 👤 **Atribuição de responsável** por veículo na etapa atual (reatribuído a
  cada avanço), visível no card e na Torre.

---

## 7. Dashboard e acompanhamento

- 📊 **KPIs:** em preparação, no prazo, atenção, atrasados.
- 🚦 **Semáforo** por tempo (verde/amarelo/vermelho) por veículo.
- 📅 **Entregas do dia** e **pipeline por etapa** (contagem + expandível).
- 🔎 **Lista de veículos** com busca (modelo/cliente/placa/chassi) e filtro por
  etapa; barra de progresso por veículo.

---

## 8. Sincronização e dados

- ☁️ **Sync por veículo:** cada veículo é um documento (`braga_veiculos/{id}`)
  + um doc de apoio (`braga/meta`) para defeitos/entradas/contadores. Escrita
  **por diff** — editar veículos diferentes em aparelhos diferentes **não gera
  sobrescrita** (dentro do mesmo veículo, vale a última escrita).
- 📴 **Offline-first:** trabalha sobre o `localStorage`; sincroniza ao
  reconectar. Indicador de status: 🟢 Sincronizado · 🟡 Conectando · 🔴 Sem
  conexão · ⚪ Local.
- 🔄 **Migração automática** do modelo antigo (documento único) e **fallback**
  seguro quando as regras novas ainda não estão publicadas.
- 🔑 Chave: **chassi** (deduplicação). Migração de esquema v4 → v5.1 automática.

---

## 9. PWA e publicação

- 📱 **Instalável** (Adicionar à Tela de Início / Instalar app), abre em tela
  cheia e funciona **offline**.
- ⚙️ **Service worker** (network-first) com atualização automática.
- 🚀 **Deploy** no Netlify a partir do GitHub (publica a `main`).
- 🧭 **Canonicalização de URL:** o app instalado aberto por engano numa URL de
  *preview* é redirecionado para a produção.

---

## 10. Identidade visual (GAE / Automania Group)

- 🖼️ Logo GAE no login e no cabeçalho.
- 🎨 Paleta **lime/verde** (`--primary #9FD84F`, verde `#35D399`) + tema escuro.
- 🔤 Fontes **Archivo Narrow** (títulos) + **Overpass** (corpo).
- Ícones de perfil em SVG e rótulos limpos.

---

## Histórico de versões (Pull Requests)

| PR | Entrega |
|----|---------|
| #3 | Migração do escopo para o **Fluxograma v5.1** (Fase 1) |
| #4 | **Fase 2** — 4 planilhas, Torre de Controle, comunicação, atribuição |
| #4 | **Sync por veículo** + **OCR do VIN SERIAL na nuvem** (com fallback) |
| #5 · #6 | Melhoria e depois remoção da tela de "Configurar sincronização" |
| #7 | Redirect preview→produção + limpeza de workflow |
| #8 | **Identidade visual GAE** (v1 + v2: ícones SVG, rótulos limpos) |

---

## Documentos relacionados

- [`MANUAL_DE_USO.md`](MANUAL_DE_USO.md) — manual por perfil (equipe).
- [`SETUP_NUVEM.md`](SETUP_NUVEM.md) — pôr online + regras do Firestore + OCR na nuvem.
- [`RESUMO_PROJETO_BRAGA_VEICULOS.md`](RESUMO_PROJETO_BRAGA_VEICULOS.md) — resumo do projeto.
- [`ANALISE_PEDIDOS_E_PLANILHAS.md`](ANALISE_PEDIDOS_E_PLANILHAS.md) — padrão das planilhas.

---

*GAE / Braga Veículos · Chevrolet · Grupo Econômico Malia · 2026*
