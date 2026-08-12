"""
Núcleo de conversão do docintel.

Converte um PDF em um Markdown pronto para Obsidian, seguindo o padrão de
nota do vault de Hugo:
- frontmatter YAML no formato: disciplina, tags, excalidraw-plugin,
  data_criacao, professora, assunto (bloco YAML), caminho
- bullet list logo abaixo do frontmatter espelhando o "assunto"
- imagens extraídas para uma subpasta images/, referenciadas como
  embeds wikilink do Obsidian (![[arquivo.png]])

## Sobre a extração de texto e OCR (leia antes de mexer aqui)

pymupdf4llm >= 1.28 ativa por padrão um motor de layout baseado em ONNX
(pymupdf.layout) que, entre outras coisas, usa um classificador de ML pra
decidir página por página se deve rodar OCR. Esse classificador erra em
páginas que têm uma imagem grande (ex: um logo/capa) MISTURADA com bastante
texto nativo: ele classifica a página inteira como "precisa de OCR", roda
Tesseract sobre o render da página, e o resultado SUBSTITUI o texto nativo
-- descartando silenciosamente todo o texto digital real que já estava lá
(sobra só o texto que o OCR conseguiu ler dentro/perto da imagem). Não é
um "quase certo com ruído": o conteúdo desaparece por completo, sem aviso.

Por isso aqui é feito o oposto do que a lib faz por padrão:
1. `pymupdf4llm.use_layout(False)` desliga esse motor ONNX/classificador e
   volta pro caminho de extração "legado", que nunca substitui texto nativo
   por OCR (ele só lê o que já está no PDF).
2. Como o caminho legado não faz OCR nenhum sozinho, OCR é feito aqui
   manualmente, mas só nas páginas onde a extração nativa (via PyMuPDF puro)
   não encontrou texto nenhum -- ou seja, só em páginas genuinamente
   escaneadas. Páginas com texto nativo nunca passam por OCR, então nunca
   correm o risco de ter conteúdo apagado.
"""

import datetime
import hashlib
import os
import re
import sys
import unicodedata

import pymupdf
import pymupdf4llm

# Desativa o motor de layout/OCR automático baseado em ONNX (ver docstring
# do módulo). Precisa rodar antes de qualquer chamada de to_markdown().
pymupdf4llm.use_layout(False)

# Abaixo deste número de caracteres de texto nativo, consideramos a página
# "sem texto digital" (provavelmente escaneada) e rodamos OCR manual nela.
_OCR_MIN_CHARS = 30

# Casa qualquer link de imagem gerado pelo pymupdf4llm, independente do
# caminho absoluto/relativo que ele tenha usado internamente.
_IMG_LINK_RE = re.compile(r"!\[\]\(([^)]+\.(?:png|jpe?g|webp))\)")

# Extrai o sufixo "-<pagina>-<indice>.<ext>" do nome de imagem gerado pela
# lib, que sempre deriva do nome do arquivo de entrada (não há kwarg que
# controle isso) -- usamos o sufixo pra remontar um nome limpo com o slug.
# Imagens que ocupam a página inteira saem como "-<pagina>-full.<ext>" (sem
# índice numérico), então o índice é opcional aqui.
_IMG_SUFFIX_RE = re.compile(r"-(\d+)-(?:full|\d+)\.(png|jpe?g|webp)$", re.IGNORECASE)

# Marcador de fim de página inserido pelo pymupdf4llm quando pedimos
# page_separators=True -- usamos pra saber onde encaixar o OCR manual.
# As quebras de linha em volta são \n+ (não \n{2} fixo): o marcador da
# ÚLTIMA página do documento fica colado ao fim da string depois do
# `.strip()` de `normalize_markdown_text`, sem "\n\n" depois dele.
_PAGE_SEP_RE = re.compile(r"\n+--- end of page=(\d+) ---\n*")

# Ruído comum de OCR/HTML que sobra no texto (comentários de "texto de
# imagem", tags soltas) -- limpar deixa a nota legível no Obsidian.
_HTML_TAG_RE = re.compile(
    r"</?(?:p|div|span|u|b|i|strong|em|font|sup|sub|table|tr|td|th|li|ul|ol|h\d|a|img)[^>]*>"
)
_HTML_COMMENT_RE = re.compile(r"(?is)<!--.*?-->")

