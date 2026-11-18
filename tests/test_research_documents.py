"""Source discovery must read selected PDFs and keep public/private queries distinct."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import paper_video as pv
import studio_research as research


def prepare(folder):
    job=Path(folder);(job/'analysis').mkdir();(job/'assets').mkdir()
    return job


def pdf_bytes(pages=2,large=False):
    import pymupdf
    with pymupdf.open() as document:
        for index in range(pages):
            page=document.new_page()
            page.insert_textbox(pymupdf.Rect(30,40,550,750),
                f'PAGE {index+1}\n'+('Actual primary-source method and evaluation text.\n'*9),fontsize=11)
        if large:document.set_metadata({'keywords':'x'*(3*1024**2+1024)})
        return document.tobytes()


class NoHTMLDecode(bytes):
    def decode(self,*args,**kwargs):
        raise AssertionError('PDF data must not enter the HTML text decoder')


class ResearchDocumentTests(unittest.TestCase):
    def test_existing_paper_pdf_is_not_mistaken_for_discovered_author_project(self):
        pdf='https://arxiv.org/pdf/1234.5678v2'
        project_url='https://author.example/project/'
        identity={'title':'A Public Paper','authors':['An Author'],'project_urls':[pdf],'search_query':'A Public Paper'}
        ai=Mock(side_effect=[identity,{**identity,'project_urls':[project_url]}])
        with tempfile.TemporaryDirectory() as folder:
            job=prepare(folder)
            sources=[{'id':'p1','type':'page','url':pdf,'name':pdf,'file':'unused.pdf','text':'A Public Paper'}]
            with patch.object(research,'urls_in_sources',return_value=[pdf]), \
                 patch.object(research,'search_public',return_value=[pdf,project_url]) as search, \
                 patch.object(research.Research,'project') as project, \
                 patch.object(research.Research,'download_media'):
                research.enrich(job,{},sources,[],ai)
            search.assert_called_once_with('A Public Paper')
            project.assert_called_once_with(project_url)

    def test_public_topic_search_runs_before_exact_title_is_known(self):
        identity={'title':'','authors':[],'project_urls':[],'search_query':'NVIDIA GR00T 2 white paper'}
        selection={**identity,'project_urls':['https://author.example/paper.pdf']}
        ai=Mock(side_effect=[identity,selection])
        with tempfile.TemporaryDirectory() as folder:
            job=prepare(folder)
            sources=[{'id':'article','type':'text','name':'用户主题','text':'英伟达最近那篇groot2白皮书'}]
            with patch.object(research,'search_public',return_value=selection['project_urls']) as search, \
                 patch.object(research.Research,'project') as project,patch.object(research.Research,'download_media'):
                research.enrich(job,{},sources,[],ai)
            search.assert_called_once_with(identity['search_query'])
            project.assert_called_once_with(selection['project_urls'][0])
            self.assertIn('不包含用户私有文案',ai.call_args_list[0].args[0])

    def test_blank_private_query_never_falls_back_to_user_text(self):
        identity={'title':'','authors':[],'project_urls':[],'search_query':'  '}
        private='内部草稿，客户代号以及还没有公开的实验。'
        with tempfile.TemporaryDirectory() as folder:
            job=prepare(folder)
            with patch.object(research,'search_public') as search, \
                 patch.object(research.Research,'download_media'):
                research.enrich(job,{'script':private},[{'id':'script','type':'text','text':private}],[],lambda *args:identity)
            search.assert_not_called()

    def test_selected_pdf_larger_than_html_cap_is_read_as_pages_and_images(self):
        url='https://author.example/download?id=whitepaper'
        data=NoHTMLDecode(pdf_bytes(large=True))
        self.assertGreater(len(data),3*1024**2)
        limits=[]
        def fetch(actual,limit):
            self.assertEqual(actual,url);limits.append(limit)
            if len(data)>limit:raise ValueError('document exceeded download limit')
            return data,url,'application/octet-stream'
        with tempfile.TemporaryDirectory() as folder:
            job=prepare(folder);reader=research.Research(job,{},None)
            with patch.object(research,'fetch',side_effect=fetch):reader.project(url)
            self.assertEqual(limits,[100*1024**2])
            self.assertEqual([source['page'] for source in reader.sources],[1,2])
            self.assertTrue(all(source['type']=='page' and source['url']==url for source in reader.sources))
            self.assertIn('Actual primary-source',reader.sources[0]['text'])
            self.assertEqual(len(reader.document_images),2)
            self.assertTrue(all(Path(path).is_file() for _,path in reader.document_images))
            self.assertTrue(Path(reader.sources[0]['file']).is_file())
            self.assertFalse(reader.gaps)

    def test_enrichment_appends_selected_pdf_previews_and_deduplicates_processed_url(self):
        url='https://author.example/paper.pdf'
        identity={'title':'A public paper','authors':[],'project_urls':[url],'search_query':''}
        with tempfile.TemporaryDirectory() as folder:
            job=prepare(folder);sources=[];images=[]
            with patch.object(research,'fetch',return_value=(pdf_bytes(),url,'application/pdf')) as fetch, \
                 patch.object(research.Research,'download_media'):
                research.enrich(job,{'links':[url]},sources,images,lambda *args:identity)
                self.assertEqual(len(sources),2);self.assertEqual(len(images),2)
                research.enrich(job,{'links':[url+'#page=2']},sources,images,
                    lambda *args:{**identity,'project_urls':[url+'#page=2']})
            self.assertEqual(fetch.call_count,1)
            self.assertEqual(len(sources),2);self.assertEqual(len(images),2)

    def test_long_author_document_keeps_unpreviewed_pages_as_text_evidence(self):
        url='https://author.example/long-paper.pdf'
        with tempfile.TemporaryDirectory() as folder:
            job=prepare(folder);reader=research.Research(job,{},None)
            with patch.object(research,'fetch',return_value=(pdf_bytes(45),url,'application/pdf')):
                reader.project(url)
            self.assertEqual(len(reader.sources),45)
            self.assertEqual(len(reader.document_images),24)
            self.assertEqual(reader.sources[-1]['page'],45)
            self.assertIn('PAGE 45',reader.sources[-1]['text'])
            self.assertTrue(any('45' in gap and '24' in gap for gap in reader.gaps))
            self.assertEqual(sum('preview' in source for source in reader.sources),24)

    def test_existing_image_budget_limits_previews_without_dropping_page_text(self):
        url='https://author.example/remaining.pdf'
        for existing,expected in [(64,1),(65,0)]:
            with self.subTest(existing=existing),tempfile.TemporaryDirectory() as folder:
                job=prepare(folder);reader=research.Research(job,{},None)
                reader.existing_documents([],[(str(i),job/'existing.png') for i in range(existing)])
                with patch.object(research,'fetch',return_value=(pdf_bytes(3),url,'application/pdf')):
                    reader.project(url)
                self.assertEqual(len(reader.sources),3)
                self.assertEqual(len(reader.document_images),expected)
                self.assertTrue(reader.gaps)

    def test_total_document_page_budget_exclusion_is_reported(self):
        url='https://author.example/over-budget.pdf'
        with tempfile.TemporaryDirectory() as folder:
            job=prepare(folder);reader=research.Research(job,{},None)
            reader.existing_documents([{'type':'page','text':''} for _ in range(399)],[])
            with patch.object(research,'fetch',return_value=(pdf_bytes(2),url,'application/pdf')):
                reader.project(url)
            self.assertEqual(reader.sources,[])
            self.assertEqual(reader.document_images,[])
            self.assertTrue(any('PDF读取未完成' in gap for gap in reader.gaps))

    def test_html_citation_links_are_not_automatically_crawled_as_documents(self):
        url='https://author.example/project'
        html=b'<p>Actual author project details with sufficient source text.</p><a href="cited-paper.pdf">Reference</a>'
        with tempfile.TemporaryDirectory() as folder:
            reader=research.Research(prepare(folder),{},None)
            with patch.object(research,'fetch',return_value=(html,url,'text/html')) as fetch:
                reader.project(url)
            self.assertEqual(fetch.call_count,1)
            self.assertEqual([source['type'] for source in reader.sources],['text'])
            self.assertEqual(reader.document_images,[])

    def test_partial_scan_or_empty_pages_are_not_silently_claimed_as_read(self):
        import pymupdf
        url='https://author.example/mixed.pdf'
        with pymupdf.open(stream=pdf_bytes(1),filetype='pdf') as document:
            document.new_page();data=document.tobytes()
        with tempfile.TemporaryDirectory() as folder:
            reader=research.Research(prepare(folder),{},None)
            with patch.object(research,'fetch',return_value=(data,url,'application/pdf')):
                reader.project(url)
            self.assertEqual(len(reader.sources),2)
            self.assertEqual(reader.sources[1]['text'],'')
            self.assertTrue(any('无可提取文字' in gap and '页码：2' in gap for gap in reader.gaps))


if __name__=='__main__':unittest.main()
