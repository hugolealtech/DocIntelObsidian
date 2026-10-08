"""
Núcleo de conversão do docintel.

Converte um PDF em um Markdown pronto para Obsidian, seguindo o padrão de
nota do vault de Hugo:
- frontmatter YAML no formato: disciplina, tags, excalidraw-plugin,
  data_criacao, professora, assunto (bloco YAML), caminho
- bullet list logo abaixo do frontmatter espelhando o "assunto"
- imagens extraídas para uma subpasta images/, referenciadas como
  embeds wikilink do Obsidian (![[arquivo.png]])

## Sobre a extração posicional e OCR (leia antes de mexer aqui)

O pipeline de apostilas usa PyMuPDF diretamente como fonte da ordem: texto,
imagens e figuras são eventos ordenados pelas coordenadas de cada página.
Isso evita que um Markdown intermediário mova todas as imagens para o início
ou para o fim do texto.

Páginas com menos de `_OCR_MIN_CHARS` caracteres nativos são reconhecidas
com Tesseract via `get_textpage_ocr`, a `_OCR_DPI`. O texto OCR conserva as
coordenadas; linhas sobrepostas a figuras são removidas do corpo e, quando
reconhecidas, ficam num callout recolhido junto ao embed. Páginas que já têm
texto nativo não passam por OCR de página inteira.

`pymupdf4llm.use_layout(False)` continua desativando o classificador ONNX
porque o pipeline de legislação ainda pode usar a extração compartilhada.
"""

import datetime
import hashlib
import io
import os
import re
import subprocess
import sys
import unicodedata

import pymupdf
import pymupdf4llm
from PIL import Image

if __package__:
    from . import tabelas
else:
    import tabelas

# Desativa o motor de layout/OCR automático baseado em ONNX (ver docstring
# do módulo). Precisa rodar antes de qualquer chamada de to_markdown().
pymupdf4llm.use_layout(False)

# Abaixo deste número de caracteres de texto nativo, consideramos a página
# "sem texto digital" (provavelmente escaneada) e rodamos OCR manual nela.
_OCR_MIN_CHARS = 30
_OCR_DPI = 300
HEADING_BASE_LEVEL = 4
_HEADING_MAX_LEVEL = 6
_LAYOUT_Y_TOLERANCE = 3.0
_PARAGRAPH_LEFT_TOLERANCE = 4.0
_PARAGRAPH_LINE_GAP_FACTOR = 1.6
_PARAGRAPH_RIGHT_EDGE_RATIO = 0.85
_FIGURE_TEXT_OVERLAP_RATIO = 0.60
_VECTOR_FIGURE_MIN_AREA_RATIO = 0.025
_VECTOR_FIGURE_MIN_WIDTH_RATIO = 0.20
_OCR_HEADING_MIN_HEIGHT = 10.0

# Casa qualquer link de imagem gerado pelo pymupdf4llm, independente do
# caminho absoluto/relativo que ele tenha usado internamente.
#
# IMPORTANTE: o grupo de caminho usa ".+" (guloso), não "[^)]+". O nome do
# arquivo de imagem deriva do nome do PDF de entrada (ver `_IMG_SUFFIX_RE`
# abaixo), e nomes de PDF com parênteses são comuns (ex: "aula (1).pdf" --
# arquivo baixado mais de uma vez). Com "[^)]+" a busca parava no primeiro
# ")" que aparecesse dentro do próprio nome do arquivo (ex:
# "...DCA3-(1).pdf-0-0.png"), o link inteiro deixava de casar com o regex e
# a imagem sobrava como link de sistema de arquivos cru (ex:
# "![](/data/output/.../DCA3-(1).pdf-0-0.png)") em vez de virar um embed
# wikilink do Obsidian -- quebrando a imagem na nota. ".+" guloso, por ser
# guloso, sempre recua até o ÚLTIMO ")" da linha (o fechamento de verdade),
# então casa corretamente mesmo com parênteses no meio do nome.
_IMG_LINK_RE = re.compile(r"!\[\]\((.+\.(?:png|jpe?g|webp))\)")

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
_PAGE_NOTE_RE = re.compile(r"(\n\n---\n\*p\. \d+\*\n\n---\n\n)")
_MARKDOWN_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_MARKDOWN_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|(?:\s*:?-{3,}:?\s*\|)+\s*$")

