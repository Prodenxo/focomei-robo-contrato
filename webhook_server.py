#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Servidor webhook 24h — recebe POST do FocoMEI após pagamento Stripe
e gera o contrato no Onety automaticamente.

Rotas:
  POST /webhook/contrato              — gera contrato Autentique (fluxo existente)
  POST /webhook/crm/preparar-proposta — cria lead CRM + move para Proposta

Uso local:
  cd "robo contrato"
  python webhook_server.py

Configure no EasyPanel (backend FocoMEI):
  ONETY_CONTRATO_WEBHOOK_URL=http://SEU-IP:8787/webhook/contrato
  ONETY_CONTRATO_WEBHOOK_SECRET=mesmo_token_do_config.env
"""

from __future__ import annotations

import json
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
ENTRADA = ROOT / "entrada"

from gerar_contrato import (
    OnetyClient,
    analisar_status_contrato,
    autenticar,
    extrair_specs,
    processar_spec,
    resolver_client_id_via_lead,
    resolver_contrato_id_por_lead,
    resolver_config,
)
from padrao_lote import carregar_padrao, expandir_lista

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("webhook-contrato")

_client_lock = threading.Lock()
_onety_client: OnetyClient | None = None
_onety_usuario: dict[str, Any] | None = None
_padrao: dict[str, Any] | None = None
_webhook_secret: str = ""


def carregar_webhook_secret(cfg: dict[str, Any]) -> str:
    import os

    from_file = ""
    env_path = ROOT / "config.env"
    if env_path.exists():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("WEBHOOK_SECRET="):
                from_file = line.split("=", 1)[1].strip().strip('"').strip("'")
                break

    return (
        os.environ.get("WEBHOOK_SECRET")
        or os.environ.get("ONETY_CONTRATO_WEBHOOK_SECRET")
        or from_file
    ).strip()


def invalidate_client() -> None:
    global _onety_client, _onety_usuario
    with _client_lock:
        _onety_client = None
        _onety_usuario = None


def _create_authenticated_client(cfg: dict[str, Any]) -> tuple[OnetyClient, dict[str, Any], dict[str, Any]]:
    padrao = carregar_padrao(ENTRADA)
    client = OnetyClient(
        cfg["api_url"],
        token=cfg.get("token") or None,
        empresa_id=cfg.get("empresa_id"),
    )

    def reauth() -> None:
        global _onety_usuario
        force_login = bool(cfg.get("email") and cfg.get("senha"))
        _onety_usuario = autenticar(client, cfg, force_login=force_login)
        log.info("Sessão Onety renovada (idle > %ss ou token inválido)", OnetyClient.SESSION_IDLE_SECONDS)

    client.set_reauth_fn(reauth)
    usuario = autenticar(client, cfg)
    return client, usuario, padrao


def get_client(force_refresh: bool = False) -> tuple[OnetyClient, dict[str, Any], dict[str, Any]]:
    global _onety_client, _onety_usuario, _padrao
    with _client_lock:
        if force_refresh:
            _onety_client = None
            _onety_usuario = None

        if _onety_client is None:
            cfg = resolver_config()
            client, usuario, padrao = _create_authenticated_client(cfg)
            _onety_client = client
            _onety_usuario = usuario
            _padrao = padrao
            log.info("Onety autenticado — empresa %s", cfg.get("empresa_id"))
        elif _onety_client._session_expired():
            cfg = resolver_config()
            force_login = bool(cfg.get("email") and cfg.get("senha"))
            _onety_usuario = autenticar(_onety_client, cfg, force_login=force_login)
            log.info("Sessão Onety expirada por idle — relogin automático")

        return _onety_client, _onety_usuario or {}, _padrao or {}


def validar_auth(header_value: str | None) -> bool:
    if not _webhook_secret:
        return True
    if not header_value:
        return False
    parts = header_value.split(" ", 1)
    token = parts[1].strip() if len(parts) == 2 and parts[0].lower() == "bearer" else header_value.strip()
    return token == _webhook_secret


def processar_payload_focomei(body: dict[str, Any]) -> dict[str, Any]:
    cfg = resolver_config()
    client, usuario, padrao = get_client()
    specs_raw = extrair_specs(body)
    if not specs_raw:
        return {"ok": False, "error": "Nenhum contrato no JSON (esperado { contratos: [...] })"}

    lead_id_body = body.get("onety_lead_id") or body.get("lead_id")
    if lead_id_body not in (None, ""):
        lid = int(lead_id_body)
        specs_raw = [
            {**s, "onety_lead_id": s.get("onety_lead_id") or s.get("lead_id") or lid}
            if isinstance(s, dict)
            else s
            for s in specs_raw
        ]

    specs = expandir_lista(specs_raw, padrao)
    resultados: list[dict[str, Any]] = []
    ok_count = 0

    for i, spec in enumerate(specs, 1):
        razao = (
            spec.get("razao_social")
            or (spec.get("cliente") or {}).get("nome")
            or f"contrato_{i}"
        )
        rotulo = str(razao).replace(" ", "_")[:60]
        log.info("Gerando contrato %s/%s: %s", i, len(specs), rotulo)
        sucesso, msg, meta = processar_spec(
            client,
            cfg,
            spec,
            rotulo=rotulo,
            dry_run=False,
            pdf_arg=None,
            usuario=usuario,
            force=False,
        )
        if sucesso:
            ok_count += 1
        item: dict[str, Any] = {"rotulo": rotulo, "ok": sucesso, "mensagem": msg}
        if isinstance(meta, dict):
            for key in ("contratoId", "signingUrl", "clientId", "leadId"):
                if meta.get(key) not in (None, ""):
                    item[key] = meta[key]
        resultados.append(item)

    return {
        "ok": ok_count == len(specs),
        "total": len(specs),
        "sucesso": ok_count,
        "falhas": len(specs) - ok_count,
        "resultados": resultados,
    }


def processar_crm_preparar_proposta(body: dict[str, Any]) -> dict[str, Any]:
    cfg = resolver_config()
    client, _, _ = get_client()

    nome = str(body.get("nome") or "").strip()
    email = str(body.get("email") or "").strip()
    funil_id = body.get("funil_id")
    fase_lead = body.get("funil_fase_id_lead") or body.get("funil_fase_id")
    fase_proposta = body.get("funil_fase_id_proposta")

    if not nome:
        return {"ok": False, "error": "nome é obrigatório"}
    if not email:
        return {"ok": False, "error": "email é obrigatório"}
    if funil_id in (None, ""):
        return {"ok": False, "error": "funil_id é obrigatório"}
    if fase_lead in (None, ""):
        return {"ok": False, "error": "funil_fase_id_lead é obrigatório"}
    if fase_proposta in (None, ""):
        return {"ok": False, "error": "funil_fase_id_proposta é obrigatório"}

    empresa_id = body.get("empresa_id") or cfg.get("empresa_id")
    usuario_id = body.get("usuario_id")
    telefone = str(body.get("telefone") or "0000000000").strip()
    valor = body.get("valor")

    lead_payload: dict[str, Any] = {
        "nome": nome,
        "telefone": telefone,
        "email": email,
        "data_prevista": body.get("data_prevista"),
        "funil_id": int(funil_id),
        "funil_fase_id": int(fase_lead),
        "usuario_id": int(usuario_id) if usuario_id not in (None, "") else None,
        "pre_venda_id": body.get("pre_venda_id"),
        "empresa_id": int(empresa_id),
        "valor": valor,
        "status": str(body.get("status") or "aberto"),
    }
    lead_payload = {k: v for k, v in lead_payload.items() if v is not None}

    log.info("CRM: criando lead funil=%s fase=%s nome=%s", funil_id, fase_lead, nome[:40])
    criado = client.criar_lead(lead_payload)
    lead_id = criado.get("leadId") or criado.get("id")
    if not lead_id:
        return {"ok": False, "error": f"API não retornou leadId: {criado}"}

    log.info("CRM: movendo lead %s → Proposta (fase %s)", lead_id, fase_proposta)
    client.mover_lead_fase(lead_id, int(fase_proposta))

    pre_cliente_id = None
    convert_warn = ""
    try:
        log.info("CRM: convertendo lead %s em pré-cliente", lead_id)
        pre_cliente_id = resolver_client_id_via_lead(
            client,
            lead_id,
            spec={"cliente": {"nome": nome, "email": email, "telefone": telefone}},
            empresa_id=empresa_id,
        )
    except Exception as exc:
        convert_warn = str(exc)
        log.warning("CRM: convert lead %s falhou (contrato tentará de novo): %s", lead_id, exc)

    return {
        "ok": True,
        "leadId": int(lead_id),
        "preClienteId": pre_cliente_id,
        "convertWarning": convert_warn or None,
        "fase_proposta_id": int(fase_proposta),
        "funil_id": int(funil_id),
        "message": "Lead criado e movido para Proposta",
    }


def processar_contrato_status(body: dict[str, Any]) -> dict[str, Any]:
    contract_id = body.get("contratoId") or body.get("contract_id") or body.get("contrato_id")
    if contract_id in (None, ""):
        return {"ok": False, "error": "contratoId é obrigatório"}
    client, _, _ = get_client()
    try:
        status = analisar_status_contrato(client, int(contract_id))
        return {"ok": True, **status}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def processar_contrato_link(body: dict[str, Any]) -> dict[str, Any]:
    contract_id = body.get("contratoId") or body.get("contract_id") or body.get("contrato_id")
    lead_id = body.get("leadId") or body.get("lead_id")
    client, _, _ = get_client()
    try:
        if contract_id in (None, ""):
            if lead_id in (None, ""):
                return {"ok": False, "error": "contratoId ou leadId é obrigatório"}
            contract_id = resolver_contrato_id_por_lead(client, lead_id)
            if contract_id is None:
                return {"ok": False, "error": f"nenhum contrato encontrado para lead {lead_id}"}
        status = analisar_status_contrato(client, int(contract_id))
        return {"ok": True, **status}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


class WebhookHandler(BaseHTTPRequestHandler):
    server_version = "RoboContratoWebhook/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/health", "/healthz"):
            self._send_json(HTTPStatus.OK, {"ok": True, "service": "robo-contrato-webhook"})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

    def _read_json_body(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "body_vazio"})
            return None

        try:
            raw = self.rfile.read(length)
            body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": f"json_invalido: {exc}"},
            )
            return None

        if not isinstance(body, dict):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "esperado objeto JSON"},
            )
            return None

        return body

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"

        if path not in (
            "/webhook/contrato",
            "/webhook/crm/preparar-proposta",
            "/webhook/contrato-status",
            "/webhook/contrato-link",
        ):
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return

        if not validar_auth(self.headers.get("Authorization")):
            self._send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
            return

        body = self._read_json_body()
        if body is None:
            return

        handler_fn = (
            processar_crm_preparar_proposta
            if path == "/webhook/crm/preparar-proposta"
            else processar_contrato_link
            if path == "/webhook/contrato-link"
            else processar_contrato_status
            if path == "/webhook/contrato-status"
            else processar_payload_focomei
        )

        try:
            resultado = handler_fn(body)
            status = HTTPStatus.OK if resultado.get("ok") else HTTPStatus.UNPROCESSABLE_ENTITY
            self._send_json(status, resultado)
        except RuntimeError as exc:
            if "Token inválido" in str(exc) or "token" in str(exc).lower():
                log.warning("Token Onety inválido — tentando relogin e reprocessando")
                invalidate_client()
                try:
                    resultado = handler_fn(body)
                    status = HTTPStatus.OK if resultado.get("ok") else HTTPStatus.UNPROCESSABLE_ENTITY
                    self._send_json(status, resultado)
                    return
                except Exception as retry_exc:
                    log.exception("Erro ao reprocessar webhook após relogin")
                    self._send_json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"ok": False, "error": str(retry_exc)},
                    )
                    return
            log.exception("Erro ao processar webhook")
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": str(exc)},
            )
        except Exception as exc:
            log.exception("Erro ao processar webhook")
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": str(exc)},
            )


def main() -> int:
    global _webhook_secret
    cfg = resolver_config()
    _webhook_secret = carregar_webhook_secret(cfg)

    import os

    host = (os.environ.get("WEBHOOK_HOST") or "0.0.0.0").strip()
    port = int(os.environ.get("WEBHOOK_PORT") or "8787")

    log.info("Pré-autenticando no Onety...")
    try:
        get_client()
    except Exception as exc:
        log.error("Falha ao autenticar no Onety: %s", exc)
        return 1

    httpd = ThreadingHTTPServer((host, port), WebhookHandler)
    log.info("Webhook ouvindo em http://%s:%s/webhook/contrato", host, port)
    log.info("CRM webhook: http://%s:%s/webhook/crm/preparar-proposta", host, port)
    log.info("Status contrato: http://%s:%s/webhook/contrato-status", host, port)
    if _webhook_secret:
        log.info("Autenticação Bearer ativa (WEBHOOK_SECRET)")
    else:
        log.warning("WEBHOOK_SECRET não definido — endpoint aberto (use só em rede interna)")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("Encerrado pelo usuário")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
