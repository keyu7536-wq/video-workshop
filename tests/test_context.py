import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import paper_video as pv
from studio_context import source_batches,context_sources
from studio_research import dossier


class ContextTests(unittest.TestCase):
    def test_edited_script_is_current_without_rewriting_original_source(self):
        original=[{'id':'script','type':'text','name':'用户文案','text':'旧稿。'},
                  {'id':'paper','type':'text','text':'论文原文不变。'}]
        effective=context_sources(original,current_script='人工改过的新稿。')
        self.assertEqual(effective[0]['text'],'人工改过的新稿。')
        self.assertIn('original_script_sha256',effective[0])
        self.assertEqual(original[0]['text'],'旧稿。')
        self.assertEqual(effective[1],original[1])
        self.assertEqual(context_sources(original),original)

    def test_batches_preserve_all_text_including_large_single_source(self):
        sources=[{'id':'a','text':'A'*130001},{'id':'b','text':'B'*24000}]
        batches=source_batches(sources)
        self.assertTrue(all(sum(len(s['text']) for s in b)<=60000 for b in batches))
        for source in sources:
            self.assertEqual(''.join(s['text'] for b in batches for s in b if s['id']==source['id']),source['text'])

    def test_later_context_prioritizes_claims_and_preserves_originals(self):
        sources=[{'id':'early','text':'e'*100000},{'id':'late','text':'l'*100000}]
        context=context_sources(sources,{'claims':[{'source_id':'late','quote':'llll'}]})
        self.assertEqual(context[1]['text'],sources[1]['text'])
        self.assertIn('context_coverage',context[0])
        self.assertEqual(len(sources[0]['text']),100000)
        self.assertLessEqual(sum(len(s['text']) for s in context),140000)

    def test_all_claims_and_edited_plan_evidence_are_reserved_before_full_pages(self):
        sources=[{'id':'a','text':'first finding '+'a'*140000},
                 {'id':'b','text':'late supporting facts'},
                 {'id':'c','text':'manually chosen evidence'}]
        research={'claims':[{'source_id':'a','quote':'first finding'},
                            {'source_id':'b','quote':'late supporting facts'}]}
        context=context_sources(sources,research,evidence=[{'source_id':'c','quote':'manually chosen evidence'}])
        for source,quote in zip(context,['first finding','late supporting facts','manually chosen evidence']):
            self.assertIn(quote,source['text'])
        self.assertLessEqual(sum(len(s['text']) for s in context),140000)
        self.assertEqual(len(sources[0]['text']),140014)

    def test_whitespace_normalized_quote_preserves_original_surrounding_context(self):
        body='prefix '+'x'*140000+' under these conditions alpha\nbeta applies only to this test'
        context=context_sources([{'id':'a','text':body}],{'claims':[{'source_id':'a','quote':'alpha beta'}]})
        self.assertIn('under these conditions alpha\nbeta applies only to this test',context[0]['text'])
        self.assertNotIn('alpha beta',context[0]['text'])

    def test_oversized_required_quotes_fail_explicitly(self):
        sources=[{'id':'a','text':'a'*2000},{'id':'b','text':'b'*2000}]
        evidence=[{'source_id':s['id'],'quote':s['text']} for s in sources]
        with self.assertRaisesRegex(ValueError,'引用原文'):
            context_sources(sources,limit=1000,evidence=evidence)

    def test_long_dossier_reads_every_batch_then_reuses_validated_cache(self):
        sources=[{'id':f'p{i}','type':'text','name':f'page {i}','text':f'Unique anchor {i}. '+'x'*50000} for i in range(4)]
        calls=[]
        def ai(prompt,images,analysis,request,schema,suffix):
            calls.append((prompt,suffix))
            if '_part_' in suffix:
                batch=json.loads(prompt.split('本批来源：')[-1]);source=batch[0]
            else:source=sources[-1]
            return {'title':'Source notes','project_identity':'fixture','story_angle':'facts','claims':[{'topic':'fact','explanation':'fact','source_id':source['id'],'quote':source['text'][:16]}],'missing_information':[],'visual_strategy':'use source'}
        with tempfile.TemporaryDirectory() as folder:
            job=Path(folder);(job/'analysis').mkdir()
            result=dossier(job,{},sources,[],ai)
            self.assertEqual(result['claims'][0]['source_id'],'p3')
            parts=[json.loads(p.split('本批来源：')[-1]) for p,s in calls if '_part_' in s]
            self.assertEqual({s['id'] for b in parts for s in b},{s['id'] for s in sources})
            coverage=pv.load_json(job/'analysis/reading_coverage.json')
            self.assertEqual(coverage['completed_batches'],4)
            before=len(calls);dossier(job,{},sources,[],ai)
            self.assertEqual(len(calls)-before,1) # only synthesis; all validated reading batches cached

    def test_bad_batch_quote_cannot_be_cached_or_merged(self):
        sources=[{'id':'a','type':'text','name':'source','text':'actual text '*15000}]
        bad={'title':'bad','project_identity':'fixture','story_angle':'fixture','claims':[{'topic':'fact','explanation':'invented','source_id':'a','quote':'invented absent claim'}],'missing_information':[],'visual_strategy':''}
        ai=Mock(return_value=bad)
        with tempfile.TemporaryDirectory() as folder:
            job=Path(folder);(job/'analysis').mkdir()
            with self.assertRaisesRegex(ValueError,'引文'):dossier(job,{},sources,[],ai)
            self.assertEqual(ai.call_count,2)
            self.assertFalse(list((job/'analysis/dossier_batches').glob('*.json')))


if __name__=='__main__':unittest.main()