# Ruído comum de OCR/HTML que sobra no texto (comentários de "texto de
# imagem", tags soltas) -- limpar deixa a nota legível no Obsidian.
_HTML_TAG_RE = re.compile(
    r"</?(?:p|div|span|u|b|i|strong|em|font|sup|sub|table|tr|td|th|li|ul|ol|h\d|a|img)\b[^>]*>"
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
    r"(?im)\n{1,}(?P<pagenum>\d{1,4})[ \t]*\n"
    r"[ \t]*(?P<url>(?:(?:https?\s*:\s*/\s*/)|(?:w\s*w\s*w\s*\.\s*))?"
    r"[A-Za-z0-9][A-Za-z0-9\-]*(?:\s*\.\s*[A-Za-z0-9\-]+)+(?:\s*/\s*\S*)?)"
    r"[.,;:!?)]*(?=[ \t]*(?:\n|$))"
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


# =============================================================================
# A PARTIR DAQUI: funções EXCLUSIVAS do pipeline de apostila (`convert_pdf`,
# a nota de aula "padrão" -- não a de legislação).
#
# `converter_legislacao.py` reaproveita (importando e chamando) várias
# funções acima -- `process_pages`, `reflow_paragraphs`, `normalize_markdown_text`,
# `_paginas_sem_texto_nativo`, `_aplicar_ocr_manual`, `slugify` -- então
# nada do que está ACIMA desta marca foi tocado (ver seção anterior, sem
# mudanças de comportamento) para não alterar, nem de forma indireta, a
# saída do "Markdown de Legislação". Tudo que segue abaixo é código NOVO,
# usado só por `convert_pdf`: onde algo precisou de uma variação de uma
# função de cima (ex: reflow com mais padrões de bloco), foi feita uma
# CÓPIA isolada em vez de editar a original, de propósito.
# =============================================================================


# --- Notas de rodapé (sobrescrito -> [^N] do Obsidian) --------------------
#
# O pymupdf4llm emite uma referência de nota de rodapé de verdade (PDF com
# número sobrescrito ligado a um texto no rodapé) como "<sup>N</sup>" no
# meio do texto. `normalize_markdown_text` (função compartilhada acima, não
# tocada) descarta QUALQUER tag <sup>/</sup> como ruído de HTML -- então,
# sem tratamento especial, o número da nota sobra como um dígito solto no
# meio da frase, sem nenhuma marcação, e o corpo da nota (que fica mais
# abaixo no texto, começando com esse mesmo número) também fica solto,
# como se fosse texto comum. Resultado: nenhuma nota de rodapé funciona no
# Obsidian (que exige a sintaxe "[^N]" ... "[^N]: corpo").
_SUP_FOOTNOTE_RE = re.compile(r"<sup>\s*(\d{1,3})\s*</sup>")

# Depois que a referência virou "[^N]" (ver função abaixo), usamos isso pra
# achar as referências de fato usadas numa página, e isso pra achar onde o
# CORPO da nota começa: um parágrafo que começa com o mesmo número seguido
# de espaço (ex: "1 **“Art. 102, CF/88:** Compete ao..."). Só conta como
# início de corpo de nota se o número bater com uma referência já vista na
# página -- não com qualquer linha que comece com dígito (ver uso em
# `_extrair_notas_rodape_pagina`).
_FOOTNOTE_REF_RE = re.compile(r"\[\^(\d{1,3})\]")
_FOOTNOTE_BODY_START_RE = re.compile(r"^(\d{1,3})\s+(?=\S)")


def _converter_notas_rodape_sup(md_text: str) -> str:
    """Converte "<sup>N</sup>" (marcador de nota de rodapé de verdade,
    emitido pelo pymupdf4llm) para a sintaxe de referência do Obsidian
    ("[^N]"). PRECISA rodar antes de `normalize_markdown_text` -- que
    descarta a tag <sup> como ruído de HTML (ver docstring da seção)."""
    if not md_text:
        return md_text
    return _SUP_FOOTNOTE_RE.sub(lambda m: f"[^{m.group(1)}]", md_text)


def _extrair_notas_rodape_pagina(conteudo: str) -> tuple:
    """Localiza, dentro do texto de UMA página (já sem rodapé, ainda sem
    reflow -- cada linha física do PDF em sua própria linha), o corpo de
    cada nota de rodapé referenciada via "[^N]" nessa página. O corpo
    normalmente aparece mais abaixo, como um parágrafo que começa com o
    mesmo número (ex: "1 **“Art. 102, CF/88:** Compete..."). Devolve
    (conteudo_sem_o_corpo_das_notas, [(numero, corpo_texto), ...]) na ordem
    em que os corpos aparecem. Uma referência "[^N]" sem corpo encontrado
    na página fica como está no texto -- vira uma nota "quebrada" (sem
    "[^N]:" correspondente), degradação aceitável, não falha o job."""
    refs = list(dict.fromkeys(_FOOTNOTE_REF_RE.findall(conteudo)))
    if not refs:
        return conteudo, []

    linhas = conteudo.split("\n")
    encontrados = set()
    notas = []
    linhas_saida = []
    i = 0
    while i < len(linhas):
        linha = linhas[i]
        candidato = linha.strip()
        m = _FOOTNOTE_BODY_START_RE.match(candidato)
        numero = m.group(1) if m else None
        if numero and numero in refs and numero not in encontrados:
            corpo_linhas = [candidato[m.end():].strip()]
            i += 1
            while i < len(linhas):
                prox = linhas[i]
                prox_strip = prox.strip()
                if not prox_strip:
                    i += 1
                    continue
                m2 = _FOOTNOTE_BODY_START_RE.match(prox_strip)
                if prox_strip.startswith("![") or (m2 and m2.group(1) in refs):
                    break
                corpo_linhas.append(prox_strip)
                i += 1
            corpo = " ".join(c for c in corpo_linhas if c)
            if corpo:
                notas.append((numero, corpo))
                encontrados.add(numero)
            continue
        linhas_saida.append(linha)
        i += 1
    return "\n".join(linhas_saida), notas


# --- Marcadores de tópico "exóticos" ---------------------------------------
#
# Apostilas costumam usar glifos de fontes decorativas (Wingdings/Marlett e
# afins) como marcador de tópico. A extração do PDF não reconhece essas
# fontes como texto normal e devolve ou o glifo Unicode mais próximo (❖ ➢
# ⮚ ...) ou, quando nem isso, o interpreta como texto monoespaçado e sai
# entre crases (ex: "`o`"). Nenhum dos dois é uma lista de verdade pro
# Obsidian -- aparecem como símbolo cru na nota (é o "símbolo que não é
# reconhecido pelo Obsidian" no índice/sumário). Quando aparecem no INÍCIO
# da linha (função de marcador de tópico), normalizamos para um "- " de
# verdade; quando sobram no FIM da linha (uso puramente decorativo, sem
# nenhum conteúdo depois), são só descartados.
_MARCADOR_ESTRANHO_RE = re.compile(r"(?m)^([ \t]*)(?:`o`|[❖➢⮚➤➥▶►∙•●○▪◦])[ \t]*")
_MARCADOR_DECORATIVO_FIM_RE = re.compile(r"(?m)[ \t]*[❖➢⮚➤➥▶►](?=[ \t]*$)")
_MARCADOR_TOPICO_CONVERTIDO = "\ue000"


def _normalizar_marcadores_apostila(texto: str) -> str:
    """Converte marcadores decorativos para um marcador interno rastreável.

    O token interno permite distinguir os hífens criados pelo conversor dos
    hífens literais do documento durante o reflow.
    """
    if not texto:
        return texto
    texto = _MARCADOR_DECORATIVO_FIM_RE.sub("", texto)
    texto = _MARCADOR_ESTRANHO_RE.sub(
        lambda m: f"{m.group(1)}{_MARCADOR_TOPICO_CONVERTIDO} ",
        texto,
    )
    return texto


def _restaurar_marcadores_topico(texto: str) -> str:
    return texto.replace(_MARCADOR_TOPICO_CONVERTIDO, "-")


# --- Reflow com padrões extras de índice/sumário ---------------------------
#
# Cópia isolada de `reflow_paragraphs` (ver docstring da seção acima sobre
# por que é cópia, não edição) com padrões extras de início de bloco que a
# apostila usa e a lista original não cobria: item de índice numerado com
# travessão ("1- Teoria Geral", "2- Ações" -- sem isso, esses itens do
# sumário grudavam no título anterior ou uns nos outros, e o índice saía
# ilegível), ordinal com parênteses ("1ª) Remédio...", "2ª) Da decisão...")
# e subtítulo de letra ("A) ROC", "A.1) ROC no STF").
_BLOCK_START_PATTERNS_APOSTILA = _BLOCK_START_PATTERNS + [
    re.compile(r"^\ue000\s"),               # marcador decorativo convertido, origem preservada
    re.compile(r"^\d{1,3}[ªº]\)\s"),      # "1ª) ", "2ª) "
    re.compile(r"^\d{1,3}[-–—]\s+\S"),  # "1- Teoria Geral", "2- Ações"
    re.compile(r"^[A-Z]\)\s"),             # "A) ROC", "B) ..."
    re.compile(r"^[A-Z]\.\d+\)\s"),        # "A.1) ROC no STF"
]


def _is_block_start_apostila(line: str) -> bool:
    if line != line.lstrip():
        return False
    return any(p.match(line) for p in _BLOCK_START_PATTERNS_APOSTILA)


def _e_subtopico_indentado(line: str) -> bool:
    """Uma linha INDENTADA (recuada em relação à margem) começa um
    sub-tópico novo -- não é só continuação do parágrafo/bullet anterior
    -- quando, depois de tirado o recuo, ela mesma casa com um marcador
    de bloco reconhecido (ver `_BLOCK_START_PATTERNS_APOSTILA`). Usado
    por `reflow_paragraphs_apostila` pra não achatar hierarquia de
    tópicos (pai -> filho) numa lista única -- ver docstring da seção
    "Reflow com padrões extras de índice/sumário" acima e o registro do
    bug de hierarquia achatada que motivou isso."""
    sem_recuo = line.lstrip()
    return any(p.match(sem_recuo) for p in _BLOCK_START_PATTERNS_APOSTILA)


def reflow_paragraphs_apostila(md_text: str) -> str:
    """Ver docstring da seção "Reflow com padrões extras de índice/sumário"
    acima. Mesma lógica de `reflow_paragraphs`, com a lista de padrões
    estendida E uma diferença adicional, exclusiva desta cópia: uma linha
    indentada que ela mesma comece com um marcador de bloco reconhecido
    (bullet, item de índice, etc. -- ver `_e_subtopico_indentado`) abre um
    sub-tópico NOVO, aninhado um nível abaixo do bloco atual (prefixo
    "  " na saída), em vez de ser colada como texto solto no bloco pai --
    a apostila usa indentação pra marcar hierarquia visual entre tópicos
    (ex: "Características fundamentais:" com "Hereditariedade:" e
    "Vitaliciedade:" como sub-itens abaixo), e colar tudo numa frase só
    misturava itens que no PDF são pontos distintos. Uma linha indentada
    que NÃO comece com marcador nenhum (ex: um "-" de abertura de aspas
    no meio de uma frase, ou a continuação sem marcador de um sub-item já
    aberto) continua tratada como simples continuação, igual antes."""
    blocos = []
    atual = []
    atual_aninhado = False

    def _fechar_bloco():
        texto = _juntar_linhas(atual)
        blocos.append(("  " + texto) if atual_aninhado else texto)

    for linha in md_text.split("\n"):
        if not linha.strip():
            continue
        indentado = linha != linha.lstrip()
        novo_subtopico = indentado and _e_subtopico_indentado(linha)
        novo_bloco_normal = (not indentado) and _is_block_start_apostila(linha)

        if not atual:
            atual = [linha.lstrip() if novo_subtopico else linha]
            atual_aninhado = novo_subtopico
            continue

        if novo_bloco_normal or novo_subtopico:
            _fechar_bloco()
            atual = [linha.lstrip() if novo_subtopico else linha]
            atual_aninhado = novo_subtopico
        else:
            atual.append(linha)

    if atual:
        _fechar_bloco()

    return "\n\n".join(blocos)


# --- Reposicionamento de imagens: mesma posição relativa do PDF original --
#
# No caminho legado do pymupdf4llm (`use_layout(False)` -- ver docstring
# do módulo), quando a página inteira cai numa única coluna de texto (o
# caso comum de apostila), a lib despeja TODAS as imagens da página no
# FIM do bloco de texto daquela página -- na ordem vertical correta entre
# si, mas sempre depois de todo o texto, mesmo quando a imagem aparece no
# meio da página no PDF original (ex: entre dois parágrafos). Resultado:
# a imagem sai associada ao texto ERRADO na nota (bug relatado: a imagem
# da página 2 apareceu grudada no texto do fim da página, não onde ela
# está de verdade no PDF).
#
# `_ancoras_de_imagem_por_pagina` acha, direto no PDF (não no Markdown já
# processado), o texto que vem imediatamente ANTES de cada imagem na
# ordem de leitura (topo -> baixo) de cada página. `_reposicionar_imagens_pagina`
# usa esse texto como âncora pra mover cada imagem, já depois do reflow de
# parágrafos, pro bloco certo -- logo depois do parágrafo/bullet que, no
# PDF, vem antes dela. Quando não acha a âncora de uma imagem no texto da
# página (heurística de correspondência textual -- pode falhar se o
# reflow reformatou demais o texto ao redor), essa imagem simplesmente
# fica onde já estava (comportamento anterior -- nunca perde a imagem, só
# deixa de reposicionar aquela).
_IMG_LINE_RE = re.compile(r"^!\[\]\(.+\.(?:png|jpe?g|webp)\)")


def _ancoras_de_imagem_por_pagina(
    doc: "pymupdf.Document",
    blocos_ocr_por_pagina: dict = None,
) -> dict:
    """Ver docstring da seção acima. Devolve `{pagina_0indexada: [texto_ou_None, ...]}`
    -- uma entrada por imagem da página, na mesma ordem (topo -> baixo)
    em que o pymupdf4llm extrai as imagens dessa página. Se a página não tiver
    texto nativo, usa os blocos de OCR já extraídos para ancorar as imagens."""
    resultado = {}
    blocos_ocr_por_pagina = blocos_ocr_por_pagina or {}
    for page in doc:
        blocks = page.get_text("dict").get("blocks", [])
        if not any(block.get("type") == 0 for block in blocks):
            blocks.extend(blocos_ocr_por_pagina.get(page.number, []))
        blocks = sorted(blocks, key=lambda b: (round(b["bbox"][1], 1), round(b["bbox"][0], 1)))
        ancoras = []
        texto_anterior = None
        for b in blocks:
            tipo = b.get("type")
            if tipo == 0:  # bloco de texto
                linhas = []
                for line in b.get("lines", []):
                    txt = "".join(span.get("text", "") for span in line.get("spans", []))
                    if txt.strip():
                        linhas.append(txt.strip())
                if linhas:
                    texto_anterior = linhas[-1]
            elif tipo == 1:  # bloco de imagem
                ancoras.append(texto_anterior)
        if ancoras:
            resultado[page.number] = ancoras
    return resultado


def _normalizar_para_match(texto: str) -> str:
    """Normaliza texto pra comparação tolerante (ver
    `_reposicionar_imagens_pagina`): minúsculo, sem marcação
    markdown/aspas nem marcador de tópico decorativo (ver
    `_MARCADOR_ESTRANHO_RE` -- o texto da âncora vem direto do PDF, com o
    glifo original, enquanto o bloco já processado teve esse mesmo glifo
    trocado por "-"; sem remover os dois lados, a âncora nunca bate),
    espaços colapsados."""
    texto = texto.lower()
    texto = re.sub(r"[*_`~\"“”‘’<>–—\-❖➢⮚➤➥▶►∙•●○▪◦\ue000]", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto


def _reposicionar_imagens_pagina(conteudo: str, ancoras: list) -> str:
    """Ver docstring da seção acima. `conteudo` é o texto de UMA página já
    reformatado (reflow já rodou -- blocos separados por linha em
    branco); `ancoras` vem de `_ancoras_de_imagem_por_pagina`."""
    if not ancoras:
        return conteudo

    blocos = conteudo.split("\n\n")
    indices_imagem = [i for i, b in enumerate(blocos) if _IMG_LINE_RE.match(b.strip())]
    if not indices_imagem:
        return conteudo

    imagens_bloco = [blocos[i] for i in indices_imagem]
    indices_imagem_set = set(indices_imagem)
    blocos_sem_imagem = [b for i, b in enumerate(blocos) if i not in indices_imagem_set]
    normalizados = [_normalizar_para_match(b) for b in blocos_sem_imagem]

    n = min(len(imagens_bloco), len(ancoras))
    cursor = 0
    por_posicao = {}  # posição (índice em blocos_sem_imagem) -> [imagens a inserir depois dela]
    imagens_posicionadas = set()
    for idx in range(n):
        ancora = ancoras[idx]
        if not ancora:
            continue
        alvo = _normalizar_para_match(ancora)
        if not alvo:
            continue
        pos_encontrada = None
        for j in range(cursor, len(normalizados)):
            if alvo in normalizados[j]:
                pos_encontrada = j
                break
        if pos_encontrada is None:
            continue
        por_posicao.setdefault(pos_encontrada, []).append(imagens_bloco[idx])
        imagens_posicionadas.add(idx)
        cursor = pos_encontrada

    if not imagens_posicionadas:
        return conteudo

    # imagens sem âncora reconhecida (não bateu com nada, ou nem tinha
    # âncora) continuam no fim da página, na ordem original entre si --
    # mesmo comportamento de antes do reposicionamento.
    imagens_restantes = [b for idx, b in enumerate(imagens_bloco) if idx not in imagens_posicionadas]

    saida = []
    for j, bloco in enumerate(blocos_sem_imagem):
        saida.append(bloco)
        if j in por_posicao:
            saida.extend(por_posicao[j])
    saida.extend(imagens_restantes)
    return "\n\n".join(saida)


def process_pages_apostila(md_text: str, ancoras_por_pagina: dict = None) -> tuple:
    """Equivalente a `process_pages` (cópia isolada -- ver nota no topo da
    seção), mas com quatro diferenças, todas exclusivas da apostila:
    1. normaliza marcadores de tópico exóticos (`_normalizar_marcadores_apostila`);
    2. extrai o corpo das notas de rodapé de dentro do fluxo de cada página
       e as renumera sequencialmente pro documento inteiro (evita colisão
       -- é comum a numeração de nota reiniciar a cada página no PDF);
    3. usa `reflow_paragraphs_apostila` (padrões extras de índice) em vez
       do `reflow_paragraphs` original;
    4. reposiciona cada imagem da página pro ponto do corpo onde ela
       aparece de verdade no PDF (ver `_reposicionar_imagens_pagina`) --
       só quando `ancoras_por_pagina` é passado (vem de
       `_ancoras_de_imagem_por_pagina`, calculado direto no PDF).
    Devolve (texto, notas) -- notas é uma lista [(numero_global, corpo), ...]
    na ordem em que apareceram, pra virar a lista de notas no fim do
    arquivo (ver `convert_pdf`)."""
    partes = _PAGE_SEP_RE.split(md_text)
    saida = []
    notas_globais = []
    contador_nota = {"n": 0}
    ancoras_por_pagina = ancoras_por_pagina or {}

    def _processar_trecho(conteudo: str, pagina_indice=None) -> str:
        conteudo = _normalizar_marcadores_apostila(conteudo)
        conteudo, notas_pagina = _extrair_notas_rodape_pagina(conteudo)
        conteudo = reflow_paragraphs_apostila(conteudo)
        if pagina_indice is not None and pagina_indice in ancoras_por_pagina:
            conteudo = _reposicionar_imagens_pagina(conteudo, ancoras_por_pagina[pagina_indice])
        for numero_original, corpo in notas_pagina:
            contador_nota["n"] += 1
            global_id = contador_nota["n"]
            conteudo = re.sub(
                rf"\[\^{re.escape(numero_original)}\]", f"[^{global_id}]", conteudo
            )
            notas_globais.append((global_id, corpo))
        return _restaurar_marcadores_topico(conteudo)

    for i in range(0, len(partes) - 1, 2):
        conteudo = partes[i]
        pagina_indice = int(partes[i + 1])

        m = _FOOTER_RE.search(conteudo)
        if m:
            pagina_num = m.group("pagenum")
            conteudo = _FOOTER_RE.sub("\n", conteudo, count=1)
        else:
            pagina_num = str(pagina_indice + 1)

        conteudo = _processar_trecho(conteudo, pagina_indice)

        saida.append(conteudo)
        saida.append(f"\n\n---\n*p. {pagina_num}*\n\n---\n\n")

    if len(partes) % 2 == 1 and partes[-1].strip():
        saida.append(_processar_trecho(partes[-1]))
    elif saida and saida[-1].startswith("\n\n---"):
        saida.pop()

    return "".join(saida), notas_globais


# --- Índice/sumário: envolve em [[wikilink]] e aninha pai/filho -----------
#
# As apostilas normalmente abrem com um bloco de índice/sumário: um título
# ("Sumário", às vezes sublinhado no PDF) seguido de subtítulos de seção
# (em negrito) e itens numerados (o "assunto de hoje"). No PDF, o item pai
# (título de seção) visualmente contém/envolve os itens filhos (numerados,
# uma indentação abaixo) -- é essa hierarquia visual que replicamos aqui
# como lista aninhada, com cada item virando um wikilink do Obsidian.
_INDICE_HEADING_RE = re.compile(r"^\*\*([^*]+?)\*\*:?$")
_INDICE_CHILD_DOTNUM_RE = re.compile(r"^(\d{1,3}(?:\.\s*\d{1,3})+)\s+(\S.*)$")
_INDICE_PARENT_NUM_RE = re.compile(r"^(\d{1,3})\.\s+([A-Za-zÀ-ÖØ-öø-ÿ].*)$")
_INDICE_CHILD_DASH_RE = re.compile(r"^(\d{1,3})\s*[-–—]\s*(\S.*)$")

# Não escaneia o documento inteiro atrás de índice -- só os primeiros
# blocos (tipicamente capa/logo + título antes do sumário de verdade). Sem
# esse limite, um trecho comum no MEIO do documento (ex: um subtítulo
# numerado de seção como "6.6- Competências...") poderia ser confundido
# com índice.
_INDICE_MAX_PREAMBULO = 25


def _classificar_linha_indice(linha: str):
    """Devolve (nivel, texto) se `linha` parece um item de índice/sumário
    -- nivel 0 = item "pai" (título de seção, ou "N. Texto" sem subitem),
    nivel 1 = item "filho" (subitem numerado, "N-texto" ou "N.M texto").
    Devolve None se não parecer índice."""
    linha = re.sub(r"^#{1,6}\s+", "", linha.strip())
    m = _INDICE_HEADING_RE.match(linha)
    if m:
        return (0, m.group(1).strip())
    m = _INDICE_CHILD_DOTNUM_RE.match(linha)
    if m:
        nivel = min(m.group(1).count("."), 3)
        numero = re.sub(r"\s*\.\s*", ".", m.group(1))
        return (nivel, f"{numero} {m.group(2)}".strip())
    m = _INDICE_PARENT_NUM_RE.match(linha)
    if m:
        return (0, f"{m.group(1)}. {m.group(2)}".strip())
    m = _INDICE_CHILD_DASH_RE.match(linha)
    if m:
        return (1, f"{m.group(1)}- {m.group(2)}".strip())
    return None


def _formatar_item_indice(texto: str, nivel: int = 0) -> str:
    texto = texto.strip().rstrip(":").strip()
    texto = re.sub(r"^\*\*|\*\*$", "", texto).strip()
    texto = re.sub(r"^#{1,6}\s*", "", texto).strip()
    if not texto:
        return ""
    nivel_heading = HEADING_BASE_LEVEL + min(
        max(nivel, 0), _HEADING_MAX_LEVEL - HEADING_BASE_LEVEL
    )
    return f"{'#' * nivel_heading} {texto}"


def _formatar_bloco_assunto(linhas: list) -> str:
    """Mesma ideia de `_processar_indice`, mas para o bloco de "assunto"
    (eco do sumário logo abaixo do frontmatter, gerado a partir de
    `detect_assunto`) -- também vira lista aninhada com wikilinks, em vez
    da lista plana de antes."""
    saida = []
    for linha in linhas:
        classificado = _classificar_linha_indice(linha) or (0, linha)
        nivel, texto = classificado
        heading = _formatar_item_indice(texto, nivel)
        if heading:
            saida.append(heading)
    return "\n".join(saida)


def _processar_indice(md_text: str) -> str:
    """Localiza o bloco de índice/sumário no início do documento e o
    reescreve como lista aninhada com wikilinks (ver docstring da seção).
    Se não achar nada parecido logo no início (dentro de
    `_INDICE_MAX_PREAMBULO` blocos) ou achar só 1 item reconhecível, não
    mexe em nada -- não força a marcação em documentos sem um índice
    claro no início."""
    blocos = md_text.split("\n\n")
    preambulo = []
    itens = []
    inicio_run = None
    fim_run = len(blocos)
    for idx, bloco in enumerate(blocos):
        linha = bloco.strip()
        if not linha:
            continue
        if inicio_run is None:
            if idx > _INDICE_MAX_PREAMBULO:
                return md_text
            classificado = _classificar_linha_indice(linha)
            if classificado is None:
                preambulo.append(bloco)
                continue
            inicio_run = idx
            itens.append(classificado)
            continue
        classificado = _classificar_linha_indice(linha)
        if classificado is None:
            fim_run = idx
            break
        itens.append(classificado)

    if inicio_run is None or len(itens) < 2:
        return md_text

    linhas_saida = []
    for nivel, texto in itens:
        heading = _formatar_item_indice(texto, nivel)
        if heading:
            linhas_saida.append(heading)

    partes = []
    if preambulo:
        partes.append("\n\n".join(preambulo))
    partes.append("\n".join(linhas_saida))
    resto = "\n\n".join(blocos[fim_run:])
    if resto.strip():
        partes.append(resto)
    return "\n\n".join(partes)


# --- Citação de lei em bloco próprio (callout de citação) ------------------
#
# A apostila cita o texto de artigo de lei/CF na íntegra o tempo todo, no
# meio do comentário da professora (ex: "**“Art. 102, CF/88:** Compete ao
# Supremo Tribunal Federal..."). Separar visualmente a citação do
# comentário ajuda a leitura -- vira um callout de citação do Obsidian.
_LEI_CITACAO_RE = re.compile(
    r"^[\-–—\*\s]*[\"“]?\**\s*Art\.?\s*\d", re.IGNORECASE
)

# Uma citação de lei raramente cabe num bloco só -- o caput vem num bloco,
# cada inciso/alínea/parágrafo vira o(s) bloco(s) seguinte(s) (são vários
# "parágrafos" markdown distintos, separados por linha em branco, mesmo
# fazendo parte da mesma citação). Continua o MESMO callout enquanto os
# blocos seguintes claramente continuarem a enumeração legal (inciso
# romano, alínea, parágrafo) -- pra citação não sair "cortada ao meio".
_LEI_CONTINUACAO_RE = re.compile(
    r"^(?:[IVXLCDM]+\s*[-–—]|[a-z]\)|§|Par[aá]grafo\s+[uú]nico)", re.IGNORECASE
)


def _destacar_citacoes_de_lei(md_text: str) -> str:
    """Envolve blocos que abrem com citação literal de artigo de lei/CF
    (ver `_LEI_CITACAO_RE`) num callout do Obsidian, separando-os do
    comentário ao redor -- inclusive os blocos seguintes de inciso/alínea/
    parágrafo que continuam a mesma citação (ver `_LEI_CONTINUACAO_RE`),
    pra não cortar a citação no meio. Heurística baseada só no início de
    cada bloco -- como o resto do módulo, pode deixar passar formatos
    atípicos."""
    blocos = md_text.split("\n\n")
    saida = []
    i = 0
    while i < len(blocos):
        bloco = blocos[i]
        primeira_linha = bloco.lstrip().split("\n", 1)[0]
        if _LEI_CITACAO_RE.match(primeira_linha) and not bloco.lstrip().startswith(">"):
            grupo = [bloco]
            i += 1
            while i < len(blocos):
                prox = blocos[i]
                prox_primeira = prox.lstrip().split("\n", 1)[0]
                if (
                    prox.lstrip().startswith(">")
                    or prox.lstrip().startswith("![")
                    or not _LEI_CONTINUACAO_RE.match(prox_primeira)
                ):
                    break
                grupo.append(prox)
                i += 1
            corpo = "\n>\n".join(
                "\n".join(f"> {linha}" if linha.strip() else ">" for linha in b.split("\n"))
                for b in grupo
            )
            # callout sem título próprio (nota o espaço depois de "]" --
            # é o que o Obsidian espera pra reconhecer o marcador mesmo
            # sem texto de título) -- "LEI" vira a primeira linha do
            # CORPO do callout, não o título, seguida do(s) artigo(s)
            # citado(s) na(s) linha(s) seguinte(s).
            saida.append(f"> [!quote] \n> LEI\n{corpo}")
            continue
        saida.append(bloco)
        i += 1
    return "\n\n".join(saida)


# --- Jurisprudência em bloco próprio (callout de alerta) --------------------
#
# A apostila também cita jurisprudência (acórdãos do STF/STJ, súmulas
# vinculantes, notícias de julgamento) no meio do comentário da
# professora -- igual acontece com citação de lei (ver seção acima), mas
# com um callout diferente (`[!warning]`), pra distinguir visualmente um
# do outro. Heurística baseada só no início do bloco: número de processo
# de uma classe processual do STF/STJ (ADI, ADPF, RE, HC...) ou "Súmula
# (Vinculante) N" -- como o resto do módulo, pode deixar passar formatos
# atípicos (ex: uma citação de jurisprudência parafraseada sem o número
# do processo no início do bloco).
_JURISPRUDENCIA_RE = re.compile(
    r"^[\-–—\*\s]*\**\s*(?:"
    r"(?:ADI|ADPF|ADC|ADO|ARE|RE|AgRg|HC|MS|RHC|REsp|AREsp|RMS|RvC)\s*\.?\s*n?\.?\s*\d|"
    r"S[uú]mula(?:\s+Vinculante)?\s*(?:n[ºo°]?\s*)?\d"
    r")",
    re.IGNORECASE,
)


def _destacar_jurisprudencia(md_text: str) -> str:
    """Envolve blocos que abrem com citação de jurisprudência (ver
    `_JURISPRUDENCIA_RE`) num callout `[!warning] Jurisprudência` --
    título fixo no próprio marcador (diferente da citação de lei, que
    não tem título -- ver `_destacar_citacoes_de_lei`), com o texto da
    jurisprudência na(s) linha(s) seguinte(s) dentro do callout. Não
    agrupa blocos seguintes como continuação (diferente da citação de
    lei): cada bloco de jurisprudência normalmente já é autocontido, e
    tentar juntar blocos seguintes arriscaria puxar comentário da
    professora pra dentro do callout por engano."""
    blocos = md_text.split("\n\n")
    saida = []
    for bloco in blocos:
        primeira_linha = bloco.lstrip().split("\n", 1)[0]
        if _JURISPRUDENCIA_RE.match(primeira_linha) and not bloco.lstrip().startswith(">"):
            citado = "\n".join(
                f"> {linha}" if linha.strip() else ">" for linha in bloco.split("\n")
            )
            saida.append(f"> [!warning] Jurisprudência\n{citado}")
            continue
        saida.append(bloco)
    return "\n\n".join(saida)


# --- Callout de atenção para avisos didáticos -------------------------------
#
# A professora sinaliza avisos importantes com palavras-gatilho (Obs.,
# Observação, Atenção, Cuidado, Não se esqueça...). Vira um callout do
# Obsidian, pra chamar atenção visualmente igual chama na aula.
_ALERTA_GATILHO_RE = re.compile(
    r"^\**\s*(?:OBS\.?\s*\d*|Observa[çc][ãa]o(?:\.?\s*\d*)?|Observa[çc][õo]es|"
    r"Aten[çc][ãa]o|Cuidado|N[ãa]o\s+se\s+esque[çc]a)\s*[:.\-]?",
    re.IGNORECASE,
)


def _aplicar_callouts_alerta(md_text: str) -> str:
    """Antepõe um callout "> [!attention] Atenção!" a qualquer bloco cujo
    início seja um aviso didático comum (ver `_ALERTA_GATILHO_RE`). O
    bloco inteiro (inclusive a palavra-gatilho) vira o corpo do callout."""
    blocos = md_text.split("\n\n")
    saida = []
    for bloco in blocos:
        primeira_linha = bloco.lstrip().split("\n", 1)[0]
        # a apostila costuma abrir o aviso com um "-" de tópico antes da
        # palavra-gatilho (ex: "-Obs.: Nosso documento...") -- descarta
        # marcação de tópico/ênfase antes de testar o gatilho.
        texto_sem_marcacao = primeira_linha.lstrip("*_-–— ")
        if _ALERTA_GATILHO_RE.match(texto_sem_marcacao) and not bloco.lstrip().startswith(">"):
            citado = "\n".join(
                f"> {linha}" if linha.strip() else ">" for linha in bloco.split("\n")
            )
            bloco = f"> [!attention] Atenção!\n{citado}"
        saida.append(bloco)
    return "\n\n".join(saida)


# --- Rede de segurança extra contra rodapé de site --------------------------
#
# `_FOOTER_RE` (seção compartilhada acima) cobre o caso comum -- número de
# página e domínio em duas linhas isoladas -- e roda ANTES do reflow, pelo
# motivo já documentado lá. Mas quando esse padrão de duas linhas não bate
# exatamente (ex: espaçamento diferente do PDF, ou o número/domínio já
# saem colados ao parágrafo anterior na extração), a URL do site sobra
# solta no meio do texto. Isso roda como último passo, sobre o documento
# inteiro já reformatado, como rede de segurança.
_SITE_TOKEN_PATTERN = (
    r"(?:(?:https?\s*:\s*/\s*/\s*)\S+|"
    r"(?:w\s*w\s*w[\s.]*[A-Za-z0-9][A-Za-z0-9/\-]*|"
    r"[A-Za-z0-9][A-Za-z0-9/\-]*)(?:\s*\.\s*[A-Za-z0-9][A-Za-z0-9/\-]*)+)"
)
_SITE_TOKEN_RE = re.compile(_SITE_TOKEN_PATTERN, re.IGNORECASE)
_SITE_ONLY_LINE_RE = re.compile(
    rf"(?i)^\s*{_SITE_TOKEN_PATTERN}\s*[.,;:!?)]*\s*$"
)
_FOOTER_PAGE_NUMBER_RE = re.compile(r"(?i)[ \t]\b\d{1,4}\b[ \t]+$")

# Uma única tabela de equivalências para notação ASCII comum em fórmulas.
# Aplicada somente a linhas com estrutura de proposição matemática.
_LOGICAL_SYMBOL_MAP = {
    "<->": "↔",
    "<=>": "⇔",
    "—»": "→",
    "—>": "→",
    "->": "→",
    "-o": "→",
    "=>": "⇒",
    "<>": "≠",
    "!=": "≠",
    "<=": "≤",
    ">=": "≥",
    "==": "≡",
    ">": "⇒",
    "=": "⇔",
    "se e somente se": "↔",
    "não está contido": "⊈",
    "não contém ou é igual": "⊉",
    "não pertence": "∉",
    "não contido": "⊄",
    "não contém": "⊅",
    "contido ou igual": "⊆",
    "contém ou igual": "⊇",
    "quantificador universal": "∀",
    "menor ou igual": "≤",
    "maior ou igual": "≥",
    "ou exclusivo": "⊻",
    "bicondicional": "↔",
    "equivalência": "⇔",
    "equivalente": "⇔",
    "implicação": "⇒",
    "condicional": "→",
    "conjunção": "∧",
    "disjunção": "∨",
    "negação": "¬",
    "subconjunto": "⊆",
    "superconjunto": "⊇",
    "contido": "⊂",
    "contém": "⊃",
    "pertence": "∈",
    "vazio": "∅",
    "união": "∪",
    "interseção": "∩",
    "não existe": "∄",
    "para todo": "∀",
    "diferente": "≠",
    "portanto": "∴",
    "porque": "∵",
    "infinito": "∞",
    "naturais": "ℕ",
    "inteiros": "ℤ",
    "racionais": "ℚ",
    "reais": "ℝ",
    "not exists": "∄",
    "forall": "∀",
    "exists": "∃",
    "~": "¬",
    "A": "∧",
    "^": "∧",
    "v": "∨",
    "V": "∨",
}
_LOGICAL_ASCII_RE = re.compile(r"<->|<=>|—»|—>|->|-o|=>|<>|!=|<=|>=|==|~")
_LOGICAL_WORD_OPERATORS_RE = re.compile(r"(?i)\b(not\s+exists|forall|exists)\b")
_LOGICAL_CONTEXT_WORD_RE = re.compile(
    r"(?i)\b(?:"
    + "|".join(
        re.escape(word)
        for word in sorted(
            (
                key for key in _LOGICAL_SYMBOL_MAP
                if len(key) > 1 and re.search(r"[A-Za-zÀ-ÿ]", key)
            ),
            key=len,
            reverse=True,
        )
    )
    + r")\b"
)
_LOGICAL_UNKNOWN_OPERATOR_RE = re.compile(r"(?<=[A-Za-z(~])\s*\?\s*(?=[A-Za-z)])")
_LOGICAL_CONNECTIVE_CONTEXT_RE = re.compile(
    r"(?:[A-Za-z0-9)]|~)(?:\s*\^\s*|\s+[vVA]\s+)(?:[A-Za-z(]|~)"
)
_LOGICAL_OPERATOR_CONTEXT_RE = re.compile(
    r"(?:[A-Za-z)]|~)\s*(?:<->|<=>|—»|—>|->|-o|=>|<>|!=|<=|>=|==|>|=)\s*(?:[A-Za-z(]|~)"
)
_LOGICAL_AND_A_CONTEXT_RE = re.compile(
    r"(?P<left>(?:(?<![A-Za-z0-9])[A-Za-z](?![A-Za-z0-9])|\)))\s+A\s+"
    r"(?P<right>(?:(?<![A-Za-z0-9])[A-Za-z](?![A-Za-z0-9])|\())"
)
_LOGICAL_V_CONTEXT_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<left>[A-Za-z]|\))\s+(?P<operator>[vV])\s+"
    r"(?P<right>[A-Za-z]|\()(?![A-Za-z0-9])"
)
_LOGICAL_CARET_CONTEXT_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<left>[A-Za-z]|\))\s*(?P<operator>\^)\s*"
    r"(?P<right>[A-Za-z]|\()(?![A-Za-z0-9])"
)
_LOGICAL_SINGLE_OPERATOR_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<left>[A-Za-z]|\))\s*(?P<operator>[>=])\s*"
    r"(?P<right>[A-Za-z]|\()(?![A-Za-z0-9])"
)
_LOGICAL_LINE_CONTEXT_RE = re.compile(
    r"(?:<->|<=>|—»|—>|->|-o|=>|<>|!=|<=|>=|==|[¬~≡↔→⇒⇔∧∨⊻⊕∈∉⊂⊆⊄⊈⊃⊇⊅⊉∅∪∩∀∃∄≠≤≥∴∵∞ℕℤℚℝ])"
)


