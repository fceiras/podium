#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import json
import subprocess
from datetime import date, timedelta

import streamlit as st

# tentar carregar .env se a lib existir; se não existir, passa batido
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

# ====== chaves (preferir Secrets quando hospedado) ======
OPENAI_API_KEY = st.secrets.get("OPENAI_API_KEY") if hasattr(st, "secrets") else None
if not OPENAI_API_KEY:
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

CONVERTAPI_TOKEN = st.secrets.get("CONVERTAPI_TOKEN") if hasattr(st, "secrets") else None
if not CONVERTAPI_TOKEN:
    CONVERTAPI_TOKEN = os.getenv("CONVERTAPI_TOKEN")

# ====== caminhos ======
HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT_PATH = os.path.join(HERE, "licitacoes_pdf.py")

if not os.path.exists(SCRIPT_PATH):
    st.error(f"Arquivo 'licitacoes_pdf.py' não encontrado em: {SCRIPT_PATH}")
    st.stop()

st.set_page_config(page_title="Relatório de Licitações (PNCP)", layout="wide")
st.title("Relatório de Licitações (PNCP)")

# ====== UI ======
with st.sidebar:
    st.subheader("Parâmetros de busca")

    # Palavra, UF, Órgão
    palavra = st.text_input("Palavra-chave (ex.: software)", value="")
    col_uf, col_org = st.columns([1, 2])
    with col_uf:
        uf = st.text_input("UF (opcional)", value="", max_chars=2).upper()
    with col_org:
        orgao = st.text_input("Órgão (opcional)", value="")

    # Valor mínimo (NOVO CAMPO)
    min_valor = st.number_input("Valor mínimo (R$)", min_value=0.0, step=1000.0, format="%.2f")

    # Modalidade
    modalidades = [
        "pregao eletronico",
        "pregao presencial",
        "concorrencia eletronica",
        "concorrencia presencial",
        "dispensa",
        "inexigibilidade",
        "credenciamento",
        "dialogo competitivo",
        "concurso",
        "manifestacao de interesse",
        "pre-qualificacao",
        "leilao eletronico",
        "leilao presencial",
        "inaplicabilidade",
    ]
    modalidade = st.selectbox("Modalidade", modalidades, index=0)

    # Datas (calendário)
    today = date.today()
    d_final = st.date_input("Data final", value=today)
    d_inicial = st.date_input("Data inicial", value=today - timedelta(days=30))
    if d_inicial > d_final:
        st.warning("A data inicial não pode ser maior que a data final.")

    # Demais opções
    tamanho_pagina = st.slider("Tamanho da página (resultados PNCP)", min_value=10, max_value=200, value=50, step=10)
    use_ai = st.checkbox("Analise por IA", value=True)
    gen_pdf = st.checkbox("Gerar PDF", value=False)
    show_inline = st.checkbox("Exibir HTML dentro do app", value=True)
    filename = st.text_input("Nome do PDF (quando marcado)", value="licitacoes.pdf")

    run_btn = st.button("Gerar relatório")

st.divider()

# ====== Execução ======
def yyyymmdd(d: date) -> str:
    return d.strftime("%Y%m%d")

if run_btn:
    # Monta comando
    args = [
        sys.executable,
        SCRIPT_PATH,
        "--modalidade", modalidade,
        "--data-inicial", yyyymmdd(d_inicial),
        "--data-final", yyyymmdd(d_final),
        "--tamanho-pagina", str(tamanho_pagina),
        "--ai", "1" if use_ai else "0",
        "--pdf", "1" if gen_pdf else "0",
        "--filename", filename,
    ]
    if palavra:
        args += ["--palavra", palavra]
    if uf:
        args += ["--uf", uf]
    if orgao:
        args += ["--orgao", orgao]
    # >>> NOVO: passa valor mínimo <<<
    if min_valor and float(min_valor) > 0:
        args += ["--min-valor", f"{float(min_valor):.2f}"]

    # Ambiente com as chaves (sem exibir)
    env = os.environ.copy()
    if OPENAI_API_KEY:
        env["OPENAI_API_KEY"] = OPENAI_API_KEY
    if CONVERTAPI_TOKEN:
        env["CONVERTAPI_TOKEN"] = CONVERTAPI_TOKEN

    # Mostra um resumo (sem chaves)
    with st.expander("Comando", expanded=False):
        safe_cmd = " ".join([a if a != SCRIPT_PATH else "licitacoes_pdf.py" for a in args])
        st.code(safe_cmd)

    # Chama o script
    with st.status("Rodando consulta e montando relatório...", expanded=True) as status:
        try:
            proc = subprocess.run(args, env=env, capture_output=True, text=True, cwd=HERE, timeout=600)
        except Exception as e:
            st.error(f"Falha ao executar o script: {e}")
            st.stop()

        st.write("**STDOUT:**")
        st.code(proc.stdout or "(vazio)")
        st.write("**STDERR (logs):**")
        st.code(proc.stderr or "(vazio)")

        if proc.returncode != 0:
            st.error(f"Script terminou com código {proc.returncode}. Veja os logs acima.")
            st.stop()
        status.update(label="Relatório gerado.", state="complete")

    # Saídas
    html_path = os.path.join(HERE, "licitacoes.html")
    json_path = os.path.join(HERE, "licitacoes.json")
    pdf_path = os.path.join(HERE, filename)

    cols = st.columns(3)
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as fh:
            html_content = fh.read()

        with cols[0]:
            st.download_button("Baixar HTML", data=html_content, file_name="licitacoes.html", mime="text/html")

        if show_inline:
            st.subheader("Visualização do Relatório (HTML)")
            st.components.v1.html(html_content, height=900, scrolling=True)

    if os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as fj:
            json_content = fj.read()
        with cols[1]:
            st.download_button("Baixar JSON", data=json_content, file_name="licitacoes.json", mime="application/json")

    if gen_pdf and os.path.exists(pdf_path):
        with open(pdf_path, "rb") as fp:
            pdf_bytes = fp.read()
        with cols[2]:
            st.download_button("Baixar PDF", data=pdf_bytes, file_name=filename, mime="application/pdf")

# Rodapé discreto
st.caption("PNCP • Geração de Relatório • Streamlit")
