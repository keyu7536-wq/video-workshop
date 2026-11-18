"""Read selected files from one public GitHub archive; never execute/extract it."""
from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import io
import json
import re
import stat
from urllib.parse import quote, urlparse
import zipfile


MAX_PAGE_BYTES = 3*1024**2
MAX_ARCHIVE_BYTES = 20*1024**2
MAX_EXPANDED_BYTES = 80*1024**2
MAX_ENTRIES = 10000
MAX_FILE_BYTES = 150000


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
