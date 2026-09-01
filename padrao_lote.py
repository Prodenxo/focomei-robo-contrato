#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Expande JSON mínimo (vindo do GPT) aplicando o padrão FOCO MEI."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any
import json
import re


PLANOS_POR_LICENCAS: dict[int, tuple[str, str]] = {
    5: ("Start", "Pacote Start — até 5 CNPJs MEI"),
    20: ("Growth", "Pacote Growth — até 20 CNPJs MEI"),
    50: ("Scale", "Pacote Scale — até 50 CNPJs MEI"),
    100: ("Max", "Pacote Max — até 100 CNPJs MEI"),
}


def resolver_plano_por_licencas(qtd: str | int, padrao: dict[str, Any]) -> tuple[str, str]:
    """Nome do plano + descrição do produto conforme quantidade comprada."""
    try:
        n = int(str(qtd).strip())
    except (TypeError, ValueError):
        n = 0
    if n in PLANOS_POR_LICENCAS:
        return PLANOS_POR_LICENCAS[n]
    fallback = (
        (padrao.get("custom") or {}).get("plano_contratado")
        or padrao.get("plano_contratado")
        or "Start"
    )
    prod = (padrao.get("produtos_dados") or [{}])[0]
    desc = prod.get("descricao") or f"Pacote FocoMEI — até {qtd} CNPJs MEI"
    return str(fallback), str(desc)


def limpar_email(valor: Any) -> str:
    """Aceita e-mail puro ou markdown [x](mailto:x) / <mailto:x>."""
    s = str(valor or "").strip()
    if not s or s.lower() in ("null", "none"):
        return ""
    m = re.search(r"mailto:([^\s\)\>]+)", s, re.I)
    if m:
        return m.group(1).strip().strip('"').strip("'")
    m = re.search(r"\[([^\]]+@[^\]]+)\]", s)
    if m:
        return m.group(1).strip()
    m = re.search(r"[\w.\-+]+@[\w.\-]+\.\w+", s)
    if m:
        return m.group(0).strip()
    return s


def _fmt_brl(value: float | int | str) -> str:
    try:
        return f"{float(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return str(value or "")


def carregar_padrao(entrada: Path, nome: str = "_padrao_focomei.json") -> dict[str, Any]:
    path = entrada / nome
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _add_months(d: date, months: int) -> date:
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    day = min(d.day, 28)
    return date(y, m, day)


def _eh_formato_minimo(spec: dict[str, Any]) -> bool:
    """GPT manda razao_social / signatario_* no topo; JSON completo já tem signatories."""
    if isinstance(spec.get("signatories"), list) and spec.get("template_id"):
        return False
    return bool(
        spec.get("razao_social")
        or spec.get("signatario_nome")
        or spec.get("signatario_cpf")
        or (isinstance(spec.get("cliente"), dict) and not spec.get("template_id"))
    )


