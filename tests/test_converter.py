import os
import sys
import unittest

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


if __name__ == "__main__":
    unittest.main()
