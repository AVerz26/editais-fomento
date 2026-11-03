#!/usr/bin/env python3
"""Coleta editais de fomento com bolsas abertos (Brasil / Mato Grosso).

Fontes: CNPq, CAPES (abrangência nacional) e FAPEMAT (Mato Grosso).
Saída: EDITAIS_ABERTOS.md, data/editais.csv e data/editais.json.
"""
from __future__ import annotations

import csv
import logging
import io
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

try:
    from pypdf import PdfReader
except ImportError:  # PDF é opcional: sem ele o valor da bolsa pode ficar "ver edital"
    PdfReader = None

logging.getLogger("pypdf").setLevel(logging.CRITICAL)
ROOT = Path(__file__).resolve().parent.parent
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; editais-fomento-bot/1.0)"}
TIMEOUT = 40
MAX_PDF_BYTES = 15 * 1024 * 1024
MAX_PDF_PAGES = 40
BRT = timezone(timedelta(hours=-3))
HOJE = datetime.now(BRT).date()

MESES = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4, "maio": 5,
    "junho": 6, "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10,
    "novembro": 11, "dezembro": 12,
}

# palavra-chave -> setor de pesquisa
SETORES = {
    "Saúde": r"sa[úu]de|sus\b|medic|cl[íi]nic|epidemi|farm[áa]c|odont|enfermag",
    "Agrárias / Agronegócio": r"agr[ií]col|agropecu|agro|pecu[áa]ri|rural|florest|safra|solo\b",
    "Engenharias / Tecnologia": r"engenhar|tecnolog|digital|intelig[êe]ncia artificial|computa|software|semicondutor",
    "Inovação / Empresas": r"inova[çc][ãa]o tecnol|startup|empreend|pme|setor produtivo",
    "Energia / Petróleo e Gás": r"energia|petr[óo]leo|petrobras|g[áa]s natural|renov[áa]ve",
    "Meio ambiente / Clima": r"ambient|clim[áa]tic|biodiversidade|sustent|amaz[ôo]nia|bioma|pantanal|cerrado",
    "Educação": r"educa[çc][ãa]o|ensino|professor|licenciatura|docente|escola",
    "Ciências Humanas e Sociais": r"humanas|sociais|cultura|hist[óo]ri|ind[íi]gena|g[êe]nero|negr[ao]s?\b|mulher",
    "Ciências Exatas e da Terra": r"matem[áa]tica|f[íi]sica|qu[íi]mica|geoci[êe]n|astronomia",
    "Ciências Biológicas": r"biolog|gen[ôo]mica|biotecnolog|zoologia|bot[âa]nica",
}


@dataclass
class Edital:
    agencia: str
    titulo: str
    url: str
    ambito: str  # "Nacional" | "Mato Grosso"
    prazo: date | None = None
    prazo_texto: str = ""
    valor_bolsa: str = ""
    setores: list[str] = field(default_factory=list)
    tem_bolsa: bool = False


def get(url: str, **kw) -> requests.Response:
    """GET educado: pausa entre chamadas e novas tentativas em 429/5xx/timeout."""
    for tentativa in range(4):
        time.sleep(0.8)
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kw)
            if r.status_code in (429, 502, 503, 504) and tentativa < 3:
                time.sleep(5 * (tentativa + 1))
                continue
            r.raise_for_status()
            return r
        except (requests.Timeout, requests.ConnectionError):
            if tentativa == 3:
                raise
            time.sleep(5 * (tentativa + 1))
    raise RuntimeError("inalcançável")


def soup_of(url: str) -> BeautifulSoup:
    r = get(url)
    return BeautifulSoup(r.content, "html.parser")


def texto_principal(soup: BeautifulSoup) -> str:
    """Texto do conteúdo da página, sem menus/rodapé (evita falsos positivos)."""
    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()
    for sel in ("#content-core", "#parent-fieldname-text", "article", "main", "#content", ".journal-content-article"):
        el = soup.select_one(sel)
        if el and len(el.get_text(strip=True)) > 200:
            return clean(el.get_text(" "))
    return clean((soup.body or soup).get_text(" "))


def clean(t: str) -> str:
    return re.sub(r"\s+", " ", t or "").strip()


# ----------------------------------------------------------------- extração --
def parse_data(txt: str) -> date | None:
    try:
        d, m, y = txt.split("/")
        return date(int(y), int(m), int(d))
    except ValueError:
        return None