def _normalizar_simbolos_logicos(md_text: str) -> str:
    """Normaliza notação ASCII de fórmulas sem substituir letras em prosa."""
    saida = []
    for linha in md_text.splitlines(keepends=True):
        conteudo = linha.rstrip("\r\n")
        terminador = linha[len(conteudo):]
        if conteudo.lstrip().startswith("|"):
            saida.append(conteudo + terminador)
            continue
        tachados = []

        def _proteger_tachado(match: re.Match) -> str:
            tachados.append(match.group(0))
            return f"\x00{len(tachados) - 1}\x00"

        conteudo = re.sub(r"~~(?=\S).*?\S~~", _proteger_tachado, conteudo)
        glifos_privados = []
        for indice, caractere in enumerate(conteudo):
            anterior = conteudo[:indice].rstrip()
            proximo = conteudo[indice + 1:].lstrip()
            if (
                unicodedata.category(caractere) == "Co"
                and anterior and proximo
                and anterior[-1] in "ABCDEFGHIJKLMNOPQRSTUVWXYZ)"
                and proximo[0] in "ABCDEFGHIJKLMNOPQRSTUVWXYZ("
            ):
                glifos_privados.append(indice)
        contexto_logico = bool(
            _LOGICAL_LINE_CONTEXT_RE.search(conteudo)
            or _LOGICAL_CONNECTIVE_CONTEXT_RE.search(conteudo)
            or _LOGICAL_OPERATOR_CONTEXT_RE.search(conteudo)
            or _LOGICAL_AND_A_CONTEXT_RE.search(conteudo)
            or _LOGICAL_V_CONTEXT_RE.search(conteudo)
            or _LOGICAL_CARET_CONTEXT_RE.search(conteudo)
            or _LOGICAL_UNKNOWN_OPERATOR_RE.search(conteudo)
            or glifos_privados
            or _LOGICAL_CONTEXT_WORD_RE.search(conteudo)
            or re.search(r"\b(?:lógica|proposição)\b", conteudo, re.I)
        )
        if contexto_logico:
            operadores_desconhecidos = list(_LOGICAL_UNKNOWN_OPERATOR_RE.finditer(conteudo))
            pistas = list(_LOGICAL_CONTEXT_WORD_RE.finditer(conteudo))
            pista = max(pistas, key=lambda match: len(match.group(0)), default=None)
            operador_semantico = _LOGICAL_SYMBOL_MAP.get(pista.group(0).lower()) if pista else None
            desconhecidos = [
                (match.start(), match.end(), f" {operador_semantico} ")
                for match in operadores_desconhecidos
            ] + [
                (indice, indice + 1, operador_semantico)
                for indice in glifos_privados
            ]
            if operador_semantico and len(desconhecidos) == 1:
                inicio, fim, substituto = desconhecidos[0]
                conteudo = conteudo[:inicio] + substituto + conteudo[fim:]
            conteudo = _LOGICAL_ASCII_RE.sub(
                lambda m: _LOGICAL_SYMBOL_MAP[m.group(0)], conteudo
            )
            conteudo = _LOGICAL_SINGLE_OPERATOR_RE.sub(
                lambda m: (
                    f"{m.group('left')} {_LOGICAL_SYMBOL_MAP[m.group('operator')]} "
                    f"{m.group('right')}"
                ),
                conteudo,
            )
            conteudo = _LOGICAL_WORD_OPERATORS_RE.sub(
                lambda m: _LOGICAL_SYMBOL_MAP[m.group(0).lower()], conteudo
            )
            conteudo = _LOGICAL_AND_A_CONTEXT_RE.sub(
                lambda m: f"{m.group('left')} {_LOGICAL_SYMBOL_MAP['A']} {m.group('right')}", conteudo
            )
            conteudo = _LOGICAL_V_CONTEXT_RE.sub(
                lambda m: (
                    f"{m.group('left')} {_LOGICAL_SYMBOL_MAP[m.group('operator')]} "
                    f"{m.group('right')}"
                ),
                conteudo,
            )
            conteudo = _LOGICAL_CARET_CONTEXT_RE.sub(
                lambda m: (
                    f"{m.group('left')} {_LOGICAL_SYMBOL_MAP[m.group('operator')]} "
                    f"{m.group('right')}"
                ),
                conteudo,
            )
        for indice, tachado in enumerate(tachados):
            conteudo = conteudo.replace(f"\x00{indice}\x00", tachado)
        saida.append(conteudo + terminador)
    return "".join(saida)


