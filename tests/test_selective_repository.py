"""A bounded public tree can supply selected source without a full archive."""
import hashlib
import json
import subprocess
import unittest
from unittest.mock import patch

import studio_repository as repository


COMMIT = 'a'*40
TREE = 'b'*40
BASE = 'https://github.com/author/project'
RAW = 'https://raw.githubusercontent.com/author/project/'+COMMIT+'/'


def page(directory='', children=(), commit=COMMIT, public=True):
    items = [{'path': (directory+'/' if directory else '')+name, 'contentType': kind}
             for name, kind in children]
    route = {'path': directory, 'refInfo': {'name': COMMIT, 'refType': 'tree', 'currentOid': commit},
             'tree': {'items': items, 'totalCount': len(items)}}
    payload = {'payload': {'codeViewTreeRoute': route, 'codeViewLayoutRoute': {'repo': {
        'name': 'project', 'ownerLogin': 'author', 'defaultBranch': 'main',
        'public': public, 'private': not public}}}}
    return ('<script type="application/json" data-target="react-app.embeddedData">'+
            json.dumps(payload)+'</script>').encode()


def fixture_reader(root=None, directories=None, files=None):
    root = root if root is not None else [('README.md', 'file'), ('src', 'directory'), ('LICENSE', 'file')]
    directories = directories if directories is not None else {'src': [('method.py', 'file'), ('config.yaml', 'file')]}
    files = files if files is not None else {'README.md': b'Project overview.', 'LICENSE': b'MIT',
        'src/method.py': b'raise RuntimeError("This downloaded code must not execute")',
        'src/config.yaml': b'rate: 0.001\n'}
    calls = []
    def read(url, kind, limit):
        calls.append((url, kind, limit))
        if url == BASE+'/tree/'+COMMIT:
            return page(children=root), url, 'text/html'
        if url.startswith(BASE+'/tree/'+COMMIT+'/'):
            directory = url.split(COMMIT+'/', 1)[1]
            return page(directory, directories[directory]), url, 'text/html'
        if url.startswith(RAW):
            return files[url[len(RAW):]], url, 'text/plain'
        raise AssertionError('Unexpected URL '+url)
    return read, calls