def achar_prazo(texto: str) -> date | None:
    """Prazo final de envio: data final do intervalo de inscrição, ou 'até <data>'."""
    m = re.search(
        r"Inscri[çc][õo]es?:?\s*(\d{2}/\d{2}/\d{4})\s*(?:a|à|até|-)\s*(\d{2}/\d{2}/\d{4})", texto
    )
    if m:
        return parse_data(m.group(2))
    cands: list[date] = []
    ctx = r"(?:inscri|submiss|envio|prazo|propostas?|candidatur)[^.]{0,120}?"
    for m in re.finditer(ctx + r"(?:at[ée]|encerr\w+|limite)[^0-9]{0,30}(\d{1,2}/\d{1,2}/\d{4})", texto, re.I):
        d = parse_data(m.group(1))
        if d:
            cands.append(d)
    for m in re.finditer(
        ctx + r"(?:at[ée]|encerr\w+|limite)[^0-9]{0,30}(\d{1,2})\s+de\s+([a-zçã]+)(?:\s+de\s+(\d{4}))?", texto, re.I
    ):
        mes = MESES.get(m.group(2).lower())
        if mes:
            ano = int(m.group(3)) if m.group(3) else HOJE.year
            try:
                cands.append(date(ano, mes, int(m.group(1))))
            except ValueError:
                pass
    return max(cands) if cands else None


def achar_valores(texto: str) -> str:
    """Valores mensais de bolsa (R$) citados perto de 'bolsa'; ignora orçamentos totais."""
    achados: list[str] = []
    for m in re.finditer(r"R\$\s?([\d.]+)(?:,(\d{2}))?", texto):
        valor = float(m.group(1).replace(".", "") + "." + (m.group(2) or "00"))
        if not 300 <= valor <= 12000:
            continue
        janela = texto[max(0, m.start() - 100): m.end() + 100].lower()
        if "bolsa" in janela and not re.search(r"or[çc]amento|recursos? (?:total|financeiro)|global|investimento", janela):
            v = f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
            if v not in achados:
                achados.append(v)
    return "; ".join(achados[:4])


def achar_setores(texto: str) -> list[str]:
    t = texto.lower()
    pontos = {s: len(re.findall(p, t)) for s, p in SETORES.items()}
    top = [s for s, n in sorted(pontos.items(), key=lambda x: -x[1]) if n > 0]
    return top[:3]


def texto_pdf(url: str) -> str:
    if PdfReader is None:
        return ""
    try:
        r = get(url, stream=True)
        if int(r.headers.get("Content-Length", 0) or 0) > MAX_PDF_BYTES:
            return ""
        data = r.content[: MAX_PDF_BYTES + 1]
        if len(data) > MAX_PDF_BYTES or not data.startswith(b"%PDF"):
            return ""
        reader = PdfReader(io.BytesIO(data))
        return " ".join((p.extract_text() or "") for p in reader.pages[:MAX_PDF_PAGES])
    except Exception as e:  # PDF quebrado/escaneado não deve derrubar a coleta
        print(f"  aviso: PDF {url}: {e}", file=sys.stderr)
        return ""


def completar(ed: Edital, texto_pagina: str, pdfs: list[str]) -> None:
    """Preenche setores / valor / bolsa lendo a página e, se preciso, os PDFs."""
    texto = texto_pagina
    for u in pdfs[:2]:
        texto += " " + texto_pdf(u)
    texto = clean(texto)
    ed.tem_bolsa = bool(re.search(r"\bbolsas?\b", texto, re.I))
    ed.valor_bolsa = achar_valores(texto) or ""
    ed.setores = achar_setores(ed.titulo + " " + texto_pagina[:2500])
    if ed.prazo is None:
        ed.prazo = achar_prazo(texto)


# ------------------------------------------------------------------- fontes --
def coletar_cnpq() -> list[Edital]:
    base = "https://www.gov.br/cnpq/pt-br/chamadas/abertas-para-submissao"
    soup = soup_of(base)
    out: list[Edital] = []
    for a in soup.select("a.summary.url"):
        # sobe na árvore até o contêiner que contém "Inscrições" sem engolir outra chamada
        bloco = a
        while bloco.parent is not None and not re.search(r"Inscri[çc][õo]es", bloco.get_text(" ")):
            if len(bloco.parent.select("a.summary.url")) > 1:
                break
            bloco = bloco.parent
        texto = clean(bloco.get_text(" "))
        ed = Edital("CNPq", clean(a.get_text()), a["href"], "Nacional")
        m = re.search(r"Inscri[çc][õo]es?:?\s*([\d/]+\s*(?:a|à|até)\s*[\d/]+)", texto)
        ed.prazo_texto = m.group(1) if m else ""
        ed.prazo = achar_prazo(texto)
        pdfs = [urljoin(base, x["href"]) for x in bloco.find_all("a", href=True)
                if re.search(r"\.pdf|/documents/|@@download|/file", x["href"], re.I)
                and re.search(r"chamada|edital", x.get_text(), re.I)]
        try:
            pag = texto_principal(soup_of(ed.url))
        except Exception as e:
            print(f"  aviso: {ed.url}: {e}", file=sys.stderr)
            pag = ""
        completar(ed, texto + " " + pag, pdfs)
        out.append(ed)
    return out


