"""Rate-limited public GitHub repositories remain readable at one snapshot."""
import io
import json
from pathlib import Path
import tempfile
import unittest
import warnings
from unittest.mock import patch
from urllib.error import HTTPError
import zipfile

import studio_research as research
from studio_repository import load_public_snapshot


COMMIT='a'*40
ROOT='project-'+COMMIT+'/'


def public_page(public=True,commit=COMMIT):
    payload={'payload':{
        'codeViewRepoRoute':{'refInfo':{'name':'main','refType':'branch','currentOid':commit}},
        'codeViewLayoutRoute':{'repo':{
            'name':'project','ownerLogin':'author','defaultBranch':'main',
            'public':public,'private':not public}},
        'sidebarAbout':{'license':{'spdxId':'MIT','name':'MIT License'}}}}
    return ('<script type="application/json" data-target="react-app.embeddedData">'+
            json.dumps(payload)+'</script>').encode()


def archive(files=None,comment=COMMIT):
    stream=io.BytesIO()
    files=files or {'README.md':'Public project overview and usage documentation.',
                   'LICENSE':'MIT License; fixture license text.',
                   'method.py':'raise RuntimeError("Downloaded code must never execute")',
                   'config.yaml':'learning_rate: 0.001\n'}
    with zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED) as output:
        output.comment=comment.encode()
        for path,content in files.items():
            output.writestr(ROOT+path,content)
    return stream.getvalue()


def reader_fixture(folder):
    job=Path(folder);(job/'analysis').mkdir()
    return research.Research(job,{},lambda *args:{'paths':['method.py','config.yaml','../secret']})


