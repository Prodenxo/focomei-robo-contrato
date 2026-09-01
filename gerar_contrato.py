#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Robô local — gera contrato no módulo Contratual do Onety (Autentique).

Fluxo espelhado da tela /contratual/criar-contrato-autentique:
  1) Login (+ login-empresa)
  2) Monta variáveis do template {{ ... }}
  3) POST /contratual/contratos-autentique/html  (ou /contratos-autentique com PDF)
  4) Salva o resultado em saida/

Uso rápido:
  cd "robo contrato"
  copy config.example.env config.env   # preencha EMAIL, SENHA, EMPRESA_ID
  copy entrada\\exemplo_contrato.json entrada\\contrato.json
  pip install -r requirements.txt
  python gerar_contrato.py --arquivo entrada/contrato.json

Vários contratos:
  - Um JSON por cliente em entrada/ (cliente_a.json, cliente_b.json...)
    python gerar_contrato.py --todos
  - Ou um único lote JSON:
    python gerar_contrato.py --arquivo entrada/lote.json
  - Ou planilha Excel (recomendado):
    python gerar_contrato.py --gerar-modelo-excel
    python gerar_contrato.py --excel entrada/lote.xlsx

Outros:
  python gerar_contrato.py --listar-modelos
  python gerar_contrato.py --listar-clientes
  python gerar_contrato.py --arquivo entrada/contrato.json --dry-run
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

try:
    import requests