def coletar_capes() -> list[Edital]:
    base = "https://www.gov.br/capes/pt-br/assuntos/editais-e-resultados-capes"
    soup = soup_of(base)
    vistos: set[str] = set()
    out: list[Edital] = []
    for a in soup.select("#content-core a[href], #parent-fieldname-text a[href], main a[href]"):
        titulo = clean(a.get_text())
        href = urljoin(base, a["href"])
        if href in vistos or len(titulo) < 12:
            continue
        if not re.search(r"edital|programa|chamada|capes/pt-br/(acesso-a-informacao/acoes-e-programas|centrais-de-conteudo/editais)", href + titulo, re.I):
            continue
        if re.search(r"resultado|retifica|altera[çc][ãa]o|errata", titulo, re.I):
            continue
        vistos.add(href)
        ed = Edital("CAPES", titulo, href, "Nacional")
        try:
            if href.lower().endswith((".pdf", "/file")) or "@@display-file" in href:
                texto, pdfs = "", [href]
            else:
                s = soup_of(href)
                pdfs = [urljoin(href, x["href"]) for x in s.find_all("a", href=True)
                        if re.search(r"edital.*\.pdf|@@display-file", x["href"], re.I)]
                texto = texto_principal(s)
        except Exception as e:
            print(f"  aviso: {href}: {e}", file=sys.stderr)
            continue
        completar(ed, texto, pdfs)
        if ed.prazo:  # CAPES lista programas permanentes: só entra o que tem prazo
            out.append(ed)
    return out


def coletar_fapemat() -> list[Edital]:
    base = "https://www.fapemat.mt.gov.br/pt/aberto_old"
    soup = soup_of(base)
    vistos: set[str] = set()
    out: list[Edital] = []
    for a in soup.select('a[href*="/pt/-/"]'):
        href = urljoin(base, a["href"])
        titulo = clean(a.get_text())
        if href in vistos or len(titulo) < 10:
            continue
        vistos.add(href)
        ed = Edital("FAPEMAT", titulo, href, "Mato Grosso")
        try:
            s = soup_of(href)
            pdfs = [urljoin(href, x["href"]) for x in s.find_all("a", href=True)
                    if re.search(r"\.pdf|/documents/", x["href"], re.I)]
            texto = texto_principal(s)
        except Exception as e:
            print(f"  aviso: {href}: {e}", file=sys.stderr)
            continue
        completar(ed, texto, pdfs)
        out.append(ed)
    return out


