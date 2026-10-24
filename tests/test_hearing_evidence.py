from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

from collectors.hearing_evidence import (
    ATTACHED_CURRENT_LAW, DRAFT_NEW_PROVISIONS, MAX_BYTES, MAX_DOCUMENTS,
    _OfficialRedirect, _official, discover_drafts, enrich_hearing, extract_fragments,
)
from collectors.hoeringsportalen import FeedConfig
from collectors.main import collect
from collectors.ingestion_client import prepare_records
from collectors.relevance import classify

FIXTURES = Path(__file__).parent / 'fixtures' / 'hearing_evidence'
URL = 'https://hoeringsportalen.dk/Hearing/Details/70923'
DRAFT = discover_drafts((FIXTURES / '70923.html').read_bytes(), URL)[0]


def hearing():
    return {'source': 'hoeringsportalen', 'external_id': '70923', 'title': 'Forslag om udlændingeloven',
            'url': URL, 'content_text': 'Høringsfrist: 23-01-2026', 'feed_codes': ['immigration_ministry']}


class EvidenceTests(unittest.TestCase):
    def test_official_attachment_discovery_selects_draft_only(self):
        self.assertEqual(discover_drafts((FIXTURES/'70923.html').read_bytes(), URL), [DRAFT])

    def test_untrusted_destinations_and_redirects_rejected(self):
        for url in ('http://hoeringsportalen.dk/a', 'https://evil.org/a',
                    'https://hoeringsportalen.dk.evil.org/a', 'https://user:password@hoeringsportalen.dk/a',
                    'https://hoeringsportalen.dk:999/a'):
            self.assertFalse(_official(url))
            self.assertEqual(discover_drafts(f'<table><tr class="documentGroup"><td>Høringsdokument</td></tr><tr><td><a href="{url}">Lovforslag</a></td></tr></table>'.encode(), URL), [])
        with self.assertRaisesRegex(ValueError, 'redirect_rejected'):
            _OfficialRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.org/a')

    def test_navigation_links_are_not_attachments(self):
        self.assertEqual(discover_drafts(f'<a href="{DRAFT}">Lovforslag</a>'.encode(), URL), [])

    def test_reply_group_cannot_be_mislabelled_as_proposed_draft(self):
        html = f'<table><tr class="documentGroup"><td>Høringssvar</td></tr><tr><td><a href="{DRAFT}">Forslag til lov om ophold</a></td></tr></table>'
        self.assertEqual(discover_drafts(html.encode(), URL), [])

    def test_discovery_document_bound_fails_closed(self):
        html = ''.join(f'<a href="https://hoeringsportalen.dk/draft/{i}">Lovforslag</a>' for i in range(MAX_DOCUMENTS+1))
        with self.assertRaisesRegex(ValueError, 'document_limit'):
            discover_drafts(('<table><tr class="documentGroup"><td>Høringsdokument</td></tr>'+html+"</table>").encode(), URL)

    def test_html_extracts_provenance_and_ignores_current_law(self):
        html = '<h1>Forslag om vejloven</h1><p>Nye vejregler uden opholdsregler.</p><h2>Gældende lov</h2><p>Fordrevne fra Ukraine har opholdstilladelse.</p>'.encode()
        fragments = extract_fragments(html, 'text/html', DRAFT)
        self.assertEqual([f.kind for f in fragments], [DRAFT_NEW_PROVISIONS, ATTACHED_CURRENT_LAW])
        result = classify({'source': 'hoeringsportalen', 'title': 'Forslag om vejloven',
                           'evidence_fragments': [f.to_dict() for f in fragments]})
        self.assertNotEqual(result.relevance, 'DIRECT')
        self.assertFalse(any(rule.startswith('direct.') for rule in result.matched_rules))
        self.assertNotIn('Ukraine', fragments[0].text)

    def test_appendix_reference_in_prose_is_not_an_appendix_boundary(self):
        html = '<h1>Forslag</h1><p>Bilag 2 i visumbekendtgørelsen beskriver lande.</p><p>Ukraine er omfattet af den nye opholdstilladelse.</p>'
        fragments = extract_fragments(html.encode(), 'text/html', DRAFT)
        self.assertTrue(any('Ukraine' in f.text and f.kind == DRAFT_NEW_PROVISIONS for f in fragments))

    def test_separate_current_law_attachment_retains_provenance(self):
        fragments = extract_fragments('<h1>Gældende lov</h1><p>Ukrainesærloven og fordrevne fra Ukraine har opholdstilladelse.</p>'.encode(), 'text/html', DRAFT)
        self.assertEqual([f.kind for f in fragments], [ATTACHED_CURRENT_LAW])

    def test_current_law_only_cannot_establish_any_relevance(self):
        result = classify({'source': 'hoeringsportalen', 'title': 'Ændring af vejloven',
                           'evidence_fragments': [{'kind': ATTACHED_CURRENT_LAW, 'text': 'Ukrainesærloven'}]})
        self.assertEqual(result.relevance, 'IRRELEVANT')

    def test_unknown_provenance_does_not_promote(self):
        self.assertEqual(classify({'source': 'hoeringsportalen', 'title': 'Vejloven',
                                  'evidence_fragments': [{'kind': 'invented', 'text': 'Ukrainesærloven'}]}).relevance, 'IRRELEVANT')

    def test_real_70923_collect_discovery_pdf_provenance_classification_preparation(self):
        calls = []
        def fetch(url):
            calls.append(url)
            if url == URL:
                return (FIXTURES/'70923.html').read_bytes(), 'text/html'
            self.assertEqual(url, DRAFT)
            return (FIXTURES/'70923-draft-pages.pdf').read_bytes(), 'application/pdf'
        result, failed = collect((FeedConfig('immigration_ministry', 'https://hoeringsportalen.dk/feed'),),
                                 lambda _: (FIXTURES/'70923.atom').read_bytes(), evidence_fetcher=fetch)
        self.assertFalse(failed)
        record = result['publications'][0]
        self.assertEqual(calls, [URL, DRAFT])
        self.assertEqual(record['evidence_status'], 'COMPLETE')
        new_text = '\n'.join(f['text'] for f in record['evidence_fragments'] if f['kind'] == DRAFT_NEW_PROVISIONS)
        self.assertIn('Ukraine', new_text)
        self.assertTrue(any(f['kind'] == ATTACHED_CURRENT_LAW for f in record['evidence_fragments']))
        classification = classify(record)
        self.assertEqual(classification.relevance, 'POTENTIAL')
        self.assertIn('evidence.DRAFT_NEW_PROVISIONS', classification.evidence_fields)
        self.assertIn('potential.residence_immigration_law', classification.matched_rules)
        self.assertNotIn('insufficient_scope_evidence', classification.diagnostics)
        prepared = prepare_records(result, 'hoeringsportalen', 'incremental')[0]
        self.assertEqual(prepared['relevance'], 'POTENTIAL')
        self.assertNotIn('evidence_fragments', prepared['source_metadata'])

    def test_draft_fetch_failure_is_local_and_safe(self):
        def fetch(url):
            if url == URL: return (FIXTURES/'70923.html').read_bytes(), 'text/html'
            raise OSError('PRIVATE token title url text')
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = enrich_hearing(hearing(), fetcher=fetch)
        self.assertEqual(result['evidence_status'], 'UNAVAILABLE')
        self.assertEqual(result['evidence_diagnostics'], ['draft_evidence_unavailable'])
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), '')
        self.assertEqual(classify(result).relevance, 'POTENTIAL')
        self.assertIn('insufficient_scope_evidence', classify(result).diagnostics)

    def test_one_unavailable_attachment_does_not_fail_collection_or_preparation(self):
        result, failed = collect((FeedConfig('immigration_ministry', 'https://hoeringsportalen.dk/feed'),),
                                 lambda _: (FIXTURES/'70923.atom').read_bytes(),
                                 evidence_fetcher=Mock(side_effect=TimeoutError('private')))
        self.assertFalse(failed)
        self.assertEqual(len(result['publications']), 1)
        self.assertEqual(result['publications'][0]['evidence_status'], 'UNAVAILABLE')
        self.assertEqual(prepare_records(result, 'hoeringsportalen', 'incremental')[0]['relevance'], 'POTENTIAL')

    def test_scanned_pdf_is_unavailable_without_ocr(self):
        from pypdf import PdfWriter
        pdf = io.BytesIO(); writer = PdfWriter(); writer.add_blank_page(width=100, height=100); writer.write(pdf)
        with self.assertRaisesRegex(ValueError, 'pdf_nontext'):
            extract_fragments(pdf.getvalue(), 'application/pdf', DRAFT)

    def test_invalid_nontext_oversize_and_unavailable_page(self):
        for data, mime in ((b'binary', 'image/png'), (b'invalid pdf', 'application/pdf'),
                           (b'x'*(MAX_BYTES+1), 'text/html')):
            record = enrich_hearing(hearing(), fetcher=lambda url: ((FIXTURES/'70923.html').read_bytes(), 'text/html') if url == URL else (data,mime))
            self.assertEqual(record['evidence_status'], 'UNAVAILABLE')
        record = enrich_hearing(hearing(), fetcher=Mock(side_effect=TimeoutError('private')))
        self.assertEqual(record['evidence_status'], 'UNAVAILABLE')

    def test_unknown_document_is_not_assumed_to_be_draft(self):
        fragments = extract_fragments('<p>Ukrainesærloven existing unrelated attachment text only.</p>'.encode(), 'text/html', DRAFT)
        self.assertEqual(fragments[0].kind, 'OTHER_OFFICIAL_ATTACHMENT')

    def test_unrelated_hearing_does_not_download_attachments(self):
        fetch = Mock()
        result = enrich_hearing({**hearing(), 'title': 'Vejloven', 'content_text': ''}, fetcher=fetch)
        fetch.assert_not_called()
        self.assertEqual(result['evidence_status'], 'NOT_NEEDED')

    def test_mixed_availability_and_unknown_fragments_are_partial(self):
        html = f'<table><tr class="documentGroup"><td>Høringsdokument</td></tr><tr><td><a href="{DRAFT}">Lovforslag</a><a href="https://hoeringsportalen.dk/second">Udkast</a></td></tr></table>'.encode()
        def fetch(url):
            if url == URL:return html,'text/html'
            if url == DRAFT:return b'<h1>Forslag</h1><p>Ny ordning giver ukrainere opholdstilladelse under bestemte betingelser.</p>','text/html'
            raise TimeoutError()
        result = enrich_hearing(hearing(), fetcher=fetch)
        self.assertEqual(result['evidence_status'], 'PARTIAL')
        self.assertEqual(classify(result).relevance, 'POTENTIAL')
