import os
import io
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import converter


class ConverterMarkdownTests(unittest.TestCase):
    def test_normalize_markdown_text_strips_ocr_noise_and_html_tags(self):
        raw = """<!-- Start of picture text -->
G7JURIDICO<br><!-- End of picture text -->

(Continuação) <u>Poder Executivo</u>
"""

        cleaned = converter.normalize_markdown_text(raw)

        self.assertNotIn("Start of picture text", cleaned)
        self.assertNotIn("<!--", cleaned)
        self.assertNotIn("<br>", cleaned)
        self.assertIn("Poder Executivo", cleaned)

    def test_normalize_preserves_line_breaks_inside_markdown_table_rows(self):
        raw = (
            "Texto antes<br>da tabela\n\n"
            "|Art. 9º Constituição<br>administrativa|Art. 10 lesão|Art. 11 princípios|\n"
            "|---|---|---|\n"
            "|Enriquecimento ilícito|Lesão ao erário|Violação a princípio|"
        )

        cleaned = converter.normalize_markdown_text(raw)
        output = converter.reflow_paragraphs(cleaned)

        self.assertIn("Texto antes\nda tabela", cleaned)
        self.assertIn("Art. 9º Constituição<br>administrativa", output)
        self.assertIn("|---|---|---|", output)
        self.assertIn("|Enriquecimento ilícito|Lesão ao erário|Violação a princípio|", output)

    def test_slugify_uses_safe_name(self):
        self.assertEqual(converter.slugify("DCA4.pdf"), "dca4")
        self.assertEqual(converter.slugify("Aula_05_Constitucional.pdf"), "aula-05-constitucional")

    def test_reflow_paragraphs_joins_wrapped_sentence(self):
        raw = (
            "-Insta recordar que a legitimação conferida às autoridades pelo art. 103\n\n"
            "foi estabelecida em caráter intuito personae, razão pela qual a própria\n\n"
            "autoridade deve subscrever a petição inicial.\n\n"
            "❖ **ADI 5.084-DF:** Trata-se de ação direta de inconstitucionalidade.\n\n"
        )
        out = converter.reflow_paragraphs(raw)
        self.assertIn(
            "-Insta recordar que a legitimação conferida às autoridades pelo art. 103 "
            "foi estabelecida em caráter intuito personae, razão pela qual a própria "
            "autoridade deve subscrever a petição inicial.",
            out,
        )
        self.assertIn("❖ **ADI 5.084-DF:** Trata-se de ação direta de inconstitucionalidade.", out)
        # os dois viram blocos distintos, não um parágrafo só
        self.assertEqual(out.count("\n\n"), 1)

    def test_reflow_paragraphs_does_not_merge_indented_quote_dash(self):
        # um "-" de abertura de aspas indentado no meio da frase não pode
        # ser confundido com um bullet novo (ver nota de referência com
        # "   - AGU/PGE ou outro advogado habilitado)")
        raw = "-Assim, ou em conjunto com\n\n   - AGU/PGE ou outro advogado habilitado).\n\n"
        out = converter.reflow_paragraphs(raw)
        self.assertEqual(out.count("\n\n"), 0)
        self.assertIn("- AGU/PGE ou outro advogado habilitado)", out)

    def test_process_pages_strips_footer_and_annotates_page_number(self):
        raw = (
            "-Conteúdo da página um.\n\n"
            "1\nwww.g7juridico.com.br\n\n"
            "--- end of page=0 ---\n\n"
            "-Conteúdo da página dois."
        )
        out = converter.process_pages(raw)
        self.assertNotIn("www.g7juridico.com.br", out)
        self.assertIn("*p. 1*", out)
        self.assertIn("-Conteúdo da página um.", out)
        self.assertIn("-Conteúdo da página dois.", out)

    def test_process_pages_strips_footer_even_when_followed_by_image(self):
        # o rodapé pode não ser a última coisa no bloco da página (ex:
        # quando a página termina com uma tabela renderizada como imagem)
        raw = (
            "-Conteúdo antes da tabela.\n\n"
            "14\nwww.g7juridico.com.br\n\n"
            "![[DCA8-img2-pg14.png]]\n\n"
            "--- end of page=13 ---\n\n"
            "-Próxima página."
        )
        out = converter.process_pages(raw)
        self.assertNotIn("www.g7juridico.com.br", out)
        self.assertIn("*p. 14*", out)
        self.assertIn("![[DCA8-img2-pg14.png]]", out)

    def test_process_pages_handles_last_page_without_trailing_blank_lines(self):
        # normalize_markdown_text faz .strip() no texto inteiro, então o
        # marcador da ÚLTIMA página do documento não tem "\n\n" depois dele
        raw = "-Conteúdo da última página.\n\n30\nwww.g7juridico.com.br\n\n--- end of page=29 ---"
        out = converter.process_pages(raw)
        self.assertNotIn("www.g7juridico.com.br", out)
        self.assertNotIn("end of page", out)
        self.assertIn("-Conteúdo da última página.", out)

    def test_normalizar_simbolos_logicos_apenas_em_contexto_de_formula(self):
        raw = "P^Q v R -> ~S; palavra ou texto comum; v sozinho; ^ símbolo"
        out = converter._normalizar_simbolos_logicos(raw)
        self.assertIn("P ∧ Q ∨ R → ¬S", out)
        self.assertIn("palavra ou texto comum; v sozinho; ^ símbolo", out)
        self.assertEqual(
            converter._normalizar_simbolos_logicos("P <-> Q; P <=> Q; P => Q"),
            "P ↔ Q; P ⇔ Q; P ⇒ Q",
        )
        self.assertEqual(
            converter._normalizar_simbolos_logicos("condicional P ? Q\nconjunção A\ue001B"),
            "condicional P → Q\nconjunção A∧B",
        )
        self.assertEqual(
            converter._normalizar_simbolos_logicos(
                "bicondicional P\ue001Q\nimply\nimplicação A?B\nnão pertence C?D"
            ),
            "bicondicional P↔Q\nimply\nimplicação A ⇒ B\nnão pertence C ∉ D",
        )
        self.assertEqual(
            converter._normalizar_simbolos_logicos("(P V Q) A R; p v q; palavra comum"),
            "(P ∨ Q) ∧ R; p ∨ q; palavra comum",
        )
        self.assertEqual(
            converter._normalizar_simbolos_logicos("CASO A QUESTAO; P A Q"),
            "CASO A QUESTAO; P ∧ Q",
        )
        self.assertEqual(
            converter._normalizar_simbolos_logicos("P > Q; P = Q"),
            "P ⇒ Q; P ⇔ Q",
        )
        self.assertEqual(
            converter._normalizar_simbolos_logicos("A questão P ? Q"),
            "A questão P ? Q",
        )

    def test_normalizar_simbolos_logicos_preserves_markdown_strikethrough(self):
        raw = (
            "~~I - praticar ato visando fim proibido em lei ou regulamento~~ "
            "~~II - retardar ou deixar de praticar ato de ofício~~"
        )
        self.assertEqual(
            converter._normalizar_simbolos_logicos(raw),
            raw,
        )

    def test_converter_pdf_normaliza_formula_e_remove_site_na_faixa_inferior(self):
        import pymupdf

        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = os.path.join(temp_dir, "logica.pdf")
            doc = pymupdf.open()
            page = doc.new_page(width=595, height=842)
            page.insert_text((50, 100), "P^Q v R -> ~S; P <-> Q", fontsize=14)
            page.insert_text((50, 160), "Link legítimo: https://exemplo.com.br/lei", fontsize=12)
            page.insert_text((420, 820), "WWW . EXEMPLO . COM . BR", fontsize=10)
            doc.save(pdf_path)
            doc.close()

            result = converter.convert_pdf(
                pdf_path,
                os.path.join(temp_dir, "output"),
                {"slug": "logica", "display_name": "logica"},
            )
            with open(result["md_path"], encoding="utf-8") as markdown_file:
                markdown = markdown_file.read()

        self.assertIn("P ∧ Q ∨ R → ¬S; P ↔ Q", markdown)
        self.assertNotIn("EXEMPLO", markdown)
        self.assertIn("https://exemplo.com.br/lei", markdown)

    def test_visual_extraction_merges_adjacent_strips_and_uses_current_stem(self):
        import pymupdf
        from PIL import Image

        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = os.path.join(temp_dir, "source.pdf")
            doc = pymupdf.open()
            page = doc.new_page(width=400, height=500)
            page.insert_text((40, 60), "ROTEIRO DE AULA", fontname="hebo", fontsize=14)
            image = Image.new("RGB", (300, 100), "white")
            image_buffer = io.BytesIO()
            image.save(image_buffer, format="PNG")
            page.insert_image(pymupdf.Rect(40, 150, 340, 250), stream=image_buffer.getvalue())
            page.insert_image(pymupdf.Rect(40, 250, 340, 350), stream=image_buffer.getvalue())
            doc.save(pdf_path)
            doc.close()

            result = converter.convert_pdf(
                pdf_path,
                os.path.join(temp_dir, "output"),
                {"slug": "source", "display_name": "Current_Stem"},
            )
            with open(result["md_path"], encoding="utf-8") as markdown_file:
                markdown = markdown_file.read()

            image_path = os.path.join(temp_dir, "output", "images", "Current_Stem-pg1-fig1.png")
            self.assertTrue(os.path.isfile(image_path))
            with Image.open(image_path) as merged:
                self.assertEqual(merged.size, (300, 200))

        self.assertEqual(result["n_images"], 1)
        self.assertIn("### ROTEIRO DE AULA", markdown)
        self.assertLess(markdown.index("### ROTEIRO DE AULA"), markdown.index("![[Current_Stem-pg1-fig1.png]]"))
        self.assertNotIn("[[ROTEIRO DE AULA]]", markdown)

    def test_visual_title_levels_follow_numbering_and_leave_prose_unmarked(self):
        def make_line(text, bold=True):
            return {"spans": [{"text": text, "flags": 16 if bold else 0}]}

        state = {"nivel": None}
        self.assertEqual(
            converter._texto_linha_pdf(make_line("3. Título principal"), state),
            "### 3. Título principal",
        )
        self.assertEqual(
            converter._texto_linha_pdf(make_line("3.1 Subtítulo"), state),
            "#### 3.1 Subtítulo",
        )
        self.assertEqual(
            converter._texto_linha_pdf(make_line("3.1.2 Neto"), state),
            "##### 3.1.2 Neto",
        )
        self.assertEqual(
            converter._texto_linha_pdf(make_line("3.1.2.4 Profundidade máxima"), state),
            "###### 3.1.2.4 Profundidade máxima",
        )
        self.assertEqual(
            converter._texto_linha_pdf(make_line("1. Texto corrido numerado", bold=False), state),
            "1. Texto corrido numerado",
        )
        self.assertEqual(
            converter._texto_linha_pdf(make_line("1. Esta é uma frase completa."), state),
            "**1. Esta é uma frase completa.**",
        )
        self.assertEqual(
            converter._texto_linha_pdf(make_line("Destaque no corpo"), state),
            "**Destaque no corpo**",
        )
        self.assertEqual(
            converter._texto_linha_pdf(make_line("### 3.1 Título já marcado"), state),
            "#### 3.1 Título já marcado",
        )

    def test_crop_watermark_is_disabled_by_default_and_explicit_when_requested(self):
        import pymupdf
        from PIL import Image

        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = os.path.join(temp_dir, "watermark.pdf")
            doc = pymupdf.open()
            page = doc.new_page(width=400, height=500)
            image = Image.new("RGB", (300, 200), "white")
            image_buffer = io.BytesIO()
            image.save(image_buffer, format="PNG")
            page.insert_image(pymupdf.Rect(40, 150, 340, 350), stream=image_buffer.getvalue())
            doc.save(pdf_path)
            doc.close()

            with patch.object(converter, "_ocr_figura", return_value="WWW.G7JURIDICO.COM.BR"):
                default_dir = os.path.join(temp_dir, "default")
                converter.convert_pdf(pdf_path, default_dir, {"slug": "watermark", "display_name": "watermark"})
                crop_dir = os.path.join(temp_dir, "crop")
                converter.convert_pdf(
                    pdf_path,
                    crop_dir,
                    {"slug": "watermark", "display_name": "watermark"},
                    crop_watermark=True,
                )

            with Image.open(os.path.join(default_dir, "images", "watermark-pg1-fig1.png")) as default_image:
                self.assertEqual(default_image.size, (300, 200))
            with Image.open(os.path.join(crop_dir, "images", "watermark-pg1-fig1.png")) as cropped_image:
                self.assertEqual(cropped_image.size, (282, 188))


