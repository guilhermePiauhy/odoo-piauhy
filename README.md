# Estrutura de Agente Claude Code para Análise Jurídica com Monitoramento n8n

## Visão Geral

Este documento descreve a arquitetura completa para executar o pipeline de análise jurídica (classificação de sentenças e acórdãos) como um **agente autônomo no Claude Code**, com orquestração e monitoramento via **n8n**.

---

## Índice

1. [Arquitetura Geral](#arquitetura-geral)
2. [Estrutura de Diretórios](#estrutura-de-diretórios)
3. [Componentes Principais](#componentes-principais)
4. [Fluxo de Execução](#fluxo-de-execução)
5. [Configuração do Agente no Claude Code](#configuração-do-agente-no-claude-code)
6. [Integração com n8n](#integração-com-n8n)
7. [Variáveis de Ambiente](#variáveis-de-ambiente)
8. [API de Status e Webhooks](#api-de-status-e-webhooks)
9. [Execução Local](#execução-local)
10. [Docker e Deploy](#docker-e-deploy)
11. [Monitoramento e Logs](#monitoramento-e-logs)
12. [Tratamento de Erros](#tratamento-de-erros)
13. [Exemplos de Payload n8n](#exemplos-de-payload-n8n)

---

## Arquitetura Geral

```
┌─────────────────────────────────────────────────────────────────┐
│                          n8n Workflow                           │
│                                                                 │
│  [Trigger] → [HTTP Request] → [Monitor Loop] → [Notification]  │
└────────────────────────┬────────────────────────────────────────┘
                         │ HTTP POST /run
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│                     Agent API Server                            │
│                    (FastAPI / Port 8080)                        │
│                                                                 │
│  POST /run        → Inicia execução do agente                  │
│  GET  /status/:id → Retorna status da execução                 │
│  GET  /results/:id→ Retorna resultados parciais/finais         │
│  POST /stop/:id   → Interrompe execução                        │
│  GET  /health     → Healthcheck                                │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Claude Code Agent                            │
│                                                                 │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────────────┐ │
│  │   Agente 1  │ →  │   Agente 2  │ →  │  Consolidador Final │ │
│  │  (Análise   │    │(Classificad │    │  (CSV + Odoo Write) │ │
│  │  Decisória) │    │    or)      │    │                     │ │
│  └─────────────┘    └─────────────┘    └─────────────────────┘ │
│           │                │                      │             │
│           └────────────────┴──────────────────────┘             │
│                            │                                    │
│                     ┌──────▼──────┐                            │
│                     │ Progress    │                            │
│                     │  Queue      │                            │
│                     └──────┬──────┘                            │
└────────────────────────────┼────────────────────────────────────┘
                             │ Webhook callbacks
                             ▼
                    n8n Webhook Listener
```

---

## Estrutura de Diretórios

```
odoo-piauhy/
├── README.md                          # Este arquivo
├── docker-compose.yml                 # Orquestração de containers
├── Dockerfile                         # Imagem do agente
├── .env.example                       # Variáveis de ambiente de exemplo
│
├── agent/
│   ├── __init__.py
│   ├── main.py                        # Ponto de entrada do agente
│   ├── api_server.py                  # FastAPI - interface com n8n
│   ├── agent_runner.py                # Orquestrador do pipeline
│   ├── progress_tracker.py            # Rastreamento de progresso
│   └── webhook_notifier.py            # Envio de callbacks para n8n
│
├── pipeline/
│   ├── __init__.py
│   ├── agente_1.py                    # Análise decisória (Agente 1)
│   ├── agente_2.py                    # Classificador jurídico (Agente 2)
│   ├── consolidador.py                # Consolidação e escrita final
│   ├── document_fetcher.py            # Busca documentos no Odoo
│   └── csv_writer.py                  # Escrita de resultados em CSV
│
├── libs/
│   ├── ai/
│   │   └── ai.py                      # Cliente IA (Claude/OpenAI)
│   ├── odoo/
│   │   └── odoofuncoes.py             # Funções Odoo
│   └── healthcheck/
│       └── healthcheck.py             # Servidor de healthcheck
│
├── templates/
│   ├── agente_1_template.txt          # Prompt do Agente 1
│   └── agente_2_template.txt          # Prompt do Agente 2
│
├── n8n/
│   ├── workflows/
│   │   ├── monitor_agent.json         # Workflow de monitoramento
│   │   └── trigger_and_report.json    # Workflow de disparo e relatório
│   └── README.md                      # Instruções de importação
│
├── scripts/
│   ├── run_local.sh                   # Execução local sem Docker
│   └── healthcheck.sh                 # Script de healthcheck
│
└── tests/
    ├── test_agente_1.py
    ├── test_agente_2.py
    └── test_api.py
```

---

## Componentes Principais

### 1. API Server (`agent/api_server.py`)

Servidor FastAPI que expõe endpoints REST para o n8n interagir com o agente.

**Responsabilidades:**
- Receber comandos de início de execução
- Retornar status em tempo real
- Expor resultados parciais e finais
- Aceitar comandos de parada
- Disparar webhooks de progresso

### 2. Agent Runner (`agent/agent_runner.py`)

Orquestrador do pipeline de análise. Gerencia:
- Busca de dossiês no Odoo
- Distribuição de trabalho entre workers
- Controle de concorrência (ThreadPoolExecutor)
- Persistência de estado da execução

### 3. Progress Tracker (`agent/progress_tracker.py`)

Mantém estado em memória (e opcionalmente Redis) com:
- Total de itens a processar
- Itens processados com sucesso
- Itens com erro
- Percentual de conclusão
- Logs estruturados de cada item

### 4. Webhook Notifier (`agent/webhook_notifier.py`)

Envia notificações assíncronas para o n8n em eventos:
- `execution_started` - Execução iniciada
- `progress_update` - A cada N itens processados
- `item_completed` - Item individual concluído
- `item_error` - Erro em item individual
- `execution_completed` - Execução finalizada
- `execution_