# Rodapé típico de apostila digitalizada: número da página impresso seguido,
# na linha seguinte, de um domínio/URL do material de apoio (ex: "14" /
# "www.g7juridico.com.br"). Genérico o bastante pra pegar qualquer domínio,
# não só um curso específico. Capturamos o número pra virar anotação de
# página e descartamos a linha de URL (é ruído de rodapé, não conteúdo).
# Sem âncora de fim de string: em páginas com imagem (ex: uma tabela
# renderizada como imagem), o pymupdf4llm emite o embed da imagem DEPOIS do
# rodapé no texto, então o rodapé não fica necessariamente no fim do bloco.
_FOOTER_RE = re.compile(
    r"\n{1,}(?P<pagenum>\d{1,4})\n"
    r"(?P<url>(?:https?://)?(?:www\.)?[A-Za-z0-9][A-Za-z0-9\-]*(?:\.[A-Za-z0-9\-]+)+(?:/\S*)?)"
    r"(?=\n|$)"
)

# --- Reflow de parágrafos ---------------------------------------------------
#
# pymupdf4llm, no caminho legado (use_layout(False) -- ver docstring do
# módulo), não reconstrói parágrafos: cada LINHA física do PDF vira um
# "parágrafo" markdown separado por linha em branco (às vezes por uma única
# quebra). Isso quebra frases e citações do STF no meio, e cada linha do PDF
# vira uma linha solta na nota do Obsidian.
#
# `reflow_paragraphs` desfaz isso: junta linhas consecutivas que são
# claramente continuação de um mesmo parágrafo/item, e preserva como quebra
# real apenas as linhas que começam um novo bloco estrutural (heading,
# marcador de lista/tópico, linha totalmente em negrito usada como
# subtítulo, tabela, imagem, linha horizontal). É uma heurística baseada em
# como as apostilas de aula (G7 Jurídico e afins) são diagramadas -- não é
# um parser de layout, então casos atípicos podem escapar.
_BLOCK_START_PATTERNS = [
    re.compile(r"^#{1,6}\s"),                       # heading markdown
    re.compile(r"^>\s"),                             # blockquote
    re.compile(r"^\|"),                               # linha de tabela
    re.compile(r"^!\["),                              # embed de imagem
    re.compile(r"^-{3,}\s*$"),                        # linha horizontal
    re.compile(r"^[-•❖\*]"),                          # bullet/tópico (a apostila usa "-Texto" sem espaço)
    re.compile(r"^\d+[.\)]\s"),                        # lista numerada "1) " "1. "
    re.compile(r"^\d+\.\d+"),                          # "5.1 ..."
    re.compile(r"^[IVXLCDM]+\s*[-.]\s", re.IGNORECASE),  # numeral romano "I - "
    re.compile(r"^\([a-z0-9ivx]+\)\s", re.IGNORECASE),  # "(a) ", "(iii) "
    re.compile(r"^<u>.*</u>\s*$"),                     # linha sublinhada inteira (subtítulo)
    re.compile(r"^\*\*[^*]+\*\*:?\s*$"),               # linha inteira em negrito (subtítulo)
]


def _is_block_start(line: str) -> bool:
    """Uma linha começa um novo bloco (não é continuação) quando está
    alinhada à margem esquerda (linhas indentadas são sempre continuação --
    evita que um "-" de aspas indentado seja confundido com bullet novo) e
    casa com um dos padrões estruturais conhecidos."""
    if line != line.lstrip():
        return False
    return any(p.match(line) for p in _BLOCK_START_PATTERNS)


def reflow_paragraphs(md_text: str) -> str:
    """Junta linhas que são continuação do mesmo parágrafo/item de lista,
    preservando quebras reais entre blocos estruturais. Ver docstring da
    seção acima para os detalhes da heurística.

    Linhas em branco são ignoradas como sinal de quebra: o pymupdf4llm
    insere uma linha em branco entre TODA linha física do PDF (seja ela
    continuação do mesmo parágrafo ou o início de um novo), então a
    presença de uma linha em branco, por si só, não indica nada -- só a
    própria linha (bater ou não com um padrão de início de bloco) decide
    onde um novo bloco começa.
    """
    blocos = []
    atual = []

    for linha in md_text.split("\n"):
        if not linha.strip():
            continue
        if not atual or _is_block_start(linha):
            if atual:
                blocos.append(_juntar_linhas(atual))
            atual = [linha]
        else:
            atual.append(linha)
    if atual:
        blocos.append(_juntar_linhas(atual))

    return "\n\n".join(blocos)