def _remover_sites_residuais(texto: str) -> str:
    """Remove URLs isoladas e rodapés de site repetidos, preservando links
    incorporados ao texto corrido do corpo."""
    linhas = texto.splitlines()
    ocorrencias = {}
    pagina_atual = 0
    for linha in linhas:
        if linha.lstrip().startswith("|"):
            continue
        if re.fullmatch(r"\*p\. \d+\*", linha.strip()):
            pagina_atual += 1
        for match in _SITE_TOKEN_RE.finditer(linha):
            normalizado = re.sub(r"\s*\.\s*", ".", match.group(0)).lower().rstrip(".,;:!?)")
            ocorrencias.setdefault(normalizado, set()).add(pagina_atual)

    resultado = []
    for linha in linhas:
        if linha.lstrip().startswith("|"):
            resultado.append(linha)
            continue
        if _SITE_ONLY_LINE_RE.fullmatch(linha):
            continue
        estado = {"remover_numero_pagina": False}

        def remover_rodape(match):
            inicio, fim = match.span()
            prefixo = linha[:inicio]
            sufixo = linha[fim:]
            normalizado = re.sub(r"\s*\.\s*", ".", match.group(0)).lower().rstrip(".,;:!?)")
            tem_numero_pagina = bool(_FOOTER_PAGE_NUMBER_RE.search(prefixo))
            eh_final_de_linha = not sufixo.strip(" \t.,;:!?)")
            repetido = len(ocorrencias.get(normalizado, set())) >= 3
            if eh_final_de_linha and (tem_numero_pagina or repetido):
                estado["remover_numero_pagina"] = tem_numero_pagina
                return ""
            return match.group(0)

        limpa = _SITE_TOKEN_RE.sub(remover_rodape, linha)
        if estado["remover_numero_pagina"]:
            limpa = _FOOTER_PAGE_NUMBER_RE.sub("", limpa)
        resultado.append(limpa)
    final = "\n".join(resultado)
    return final + ("\n" if texto.endswith("\n") else "")


