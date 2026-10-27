"""Portable workflow tests using only small, original, runtime-created books.

These tests verify persistence, coverage and assembly, not model translation
quality. No EPUB fixtures, private projects, network access or model calls are
required. Run: python -m unittest discover -s tests -v
"""
from __future__ import annotations

import copy
import html
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import epub_workflow as workflow
import plan_chunks as planner
import chunk_pipeline as pipeline


CAPACITY = {"context_window": 32768, "max_output_tokens": 8192, "expansion_ratio": 12}
SENTENCES = ["La lampe éclaire la table.", "Je cherche mon carnet.", "Enfin, je retrouve la page."]
GUIDANCE = {
    SENTENCES[0]: {
        "language": ["The subject <i>la lampe</i> means the lamp. The present-tense verb <i>éclaire</i>, from <i>éclairer</i>, means lights up. Its direct object is <i>la table</i>, the table."],
        "translation": "The lamp lights up the table.",
        "thought": ["The opening places a visible object under a light, establishing the place where the narrator will search."],
    },
    SENTENCES[1]: {
        "language": ["The pronoun <i>je</i> means I. The present-tense <i>cherche</i>, from <i>chercher</i>, means am looking for. The possessive <i>mon</i> marks <i>carnet</i>, notebook, as belonging to the narrator."],
        "translation": "I am looking for my notebook.",
        "thought": ["The narrator's search now gives the scene a purpose. The attached note explains that this notebook records observations."],
    },
    SENTENCES[2]: {
        "language": ["The adverb <i>enfin</i> means finally. The verb <i>retrouve</i>, from <i>retrouver</i>, means find again; <i>la page</i> is the page."],
        "translation": "Finally, I find the page again.",
        "thought": ["The search reaches a small resolution: the narrator recognizes the page that was missing."],
    },
}


def xhtml(title, body):
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:epub="http://www.idpf.org/2007/ops" lang="fr">'
            f'<head><title>{html.escape(title)}</title></head><body>{body}</body></html>')