class RepositoryFallbackTests(unittest.TestCase):
    def test_rate_limited_api_reads_only_selected_files_from_one_pinned_archive(self):
        calls=[]
        def fetch(url,limit):
            calls.append((url,limit))
            if url.startswith('https://api.github.com/'):
                raise HTTPError(url,403,'rate limit exceeded',{},None)
            if url=='https://github.com/author/project':
                return public_page(),url,'text/html'
            if url=='https://codeload.github.com/author/project/zip/'+COMMIT:
                return archive(),url,'application/zip'
            raise AssertionError('Unexpected request '+url)
        with tempfile.TemporaryDirectory() as folder:
            reader=reader_fixture(folder)
            with patch.object(research,'fetch',side_effect=fetch), patch(
                    'studio_repository.load_selective_snapshot',side_effect=ValueError('Selective fixture unavailable')):
                reader.repo('https://github.com/author/project')
            self.assertEqual(len(calls),3)
            meta=json.loads((reader.folder/'repo_author_project.json').read_text(encoding='utf-8'))
            self.assertEqual(meta['commit'],COMMIT)
            self.assertIsNone(meta['tree_sha'])
            self.assertEqual(set(meta['selected_paths']),{'README.md','LICENSE','method.py','config.yaml'})
            self.assertEqual(len(meta['archive_sha256']),64)
            files=[source for source in reader.sources if source['provenance']=='repository_file']
            self.assertEqual(len(files),4)
            self.assertTrue(all(source['repository_commit']==COMMIT for source in files))
            self.assertTrue(any('Downloaded code must never execute' in s['text'] for s in files))
            self.assertTrue(any('rate limit exceeded' in gap for gap in reader.gaps))
            self.assertFalse((Path(folder)/'method.py').exists())

    def test_permission_or_not_found_errors_never_trigger_alternate_access(self):
        for code,reason in [(403,'Forbidden'),(404,'Not Found'),(401,'Unauthorized')]:
            with self.subTest(code=code),tempfile.TemporaryDirectory() as folder:
                reader=reader_fixture(folder)
                with patch.object(research,'fetch',side_effect=HTTPError(
                        'https://api.github.com/repos/author/project',code,reason,{},None)) as fetch:
                    reader.repo('https://github.com/author/project')
                fetch.assert_called_once()
                self.assertEqual(reader.sources,[])

    def test_private_or_mismatched_page_does_not_fetch_archive(self):
        for body in [public_page(public=False),public_page().replace(b'"author"',b'"other"')]:
            with self.subTest(body=body):
                calls=[]
                def read(url,*args):
                    calls.append(url);return body,url,'text/html'
                with self.assertRaises(ValueError):load_public_snapshot('author','project',read)
                self.assertEqual(calls,['https://github.com/author/project'])

    def test_archive_revision_mismatch_is_rejected(self):
        def read(url,*args):
            return ((public_page() if 'codeload' not in url else archive(comment='b'*40)),
                    url,'text/html' if 'codeload' not in url else 'application/zip')
        with self.assertRaisesRegex(ValueError,'commit'):
            load_public_snapshot('author','project',read)

    def test_paths_and_uncompressed_budget_are_checked_before_file_reads(self):
        for files in [{'../outside.py':'x'}, {'a.py':'x','nested/../../escape':'x'}]:
            with self.subTest(files=files):
                def read(url,*args):
                    return (public_page() if 'codeload' not in url else archive(files),url,'text/html')
                with self.assertRaises(ValueError):load_public_snapshot('author','project',read)
        def read_large(url,*args):
            return (public_page() if 'codeload' not in url else archive({'large.txt':'x'*200}),url,'text/html')
        with patch('studio_repository.MAX_EXPANDED_BYTES',100):
            with self.assertRaisesRegex(ValueError,'uncompressed'):
                load_public_snapshot('author','project',read_large)

    def test_oversized_files_are_not_candidates_and_unlisted_paths_cannot_be_read(self):
        files={'README.md':'Safe overview text.','large.py':'x'*150001,'method.py':'print("read only")'}
        def read(url,*args):
            return (public_page() if 'codeload' not in url else archive(files),url,'text/html')
        snapshot=load_public_snapshot('author','project',read)
        self.assertNotIn('large.py',snapshot.paths)
        self.assertIn('method.py',snapshot.paths)
        with self.assertRaises(ValueError):snapshot.read_text('../secret')
        with self.assertRaises(ValueError):snapshot.read_text('large.py')
        self.assertEqual(snapshot.read_text('method.py'),'print("read only")')

    def test_duplicate_and_symlink_entries_are_not_read_as_regular_source(self):
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as z:
            z.comment=COMMIT.encode()
            info=zipfile.ZipInfo(ROOT+'link.py');info.create_system=3
            info.external_attr=(0o120777 << 16)
            z.writestr(info,'../../outside')
            z.writestr(ROOT+'README.md','Actual project README.')
        def read(url,*args):
            return (public_page() if 'codeload' not in url else stream.getvalue(),url,'text/html')
        snapshot=load_public_snapshot('author','project',read)
        self.assertNotIn('link.py',snapshot.paths)
        self.assertTrue(any(e['path']=='link.py' for e in snapshot.metadata['excluded_files']))
        duplicate=io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter('ignore',UserWarning)
            with zipfile.ZipFile(duplicate,'w') as z:
                z.writestr(ROOT+'same.py','one')
                z.writestr(ROOT+'same.py','two')
        def read_duplicate(url,*args):
            return (public_page() if 'codeload' not in url else duplicate.getvalue(),url,'text/html')
        with self.assertRaisesRegex(ValueError,'duplicate'):
            load_public_snapshot('author','project',read_duplicate)

    def test_rate_limit_at_tree_or_commit_stage_still_pins_archive_without_more_api_calls(self):
        for stage in ('tree','commit'):
            calls=[]
            def fetch(url,limit):
                calls.append(url)
                if 'api.github.com' in url:
                    if '/git/trees/' in url:
                        if stage=='tree':raise HTTPError(url,403,'rate limit exceeded',{},None)
                        value={'sha':'b'*40,'tree':[]}
                    elif '/commits/' in url:
                        raise HTTPError(url,403,'rate limit exceeded',{},None)
                    else:value={'default_branch':'main','license':None}
                    return json.dumps(value).encode(),url,'application/json'
                return (public_page() if 'codeload' not in url else archive(),url,
                        'text/html' if 'codeload' not in url else 'application/zip')
            with self.subTest(stage=stage),tempfile.TemporaryDirectory() as folder:
                reader=reader_fixture(folder)
                with patch.object(research,'fetch',side_effect=fetch), patch(
                        'studio_repository.load_selective_snapshot',side_effect=ValueError('Selective fixture unavailable')):
                    reader.repo('https://github.com/author/project')
                meta=json.loads((reader.folder/'repo_author_project.json').read_text(encoding='utf-8'))
                self.assertEqual(meta['commit'],COMMIT)
                self.assertIsNone(meta['tree_sha'])
                self.assertEqual(sum('api.github.com' in u for u in calls),2 if stage=='tree' else 3)
                self.assertFalse(any('raw.githubusercontent.com' in u for u in calls))


if __name__=='__main__':
    unittest.main()
