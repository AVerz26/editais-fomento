"""Painel Streamlit dos editais de fomento com bolsas abertos.

Lê data/editais.json (atualizado pelo GitHub Actions). Se o arquivo local não
existir, busca a versão publicada no repositório.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd
import streamlit as st

REPO = "AVerz26/editais-fomento"
RAW_URL = f"https://raw.githubusercontent.com/{REPO}/main/data/editais.json"
ARQ_LOCAL = Path(__file__).parent / "data" / "editais.json"

st.set_page_config(page_title="Editais de fomento", page_icon="🎓", layout="wide")


@st.cache_data(ttl=1800, show_spinner="Carregando editais…")
def carregar() -> pd.DataFrame:
    if ARQ_LOCAL.exists():
        dados = json.loads(ARQ_LOCAL.read_text(encoding="utf-8"))
    else:
        req = Request(RAW_URL, headers={"User-Agent": "editais-app"})
        with urlopen(req, timeout=20) as r:
            dados = json.loads(r.read().decode("utf-8"))
    df = pd.DataFrame(dados)
    for col, padrao in {
        "agencia": "", "titulo": "", "url": "", "ambito": "", "prazo": None, "valor_bolsa": "",
        "resumo": "", "coletado_em": "", "setores": [], "modalidades": [], "pdfs": [],
    }.items():
        if col not in df:
            df[col] = [padrao.copy() if isinstance(padrao, list) else padrao for _ in range(len(df))]
    df["prazo"] = pd.to_datetime(df["prazo"], errors="coerce").dt.date
    hoje = date.today()
    df["dias"] = df["prazo"].map(lambda d: (d - hoje).days if pd.notna(d) and d else None)
    df["grupo"] = df["ambito"].map(
        lambda a: "Nacional / geral" if a == "Nacional"
        else "Mato Grosso" if a in ("Mato Grosso", "Estadual (MT)")
        else "Outros estados"
    )
    return df.sort_values(["prazo", "agencia"], na_position="last").reset_index(drop=True)


def opcoes(serie: pd.Series) -> list[str]:
    return sorted({x for lista in serie for x in (lista or [])})


def chip_prazo(dias) -> str:
    if dias is None or pd.isna(dias):
        return "⚪ prazo não identificado"
    dias = int(dias)
    if dias < 0:
        return "⛔ encerrado"
    if dias == 0:
        return "🔴 último dia: hoje"
    if dias <= 7:
        return f"🔴 {dias} dia(s) restante(s)"
    if dias <= 30:
        return f"🟠 {dias} dias restantes"
    return f"🟢 {dias} dias restantes"


try:
    df = carregar()
except Exception as e:  # noqa: BLE001
    st.error(f"Não consegui carregar os editais: {e}")
    st.stop()

st.title("🎓 Editais de fomento com bolsas abertos")
atualizado = max([x for x in df["coletado_em"] if x], default="")
if atualizado:
    atualizado = datetime.fromisoformat(atualizado).strftime("%d/%m/%Y")
st.caption(
    f"Fontes: CNPq, CAPES, CONFAP e fundações estaduais de amparo à pesquisa · "
    f"última coleta: {atualizado or 'ver repositório'} · dados extraídos automaticamente: "
    "**confirme sempre no edital oficial**."
)

# ------------------------------------------------------------------ filtros --
with st.sidebar:
    st.header("Filtros")
    busca = st.text_input("Buscar no texto", placeholder="ex.: mestrado, saúde, Amazônia")
    grupos = st.multiselect("Âmbito", ["Nacional / geral", "Mato Grosso", "Outros estados"],
                            default=["Nacional / geral", "Mato Grosso"])
    agencias = st.multiselect("Agência", sorted(df["agencia"].unique()))
    setores = st.multiselect("Setor de pesquisa", opcoes(df["setores"]))
    modalidades = st.multiselect("Modalidade de bolsa", opcoes(df["modalidades"]))
    max_dias = st.slider("Prazo em até (dias)", 7, 365, 365, step=7)
    so_valor = st.checkbox("Só com valor da bolsa informado")
    ordem = st.selectbox("Ordenar por", ["Prazo mais próximo", "Agência", "Título"])

f = df.copy()
if grupos:
    f = f[f["grupo"].isin(grupos)]
if agencias:
    f = f[f["agencia"].isin(agencias)]
if setores:
    f = f[f["setores"].map(lambda l: bool(set(l or []) & set(setores)))]
if modalidades:
    f = f[f["modalidades"].map(lambda l: bool(set(l or []) & set(modalidades)))]
f = f[f["dias"].isna() | (f["dias"] <= max_dias)]
if so_valor:
    f = f[f["valor_bolsa"].astype(bool)]
if busca:
    alvo = (f["titulo"] + " " + f["resumo"] + " " + f["agencia"] + " "
            + f["setores"].map(lambda l: " ".join(l or [])) + " "
            + f["modalidades"].map(lambda l: " ".join(l or [])))
    f = f[alvo.str.contains(busca, case=False, na=False, regex=False)]
if ordem == "Agência":
    f = f.sort_values(["agencia", "prazo"], na_position="last")
elif ordem == "Título":
    f = f.sort_values("titulo")

# ---------------------------------------------------------------- resumo topo --
c1, c2, c3, c4 = st.columns(4)
c1.metric("Editais encontrados", len(f))
c2.metric("Encerram em até 7 dias", int((f["dias"].fillna(999) <= 7).sum()))
c3.metric("Agências", f["agencia"].nunique())
c4.metric("Com valor informado", int(f["valor_bolsa"].astype(bool).sum()))

aba_det, aba_tab, aba_sobre = st.tabs(["📋 Detalhes de cada edital", "📊 Tabela", "ℹ️ Sobre"])

# ------------------------------------------------------------------- detalhes --
with aba_det:
    if f.empty:
        st.info("Nenhum edital com esses filtros. Tente ampliar o âmbito ou o prazo.")
    for _, r in f.iterrows():
        with st.container(border=True):
            st.markdown(f"#### [{r['titulo']}]({r['url']})")
            st.markdown(
                f"**{r['agencia']}** · {r['ambito']} · "
                f"📅 **{r['prazo'].strftime('%d/%m/%Y') if r['prazo'] else 'ver edital'}** · {chip_prazo(r['dias'])}"
            )
            a, b, c = st.columns(3)
            a.markdown("**💰 Valor da bolsa**")
            a.write(r["valor_bolsa"] or "Não informado no texto — ver edital")
            b.markdown("**🎯 Modalidades**")
            b.write(", ".join(r["modalidades"]) if r["modalidades"] else "Ver edital")
            c.markdown("**🔬 Setores de pesquisa**")
            c.write(", ".join(r["setores"]) if r["setores"] else "Geral / multidisciplinar")
            if r["resumo"]:
                with st.expander("Resumo do edital"):
                    st.write(r["resumo"])
            links = [f"[🔗 Página do edital]({r['url']})"] + [
                f"[📄 PDF {i}]({u})" for i, u in enumerate(r["pdfs"] or [], 1)
            ]
            st.markdown(" · ".join(links))

# --------------------------------------------------------------------- tabela --
with aba_tab:
    vis = pd.DataFrame({
        "Agência": f["agencia"],
        "Edital": f["titulo"],
        "Setores": f["setores"].map(lambda l: ", ".join(l or [])),
        "Modalidades": f["modalidades"].map(lambda l: ", ".join(l or [])),
        "Valor da bolsa": f["valor_bolsa"].replace("", "Ver edital"),
        "Âmbito": f["ambito"],
        "Data final": f["prazo"],
        "Dias": f["dias"],
        "Link": f["url"],
    })
    st.dataframe(
        vis, width="stretch", hide_index=True,
        column_config={
            "Link": st.column_config.LinkColumn("Link", display_text="abrir"),
            "Data final": st.column_config.DateColumn("Data final", format="DD/MM/YYYY"),
        },
    )
    st.download_button(
        "⬇️ Baixar CSV (Excel)",
        vis.to_csv(index=False, sep=";", date_format="%d/%m/%Y").encode("utf-8-sig"),
        file_name="editais_abertos.csv", mime="text/csv",
    )

# ---------------------------------------------------------------------- sobre --
with aba_sobre:
    st.markdown(
        f"""
**Como funciona:** um robô (GitHub Actions) consulta as páginas oficiais de segunda a sexta, abre cada
edital e o PDF, e mantém só os que citam *bolsa* e têm prazo futuro. O resultado fica em
[`data/editais.json`](https://github.com/{REPO}/blob/main/data/editais.json) e este painel o lê.

**Limitações:** setores, modalidades, valor e prazo vêm de heurísticas sobre o texto.
O valor mensal da bolsa muitas vezes não está no edital (ele remete a tabelas do CNPq/CAPES);
nesses casos aparece "ver edital". Valores de contrapartida e orçamento total são ignorados.

**Fontes sem resultado** (sites que bloqueiam robôs ou carregam por JavaScript) são listadas em
[`EDITAIS_ABERTOS.md`](https://github.com/{REPO}/blob/main/EDITAIS_ABERTOS.md).
"""
    )
