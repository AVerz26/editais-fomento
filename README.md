# Editais de fomento com bolsas abertos

Coleta automática (GitHub Actions) de editais abertos com bolsas: CNPq, CAPES, CONFAP, Finep/BNDES/Embrapii (quando listam bolsas) e fundações estaduais de amparo à pesquisa (FAPESP, FAPERJ, FAPEMAT, FUNDECT etc.).

**Resultado atual:** [EDITAIS_ABERTOS.md](EDITAIS_ABERTOS.md) (tabela por âmbito: Nacional, Mato Grosso, Outros estados) · também em `data/editais.csv` (abre no Excel) e `data/editais.json`.

## Como funciona
- `.github/workflows/editais.yml` roda de segunda a sexta às 8h (Brasília) e manualmente em *Actions → Run workflow*.
- `scripts/coletar_editais.py` lê as páginas de editais, abre cada edital (e o PDF), mantém só os que citam **bolsa** e têm **prazo futuro**.
- O resultado é gravado de volta no repositório e aparece no resumo de cada execução.

## Painel (Streamlit)
`app.py` mostra cada edital em detalhe (valor, modalidades, setores, resumo, links e PDFs), com filtros e tabela exportável.

```bash
pip install -r requirements.txt
streamlit run app.py
```

Para publicar: [share.streamlit.io](https://share.streamlit.io) → *New app* → repositório `AVerz26/editais-fomento`, branch `main`, arquivo `app.py`. O painel lê `data/editais.json`, que o workflow atualiza sozinho.

## Configuração única
Settings → Actions → General → Workflow permissions → **Read and write permissions**.

## Adicionar fontes
Edite a lista `GENERICAS` em `scripts/coletar_editais.py` (agência, âmbito, URL da página que lista os editais).

## Limitações
Setores, valor da bolsa e prazo são extraídos do texto por heurística: confira no edital oficial. Sites que carregam a lista por JavaScript ou bloqueiam robôs aparecem em "Fontes sem resultado".