# Fundações estaduais e outras agências: páginas de listagem de editais abertos.
# (agência, âmbito, [páginas de listagem]). Adicione novas fontes aqui.
GENERICAS: list[tuple[str, str, list[str]]] = [
    ("FAPESP", "Estadual (SP)", ["https://fapesp.br/oportunidades/", "https://fapesp.br/bolsas/"]),
    ("FAPERJ", "Estadual (RJ)", ["https://www.faperj.br/?tpl=editais"]),
    ("FAPEMIG", "Estadual (MG)", ["https://fapemig.br/pt/chamadas/", "https://fapemig.br/pt/chamadas-abertas/"]),
    ("FAPESB", "Estadual (BA)", ["https://www.fapesb.ba.gov.br/editais/"]),
    ("FAPEAM", "Estadual (AM)", ["https://www.fapeam.am.gov.br/editais/"]),
    ("FAPDF", "Estadual (DF)", ["https://fap.df.gov.br/editais/"]),
    ("FAPEG", "Estadual (GO)", ["https://goias.gov.br/fapeg/editais/"]),
    ("FAPESC", "Estadual (SC)", ["https://fapesc.sc.gov.br/editais/"]),
    ("Fundação Araucária", "Estadual (PR)", ["https://www.fappr.pr.gov.br/Pagina/Editais-e-Chamadas-Abertos"]),
    ("FAPERGS", "Estadual (RS)", ["https://fapergs.rs.gov.br/abertos"]),
    ("FACEPE", "Estadual (PE)", ["https://www2.facepe.br/editais/todos/"]),
    ("FUNCAP", "Estadual (CE)", ["https://www.ce.gov.br/funcap/category/editais/"]),
    ("FAPEAL", "Estadual (AL)", ["https://www.fapeal.br/pesquisador/editais/"]),
    ("FAPESQ", "Estadual (PB)", ["https://fapesq.rpp.br/editais/"]),
    ("FAPEMA", "Estadual (MA)", ["https://www.fapema.br/editais/"]),
    ("FAPEPI", "Estadual (PI)", ["https://fapepi.pi.gov.br/editais/"]),
    ("FAPES", "Estadual (ES)", ["https://fapes.es.gov.br/editais-abertos"]),
    ("FAPESPA", "Estadual (PA)", ["https://www.fapespa.pa.gov.br/editais"]),
    ("FUNDECT", "Estadual (MS)", ["https://www.fundect.ms.gov.br/editais"]),
    ("FAPERO", "Estadual (RO)", ["https://www.fapero.ro.gov.br/editais/"]),
    ("FAPAC", "Estadual (AC)", ["https://fapac.ac.gov.br/editais-em-aberto/"]),
    ("FAPT", "Estadual (TO)", ["https://www.to.gov.br/fapt/editais/31uugl1y7as4"]),
    ("FAPITEC", "Estadual (SE)", ["https://fapitec.se.gov.br/editais-abertos/"]),
    ("FAPERN", "Estadual (RN)", ["https://www.fapern.rn.gov.br/destaque-orgaos/editais/"]),
    ("FAPEAP", "Estadual (AP)", ["https://fapeap.ap.gov.br/editais/"]),
    ("FAPERR", "Estadual (RR)", ["https://www.rr.gov.br/fapern/"]),
    ("Finep", "Nacional", ["https://www.finep.gov.br/chamadas-publicas"]),
    ("BNDES", "Nacional", ["https://www.bndes.gov.br/wps/portal/site/home/transparencia/chamadas-publicas"]),
    ("Embrapii", "Nacional", ["https://embrapii.org.br/categoria/editais-e-chamadas/"]),
    ("CONFAP", "Nacional", ["https://confap.org.br/pt/editais"]),
]
EXCLUIR_LINK = re.compile(
    r"licita|preg[ãa]o|dispensa|contrato|resultado|retifica|errata|homologa|extrato|"
    r"anteriores|encerrad|ata\b|recurso|gabarito|inscri[çc][ãa]o homolog|cronograma de desembolso",
    re.I,
)
MAX_LINKS_POR_FONTE = 40


def coletar_generico(agencia: str, ambito: str, paginas: list[str]) -> list[Edital]:
    vistos: set[str] = set()
    cand: list[tuple[str, str]] = []
    for pagina in paginas:
        try:
            soup = soup_of(pagina)
        except Exception as e:
            print(f"  aviso: {agencia}: {pagina}: {e}", file=sys.stderr)
            continue
        for a in soup.find_all("a", href=True):
            titulo, href = clean(a.get_text()), urljoin(pagina, a["href"])
            if href in vistos or len(titulo) < 12 or href.startswith(("mailto:", "javascript:", "#")):
                continue
            if not re.search(r"edital|chamada|sele[çc][ãa]o|programa|bolsa", titulo + " " + href, re.I):
                continue
            if EXCLUIR_LINK.search(titulo) or EXCLUIR_LINK.search(href):
                continue
            vistos.add(href)
            cand.append((titulo, href))
    if not cand:
        raise RuntimeError("nenhum link de edital encontrado na listagem")
    out: list[Edital] = []
    for titulo, href in cand[:MAX_LINKS_POR_FONTE]:
        ed = Edital(agencia, titulo, href, ambito)
        try:
            if re.search(r"\.pdf($|\?)|/file$|@@display-file", href, re.I):
                texto, pdfs = "", [href]
            else:
                s = soup_of(href)
                pdfs = [urljoin(href, x["href"]) for x in s.find_all("a", href=True)
                        if re.search(r"\.pdf($|\?)", x["href"], re.I) and re.search(r"edital|chamada", x["href"] + x.get_text(), re.I)]
                h1 = s.find("h1")
                if h1 and re.match(r"(ver|saiba|leia|clique|mais|detalhes)", titulo, re.I):
                    ed.titulo = clean(h1.get_text()) or titulo
                texto = texto_principal(s)
        except Exception as e:
            print(f"  aviso: {href}: {e}", file=sys.stderr)
            continue
        completar(ed, texto, pdfs)
        if ed.prazo:
            out.append(ed)
    return out


FONTES = {"CNPq": coletar_cnpq, "CAPES": coletar_capes, "FAPEMAT": coletar_fapemat}
for _ag, _amb, _pags in GENERICAS:
    FONTES[_ag] = (lambda ag=_ag, amb=_amb, pags=_pags: coletar_generico(ag, amb, pags))