def write_book(path, chapters=None, with_note=True, with_image=False):
    """Build a tiny source archive entirely from text authored for these tests."""
    if chapters is None:
        call = '<a class="noteref" id="call-1" href="notes.xhtml#n1"><sup>1</sup></a>' if with_note else ""
        chapters = [
            ("La table", f"<p>{SENTENCES[0]}</p><p>{SENTENCES[1]}{call}</p>"),
            ("La page", f"<p>{SENTENCES[2]}</p>"),
        ]
    manifest, spine = [], []
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="EPUB/content.opf"/></rootfiles></container>')
        for number, (title, body) in enumerate(chapters, 1):
            name = f"chapter{number}.xhtml"
            manifest.append(f'<item id="c{number}" href="{name}" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="c{number}"/>')
            if with_image and number == 1:
                body += '<img src="diagram.svg" alt="A source diagram requiring review"/>'
            archive.writestr("EPUB/" + name, xhtml(title, f"<h1>{title}</h1>" + body))
        if with_note:
            manifest.append('<item id="notes" href="notes.xhtml" media-type="application/xhtml+xml"/>')
            spine.append('<itemref idref="notes"/>')
            archive.writestr("EPUB/notes.xhtml", xhtml("Notes", '<p class="note"><a id="n1"></a>Le carnet sert à noter les observations.</p>'))
        if with_image:
            manifest.append('<item id="diagram" href="diagram.svg" media-type="image/svg+xml"/>')
            archive.writestr("EPUB/diagram.svg", '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><circle cx="5" cy="5" r="4"/></svg>')
        archive.writestr("EPUB/content.opf", '<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>The little search</dc:title></metadata><manifest>' + "".join(manifest) + '</manifest><spine>' + "".join(spine) + '</spine></package>')
    return path


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="guided-reading-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = write_book(self.root / "source.epub")
        self.project = self.initialize(self.source, "project")

    def initialize(self, source, name, with_note=True):
        project = self.root / name
        workflow.command_init(SimpleNamespace(
            source=source, project=project, title="A guided reading test",
            source_language="fr", output=project / "Master.epub", start_member=None,
            skip_member=["EPUB/notes.xhtml"] if with_note else [],
        ))
        return project

    def json_file(self, name, value):
        path = self.root / name
        workflow.atomic_json(path, value)
        return path

    def draft(self, plan, chunk):
        units = []
        for span in chunk["spans"]:
            text = workflow.norm(span["text"])
            if not text:
                continue
            guide = copy.deepcopy(GUIDANCE.get(text, {
                "language": ["This original test fragment exercises exact source alignment across a split paragraph."],
                "translation": "A structural fixture fragment, not a translation-quality example.",
                "thought": ["This fixture checks that the original survives splitting and final assembly in order."],
            }))
            units.append({"source_ps": [span["source_p"]], "original": span["text"], "lang": "fr", **guide})
        return {"plan_sha256": plan["plan_sha256"], "chunk_id": chunk["chunk_id"],
                "units": units, "footnotes_integrated": [note["id"] for note in chunk["footnotes"]]}

    def save_all(self, project, plan):
        for chunk in plan["chunks"]:
            pipeline.save(project, self.json_file(chunk["chunk_id"] + ".json", self.draft(plan, chunk)))

    def test_plans_whole_source_and_attaches_notes_before_generation(self):
        plan = planner.create_plan(self.project, CAPACITY)
        self.assertEqual(plan["coverage"]["remaining_paragraphs"], 3)
        self.assertEqual(len(plan["chapters"]), 2)
        self.assertEqual(len(plan["chunks"]), 2)
        self.assertEqual([note["id"] for note in plan["chunks"][0]["footnotes"]], ["n1"])
        self.assertIn("observations", plan["chunks"][0]["footnotes"][0]["text"])
        self.assertFalse((self.project / "Master.epub").exists())
        self.assertFalse((self.project / "chunk-progress.json").exists())
        self.assertEqual(planner.create_plan(self.project, CAPACITY), plan)

    def test_drafts_are_separate_resumeable_and_do_not_publish(self):
        plan = planner.create_plan(self.project, CAPACITY)
        first, second = plan["chunks"]
        first_path = self.json_file("first.json", self.draft(plan, first))
        pipeline.save(self.project, first_path)
        ledger_before = (self.project / "chunk-progress.json").read_bytes()
        result = pipeline.save(self.project, first_path)
        self.assertEqual(result["status"], "UNCHANGED")
        self.assertEqual((self.project / "chunk-progress.json").read_bytes(), ledger_before)
        self.assertEqual(pipeline.next_chunk(self.project)["chunk"]["chunk_id"], second["chunk_id"])
        self.assertEqual(len(list((self.project / "chunk-drafts").glob("*.json"))), 1)
        self.assertEqual(workflow.read_json(self.project / "workflow.json")["commits"], [])
        self.assertFalse((self.project / "Master.epub").exists())
        with self.assertRaises(workflow.WorkflowError):
            pipeline.assemble(self.project)
        pipeline.save(self.project, self.json_file("second.json", self.draft(plan, second)))
        self.assertEqual(pipeline.status(self.project)["phase"], "ready_to_assemble")
        self.assertFalse((self.project / "Master.epub").exists())

    def test_rejects_out_of_order_missing_fields_notes_and_changed_original(self):
        plan = planner.create_plan(self.project, CAPACITY)
        with self.assertRaises(workflow.WorkflowError):
            pipeline.save(self.project, self.json_file("second.json", self.draft(plan, plan["chunks"][1])))
        original = self.draft(plan, plan["chunks"][0])
        mutations = [
            lambda draft: draft["units"][0].update(original="A different source sentence."),
            lambda draft: draft["units"][0].update(thought=[]),
            lambda draft: draft.update(footnotes_integrated=[]),
            lambda draft: draft.update(plan_sha256="wrong-plan"),
            lambda draft: draft["units"][0].update(source_ps=[99]),
        ]
        for number, mutate in enumerate(mutations):
            with self.subTest(mutation=number):
                draft = copy.deepcopy(original)
                mutate(draft)
                with self.assertRaises(workflow.WorkflowError):
                    pipeline.save(self.project, self.json_file(f"bad-{number}.json", draft))
        self.assertFalse((self.project / "chunk-progress.json").exists())

    def test_final_assembly_preserves_order_reflow_and_single_chapter_titles(self):
        plan = planner.create_plan(self.project, CAPACITY)
        self.save_all(self.project, plan)
        report = pipeline.assemble(self.project)
        self.assertTrue(report["whole_book_complete"])
        self.assertEqual(report["annotated_reading_units"], 3)
        self.assertEqual(report["source_paragraphs_covered"], 3)
        master = self.project / "Master.epub"
        original_bytes = master.read_bytes()
        self.assertEqual(pipeline.assemble(self.project)["status"], "UNCHANGED")
        self.assertEqual(master.read_bytes(), original_bytes)
        self.assertEqual(workflow.command_audit(SimpleNamespace(project=self.project))["status"], "PASS")
        with zipfile.ZipFile(master) as archive:
            self.assertEqual(archive.infolist()[0].filename, "mimetype")
            self.assertEqual(archive.infolist()[0].compress_type, zipfile.ZIP_STORED)
            documents = [name for name in archive.namelist() if name.startswith("OEBPS/Text/c") and name.endswith(".xhtml")]
            texts = []
            for name in sorted(documents):
                tree = workflow.parse_xml(archive.read(name))
                self.assertEqual(len(tree.xpath('//*[local-name()="h1"]')), 1)
                self.assertEqual(tree.xpath('//*[local-name()="h2" or local-name()="h3"]'), [])
                texts.extend(workflow.norm(workflow.visible(node)) for node in tree.xpath('//*[local-name()="p"][@class="french"]'))
            self.assertEqual(texts, SENTENCES)
            self.assertNotIn("break-before: page", archive.read("OEBPS/Styles/reading.css").decode())

    def test_oversize_paragraphs_split_into_sentences_and_clauses_then_rejoin(self):
        sentences = [
            "La lumière du matin passe par la fenêtre ouverte et dessine une longue ligne sur la table où repose le cahier de la jeune voyageuse.",
            "Elle prend le temps de regarder cette ligne avant de préparer ses affaires et de choisir le chemin qui la conduira jusqu'au petit village.",
            "Son compagnon attend près de la porte avec une carte et lui montre le pont qu'ils devront traverser avant que la pluie commence à tomber.",
        ]
        clauses = [
            "La voyageuse ouvre le cahier pour vérifier le nom de la rue et se souvient de la lettre reçue la veille; ",
            "son compagnon ferme la fenêtre et range la carte dans une poche pour la protéger des premières gouttes; ",
            "ils quittent enfin la maison et suivent ensemble le chemin qui descend doucement vers la rivière.",
        ]
        source = write_book(self.root / "long.epub", [("Le départ", "<p>" + " ".join(sentences) + "</p><p></p><p>" + "".join(clauses) + "</p>")], with_note=False)
        project = self.initialize(source, "long", with_note=False)
        plan = planner.create_plan(project, {"context_window": 8192, "max_output_tokens": 1800, "overhead_tokens": 512, "expansion_ratio": 12})
        self.assertTrue({"sentence", "clause"}.issubset({span["split_level"] for chunk in plan["chunks"] for span in chunk["spans"]}))
        self.assertTrue(all(planner.fits(chunk["spans"], chunk["footnotes"], plan["capacity"]) for chunk in plan["chunks"]))
        self.save_all(project, plan)
        report = pipeline.assemble(project)
        self.assertTrue(report["whole_book_complete"])
        self.assertEqual(report["source_paragraphs_covered"], 3)
        self.assertEqual(workflow.command_audit(SimpleNamespace(project=project))["status"], "PASS")

    def test_plan_gaps_modified_sources_and_impossible_budgets_fail(self):
        plan = planner.create_plan(self.project, CAPACITY)
        bad = copy.deepcopy(plan)
        bad["chunks"][0]["spans"][0]["start"] = 1
        bad["plan_sha256"] = planner.fingerprint(bad)
        with self.assertRaises(workflow.WorkflowError):
            planner.validate_plan(self.project, bad)
        impossible = self.initialize(self.source, "impossible")
        with self.assertRaises(workflow.WorkflowError):
            planner.create_plan(impossible, {"context_window": 2048, "max_output_tokens": 400, "overhead_tokens": 256, "expansion_ratio": 12})
        self.assertFalse((impossible / "chunk-plan.json").exists())
        with self.source.open("ab") as stream:
            stream.write(b"changed")
        with self.assertRaises(workflow.WorkflowError):
            pipeline.next_chunk(self.project)

    def test_nonparagraph_material_blocks_whole_book_claim(self):
        source = write_book(self.root / "illustrated.epub", with_image=True)
        project = self.initialize(source, "illustrated")
        plan = planner.create_plan(project, CAPACITY)
        self.save_all(project, plan)
        with self.assertRaises(workflow.WorkflowError):
            pipeline.assemble(project)
        report = pipeline.assemble(project, allow_incomplete_source_review=True)
        self.assertFalse(report["whole_book_complete"])
        self.assertTrue(report["selected_paragraph_scope_complete"])

    def test_preserved_prefix_and_failed_publication_recover_without_rewriting(self):
        state = workflow.read_json(self.project / "workflow.json")
        units = [{"source_ps": [number], "original": sentence, **copy.deepcopy(GUIDANCE[sentence])}
                 for number, sentence in enumerate(SENTENCES[:2], 1)]
        batch = {"batch_id": "approved-prefix", "source_sha256": state["source_sha256"],
                 "sections": [{"source_member": "EPUB/chapter1.xhtml", "title": "La table", "start_p": 1,
                               "end_p": 2, "units": units, "footnotes_integrated": ["n1"]}]}
        workflow.command_commit(SimpleNamespace(project=self.project, batch=self.json_file("prefix.json", batch), defer_build=False))
        master = self.project / "Master.epub"
        prefix_bytes = master.read_bytes()
        plan = planner.create_plan(self.project, CAPACITY)
        self.assertEqual(plan["legacy_prefix"]["covered_paragraphs"], 2)
        self.assertEqual(len(plan["chunks"]), 1)
        self.save_all(self.project, plan)
        self.assertEqual(master.read_bytes(), prefix_bytes)
        original_publish = workflow.publish

        def fail_publication(*args, **kwargs):
            original_write = workflow.atomic_json
            failed = False

            def injected_write(path, value):
                nonlocal failed
                if Path(path) == self.project / "workflow.json" and not failed:
                    failed = True
                    raise OSError("Injected publication failure")
                return original_write(path, value)

            with patch.object(workflow, "atomic_json", injected_write):
                return original_publish(*args, **kwargs)

        with patch.object(workflow, "publish", fail_publication):
            with self.assertRaises(OSError):
                pipeline.assemble(self.project)
        self.assertEqual(master.read_bytes(), prefix_bytes)
        status = workflow.command_status(SimpleNamespace(project=self.project))
        self.assertTrue(status["assembly_pending"])
        audit = workflow.command_audit(SimpleNamespace(project=self.project))
        self.assertEqual(audit["annotated_reading_units"], 2)
        self.assertEqual(audit["generation_status"]["annotated_reading_units"], 3)
        pipeline.assemble(self.project)
        self.assertEqual(workflow.command_audit(SimpleNamespace(project=self.project))["annotated_reading_units"], 3)


if __name__ == "__main__":
    unittest.main()