class ApostilaOnlyTests(unittest.TestCase):
    """Testes das funções exclusivas do pipeline de apostila (adicionadas
    na correção de índice/notas de rodapé/callouts). Todas isoladas das
    funções compartilhadas com `converter_legislacao.py` -- ver
    `test_converter_legislacao.py`, que continua verde sem nenhuma mudança."""

    def test_img_link_re_handles_parentheses_in_filename(self):
        # bug real: PDF batizado "vavilov_DCA3-(1).pdf" fazia a imagem
        # nunca virar embed wikilink (ficava como link de sistema de
        # arquivos cru, quebrado no Obsidian).
        sample = "![](/data/output/job/images/vavilov_DCA3-(1).pdf-0-0.png)"
        m = converter._IMG_LINK_RE.search(sample)
        self.assertIsNotNone(m)
        self.assertTrue(m.group(1).endswith("vavilov_DCA3-(1).pdf-0-0.png"))

    def test_reflow_apostila_keeps_indice_items_separate(self):
        raw = "**Controle de constitucionalidade**\n\n1- Teoria Geral\n\n**Controle Difuso e Concentrado**\n\n1- Ações\n\n2- Legitimidade\n\n"
        out = converter.reflow_paragraphs_apostila(raw)
        for esperado in [
            "**Controle de constitucionalidade**",
            "1- Teoria Geral",
            "**Controle Difuso e Concentrado**",
            "1- Ações",
            "2- Legitimidade",
        ]:
            self.assertIn(esperado, out.split("\n\n"))

    def test_reflow_apostila_keeps_ordinal_items_separate(self):
        raw = "**-Cabível em duas situações:**\n\n1ª) Remédio Constitucional decidido.\n\n2ª) Da decisão prolatada.\n\n"
        out = converter.reflow_paragraphs_apostila(raw)
        blocos = out.split("\n\n")
        self.assertIn("**-Cabível em duas situações:**", blocos)
        self.assertIn("1ª) Remédio Constitucional decidido.", blocos)
        self.assertIn("2ª) Da decisão prolatada.", blocos)

    def test_reflow_apostila_nests_indented_subtopics_instead_of_flattening(self):
        # bug real (DCA1.pdf, pág. 5): "Características fundamentais:" e
        # seus sub-itens indentados no PDF ("Hereditariedade:",
        # "Vitaliciedade:", "Irresponsabilidade política do monarca")
        # saíam todos grudados num único parágrafo achatado. Cada
        # sub-item indentado deve virar um bloco filho aninhado (prefixo
        # "  "), não texto solto colado no bloco pai.
        raw = (
            "- Características fundamentais:\n"
            "   - Hereditariedade: Governante não é eleito\n"
            "   - Vitaliciedade: Não existe mandato pré-fixado\n"
            "   - Irresponsabilidade política do monarca\n"
        )
        out = converter.reflow_paragraphs_apostila(raw)
        blocos = out.split("\n\n")
        self.assertEqual(
            blocos,
            [
                "- Características fundamentais:",
                "  - Hereditariedade: Governante não é eleito",
                "  - Vitaliciedade: Não existe mandato pré-fixado",
                "  - Irresponsabilidade política do monarca",
            ],
        )

    def test_reflow_apostila_indented_continuation_without_marker_still_joins(self):
        # uma linha indentada que NÃO comece com marcador de bloco
        # reconhecido continua sendo tratada como simples continuação do
        # bloco aberto (comportamento anterior preservado -- só linhas
        # indentadas que abrem sub-tópico novo viram bloco aninhado).
        raw = "-Assim, ou em conjunto com\n   AGU/PGE ou outro advogado habilitado).\n"
        out = converter.reflow_paragraphs_apostila(raw)
        self.assertEqual(out.count("\n\n"), 0)
        self.assertIn("-Assim, ou em conjunto com AGU/PGE ou outro advogado habilitado).", out)

    def test_processar_indice_wraps_and_nests(self):
        md = (
            "**Sumário**\n\n"
            "**Controle de constitucionalidade**\n\n"
            "1- Teoria Geral\n\n"
            "![[capa.png]]\n\n"
            "Texto normal do corpo da aula."
        )
        out = converter._processar_indice(md)
        self.assertIn("### Sumário", out)
        self.assertIn("### Controle de constitucionalidade", out)
        self.assertIn("#### 1- Teoria Geral", out)
        self.assertNotIn("[[Sumário]]", out)
        self.assertNotIn("[[Controle de constitucionalidade]]", out)
        # não mexe no que vem depois do índice
        self.assertIn("![[capa.png]]", out)
        self.assertIn("Texto normal do corpo da aula.", out)

    def test_formatar_bloco_assunto_nests_dotted_outline(self):
        linhas = [
            "3. Poder Executivo",
            "3.1 exercício do poder executivo",
            "3. 6 Imunidades do Presidente",
            "3.1.2 Competências",
        ]
        out = converter._formatar_bloco_assunto(linhas)
        self.assertEqual(
            out,
            "### 3. Poder Executivo\n"
            "#### 3.1 exercício do poder executivo\n"
            "#### 3.6 Imunidades do Presidente\n"
            "##### 3.1.2 Competências",
        )

    def test_alerta_callout_wraps_obs_with_leading_bullet_dash(self):
        # a apostila abre o aviso com um "-" de tópico antes da palavra-gatilho
        md = "-Obs.: Nosso documento constitucional de 1988 é prolixo.\n\nTexto comum depois."
        out = converter._aplicar_callouts_alerta(md)
        self.assertIn("> [!attention] Atenção!\n> -Obs.: Nosso documento constitucional de 1988 é prolixo.", out)
        self.assertIn("Texto comum depois.", out)

    def test_alerta_callout_recognizes_atencao_and_cuidado(self):
        for gatilho in ["Atenção: verifique o prazo.", "Cuidado com a prescrição.", "Não se esqueça de recorrer."]:
            out = converter._aplicar_callouts_alerta(gatilho)
            self.assertTrue(out.startswith("> [!attention] Atenção!\n> " + gatilho), out)

    def test_lei_citacao_groups_caput_and_incisos_in_one_callout(self):
        md = (
            '**"Art. 102, CF/88:** Compete ao Supremo Tribunal Federal, cabendo-lhe:\n\n'
            "II - julgar, em recurso ordinário: o crime político.\n\n"
            "Comentário da professora depois da citação."
        )
        out = converter._destacar_citacoes_de_lei(md)
        blocos = out.split("\n\n")
        self.assertEqual(len(blocos), 2)  # citação (caput+inciso juntos) + comentário
        # callout sem título (espaço depois do "]"), "LEI" como primeira
        # linha do corpo -- não como título do callout.
        self.assertTrue(blocos[0].startswith("> [!quote] \n> LEI\n"))
        self.assertIn("> II - julgar, em recurso ordinário", blocos[0])
        self.assertEqual(blocos[1], "Comentário da professora depois da citação.")

    def test_destacar_jurisprudencia_wraps_adpf_citation(self):
        md = (
            "**ADPF 54/DF, rel. Min. Marco Aurélio:** O Plenário, por maioria, julgou procedente "
            "pedido formulado em arguição de descumprimento de preceito fundamental.\n\n"
            "Comentário da professora depois da jurisprudência."
        )
        out = converter._destacar_jurisprudencia(md)
        blocos = out.split("\n\n")
        self.assertEqual(len(blocos), 2)
        self.assertTrue(blocos[0].startswith("> [!warning] Jurisprudência\n"))
        self.assertIn("> **ADPF 54/DF, rel. Min. Marco Aurélio:**", blocos[0])
        self.assertEqual(blocos[1], "Comentário da professora depois da jurisprudência.")

    def test_destacar_jurisprudencia_wraps_sumula_vinculante(self):
        md = '**Súmula Vinculante 11:** "Só é lícito o uso de algemas em casos de resistência."'
        out = converter._destacar_jurisprudencia(md)
        self.assertTrue(out.startswith("> [!warning] Jurisprudência\n> **Súmula Vinculante 11:**"))

    def test_destacar_jurisprudencia_does_not_touch_unrelated_text(self):
        md = "Texto comum de comentário da professora, sem citação de jurisprudência nenhuma."
        out = converter._destacar_jurisprudencia(md)
        self.assertEqual(out, md)

    def test_sup_footnote_roundtrip_through_process_pages_apostila(self):
        raw = (
            "Texto com nota. <sup>1</sup>\n\n"
            "\n\n1 Corpo da nota de rodapé.\n\n"
            "--- end of page=0 ---"
        )
        raw = converter._converter_notas_rodape_sup(raw)
        raw = converter.normalize_markdown_text(raw)
        out, notas = converter.process_pages_apostila(raw)
        self.assertIn("[^1]", out)
        self.assertNotIn("<sup>", out)
        self.assertEqual(notas, [(1, "Corpo da nota de rodapé.")])

    def test_normalizar_marcadores_converts_leading_glyph_and_strips_trailing(self):
        raw = "**Tipologias** ⮚\n\n`o` O Poder Executivo pode se estruturar.\n"
        out = converter._normalizar_marcadores_apostila(raw)
        self.assertIn("**Tipologias**\n", out)
        self.assertNotIn("⮚", out)
        self.assertIn("- O Poder Executivo pode se estruturar.", out)

    def test_remover_sites_residuais_strips_footer_but_preserves_inline_links(self):
        raw = (
            "Veja o material em https://exemplo.com.br/documento.\n"
            "-Veja que NÃO é a autorização da Câmara 35 WWW . G7JURIDICO . COM . BR"
        )
        out = converter._remover_sites_residuais(raw)
        self.assertNotIn("G7JURIDICO", out)
        self.assertNotIn(" 35 ", out)
        self.assertIn("https://exemplo.com.br/documento", out)

    def test_remover_sites_residuais_strips_standalone_domain(self):
        out = converter._remover_sites_residuais("Texto útil.\nwww.exemplo.com.br\nMais texto.")
        self.assertNotIn("www.exemplo.com.br", out)
        self.assertIn("Texto útil.", out)
        self.assertIn("Mais texto.", out)

    def test_remover_sites_residuais_handles_ocr_digit_and_spacing_noise(self):
        out = converter._remover_sites_residuais("www.g/7juridico.com.br\nWWW . G7JURIDICO . COM . BR")
        self.assertNotIn("juridico", out.lower())
        self.assertNotIn("www", out.lower())


if __name__ == "__main__":
    unittest.main()
