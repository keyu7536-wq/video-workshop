"""Read bounded, pinned public repository files; never execute downloaded code."""
from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import io
import json
import re
import stat
from urllib.parse import quote, urlparse
import zipfile
from pathlib import PurePosixPath


MAX_PAGE_BYTES = 3*1024**2
MAX_ARCHIVE_BYTES = 20*1024**2
MAX_EXPANDED_BYTES = 80*1024**2
MAX_ENTRIES = 10000
MAX_FILE_BYTES = 150000
MAX_SELECTIVE_FILES = 9
MAX_SELECTIVE_BYTES = 1024*1024
MAX_DIRECTORY_PAGES = 20
MAX_DIRECTORY_BYTES = 8*1024*1024
MAX_DIRECTORY_DEPTH = 5
TEXT_SUFFIXES = {'.py', '.md', '.txt', '.yaml', '.yml', '.json', '.toml', '.cpp', '.cc', '.c', '.h', '.hpp', '.rst', '.ini', '.cfg'}


def explicit_api_rate_limit(gaps, endpoint):
    """Only an explicit API 403 rate-limit failure permits this alternate read."""
    prefix=endpoint+': HTTPError:'
    return next((gap for gap in reversed(gaps)
                 if gap.startswith(prefix) and re.search(
                     r'HTTP Error 403:.*rate limit exceeded',gap,re.I)),None)