def expandir_com_padrao(spec: dict[str, Any], padrao: dict[str, Any]) -> dict[str, Any]:
    """Converte item mínimo (ou parcial) no spec completo do robô."""
    if not padrao:
        return spec

    # Já é contrato completo: só completa campos faltando do padrão
    if not _eh_formato_minimo(spec):
        out = dict(spec)
        for k in ("modo", "template_id", "financeiro"):
            if out.get(k) in (None, "", []) and padrao.get(k) not in (None, ""):
                out[k] = padrao[k]
        if not out.get("produtos_dados") and padrao.get("produtos_dados"):
            out["produtos_dados"] = padrao["produtos_dados"]
        # garante signatário contratada
        signs = list(out.get("signatories") or [])
        contr = padrao.get("contratada") or {}
        if contr.get("name") and contr.get("email"):
            ja_tem = any(
                (s.get("funcao_assinatura") or "").lower().find("contratada") >= 0
                for s in signs
            )
            if not ja_tem:
                signs.append(dict(contr))
                out["signatories"] = signs
        lead_id = spec.get("onety_lead_id") or spec.get("lead_id")
        if lead_id not in (None, ""):
            out["lead_id"] = int(lead_id)
        return out

    cli_in = dict(spec.get("cliente") or {})
    razao = (
        spec.get("razao_social")
        or cli_in.get("nome")
        or cli_in.get("razao_social")
        or ""
    )
    email = limpar_email(spec.get("email") or cli_in.get("email") or "")
    telefone = spec.get("telefone") or cli_in.get("telefone") or ""
    cpf_cnpj = (
        spec.get("cpf_cnpj")
        or cli_in.get("cpf_cnpj")
        or cli_in.get("cnpj")
        or cli_in.get("cpf")
        or ""
    )
    tipo = (spec.get("tipo_cliente") or cli_in.get("tipo") or "empresa").strip().lower()
    if tipo in ("pj", "pessoa_juridica", "juridica"):
        tipo = "empresa"
    if tipo in ("pf", "fisica"):
        tipo = "pessoa_fisica"

    valor_mensal = spec.get("valor_mensal")
    if valor_mensal is None:
        valor_mensal = padrao.get("valor_mensal", 100)
    valor_mensal = float(valor_mensal)
    parcelas = int(spec.get("parcelas") or padrao.get("parcelas") or 12)
    valor_total = spec.get("valor_total")
    if valor_total is None:
        valor_total = valor_mensal * parcelas
    else:
        valor_total = float(valor_total)

    hoje = date.today()
    expires_dias = int(padrao.get("expires_dias") or 2)
    meses_vig = int(padrao.get("meses_vigencia") or parcelas)
    expires_at = spec.get("expires_at") or (hoje + timedelta(days=expires_dias)).isoformat()
    start_at = spec.get("start_at") or hoje.isoformat()
    end_at = spec.get("end_at") or _add_months(hoje, meses_vig).isoformat()

    qtd = str(
        spec.get("quantidade_licencas")
        or (padrao.get("custom") or {}).get("quantidade_de_cnpjs")
        or padrao.get("quantidade_licencas_padrao")
        or "5"
    )
    plano, descricao_produto = resolver_plano_por_licencas(qtd, padrao)
    if spec.get("plano_contratado"):
        plano = str(spec.get("plano_contratado"))
    meses_promo = str(
        spec.get("meses_promocionais")
        if spec.get("meses_promocionais") is not None
        else (padrao.get("custom") or {}).get("meses_promocionais")
        or padrao.get("meses_promocionais")
        or "0"
    )

    custom = {
        "plano_contratado": plano,
        "quantidade_de_cnpjs": qtd,
        "meses_promocionais": meses_promo,
        "mensalidade": _fmt_brl(valor_mensal),
    }
    # mantém outros custom do padrão, se existirem
    for k, v in (padrao.get("custom") or {}).items():
        custom.setdefault(k, v)

    sig_nome = spec.get("signatario_nome") or razao
    sig_email = limpar_email(spec.get("signatario_email") or email)
    sig_tel = spec.get("signatario_telefone") or telefone
    sig_cpf = spec.get("signatario_cpf") or ""
    sig_funcao = (
        spec.get("signatario_funcao")
        or padrao.get("signatario_funcao_padrao")
        or "Assinar como contratante"
    )

    signatories = [
        {
            "name": sig_nome,
            "email": sig_email,
            "cpf": sig_cpf,
            "telefone": sig_tel,
            "funcao_assinatura": sig_funcao,
        }
    ]
    contr = padrao.get("contratada") or {}
    if contr.get("name") and contr.get("email"):
        signatories.append(dict(contr))

    produto_base = dict((padrao.get("produtos_dados") or [{}])[0])
    produto_base.update(
        {
            "nome": plano,
            "descricao": descricao_produto,
            "quantidade": 1,
            "valor_de_venda": valor_mensal,
            "valor_total": valor_total,
            "parcelas": parcelas,
        }
    )

    nome = spec.get("nome") or razao.replace(" ", "_")[:60] or "Contrato"

    out: dict[str, Any] = {
        "nome": nome,
        "modo": padrao.get("modo") or "html",
        "template_id": spec.get("template_id") or padrao.get("template_id"),
        "criar_cliente": True if not spec.get("client_id") else False,
        "expires_at": expires_at,
        "start_at": start_at,
        "end_at": end_at,
        "valor": valor_total,
        "valor_recorrente": valor_mensal,
        "cliente": {
            "tipo": tipo,
            "nome": razao,
            "email": email,
            "cpf_cnpj": cpf_cnpj,
            "telefone": telefone,
            "endereco": spec.get("endereco") or cli_in.get("endereco") or "",
            "numero": spec.get("numero") or cli_in.get("numero") or "",
            "complemento": spec.get("complemento") or cli_in.get("complemento") or "",
            "bairro": spec.get("bairro") or cli_in.get("bairro") or "",
            "cidade": spec.get("cidade") or cli_in.get("cidade") or "",
            "estado": spec.get("estado")
            or spec.get("uf")
            or cli_in.get("estado")
            or cli_in.get("uf")
            or "",
            "cep": spec.get("cep") or cli_in.get("cep") or "",
        },
        "signatories": signatories,
        "produtos_dados": [produto_base],
        "custom": custom,
        "financeiro": dict(padrao.get("financeiro") or {}),
    }
    # remove chave de nota do financeiro se houver
    out["financeiro"].pop("_nota", None)
    if spec.get("client_id"):
        out["client_id"] = int(spec["client_id"])
        out.pop("criar_cliente", None)
    lead_id = spec.get("onety_lead_id") or spec.get("lead_id")
    if lead_id not in (None, ""):
        out["lead_id"] = int(lead_id)
    return out


def expandir_lista(specs: list[dict[str, Any]], padrao: dict[str, Any]) -> list[dict[str, Any]]:
    return [expandir_com_padrao(s, padrao) for s in specs if isinstance(s, dict)]