def _sites_no_rodape_pdf(doc: "pymupdf.Document") -> dict:
    """Encontra domínios na faixa inferior das páginas nativas do PDF."""
    sites_por_pagina = {}
    for page in doc:
        sites = []
        limite_superior_rodape = page.rect.height * 0.86
        for bloco in page.get_text("dict").get("blocks", []):
            for linha in bloco.get("lines", []):
                if linha.get("bbox", (0, 0, 0, 0))[1] < limite_superior_rodape:
                    continue
                texto_linha = "".join(span.get("text", "") for span in linha.get("spans", []))
                for match in _SITE_TOKEN_RE.finditer(texto_linha):
                    site = re.sub(r"\s*\.\s*", ".", match.group(0)).lower().strip(".,;:!?) ")
                    sites.append(site)
        if sites:
            sites_por_pagina[page.number] = sites
    return sites_por_pagina


def _remover_sites_por_posicao(md_text: str, sites_por_pagina: dict) -> str:
    """Remove a ocorrência do domínio extraído na faixa de rodapé da página."""
    if not sites_por_pagina:
        return md_text
    partes = _PAGE_SEP_RE.split(md_text)
    for i in range(0, len(partes) - 1, 2):
        pagina = int(partes[i + 1])
        conteudo = partes[i]
        for site in sites_por_pagina.get(pagina, []):
            ocorrencias = [
                match for match in _SITE_TOKEN_RE.finditer(conteudo)
                if re.sub(r"\s*\.\s*", ".", match.group(0)).lower().strip(".,;:!?) ") == site
            ]
            if ocorrencias:
                match = ocorrencias[-1]
                conteudo = conteudo[:match.start()] + conteudo[match.end():]
        partes[i] = conteudo
    saida = []
    for i, parte in enumerate(partes):
        if i % 2 == 1:
            saida.append(f"\n\n--- end of page={parte} ---\n\n")
        else:
            saida.append(parte)
    return "".join(saida)