class SelectiveRepositoryTests(unittest.TestCase):
    def test_public_tree_is_pinned_and_files_are_lazy_cached_bounded_reads(self):
        read, calls = fixture_reader()
        snapshot = repository.load_selective_snapshot('author', 'project', read, revision=COMMIT)
        self.assertEqual(set(snapshot.paths), {'README.md', 'LICENSE', 'src/method.py', 'src/config.yaml'})
        self.assertEqual(len(calls), 2)
        self.assertFalse(snapshot.metadata['directory_truncated'])
        self.assertIn('must not execute', snapshot.read_text('src/method.py'))
        snapshot.read_text('src/method.py')
        self.assertEqual(len(calls), 3)
        self.assertEqual(snapshot.source_url('src/method.py'), BASE+'/blob/'+COMMIT+'/src/method.py')
        self.assertEqual(snapshot.read_records['src/method.py']['commit'], COMMIT)
        self.assertEqual(len(snapshot.read_records['src/method.py']['sha256']), 64)
        self.assertFalse(any('api.github' in c[0] or 'codeload' in c[0] for c in calls))
        with self.assertRaises(ValueError): snapshot.read_text('../secret')

    def test_private_changed_revision_and_unsafe_entries_are_rejected(self):
        for body in (page(public=False), page(commit='c'*40),
                     page(children=[('../secret.py', 'file')]),
                     page(children=[('README.md', 'file'), ('README.md', 'file')])):
            with self.subTest(body=body):
                with self.assertRaises(ValueError):
                    repository.load_selective_snapshot('author', 'project',
                        lambda url,*args:(body,url,'text/html'), revision=COMMIT)

    def test_unknown_revision_is_not_accepted(self):
        for value in ('main', 'a'*7, '../main'):
            with self.assertRaises(ValueError):
                repository.load_selective_snapshot('author', 'project', None, revision=value)

    def test_github_fixed_commit_root_slash_is_accepted(self):
        body = page().replace(b'"path": ""', b'"path": "/"')
        snapshot = repository.load_selective_snapshot('author', 'project',
            lambda url,*args:(body,url,'text/html'), revision=COMMIT)
        self.assertEqual(snapshot.paths, [])

    def test_child_revision_or_failed_directory_is_an_explicit_partial_tree(self):
        read, calls = fixture_reader()
        def changed(url, kind, limit):
            if url.endswith('/src'): return page('src', [('method.py','file')], commit='c'*40),url,'text/html'
            return read(url, kind, limit)
        snapshot = repository.load_selective_snapshot('author','project',changed,revision=COMMIT)
        self.assertTrue(snapshot.metadata['directory_truncated'])
        self.assertEqual(snapshot.metadata['omitted_directories'][0]['path'], 'src')
        self.assertNotIn('src/method.py', snapshot.paths)

    def test_directory_budget_is_partial_not_an_unbounded_crawl(self):
        read, calls = fixture_reader()
        with patch.object(repository, 'MAX_DIRECTORY_PAGES', 1):
            snapshot = repository.load_selective_snapshot('author','project',read,revision=COMMIT)
        self.assertEqual(len(calls), 1)
        self.assertTrue(snapshot.metadata['directory_truncated'])
        self.assertEqual(snapshot.metadata['omitted_directories'][0]['path'], 'src')

    def test_only_small_text_files_are_read_and_file_limit_is_enforced(self):
        read, calls = fixture_reader(root=[('a.py','file'),('b.py','file'),('image.png','file')],
                                    files={'a.py':b'x', 'b.py':b'y'})
        snapshot = repository.load_selective_snapshot('author','project',read,revision=COMMIT)
        with self.assertRaises(ValueError): snapshot.read_text('image.png')
        with patch.object(repository, 'MAX_SELECTIVE_FILES', 1):
            self.assertEqual(snapshot.read_text('a.py'), 'x')
            with self.assertRaisesRegex(ValueError, 'budget'): snapshot.read_text('b.py')
        self.assertEqual(len(calls), 2)

    def test_binary_html_redirect_and_over_budget_text_are_not_source(self):
        for data, mime, redirect in [(b'\0binary','text/plain',False), (b'<html>failure','text/html',False),
                                      (b'x','text/plain',True), (b'x'*150000,'text/plain',False)]:
            read, calls = fixture_reader(root=[('README.md','file')])
            def invalid(url,kind,limit):
                if url.startswith(RAW): return data, 'https://example.org/README.md' if redirect else url,mime
                return read(url,kind,limit)
            snapshot = repository.load_selective_snapshot('author','project',invalid,revision=COMMIT)
            with self.subTest(mime=mime,redirect=redirect,bytes=len(data)):
                with self.assertRaises(ValueError): snapshot.read_text('README.md')
                self.assertEqual(snapshot.read_records,{})

    def test_optional_gh_failure_never_authorizes_and_falls_back_publicly(self):
        read, calls = fixture_reader()
        with patch('subprocess.run', return_value=subprocess.CompletedProcess([],1)) as run:
            snapshot = repository.load_selective_snapshot('author','project',read,revision=COMMIT,gh_executable='gh')
        self.assertIn('src/method.py', snapshot.paths)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][1:3], ['auth','status'])
        self.assertTrue(snapshot.metadata['gaps'])

    def test_authenticated_tree_uses_actual_tree_sha_and_checks_selected_blob_bytes(self):
        data = b'actual source'
        blob = hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
        read, calls = fixture_reader(root=[], files={'method.py':data})
        outputs = [subprocess.CompletedProcess([],0),
            subprocess.CompletedProcess([],0,json.dumps({'sha':COMMIT,'commit':{'tree':{'sha':TREE}}}).encode()),
            subprocess.CompletedProcess([],0,json.dumps({'sha':TREE,'tree':[
                {'path':'method.py','type':'blob','mode':'100644','size':len(data),'sha':blob},
                {'path':'link.py','type':'blob','mode':'120000','size':4,'sha':'c'*40}]}).encode())]
        with patch('subprocess.run',side_effect=outputs) as run:
            snapshot = repository.load_selective_snapshot('author','project',read,revision=COMMIT,gh_executable='gh')
        self.assertEqual(snapshot.metadata['tree_sha'], TREE)
        self.assertTrue(run.call_args.args[0][-1].endswith('/'+TREE+'?recursive=1'))
        self.assertEqual(snapshot.paths,['method.py'])
        self.assertEqual(snapshot.read_text('method.py'),'actual source')
        snapshot._texts.clear()
        snapshot._entries['method.py']['sha']='c'*40
        with self.assertRaisesRegex(ValueError,'bytes do not match'): snapshot.read_text('method.py')


if __name__ == '__main__':
    unittest.main()
