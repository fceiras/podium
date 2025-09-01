#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gerador de PDF de licitações (equivalente ao fluxo n8n) — Fabricio

Funcionalidades:
- Busca PNCP por período/modalidade/UF
- Normaliza + pontua (score local + recomendação)
- Descobre PDF por texto/JS e confirma por Content-Type (HEAD/GET)
- Extrai texto dos PDFs via ConvertAPI (pdf->txt) e tenta puxar objeto/valor/prazos via regex
- Fallback: extrai valor do HTML da página quando PDF não está acessível
- IA (gpt-4o-mini): resumo executivo + análise por item + destaque IA no topo
- HTML final (sempre). Opcionalmente PDF via ConvertAPI (html->pdf)

Dependências (mínimo):
  pip install requests beautifulsoup4

Variáveis de ambiente (opcional):
  OPENAI_API_KEY    (para --ai 1)
  CONVERTAPI_TOKEN  (para --pdf 1 e/ou --extract-pdf 1)
"""

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# -------------------- Constantes / util --------------------

PNCP_URL = "https://pncp.gov.br/api/consulta/v1/contratacoes/publicacao"

MODALIDADE_MAP = {
    "leilao eletronico": 1,
    "dialogo competitivo": 2,
    "concurso": 3,
    "concorrencia eletronica": 4,
    "concorrencia presencial": 5,
    "pregao eletronico": 6,
    "pregao presencial": 7,
    "dispensa": 8,
    "inexigibilidade": 9,
    "manifestacao de interesse": 10,
    "pre-qualificacao": 11,
    "credenciamento": 12,
    "leilao presencial": 13,
    "inaplicabilidade": 14,
}

PDF_CANDIDATE_TEXT = re.compile(r"(edital|anexo|itens|item|termo|refer[eê]ncia|arquivo|download)", re.I)

def nlower(s: Optional[str]) -> str:
    if s is None:
        return ""
    import unicodedata
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return s.lower()

def clamp_tamanho_pagina(v: Any) -> int:
    try:
        n = int(v)
    except Exception:
        return 50
    n = max(10, min(200, n))
    return n

def parse_flag(v: Any) -> int:
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y"}:
        return 1
    try:
        if float(s) > 0:
            return 1
    except Exception:
        pass
    return 0

def normalize_url(raw: Optional[str]) -> str:
    if not raw:
        return ""
    url = str(raw).strip().replace("&amp;", "&")
    if url.startswith("www."):
        url = "https://" + url
    if not re.match(r"^[a-z]+://", url, flags=re.I):
        url = "https://" + url
    return url

def abs_url(href: str, base: str) -> Optional[str]:
    try:
        return urljoin(base, href)
    except Exception:
        return None

def money_br_to_number(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    s = re.sub(r"[^\d,\.]", "", s).replace(".", "").replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None

def fmt_money_br(v: Optional[float]) -> str:
    if v is None:
        return "—"
    try:
        return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return "—"

def fmt_date_br(iso_like: Optional[str]) -> str:
    if not iso_like:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_like.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%d/%m/%Y às %H:%M")
    except Exception:
        return "—"

def esc_html(s: Any) -> str:
    s = "" if s is None else str(s)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

# -------------------- Padrões de extração --------------------

PDF_PATTERNS_VALOR = [
    re.compile(r"valor\s+estimado\s*[:\-]\s*R?\$?\s*([\d\.\,]+)", re.I),
    re.compile(r"valor\s+global\s*[:\-]\s*R?\$?\s*([\d\.\,]+)", re.I),
    re.compile(r"preço\s+estimado\s*[:\-]\s*R?\$?\s*([\d\.\,]+)", re.I),
]

PDF_PATTERNS_OBJETO = [
    re.compile(r"objeto\s*[:\-]\s*(.+?)(?:\n{2,}|item\s*1|\n\d+\.)", re.I | re.S),
    re.compile(r"do objeto\s*[:\-]\s*(.+?)(?:\n{2,}|item\s*1|\n\d+\.)", re.I | re.S),
]

PDF_PATTERNS_ABERTURA = [
    re.compile(r"abertura(?:\s+das\s+propostas)?\s*[:\-]\s*(.+)", re.I),
    re.compile(r"sess[aã]o\s+de\s+abertura\s*[:\-]\s*(.+)", re.I),
]
PDF_PATTERNS_ENCERRAMENTO = [
    re.compile(r"encerramento(?:\s+da\s+entrega|\s+recep[cç][aã]o)?\s*[:\-]\s*(.+)", re.I),
]

HTML_PATTERNS_VALOR = [
    re.compile(r"valor\s+(?:estimado|global|total)\s*[:\-]?\s*R?\$?\s*([\d\.\,]+)", re.I),
    re.compile(r"R\$\s*([\d\.\,]{3,})"),
]

def pick_first(text: str, patterns: List[re.Pattern]) -> Optional[str]:
    if not text:
        return None
    for pat in patterns:
        m = pat.search(text)
        if m and m.group(1):
            return m.group(1).strip()
    return None

# -------------------- Data classes --------------------

@dataclass
class Unidade:
    codigo: Optional[str] = None
    nome: Optional[str] = None
    uf: Optional[str] = None

@dataclass
class Orgao:
    cnpj: Optional[str] = None
    razaoSocial: Optional[str] = None

@dataclass
class Resultado:
    numeroControlePNCP: Optional[str] = None
    numeroCompra: Optional[str] = None
    anoCompra: Optional[int] = None
    modalidadeId: Optional[int] = None
    modalidadeNome: Optional[str] = None
    modoDisputaId: Optional[int] = None
    modoDisputaNome: Optional[str] = None
    situacaoCompraId: Optional[int] = None
    situacaoCompraNome: Optional[str] = None
    objetoCompra: Optional[str] = None
    valorEstimado: Optional[float] = None
    dataAberturaProposta: Optional[str] = None
    dataEncerramentoProposta: Optional[str] = None
    dataPublicacaoPncp: Optional[str] = None
    orgao: Orgao = field(default_factory=Orgao)
    unidade: Unidade = field(default_factory=Unidade)
    linkSistemaOrigem: Optional[str] = None
    analise: Dict[str, Any] = field(default_factory=dict)
    processo: Optional[str] = ""
    tipoJulgamento: Optional[str] = "Menor Preço (quando aplicável)"
    sistema: Optional[str] = "PNCP / Compras públicas"
    leiAplicavel: Optional[str] = "Lei 14.133/2021"
    inicioDisputa: Optional[str] = ""

# -------------------- Núcleo --------------------

def decide_score(obj: Resultado, filtros: Dict[str, Any]) -> Tuple[int, str, Optional[str]]:
    palavra = filtros.get("palavra") or ""
    uf = filtros.get("uf") or ""
    orgao_f = filtros.get("orgao") or ""
    minValor = filtros.get("minValor") or 0
    try:
        minValor = float(minValor)
    except Exception:
        minValor = 0.0

    passaPalavra = True if not palavra else (nlower(obj.objetoCompra or "").find(nlower(palavra)) >= 0)
    passaUf = True if not uf else ((obj.unidade.uf or "").upper() == str(uf).upper())
    passaOrgao = True if not orgao_f else (nlower(obj.orgao.razaoSocial or "").find(nlower(orgao_f)) >= 0)
    passaValor = True if not minValor else ((obj.valorEstimado or 0.0) >= minValor)

    score = (35 if passaPalavra else 0) + (20 if passaUf else 0) + (10 if passaOrgao else 0) + (35 if passaValor else 0)
    bloqueiaBahia = ((obj.unidade.uf or "").upper() == "BA") or ("bahia" in nlower(obj.orgao.razaoSocial or ""))
    if bloqueiaBahia:
        return score, "descartar", "Excluído: órgão/UF Bahia"
    elif score >= 70:
        return score, "prosseguir", None
    else:
        return score, "avaliar", None

def resolve_modalidade_code(modalidade: Optional[str], cod: Optional[str]) -> int:
    if cod and str(cod).strip().isdigit():
        return int(str(cod).strip())
    m = nlower(modalidade or "").strip()
    if m in MODALIDADE_MAP:
        return MODALIDADE_MAP[m]
    raise ValueError("codModalidade obrigatório (ex: 6=pregao eletronico) ou informe --modalidade 'pregao eletronico'")

def fetch_pncp(params: Dict[str, Any]) -> Dict[str, Any]:
    q = {
        "dataInicial": params["dataInicial"],
        "dataFinal": params["dataFinal"],
        "codigoModalidadeContratacao": params["codModalidade"],
        "codigoModoDisputa": params.get("modoDisputa") or None,
        "uf": params.get("uf") or None,
        "pagina": 1,
        "tamanhoPagina": params["tamanhoPagina"],
    }
    q = {k: v for k, v in q.items() if v not in (None, "", [])}
    r = requests.get(PNCP_URL, params=q, timeout=60)
    r.raise_for_status()
    data = r.json()
    arr = (data.get("_response", {}).get("data", {}).get("data")) or data.get("data") or []
    return {"raw": data, "arr": arr}

def normalize_results(arr: List[Dict[str, Any]], filtros: Dict[str, Any]) -> List[Resultado]:
    out: List[Resultado] = []
    for i in arr:
        nomeOrgao = (i.get("orgaoEntidade") or {}).get("razaoSocial") or (i.get("orgaoEntidade") or {}).get("nome") or ""
        nomeUnidade = (i.get("unidadeOrgao") or {}).get("nome") or ""
        ufItem = (i.get("unidadeOrgao") or {}).get("uf") or ""
        objeto = i.get("objetoCompra") or ""
        valorEstimado = i.get("valorEstimado")
        try:
            valorEstimado = float(valorEstimado) if valorEstimado is not None else None
        except Exception:
            valorEstimado = None

        res = Resultado(
            numeroControlePNCP=i.get("numeroControlePNCP"),
            numeroCompra=i.get("numeroCompra"),
            anoCompra=i.get("anoCompra"),
            modalidadeId=i.get("modalidadeId"),
            modalidadeNome=i.get("modalidadeNome"),
            modoDisputaId=i.get("modoDisputaId"),
            modoDisputaNome=i.get("modoDisputaNome"),
            situacaoCompraId=i.get("situacaoCompraId"),
            situacaoCompraNome=i.get("situacaoCompraNome"),
            objetoCompra=objeto,
            valorEstimado=valorEstimado,
            dataAberturaProposta=i.get("dataAberturaProposta"),
            dataEncerramentoProposta=i.get("dataEncerramentoProposta"),
            dataPublicacaoPncp=i.get("dataPublicacaoPncp"),
            orgao=Orgao(
                cnpj=(i.get("orgaoEntidade") or {}).get("cnpj"),
                razaoSocial=nomeOrgao,
            ),
            unidade=Unidade(
                codigo=(i.get("unidadeOrgao") or {}).get("codigoUnidade"),
                nome=nomeUnidade,
                uf=ufItem,
            ),
            linkSistemaOrigem=i.get("linkSistemaOrigem"),
        )
        score, recomendacao, motivo = decide_score(res, filtros)
        res.analise = {"score": score, "recomendacao": recomendacao}
        if motivo:
            res.analise["motivo"] = motivo
        out.append(res)
    return out

# -------------------- Busca de PDF / extração --------------------

def find_pdf_links(page_url: str, html: str) -> Tuple[Optional[str], Optional[str]]:
    soup = BeautifulSoup(html, "html.parser")
    hrefs = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = (a.get_text() or "").strip()
        if href.lower().endswith(".pdf") or PDF_CANDIDATE_TEXT.search(text):
            hrefs.append(href)
    # tenta data-href e window.open(...)
    for tag in soup.select("[data-href]"):
        hrefs.append(tag["data-href"])
    for m in re.finditer(r"window\.open\(['\"](.*?)['\"]", html, re.I):
        hrefs.append(m.group(1))

    # absolutiza + dedup
    base = page_url
    seen, uniq = set(), []
    for h in hrefs:
        u = abs_url(h, base)
        if u and u not in seen:
            seen.add(u)
            uniq.append(u)

    edital = next((u for u in uniq if re.search(r"edital", u, re.I)), None) or (uniq[0] if uniq else None)
    anexo = next((u for u in uniq if re.search(r"(anexo|itens|item|termo|referencia)", u, re.I) and u != edital), None)
    return edital, anexo

def ensure_pdf_url(u: Optional[str]) -> Optional[str]:
    if not u:
        return None
    try:
        hr = requests.head(u, allow_redirects=True, timeout=40)
        ct = (hr.headers.get("Content-Type") or "")
        if "application/pdf" in ct.lower():
            return hr.url
        gr = requests.get(u, stream=True, allow_redirects=True, timeout=60)
        ct = (gr.headers.get("Content-Type") or "")
        if "application/pdf" in ct.lower():
            return gr.url
    except Exception:
        pass
    return None

def fetch_text_from_pdf_via_convertapi(pdf_bytes: bytes, token: str) -> str:
    url = "https://v2.convertapi.com/convert/pdf/to/txt"
    files = {"File": ("edital.pdf", pdf_bytes, "application/pdf")}
    headers = {"Authorization": f"Bearer {token}"}
    data = {"StoreFile": "false"}
    r = requests.post(url, headers=headers, files=files, data=data, timeout=120)
    r.raise_for_status()
    return r.text

def extract_fields_from_pdf_text(edital_txt: str, anexo_txt: str) -> Dict[str, Any]:
    full = (edital_txt or "") + "\n\n" + (anexo_txt or "")
    valor_str = pick_first(full, PDF_PATTERNS_VALOR)
    valor = money_br_to_number(valor_str)
    objeto = pick_first(edital_txt, PDF_PATTERNS_OBJETO)
    abertura = pick_first(full, PDF_PATTERNS_ABERTURA)
    encerramento = pick_first(full, PDF_PATTERNS_ENCERRAMENTO)
    return {
        "objetoCompra": objeto,
        "valorEstimado": valor,
        "obsPrazosPdf": {"abertura": abertura, "encerramento": encerramento},
    }

def extract_value_from_html(html: str) -> Optional[float]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script","style"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    for pat in HTML_PATTERNS_VALOR:
        m = pat.search(text)
        if m:
            return money_br_to_number(m.group(1))
    return None

# -------------------- IA --------------------

def call_openai_summary(data_for_ai: Dict[str, Any], api_key: str) -> Dict[str, Any]:
    """
    Retorna estrutura:
    {
      "resumo": {"executivo": "..."},
      "metricas": {"total": int, "prosseguir": int, "avaliar": int, "descartar": int},
      "destaques": [{"numero":"90043/2025","orgao":"...","motivo":"..."}],
      "por_item": {
        "<chave>": {
          "titulo": "string",
          "pontos_positivos": ["..."],
          "riscos": ["..."],
          "valor_estimado_texto": "R$ ...",
          "recomendacao": "prosseguir|avaliar|descartar",
          "score_ai": 0-100
        }
      }
    }
    """
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    itens = []
    for r in data_for_ai.get("resultados", []):
        chave = r.get("numeroControlePNCP") or (f"{r.get('numeroCompra')}/{r.get('anoCompra')}" if r.get("numeroCompra") else None)
        itens.append({
            "chave": chave,
            "numeroCompra": r.get("numeroCompra"),
            "anoCompra": r.get("anoCompra"),
            "numeroControlePNCP": r.get("numeroControlePNCP"),
            "orgao": (r.get("orgao") or {}).get("razaoSocial"),
            "uf": (r.get("unidade") or {}).get("uf"),
            "objeto": r.get("objetoCompra"),
            "valorEstimado": r.get("valorEstimado"),
            "score_local": (r.get("analise") or {}).get("score"),
            "recomendacao_local": (r.get("analise") or {}).get("recomendacao"),
            "datas": {
                "abertura": r.get("dataAberturaProposta"),
                "encerramento": r.get("dataEncerramentoProposta"),
            }
        })

    system = (
        "Você é analista de licitações. Dado 'itens' e 'filtros', devolva JSON ESTRITO "
        "com {resumo:{executivo}, metricas:{total,prosseguir,avaliar,descartar}, "
        "destaques:[{numero,orgao,motivo}], por_item:{<chave>:{titulo,pontos_positivos[],riscos[],valor_estimado_texto,recomendacao,score_ai}}}. "
        "Se não souber valor, use '—'. Não invente números. Recomendações coerentes: prosseguir/avaliar/descartar."
    )

    body = {
        "model": "gpt-4o-mini",
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps({"filtros": data_for_ai.get("filtros"), "itens": itens}, ensure_ascii=False)}
        ],
        "response_format": {"type": "json_object"}
    }

    r = requests.post(url, headers=headers, json=body, timeout=120)
    r.raise_for_status()
    jr = r.json()
    content = jr["choices"][0]["message"]["content"]
    try:
        return json.loads(content)
    except Exception:
        return {"resumo": {"executivo": content}}

# -------------------- IA: casadores e fallbacks --------------------

def key_candidates_for(r: "Resultado") -> List[str]:
    keys = []
    if r.numeroControlePNCP:
        keys.append(str(r.numeroControlePNCP))
    if r.numeroCompra:
        if r.anoCompra:
            keys.append(f"{r.numeroCompra}/{r.anoCompra}")
        keys.append(str(r.numeroCompra))
    return [k for k in keys if k]

def resolve_ai_item(ai: Dict[str, Any], r: "Resultado") -> Optional[Dict[str, Any]]:
    """Tenta casar o item do relatório com a entrada correspondente em ai['por_item']."""
    if not ai:
        return None
    por_item = ai.get("por_item") or {}
    # 1) chaves diretas
    for k in key_candidates_for(r):
        if k in por_item:
            return por_item[k]
    # 2) fuzzy: contém numeroCompra
    if r.numeroCompra:
        for k, v in por_item.items():
            if str(r.numeroCompra) in str(k):
                return v
    return None

def _human_val_txt(v: Optional[float]) -> str:
    return fmt_money_br(v) if v is not None else "—"

def ensure_ai_defaults(ai: Optional[Dict[str, Any]], resultados: List["Resultado"]) -> Dict[str, Any]:
    """Garante que cada item tenha campos mínimos populados (fallback) se a IA não mandou."""
    ai = ai or {}
    ai.setdefault("por_item", {})
    por_item = ai["por_item"]

    for r in resultados:
        entry = resolve_ai_item(ai, r)
        if not entry:
            # cria entrada se não existir
            key = r.numeroControlePNCP or (f"{r.numeroCompra}/{r.anoCompra}" if r.numeroCompra else None)
            if not key:
                continue
            entry = {}
            por_item[str(key)] = entry

        entry.setdefault("titulo", (r.objetoCompra or "Licitação"))
        entry.setdefault("valor_estimado_texto", _human_val_txt(r.valorEstimado))

        # Heurísticas simples se a IA não preencheu listas
        if not entry.get("pontos_positivos"):
            obj = (r.objetoCompra or "").lower()
            pos = []
            if any(x in obj for x in ["merenda", "alimento", "escola"]):
                pos += ["Demanda recorrente (rede escolar)", "Entrega fracionada facilita logística"]
            if any(x in obj for x in ["ti", "informát", "informat"]):
                pos += ["Ampla oferta de marcas e insumos", "Distribuição simplificada"]
            if any(x in obj for x in ["obra", "engenharia", "manutenção predial"]):
                pos += ["Escopo padronizado no edital", "Cronograma definido"]
            if not pos:
                pos = ["Escopo com boa previsibilidade", "Regras objetivas no edital"]
            entry["pontos_positivos"] = pos[:3]

        if not entry.get("riscos"):
            riscos = [
                "Exigências de habilitação podem restringir competição",
                "Risco de atraso logístico/entrega",
            ]
            if (r.valorEstimado or 0) > 0:
                riscos.append("Possível glosa por sobrepreço frente ao estimado")
            entry["riscos"] = riscos[:3]

        # Herdar recomendação/score locais se IA não mandou
        if not entry.get("recomendacao"):
            entry["recomendacao"] = (r.analise or {}).get("recomendacao", "avaliar")
        if entry.get("score_ai") is None:
            entry["score_ai"] = (r.analise or {}).get("score")

    return ai

def ensure_ai_metrics(ai: Dict[str, Any], resultados: List["Resultado"]) -> Dict[str, Any]:
    """Se IA não preencheu métricas, contamos com base nas recomendações por item."""
    ai = ai or {}
    m = ai.setdefault("metricas", {})
    if m.get("total") is None:
        m["total"] = len(resultados)

    counts = {"prosseguir": 0, "avaliar": 0, "descartar": 0}
    for r in resultados:
        per = resolve_ai_item(ai, r)
        rec = (per or {}).get("recomendacao") or (r.analise or {}).get("recomendacao")
        if rec in counts:
            counts[rec] += 1
    for k, v in counts.items():
        m.setdefault(k, v)
    return ai

# -------------------- HTML --------------------

def build_html(resultados: List[Resultado], filtros: Dict[str, Any], ai: Optional[Dict[str, Any]]) -> str:
    estilo = """
    body{font-family:Arial,Helvetica,sans-serif;padding:24px;color:#111}
    h1{margin:0 0 8px 0;font-size:22px}
    h3{margin:10px 0 8px}
    .sub{color:#666;margin:0 0 24px 0}
    .card{border:1px solid #ddd;border-radius:10px;margin:18px 0;overflow:hidden}
    .hd{background:#fafafa;border-bottom:1px solid #eee;padding:16px}
    .sec{padding:16px}
    .grid{display:grid;grid-template-columns: 240px 1fr;gap:8px 16px}
    .lab{color:#555}
    .val{font-weight:600}
    ul{margin:8px 0 0 18px}
    .muted{color:#777}
    .pill{display:inline-block;padding:2px 8px;border-radius:999px;background:#eef;border:1px solid #dde;margin-left:8px;font-size:12px}
    .small{font-size:12px}
    .hr{height:1px;background:#eee;margin:14px 0}
    .featured{border-color:#4f46e5; box-shadow:0 0 0 2px rgba(79,70,229,.15)}
    .ribbon{display:inline-block;background:#4f46e5;color:#fff;border-radius:999px;padding:2px 10px;font-size:12px;margin-left:8px}
    """

    def ai_card_top(ai: Dict[str, Any]) -> str:
        if not ai:
            return ""
        resumo = ((ai.get("resumo") or {}).get("executivo")) or ""
        mets = ai.get("metricas") or {}
        destaques = ai.get("destaques") or []
        dlist = "".join(
            f"<li><b>{esc_html(d.get('numero') or '—')}</b> — {esc_html(d.get('orgao') or '—')}: {esc_html(d.get('motivo') or '')}</li>"
            for d in destaques[:5]
        )
        return f"""
        <div class="card">
          <div class="hd"><h1>Resumo executivo (IA)</h1></div>
          <div class="sec">
            <p>{esc_html(resumo) or '—'}</p>
            <div class="grid">
              <div class="lab">Total analisadas (IA):</div><div class="val">{esc_html(str(mets.get('total') or '—'))}</div>
              <div class="lab">Prosseguir:</div><div>{esc_html(str(mets.get('prosseguir') or '—'))}</div>
              <div class="lab">Avaliar:</div><div>{esc_html(str(mets.get('avaliar') or '—'))}</div>
              <div class="lab">Descartar:</div><div>{esc_html(str(mets.get('descartar') or '—'))}</div>
            </div>
            <div class="hr"></div>
            <h3>Destaques</h3>
            <ul>{dlist or '<li class="muted">—</li>'}</ul>
          </div>
        </div>
        """

    def ai_block_for_item(ai: Dict[str, Any], r: Resultado) -> str:
        if not ai:
            return ""
        per = resolve_ai_item(ai, r)
        if not per:
            return ""
        pos = "".join(f"<li>{esc_html(x)}</li>" for x in per.get("pontos_positivos") or [])
        rks = "".join(f"<li>{esc_html(x)}</li>" for x in per.get("riscos") or [])
        recomend = per.get("recomendacao") or "avaliar"
        score_ai = per.get("score_ai")
        val_txt = per.get("valor_estimado_texto") or _human_val_txt(r.valorEstimado)
        pill = f'<span class="pill">{esc_html(recomend.capitalize())}</span>'
        score_html = f"<div class='small'>Score IA: {int(score_ai)} / 100</div>" if isinstance(score_ai, (int,float)) else ""
        return f"""
        <div class="sec">
          <h3>Análise IA {pill}</h3>
          {score_html}
          <div class="grid">
            <div class="lab">Valor (IA):</div><div>{esc_html(val_txt)}</div>
            <div class="lab">Pontos positivos:</div><div><ul>{pos or '<li class="muted">—</li>'}</ul></div>
            <div class="lab">Riscos:</div><div><ul>{rks or '<li class="muted">—</li>'}</ul></div>
          </div>
        </div>
        """

    def match_ai_featured(ai: Dict[str, Any], itens: List[Resultado]) -> Optional[int]:
        """Retorna o índice do item destacado pela IA (primeiro de ai['destaques'])."""
        if not ai:
            return None
        destaques = ai.get("destaques") or []
        if not destaques:
            return None
        alvo = (destaques[0].get("numero") or "").strip()
        if not alvo:
            return None

        # 1) numeroControlePNCP exato
        for idx, r in enumerate(itens):
            key_full = r.numeroControlePNCP or (f"{r.numeroCompra}/{r.anoCompra}" if r.numeroCompra else "")
            if key_full and key_full == alvo:
                return idx
        # 2) "numeroCompra/anoCompra" exato
        for idx, r in enumerate(itens):
            key_na = f"{r.numeroCompra}/{r.anoCompra}" if r.numeroCompra else ""
            if key_na and key_na == alvo:
                return idx
        # 3) só numeroCompra (quando o destaque vier "90025")
        for idx, r in enumerate(itens):
            if r.numeroCompra and (alvo == str(r.numeroCompra) or alvo.startswith(str(r.numeroCompra))):
                return idx
        return None

    def header_card(i: Resultado) -> str:
        titulo = "Análise Detalhada da Licitação"
        numero = f"{i.numeroCompra}/{i.anoCompra or ''}".rstrip("/") if i.numeroCompra else (i.numeroControlePNCP or "—")
        valor = fmt_money_br(i.valorEstimado)
        situacao = f'<span class="pill">{esc_html(i.situacaoCompraNome)}</span>' if i.situacaoCompraNome else ""
        link = f'<div class="small"><a href="{esc_html(i.linkSistemaOrigem)}" target="_blank" rel="noopener">Sistema de origem</a></div>' if i.linkSistemaOrigem else ""
        return f"""
        <div class="hd">
          <h1>{esc_html(titulo)}</h1>
          <div class="sub">Identificação do Certame • Nº {esc_html(numero)} {situacao}</div>
          <div><b>Valor Estimado:</b> {valor}</div>
          {link}
        </div>
        """

    def bloco_ident(i: Resultado) -> str:
        return f"""
        <div class="sec">
          <div class="grid">
            <div class="lab">Modalidade:</div><div class="val">{esc_html(i.modalidadeNome or "—")}</div>
            <div class="lab">Processo:</div><div>{esc_html(i.processo or "—")}</div>
            <div class="lab">Órgão:</div><div>{esc_html(i.orgao.razaoSocial or "—")}</div>
            <div class="lab">Tipo de Julgamento:</div><div>{esc_html(i.tipoJulgamento or "Menor Preço (quando aplicável)")}</div>
            <div class="lab">Sistema:</div><div>{esc_html(i.sistema or "PNCP / Compras públicas")}</div>
            <div class="lab">Lei Aplicável:</div><div>{esc_html(i.leiAplicavel or "Lei 14.133/2021")}</div>
            <div class="lab">Modo de Disputa:</div><div>{esc_html(i.modoDisputaNome or "—")}</div>
          </div>
        </div>
        """

    def bloco_objeto(i: Resultado) -> str:
        local = f"Unidade(s) na UF {esc_html(i.unidade.uf)}" + ((" - " + esc_html(i.unidade.nome)) if i.unidade.nome else "") if i.unidade.uf else (i.unidade.nome or "—")
        prazo_vig = "—"
        prazo_sub = "—"
        return f"""
        <div class="sec">
          <h3>Objeto e Itens</h3>
          <div class="grid">
            <div class="lab">Descrição Geral:</div><div>{esc_html(i.objetoCompra or "—")}</div>
            <div class="lab">Escopo:</div>
            <div><ul class="muted"><li>—</li></ul></div>
            <div class="lab">Local de Execução:</div><div>{esc_html(local)}</div>
            <div class="lab">Prazo de Vigência:</div><div>{esc_html(prazo_vig)}</div>
            <div class="lab">Prazo para Substituição:</div><div>{esc_html(prazo_sub)}</div>
          </div>
        </div>
        """

    def bloco_requisitos() -> str:
        return """
        <div class="sec">
          <h3>Requisitos Técnicos e Comerciais</h3>
          <ul>
            <li class="muted">Atestado de capacidade técnica quando exigido no edital</li>
            <li class="muted">Comprovação de equipe mínima e atendimento a normas aplicáveis</li>
            <li class="muted">Garantia e subcontratação conforme edital e Lei 14.133/2021</li>
          </ul>
        </div>
        """

    def bloco_habilitacao() -> str:
        return """
        <div class="sec">
          <h3>Aspectos Jurídicos e de Habilitação</h3>
          <ul>
            <li class="muted">Habilitação jurídica (ato constitutivo/registro e representação)</li>
            <li class="muted">Regularidade fiscal e trabalhista (CNDs, FGTS, JT)</li>
            <li class="muted">Econômico-financeira (BP e índices) quando exigido</li>
            <li class="muted">Declarações complementares (LGPD, etc.)</li>
          </ul>
        </div>
        """

    def bloco_viabilidade() -> str:
        return """
        <div class="sec">
          <h3>Viabilidade e Riscos</h3>
          <div class="grid">
            <div class="lab">Fatores Positivos:</div>
            <div><ul class="muted"><li>Demanda contínua pode gerar receita recorrente</li><li>Escala operacional conforme escopo</li></ul></div>
            <div class="lab">Riscos:</div>
            <div><ul class="muted"><li>Dependência de mão de obra / logística</li><li>Exigências específicas do edital</li></ul></div>
          </div>
        </div>
        """

    def bloco_prazos(i: Resultado) -> str:
        return f"""
        <div class="sec">
          <h3>Prazos Relevantes</h3>
          <div class="grid">
            <div class="lab">Abertura das Propostas:</div><div>{fmt_date_br(i.dataAberturaProposta)}</div>
            <div class="lab">Encerramento Recepção:</div><div>{fmt_date_br(i.dataEncerramentoProposta)}</div>
            <div class="lab">Início da Disputa:</div><div>{fmt_date_br(i.inicioDisputa)}</div>
          </div>
        </div>
        """

    def render_card(i: Resultado, ai_block: Dict[str, Any], destacado: bool = False) -> str:
        banner = '<span class="ribbon">Destaque IA</span>' if destacado else ''
        return f"""
            <div class="card {'featured' if destacado else ''}">
              {header_card(i).replace('</h1>', f'</h1> {banner}', 1)}
              {bloco_ident(i)}
              <div class="hr"></div>
              {bloco_objeto(i)}
              <div class="hr"></div>
              {bloco_requisitos()}
              <div class="hr"></div>
              {bloco_habilitacao()}
              <div class="hr"></div>
              {bloco_viabilidade()}
              <div class="hr"></div>
              {bloco_prazos(i)}
              {ai_block_for_item(ai_block, i)}
            </div>
        """

    # Resumo IA no topo (quando houver)
    top_ai_html = ai_card_top(ai)

    # Seleciona destaque IA (primeiro da lista de destaques)
    featured_idx = match_ai_featured(ai, resultados)
    cards_html = []

    # Renderiza destaque primeiro (se houver match)
    if featured_idx is not None:
        cards_html.append(render_card(resultados[featured_idx], ai, destacado=True))

    # Renderiza os demais, pulando o que já foi destaque
    for idx, r in enumerate(resultados):
        if featured_idx is not None and idx == featured_idx:
            continue
        cards_html.append(render_card(r, ai, destacado=False))

    html = f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <title>Relatório de Licitações</title>
  <style>{estilo}</style>
</head>
<body>
  <h1>Relatório de Licitações</h1>
  {top_ai_html}
  {''.join(cards_html) if cards_html else '<p class="muted">Nenhum resultado.</p>'}
</body>
</html>
"""
    return html

# -------------------- Conversão HTML->PDF --------------------

def html_to_pdf_via_convertapi(html: str, token: str) -> bytes:
    url = "https://v2.convertapi.com/convert/html/to/pdf"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/octet-stream"}
    files = {"File": ("index.html", html.encode("utf-8"), "text/html; charset=utf-8")}
    data = {"StoreFile": "false"}
    r = requests.post(url, headers=headers, files=files, data=data, timeout=180)
    r.raise_for_status()
    return r.content

# -------------------- Main --------------------

def main():
    p = argparse.ArgumentParser(description="Gerador de PDF de licitações (PNCP)")
    p.add_argument("--api-key", default="", help="Chave de autorização local (simulação do x-api-key do n8n).")
    p.add_argument("--openai-key", default=os.getenv("OPENAI_API_KEY", ""), help="OpenAI API key (ou use env OPENAI_API_KEY)")
    p.add_argument("--convertapi-token", default=os.getenv("CONVERTAPI_TOKEN", ""), help="ConvertAPI token (ou use env CONVERTAPI_TOKEN)")
    p.add_argument("--palavra", default="")
    p.add_argument("--uf", default="")
    p.add_argument("--orgao", default="")
    p.add_argument("--min-valor", default="0")
    p.add_argument("--ai", type=int, default=0)
    p.add_argument("--pdf", type=int, default=0)
    p.add_argument("--data-inicial", default="20250713")
    p.add_argument("--data-final", default="20250812")
    p.add_argument("--modalidade", default="")
    p.add_argument("--cod-modalidade", default="")
    p.add_argument("--modo-disputa", default="")
    p.add_argument("--tamanho-pagina", type=int, default=50)
    p.add_argument("--filename", default="licitacoes.pdf")
    p.add_argument("--extract-pdf", type=int, default=1, help="Extrair campos dos PDFs via ConvertAPI (1/0). Requer CONVERTAPI_TOKEN.")
    args = p.parse_args()

    try:
        cod_modalidade = resolve_modalidade_code(args.modalidade, args.cod_modalidade)
    except Exception as e:
        print(f"[erro] {e}", file=sys.stderr)
        sys.exit(2)

    filtros = {
        "palavra": args.palavra,
        "uf": (args.uf or "").upper(),
        "orgao": args.orgao,
        "minValor": float(args.min_valor) if str(args.min_valor).strip() else 0.0,
        "codModalidade": cod_modalidade,
        "tamanhoPagina": clamp_tamanho_pagina(args.tamanho_pagina),
    }

    query = {
        "dataInicial": args.data_inicial,
        "dataFinal": args.data_final,
        "codModalidade": cod_modalidade,
        "modoDisputa": args.modo_disputa or None,
        "uf": filtros["uf"] or None,
        "tamanhoPagina": filtros["tamanhoPagina"],
    }

    print("[info] Consultando PNCP...", file=sys.stderr)
    resp = fetch_pncp(query)
    arr = resp["arr"]

    resultados = normalize_results(arr, filtros)
    # --- Filtro duro por palavra-chave (inclui sinônimos quando chave é "software") ---
    if args.palavra:
        _p = nlower(args.palavra)
        def _is_softwareish(text):
            t = nlower(text or "")
            for w in ["software","sistema","aplicativo","plataforma","licenca","licenças","ti","informatica","tecnologia da informacao","nuvem","cloud","erp","crm","banco de dados","portal","web","site","backup","seguranca da informacao"]:
                if w in t:
                    return True
            return False
        if _p in {"software","ti","informatica","tecnologia da informacao"}:
            resultados = [r for r in resultados if _is_softwareish(r.objetoCompra)]
        else:
            resultados = [r for r in resultados if _p in nlower(r.objetoCompra or "")]
    # --- Garantir ao menos 1 "prosseguir" ---
    if resultados and not any((r.analise or {}).get("recomendacao") == "prosseguir" for r in resultados):
        def _score_key(r):
            base = (r.analise or {}).get("score", 0)
            bonus = 10 if args.palavra and (nlower(args.palavra) in nlower(r.objetoCompra or "")) else 0
            return base + bonus
        _best = max(resultados, key=_score_key)
        _best.analise["recomendacao"] = "prosseguir"
        _best.analise["score"] = max(80, _best.analise.get("score", 0))

    # Enriquecimento via páginas/PDFs
    ai_payload = {"total": len(resultados), "resultados": [asdict(r) for r in resultados], "filtros": filtros}

    token = os.getenv("CONVERTAPI_TOKEN", args.convertapi_token) if args.extract_pdf else ""

    for r in resultados:
        link = normalize_url(r.linkSistemaOrigem or "")
        edital_url = None
        anexo_url = None
        page_html = ""

        if link:
            try:
                html_r = requests.get(link, timeout=40)
                if html_r.ok:
                    page_html = html_r.text
                    ed, an = find_pdf_links(link, page_html)
                    edital_url = ensure_pdf_url(ed) or ed
                    anexo_url  = ensure_pdf_url(an) or an
            except Exception as e:
                print(f"[aviso] Falha ao baixar/parsear página origem: {e}", file=sys.stderr)

        if not edital_url and link.lower().endswith(".pdf"):
            edital_url = link

        if token and (edital_url or anexo_url):
            edital_txt = ""
            anexo_txt = ""
            try:
                if edital_url:
                    pr = requests.get(edital_url, timeout=120)
                    pr.raise_for_status()
                    edital_txt = fetch_text_from_pdf_via_convertapi(pr.content, token)
            except Exception as e:
                print(f"[aviso] Falha pdf->txt (edital): {e}", file=sys.stderr)
            try:
                if anexo_url:
                    pr = requests.get(anexo_url, timeout=120)
                    pr.raise_for_status()
                    anexo_txt = fetch_text_from_pdf_via_convertapi(pr.content, token)
            except Exception as e:
                print(f"[aviso] Falha pdf->txt (anexo): {e}", file=sys.stderr)

            extracted = extract_fields_from_pdf_text(edital_txt, anexo_txt)
            if extracted.get("objetoCompra"):
                r.objetoCompra = extracted["objetoCompra"]
            if extracted.get("valorEstimado") is not None:
                r.valorEstimado = extracted["valorEstimado"]
            obs = extracted.get("obsPrazosPdf") or {}
            if obs.get("abertura") and not r.dataAberturaProposta:
                r.dataAberturaProposta = obs["abertura"]
            if obs.get("encerramento") and not r.dataEncerramentoProposta:
                r.dataEncerramentoProposta = obs["encerramento"]

        # Fallback: valor no HTML
        if r.valorEstimado is None and page_html:
            try:
                v_html = extract_value_from_html(page_html)
                if v_html is not None:
                    r.valorEstimado = v_html
            except Exception as e:
                print(f"[aviso] Falha ao extrair valor do HTML: {e}", file=sys.stderr)

        print(f"[depuracao] {r.numeroCompra}/{r.anoCompra}: link={link} edital={edital_url} anexo={anexo_url} valor={r.valorEstimado}", file=sys.stderr)

    # IA opcional
    ai_block = None
    if parse_flag(args.ai) and (args.openai_key or os.getenv("OPENAI_API_KEY")):
        key = args.openai_key or os.getenv("OPENAI_API_KEY")
        try:
            ai_block = call_openai_summary(ai_payload, key)
        except Exception as e:
            ai_block = {"resumo": {"executivo": f"Falha IA: {e}"}}

    # Preenche faltantes / garante métricas
    ai_block = ensure_ai_defaults(ai_block, resultados) if ai_block is not None else None
    ai_block = ensure_ai_metrics(ai_block, resultados) if ai_block is not None else None

    html = build_html(resultados, filtros, ai_block)

    # Sempre grava HTML e JSON
    html_path = os.path.abspath("licitacoes.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[ok] HTML salvo em: {html_path}", file=sys.stderr)

    if parse_flag(args.pdf):
        token = args.convertapi_token or os.getenv("CONVERTAPI_TOKEN")
        if not token:
            print("[erro] --pdf 1 exige CONVERTAPI_TOKEN", file=sys.stderr)
            sys.exit(3)
        print("[info] Convertendo HTML->PDF via ConvertAPI...", file=sys.stderr)
        try:
            pdf_bytes = html_to_pdf_via_convertapi(html, token)
        except Exception as e:
            print(f"[erro] Falha na conversão HTML->PDF: {e}", file=sys.stderr)
            sys.exit(4)
        out_name = args.filename or "licitacoes.pdf"
        with open(out_name, "wb") as fw:
            fw.write(pdf_bytes)
        print(f"[ok] PDF salvo em: {os.path.abspath(out_name)}", file=sys.stderr)
    else:
        print("[info] PDF não solicitado (--pdf 0).", file=sys.stderr)

    json_path = os.path.abspath("licitacoes.json")
    with open(json_path, "w", encoding="utf-8") as jf:
        json.dump({"filtros": filtros, "total": len(resultados), "resultados": [asdict(r) for r in resultados], "ai": ai_block}, jf, ensure_ascii=False, indent=2)
    print(f"[ok] JSON salvo em: {json_path}", file=sys.stderr)

if __name__ == "__main__":
    main()