def _juntar_linhas(linhas: list) -> str:
    """Junta as linhas de um único bloco/parágrafo em uma string contínua.
    Preserva a quebra de linha explícita quando o bloco começa com um
    marcador de heading (headings não devem ganhar texto colado depois).
    Trata hifenização visível no fim de linha (quebra de palavra) juntando
    sem espaço."""
    partes = [linhas[0]]
    for linha in linhas[1:]:
        anterior = partes[-1]
        if anterior.endswith("-") and not anterior.endswith("--") and re.search(r"[A-Za-zÀ-ÿ]-$", anterior):
            partes[-1] = anterior[:-1] + linha.lstrip()
        else:
            partes.append(linha.strip())
    return " ".join(p for p in partes if p != "") if len(partes) > 1 else partes[0]


def process_pages(md_text: str) -> str:
    """Separa o texto por página (marcadores do pymupdf4llm), e para cada
    página: remove a linha de rodapé (número da página + domínio do
    material -- ver `_FOOTER_RE`), aplica o reflow de parágrafos (ver
    `reflow_paragraphs`) e por fim junta tudo de volta com uma anotação de
    página legível no Obsidian entre elas.

    IMPORTANTE: o rodapé precisa ser removido ANTES do reflow -- o rodapé é
    sempre duas linhas soltas ("14" / "www.exemplo.com.br") e o reflow, ao
    juntar linhas de continuação, coalesceria as duas em uma linha só,
    quebrando o casamento do `_FOOTER_RE`.
    """
    partes = _PAGE_SEP_RE.split(md_text)
    saida = []
    for i in range(0, len(partes) - 1, 2):
        conteudo = partes[i]
        pagina_indice = int(partes[i + 1])

        m = _FOOTER_RE.search(conteudo)
        if m:
            pagina_num = m.group("pagenum")
            conteudo = _FOOTER_RE.sub("\n", conteudo, count=1)
        else:
            pagina_num = str(pagina_indice + 1)

        conteudo = reflow_paragraphs(conteudo)

        saida.append(conteudo)
        saida.append(f"\n\n---\n*p. {pagina_num}*\n\n---\n\n")

    if len(partes) % 2 == 1 and partes[-1].strip():
        saida.append(reflow_paragraphs(partes[-1]))
    elif saida and saida[-1].startswith("\n\n---"):
        # não deixa a nota terminar com um separador de página sem conteúdo depois
        saida.pop()

    return "".join(saida)

# Detecta um item de sumário numerado no estilo usado nos slides das aulas:
# "4. Poder Legislativo", "4.6 Imunidades...", "5- Processo legislativo...",
# "5.1. Introdução". Testado contra a nota de referência do usuário.
_OUTLINE_RE = re.compile(r"^\d+(?:[.\-]\d+)*\.?[\s\-]+\S")


def slugify(filename: str) -> str:
    """Gera um slug seguro (minúsculo, sem acento) para nome de pasta -- usado
    internamente para a pasta de saída (job_dir) e deduplicação."""
    base = os.path.splitext(filename)[0]
    base = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-zA-Z0-9]+", "-", base).strip("-").lower()
    return base or "documento"


def display_name(filename: str) -> str:
    """Nome legível para o .md e as imagens, preservando a grafia original
    do arquivo (ex: 'DCA1.pdf' -> 'DCA1'), só sanitizado o suficiente pra
    ser um nome de arquivo seguro."""
    base = os.path.splitext(filename)[0]
    base = re.sub(r"\s+", "_", base.strip())
    base = re.sub(r"[^A-Za-z0-9._\-]+", "", base)
    base = base.strip("._-")
    return base or slugify(filename)