def _ocr_figura(image: Image.Image) -> str:
    """Executa OCR de busca sobre a figura, sem promovê-lo a texto principal."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    try:
        result = subprocess.run(
            ["tesseract", "stdin", "stdout", "-l", "por+eng", "--psm", "6"],
            input=buffer.getvalue(), capture_output=True, timeout=45, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.decode("utf-8", errors="replace").strip() if result.returncode == 0 else ""


def _limpar_ocr_site(texto: str) -> str:
    linhas = []
    for linha in texto.splitlines():
        if _SITE_ONLY_LINE_RE.fullmatch(linha.strip()):
            continue
        if re.fullmatch(r"(?i)\s*(?:G\s*7\s*)?JUR[ií]DICO\s*", linha):
            continue
        limpa = re.sub(
            r"(?i)\bG\s*7\s*JUR[ií]DICO\b", "", linha
        )
        limpa = _SITE_TOKEN_RE.sub("", limpa).strip()
        if limpa:
            linhas.append(limpa)
    return "\n".join(linhas)


def _texto_linha_pdf(line: dict, titulo_state: dict) -> str:
    spans = line.get("spans", [])
    texto = "".join(span.get("text", "") for span in spans).strip()
    if not texto:
        return ""
    spans_visiveis = [span for span in spans if span.get("text", "").strip()]
    negrito = bool(spans_visiveis) and all(span.get("flags", 0) & 16 for span in spans_visiveis)
    if not negrito:
        return texto

    titulo = re.sub(r"\s+", " ", texto).strip()
    titulo = re.sub(r"^#{1,6}\s*", "", titulo).strip()
    normalizado = titulo.casefold()
    if normalizado == "roteiro de aula":
        titulo_state["nivel"] = 0
        return _formatar_item_indice(titulo, 0)
    numeracao = re.match(r"^(\d+(?:\.\d+)*)(?:[.)])?\s+", titulo)
    if numeracao:
        if re.search(r"[.!?]$", titulo):
            return f"**{texto}**"
        profundidade = numeracao.group(1).count(".")
        titulo_state["nivel"] = min(profundidade, 3)
        return _formatar_item_indice(titulo, titulo_state["nivel"])
    if re.fullmatch(r"bloco\s+\d+", normalizado):
        nivel = min((titulo_state.get("nivel") or 0) + 1, 3)
        return _formatar_item_indice(titulo, nivel)
    if titulo_state.get("nivel") is not None and (
        normalizado.startswith("lógica proposicional")
        or titulo.isupper()
    ):
        nivel = min(titulo_state["nivel"] + 1, 3)
        return _formatar_item_indice(titulo, nivel)
    return f"**{texto}**"


def _extrair_layout_pdf(
    input_path: str,
    img_dir: str,
    stem: str,
    crop_watermark: bool = False,
    derecho_profile: bool = False,
) -> tuple:
    """Extrai todas as páginas por geometria, sem depender da ordem do Markdown."""
    os.makedirs(img_dir, exist_ok=True)
    with pymupdf.open(input_path) as doc:
        page_data = []
        all_lines = []
        footer_numbers = []

        for page in doc:
            native = page.get_text("dict")
            native_text = "".join(
                span.get("text", "")
                for block in native.get("blocks", [])
                if block.get("type") == 0
                for line in block.get("lines", [])
                for span in line.get("spans", [])
            )
            text_page = None
            page_dict = native
            if len(native_text.strip()) < _OCR_MIN_CHARS:
                try:
                    text_page = page.get_textpage_ocr(
                        full=True, language="por+eng", dpi=_OCR_DPI
                    )
                except Exception as exc:  # noqa: BLE001 - report actionable OCR setup failures
                    if "Tesseract is not installed" in str(exc):
                        raise RuntimeError(
                            "OCR é necessário para páginas sem texto extraível, mas o "
                            "Tesseract não está instalado. A conversão foi interrompida."
                        ) from exc
                    raise RuntimeError(
                        f"Falha no OCR da página {page.number + 1}: {exc}"
                    ) from exc
                ocr_dict = page.get_text("dict", textpage=text_page)
                page_dict = {
                    "blocks": [
                        block for block in native.get("blocks", [])
                        if block.get("type") == 1
                    ] + [
                        block for block in ocr_dict.get("blocks", [])
                        if block.get("type") == 0
                    ]
                }

            images = [
                block for block in page_dict.get("blocks", [])
                if block.get("type") == 1 and block.get("image")
            ]
            image_groups = _agrupar_blocos_imagem(images)
            figures = [
                {
                    "bbox": (
                        min(block["bbox"][0] for block in group),
                        min(block["bbox"][1] for block in group),
                        max(block["bbox"][2] for block in group),
                        max(block["bbox"][3] for block in group),
                    ),
                    "kind": "raster",
                    "blocks": group,
                }
                for group in image_groups
            ]
            tables = _extrair_tabelas_nativas(page, text_page is None)
            figures.extend(_figuras_vetoriais_da_pagina(page, figures))
            figures = [
                figure for figure in figures
                if not any(
                    _bbox_overlap_ratio(figure["bbox"], table["bbox"]) >= 0.60
                    for table in tables
                )
            ]

            lines = []
            for block in page_dict.get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    info = _informacao_linha_pdf(line, page.rect)
                    if not info["text"] or info["bbox"][2] - info["bbox"][0] <= 2:
                        continue
                    info["page"] = page.number
                    info["native"] = text_page is None
                    lines.append(info)

            lines = _combinar_fragmentos_numericos(lines)
            page_data.append(
                {
                    "page": page,
                    "lines": lines,
                    "figures": figures,
                    "tables": tables,
                    "ocr_used": text_page is not None,
                }
            )
            all_lines.extend(lines)

        table_groups = tabelas.unir_tabelas_entre_paginas(
            [data["tables"] for data in page_data]
        )
        table_image_count = 0
        for data in page_data:
            data["render_tables"] = []
        for table_number, table_group in enumerate(table_groups, start=1):
            compact_right_table = derecho_profile and any(
                len(part["rows"]) <= 3 and part["column_count"] >= 4
                for part in table_group["parts"]
            )
            if compact_right_table:
                table_group["fallback_image"] = True
            if table_group["fallback_image"]:
                table_group["markdown"] = _renderizar_grupo_tabela_como_imagem(
                    table_group, page_data, img_dir, stem, table_number
                )
                table_image_count += len(table_group["parts"])
            page_data[table_group["render_page"]]["render_tables"].append(
                table_group
            )

        repeated_headers = _cabecalhos_corridos_repetidos(page_data)
        for data in page_data:
            page = data["page"]
            body_lines = []
            footer_text = []
            for line in data["lines"]:
                if line["bbox"][1] >= page.rect.height * 0.93:
                    footer_text.append(line["text"])
                elif (
                    line["bbox"][1] <= page.rect.height * 0.08
                    and _normalizar_linha_corrida(line["text"]) in repeated_headers
                ):
                    continue
                elif any(
                    _bbox_overlap_ratio(line["bbox"], table["bbox"])
                    >= _FIGURE_TEXT_OVERLAP_RATIO
                    for table in data["tables"]
                ):
                    continue
                else:
                    body_lines.append(line)
            footer_digits = [
                int(value)
                for text in footer_text
                for value in re.findall(r"(?<!\w)\d{1,3}(?!\w)", text)
            ]
            footer_numbers.append(footer_digits[-1] if footer_digits else None)
            data["lines"] = body_lines

        use_printed_pages = (
            all(number is not None for number in footer_numbers)
            and all(
                current > previous
                for previous, current in zip(footer_numbers, footer_numbers[1:])
            )
        )

        heading_lines = [
            line
            for data in page_data
            for line in data["lines"]
            if not any(
                _bbox_overlap_ratio(line["bbox"], figure["bbox"])
                >= _FIGURE_TEXT_OVERLAP_RATIO
                for figure in data["figures"]
            ) and not any(
                _bbox_overlap_ratio(line["bbox"], table["bbox"])
                >= _FIGURE_TEXT_OVERLAP_RATIO
                for table in data["tables"]
            )
        ]
        heading_styles, median_heading_size = _rank_heading_styles(heading_lines)
        image_index = 0
        rendered_pages = []
        for data in page_data:
            page = data["page"]
            page_number = page.number + 1
            lines = data["lines"]
            figures = data["figures"]
            in_summary = False

            visible_lines = []
            for line in lines:
                contained = [
                    figure for figure in figures
                    if _bbox_overlap_ratio(line["bbox"], figure["bbox"])
                    >= _FIGURE_TEXT_OVERLAP_RATIO
                ]
                if contained:
                    line["figure"] = contained[0]
                else:
                    visible_lines.append(line)

            list_x_positions = [
                line["bbox"][0] for line in visible_lines if line.get("list_marker")
            ]
            list_origin = min(list_x_positions, default=0.0)
            events = [
                {
                    "y": line["bbox"][1],
                    "x": line["bbox"][0],
                    "kind": "text",
                    "line": line,
                }
                for line in visible_lines
            ]
            events.extend(
                {
                    "y": figure["bbox"][1],
                    "x": figure["bbox"][0],
                    "kind": "figure",
                    "figure": figure,
                }
                for figure in figures
            )
            events.extend(
                {
                    "y": table["render_y"],
                    "x": table["parts"][-1]["bbox"][0],
                    "kind": "table",
                    "table": table,
                }
                for table in data["render_tables"]
            )
            events = _ordenar_eventos_geometricos(events)
            blocks = []
            current_lines = []

            def flush_paragraph() -> None:
                if current_lines:
                    blocks.append(_juntar_linhas_geometricamente(current_lines))
                    current_lines.clear()

            for event in events:
                if event["kind"] == "figure":
                    flush_paragraph()
                    figure = event["figure"]
                    image_index += 1
                    filename = f"{stem}-img{image_index}-pg{page_number}.png"
                    figure_text = [
                        line["text"]
                        for line in lines
                        if line.get("figure") is figure
                    ]
                    ocr_text = _salvar_figura_posicional(
                        page, figure, filename, img_dir, crop_watermark,
                        "\n".join(figure_text),
                        data["ocr_used"],
                    )
                    embed = f"![[{filename}]]"
                    ocr_text = _limpar_ocr_site(ocr_text)
                    if len(re.sub(r"\s+", "", ocr_text)) >= 4:
                        callout = "> [!note]- Texto OCR (aproximado)\n" + "\n".join(
                            f"> {line.strip()}"
                            for line in ocr_text.splitlines()
                            if line.strip()
                        )
                        embed += "\n\n" + callout
                    blocks.append(embed)
                    continue
                if event["kind"] == "table":
                    flush_paragraph()
                    blocks.append(event["table"]["markdown"])
                    continue

                line = event["line"]
                text = re.sub(r"\s+", " ", line["text"]).strip()
                is_summary_title = text.casefold() == "sumário"
                is_summary_item = in_summary and bool(_OUTLINE_RE.match(text))
                if is_summary_title:
                    in_summary = True
                elif in_summary and not is_summary_item:
                    in_summary = False
                heading_level = (
                    None
                    if is_summary_item
                    else _nivel_heading_posicional(
                        line, heading_styles, median_heading_size
                    )
                )
                if heading_level is not None:
                    flush_paragraph()
                    heading_text = _formatar_linha_posicional(line, list_origin)
                    heading_text = re.sub(r"\*\*(.*?)\*\*", r"\1", heading_text)
                    heading_text = re.sub(r"(?<!\*)\*(?!\*)(.*?)\*(?!\*)", r"\1", heading_text)
                    blocks.append(f"{'#' * heading_level} {heading_text.strip()}")
                    continue

                formatted = _formatar_linha_posicional(line, list_origin)
                if not current_lines or not _linhas_formam_paragrafo(
                    current_lines[-1], line, page.rect.width
                ):
                    flush_paragraph()
                current_lines.append({**line, "formatted": formatted})

            flush_paragraph()
            content = "\n\n".join(block for block in blocks if block.strip())
            content = re.sub(r"(?m)^--- end of page=\d+ ---\s*$", "", content)
            printed_page = (
                footer_numbers[page.number]
                if use_printed_pages else page_number
            )
            rendered_pages.append(
                f"{content}\n\n---\n*p. {printed_page}*\n\n---\n\n"
            )

    return "".join(rendered_pages), image_index + table_image_count


def _extrair_tabelas_nativas(page, has_native_text: bool) -> list:
    if not has_native_text:
        return []
    return tabelas.extrair_tabelas_pagina(page)


def _renderizar_grupo_tabela_como_imagem(
    table_group: dict,
    page_data: list[dict],
    img_dir: str,
    stem: str,
    table_number: int,
) -> str:
    links = []
    for part in table_group["parts"]:
        page_index = part["page_index"]
        page = page_data[page_index]["page"]
        rect = pymupdf.Rect(part["bbox"])
        rect.x0 = max(page.rect.x0, rect.x0 - 3)
        rect.y0 = max(page.rect.y0, rect.y0 - 3)
        rect.x1 = min(page.rect.x1, rect.x1 + 3)
        rect.y1 = min(page.rect.y1, rect.y1 + 3)
        filename = f"{stem}-table{table_number}-pg{page_index + 1}.png"
        page.get_pixmap(
            matrix=pymupdf.Matrix(
                tabelas.TABLE_RENDER_DPI_SCALE,
                tabelas.TABLE_RENDER_DPI_SCALE,
            ),
            clip=rect,
            alpha=False,
        ).save(os.path.join(img_dir, filename))
        links.append(f"![[{filename}]]")

    quoted_text = "\n".join(
        f"> {line}" if line else ">"
        for line in table_group["fallback_text"].splitlines()
    )
    return "\n\n".join(links) + "\n\n> [!note]- Texto da tabela\n" + quoted_text


def _informacao_linha_pdf(line: dict, page_rect) -> dict:
    spans = [span for span in line.get("spans", []) if span.get("text", "").strip()]
    bbox = tuple(line.get("bbox", (0, 0, 0, 0)))
    sizes = [float(span.get("size", 0)) for span in spans if span.get("size")]
    size = max(sizes, default=max(1.0, bbox[3] - bbox[1]))
    text = _texto_spans_geometricamente(spans, size)
    bold_spans = [bool(span.get("flags", 0) & 16) for span in spans]
    bold = bool(bold_spans) and all(bold_spans)
    italic = bool(bold_spans) and all(span.get("flags", 0) & 2 for span in spans)
    center = (bbox[0] + bbox[2]) / 2
    centered = abs(center - page_rect.width / 2) <= page_rect.width * 0.03
    list_marker, list_text_x = _detectar_marcador_posicional(spans, text, bbox)
    return {
        "bbox": bbox,
        "page_width": page_rect.width,
        "page_height": page_rect.height,
        "text": text,
        "size": size,
        "height": max(1.0, bbox[3] - bbox[1]),
        "bold": bold,
        "any_bold": any(bold_spans),
        "italic": italic,
        "centered": centered,
        "list_marker": list_marker,
        "list_text_x": list_text_x,
        "spans": spans,
    }


def _texto_spans_geometricamente(spans: list, size: float) -> str:
    text = []
    previous_span = None
    for span in spans:
        value = span.get("text", "")
        if not value:
            continue
        if previous_span is not None:
            previous_bbox = previous_span.get("bbox", (0, 0, 0, 0))
            current_bbox = span.get("bbox", (0, 0, 0, 0))
            gap = current_bbox[0] - previous_bbox[2]
            if (
                gap > max(0.5, size * 0.06)
                and text
                and not text[-1].endswith((" ", "\t"))
                and not value.startswith((" ", "\t"))
            ):
                text.append(" ")
        text.append(value)
        previous_span = span
    return "".join(text).strip()


def _combinar_fragmentos_numericos(lines: list) -> list:
    ordered = sorted(lines, key=lambda line: (line["bbox"][1], line["bbox"][0]))
    combined = []
    index = 0
    while index < len(ordered):
        line = ordered[index]
        if (
            re.fullmatch(r"\d{1,3}[.)]", line["text"])
            and index + 1 < len(ordered)
        ):
            following = ordered[index + 1]
            same_baseline = abs(line["bbox"][1] - following["bbox"][1]) <= 2.0
            horizontal_gap = following["bbox"][0] - line["bbox"][2]
            if same_baseline and 0 <= horizontal_gap <= 24:
                combined.append({
                    **following,
                    "text": f"{line['text']} {following['text']}",
                    "bbox": (
                        line["bbox"][0],
                        min(line["bbox"][1], following["bbox"][1]),
                        following["bbox"][2],
                        max(line["bbox"][3], following["bbox"][3]),
                    ),
                    "spans": line["spans"] + following["spans"],
                })
                index += 2
                continue
        combined.append(line)
        index += 1
    return combined


_POSITIONAL_BULLET_RE = re.compile(
    r"^[ \t]*(?P<marker>▪|❖|➢|✓|●|○|•|◦|=|\*\s*\||\*|\+|QO\))"
    r"(?:[ \t]+|$)(?P<text>\S.*)$",
    re.IGNORECASE,
)


def _detectar_marcador_posicional(spans: list, text: str, bbox: tuple) -> tuple:
    visible = [span for span in spans if span.get("text", "").strip()]
    if len(visible) >= 2:
        marker = visible[0].get("text", "").strip()
        following = visible[1].get("text", "").strip()
        gap = visible[1].get("bbox", (0, 0, 0, 0))[0] - visible[0].get(
            "bbox", (0, 0, 0, 0)
        )[2]
        font = str(visible[0].get("font", "")).casefold()
        is_courier_o = marker.casefold() == "o" and "courier" in font
        recognized = marker in {"▪", "❖", "➢", "✓", "●", "○", "•", "◦", "=", "*", "+"}
        if len(marker) <= 3 and gap >= 6 and following and (recognized or is_courier_o):
            return marker, visible[1]["bbox"][0]
    if re.match(r"^o\s+\S", text) and any(
        "courier" in str(span.get("font", "")).casefold() for span in visible
    ):
        first = visible[0].get("bbox", bbox)
        return "o", first[0] + 18
    match = _POSITIONAL_BULLET_RE.match(text)
    if match:
        marker = match.group("marker")
        return marker, bbox[0] + 18
    return None, None


def _bbox_overlap_ratio(inner: tuple, outer: tuple) -> float:
    x0 = max(inner[0], outer[0])
    y0 = max(inner[1], outer[1])
    x1 = min(inner[2], outer[2])
    y1 = min(inner[3], outer[3])
    intersection = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    area = max(0.0, inner[2] - inner[0]) * max(0.0, inner[3] - inner[1])
    return intersection / area if area else 0.0


def _normalizar_linha_corrida(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def _cabecalhos_corridos_repetidos(page_data: list) -> set:
    por_texto = {}
    for data in page_data:
        page = data["page"]
        vistos = set()
        for line in data["lines"]:
            if line["bbox"][1] > page.rect.height * 0.08:
                continue
            normalizado = _normalizar_linha_corrida(line["text"])
            if len(normalizado) >= 4:
                vistos.add(normalizado)
        for text in vistos:
            por_texto[text] = por_texto.get(text, 0) + 1
    return {text for text, count in por_texto.items() if count >= 3}


def _figuras_vetoriais_da_pagina(page, raster_figures: list) -> list:
    figures = []
    drawings = page.get_drawings()
    page_area = page.rect.get_area()
    figure_drawings = []
    for drawing in drawings:
        rect = pymupdf.Rect(drawing["rect"])
        area = rect.get_area()
        fill = drawing.get("fill")
        colored_fill = (
            fill is not None
            and min(fill[:3]) < 0.96
            and max(fill[:3]) - min(fill[:3]) >= 0.08
        )
        large_frame = (
            area >= page_area * _VECTOR_FIGURE_MIN_AREA_RATIO
            and area < page_area * 0.50
            and any(item and item[0] == "re" for item in drawing.get("items", []))
        )
        if area < page_area * 0.50 and (colored_fill or large_frame):
            figure_drawings.append(drawing)
    if not figure_drawings:
        return figures

    for cluster in page.cluster_drawings(
        drawings=figure_drawings,
        x_tolerance=_LAYOUT_Y_TOLERANCE,
        y_tolerance=_LAYOUT_Y_TOLERANCE,
    ):
        rect = pymupdf.Rect(cluster)
        area = rect.get_area()
        if (
            not area
            or area < page_area * _VECTOR_FIGURE_MIN_AREA_RATIO
            or rect.width < page.rect.width * _VECTOR_FIGURE_MIN_WIDTH_RATIO
        ):
            continue
        rect_tuple = tuple(rect)
        if any(_bbox_overlap_ratio(rect_tuple, image["bbox"]) >= 0.25 for image in raster_figures):
            continue
        if any(_bbox_overlap_ratio(rect_tuple, other["bbox"]) >= 0.80 for other in figures):
            continue
        figures.append({"bbox": rect_tuple, "kind": "vector", "blocks": []})
    return figures


def _rank_heading_styles(all_lines: list) -> tuple:
    sizes = sorted(line["size"] for line in all_lines if line["text"].strip())
    median_size = sizes[len(sizes) // 2] if sizes else 0
    candidates = [
        line for line in all_lines
        if _es_titulo_posicional(line, median_size)
    ]
    styles = {
        _heading_style_key(line): line
        for line in candidates
    }
    ranked = sorted(
        styles,
        key=lambda key: (-key[0], -int(key[1]), -int(key[2])),
    )
    return (
        {
            key: HEADING_BASE_LEVEL + min(
                index, _HEADING_MAX_LEVEL - HEADING_BASE_LEVEL
            )
            for index, key in enumerate(ranked)
        },
        median_size,
    )


def _heading_style_key(line: dict) -> tuple:
    return (round(line["size"] * 2) / 2, line["bold"], line["centered"])


_NUMERIC_HEADING_RE = re.compile(
    r"^(?P<number>\d+(?:\.\d+)*)(?:\.(?=\s)|\s*[-–—)]\s*|\s+)(?P<title>\S.*)$"
)
_LETTER_HEADING_RE = re.compile(
    r"^(?P<number>[A-Z](?:\.\d+)?)\)\s+(?P<title>\S.*)$"
)


def _es_titulo_posicional(line: dict, median_size: float) -> bool:
    text = re.sub(r"\s+", " ", line["text"]).strip()
    if (
        len(text) < 3
        or len(text) > 120
        or re.search(r"[.!?:;]$", text)
        or re.search(r"[.!?]\s+\w{1,3}$", text)
    ):
        return False
    if re.fullmatch(r"(?i)bloco\s+\d+", text):
        return True
    if _NUMERIC_HEADING_RE.match(text):
        if len(text) > 85:
            return False
        return line["bold"] or text.isupper() or line["size"] >= median_size * 1.12
    if _LETTER_HEADING_RE.match(text):
        return line["bold"] or text.isupper() or line["size"] >= median_size * 1.12
    if line["any_bold"] and line["bold"]:
        return True
    if line.get("page") != 0 and text.isupper() and len(text) <= 60:
        return (
            line.get("centered", False)
            or line["size"] >= median_size * 1.08
            or line["height"] >= max(_OCR_HEADING_MIN_HEIGHT, median_size * 1.08)
        )
    return bool(
        line.get("page") != 0
        and text.isupper()
        and line["centered"]
        and (
            line["size"] >= median_size * 1.08
            or line["height"] >= max(_OCR_HEADING_MIN_HEIGHT, median_size * 1.08)
        )
    )


def _nivel_heading_posicional(
    line: dict, heading_styles: dict, median_size: float
):
    text = re.sub(r"\s+", " ", line["text"]).strip()
    if text.casefold() in {"sumário", "roteiro de aula"}:
        return HEADING_BASE_LEVEL
    if (
        text.isupper()
        and len(text) >= 12
        and line["bbox"][1] <= line.get("page_height", 0) * 0.18
        and line["bbox"][2] - line["bbox"][0] <= line.get("page_width", 0) * 0.85
    ):
        return HEADING_BASE_LEVEL
    if not _es_titulo_posicional(line, median_size):
        return None
    if re.fullmatch(r"(?i)bloco\s+\d+", text):
        return HEADING_BASE_LEVEL
    numeric = _NUMERIC_HEADING_RE.match(text)
    if numeric:
        depth = numeric.group("number").count(".")
        return min(HEADING_BASE_LEVEL + depth, _HEADING_MAX_LEVEL)
    letter = _LETTER_HEADING_RE.match(text)
    if letter:
        return min(
            HEADING_BASE_LEVEL + letter.group("number").count("."),
            _HEADING_MAX_LEVEL,
        )
    if (
        text.isupper()
        and len(text) <= 60
        and len(text) >= 8
        and line.get("page") != 0
        and (
            line.get("centered", False)
            or line["size"] >= median_size * 1.08
            or line["height"] >= max(_OCR_HEADING_MIN_HEIGHT, median_size * 1.08)
        )
    ):
        return HEADING_BASE_LEVEL
    return HEADING_BASE_LEVEL


def _formatar_linha_posicional(line: dict, list_origin: float) -> str:
    text = line["text"].strip()
    if line.get("list_marker"):
        level = min(max(round((line["bbox"][0] - list_origin) / 18), 0), 5)
        indent = "  " * level
        text = re.sub(
            r"^[ \t]*(?:▪|❖|➢|✓|●|○|•|◦|=|\*\s*\||\*|\+|QO\)|o)[ \t]*",
            "",
            text,
            count=1,
            flags=re.IGNORECASE,
        )
        if text == line["text"].strip():
            text = re.sub(r"^\S+\s*", "", text, count=1)
        return f"{indent}- {text}".rstrip()
    if line["spans"]:
        formatted = []
        previous_span = None
        for span in line["spans"]:
            value = span.get("text", "")
            if not value:
                continue
            if previous_span is not None:
                previous_bbox = previous_span.get("bbox", (0, 0, 0, 0))
                current_bbox = span.get("bbox", (0, 0, 0, 0))
                gap = current_bbox[0] - previous_bbox[2]
                if (
                    gap > max(0.5, line["size"] * 0.06)
                    and formatted
                    and not formatted[-1].endswith((" ", "\t"))
                    and not value.startswith((" ", "\t"))
                ):
                    formatted.append(" ")
            flags = span.get("flags", 0)
            if flags & 1 and value.strip().isdigit():
                value = f"[^{value.strip()}]"
            else:
                if flags & 16:
                    value = f"**{value}**"
                if flags & 2:
                    value = f"*{value}*"
            formatted.append(value)
            previous_span = span
        text = "".join(formatted).strip()
    text = _POSITIONAL_BULLET_RE.sub(
        lambda match: f"- {match.group('text')}", text, count=1
    )
    text = re.sub(
        r"(?m)^(?:Il|IIl|lll|III)(?=\s*[-–—]\s)",
        lambda match: "II" if match.group(0) == "Il" else "III",
        text,
    )
    return text


def _ordenar_eventos_geometricos(events: list) -> list:
    events.sort(key=lambda event: (event["y"], event["x"]))
    rows = []
    for event in events:
        if rows and event["y"] <= rows[-1]["anchor"] + _LAYOUT_Y_TOLERANCE:
            rows[-1]["events"].append(event)
        else:
            rows.append({"anchor": event["y"], "events": [event]})
    return [
        event
        for row in rows
        for event in sorted(row["events"], key=lambda item: item["x"])
    ]


def _linhas_formam_paragrafo(previous: dict, current: dict, page_width: float) -> bool:
    if current.get("list_marker"):
        return False
    if _es_titulo_posicional(previous, 0) or _es_titulo_posicional(current, 0):
        return False
    if _heading_style_key(previous) != _heading_style_key(current):
        return False
    expected_left = (
        previous["list_text_x"]
        if previous.get("list_marker") and previous.get("list_text_x") is not None
        else previous["bbox"][0]
    )
    if abs(expected_left - current["bbox"][0]) > _PARAGRAPH_LEFT_TOLERANCE:
        return False
    gap = current["bbox"][1] - previous["bbox"][3]
    if gap > _PARAGRAPH_LINE_GAP_FACTOR * previous["height"]:
        return False
    if previous["bbox"][2] < page_width * _PARAGRAPH_RIGHT_EDGE_RATIO:
        return False
    return not re.search(r"[.!?:]$", previous["text"].strip())


def _juntar_linhas_geometricamente(lines: list) -> str:
    if not lines:
        return ""
    result = [lines[0]["formatted"]]
    for line in lines[1:]:
        previous = result[-1]
        current = line["formatted"]
        if previous.endswith("-") and not previous.endswith("--"):
            result[-1] = previous[:-1] + current.lstrip()
        else:
            result.append(current.strip())
    return " ".join(part for part in result if part)


def _salvar_figura_posicional(
    page,
    figure: dict,
    filename: str,
    img_dir: str,
    crop_watermark: bool,
    ocr_text: str,
    page_ocr_used: bool,
) -> str:
    if figure["kind"] == "raster":
        parts = [
            Image.open(io.BytesIO(block["image"])).convert("RGB")
            for block in figure["blocks"]
        ]
        width = max(part.width for part in parts)
        scaled = [
            part.resize((width, max(1, round(part.height * width / part.width))))
            if part.width != width else part
            for part in parts
        ]
        image = Image.new("RGB", (width, sum(part.height for part in scaled)), "white")
        y = 0
        for part in scaled:
            image.paste(part, (0, y))
            y += part.height
    else:
        rect = pymupdf.Rect(figure["bbox"])
        rect.x0 = max(page.rect.x0, rect.x0 - 3)
        rect.y0 = max(page.rect.y0, rect.y0 - 3)
        rect.x1 = min(page.rect.x1, rect.x1 + 3)
        rect.y1 = min(page.rect.y1, rect.y1 + 3)
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=rect, alpha=False)
        image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)

    watermark_text = ocr_text
    figure_ocr = ""
    if not page_ocr_used and len(re.sub(r"\s+", "", watermark_text)) < 4:
        figure_ocr = _ocr_figura(image)
        watermark_text = watermark_text or figure_ocr
    if crop_watermark and re.search(
        r"(?i)(?:www|jur[ií]dico|\.com\.?br)", watermark_text
    ):
        image = image.crop(
            (0, 0, max(1, int(image.width * 0.94)), max(1, int(image.height * 0.94)))
        )
    image.save(os.path.join(img_dir, filename), format="PNG")
    return watermark_text


# --- Checagem de sanidade: imagem na mesma página do PDF -------------------


def _verificar_imagens_mesma_pagina(md_text: str) -> None:
    """Confere que cada imagem embutida (![[...-pgN.ext]]) ficou dentro do
    bloco anotado com a MESMA página N que o nome do arquivo indica (ver
    `_fix_link`, em `convert_pdf`) -- ou seja, que nada no pós-processamento
    moveu uma imagem para a página errada da nota. Só avisa em stderr (não
    falha o job): é checagem de sanidade, não correção automática."""
    partes = re.split(r"(\n\n---\n\*p\. \d+\*\n\n---\n\n)", md_text)
    i = 0
    while i < len(partes):
        trecho = partes[i]
        marcador = partes[i + 1] if i + 1 < len(partes) else None
        rotulo = None
        if marcador:
            m = re.match(r"\n\n---\n\*p\. (\d+)\*\n\n---\n\n", marcador)
            rotulo = m.group(1) if m else None
        for nome, pg in re.findall(r"!\[\[([^\]]+-pg(\d+)\.\w+)\]\]", trecho):
            if rotulo is not None and pg != rotulo:
                print(
                    f"aviso: imagem {nome} aparenta ter saído da página {pg} do PDF "
                    f"mas ficou anotada na página {rotulo} da nota",
                    file=sys.stderr,
                )
        i += 2


def _eh_perfil_direito(meta: dict) -> bool:
    identificadores = " ".join(
        str(meta.get(chave) or "")
        for chave in ("disciplina", "display_name", "original_filename", "slug")
    )
    return bool(
        re.search(r"\bdireito\b", identificadores, re.IGNORECASE)
        or re.search(r"(?:^|[\s._-])dir(?:eito)?(?:$|[\s._-])", identificadores, re.IGNORECASE)
    )


def _substituir_tabelas_markdown_por_imagens(content: str, image_links: list) -> str:
    linhas = content.splitlines()
    substituicoes = iter(image_links)
    resultado = []
    i = 0
    encontrados = 0

    while i < len(linhas):
        if not _MARKDOWN_TABLE_ROW_RE.match(linhas[i]):
            resultado.append(linhas[i])
            i += 1
            continue

        inicio = i
        fim = i + 1
        separador_visto = bool(_MARKDOWN_TABLE_SEPARATOR_RE.match(linhas[i]))
        while fim < len(linhas):
            if separador_visto and _MARKDOWN_TABLE_ROW_RE.match(linhas[fim]):
                proxima_linha = fim + 1
                while proxima_linha < len(linhas) and not linhas[proxima_linha].strip():
                    proxima_linha += 1
                if (
                    proxima_linha < len(linhas)
                    and _MARKDOWN_TABLE_SEPARATOR_RE.match(linhas[proxima_linha])
                ):
                    break
            if _MARKDOWN_TABLE_ROW_RE.match(linhas[fim]):
                separador_visto = separador_visto or bool(
                    _MARKDOWN_TABLE_SEPARATOR_RE.match(linhas[fim])
                )
                fim += 1
                continue
            if not linhas[fim].strip():
                proxima_linha = fim + 1
                while proxima_linha < len(linhas) and not linhas[proxima_linha].strip():
                    proxima_linha += 1
                if proxima_linha < len(linhas) and _MARKDOWN_TABLE_ROW_RE.match(linhas[proxima_linha]):
                    fim = proxima_linha
                    continue
            break

        bloco = linhas[inicio:fim]
        if len([linha for linha in bloco if _MARKDOWN_TABLE_ROW_RE.match(linha)]) >= 2:
            try:
                link = next(substituicoes)
            except StopIteration:
                resultado.extend(bloco)
            else:
                resultado.append(link)
                encontrados += 1
        else:
            resultado.extend(bloco)
        i = fim

    if encontrados != len(image_links):
        raise ValueError(
            "Quantidade de tabelas Markdown não corresponde às tabelas detectadas no PDF "
            f"({encontrados} substituídas, {len(image_links)} esperadas)"
        )
    return "\n".join(resultado)


def _markdown_table_block_count(content: str) -> int:
    return sum(
        bool(_MARKDOWN_TABLE_SEPARATOR_RE.match(line))
        for line in content.splitlines()
    )


def _indice_pagina_pdf(partes: list, parte_index: int) -> int:
    for match in _IMG_LINK_RE.finditer(partes[parte_index]):
        sufixo = _IMG_SUFFIX_RE.search(os.path.basename(match.group(1)))
        if sufixo:
            return int(sufixo.group(1))
    if parte_index + 1 < len(partes):
        marcador = re.search(r"\*p\.\s*(\d+)\*", partes[parte_index + 1])
        if marcador:
            return max(0, int(marcador.group(1)) - 1)
    return parte_index // 2


def _is_complex_pdf_table(table) -> bool:
    return tabelas.is_compact_complex_table(table)


def _tabela_colorida_associada_a_imagem(page, table) -> bool:
    import pymupdf

    rect = pymupdf.Rect(table.bbox)
    area = rect.get_area()
    if not area or not _retangulo_tem_fundo_nao_branco(page, rect):
        return False
    area_imagens = sum(
        (rect & pymupdf.Rect(bloco["bbox"])).get_area()
        for bloco in page.get_text("dict").get("blocks", [])
        if bloco.get("type") == 1
    )
    return min(area_imagens / area, 1.0) >= 0.5


def _retangulo_tem_fundo_nao_branco(page, bbox) -> bool:
    import pymupdf

    pixmap = page.get_pixmap(
        matrix=pymupdf.Matrix(1, 1),
        clip=pymupdf.Rect(bbox),
        colorspace=pymupdf.csRGB,
        alpha=False,
    )
    if not pixmap.width or not pixmap.height:
        return False
    amostra = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    amostra.thumbnail((128, 128))
    cores = amostra.getcolors(maxcolors=128 * 128)
    if cores is None:
        return False
    total = sum(contagem for contagem, _ in cores)
    nao_brancos = sum(
        contagem
        for contagem, (r, g, b) in cores
        if (299 * r + 587 * g + 114 * b) // 1000 >= 100
        and min(r, g, b) < 245
    )
    return bool(total) and nao_brancos / total >= 0.12


def _agrupar_blocos_imagem(blocos: list) -> list:
    grupos = []
    for bloco in sorted(blocos, key=lambda item: (item["bbox"][1], item["bbox"][0])):
        bbox = bloco["bbox"]
        if grupos:
            anterior = grupos[-1][-1]["bbox"]
            mesma_largura = abs((anterior[2] - anterior[0]) - (bbox[2] - bbox[0])) <= 1
            mesma_coluna = abs(anterior[0] - bbox[0]) <= 1
            adjacente = abs(anterior[3] - bbox[1]) <= 3
            if mesma_largura and mesma_coluna and adjacente:
                grupos[-1].append(bloco)
                continue
        grupos.append([bloco])
    return grupos


def _renderizar_tabelas_como_imagens(
    md_text: str,
    input_path: str,
    img_dir: str,
    image_stem: str,
) -> str:
    """No perfil Direito, renderiza como imagem quadros compactos com 4+ colunas."""
    import pymupdf

    partes = _PAGE_NOTE_RE.split(md_text)
    with pymupdf.open(input_path) as doc:
        for parte_index in range(0, len(partes), 2):
            page_index = _indice_pagina_pdf(partes, parte_index)
            if page_index >= len(doc):
                break
            if _markdown_table_block_count(partes[parte_index]) == 0:
                continue
            page = doc[page_index]
            tabelas = sorted(
                (
                    tabela
                    for tabela in page.find_tables().tables
                    if _is_complex_pdf_table(tabela)
                    or _tabela_colorida_associada_a_imagem(page, tabela)
                ),
                key=lambda tabela: (tabela.bbox[1], tabela.bbox[0]),
            )
            if not tabelas:
                continue
            markdown_tables = _markdown_table_block_count(partes[parte_index])
            if markdown_tables != len(tabelas):
                raise ValueError(
                    f"Página {page_index + 1}: {markdown_tables} tabelas Markdown, "
                    f"mas {len(tabelas)} tabelas detectadas no PDF"
                )

            links = []
            for table_index, tabela in enumerate(tabelas, start=1):
                rect = pymupdf.Rect(tabela.bbox)
                rect.x0 = max(page.rect.x0, rect.x0 - 3)
                rect.y0 = max(page.rect.y0, rect.y0 - 3)
                rect.x1 = min(page.rect.x1, rect.x1 + 3)
                rect.y1 = min(page.rect.y1, rect.y1 + 3)
                filename = f"{image_stem}-table{table_index}-pg{page_index + 1}.png"
                page.get_pixmap(
                    matrix=pymupdf.Matrix(2, 2),
                    clip=rect,
                    alpha=False,
                ).save(os.path.join(img_dir, filename))
                links.append(f"![[{filename}]]")

            partes[parte_index] = _substituir_tabelas_markdown_por_imagens(
                partes[parte_index], links
            )
    return "".join(partes)


def _renderizar_imagens_coloridas_fragmentadas(
    md_text: str,
    input_path: str,
    img_dir: str,
    image_stem: str,
) -> str:
    """Recompõe como um recorte regiões coloridas fragmentadas em várias imagens."""
    import pymupdf

    partes = _PAGE_NOTE_RE.split(md_text)
    with pymupdf.open(input_path) as doc:
        for parte_index in range(0, len(partes), 2):
            page_index = _indice_pagina_pdf(partes, parte_index)
            if page_index >= len(doc):
                break
            conteudo = partes[parte_index]
            links = list(_IMG_LINK_RE.finditer(conteudo))
            if len(links) <= 1:
                continue
            page = doc[page_index]
            blocos = [
                bloco
                for bloco in page.get_text("dict").get("blocks", [])
                if bloco.get("type") == 1
            ]
            grupos = _agrupar_blocos_imagem(blocos)
            if len(grupos) != 1:
                continue
            bbox = (
                min(bloco["bbox"][0] for bloco in grupos[0]),
                min(bloco["bbox"][1] for bloco in grupos[0]),
                max(bloco["bbox"][2] for bloco in grupos[0]),
                max(bloco["bbox"][3] for bloco in grupos[0]),
            )
            area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
            if (
                area < page.rect.width * page.rect.height * 0.10
                or not _retangulo_tem_fundo_nao_branco(page, bbox)
            ):
                continue

            rect = pymupdf.Rect(bbox)
            rect.x0 = max(page.rect.x0, rect.x0 - 3)
            rect.y0 = max(page.rect.y0, rect.y0 - 3)
            rect.x1 = min(page.rect.x1, rect.x1 + 3)
            rect.y1 = min(page.rect.y1, rect.y1 + 3)
            filename = f"{image_stem}-figure-pg{page_index + 1}.png"
            page.get_pixmap(
                matrix=pymupdf.Matrix(2, 2),
                clip=rect,
                alpha=False,
            ).save(os.path.join(img_dir, filename))
            embed = f"![[{filename}]]"
            partes[parte_index] = (
                conteudo[:links[0].start()]
                + embed
                + _IMG_LINK_RE.sub("", conteudo[links[0].end():])
            )
    return "".join(partes)


# Detecta um item de sumário numerado no estilo usado nos slides das aulas:
# "4. Poder Legislativo", "4.6 Imunidades...", "5- Processo legislativo...",
# "5.1. Introdução". Testado contra a nota de referência do usuário.
_OUTLINE_RE = re.compile(
    r"^\d+(?:\.\d+)*(?:\.)?(?:\s+|[-–—]\s*)\S"
)


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
    linhas = text.split("\n")
    for indice, linha in enumerate(linhas):
        if not linha.lstrip().startswith("|"):
            linhas[indice] = re.sub(r"<br\s*/?>", "\n", linha)
    text = "\n".join(linhas)
    text = _HTML_TAG_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _paginas_sem_texto_nativo(
    doc: "pymupdf.Document",
    min_chars: int = _OCR_MIN_CHARS,
) -> tuple:
    """Roda OCR manual só nas páginas sem texto nativo suficiente (páginas
    genuinamente escaneadas). Não toca em páginas com texto digital, mesmo
    que tenham imagens grandes -- é exatamente esse caso que o classificador
    automático da lib erra (ver docstring do módulo). Também devolve os blocos
    posicionados do OCR para que as imagens possam ser ancoradas no fluxo.
    """
    resultado = {}
    blocos_ocr_por_pagina = {}
    for page in doc:
        nativo = page.get_text().strip()
        if len(nativo) >= min_chars:
            continue
        try:
            tp = page.get_textpage_ocr(full=True, language="por+eng", dpi=200)
            texto_ocr = page.get_text(textpage=tp).strip()
        except Exception as exc:  # noqa: BLE001 -- não deixa uma página ruim derrubar o job inteiro
            if "Tesseract is not installed" in str(exc):
                raise RuntimeError(
                    "OCR é necessário para páginas sem texto extraível, mas o Tesseract "
                    "não está instalado. A conversão foi interrompida para evitar gerar "
                    "uma nota sem o texto dessas páginas."
                ) from exc
            print(f"aviso: OCR falhou na página {page.number + 1}: {exc}", file=sys.stderr)
            continue
        if texto_ocr:
            resultado[page.number] = texto_ocr
            blocos_ocr_por_pagina[page.number] = [
                block
                for block in page.get_text("dict", textpage=tp).get("blocks", [])
                if block.get("type") == 0
            ]
    return resultado, blocos_ocr_por_pagina


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
    return bool(
        re.search(r'[:#\[\]{}]|^[\-?!&*|>%@`"\']', valor)
    )


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
        conteudo = re.sub(r"^\*\*(.*?)\*\*$", r"\1", conteudo).strip()
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


def _processar_notas_rodape_posicionais(md_text: str) -> tuple:
    partes = _PAGE_NOTE_RE.split(md_text)
    saida = []
    notas = []
    contador = 0
    for indice in range(0, len(partes), 2):
        conteudo = partes[indice]
        conteudo, notas_pagina = _extrair_notas_rodape_pagina(conteudo)
        for numero, corpo in notas_pagina:
            contador += 1
            conteudo = re.sub(
                rf"\[\^{re.escape(numero)}\]", f"[^{contador}]", conteudo
            )
            notas.append((contador, corpo))
        saida.append(conteudo)
        if indice + 1 < len(partes):
            saida.append(partes[indice + 1])
    return "".join(saida), notas


def convert_pdf(input_path: str, job_dir: str, meta: dict, crop_watermark: bool = False) -> dict:
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
    nome = meta.get("display_name") or meta["slug"]
    image_stem = meta.get("slug") or slugify(nome or os.path.basename(input_path))
    md_text, n_images = _extrair_layout_pdf(
        input_path,
        img_dir,
        image_stem,
        crop_watermark,
        _eh_perfil_direito(meta),
    )
    md_text, notas_rodape = _processar_notas_rodape_posicionais(md_text)
    md_text = _destacar_citacoes_de_lei(md_text)
    md_text = _destacar_jurisprudencia(md_text)
    md_text = _aplicar_callouts_alerta(md_text)
    md_text = _normalizar_simbolos_logicos(md_text)
    md_text = _remover_sites_residuais(md_text)
    md_text = re.sub(r"(?m)^--- end of page=\d+ ---\s*$", "", md_text)
    md_text = re.sub(r"(?m)^(#{1,3})\s+", r"#### ", md_text)
    assunto_lines = detect_assunto(md_text)

    _verificar_imagens_mesma_pagina(md_text)

    if os.path.isdir(img_dir) and not os.listdir(img_dir):
        os.rmdir(img_dir)

    meta = dict(meta)
    meta["assunto_lines"] = assunto_lines
    meta.setdefault("data_criacao", datetime.date.today().isoformat())

    frontmatter = build_frontmatter(meta)

    partes = [frontmatter]
    partes.append(md_text.strip())
    if notas_rodape:
        partes.append("\n".join(f"[^{n}]: {corpo}" for n, corpo in notas_rodape))
    final_md = "\n\n".join(partes) + "\n"

    md_path = os.path.join(job_dir, f"{nome}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(final_md)

    return {"md_path": md_path, "n_images": n_images}
