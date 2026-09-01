# Robô Contrato FocoMEI → Onety

Serviço Python que recebe POST do backend FocoMEI e gera contrato no Onety/Autentique. Também prepara leads no CRM Onety (funil → Proposta).

Repositório **independente** do monorepo `Prodenxo/focomei` — deploy no EasyPanel a partir da raiz deste repo.

**Documentação completa:** [`docs/onety-crm-contrato-api.md`](docs/onety-crm-contrato-api.md) (API Onety, funis, payloads, env).

## Deploy no EasyPanel

1. **Create Service → App**
2. Fonte: **GitHub** → `Prodenxo/focomei-robo-contrato`
3. **Root directory:** `/` (raiz do repo)
4. **Dockerfile:** `Dockerfile`
5. **Porta:** `8787`
6. Nome sugerido: `robo-contrato`

### Variáveis de ambiente (app robo-contrato)

| Variável | Valor |
|---|---|
| `API_URL` | `https://back.cfonety.com.br` |
| `EMAIL` | login Onety |
| `SENHA` | senha Onety |
| `EMPRESA_ID` | `785` |
| `WEBHOOK_SECRET` | token forte (anote) |
| `WEBHOOK_HOST` | `0.0.0.0` |
| `WEBHOOK_PORT` | `8787` |
| `ONETY_AUTO_ENVIAR_WHATSAPP` | `true` |
| `ONETY_WHATSAPP_INSTANCIA_ID` | ID Z-API (opcional) |
| `ONETY_WHATSAPP_INSTANCIA_NOME` | `Comercial Foco MEI` |

Copie de `.env.example` — **não commite senhas**.

### Backend FocoMEI (monorepo separado)

No app backend no EasyPanel:

| Variável | Valor |
|---|---|
| `ONETY_CONTRATO_WEBHOOK_URL` | `http://robo-contrato:8787/webhook/contrato` |
| `ONETY_CONTRATO_WEBHOOK_SECRET` | **mesmo** `WEBHOOK_SECRET` do robô |
| `ONETY_CRM_WEBHOOK_URL` | (opcional) `http://robo-contrato:8787/webhook/crm/preparar-proposta` |

`robo-contrato` = nome do serviço no EasyPanel (rede interna Docker). Se renomear o serviço, ajuste a URL.

Redeploy **nos dois** apps após mudar env.

## Rotas HTTP

| Método | Rota | Descrição |
|--------|------|-----------|
| GET | `/health` | Health check |
| POST | `/webhook/contrato` | Gera contrato Autentique |
| POST | `/webhook/crm/preparar-proposta` | Cria lead + move para Proposta |
| POST | `/webhook/contrato-status` | Status de assinatura |
| POST | `/webhook/contrato-link` | Link de assinatura (por contrato ou lead) |

Todas as rotas POST exigem `Authorization: Bearer <WEBHOOK_SECRET>` quando `WEBHOOK_SECRET` está definido.

## Testar

```bash
curl https://SEU-ROBO-CONTRATO.easypanel.host/health
```

```bash
curl -X POST http://robo-contrato:8787/webhook/contrato \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer SEU_WEBHOOK_SECRET" \
  -d '{"contratos":[{"tipo_cliente":"empresa","razao_social":"Teste Ltda","cpf_cnpj":"02899404000194","email":"teste@email.com","telefone":"5511999999999","endereco":"Rua A","numero":"1","bairro":"Centro","cidade":"São Paulo","estado":"SP","cep":"01000000","signatario_nome":"João Teste","signatario_cpf":"12345678901","signatario_email":"teste@email.com","signatario_telefone":"5511999999999","quantidade_licencas":"5","valor_mensal":100}]}'
```

## Fluxo

```
Cliente paga / confirma plano → Backend FocoMEI → POST /webhook/crm/preparar-proposta (opcional)
                                              → POST /webhook/contrato → Onety/Autentique
App aguardando contrato → POST /webhook/contrato-status ou /webhook/contrato-link
```

## Modo manual (local)

```bash
pip install -r requirements.txt
cp .env.example config.env   # edite credenciais
python gerar_contrato.py --arquivo entrada/exemplo_contrato.json
python webhook_server.py
```

## Arquivos

| Arquivo | Função |
|---|---|
| `webhook_server.py` | Servidor 24h (Docker/EasyPanel) |
| `gerar_contrato.py` | Geração no Onety |
| `padrao_lote.py` | Expande JSON mínimo do FocoMEI |
| `entrada/_padrao_focomei.json` | Template (não apagar) |
| `docs/onety-crm-contrato-api.md` | API Onety + funis + integração FocoMEI |

Saídas: pasta `saida/` (não versionada).