def file_hash(path: str) -> str:
    """Hash sha256 (16 chars) do conteúdo do arquivo -- usado para deduplicação."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def guess_aula_numero(original_filename: str):
    """Tenta extrair o número da aula do nome do arquivo (ex: 'Aula_05_x.pdf' -> '5')."""
    m = re.search(r"(\d{1,3})", original_filename)
    if not m:
        return None
    return str(int(m.group(1)))


def normalize_markdown_text(md_text: str) -> str:
    """Remove ruído comum de OCR/HTML para deixar o Markdown legível em Obsidian."""
    if not md_text:
        return ""
    text = md_text.replace("\r\n", "\n").replace("\r", "\n")
    text = _HTML_COMMENT_RE.sub("", text)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = _HTML_TAG_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _paginas_sem_texto_nativo(doc: "pymupdf.Document", min_chars: int = _OCR_MIN_CHARS) -> dict:
    """Roda OCR manual só nas páginas sem texto nativo suficiente (páginas
    genuinamente escaneadas). Não toca em páginas com texto digital, mesmo
    que tenham imagens grandes -- é exatamente esse caso que o classificador
    automático da lib erra (ver docstring do módulo).
    """
    resultado = {}
    for page in doc:
        nativo = page.get_text().strip()
        if len(nativo) >= min_chars:
            continue
        try:
            tp = page.get_textpage_ocr(full=True, language="por+eng", dpi=200)
            texto_ocr = page.get_text(textpage=tp).strip()
        except Exception as exc:  # noqa: BLE001 -- não deixa uma página ruim derrubar o job inteiro
            print(f"aviso: OCR falhou na página {page.number + 1}: {exc}", file=sys.stderr)
            continue
        if texto_ocr:
            resultado[page.number] = texto_ocr
    return resultado


def _aplicar_ocr_manual(md_text: str, ocr_por_pagina: dict) -> str:
    """Reencaixa o texto de OCR manual nas páginas que a extração nativa
    deixou vazias, usando os marcadores de página (page_separators=True).
    Mantém os marcadores de página no resultado -- eles são removidos (e
    convertidos em anotação de página) depois, por `annotate_pages`."""
    if not ocr_por_pagina:
        return md_text

    partes = _PAGE_SEP_RE.split(md_text)
    # re.split intercala: [conteudo_pg0, "0", conteudo_pg1, "1", ..., sobra_final]
    saida = []
    for i in range(0, len(partes) - 1, 2):
        conteudo = partes[i]
        pagina = int(partes[i + 1])
        # o link de imagem (com caminho completo) não conta como "texto" pra
        # essa checagem -- senão uma página só-imagem nunca pareceria vazia.
        texto_sem_imagens = _IMG_LINK_RE.sub("", conteudo).strip()
        if pagina in ocr_por_pagina and len(texto_sem_imagens) < _OCR_MIN_CHARS:
            texto_ocr = ocr_por_pagina[pagina]
            conteudo = f"{conteudo}\n\n{texto_ocr}" if conteudo.strip() else texto_ocr
        saida.append(conteudo)
        saida.append(f"\n\n--- end of page={pagina} ---\n\n")
    if len(partes) % 2 == 1 and partes[-1].strip():
        saida.append(partes[-1])
    elif saida:
        saida.pop()  # remove o marcador de página sem conteúdo real depois dele
    return "".join(saida)


def _precisa_aspas(valor: str) -> bool:
    if valor != valor.strip():
        return True
    return bool(re.search(r'[:#\[\]{}]|^[\-?!&*|>%@`"\']', valor))


def _yaml_escalar(valor: str) -> str:
    valor = valor.strip()
    if _precisa_aspas(valor):
        return '"' + valor.replace('"', '\\"') + '"'
    return valor


def detect_assunto(md_text: str, max_linhas: int = 15, max_varredura: int = 40) -> list:
    """Heurística: procura, perto do início do documento, um bloco de linhas
    no formato de sumário numerado (ex: slide "assuntos de hoje" da aula) e
    devolve essas linhas. Se não encontrar nada parecido, devolve lista vazia
    -- não força a marcação.
    """
    linhas = [l.strip() for l in md_text.strip().splitlines()]
    outline = []
    varridas = 0
    for linha in linhas:
        # pymupdf4llm às vezes renderiza o primeiro item como heading ("## ")
        # e cada item seguinte como bullet ("- "); remove os dois antes de testar.
        conteudo = linha.lstrip("#-* ").strip()
        if not conteudo:
            if outline:
                continue  # tolera linha em branco entre itens do sumário
            varridas += 1
            if varridas > max_varredura:
                break
            continue
        if _OUTLINE_RE.match(conteudo):
            outline.append(conteudo)
            if len(outline) >= max_linhas:
                break
        else:
            if outline:
                break
            varridas += 1
            if varridas > max_varredura:
                break
    return outline


def build_frontmatter(meta: dict) -> str:
    lines = ["---"]

    disciplina = (meta.get("disciplina") or "").strip()
    lines.append(f"disciplina: {_yaml_escalar(disciplina.upper())}" if disciplina else "disciplina:")

    tags = [t.strip() for t in (meta.get("tags") or []) if t.strip()]
    if tags:
        lines.append("tags:")
        for t in tags:
            lines.append(f"  - {t}")
    else:
        lines.append("tags:")

    lines.append("excalidraw-plugin: parsed")
    lines.append(f'data_criacao: {meta.get("data_criacao", "")}')

    professora = (meta.get("professora") or "").strip()
    lines.append(f"professora: {_yaml_escalar(professora)}" if professora else "professora:")

    assunto_lines = meta.get("assunto_lines") or []
    if assunto_lines:
        lines.append("assunto: |-")
        for a in assunto_lines:
            lines.append(f"  {a}")
    else:
        lines.append("assunto:")

    caminho = (meta.get("caminho") or "").strip()
    lines.append(f"caminho: {_yaml_escalar(caminho)}" if caminho else "caminho:")

    lines.append("---")
    return "\n".join(lines)


def convert_pdf(input_path: str, job_dir: str, meta: dict) -> dict:
    """Converte um PDF em Markdown para Obsidian dentro de job_dir.

    Estrutura gerada:
        job_dir/
            <slug>.md
            images/*.png   (se houver imagens)

    Retorna um dict com o caminho do .md e a quantidade de imagens extraídas.
    """
    os.makedirs(job_dir, exist_ok=True)
    img_dir = os.path.join(job_dir, "images")
    os.makedirs(img_dir, exist_ok=True)

    doc = pymupdf.open(input_path)
    try:
        ocr_por_pagina = _paginas_sem_texto_nativo(doc)
    finally:
        doc.close()

    md_text = pymupdf4llm.to_markdown(
        input_path,
        write_images=True,
        image_path=img_dir,
        image_format="png",
        page_separators=True,
    )
    md_text = _aplicar_ocr_manual(md_text, ocr_por_pagina)
    md_text = normalize_markdown_text(md_text)

    # detect_assunto depende da granularidade linha-a-linha original (cada
    # item do sumário numerado em sua própria linha) pra casar com
    # `_OUTLINE_RE` -- por isso roda ANTES do reflow, que junta linhas de
    # continuação e destruiria essa granularidade.
    assunto_lines = detect_assunto(md_text)

    # process_pages remove o rodapé de cada página, faz o reflow de
    # parágrafos (ver docstrings de `process_pages` e `reflow_paragraphs`
    # para a ordem e o porquê) e substitui os marcadores internos de página
    # por uma anotação legível no Obsidian.
    md_text = process_pages(md_text)

    nome = meta.get("display_name") or meta["slug"]
    contador = {"n": 0}

    def _fix_link(match: "re.Match") -> str:
        old_basename = os.path.basename(match.group(1))
        suffix = _IMG_SUFFIX_RE.search(old_basename)
        if suffix:
            # page.number no caminho legado é 0-indexado; +1 pra bater com a
            # numeração de página real que o usuário vê no PDF.
            pagina = int(suffix.group(1)) + 1
            ext = suffix.group(2)
        else:
            pagina = 0
            ext = old_basename.rsplit(".", 1)[-1] if "." in old_basename else "png"

        contador["n"] += 1
        # Numeração sequencial global (ordem de leitura, já é a ordem em que
        # o pymupdf4llm emite as imagens no texto) + página real do PDF --
        # ex: DCA1-img1-pg2.png, DCA1-img2-pg2.png (2ª imagem, mesma página).
        new_basename = f"{nome}-img{contador['n']}-pg{pagina}.{ext}"

        old_path = os.path.join(img_dir, old_basename)
        new_path = os.path.join(img_dir, new_basename)
        if old_basename != new_basename and os.path.exists(old_path) and not os.path.exists(new_path):
            os.rename(old_path, new_path)
        # Embed no estilo wikilink do Obsidian: só o nome do arquivo, sem
        # caminho -- o Obsidian resolve pelo vault inteiro, igual ao padrão
        # observado na nota de referência (![[Pasted image ...]]).
        return f"![[{new_basename}]]"

    md_text = _IMG_LINK_RE.sub(_fix_link, md_text)

    # Remove a pasta de imagens se o documento não gerou nenhuma
    if os.path.isdir(img_dir) and not os.listdir(img_dir):
        os.rmdir(img_dir)

    n_images = len(os.listdir(img_dir)) if os.path.isdir(img_dir) else 0

    meta = dict(meta)
    meta["assunto_lines"] = assunto_lines
    meta.setdefault("data_criacao", datetime.date.today().isoformat())

    frontmatter = build_frontmatter(meta)

    partes = [frontmatter]
    if assunto_lines:
        partes.append("\n".join(f"- {a}" for a in assunto_lines))
    partes.append(md_text.strip())
    final_md = "\n\n".join(partes) + "\n"

    md_path = os.path.join(job_dir, f"{nome}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(final_md)

    return {"md_path": md_path, "n_images": n_images}