# --------------------------------------------------------------------- saída --
def fmt(d: date | None) -> str:
    return d.strftime("%d/%m/%Y") if d else "ver edital"


def md_cell(t: str, limite: int = 170) -> str:
    t = clean(t).replace("|", "/")
    return t if len(t) <= limite else t[: limite - 1].rstrip() + "…"


def gerar_md(eds: list[Edital], sem_prazo: list[Edital], falhas: dict[str, str]) -> str:
    agora = datetime.now(BRT).strftime("%d/%m/%Y %H:%M")
    grupos = [
        ("Nacional / geral", [e for e in eds if e.ambito == "Nacional"]),
        ("Mato Grosso", [e for e in eds if e.ambito == "Mato Grosso" or e.ambito.endswith("(MT)")]),
        ("Outros estados", [e for e in eds if e.ambito.startswith("Estadual") and not e.ambito.endswith("(MT)")]),
    ]
    L = [
        "# Editais de fomento com bolsas abertos",
        "",
        f"Atualizado em **{agora} (Brasília)** · {len(eds)} edital(is) com bolsa e prazo em aberto · "
        + " · ".join(f"{n}: {len(g)}" for n, g in grupos),
        "",
    ]
    cab = ["| Agência | Pesquisa / Edital | Setores de pesquisa | Valor da bolsa | Âmbito | Data final de envio |",
           "|---|---|---|---|---|---|"]
    for nome, grupo in grupos:
        L += [f"## {nome} ({len(grupo)})", ""]
        if not grupo:
            L += ["_Nenhum edital com bolsa e prazo identificado nesta execução._", ""]
            continue
        L += cab
        for e in grupo:
            L.append(
                f"| {e.agencia} | [{md_cell(e.titulo)}]({e.url}) | {md_cell(', '.join(e.setores)) or 'Geral / multidisciplinar'} "
                f"| {md_cell(e.valor_bolsa) or 'Ver edital'} | {e.ambito} | {fmt(e.prazo)} |"
            )
        L.append("")
    if sem_prazo:
        L += ["## Prazo não identificado automaticamente (confirmar no edital)", ""]
        L += [f"- {e.agencia} · [{md_cell(e.titulo)}]({e.url})" for e in sem_prazo]
        L.append("")
    L += ["> Setores, valores e prazos são extraídos automaticamente do texto dos editais; confirme sempre no edital oficial."]
    if falhas:
        L += ["", "**Fontes sem resultado nesta execução:** " + "; ".join(f"{k} ({v[:80]})" for k, v in falhas.items())]
    return "\n".join(L) + "\n"


def main() -> int:
    todos: list[Edital] = []
    falhas: dict[str, str] = {}
    def rodar(nome: str):
        try:
            achados = FONTES[nome]()
            print(f"{nome}: {len(achados)} candidatos", flush=True)
            return nome, achados, None
        except Exception as e:
            print(f"{nome}: FALHOU {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            return nome, [], f"{type(e).__name__}: {e}"

    # fontes são sites diferentes: roda em paralelo (cada uma é sequencial e educada)
    with ThreadPoolExecutor(max_workers=8) as pool:
        for nome, achados, erro in pool.map(rodar, FONTES):
            todos += achados
            if erro:
                falhas[nome] = erro

    com_bolsa = [e for e in todos if e.tem_bolsa]
    filtrados = sorted((e for e in com_bolsa if e.prazo and e.prazo >= HOJE), key=lambda e: (e.prazo, e.agencia))
    sem_prazo = [e for e in com_bolsa if e.prazo is None]

    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "EDITAIS_ABERTOS.md").write_text(gerar_md(filtrados, sem_prazo, falhas), encoding="utf-8")
    campos = ["agencia", "titulo", "setores", "valor_bolsa", "ambito", "prazo", "url"]
    with open(ROOT / "data" / "editais.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=campos, delimiter=";")
        w.writeheader()
        for e in filtrados:
            w.writerow({"agencia": e.agencia, "titulo": e.titulo, "setores": ", ".join(e.setores),
                        "valor_bolsa": e.valor_bolsa, "ambito": e.ambito, "prazo": fmt(e.prazo), "url": e.url})
    (ROOT / "data" / "editais.json").write_text(
        json.dumps([{**asdict(e), "prazo": e.prazo.isoformat() if e.prazo else None} for e in filtrados],
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(filtrados)} editais com bolsa gravados.")
    # falha explícita no Actions se TODAS as fontes caíram
    return 1 if len(falhas) == len(FONTES) else 0


if __name__ == "__main__":
    sys.exit(main())
