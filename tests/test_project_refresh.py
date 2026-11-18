"""Author landing pages may use an explicit static HTML refresh."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import studio_research as research


def refresh(target, delay='0'):
    return f'<meta http-equiv="refresh" content="{delay}; url={target}">'.encode()


def project_html():
    return b'''<p>Actual author project method and results with enough source text.</p>
        <video src="demo.mp4"></video>
        <a href="https://github.com/author/project">Code</a>'''


class ProjectRefreshTests(unittest.TestCase):
    def reader(self, folder):
        job=Path(folder);(job/'analysis').mkdir();(job/'assets').mkdir()
        return research.Research(job,{},None)

    def test_static_refresh_reaches_media_repo_and_records_source_chain(self):
        original='https://author.example/old'
        final='https://author.example/project/new/'
        with tempfile.TemporaryDirectory() as folder:
            reader=self.reader(folder)
            with patch.object(research,'fetch',side_effect=[
                    (refresh('/project/new/'),original+'/', 'text/html'),
                    (project_html(),final,'text/html')]) as fetch, \
                 patch.object(research,'public_url',side_effect=lambda u:u) as guard, \
                 patch.object(reader,'repo') as repo:
                reader.project(original)
            self.assertEqual(fetch.call_count,2)
            guard.assert_called_once_with(final)
            repo.assert_called_once_with('https://github.com/author/project')
            self.assertIn((final+'demo.mp4',final),reader.media)
            self.assertEqual(reader.sources[0]['url'],final)
            first=reader.records[0]
            self.assertEqual(first['meta_refresh']['target_url'],final)
            self.assertEqual(first['meta_refresh']['status'],'followed')
            self.assertEqual(reader.records[-1]['project_entry_url'],original)
            self.assertEqual(len(reader.records[-1]['meta_refresh_chain']),1)
            self.assertEqual(fetch.call_args_list[1].args[1],
                             research.DOCUMENT_BYTES-len(refresh('/project/new/')))

    def test_parser_accepts_case_spaces_relative_target_and_quoted_short_delay(self):
        page=research.Page('https://author.example/a/old')
        page.feed("""<META HTTP-EQUIV="Refresh" CONTENT=" 1.5 ; URL='../new?q=1&amp;x=2' ">""")
        self.assertEqual(page.refreshes,[{'delay':1.5,'url':'https://author.example/new?q=1&x=2'}])

    def test_javascript_and_canonical_do_not_trigger_fetch(self):
        url='https://author.example/old'
        body=b'''<link rel="canonical" href="/new"><script>location="/new"</script>
            <p>Author page content without an explicit HTML refresh.</p>'''
        with tempfile.TemporaryDirectory() as folder:
            reader=self.reader(folder)
            with patch.object(research,'fetch',return_value=(body,url,'text/html')) as fetch:
                reader.project(url)
            fetch.assert_called_once()
            self.assertEqual(reader.sources[0]['url'],url)

    def test_long_delay_is_recorded_and_not_followed(self):
        url='https://author.example/old'
        with tempfile.TemporaryDirectory() as folder:
            reader=self.reader(folder)
            with patch.object(research,'fetch',return_value=(refresh('/new','30'),url,'text/html')) as fetch:
                reader.project(url)
            fetch.assert_called_once()
            self.assertTrue(any('meta refresh' in gap for gap in reader.gaps))

    def test_private_refresh_target_is_rejected_before_second_fetch(self):
        url='https://author.example/old'
        with tempfile.TemporaryDirectory() as folder:
            reader=self.reader(folder)
            with patch.object(research,'fetch',return_value=(refresh('http://127.0.0.1/private'),url,'text/html')) as fetch:
                reader.project(url)
            fetch.assert_called_once()
            self.assertEqual(reader.records[0]['meta_refresh']['status'],'rejected')
            self.assertTrue(reader.gaps)

    def test_loop_is_bounded_including_fragment_alias(self):
        first='https://author.example/first'
        second='https://author.example/second'
        with tempfile.TemporaryDirectory() as folder:
            reader=self.reader(folder)
            with patch.object(research,'fetch',side_effect=[
                    (refresh(second),first,'text/html'),
                    (refresh(first+'#again'),second,'text/html')]) as fetch, \
                 patch.object(research,'public_url',side_effect=lambda u:u):
                reader.project(first)
            self.assertEqual(fetch.call_count,2)
            self.assertEqual(reader.records[-1]['meta_refresh']['status'],'loop')
            self.assertTrue(reader.gaps)

    def test_no_more_than_three_html_refresh_hops(self):
        base='https://author.example/'
        def fake(url,limit):
            number=int(url.rsplit('/',1)[-1])
            return refresh(base+str(number+1)),url,'text/html'
        with tempfile.TemporaryDirectory() as folder:
            reader=self.reader(folder)
            with patch.object(research,'fetch',side_effect=fake) as fetch, \
                 patch.object(research,'public_url',side_effect=lambda u:u):
                reader.project(base+'0')
            self.assertEqual(fetch.call_count,4)
            self.assertEqual(reader.records[-1]['meta_refresh']['status'],'hop_limit')
            self.assertTrue(reader.gaps)

    def test_conflicting_refresh_targets_are_not_guessed(self):
        url='https://author.example/old'
        with tempfile.TemporaryDirectory() as folder:
            reader=self.reader(folder)
            with patch.object(research,'fetch',return_value=(
                    refresh('/first')+refresh('/second'),url,'text/html')) as fetch:
                reader.project(url)
            fetch.assert_called_once()
            self.assertTrue(any('meta refresh' in gap for gap in reader.gaps))


if __name__=='__main__':
    unittest.main()
