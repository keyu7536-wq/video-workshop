"""Public discovery must distinguish access failures from an empty search."""
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

from studio_search import search_public


def atom(title="A Public Study", comment="Website: https://author.example/project"):
    return (f'''<feed xmlns="http://www.w3.org/2005/Atom"
        xmlns:arxiv="http://arxiv.org/schemas/atom">
        <entry><id>http://arxiv.org/abs/1234.56789v2</id>
        <title>{title}</title><author><name>A Researcher</name></author>
        <summary>Code: https://github.com/author/example</summary>
        <arxiv:comment>{comment}</arxiv:comment>
        <link rel="alternate" href="https://arxiv.org/abs/1234.56789v2"/>
        <link rel="related" type="application/pdf" href="https://arxiv.org/pdf/1234.56789v2"/>
        </entry></feed>''').encode()


class PublicSearchTests(unittest.TestCase):
    def setUp(self):
        self.spacing=patch('studio_search._pause_for_arxiv')
        self.spacing.start()
        self.addCleanup(self.spacing.stop)

    def test_challenge_falls_back_to_title_metadata_and_keeps_version_provenance(self):
        fetch=Mock(side_effect=[
            (b'<form id="challenge-form" action="/anomaly.js">Verify you are human</form>',
             'https://html.duckduckgo.com/html/', 'text/html'),
            (atom(), 'https://export.arxiv.org/api/query', 'application/atom+xml')])
        result=search_public('A Public Study',fetch)
        self.assertIsInstance(result,list)
        self.assertEqual(result[0],'https://author.example/project')
        self.assertIn('https://github.com/author/example',result)
        self.assertNotIn('https://arxiv.org/pdf/1234.56789v2',result)
        self.assertEqual(result.attempts[0]['status'],'challenge')
        self.assertEqual(result.attempts[1]['provider'],'arxiv_title')
        source=next(p for p in result.provenance if p['url']==result[0])
        self.assertEqual(source['title'],'A Public Study')
        self.assertEqual(source['authors'],['A Researcher'])
        self.assertTrue(source['record_url'].endswith('1234.56789v2'))
        query=parse_qs(urlparse(fetch.call_args_list[1].args[0]).query)
        self.assertEqual(query['search_query'],['ti:"A Public Study"'])
        self.assertEqual(fetch.call_count,2)

    def test_ddg_result_success_does_not_call_another_provider(self):
        body=b'''<a href="https://unrelated.example/privacy">Privacy</a>
        <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fauthor.example%2Fpaper">Paper</a>
        <a class="result__a" href="https://author.example/paper">duplicate</a>'''
        fetch=Mock(return_value=(body,'https://html.duckduckgo.com/html/','text/html'))
        result=search_public('A Public Study',fetch)
        self.assertEqual(result,['https://author.example/paper'])
        fetch.assert_called_once()
        self.assertEqual(result.provenance[0]['provider'],'duckduckgo_html')

    def test_rate_limit_is_recorded_without_retrying_same_provider(self):
        fetch=Mock(side_effect=[
            HTTPError('https://html.duckduckgo.com/html/',429,'Too Many Requests',{},None),
            (atom(),'https://export.arxiv.org/api/query','application/atom+xml')])
        result=search_public('A Public Study',fetch)
        self.assertEqual(fetch.call_count,2)
        self.assertEqual(result.attempts[0]['status'],'error')
        self.assertEqual(result.attempts[0]['http_status'],429)
        self.assertIn('HTTPError',result.attempts[0]['reason'])
        self.assertTrue(result)

    def test_similarly_named_paper_is_not_claimed_as_exact_title_match(self):
        fetch=Mock(side_effect=[
            (b'<p>No results</p>','https://html.duckduckgo.com/html/','text/html'),
            (atom(title='A Different Public Study'),'https://export.arxiv.org/api/query','application/atom+xml')])
        result=search_public('A Public Study',fetch)
        self.assertEqual(result,[])
        self.assertEqual(result.attempts[-1]['status'],'no_exact_match')
        self.assertEqual(result.provenance,[])

    def test_bad_metadata_is_reported_without_fabricated_results(self):
        fetch=Mock(side_effect=[
            (b'<p>No results</p>','https://html.duckduckgo.com/html/','text/html'),
            (b'<feed>broken','https://export.arxiv.org/api/query','application/atom+xml')])
        result=search_public('A Public Study',fetch)
        self.assertEqual(result,[])
        self.assertEqual(result.attempts[-1]['status'],'invalid_response')
        self.assertIn('ParseError',result.attempts[-1]['reason'])

    def test_blank_query_never_fetches_and_overlong_query_is_rejected(self):
        fetch=Mock()
        result=search_public('  ',fetch)
        self.assertEqual(result,[])
        fetch.assert_not_called()
        with self.assertRaises(ValueError):search_public('a'*241,fetch)
        fetch.assert_not_called()

    def test_extracted_links_are_bounded_and_exclude_nonpublic_or_credential_urls(self):
        links=' '.join(['http://127.0.0.1/private','http://user:secret@example.com/x',
                        'https://localhost/x']+[f'https://author.example/item{i}' for i in range(20)])
        fetch=Mock(side_effect=[
            (b'No results','https://html.duckduckgo.com/html/','text/html'),
            (atom(comment=links),'https://export.arxiv.org/api/query','application/atom+xml')])
        result=search_public('A Public Study',fetch)
        self.assertEqual(len(result),10)
        self.assertTrue(all(url.startswith('https://author.example/') for url in result))


if __name__=='__main__':
    unittest.main()