class _GitHubData(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active=False;self.buffer=[];self.payloads=[]

    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if (tag=='script' and attrs.get('type')=='application/json' and
                attrs.get('data-target')=='react-app.embeddedData'):
            self.active=True;self.buffer=[]

    def handle_data(self,text):
        if self.active:self.buffer.append(text)

    def handle_endtag(self,tag):
        if tag=='script' and self.active:
            self.active=False
            try:
                value=json.loads(''.join(self.buffer))
                if isinstance(value,dict):self.payloads.append(value.get('payload',{}))
            except (ValueError,TypeError):
                pass


def _repository_identity(body,owner,name):
    page=_GitHubData();page.feed(body.decode('utf-8',errors='replace'))
    for payload in page.payloads:
        if not isinstance(payload,dict):continue
        layout=payload.get('codeViewLayoutRoute',{})
        route=payload.get('codeViewRepoRoute',{})
        if not isinstance(layout,dict) or not isinstance(route,dict):continue
        repo=layout.get('repo',{});ref=route.get('refInfo',{})
        if not isinstance(repo,dict) or not isinstance(ref,dict):continue
        if (str(repo.get('ownerLogin','')).casefold()!=owner.casefold() or
                str(repo.get('name','')).casefold()!=name.casefold()):
            continue
        if repo.get('public') is not True or repo.get('private') is not False:
            raise ValueError('Repository page does not explicitly identify a public repository.')
        commit=ref.get('currentOid','')
        if not isinstance(commit,str) or not re.fullmatch(r'[0-9a-f]{40}',commit):
            raise ValueError('Public repository page has no confirmed commit identifier.')
        if ref.get('refType')!='branch' or ref.get('name')!=repo.get('defaultBranch'):
            raise ValueError('Public repository page is not the default-branch snapshot.')
        about=payload.get('sidebarAbout',{})
        return {
            'commit':commit,'tree_sha':None,'default_branch':repo['defaultBranch'],
            'license':about.get('license') if isinstance(about,dict) else None,
            'public':True,'commit_evidence':'GitHub public page refInfo.currentOid',
            'paper_version_match':'Unverified; this is a pinned current default branch, not necessarily the paper release.',
        }
    raise ValueError('Public GitHub repository identity could not be verified from structured page data.')


class PublicSnapshot:
    def __init__(self,data,owner,name,metadata):
        self.metadata=metadata
        self.owner=owner;self.name=name
        self._zip=zipfile.ZipFile(io.BytesIO(data))
        self._members={}
        self.read_records={}
        self.paths=[]
        self._index()

    def _index(self):
        commit=self.metadata['commit']
        comment=self._zip.comment.decode('ascii',errors='strict').strip()
        if comment and comment!=commit:
            raise ValueError('Archive commit comment disagrees with the selected commit.')
        self.metadata['archive_commit_comment']=comment or None
        root=self.name+'-'+commit
        infos=self._zip.infolist()
        if len(infos)>MAX_ENTRIES:
            raise ValueError('Repository archive exceeds the entry-count limit.')
        if sum(info.file_size for info in infos)>MAX_EXPANDED_BYTES:
            raise ValueError('Repository archive exceeds the uncompressed size limit.')
        seen=set();excluded=[]
        for info in infos:
            raw=info.filename
            parts=raw.rstrip('/').split('/')
            if ('\\' in raw or '\x00' in raw or raw.startswith('/') or
                    any(p in ('','..','.') or ':' in p for p in parts) or
                    not parts or parts[0]!=root):
                raise ValueError('Repository archive contains an unsafe or mismatched path.')
            path='/'.join(parts[1:])
            if not path:
                if not info.is_dir():raise ValueError('Archive root is not a directory.')
                continue
            if path in seen:
                raise ValueError('Repository archive contains duplicate paths.')
            seen.add(path)
            if info.is_dir():continue
            mode=info.external_attr>>16
            reason=None
            if stat.S_ISLNK(mode):
                reason='symbolic link'
            elif stat.S_IFMT(mode) not in (0,stat.S_IFREG):
                reason='nonregular file'
            elif info.flag_bits&1:
                reason='encrypted entry'
            elif info.compress_type not in (zipfile.ZIP_STORED,zipfile.ZIP_DEFLATED):
                reason='unsupported compression'
            elif info.file_size>=MAX_FILE_BYTES:
                reason='file exceeds selected-source byte limit'
            if reason:
                excluded.append({'path':path,'reason':reason});continue
            self._members[path]=info;self.paths.append(path)
        self.metadata.update(paths=self.paths,archive_entries=len(infos),
            archive_uncompressed_bytes=sum(info.file_size for info in infos),
            excluded_files=excluded)

    def read_text(self,path):
        if path not in self._members:
            raise ValueError('Source path is outside the validated archive file list.')
        info=self._members[path]
        with self._zip.open(info,'r') as source:
            data=source.read(MAX_FILE_BYTES+1)
        if len(data)>MAX_FILE_BYTES or len(data)!=info.file_size:
            raise ValueError('Selected archive source exceeds its declared byte limit.')
        if b'\x00' in data:
            raise ValueError('Selected source contains binary data.')
        self.read_records[path]={'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}
        return data.decode('utf-8',errors='replace')

    def source_url(self,path):
        if path not in self._members:
            raise ValueError('Source path is outside the validated archive file list.')
        return (f'https://github.com/{self.owner}/{self.name}/blob/'+
                self.metadata['commit']+'/'+quote(path,safe='/'))


def load_public_snapshot(owner,name,read):
    """Verify the public page, then download the displayed commit once.

    The caller must first establish an explicit API rate-limit error. The read
    callback retains public-URL, redirect, TLS and byte-limit checks. Only
    selected entries are decompressed in memory; no archive is extracted.
    """
    if not all(re.fullmatch(r'[\w.-]+',part) and part not in ('.','..')
               for part in (owner,name)):
        raise ValueError('Invalid GitHub repository name.')
    page_url=f'https://github.com/{owner}/{name}'
    response=read(page_url,'repository_public_page',MAX_PAGE_BYTES)
    if not response:raise ValueError('Public repository page was not readable.')
    body,final,mime=response
    parsed=urlparse(final)
    if (parsed.scheme!='https' or parsed.hostname!='github.com' or
            parsed.path.rstrip('/').casefold()!=f'/{owner}/{name}'.casefold() or
            'html' not in mime or len(body)>MAX_PAGE_BYTES):
        raise ValueError('Repository page response did not match the requested public repository.')
    metadata=_repository_identity(body,owner,name)
    archive_url=f'https://codeload.github.com/{owner}/{name}/zip/'+metadata['commit']
    response=read(archive_url,'repository_public_archive',MAX_ARCHIVE_BYTES)
    if not response:raise ValueError('Public commit archive was not readable.')
    data,final,mime=response
    parsed=urlparse(final)
    if (parsed.scheme!='https' or parsed.hostname!='codeload.github.com' or
            parsed.path!=urlparse(archive_url).path or len(data)>MAX_ARCHIVE_BYTES):
        raise ValueError('Repository archive response did not match the pinned commit.')
    metadata.update(url=page_url,transport='public_codeload_after_api_rate_limit',
        public_page_url=page_url,public_page_sha256=hashlib.sha256(body).hexdigest(),
        archive_url=archive_url,archive_resolved_url=final,
        archive_sha256=hashlib.sha256(data).hexdigest(),archive_bytes=len(data),
        extraction='none; selected files read in memory')
    return PublicSnapshot(data,owner,name,metadata)


def _source_path(path):
    if (not isinstance(path, str) or not path or path.startswith('/') or
            '\\' in path or ':' in path or any(ord(c)<32 for c in path) or
            any(p in ('', '.', '..') for p in path.split('/'))):
        raise ValueError('Invalid repository-relative path.')
    return path


def _text_path(path):
    name = PurePosixPath(path).name.lower()
    return (PurePosixPath(path).suffix.lower() in TEXT_SUFFIXES or
            name.startswith(('readme', 'license', 'licence', 'copying')))


def _directory_page(body, owner, name, directory='', commit=None):
    """Read only GitHub's structured public tree, never HTML scripts as code."""
    page = _GitHubData(); page.feed(body.decode('utf-8', errors='replace'))
    for payload in page.payloads:
        if not isinstance(payload, dict):
            continue
        layout = payload.get('codeViewLayoutRoute', {})
        route = payload.get('codeViewTreeRoute') or payload.get('codeViewRepoRoute', {})
        if not isinstance(layout, dict) or not isinstance(route, dict):
            continue
        repo, ref = layout.get('repo', {}), route.get('refInfo', {})
        if not isinstance(repo, dict) or not isinstance(ref, dict):
            continue
        if (str(repo.get('ownerLogin', '')).casefold()!=owner.casefold() or
                str(repo.get('name', '')).casefold()!=name.casefold()):
            continue
        if repo.get('public') is not True or repo.get('private') is not False:
            raise ValueError('Selective reading requires an explicitly public repository.')
        oid = ref.get('currentOid', '')
        if not isinstance(oid, str) or not re.fullmatch(r'[0-9a-f]{40}', oid):
            raise ValueError('Repository page has no fixed commit.')
        if commit is not None and oid!=commit:
            raise ValueError('Repository directory changed revision.')
        if commit is None and (ref.get('refType')!='branch' or ref.get('name')!=repo.get('defaultBranch')):
            raise ValueError('Repository root is not the public default branch.')
        route_path = route.get('path', '')
        if route_path=='/': route_path=''
        if route_path!=directory:
            raise ValueError('Repository directory response does not match its requested path.')
        tree = route.get('tree', {})
        if not isinstance(tree, dict) or not isinstance(tree.get('items'), list):
            raise ValueError('Repository page has no structured directory listing.')
        items = []
        if len(tree['items'])>MAX_ENTRIES:
            raise ValueError('Repository directory exceeds the entry-count budget.')
        seen = set()
        for item in tree['items']:
            if not isinstance(item, dict):
                raise ValueError('Invalid directory entry.')
            path = _source_path(item.get('path'))
            if path in seen:
                raise ValueError('Repository directory contains duplicate entries.')
            seen.add(path)
            parent = str(PurePosixPath(path).parent)
            if ('' if parent=='.' else parent)!=directory:
                raise ValueError('Directory entry is outside the requested directory.')
            kind = item.get('contentType')
            if kind in ('file', 'directory'):
                items.append({'path': path, 'type': 'blob' if kind=='file' else 'tree', 'size': None, 'sha': None})
        total = tree.get('totalCount')
        about = payload.get('sidebarAbout', {})
        metadata = {'commit': oid, 'tree_sha': None, 'public': True,
                    'default_branch': repo.get('defaultBranch'),
                    'license': about.get('license') if isinstance(about, dict) else None,
                    'commit_evidence': 'GitHub public structured page refInfo.currentOid',
                    'paper_version_match': 'Unverified; pinned public revision is not necessarily the paper release.'}
        return metadata, items, isinstance(total, int) and total>len(tree['items'])
    raise ValueError('Public repository directory identity could not be verified.')


def _public_read(read, url, kind, limit, host):
    response = read(url, kind, limit)
    if not response:
        raise ValueError('Public repository response was unavailable.')
    data, final, mime = response
    requested, resolved = urlparse(url), urlparse(final)
    if (resolved.scheme!='https' or resolved.hostname!=host or
            resolved.path.rstrip('/')!=requested.path.rstrip('/') or len(data)>limit):
        raise ValueError('Repository response did not match its fixed public URL or byte limit.')
    return data, final, mime


def _gh_tree(executable, owner, name, commit):
    """Optional existing login only; never obtain, print or persist tokens."""
    import os
    import subprocess
    env = dict(os.environ, GH_PROMPT_DISABLED='1', GH_PAGER='cat')
    status = subprocess.run([str(executable), 'auth', 'status', '--active', '--hostname', 'github.com'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20, env=env)
    if status.returncode:
        raise ValueError('No active GitHub CLI login; using public directory pages.')
    def api(endpoint):
        result = subprocess.run([str(executable), 'api', '--hostname', 'github.com', endpoint],
                                capture_output=True, timeout=30, env=env)
        if result.returncode or len(result.stdout)>MAX_DIRECTORY_BYTES:
            raise ValueError('Authenticated repository metadata was unavailable or over budget.')
        return json.loads(result.stdout)
    revision = api(f'repos/{owner}/{name}/commits/{commit}')
    if revision.get('sha')!=commit:
        raise ValueError('Authenticated revision does not match the public fixed commit.')
    tree_sha = revision.get('commit', {}).get('tree', {}).get('sha', '')
    if not re.fullmatch(r'[0-9a-f]{40}', tree_sha):
        raise ValueError('Authenticated commit has no verified tree SHA.')
    tree = api(f'repos/{owner}/{name}/git/trees/{tree_sha}?recursive=1')
    if tree.get('sha')!=tree_sha or not isinstance(tree.get('tree'), list):
        raise ValueError('Authenticated tree does not match the fixed commit.')
    entries = []
    for item in tree['tree'][:MAX_ENTRIES]:
        if not isinstance(item, dict):
            raise ValueError('Invalid authenticated tree entry.')
        path = _source_path(item.get('path'))
        if item.get('type')=='blob' and item.get('mode') in ('100644', '100755'):
            entries.append({'path': path, 'type': 'blob', 'size': item.get('size'), 'sha': item.get('sha')})
    return entries, tree_sha, bool(tree.get('truncated') or len(tree['tree'])>MAX_ENTRIES)


class SelectiveSnapshot:
    """Lazy bounded text reads from explicitly discovered, fixed-revision paths."""
    def __init__(self, owner, name, read, metadata, entries):
        self.owner, self.name, self._read = owner, name, read
        self.metadata, self.entries = metadata, entries
        self.paths = list(dict.fromkeys(item['path'] for item in entries if item['type']=='blob'))
        self._entries = {item['path']: item for item in entries}
        self._texts, self.read_records = {}, {}
        self._attempts = 0
        self.metadata.update(paths=self.paths, selected_source_file_limit=MAX_SELECTIVE_FILES,
                             selected_source_byte_limit=MAX_SELECTIVE_BYTES, read_bytes=0)

    def source_url(self, path):
        if path not in self.paths:
            raise ValueError('Source path was not discovered in the fixed repository tree.')
        return f'https://github.com/{self.owner}/{self.name}/blob/{self.metadata["commit"]}/'+quote(path, safe='/')

    def raw_url(self, path):
        self.source_url(path)
        return f'https://raw.githubusercontent.com/{self.owner}/{self.name}/{self.metadata["commit"]}/'+quote(path, safe='/')

    def read_text(self, path):
        self.source_url(path)
        if not _text_path(path):
            raise ValueError('Only discovered text source, README and license files may be read as code context.')
        if path in self._texts:
            return self._texts[path]
        size = self._entries[path].get('size')
        if type(size) is int and size>=MAX_FILE_BYTES:
            raise ValueError('Selected source exceeds the per-file byte limit.')
        if len(self._texts)>=MAX_SELECTIVE_FILES or self._attempts>=MAX_SELECTIVE_FILES+3:
            raise ValueError('Selective repository file budget exhausted.')
        remaining = MAX_SELECTIVE_BYTES-self.metadata['read_bytes']
        if remaining<=0:
            raise ValueError('Selective repository byte budget exhausted.')
        self._attempts += 1
        data, final, mime = _public_read(self._read, self.raw_url(path), 'repository_selective_file',
                                         min(MAX_FILE_BYTES, remaining), 'raw.githubusercontent.com')
        self.metadata['read_bytes'] += len(data)
        if len(data)>=MAX_FILE_BYTES or b'\x00' in data or 'html' in mime.lower():
            raise ValueError('Selected source is oversized, binary or an HTML response.')
        expected = self._entries[path].get('sha')
        blob = hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
        if expected and blob!=expected:
            raise ValueError('Selected file bytes do not match the fixed repository tree.')
        text = data.decode('utf-8')
        self.read_records[path] = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                                   'git_blob_sha': blob, 'commit': self.metadata['commit'], 'url': final}
        self._texts[path] = text
        return text


def load_selective_snapshot(owner, name, read, *, revision=None, gh_executable=None):
    """Pin a public revision, list bounded paths, lazily fetch <=9 small files.

    Existing authenticated gh is optional and only supplies commit/tree metadata.
    Without it, bounded public directory pages provide a partial, explicit tree.
    No repository archive, checkout, executable source or authorization flow.
    """
    if not all(isinstance(part, str) and re.fullmatch(r'[\w.-]+', part) and part not in ('.', '..') for part in (owner, name)):
        raise ValueError('Invalid repository name.')
    if revision is not None and (not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}', revision)):
        raise ValueError('Selective repository revision must be a full commit SHA.')
    base = f'https://github.com/{owner}/{name}'
    initial = base+('/tree/'+revision if revision else '')
    body, final, mime = _public_read(read, initial, 'repository_public_page', MAX_PAGE_BYTES, 'github.com')
    if 'html' not in mime:
        raise ValueError('Public repository root is not HTML.')
    metadata, root_items, truncated = _directory_page(body, owner, name, '', revision)
    metadata.update(url=base, public_page_url=final, public_page_sha256=hashlib.sha256(body).hexdigest(),
                    transport='public_selective_files', extraction='none; selected raw files only',
                    directory_pages_read=1, directory_bytes=len(body), omitted_directories=[], gaps=[])
    if gh_executable:
        try:
            entries, tree_sha, incomplete = _gh_tree(gh_executable, owner, name, metadata['commit'])
            metadata.update(transport='authenticated_tree_public_selective_files', tree_sha=tree_sha,
                            directory_truncated=incomplete, directory_scope='Bounded Git tree metadata; files read separately.')
            return SelectiveSnapshot(owner, name, read, metadata, entries)
        except (ValueError, OSError, RuntimeError) as exc:
            metadata['gaps'].append('Optional authenticated directory unavailable: '+type(exc).__name__)
        except Exception as exc:
            # subprocess timeouts and malformed metadata also preserve public fallback.
            metadata['gaps'].append('Optional authenticated directory unavailable: '+type(exc).__name__)
    entries = [item for item in root_items if item['type']=='blob']
    queue = [item['path'] for item in root_items if item['type']=='tree']
    seen = set()
    def priority(path):
        parts = set(path.lower().split('/'))
        penalty = 3 if parts & {'third_party', 'vendor', 'node_modules', 'tests', 'test', 'media', 'scores', 'data', 'datasets'} else 0 if parts & {'src', 'embodied', 'agents', 'models', 'model', 'configs', 'config', 'run', 'envs', 'motion_imitation'} else 1
        return penalty, len(path.split('/')), path
    while queue and metadata['directory_pages_read']<MAX_DIRECTORY_PAGES:
        queue.sort(key=priority); directory = queue.pop(0)
        if directory in seen:
            continue
        seen.add(directory)
        if len(directory.split('/'))>MAX_DIRECTORY_DEPTH or len(entries)>=MAX_ENTRIES:
            metadata['omitted_directories'].append({'path': directory, 'reason': 'Directory depth or entry budget'})
            continue
        remaining = MAX_DIRECTORY_BYTES-metadata['directory_bytes']
        if remaining<=0:
            queue.insert(0, directory);break
        url = base+'/tree/'+metadata['commit']+'/'+quote(directory, safe='/')
        metadata['directory_pages_read'] += 1
        try:
            data, _, kind = _public_read(read, url, 'repository_public_directory', min(MAX_PAGE_BYTES, remaining), 'github.com')
            metadata['directory_bytes'] += len(data)
            if 'html' not in kind:raise ValueError('Directory page is not HTML.')
            _, items, partial = _directory_page(data, owner, name, directory, metadata['commit'])
            truncated = truncated or partial
            for item in items:
                if item['type']=='tree':queue.append(item['path'])
                elif len(entries)<MAX_ENTRIES:entries.append(item)
                else:truncated=True
        except (ValueError, OSError, RuntimeError) as exc:
            metadata['omitted_directories'].append({'path': directory, 'reason': type(exc).__name__})
    metadata['omitted_directories'].extend({'path': path, 'reason': 'Directory-page or byte budget'} for path in sorted(set(queue)))
    metadata.update(directory_truncated=bool(truncated or metadata['omitted_directories']),
                    directory_scope='Bounded public directory traversal; omitted directories are not inspected.')
    return SelectiveSnapshot(owner, name, read, metadata, entries)
