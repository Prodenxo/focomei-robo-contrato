# Onety CRM + Contrato — integração FocoMEI

Documentação operacional da API Onety usada pelo fluxo **funil → lead → Proposta → contrato**.

Este arquivo vive no repo **[focomei-robo-contrato](https://github.com/Prodenxo/focomei-robo-contrato)** — fonte da verdade para deploy, webhooks e API Onety.

## Visão geral

```
[FocoMEI admin / app]
  1. Seleciona funil comercial (admin) ou funil fixo self-serve (598)
  2. Backend FocoMEI chama robô CRM (criar lead + mover Proposta)
  3. Backend FocoMEI chama robô contrato (geração Autentique)

[Onety — automático]
  ~3s após Proposta → abre tela de geração de contrato
  Após assinatura → fase Ganhou (Onety)
```

O FocoMEI **não** duplica a tela de contrato do Onety; prepara o card no CRM antes da geração via este serviço.

## Base e autenticação

| Item | Valor |
|------|--------|
| Base API | `https://back.cfonety.com.br` |
| Auth | `Authorization: Bearer <JWT>` |
| Header empresa | `x-empresa-id: 785` |
| `empresa_id` (payload) | `785` |

Credenciais: `.env` deste repo (`EMAIL`, `SENHA`, `EMPRESA_ID`) — ver `.env.example`.

## Mapa de funis (`funil_id`)

| Funil | ID | Fase Lead | Fase Proposta | Status |
|-------|-----|-----------|---------------|--------|
| **BNI** | **597** | **2874** | **2871** | Pronto |
| **Tráfego Pago** | **598** | **2880** | **2877** | Pronto |
| **Franqueado Cf** | **583** | **2827** | **2798** | Pronto |
| **Workshop - Método Mei Lucrativo** | **716** | **3482** | **3479** | Pronto |
| **Funil De Captação - Evento Dna Contábil** | **807** | **3951** | **3948** | Pronto |

**Não liberados para contrato no FocoMEI** (sem fases CRM mapeadas):

| Funil | ID |
|-------|-----|
| Funil De Aquisição Whatsapp | 794 |
| Contrio Mangaratiba | 682 |

**Dois IDs por funil:** Lead vem do `POST /comercial/leads`; Proposta vem do `PUT .../mover-fase` (arrastar card no kanban).

Fases Lead/Proposta variam por funil. O backend FocoMEI envia os IDs no payload do webhook CRM; a lista de funis disponíveis na UI fica em `Prodenxo/focomei` → `backend/src/config/onety-crm-funis.js`.

## Endpoints CRM (API Onety)

### Criar lead

```http
POST /comercial/leads
Content-Type: application/json
Authorization: Bearer <token>
x-empresa-id: 785
```

**Payload (exemplo BNI):**

```json
{
  "nome": "Leo teste",
  "telefone": "2111111111",
  "email": "cliente@exemplo.com",
  "data_prevista": null,
  "funil_id": 597,
  "funil_fase_id": 2874,
  "usuario_id": 1083,
  "pre_venda_id": null,
  "empresa_id": 785,
  "valor": 100,
  "status": "aberto"
}
```

**Response `201`:**

```json
{
  "message": "Lead criado com sucesso.",
  "leadId": 74598
}
```

### Mover para Proposta

```http
PUT /comercial/leads/{leadId}/mover-fase
```

**Payload:**

```json
{
  "funil_fase_id": 2871
}
```

**Response:** `200 OK`

Após o `mover-fase`, o Onety abre a tela de contrato (~3s). A geração Autentique continua sendo feita pelo webhook `/webhook/contrato`.

### Converter lead em pré-cliente (vínculo CRM ↔ contrato)

O Onety **não** grava `contract_id + lead_id` juntos. O elo é:

```
leads.id → pre_clientes.lead_id → contratos.pre_cliente_id
```

Antes do `POST /contratual/contratos-autentique/html`, o pré-cliente usado como `client_id` **precisa** ter `lead_id`.

```http
POST /comercial/leads/convert/{leadId}
```

Alternativa: `POST /comercial/pre-clientes` com `lead_id` no body.

O robô chama `convert` automaticamente quando recebe `onety_lead_id` no payload do contrato (ou após o webhook CRM).

**Consulta:**

- `GET /comercial/pre-clientes/{clientId}/contracts`
- `GET /comercial/leads/{leadId}` (JOIN com `pre_cliente_id`)

### Endpoints auxiliares (UI Onety — robô não precisa)

- `GET /comercial/leads/{leadId}` — refresh do card
- `GET /comercial/funil-fases/{funil_id}/metas` — KPIs do funil

## Webhooks deste serviço (`webhook_server.py`)

| Rota | Uso |
|------|-----|
| `POST /webhook/contrato` | Gera contrato Autentique |
| `POST /webhook/crm/preparar-proposta` | Cria lead + move para Proposta |
| `POST /webhook/contrato-status` | Status de assinatura por `contratoId` |
| `POST /webhook/contrato-link` | Link de assinatura por `contratoId` ou `leadId` |
| `GET /health` | Health check |

Auth: `Authorization: Bearer <WEBHOOK_SECRET>` (mesmo segredo em todas as rotas).

**Payload CRM (`/webhook/crm/preparar-proposta`):**

```json
{
  "nome": "Razão Social Ltda",
  "telefone": "21999998888",
  "email": "admin@empresa.com",
  "funil_id": 597,
  "funil_fase_id_lead": 2874,
  "funil_fase_id_proposta": 2871,
  "usuario_id": 1083,
  "empresa_id": 785,
  "valor": 149.9
}
```

**Response:**

```json
{
  "ok": true,
  "leadId": 74598,
  "preClienteId": 2595,
  "fase_proposta_id": 2871
}
```

`preClienteId` vem do `POST /comercial/leads/convert/{leadId}` (best-effort; o contrato repete convert se necessário).

## Payload contrato com CRM

Quando o admin escolhe funil, o backend FocoMEI envia `onety_lead_id` junto ao webhook de contrato:

```json
{
  "onety_lead_id": 74598,
  "contratos": [
    {
      "razao_social": "...",
      "onety_lead_id": 74598
    }
  ]
}
```

O robô converte o lead → usa `client_id` do pré-cliente → gera HTML. A proposta aparece no card do lead no CRM.

## Variáveis de ambiente (backend FocoMEI — repo separado)

```env
# Contrato
ONETY_CONTRATO_WEBHOOK_URL=http://robo-contrato:8787/webhook/contrato
ONETY_CONTRATO_WEBHOOK_SECRET=mesmo_WEBHOOK_SECRET_do_robo

# CRM (opcional — se vazio, deriva de ONETY_CONTRATO_WEBHOOK_URL)
# ONETY_CRM_WEBHOOK_URL=http://robo-contrato:8787/webhook/crm/preparar-proposta

# Vendedor padrão ao criar lead (usuario_id Onety)
ONETY_CRM_DEFAULT_VENDEDOR_ID=1083

# Cadastro self-serve: funil fixo (598 = Tráfego Pago)
ONETY_CRM_SELF_SERVE_FUNIL_ID=598
```

## Cadastro self-serve (contract_first)

Todo cadastro pelo app cai no funil **Tráfego Pago (598)** — sem pergunta de origem na UI.

| Config | Valor |
|--------|--------|
| Funil | `598` Tráfego Pago |
| Fase Lead | `2880` |
| Fase Proposta | `2877` |
| Override env | `ONETY_CRM_SELF_SERVE_FUNIL_ID=598` |

Fluxo: `POST /billing/mei/confirm-plan` → CRM + contrato → `/aguardando-contrato`.

## Fluxo admin FocoMEI

1. `GET /api/admin/billing/onety-crm/funis` — lista funis disponíveis (`ready: true` quando fases configuradas)
2. `POST /api/admin/billing/stripe/emit-contrato` com `{ empresaId, funilId?, vendedorId?, valor? }`
3. Se `funilId` informado: CRM primeiro, depois contrato
4. Se `funilId` omitido: só contrato (comportamento anterior)

## Descobrir fases de um funil novo

1. Abrir funil no Onety (`?funil=<id>`)
2. DevTools → Network → filtro `comercial`
3. Criar lead manual → anotar `funil_fase_id` da coluna Lead
4. Arrastar para Proposta → anotar `funil_fase_id` do `mover-fase`
5. Atualizar `onety-crm-funis.js` no repo **focomei** (backend)

## Segurança

- Não commitar JWT, senha ou `WEBHOOK_SECRET` em repositório
- Renovar sessão Onety se token vazar
- Webhook só em rede interna ou com Bearer obrigatório