except ImportError:
    print("Instale as dependências: pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)

ROOT = Path(__file__).resolve().parent
ENTRADA = ROOT / "entrada"
SAIDA = ROOT / "saida"


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

def carregar_env(path: Path) -> dict[str, str]:
    """Lê arquivo KEY=VALUE simples (sem depender de python-dotenv)."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def resolver_config() -> dict[str, Any]:
    arquivo = ROOT / "config.env"
    if not arquivo.exists():
        arquivo = ROOT / ".env"
    file_env = carregar_env(arquivo)

    def get(key: str, default: str = "") -> str:
        return (os.environ.get(key) or file_env.get(key) or default).strip()

    api = get("API_URL", "http://localhost:5000").rstrip("/")
    auto_raw = get("ONETY_AUTO_ENVIAR_WHATSAPP", "true").lower()
    return {
        "api_url": api,
        "email": get("EMAIL"),
        "senha": get("SENHA"),
        "empresa_id": get("EMPRESA_ID"),
        "token": get("TOKEN"),
        "config_path": str(arquivo) if arquivo.exists() else None,
        "auto_enviar_whatsapp": auto_raw not in ("0", "false", "off", "no"),
        "whatsapp_instancia_id": get("ONETY_WHATSAPP_INSTANCIA_ID"),
        "whatsapp_instancia_nome": get(
            "ONETY_WHATSAPP_INSTANCIA_NOME",
            "Comercial Foco MEI",
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# API
# ─────────────────────────────────────────────────────────────────────────────

class OnetyClient:
    SESSION_IDLE_SECONDS = 180
    _AUTH_PATHS = ("/auth/login", "/auth/login-empresa")

    def __init__(
        self,
        api_url: str,
        token: str | None = None,
        empresa_id: str | int | None = None,
        *,
        reauth_fn: Callable[[], None] | None = None,
        session_idle_seconds: int | None = None,
    ):
        self.api_url = api_url.rstrip("/")
        self.token = token
        self.empresa_id = empresa_id
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self._reauth_fn = reauth_fn
        self._session_idle_seconds = int(
            session_idle_seconds or self.SESSION_IDLE_SECONDS
        )
        self._last_auth_at: float | None = None
        self._reauth_in_progress = False

    def set_reauth_fn(self, fn: Callable[[], None] | None) -> None:
        self._reauth_fn = fn

    def touch_auth(self) -> None:
        self._last_auth_at = time.monotonic()

    def _is_auth_path(self, path: str) -> bool:
        normalized = path if path.startswith("/") else f"/{path}"
        return any(normalized.startswith(p) for p in self._AUTH_PATHS)

    def _session_expired(self) -> bool:
        if self._last_auth_at is None:
            return True
        return (time.monotonic() - self._last_auth_at) > self._session_idle_seconds

    def _ensure_fresh_session(self) -> None:
        if not self._reauth_fn or self._reauth_in_progress:
            return
        if not self._session_expired():
            return
        self._reauth_in_progress = True
        try:
            self._reauth_fn()
            self.touch_auth()
        finally:
            self._reauth_in_progress = False

    @staticmethod
    def _is_token_error_response(resp: requests.Response) -> bool:
        if resp.status_code in (401, 403):
            return True
        if resp.status_code != 400:
            return False
        body = (resp.text or "").lower()
        return "token" in body and ("inválido" in body or "invalido" in body or "expir" in body)

    def _headers(self, json_body: bool = True) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.empresa_id is not None:
            headers["x-empresa-id"] = str(self.empresa_id)
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _url(self, path: str) -> str:
        return f"{self.api_url}{path if path.startswith('/') else '/' + path}"

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        data: Any | None = None,
        files: Any | None = None,
        timeout: int = 120,
        _retry_on_token: bool = True,
    ) -> Any:
        if not self._is_auth_path(path):
            self._ensure_fresh_session()

        use_json = files is None and data is None
        resp = self.session.request(
            method,
            self._url(path),
            headers=self._headers(json_body=use_json),
            json=json_body if use_json else None,
            data=data,
            files=files,
            timeout=timeout,
        )

        if (
            _retry_on_token
            and self._reauth_fn
            and self._is_token_error_response(resp)
            and not self._is_auth_path(path)
        ):
            self._reauth_in_progress = True
            try:
                self._reauth_fn()
                self.touch_auth()
            finally:
                self._reauth_in_progress = False
            resp = self.session.request(
                method,
                self._url(path),
                headers=self._headers(json_body=use_json),
                json=json_body if use_json else None,
                data=data,
                files=files,
                timeout=timeout,
            )

        if not resp.ok:
            detail = resp.text[:2000]
            raise RuntimeError(f"HTTP {resp.status_code} em {path}: {detail}")
        if not resp.content:
            return None
        ctype = resp.headers.get("Content-Type", "")
        if "application/json" in ctype:
            return resp.json()
        return resp.content

    def login(self, email: str, senha: str) -> str:
        data = self.request(
            "POST",
            "/auth/login",
            json_body={"email": email, "senha": senha},
            _retry_on_token=False,
        )
        token = data.get("token") if isinstance(data, dict) else None
        if not token:
            raise RuntimeError(f"Login sem token. Resposta: {data}")
        self.token = token
        return token

    def login_empresa(self, empresa_id: int | str) -> str:
        data = self.request(
            "POST",
            "/auth/login-empresa",
            json_body={"empresaId": int(empresa_id)},
        )
        # Alguns ambientes devolvem token novo; outros só confirmam.
        if isinstance(data, dict):
            novo = data.get("token") or data.get("accessToken")
            if novo:
                self.token = novo
        return self.token or ""

    def listar_modelos(self) -> list[dict[str, Any]]:
        data = self.request("GET", "/contratual/modelos-contrato/light")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("data") or data.get("modelos") or []
        return []

    def listar_clientes(self, empresa_id: int | str) -> list[dict[str, Any]]:
        data = self.request("GET", f"/comercial/pre-clientes/empresa/{empresa_id}")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("data") or data.get("preClientes") or data.get("clientes") or []
        return []

    def get_cliente(self, client_id: int | str) -> dict[str, Any]:
        data = self.request("GET", f"/comercial/pre-clientes/{client_id}")
        if not isinstance(data, dict):
            raise RuntimeError(f"Cliente {client_id} invalido: {data}")
        return data

    def listar_contratos_pre_cliente(self, client_id: int | str) -> list[dict[str, Any]]:
        data = self.request(
            "GET",
            f"/comercial/pre-clientes/{client_id}/contracts",
            timeout=60,
        )
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            for key in ("data", "contracts", "contratos", "items"):
                raw = data.get(key)
                if isinstance(raw, list):
                    return [x for x in raw if isinstance(x, dict)]
        return []

    def get_modelo(self, template_id: int | str) -> dict[str, Any]:
        data = self.request("GET", f"/contratual/modelos-contrato/{template_id}")
        if isinstance(data, dict) and "conteudo" not in data and isinstance(data.get("modelo"), dict):
            return data["modelo"]
        if not isinstance(data, dict):
            raise RuntimeError(f"Modelo {template_id} invalido: {data}")
        return data

    def listar_contratadas(self, empresa_id: int | str) -> list[dict[str, Any]]:
        data = self.request("GET", f"/contratual/contratada/empresa/{empresa_id}")
        if isinstance(data, list):
            return [e for e in data if e.get("ativo") in (1, True, "1", None) or e.get("ativo") is None]
        if isinstance(data, dict):
            return [data] if data.get("ativo") in (1, True, "1", None) else [data]
        return []

    def criar_cliente(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = self.request("POST", "/comercial/pre-clientes", json_body=payload)
        if not isinstance(data, dict):
            raise RuntimeError(f"Resposta inesperada ao criar cliente: {data}")
        return data

    def criar_lead(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = self.request("POST", "/comercial/leads", json_body=payload)
        if not isinstance(data, dict):
            raise RuntimeError(f"Resposta inesperada ao criar lead: {data}")
        return data

    def converter_lead(self, lead_id: int | str) -> dict[str, Any]:
        """Lead → pré-cliente com pre_clientes.lead_id (vínculo CRM ↔ contrato)."""
        data = self.request("POST", f"/comercial/leads/convert/{lead_id}")
        if not isinstance(data, dict):
            raise RuntimeError(f"Resposta inesperada ao converter lead {lead_id}: {data}")
        return data

    def get_lead(self, lead_id: int | str) -> dict[str, Any]:
        data = self.request("GET", f"/comercial/leads/{lead_id}")
        if not isinstance(data, dict):
            raise RuntimeError(f"Lead {lead_id} inválido: {data}")
        return data

    def mover_lead_fase(self, lead_id: int | str, funil_fase_id: int) -> dict[str, Any]:
        data = self.request(
            "PUT",
            f"/comercial/leads/{lead_id}/mover-fase",
            json_body={"funil_fase_id": int(funil_fase_id)},
        )
        if isinstance(data, dict):
            return data
        return {"ok": True, "raw": data}

    def listar_funil_fases(self, funil_id: int | str) -> list[dict[str, Any]]:
        """Tenta listar fases do funil (endpoint pode variar por versão Onety)."""
        paths = (
            f"/comercial/funis/{funil_id}/fases",
            f"/comercial/funil-fases/{funil_id}",
            f"/comercial/funil/{funil_id}/fases",
        )
        for path in paths:
            try:
                data = self.request("GET", path, timeout=30)
            except Exception:
                continue
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
            if isinstance(data, dict):
                for key in ("data", "fases", "funil_fases", "items"):
                    raw = data.get(key)
                    if isinstance(raw, list):
                        return [x for x in raw if isinstance(x, dict)]
        return []

    def criar_contrato_html(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = self.request(
            "POST",
            "/contratual/contratos-autentique/html",
            json_body=payload,
            timeout=180,
        )
        if not isinstance(data, dict):
            raise RuntimeError(f"Resposta inesperada: {data}")
        return data

    def listar_signatarios_contrato(self, contract_id: int | str) -> list[dict[str, Any]]:
        data = self.request(
            "GET",
            f"/contratual/contratos/{contract_id}/signatories",
            timeout=60,
        )
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            for key in ("data", "signatories", "signatarios", "items"):
                raw = data.get(key)
                if isinstance(raw, list):
                    return [x for x in raw if isinstance(x, dict)]
        return []

    def obter_contrato(self, contract_id: int | str) -> dict[str, Any]:
        """Detalhe do contrato — fallback para achar link Autentique."""
        paths = (
            f"/contratual/contratos/{contract_id}",
            f"/contratual/contratos-autentique/{contract_id}",
            f"/contratual/contratos/{contract_id}/detalhe",
        )
        last_exc: Exception | None = None
        for path in paths:
            try:
                data = self.request("GET", path, timeout=60)
            except Exception as exc:
                last_exc = exc
                continue
            if isinstance(data, dict):
                return data
        if last_exc:
            raise last_exc
        return {}

    def criar_contrato_pdf(
        self,
        *,
        nome: str,
        pdf_path: Path,
        payload_base: dict[str, Any],
    ) -> dict[str, Any]:
        pdf_bytes = pdf_path.read_bytes()
        b64 = base64.b64encode(pdf_bytes).decode("ascii")
        body = {
            **payload_base,
            "name": nome,
            "content": b64,
        }
        # Remove campos exclusivos do fluxo HTML
        body.pop("template_id", None)
        body.pop("variables", None)
        data = self.request(
            "POST",
            "/contratual/contratos-autentique",
            json_body=body,
            timeout=180,
        )
        if not isinstance(data, dict):
            raise RuntimeError(f"Resposta inesperada: {data}")
        return data

    def listar_instancias_whatsapp(
        self,
        *,
        empresa_id: str | int | None = None,
    ) -> list[dict[str, Any]]:
        """Instâncias Z-API cadastradas em Atendimento (para envio de link de assinatura)."""
        empresa_suffix = (
            f"?empresa_id={empresa_id}"
            if empresa_id not in (None, "")
            else ""
        )
        empresa_suffix_alt = (
            f"?empresaId={empresa_id}"
            if empresa_id not in (None, "")
            else ""
        )
        paths = (
            f"/atendimento/instancias{empresa_suffix}",
            f"/atendimento/instancias{empresa_suffix_alt}",
            f"/atendimento/instancias/empresa/{empresa_id}" if empresa_id else None,
            "/atendimento/instancias",
            "/atendimento/instancias-whatsapp",
            "/atendimento/instancias/zapi",
            "/atendimento/whatsapp/instancias",
            "/atendimento/zapi/instancias",
            "/atendimento/conexoes",
            "/atendimento/conexoes-whatsapp",
            "/zapi/instancias",
            "/integracao/zapi/instancias",
            "/contratual/whatsapp/instancias",
            "/contratual/instancias-whatsapp",
        )
        found: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for path in paths:
            if not path:
                continue
            try:
                data = self.request("GET", path, timeout=30)
            except Exception:
                continue
            items: list[dict[str, Any]] = []
            if isinstance(data, list):
                items = [x for x in data if isinstance(x, dict)]
            elif isinstance(data, dict):
                for key in ("data", "instancias", "items", "rows", "conexoes"):
                    raw = data.get(key)
                    if isinstance(raw, list):
                        items = [x for x in raw if isinstance(x, dict)]
                        break
            for item in items:
                iid = item.get("id") or item.get("instanciaId") or item.get("instancia_id")
                key = str(iid) if iid is not None else json.dumps(item, sort_keys=True, default=str)
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                item = dict(item)
                item["_robo_source_path"] = path
                found.append(item)
        return found

    def enviar_contrato_whatsapp(
        self,
        contract_id: int | str,
        *,
        instancia_id: int | str | None = None,
    ) -> dict[str, Any]:
        """
        Espelha o botão Onety: Enviar documento → WhatsApp.
        POST /contratual/contratos/{id}/send-whatsapp
        """
        bodies: list[dict[str, Any]] = []
        if instancia_id is not None:
            iid = int(instancia_id)
            bodies.append({"instanciaId": iid})
            bodies.append({"instancia_id": iid})
        bodies.append({})

        last_exc: Exception | None = None
        for body in bodies:
            try:
                data = self.request(
                    "POST",
                    f"/contratual/contratos/{contract_id}/send-whatsapp",
                    json_body=body,
                    timeout=120,
                )
            except Exception as exc:
                last_exc = exc
                continue
            if isinstance(data, dict):
                data["_robo_send_body"] = body
                return data
            return {"ok": True, "raw": data, "_robo_send_body": body}

        if last_exc:
            raise last_exc
        raise RuntimeError("Falha ao enviar contrato por WhatsApp")


# ─────────────────────────────────────────────────────────────────────────────
# Montagem de payload (espelha criar-contrato-autentique.js)
# ─────────────────────────────────────────────────────────────────────────────

_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


def _var(name: str, value: Any) -> dict[str, str]:
    if value is None:
        value = ""
    return {"variable_name": name, "value": str(value)}


def _fmt_brl(value: Any) -> str:
    try:
        return f"{float(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return str(value or "")


def _fmt_date_br(value: Any) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    if re.match(r"^\d{2}/\d{2}/\d{4}$", s):
        return s
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
    return s


def extrair_variaveis_template(conteudo: str) -> list[str]:
    return sorted(set(_VAR_RE.findall(conteudo or "")))


def build_pre_cliente_variables(cliente: dict[str, Any], prefix: str = "client") -> list[dict[str, str]]:
    """Espelho de frontend/utils/contratual/clienteExtra.js — CONTRATANTE."""
    if not cliente:
        return []
    getters = {
        "type": lambda c: c.get("tipo") or c.get("type") or "",
        "name": lambda c: c.get("nome") or c.get("name") or "",
        "cpf_cnpj": lambda c: c.get("cpf_cnpj") or c.get("cnpj") or c.get("cpf") or "",
        "email": lambda c: c.get("email") or "",
        "telefone": lambda c: c.get("telefone") or "",
        "endereco": lambda c: c.get("endereco") or "",
        "numero": lambda c: c.get("numero") or "",
        "complemento": lambda c: c.get("complemento") or "",
        "bairro": lambda c: c.get("bairro") or "",
        "cidade": lambda c: c.get("cidade") or "",
        "estado": lambda c: c.get("estado") or c.get("uf") or "",
        "cep": lambda c: c.get("cep") or "",
        "rg": lambda c: c.get("rg") or "",
        "estado_civil": lambda c: c.get("estado_civil") or "",
        "profissao": lambda c: c.get("profissao") or "",
        "sexo": lambda c: c.get("sexo") or "",
        "nacionalidade": lambda c: c.get("nacionalidade") or "",
        "representante": lambda c: c.get("representante") or "",
        "funcao": lambda c: c.get("funcao") or "",
        "empresa_id": lambda c: c.get("empresa_id") or "",
        "created_at": lambda c: c.get("criado_em") or c.get("created_at") or "",
        "equipe_id": lambda c: c.get("equipe_id") or "",
    }
    return [_var(f"{prefix}.{campo}", getter(cliente)) for campo, getter in getters.items()]


def build_company_variables(contratadas: list[dict[str, Any]]) -> list[dict[str, str]]:
    """CONTRATADA(s) ativas — company.*List do front."""
    if not contratadas:
        return []
    def join(key: str, alt: str | None = None) -> str:
        vals = []
        for e in contratadas:
            vals.append(str(e.get(key) or (e.get(alt) if alt else "") or ""))
        return ", ".join(vals)

    company_list = "\n".join(
        f"{i}. {e.get('nome') or ''} | {e.get('cnpj') or ''} | "
        f"{e.get('endereco') or ''}, {e.get('numero') or ''} - "
        f"{e.get('cidade') or ''}/{e.get('estado') or ''}"
        for i, e in enumerate(contratadas, 1)
    )
    return [
        _var("company.nameList", join("nome", "name")),
        _var("company.cnpjList", join("cnpj")),
        _var("company.razao_socialList", join("razao_social")),
        _var("company.enderecoList", join("endereco")),
        _var("company.numeroList", join("numero")),
        _var("company.complementoList", join("complemento")),
        _var("company.bairroList", join("bairro")),
        _var("company.cidadeList", join("cidade")),
        _var("company.estadoList", join("estado")),
        _var("company.cepList", join("cep")),
        _var("company.telefoneList", join("telefone")),
        _var("company.list", company_list),
    ]


def build_custom_from_contratada(contratada: dict[str, Any] | None) -> dict[str, str]:
    """Mapeia a unidade CF (contratada) para custom.* usados nos modelos de honorários."""
    if not contratada:
        return {}
    razao = contratada.get("razao_social") or contratada.get("nome") or ""
    cnpj = contratada.get("cnpj") or ""
    cidade = contratada.get("cidade") or ""
    return {
        "custom.razao_social_cf_contabilidade_ou_unidade_cf": razao,
        "custom.cpf_cnpj_da_cf_ou_da_unidade_franqueada_cf": cnpj,
        "custom.cidade": cidade,
    }


def montar_variaveis(
    spec: dict[str, Any],
    *,
    usuario: dict[str, Any] | None = None,
    contratadas: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    vars_map: dict[str, str] = {}

    def put(name: str, value: Any) -> None:
        if value is None:
            return
        # Não sobrescreve valor já preenchido com string vazia
        s = str(value)
        if name in vars_map and vars_map[name] and not s:
            return
        vars_map[name] = s

    cliente = spec.get("cliente") or {}
    for item in build_pre_cliente_variables(cliente, "client"):
        put(item["variable_name"], item["value"])

    if spec.get("cliente_extra"):
        for item in build_pre_cliente_variables(spec["cliente_extra"], "client_extra"):
            put(item["variable_name"], item["value"])

    for item in build_company_variables(contratadas or []):
        put(item["variable_name"], item["value"])

    # custom.* vindos da contratada (nós = CF / escritório)
    for k, v in build_custom_from_contratada((contratadas or [None])[0]).items():
        put(k, v)

    signatories = spec.get("signatories") or []
    if signatories:
        formatted = "\n".join(
            f"{i}. {s.get('name')} - {s.get('email')} - CPF: {s.get('cpf')} - Nascimento: {s.get('birth_date', '')}"
            for i, s in enumerate(signatories, 1)
        )
        put("signatory.list", formatted)
        put("signatory.nameList", ", ".join(s.get("name", "") for s in signatories))
        put("signatory.emailList", ", ".join(s.get("email", "") for s in signatories))
        put("signatory.cpfList", ", ".join(str(s.get("cpf") or "") for s in signatories))
        put("signatory.birthList", ", ".join(str(s.get("birth_date") or "") for s in signatories))
        put("contact.nomeList", ", ".join(s.get("name", "") for s in signatories))
        put("contact.emailList", ", ".join(s.get("email", "") for s in signatories))
        put("contact.telefoneList", ", ".join(str(s.get("telefone") or "") for s in signatories))
        put("contact.cpfList", ", ".join(str(s.get("cpf") or "") for s in signatories))

    produtos = spec.get("produtos_dados") or []
    if produtos:
        product_list = "\n".join(
            f"{i}. {p.get('nome')} - Quantidade: {p.get('quantidade', 1)} "
            f"- Descrição: {p.get('descricao', '')} - Valor: {p.get('valor_de_venda', p.get('valor', 0))}"
            for i, p in enumerate(produtos, 1)
        )
        put("product.list", product_list)
        put("product.nomeList", ", ".join(str(p.get("nome") or "") for p in produtos))
        put("product.nameList", ", ".join(str(p.get("nome") or "") for p in produtos))
        put("product.valorList", ", ".join(str(p.get("valor") or p.get("valor_de_venda") or "") for p in produtos))
        put(
            "product.valor_de_vendaList",
            ", ".join(str(p.get("valor_de_venda") or p.get("valor") or "") for p in produtos),
        )
        put(
            "product.descricaoList",
            "\n\n".join(f"{p.get('nome')}: {p.get('descricao') or ''}" for p in produtos),
        )
        tipos = {str(p.get("tipo") or "") for p in produtos}
        put("contract.type", next(iter(tipos)) if len(tipos) == 1 else "multiplos")

    if spec.get("valor") is not None:
        put("contract.total_value", _fmt_brl(spec["valor"]))
        put("contract.value", _fmt_brl(spec["valor"]))
    if spec.get("valor_recorrente") is not None:
        put("contract.mrr", _fmt_brl(spec["valor_recorrente"]))
    put("contract.created_at", datetime.now().strftime("%d/%m/%Y"))
    if spec.get("expires_at"):
        put("contract.expires_at", _fmt_date_br(spec["expires_at"]))
    if spec.get("start_at"):
        put("contract.start_at", _fmt_date_br(spec["start_at"]))
    if spec.get("end_at"):
        put("contract.end_at", _fmt_date_br(spec["end_at"]))

    extras = spec.get("variaveis_extras") or spec.get("variables") or {}
    if isinstance(extras, dict):
        for k, v in extras.items():
            put(k, v)
    elif isinstance(extras, list):
        for item in extras:
            if isinstance(item, dict) and item.get("variable_name"):
                put(item["variable_name"], item.get("value", ""))

    # custom explícito no JSON (prioridade)
    custom = spec.get("custom") or {}
    if isinstance(custom, dict):
        for k, v in custom.items():
            key = k if str(k).startswith("custom.") else f"custom.{k}"
            put(key, v)

    if spec.get("valor_recorrente") is not None:
        if not str(vars_map.get("custom.mensalidade") or "").strip():
            put("custom.mensalidade", _fmt_brl(spec["valor_recorrente"]))

    if usuario:
        put("user.full_name", usuario.get("nome") or usuario.get("full_name") or "")
        put("user.email", usuario.get("email") or "")

    return [_var(k, v) for k, v in sorted(vars_map.items())]


def checar_variaveis_faltantes(
    template_vars: list[str],
    filled: list[dict[str, str]],
    *,
    opcionais_vazias: set[str] | None = None,
) -> list[str]:
    """Retorna variáveis do template sem valor. Opcionais (ex.: custom.notes) podem ficar vazias."""
    allow_empty = opcionais_vazias or {"custom.notes"}
    filled_map = {i["variable_name"]: str(i.get("value") or "").strip() for i in filled}
    faltando = []
    for name in template_vars:
        if name in allow_empty:
            continue
        val = filled_map.get(name, "")
        if not val:
            faltando.append(name)
    return faltando


def enriquecer_spec_com_api(
    client: OnetyClient,
    cfg: dict[str, Any],
    spec: dict[str, Any],
    *,
    usuario: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[dict[str, str]], list[str], list[str]]:
    """
    Busca cliente + contratada + template.
    Retorna (spec_enriquecido, variables, vars_do_template, faltando).
    """
    spec = dict(spec)
    contratadas = client.listar_contratadas(cfg["empresa_id"])

    if spec.get("client_id"):
        try:
            remoto = client.get_cliente(spec["client_id"])
            local = dict(spec.get("cliente") or {})
            # API preenche base; JSON local sobrescreve campos informados
            merged = {**remoto, **{k: v for k, v in local.items() if v not in (None, "")}}
            spec["cliente"] = merged
        except Exception as exc:
            print(f"  aviso: nao foi possivel recarregar cliente: {exc}")

    template_vars: list[str] = []
    if spec.get("template_id"):
        try:
            modelo = client.get_modelo(spec["template_id"])
            template_vars = extrair_variaveis_template(modelo.get("conteudo") or "")
        except Exception as exc:
            print(f"  aviso: nao foi possivel ler template: {exc}")

    variables = montar_variaveis(spec, usuario=usuario, contratadas=contratadas)
    faltando = checar_variaveis_faltantes(template_vars, variables) if template_vars else []
    return spec, variables, template_vars, faltando


def validar_spec(spec: dict[str, Any], *, modo: str) -> list[str]:
    erros: list[str] = []
    if modo == "html" and not spec.get("template_id"):
        erros.append("template_id é obrigatório no modo html")

    vai_criar_cliente = bool(spec.get("criar_cliente")) or (
        not spec.get("client_id") and isinstance(spec.get("cliente"), dict)
    )
    if not spec.get("client_id") and not vai_criar_cliente:
        erros.append("client_id é obrigatório (ou use criar_cliente + bloco cliente)")
    if vai_criar_cliente and not spec.get("client_id"):
        cli = spec.get("cliente") or {}
        if not cli.get("nome") and not cli.get("razao_social") and not cli.get("nome_fantasia"):
            erros.append("cliente.nome é obrigatório para criar cliente")
        if not cli.get("email"):
            erros.append("cliente.email é obrigatório para criar cliente")

    signs = spec.get("signatories") or []
    if not signs:
        erros.append("signatories precisa ter ao menos 1 signatário")
    else:
        for i, s in enumerate(signs, 1):
            if not s.get("name") or not s.get("email"):
                erros.append(f"signatário {i}: name e email são obrigatórios")
    if not spec.get("expires_at"):
        erros.append("expires_at é obrigatório (YYYY-MM-DD)")
    return erros


def montar_payload_novo_cliente(spec: dict[str, Any], empresa_id: str | int) -> dict[str, Any]:
    cli = dict(spec.get("cliente") or {})
    # Se o bloco cliente estiver magro, completa com o 1º signatário
    sig0 = (spec.get("signatories") or [{}])[0]
    nome = cli.get("nome") or cli.get("razao_social") or cli.get("nome_fantasia") or sig0.get("name")
    email = cli.get("email") or sig0.get("email")
    telefone = cli.get("telefone") or cli.get("telefone_celular") or sig0.get("telefone")
    cpf_cnpj = cli.get("cpf_cnpj") or cli.get("cnpj") or cli.get("cpf") or sig0.get("cpf")
    cpf_digits = "".join(ch for ch in str(cpf_cnpj or "") if ch.isdigit())
    cpf_cnpj = cpf_digits or cpf_cnpj
    tipo = cli.get("tipo") or ("empresa" if len(cpf_digits) > 11 else "pessoa_fisica")
    if tipo in ("pessoa_juridica", "pj", "juridica"):
        tipo = "empresa"

    payload = {
        "tipo": tipo,
        "nome": nome,
        "email": email,
        "telefone": telefone,
        "cpf_cnpj": cpf_cnpj,
        "endereco": cli.get("endereco"),
        "cep": cli.get("cep"),
        "numero": cli.get("numero"),
        "complemento": cli.get("complemento"),
        "bairro": cli.get("bairro"),
        "cidade": cli.get("cidade"),
        "estado": cli.get("uf") or cli.get("estado"),
        "empresa_id": int(empresa_id),
    }
    lead_id = spec.get("lead_id") or spec.get("onety_lead_id")
    if lead_id not in (None, ""):
        payload["lead_id"] = int(lead_id)
    return {k: v for k, v in payload.items() if v not in (None, "")}


def montar_payload(
    spec: dict[str, Any],
    empresa_id: str | int,
    *,
    variables: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    fin = spec.get("financeiro") or {}
    payload: dict[str, Any] = {
        "template_id": spec.get("template_id"),
        "client_id": spec["client_id"],
        "client_extra_id": spec.get("client_extra_id"),
        "signatories": spec["signatories"],
        "variables": variables if variables is not None else montar_variaveis(spec),
        "empresa_id": int(empresa_id),
        "valor": spec.get("valor"),
        "valor_recorrente": spec.get("valor_recorrente"),
        "expires_at": spec["expires_at"],
        "start_at": spec.get("start_at"),
        "end_at": spec.get("end_at"),
        "produtos_dados": spec.get("produtos_dados") or [],
    }
    for key in ("categoria_id", "sub_categoria_id", "centro_de_custo_id", "conta_id", "conta_api_id"):
        val = fin.get(key) if key in fin else spec.get(key)
        if val is not None:
            payload[key] = val
    return payload


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def autenticar(
    client: OnetyClient,
    cfg: dict[str, Any],
    *,
    force_login: bool = False,
) -> dict[str, Any]:
    usuario: dict[str, Any] = {}
    use_static_token = bool(cfg.get("token")) and not force_login

    if use_static_token:
        client.token = cfg["token"]
        print("Usando TOKEN do config.env")
    else:
        if not cfg["email"] or not cfg["senha"]:
            if cfg.get("token"):
                client.token = cfg["token"]
                print("TOKEN estático — relogin de empresa apenas")
            else:
                raise SystemExit(
                    "Configure EMAIL e SENHA em config.env (copie de config.example.env) "
                    "ou defina TOKEN."
                )
        else:
            print(f"Login em {cfg['api_url']} como {cfg['email']}...")
            data = client.request(
                "POST",
                "/auth/login",
                json_body={"email": cfg["email"], "senha": cfg["senha"]},
                _retry_on_token=False,
            )
            token = data.get("token") if isinstance(data, dict) else None
            if not token:
                raise RuntimeError(f"Login sem token. Resposta: {data}")
            client.token = token
            usuario = (data.get("user") if isinstance(data, dict) else None) or {}
            print("Login OK")

    if not cfg.get("empresa_id"):
        raise SystemExit("EMPRESA_ID é obrigatório em config.env")

    print(f"Selecionando empresa {cfg['empresa_id']}...")
    client.empresa_id = cfg["empresa_id"]
    client.login_empresa(cfg["empresa_id"])
    client.touch_auth()
    print("Empresa OK")
    return usuario


IGNORAR_ENTRADA = {
    "exemplo_contrato.json",
    "lote_exemplo.json",
    "exemplo_lote_gpt.json",
    "COMO_PREENCHER.txt",
    "PROMPT_PARA_GPT.txt",
    "modelo_contratos.xlsx",
}


def carregar_specs_de_arquivo(arquivo: Path) -> list[dict[str, Any]]:
    """JSON ou Excel (.xlsx) → lista de specs de contrato (com padrão FOCO MEI)."""
    from padrao_lote import carregar_padrao, expandir_lista

    padrao = carregar_padrao(ENTRADA)
    suf = arquivo.suffix.lower()
    if suf in (".xlsx", ".xlsm"):
        from excel_import import planilha_para_specs

        return expandir_lista(planilha_para_specs(arquivo), padrao)
    dados = json.loads(arquivo.read_text(encoding="utf-8"))
    return expandir_lista(extrair_specs(dados), padrao)


def cmd_listar_modelos(client: OnetyClient) -> None:
    modelos = client.listar_modelos()
    if not modelos:
        print("Nenhum modelo retornado.")
        return
    print(f"\n{len(modelos)} modelo(s) — use o id em template_id:\n")
    for m in modelos:
        mid = m.get("id")
        nome = m.get("nome") or m.get("titulo") or m.get("name") or "?"
        print(f"  id={mid}  {nome}")


def cmd_listar_clientes(client: OnetyClient, empresa_id: str | int) -> None:
    clientes = client.listar_clientes(empresa_id)
    if not clientes:
        print("Nenhum pré-cliente retornado.")
        return
    print(f"\n{len(clientes)} cliente(s) — use o id em client_id:\n")
    for c in clientes[:100]:
        cid = c.get("id")
        nome = (
            c.get("nome_fantasia")
            or c.get("razao_social")
            or c.get("nome")
            or c.get("apelido")
            or "?"
        )
        print(f"  id={cid}  {nome}")
    if len(clientes) > 100:
        print(f"  ... e mais {len(clientes) - 100}")


def cmd_listar_instancias_whatsapp(
    client: OnetyClient,
    cfg: dict[str, Any],
    *,
    filtro_nome: str = "",
    somente_conectadas: bool = False,
    dump_json: bool = False,
) -> None:
    instancias = client.listar_instancias_whatsapp(empresa_id=cfg.get("empresa_id"))
    filtradas = _filtrar_instancias_whatsapp(
        instancias,
        empresa_id=cfg.get("empresa_id"),
        filtro_nome=filtro_nome,
        somente_conectadas=somente_conectadas,
    )
    if not instancias:
        print(
            "\nNenhuma instância WhatsApp encontrada via API.\n"
            "Dicas:\n"
            "  1) No Onety, abra o modal WhatsApp e veja GET na aba Network (lista instâncias)\n"
            "  2) Inspecione o card 'Comercial Foco MEI' (data-id / value no HTML)\n"
            "  3) Use --testar-send-whatsapp 8980 após definir ONETY_WHATSAPP_INSTANCIA_ID\n"
        )
        return

    if dump_json and filtradas:
        out = SAIDA / "debug_instancias_whatsapp.json"
        SAIDA.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(filtradas[:20], ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"\nDump JSON (até 20): {out}\n")

    total_label = (
        f"{len(filtradas)} filtrada(s) de {len(instancias)} total"
        if filtradas and len(filtradas) != len(instancias)
        else f"{len(instancias)} instância(s)"
    )
    print(f"\n{total_label} WhatsApp:\n")
    rows = filtradas if filtradas else instancias
    for inst in rows:
        iid = _extrair_id_instancia(inst)
        nome = _extrair_nome_instancia(inst) or "?"
        status = _extrair_status_instancia(inst) or "?"
        phone = inst.get("telefone") or inst.get("phone") or inst.get("numero") or ""
        src = inst.get("_robo_source_path") or "?"
        extra = f" tel={phone}" if phone else ""
        print(f"  id={iid}  {nome}  status={status}{extra}  (via {src})")

    if not filtradas and filtro_nome:
        print(
            f"\nNenhuma instância bate com filtro {filtro_nome!r}. "
            "Tente: --listar-instancias-whatsapp --filtro-instancia foco --somente-conectadas"
        )

    print(
        "\nUse no config.env:\n"
        "  ONETY_WHATSAPP_INSTANCIA_ID=<id acima>\n"
        "  ONETY_WHATSAPP_INSTANCIA_NOME=Comercial Foco MEI\n"
        "\nDica: ao enviar manual no Onety, veja POST send-whatsapp → Payload → instanciaId\n"
    )


def salvar_saida(resultado: dict[str, Any], origem: Path | str) -> Path:
    SAIDA.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = origem.stem if isinstance(origem, Path) else str(origem)
    out = SAIDA / f"resultado_{stem}_{stamp}.json"
    out.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def extrair_specs(dados: Any) -> list[dict[str, Any]]:
    """Aceita 1 contrato, lista, ou { \"contratos\": [...] }."""
    if isinstance(dados, list):
        return [x for x in dados if isinstance(x, dict)]
    if isinstance(dados, dict):
        if isinstance(dados.get("contratos"), list):
            return [x for x in dados["contratos"] if isinstance(x, dict)]
        return [dados]
    raise ValueError("JSON deve ser um objeto, uma lista ou { contratos: [...] }")


def resolver_arquivo(caminho: str | None) -> Path:
    if not caminho:
        raise FileNotFoundError("arquivo não informado")
    arquivo = Path(caminho)
    if not arquivo.is_absolute():
        cand = (Path.cwd() / arquivo).resolve()
        if cand.exists():
            return cand
        return (ROOT / caminho).resolve()
    return arquivo


def listar_arquivos_entrada() -> list[Path]:
    return sorted(
        [
            p
            for p in ENTRADA.glob("*.json")
            if p.name not in IGNORAR_ENTRADA and not p.name.startswith("_")
        ],
        key=lambda p: p.name.lower(),
    )


_PRE_CLIENTE_KEYS = ("preClienteId", "pre_cliente_id", "clientId", "client_id")


def _ler_int_em_chaves(dados: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        val = dados.get(key)
        if val is not None and str(val).strip() != "":
            try:
                return int(val)
            except (TypeError, ValueError):
                continue
    return None


def extrair_pre_cliente_id_convert(dados: dict[str, Any] | None) -> int | None:
    """Resposta de POST /comercial/leads/convert/{id} ou POST /pre-clientes."""
    if not isinstance(dados, dict):
        return None
    found = _ler_int_em_chaves(dados, _PRE_CLIENTE_KEYS)
    if found is not None:
        return found
    # Resposta de convert/criação pode usar id raiz como pre_clientes.id
    root_id = _ler_int_em_chaves(dados, ("id",))
    if root_id is not None:
        return root_id
    for nest in ("preCliente", "pre_cliente", "data"):
        nested = dados.get(nest)
        if isinstance(nested, dict):
            found = extrair_pre_cliente_id_convert(nested)
            if found is not None:
                return found
    return None


def extrair_pre_cliente_id_do_lead(lead: dict[str, Any] | None) -> int | None:
    """GET /comercial/leads/{id} — nunca usar lead.id como pre_cliente_id."""
    if not isinstance(lead, dict):
        return None
    found = _ler_int_em_chaves(lead, _PRE_CLIENTE_KEYS)
    if found is not None:
        return found
    for nest in ("preCliente", "pre_cliente"):
        nested = lead.get(nest)
        if not isinstance(nested, dict):
            continue
        found = _ler_int_em_chaves(nested, _PRE_CLIENTE_KEYS)
        if found is not None:
            return found
        nested_id = _ler_int_em_chaves(nested, ("id",))
        if nested_id is not None:
            return nested_id
    return None


def resolver_client_id_via_lead(
    client: OnetyClient,
    lead_id: int | str,
    *,
    spec: dict[str, Any] | None = None,
    empresa_id: str | int | None = None,
) -> int:
    """
    Garante pré-cliente ligado ao lead antes do contrato HTML.
    1) convert  2) GET lead (só campos de vínculo)  3) POST pre-clientes com lead_id
    """
    lid = int(lead_id)
    convert_detail = ""

    try:
        convertido = client.converter_lead(lid)
        pre_id = extrair_pre_cliente_id_convert(convertido)
        if pre_id is not None:
            return pre_id
        convert_detail = f"convert sem pre_cliente_id: {convertido!r}"
    except Exception as exc:
        convert_detail = f"convert falhou: {exc}"

    try:
        lead = client.get_lead(lid)
        pre_id = extrair_pre_cliente_id_do_lead(lead)
        if pre_id is not None:
            return pre_id
    except Exception as exc:
        convert_detail = f"{convert_detail}; GET lead falhou: {exc}".strip("; ")

    if spec is not None and empresa_id is not None:
        body_cli = montar_payload_novo_cliente({**spec, "lead_id": lid, "onety_lead_id": lid}, empresa_id)
        try:
            criado = client.criar_cliente(body_cli)
            pre_id = extrair_pre_cliente_id_convert(criado)
            if pre_id is not None:
                return pre_id
            convert_detail = f"{convert_detail}; criar pre-cliente sem id: {criado!r}".strip("; ")
        except Exception as exc:
            convert_detail = f"{convert_detail}; criar pre-cliente falhou: {exc}".strip("; ")

    raise RuntimeError(
        f"Não foi possível obter pre_cliente_id válido do lead {lid} ({convert_detail})"
    )


def _url_parece_assinatura(val: Any) -> bool:
    s = str(val or "").strip()
    if not s.startswith("http"):
        return False
    low = s.lower()
    if any(x in low for x in ("/api/", "back.cfonety", "localhost", ".json")):
        return False
    if any(x in low for x in ("autentique", "sign", "assin", "doc", "contract", "contrato")):
        return True
    return len(s) >= 20


def _deep_scan_http_urls(node: Any, *, depth: int = 0) -> list[str]:
    if depth > 8:
        return []
    found: list[str] = []
    if isinstance(node, str):
        if _url_parece_assinatura(node):
            found.append(str(node).strip())
        return found
    if isinstance(node, dict):
        for val in node.values():
            found.extend(_deep_scan_http_urls(val, depth=depth + 1))
        return found
    if isinstance(node, list):
        for item in node:
            found.extend(_deep_scan_http_urls(item, depth=depth + 1))
    return found


def extrair_link_assinatura(
    resultado: dict[str, Any] | None,
    *,
    client: OnetyClient | None = None,
    contract_id: int | str | None = None,
) -> str | None:
    """Link Autentique do signatário contratante (para copiar na UI)."""
    url_keys = (
        "link",
        "signingUrl",
        "signing_url",
        "signatureUrl",
        "signature_url",
        "url_assinatura",
        "link_assinatura",
        "linkAssinatura",
        "urlAssinatura",
        "public_url",
        "publicUrl",
        "public_link",
        "publicLink",
        "short_link",
        "shortLink",
        "url",
        "href",
    )

    def scan_signatario(sig: dict[str, Any]) -> str | None:
        funcao = str(sig.get("funcao_assinatura") or sig.get("role") or "").lower()
        is_contratante = "contratante" in funcao and "contratada" not in funcao
        if not is_contratante and funcao:
            return None
        for key in url_keys:
            val = sig.get(key)
            if _url_parece_assinatura(val):
                return str(val).strip()
        return None

    if isinstance(resultado, dict):
        for key in url_keys:
            val = resultado.get(key)
            if _url_parece_assinatura(val):
                return str(val).strip()
        for nest in ("contract", "contrato", "data"):
            nested = resultado.get(nest)
            if isinstance(nested, dict):
                found = extrair_link_assinatura(nested, client=client, contract_id=contract_id)
                if found:
                    return found
        signs = resultado.get("signatories") or resultado.get("signatarios") or []
        if isinstance(signs, list):
            for sig in signs:
                if not isinstance(sig, dict):
                    continue
                found = scan_signatario(sig)
                if found:
                    return found
            for sig in signs:
                if not isinstance(sig, dict):
                    continue
                for key in url_keys:
                    val = sig.get(key)
                    if _url_parece_assinatura(val):
                        return str(val).strip()
        for url in _deep_scan_http_urls(resultado):
            return url

    if client is not None and contract_id is not None:
        try:
            for sig in client.listar_signatarios_contrato(contract_id):
                found = scan_signatario(sig)
                if found:
                    return found
                for key in url_keys:
                    val = sig.get(key)
                    if _url_parece_assinatura(val):
                        return str(val).strip()
                for url in _deep_scan_http_urls(sig):
                    return url
        except Exception:
            pass

        try:
            detail = client.obter_contrato(contract_id)
            for url in _deep_scan_http_urls(detail):
                return url
        except Exception:
            pass

    return None


def contratante_ja_assinou(signatories: list[dict[str, Any]]) -> bool:
    """True quando o signatário contratante tem assinado_em (parcial — suficiente p/ liberar FocoMEI)."""
    if not signatories:
        return False
    for sig in signatories:
        funcao = str(sig.get("funcao_assinatura") or sig.get("role") or "").lower()
        is_contratante = "contratante" in funcao and "contratada" not in funcao
        if not is_contratante:
            continue
        if sig.get("assinado_em") or sig.get("signed_at") or sig.get("assinadoEm"):
            return True
    # Fallback: primeiro signatário que não é contratada
    for sig in signatories:
        funcao = str(sig.get("funcao_assinatura") or sig.get("role") or "").lower()
        if "contratada" in funcao:
            continue
        if sig.get("assinado_em") or sig.get("signed_at") or sig.get("assinadoEm"):
            return True
    return False


def analisar_status_contrato(client: OnetyClient, contract_id: int | str) -> dict[str, Any]:
    signs = client.listar_signatarios_contrato(contract_id)
    client_signed = contratante_ja_assinou(signs)
    signing_url = extrair_link_assinatura(None, client=client, contract_id=contract_id)
    total = len(signs)
    assinados = sum(
        1
        for s in signs
        if s.get("assinado_em") or s.get("signed_at") or s.get("assinadoEm")
    )
    return {
        "contratoId": int(contract_id),
        "clientSigned": client_signed,
        "fullySigned": total > 0 and assinados >= total,
        "signedCount": assinados,
        "totalSignatories": total,
        "signingUrl": signing_url,
    }


def _extrair_id_contrato_item(item: dict[str, Any]) -> int | None:
    for key in ("id", "contract_id", "contrato_id", "contratoId"):
        val = item.get(key)
        if val in (None, ""):
            continue
        try:
            return int(val)
        except (TypeError, ValueError):
            continue
    return None


def resolver_contrato_id_por_lead(client: OnetyClient, lead_id: int | str) -> int | None:
    """Último contrato do pré-cliente vinculado ao lead (fallback quando BD perdeu contrato_onety_id)."""
    lead = client.get_lead(lead_id)
    pre_id = (
        lead.get("pre_cliente_id")
        or lead.get("preClienteId")
        or lead.get("client_id")
        or lead.get("clientId")
    )
    if pre_id in (None, ""):
        return None

    contracts = client.listar_contratos_pre_cliente(pre_id)
    if not contracts:
        return None

    def sort_key(item: dict[str, Any]) -> str:
        for key in ("created_at", "createdAt", "updated_at", "updatedAt", "id"):
            val = item.get(key)
            if val not in (None, ""):
                return str(val)
        return ""

    for item in sorted(contracts, key=sort_key, reverse=True):
        cid = _extrair_id_contrato_item(item)
        if cid is not None:
            return cid
    return _extrair_id_contrato_item(contracts[0])


def extrair_contrato_id(resultado: dict[str, Any] | None) -> int | None:
    """ID usado em POST /contratual/contratos/{id}/send-whatsapp."""
    if not isinstance(resultado, dict):
        return None
    for key in ("id", "contract_id", "contrato_id", "contratoId"):
        val = resultado.get(key)
        if val is not None and str(val).strip() != "":
            try:
                return int(val)
            except (TypeError, ValueError):
                continue
    for nest in ("contract", "contrato", "data"):
        nested = resultado.get(nest)
        if isinstance(nested, dict):
            cid = extrair_contrato_id(nested)
            if cid is not None:
                return cid
    return None


def _normalizar_nome_instancia(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _extrair_nome_instancia(item: dict[str, Any]) -> str:
    direct_keys = (
        "nome",
        "name",
        "titulo",
        "label",
        "descricao",
        "apelido",
        "instance_name",
        "instanceName",
        "session_name",
        "sessionName",
        "alias",
        "display_name",
        "displayName",
        "nome_instancia",
        "nomeInstancia",
        "titulo_instancia",
        "identificador",
        "instancia_nome",
        "instanciaNome",
    )
    for key in direct_keys:
        val = item.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    for nest_key in ("zapi", "instance", "instancia", "conexao", "whatsapp", "ZApiInstancia"):
        nested = item.get(nest_key)
        if isinstance(nested, dict):
            nested_name = _extrair_nome_instancia(nested)
            if nested_name:
                return nested_name
    return ""


def _extrair_status_instancia(item: dict[str, Any]) -> str:
    for key in ("status", "situacao", "connected", "conectado", "state", "connection_state"):
        val = item.get(key)
        if val is None:
            continue
        if isinstance(val, bool):
            return "conectado" if val else "desconectado"
        text = str(val).strip().lower()
        if text:
            return text
    return ""


def _instancia_esta_conectada(item: dict[str, Any]) -> bool:
    status = _extrair_status_instancia(item)
    return status in ("conectado", "connected", "open", "online", "true", "1")


def _instancia_pertence_empresa(item: dict[str, Any], empresa_id: str | int | None) -> bool:
    if empresa_id in (None, ""):
        return True
    alvo = str(empresa_id).strip()
    for key in ("empresa_id", "empresaId", "empresa", "company_id", "companyId"):
        val = item.get(key)
        if val is None:
            continue
        if str(val).strip() == alvo:
            return True
    return False


def _filtrar_instancias_whatsapp(
    instancias: list[dict[str, Any]],
    *,
    empresa_id: str | int | None = None,
    filtro_nome: str = "",
    somente_conectadas: bool = False,
) -> list[dict[str, Any]]:
    filtro = _normalizar_nome_instancia(filtro_nome)
    out: list[dict[str, Any]] = []
    for inst in instancias:
        if empresa_id not in (None, "") and not _instancia_pertence_empresa(inst, empresa_id):
            # API /atendimento/instancias costuma ser global — não excluir se não vier empresa_id
            pass
        if somente_conectadas and not _instancia_esta_conectada(inst):
            continue
        if filtro:
            nome = _normalizar_nome_instancia(_extrair_nome_instancia(inst))
            blob = _normalizar_nome_instancia(json.dumps(inst, ensure_ascii=False, default=str))
            if filtro not in nome and filtro not in blob:
                continue
        out.append(inst)
    return out


def _extrair_id_instancia(item: dict[str, Any]) -> int | None:
    for key in ("id", "instanciaId", "instancia_id", "instanceId", "conexao_id"):
        val = item.get(key)
        if val is not None and str(val).strip() != "":
            try:
                return int(val)
            except (TypeError, ValueError):
                continue
    return None


def resolver_whatsapp_instancia_id(
    client: OnetyClient,
    cfg: dict[str, Any],
) -> int | None:
    raw_id = cfg.get("whatsapp_instancia_id")
    if raw_id not in (None, ""):
        try:
            return int(raw_id)
        except (TypeError, ValueError):
            raise RuntimeError(
                f"ONETY_WHATSAPP_INSTANCIA_ID inválido: {raw_id!r}",
            )

    instancias = client.listar_instancias_whatsapp(empresa_id=cfg.get("empresa_id"))
    instancias = _filtrar_instancias_whatsapp(
        instancias,
        empresa_id=cfg.get("empresa_id"),
        filtro_nome=str(cfg.get("whatsapp_instancia_nome") or ""),
        somente_conectadas=True,
    )
    if not instancias:
        return None

    nome_alvo = _normalizar_nome_instancia(cfg.get("whatsapp_instancia_nome"))
    if nome_alvo:
        for inst in instancias:
            nome = _normalizar_nome_instancia(_extrair_nome_instancia(inst))
            blob = _normalizar_nome_instancia(json.dumps(inst, ensure_ascii=False, default=str))
            if nome == nome_alvo or nome_alvo in nome or nome_alvo in blob:
                iid = _extrair_id_instancia(inst)
                if iid is not None:
                    return iid

    if len(instancias) == 1:
        return _extrair_id_instancia(instancias[0])

    return None


def tentar_enviar_contrato_whatsapp(
    client: OnetyClient,
    cfg: dict[str, Any],
    contract_id: int,
) -> tuple[bool, str]:
    if not cfg.get("auto_enviar_whatsapp"):
        return False, "desabilitado"

    instancia_id: int | None
    try:
        instancia_id = resolver_whatsapp_instancia_id(client, cfg)
    except RuntimeError as exc:
        return False, str(exc)

    try:
        resp = client.enviar_contrato_whatsapp(
            contract_id,
            instancia_id=instancia_id,
        )
    except Exception as exc:
        return False, str(exc)

    if isinstance(resp, dict):
        if resp.get("ok") is False or resp.get("error"):
            return False, str(resp.get("error") or resp.get("message") or resp)
        body_used = resp.get("_robo_send_body") or {}
        if instancia_id is not None:
            return True, f"instancia={instancia_id} body={body_used}"
        if body_used:
            return True, f"body={body_used}"
        return True, "enviado"

    return True, "enviado"


def processar_spec(
    client: OnetyClient,
    cfg: dict[str, Any],
    spec: dict[str, Any],
    *,
    rotulo: str,
    dry_run: bool,
    pdf_arg: str | None,
    usuario: dict[str, Any] | None = None,
    force: bool = False,
) -> tuple[bool, str, dict[str, Any]]:
    modo = (pdf_arg and "pdf") or (spec.get("modo") or "html")
    erros = validar_spec(spec, modo=modo)
    if erros:
        return False, "invalido: " + "; ".join(erros), {}

    cliente_criado_id = None
    lead_id = spec.get("lead_id") or spec.get("onety_lead_id")
    precisa_criar = bool(spec.get("criar_cliente")) or not spec.get("client_id")

    if lead_id not in (None, "") and not dry_run:
        try:
            pre_id = resolver_client_id_via_lead(
                client,
                lead_id,
                spec=spec,
                empresa_id=cfg["empresa_id"],
            )
            spec = {**spec, "client_id": pre_id, "criar_cliente": False}
            precisa_criar = False
        except Exception as exc:
            return False, f"falha ao vincular lead {lead_id} ao pré-cliente: {exc}", {}

    if precisa_criar and not spec.get("client_id"):
        body_cli = montar_payload_novo_cliente(spec, cfg["empresa_id"])
        if dry_run:
            spec = {**spec, "client_id": 0}
        else:
            try:
                criado = client.criar_cliente(body_cli)
            except Exception as exc:
                msg = str(exc)
                doc = "".join(ch for ch in str(body_cli.get("cpf_cnpj") or "") if ch.isdigit())
                if not doc:
                    doc = "".join(
                        ch
                        for ch in str(
                            (spec.get("cliente") or {}).get("cpf_cnpj")
                            or (spec.get("cliente") or {}).get("cnpj")
                            or ""
                        )
                        if ch.isdigit()
                    )
                msg_lower = msg.lower()
                duplicate_hint = (
                    "já cadastrado" in msg_lower
                    or "ja cadastrado" in msg_lower
                    or "already exists" in msg_lower
                    or "duplicad" in msg_lower
                )
                # Se já existe, tenta achar pelo CPF/CNPJ e segue
                if duplicate_hint or "400" in msg or "409" in msg:
                    achado = None
                    if doc:
                        try:
                            for c in client.listar_clientes(cfg["empresa_id"]):
                                d = "".join(
                                    ch
                                    for ch in str(
                                        c.get("cpf_cnpj") or c.get("cnpj") or c.get("cpf") or ""
                                    )
                                    if ch.isdigit()
                                )
                                if d == doc:
                                    achado = c.get("id")
                                    break
                        except Exception:
                            achado = None
                    if achado:
                        spec = {**spec, "client_id": int(achado)}
                        precisa_criar = False
                    else:
                        return False, f"falha ao criar cliente: {exc}", {}
                else:
                    return False, f"falha ao criar cliente: {exc}", {}
            else:
                cliente_criado_id = criado.get("clientId") or criado.get("id")
                if not cliente_criado_id:
                    return False, f"API nao retornou clientId: {criado}", {}
                spec = {**spec, "client_id": int(cliente_criado_id)}

    try:
        spec, variables, template_vars, faltando = enriquecer_spec_com_api(
            client, cfg, spec, usuario=usuario
        )
    except Exception as exc:
        return False, f"falha ao montar variaveis: {exc}", {}

    if faltando and not force and not dry_run:
        preview = {
            "rotulo": rotulo,
            "faltando": faltando,
            "variables": variables,
            "template_vars": template_vars,
        }
        out = salvar_saida(preview, f"faltando_{rotulo}")
        return (
            False,
            "variaveis vazias no template (ficariam vermelhas): "
            + ", ".join(faltando)
            + f" | detalhe em {out.name} | use --force para gerar mesmo assim",
            {},
        )

    payload = montar_payload(spec, cfg["empresa_id"], variables=variables)

    if dry_run:
        preview = {
            "rotulo": rotulo,
            "modo": modo,
            "api": cfg["api_url"],
            "empresa_id": cfg["empresa_id"],
            "criar_cliente": precisa_criar,
            "cliente": spec.get("cliente"),
            "template_vars": template_vars,
            "faltando": faltando,
            "variables": variables,
            "payload": payload,
        }
        out = salvar_saida(preview, rotulo)
        extra = f" | faltando={len(faltando)}" if faltando else " | vars OK"
        return True, f"dry-run -> {out.name}{extra}", {}

    try:
        if modo == "pdf":
            if not pdf_arg:
                return False, "modo pdf exige --pdf", {}
            pdf_path = Path(pdf_arg)
            if not pdf_path.is_absolute():
                pdf_path = (ROOT / pdf_arg).resolve()
            if not pdf_path.exists():
                return False, f"PDF nao encontrado: {pdf_path}", {}
            resultado = client.criar_contrato_pdf(
                nome=spec.get("nome") or f"Contrato {spec['client_id']}",
                pdf_path=pdf_path,
                payload_base=payload,
            )
        else:
            resultado = client.criar_contrato_html(payload)
    except Exception as exc:
        return False, str(exc), {}

    resultado["_meta"] = {
        "client_id": spec.get("client_id"),
        "cliente_criado": cliente_criado_id is not None,
        "cliente_criado_id": cliente_criado_id,
        "faltando": faltando,
    }
    contract_id = extrair_contrato_id(resultado)
    signing_url = extrair_link_assinatura(resultado, client=client, contract_id=contract_id)
    whatsapp_ok = False
    whatsapp_msg = ""
    if contract_id is not None:
        if not signing_url:
            signing_url = extrair_link_assinatura(None, client=client, contract_id=contract_id)
        whatsapp_ok, whatsapp_msg = tentar_enviar_contrato_whatsapp(
            client,
            cfg,
            contract_id,
        )
        resultado["_meta"]["whatsapp_enviado"] = whatsapp_ok
        resultado["_meta"]["whatsapp_detalhe"] = whatsapp_msg
        resultado["_meta"]["contrato_id"] = contract_id
        if signing_url:
            resultado["_meta"]["signing_url"] = signing_url

    out = salvar_saida(resultado, rotulo)
    parts = []
    if cliente_criado_id:
        parts.append(f"cliente={cliente_criado_id}")
    if contract_id:
        parts.append(f"contrato={contract_id}")
    if signing_url:
        parts.append("link=OK")
    if cfg.get("auto_enviar_whatsapp"):
        if whatsapp_ok:
            parts.append("whatsapp=OK")
        else:
            parts.append(f"whatsapp=FALHA({whatsapp_msg})")
    if faltando:
        parts.append(f"faltando={len(faltando)}")
    extra = (" " + " ".join(parts)) if parts else ""
    meta: dict[str, Any] = {
        "contratoId": contract_id,
        "signingUrl": signing_url,
        "clientId": spec.get("client_id"),
        "leadId": spec.get("lead_id") or spec.get("onety_lead_id"),
    }
    return True, f"OK{extra} -> {out.name}", meta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Gera contrato Contratual (Autentique) via API do Onety.",
    )
    parser.add_argument(
        "--arquivo",
        "-a",
        help="JSON de 1 contrato ou lote (lista / {contratos:[]}). Ex.: entrada/lote.json",
    )
    parser.add_argument(
        "--excel",
        "-e",
        help="Planilha .xlsx no modelo (abas padrao + contratos). Ex.: entrada/lote.xlsx",
    )
    parser.add_argument(
        "--gerar-modelo-excel",
        action="store_true",
        help="Cria entrada/modelo_contratos.xlsx para copiar e preencher.",
    )
    parser.add_argument(
        "--todos",
        action="store_true",
        help="Processa TODOS os .json em entrada/ (exceto exemplos).",
    )
    parser.add_argument(
        "--pdf",
        help="PDF local (só para 1 contrato) — POST /contratos-autentique.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Só valida e salva o payload em saida/, sem criar na Autentique.",
    )
    parser.add_argument(
        "--listar-modelos",
        action="store_true",
        help="Lista modelos (template_id) da empresa.",
    )
    parser.add_argument(
        "--listar-clientes",
        action="store_true",
        help="Lista pré-clientes (client_id) da empresa.",
    )
    parser.add_argument(
        "--listar-instancias-whatsapp",
        action="store_true",
        help="Lista instâncias Z-API (Atendimento) para send-whatsapp.",
    )
    parser.add_argument(
        "--filtro-instancia",
        default="",
        help="Filtra instâncias WhatsApp por texto (ex.: foco, comercial).",
    )
    parser.add_argument(
        "--somente-conectadas",
        action="store_true",
        help="Com --listar-instancias-whatsapp, mostra só status conectado.",
    )
    parser.add_argument(
        "--dump-instancias-json",
        action="store_true",
        help="Salva amostra das instâncias filtradas em saida/debug_instancias_whatsapp.json.",
    )
    parser.add_argument(
        "--testar-send-whatsapp",
        metavar="CONTRATO_ID",
        help="Testa POST /contratual/contratos/{id}/send-whatsapp (ex.: 8980).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Gera mesmo com variaveis do template faltando (apareceriam em vermelho).",
    )
    args = parser.parse_args(argv)

    if args.gerar_modelo_excel:
        from excel_import import gerar_modelo_excel

        destino = ENTRADA / "modelo_contratos.xlsx"
        gerar_modelo_excel(destino)
        print(f"Modelo criado: {destino}")
        print("Copie, preencha a aba padrao + contratos e rode:")
        print('  python gerar_contrato.py --excel entrada/seu_lote.xlsx --dry-run')
        return 0

    cfg = resolver_config()
    if not cfg["config_path"] and not cfg.get("token") and not (cfg["email"] and cfg["senha"]):
        print(
            "Crie config.env a partir de config.example.env e preencha EMAIL/SENHA/EMPRESA_ID.",
            file=sys.stderr,
        )
        return 1

    client = OnetyClient(
        cfg["api_url"],
        token=cfg.get("token") or None,
        empresa_id=cfg.get("empresa_id"),
    )

    try:
        usuario = autenticar(client, cfg)
    except Exception as exc:
        print(f"Falha na autenticação: {exc}", file=sys.stderr)
        return 1

    if args.listar_modelos:
        try:
            cmd_listar_modelos(client)
        except Exception as exc:
            print(f"Erro ao listar modelos: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.listar_clientes:
        try:
            cmd_listar_clientes(client, cfg["empresa_id"])
        except Exception as exc:
            print(f"Erro ao listar clientes: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.listar_instancias_whatsapp:
        try:
            cmd_listar_instancias_whatsapp(
                client,
                cfg,
                filtro_nome=args.filtro_instancia,
                somente_conectadas=args.somente_conectadas,
                dump_json=args.dump_instancias_json,
            )
        except Exception as exc:
            print(f"Erro ao listar instâncias WhatsApp: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.testar_send_whatsapp:
        try:
            cid = int(args.testar_send_whatsapp)
            instancia_id = resolver_whatsapp_instancia_id(client, cfg)
            print(f"Contrato {cid} | instancia resolvida: {instancia_id}")
            resp = client.enviar_contrato_whatsapp(cid, instancia_id=instancia_id)
            print(json.dumps(resp, ensure_ascii=False, indent=2))
        except Exception as exc:
            print(f"Falha no send-whatsapp: {exc}", file=sys.stderr)
            return 1
        return 0

    # Monta fila de jobs: (rotulo, spec, pdf_opcional)
    jobs: list[tuple[str, dict[str, Any], str | None]] = []

    if args.todos:
        arquivos = listar_arquivos_entrada()
        if not arquivos:
            print(
                "Nenhum JSON em entrada/. Copie exemplo_contrato.json ou use lote_exemplo.json.",
                file=sys.stderr,
            )
            return 1
        for arq in arquivos:
            specs = carregar_specs_de_arquivo(arq)
            if len(specs) == 1:
                jobs.append((arq.stem, specs[0], None))
            else:
                for i, spec in enumerate(specs, 1):
                    nome = spec.get("nome") or spec.get("cliente", {}).get("nome") or f"{arq.stem}_{i}"
                    jobs.append((str(nome).replace(" ", "_")[:60], spec, None))
    elif args.excel or args.arquivo:
        caminho = args.excel or args.arquivo
        arquivo = resolver_arquivo(caminho)
        if not arquivo.exists():
            print(f"Arquivo não encontrado: {arquivo}", file=sys.stderr)
            return 1
        try:
            specs = carregar_specs_de_arquivo(arquivo)
        except Exception as exc:
            print(f"Erro ao ler planilha/JSON: {exc}", file=sys.stderr)
            return 1
        for i, spec in enumerate(specs, 1):
            nome = (
                spec.get("nome")
                or (spec.get("cliente") or {}).get("nome")
                or f"{arquivo.stem}_{i}"
            )
            jobs.append(
                (
                    str(nome).replace(" ", "_")[:60],
                    spec,
                    args.pdf if len(specs) == 1 else None,
                )
            )
    else:
        arquivos = listar_arquivos_entrada()
        if not arquivos:
            print(
                "Coloque JSONs em entrada/ ou use --arquivo / --todos.\n"
                "Veja entrada/COMO_PREENCHER.txt",
                file=sys.stderr,
            )
            return 1
        if len(arquivos) > 1:
            print(
                f"Há {len(arquivos)} arquivos em entrada/. Use --todos para processar todos,\n"
                f"ou --arquivo entrada/NOME.json para um só.",
                file=sys.stderr,
            )
            for a in arquivos:
                print(f"  - {a.name}")
            return 1
        arq = arquivos[0]
        print(f"Usando: {arq.name}")
        dados = json.loads(arq.read_text(encoding="utf-8"))
        for i, spec in enumerate(extrair_specs(dados), 1):
            nome = spec.get("nome") or (spec.get("cliente") or {}).get("nome") or f"{arq.stem}_{i}"
            jobs.append((str(nome).replace(" ", "_")[:60], spec, args.pdf if i == 1 else None))

    print(f"\nFila: {len(jobs)} contrato(s){' (dry-run)' if args.dry_run else ''}\n")
    ok = 0
    falhas = 0
    for idx, (rotulo, spec, pdf_arg) in enumerate(jobs, 1):
        print(f"[{idx}/{len(jobs)}] {rotulo} ...", end=" ", flush=True)
        sucesso, msg, _meta = processar_spec(
            client,
            cfg,
            spec,
            rotulo=rotulo,
            dry_run=args.dry_run,
            pdf_arg=pdf_arg,
            usuario=usuario,
            force=args.force,
        )
        print(msg)
        if sucesso:
            ok += 1
        else:
            falhas += 1

    print(f"\nResumo: {ok} ok, {falhas} falha(s). Saídas em: {SAIDA}")
    return 0 if falhas == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
