import os
import sys
import subprocess
import tempfile
import base64
from pathlib import Path
from datetime import date
from dotenv import load_dotenv
import streamlit as st
from streamlit import components

# Carrega variáveis do .env se existir (NÃO exponho chaves na UI)
# .env deve conter:
# OPENAI_API_KEY=sk-...
# CONVERTAPI_TOKEN=xxxxxxxx
load_dotenv()

# Caminho absoluto do script principal
SCRIPT = (Path(__file__).parent / "licitacoes_pdf.py").resolve()

st.set_page_config(page_title="Relatório de Licitações", page_icon="📄", layout="centered")
st.title("Relatório de Licitações (PNCP)")

with st.sidebar:
    st.header("Parâmetros")


    MODALIDADES = [
        "pregao eletronico",
        "pregao presencial",
        "concorrencia eletronica",
        "concorrencia presencial",
        "dispensa",
        "inexigibilidade",
        "dialogo competitivo",
        "concurso",
        "leilao eletronico",
        "leilao presencial",
        "manifestacao de interesse",
        "pre-qualificacao",
        "credenciamento",
        "inaplicabilidade",
    ]
    modalidade = st.selectbox("Modalidade", MODALIDADES, index=0)

    palavra = st.text_input("Palavra-chave (opcional)", value="")
    uf = st.text_input("UF (opcional, ex: SP)", value="")

    # Calendário
    col1, col2 = st.columns(2)
    with col1:
        d_ini = st.date_input("Data inicial", value=date(2025, 7, 13), format="DD/MM/YYYY")
    with col2:
        d_fim = st.date_input("Data final", value=date(2025, 8, 12), format="DD/MM/YYYY")

    def to_yyyymmdd(d: date) -> str:
        return f"{d.year:04d}{d.month:02d}{d.day:02d}"

    data_inicial = to_yyyymmdd(d_ini)
    data_final = to_yyyymmdd(d_fim)

    tamanho_pagina = st.number_input("Tamanho página (10-200)", min_value=10, max_value=200, value=20, step=10)
    ai = st.checkbox("Usar IA (OpenAI)", value=False)
    pdf = st.checkbox("Gerar PDF (ConvertAPI)", value=False)

# Chaves SOMENTE por ambiente/.env
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
CONVERTAPI_TOKEN = os.getenv("CONVERTAPI_TOKEN", "")

# Alertas de sanidade
if ai and not OPENAI_API_KEY:
    st.warning("IA marcada, mas OPENAI_API_KEY não está setada no ambiente (.env/variável).")
if pdf and not CONVERTAPI_TOKEN:
    st.warning("PDF marcado, mas CONVERTAPI_TOKEN não está setado no ambiente (.env/variável).")

run = st.button("Gerar relatório")

if run:
    with st.spinner("Executando..."):
        # Diretório temporário para salvar saídas
        workdir = tempfile.mkdtemp()
        out_pdf = Path(workdir) / "licitacoes.pdf"
        out_html = Path(workdir) / "licitacoes.html"
        out_json = Path(workdir) / "licitacoes.json"

        # Ambiente do subprocesso (já inclui variáveis carregadas pelo dotenv)
        env = os.environ.copy()

        # Comando usando o MESMO Python do Streamlit, caminho do script absoluto
        cmd = [
            sys.executable,
            str(SCRIPT),
            "--modalidade", modalidade,
            "--data-inicial", data_inicial,
            "--data-final", data_final,
            "--tamanho-pagina", str(tamanho_pagina),
            "--filename", str(out_pdf.name),
        ]
        if palavra:
            cmd += ["--palavra", palavra]
        if uf:
            cmd += ["--uf", uf]
        if ai:
            cmd += ["--ai", "1"]
        if pdf:
            cmd += ["--pdf", "1"]

        proc = subprocess.run(
            cmd, cwd=workdir, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )

        st.subheader("Logs")
        st.code(proc.stderr or "(sem logs)")

        # Downloads
        if out_html.exists():
            st.download_button("Baixar HTML", data=out_html.read_bytes(), file_name=out_html.name, mime="text/html")
        if pdf and out_pdf.exists():
            st.download_button("Baixar PDF", data=out_pdf.read_bytes(), file_name=out_pdf.name, mime="application/pdf")
        if out_json.exists():
            st.download_button("Baixar JSON", data=out_json.read_bytes(), file_name=out_json.name, mime="application/json")

        # Visualização: embed e nova aba
        if out_html.exists():
            html_bytes = out_html.read_bytes()
            html_str = html_bytes.decode("utf-8", errors="ignore")

            st.markdown("### Visualizar o relatório")

            # 1) Ver na mesma página (embed)
            with st.expander("Ver aqui na página (embed)"):
                components.v1.html(html_str, height=900, scrolling=True)

            # 2) Abrir em nova aba via Data URI
            data_uri = "data:text/html;base64," + base64.b64encode(html_bytes).decode("utf-8")
            st.markdown(
                f'<a href="{data_uri}" target="_blank" rel="noopener">Abrir relatório em nova aba</a>',
                unsafe_allow_html=True,
            )
