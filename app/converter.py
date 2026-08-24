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


def _normalizar_marcadores_apostila(texto: str) -> str:
    """Ver docstring da seção acima. Roda sobre o documento inteiro, antes
    do reflow (pra que os novos "- " já sejam reconhecidos como início de
    bloco por `reflow_paragraphs_apostila`)."""
    if not texto:
        return texto
    texto = _MARCADOR_DECORATIVO_FIM_RE.sub("", texto)
    texto = _MARCADOR_ESTRANHO_RE.sub(lambda m: f"{m.group(1)}- ", texto)
    return texto


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


def _ancoras_de_imagem_por_pagina(doc: "pymupdf.Document") -> dict:
    """Ver docstring da seção acima. Devolve `{pagina_0indexada: [texto_ou_None, ...]}`
    -- uma entrada por imagem da página, na mesma ordem (topo -> baixo)
    em que o pymupdf4llm extrai as imagens dessa página."""
    resultado = {}
    for page in doc:
        blocks = page.get_text("dict").get("blocks", [])
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
    texto = re.sub(r"[*_`~\"“”‘’<>–—\-❖➢⮚➤➥▶►∙•●○▪◦]", " ", texto)
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
        return conteudo

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
_INDICE_CHILD_DOTNUM_RE = re.compile(r"^(\d{1,3})\.\s*(\d{1,3})\s+(\S.*)$")
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
    m = _INDICE_HEADING_RE.match(linha)
    if m:
        return (0, m.group(1).strip())
    m = _INDICE_CHILD_DOTNUM_RE.match(linha)
    if m:
        return (1, f"{m.group(1)}.{m.group(2)} {m.group(3)}".strip())
    m = _INDICE_PARENT_NUM_RE.match(linha)
    if m:
        return (0, f"{m.group(1)}. {m.group(2)}".strip())
    m = _INDICE_CHILD_DASH_RE.match(linha)
    if m:
        return (1, f"{m.group(1)}- {m.group(2)}".strip())
    return None


def _formatar_item_indice(texto: str) -> str:
    texto = texto.strip().rstrip(":").strip()
    texto = re.sub(r"^\*\*|\*\*$", "", texto).strip()
    if not texto:
        return ""
    return f"[[{texto}]]"


def _formatar_bloco_assunto(linhas: list) -> str:
    """Mesma ideia de `_processar_indice`, mas para o bloco de "assunto"
    (eco do sumário logo abaixo do frontmatter, gerado a partir de
    `detect_assunto`) -- também vira lista aninhada com wikilinks, em vez
    da lista plana de antes."""
    saida = []
    for linha in linhas:
        classificado = _classificar_linha_indice(linha) or (0, linha)
        nivel, texto = classificado
        link = _formatar_item_indice(texto)
        if not link:
            continue
        prefixo = "  - " if nivel else "- "
        saida.append(f"{prefixo}{link}")
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
        link = _formatar_item_indice(texto)
        if not link:
            continue
        prefixo = "  - " if nivel else "- "
        linhas_saida.append(f"{prefixo}{link}")

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
_SITE_RESIDUAL_RE = re.compile(
    r"[ \t]*\b\d{1,4}\b(?=[ \t]+(?:https?://|www\.))"
    r"|https?://\S+"
    r"|\bwww\.[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+(?:/\S*)?"
)


def _remover_sites_residuais(texto: str) -> str:
    """Ver docstring da seção acima."""
    return _SITE_RESIDUAL_RE.sub("", texto)


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
        ancoras_por_pagina = _ancoras_de_imagem_por_pagina(doc)
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
    # Converte "<sup>N</sup>" (referência de nota de rodapé) para "[^N]"
    # ANTES de normalize_markdown_text, que descarta a tag <sup> como ruído
    # de HTML (ver docstring de `_converter_notas_rodape_sup`).
    md_text = _converter_notas_rodape_sup(md_text)
    md_text = normalize_markdown_text(md_text)

    # detect_assunto depende da granularidade linha-a-linha original (cada
    # item do sumário numerado em sua própria linha) pra casar com
    # `_OUTLINE_RE` -- por isso roda ANTES do reflow, que junta linhas de
    # continuação e destruiria essa granularidade.
    assunto_lines = detect_assunto(md_text)

    # process_pages_apostila remove o rodapé de cada página, normaliza
    # marcadores de tópico exóticos, extrai o corpo das notas de rodapé
    # (renumerando pro documento inteiro), reposiciona cada imagem pro
    # ponto do corpo onde ela aparece de verdade no PDF (ver
    # `_ancoras_de_imagem_por_pagina` / `_reposicionar_imagens_pagina`) e
    # faz o reflow de parágrafos com os padrões extras de índice/sumário
    # da apostila (ver docstrings da seção "funções EXCLUSIVAS do
    # pipeline de apostila" acima). É uma cópia isolada de `process_pages`
    # -- `converter_legislacao.py` continua usando o `process_pages`
    # original, sem essas adições (e não extrai imagem nenhuma).
    md_text, notas_rodape = process_pages_apostila(md_text, ancoras_por_pagina)

    # Índice/sumário do início do documento -> lista aninhada com
    # wikilinks; citação de lei em bloco -> callout de citação; avisos
    # didáticos (Obs., Atenção...) -> callout de atenção; e uma última
    # rede de segurança contra resíduo de rodapé de site que tenha
    # sobrevivido ao reflow.
    md_text = _processar_indice(md_text)
    md_text = _destacar_citacoes_de_lei(md_text)
    md_text = _destacar_jurisprudencia(md_text)
    md_text = _aplicar_callouts_alerta(md_text)
    md_text = _remover_sites_residuais(md_text)

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

    # Checagem de sanidade (só avisa em stderr, não falha o job): garante
    # que nenhum passo acima moveu uma imagem para fora da página do PDF
    # de onde ela foi extraída.
    _verificar_imagens_mesma_pagina(md_text)

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
        partes.append(_formatar_bloco_assunto(assunto_lines))
    partes.append(md_text.strip())
    if notas_rodape:
        partes.append("\n".join(f"[^{n}]: {corpo}" for n, corpo in notas_rodape))
    final_md = "\n\n".join(partes) + "\n"

    md_path = os.path.join(job_dir, f"{nome}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(final_md)

    return {"md_path": md_path, "n_images": n_images}
