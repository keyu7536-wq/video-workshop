"""Rate-limit fallback preserves pinned source provenance through Research."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import studio_research as research
from tests.test_selective_repository import BASE, COMMIT, fixture_reader, page


class SelectiveResearchIntegrationTests(unittest.TestCase):
    def test_rate_limit_reads_selected_raw_files_without_download_archive(self):
        low_read, _ = fixture_reader()
        calls = []
        root = page(children=[('README.md','file'),('LICENSE','file'),('src','directory')])
        root = root.replace(('"name": "'+COMMIT+'"').encode(), b'"name": "main"')
        root = root.replace(b'"refType": "tree"', b'"refType": "branch"')
        def fetch(url, limit):
            calls.append(url)
            if url.startswith('https://api.github.com/'):
                raise HTTPError(url, 403, 'rate limit exceeded', {}, None)
            if url==BASE: return root,url,'text/html'
            return low_read(url, 'test', limit)
        with tempfile.TemporaryDirectory() as folder:
            job=Path(folder);(job/'analysis').mkdir()
            reader=research.Research(job,{},lambda *args:{'paths':['src/method.py','src/config.yaml','../secret']})
            with patch.object(research,'fetch',side_effect=fetch):
                reader.repo(BASE)
            metadata=json.loads((reader.folder/'repo_author_project.json').read_text(encoding='utf-8'))
            self.assertEqual(metadata['commit'], COMMIT)
            self.assertEqual(metadata['transport'], 'public_selective_files')
            self.assertEqual(set(metadata['read_paths']), {'README.md','LICENSE','src/method.py','src/config.yaml'})
            self.assertFalse(any('codeload' in url for url in calls))
            sources=[s for s in reader.sources if s['provenance']=='repository_file']
            self.assertEqual(len(sources),4)
            self.assertTrue(all(s['repository_commit']==COMMIT for s in sources))
            self.assertTrue(all('/blob/'+COMMIT+'/' in s['url'] for s in sources))
            records=[r for r in reader.records if r['kind']=='repository_file' and 'sha256' in r]
            self.assertEqual(len(records),4)
            self.assertTrue(all(r['commit']==COMMIT for r in records))
            self.assertFalse((job/'src'/'method.py').exists())


if __name__=='__main__':
    unittest.main()
