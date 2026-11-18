import json
import tempfile
import unittest
from pathlib import Path

from jsonschema import validate,ValidationError
import paper_video as pv
from studio_context import dossier_batches,_note_schema
from studio_research import DOSSIER


class DossierBatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.job=Path(self.temporary.name);(self.job/'analysis').mkdir()
        self.sources=[{'id':f's{i}','type':'text','name':f'source {i}',
                       'text':f'Unique source anchor {i}. '+chr(65+i)*50000} for i in range(7)]
        self.calls=[]

    def tearDown(self):
        self.temporary.cleanup()

    def note(self,claims):
        return {'title':'notes','project_identity':'fixture','story_angle':'explanation',
                'claims':claims,'missing_information':[],'visual_strategy':'use available evidence'}

    def claim(self,source,quote=None,explanation='A supported statement.'):
        return {'topic':'fact','explanation':explanation,'source_id':source['id'],
                'quote':quote or source['text'][:40]}

    def checked(self,value,schema):
        validate(value,schema)
        lookup={s['id']:s['text'] for s in self.sources}
        for claim in value['claims']:
            if ''.join(claim['quote'].split()) not in ''.join(lookup.get(claim['source_id'],'').split()):
                raise ValueError('global quote missing')

    def read_payload(self,prompt,marker):
        return json.JSONDecoder().raw_decode(prompt.split(marker,1)[1])[0]

    def normal_ai(self,prompt,images,analysis,request,schema,suffix):
        self.calls.append((suffix,len(prompt)))
        if '_part_' in suffix:
            source=self.read_payload(prompt,'本批来源：')[0]
            return self.note([self.claim(source)])
        notes=self.read_payload(prompt,'分批笔记：')
        return self.note([notes[0]['claims'][0]])

    def run_batches(self,ai=None,sources=None,images=None):
        return dossier_batches(self.job,{},self.sources if sources is None else sources,
            images or [],ai or self.normal_ai,DOSSIER,'Create accurate source notes.',self.checked)

    def test_quote_valid_globally_but_outside_batch_is_rejected_and_not_cached(self):
        calls=[]
        def bad_ai(prompt,images,analysis,request,schema,suffix):
            calls.append(suffix)
            return self.note([self.claim(self.sources[-1])])
        with self.assertRaisesRegex(ValueError,'当前批次'):
            self.run_batches(bad_ai)
        self.assertEqual(len(calls),2)
        self.assertTrue(calls[-1].endswith('_fix'))
        self.assertFalse((self.job/'analysis/dossier_batches/001.json').exists())

    def test_quote_from_other_chunk_of_same_source_is_rejected(self):
        self.sources=[{'id':'single','type':'text','name':'long','text':'A'*60000+'LATE UNIQUE EVIDENCE '+ 'B'*1000}]
        def bad_ai(*args):
            return self.note([self.claim(self.sources[0],quote='LATE UNIQUE EVIDENCE')])
        with self.assertRaisesRegex(ValueError,'当前批次'):
            self.run_batches(bad_ai)

    def test_corrupt_and_matching_fingerprint_invalid_cache_are_reread(self):
        self.sources=self.sources[:2]
        self.run_batches()
        folder=self.job/'analysis/dossier_batches'
        (folder/'001.json').write_text('{broken',encoding='utf-8')
        entry=pv.load_json(folder/'002.json')
        entry['result']['claims'][0]['quote']='nonexistent source quote'
        pv.save_json(folder/'002.json',entry)
        self.calls=[];self.run_batches()
        self.assertEqual(len([s for s,_ in self.calls if '_part_' in s]),2)
        self.assertIn('Unique source anchor 0',pv.load_json(folder/'001.json')['result']['claims'][0]['quote'])
        self.assertIn('Unique source anchor 1',pv.load_json(folder/'002.json')['result']['claims'][0]['quote'])

    def test_large_valid_notes_use_bounded_hierarchical_merge(self):
        def long_notes_ai(prompt,images,analysis,request,schema,suffix):
            self.calls.append((suffix,len(prompt)))
            if '_part_' in suffix:
                source=self.read_payload(prompt,'本批来源：')[0]
                return self.note([self.claim(source,quote=source['text'][:200],explanation='x'*500) for _ in range(12)])
            notes=self.read_payload(prompt,'分批笔记：')
            return self.note([notes[0]['claims'][0]])
        result=self.run_batches(long_notes_ai)
        self.assertTrue(any(s.startswith('_dossier_merge_01_') for s,_ in self.calls))
        self.assertLessEqual(max(size for _,size in self.calls),100000)
        self.assertEqual(len(result['claims'][0]['quote']),200)
        for path in (self.job/'analysis/dossier_batches').glob('[0-9][0-9][0-9].json'):
            original=pv.load_json(path)['result']
            self.assertEqual(len(original['claims']),12)
            self.assertEqual(len(original['claims'][0]['quote']),200)
        coverage=pv.load_json(self.job/'analysis/reading_coverage.json')
        self.assertEqual(coverage['completed_batches'],7)
        self.assertGreater(coverage['merge_levels'],1)

    def test_oversized_note_is_repaired_without_cutting_its_quote(self):
        self.sources=self.sources[:1]
        def ai(prompt,images,analysis,request,schema,suffix):
            if suffix=='_dossier_part_001':
                self.calls.append((suffix,len(prompt)))
                return self.note([self.claim(self.sources[0],quote=self.sources[0]['text'][:1500])])
            return self.normal_ai(prompt,images,analysis,request,schema,suffix)
        self.run_batches(ai)
        self.assertTrue(any(s=='_dossier_part_001_fix' for s,_ in self.calls))
        cached=pv.load_json(self.job/'analysis/dossier_batches/001.json')['result']
        self.assertEqual(cached['claims'][0]['quote'],self.sources[0]['text'][:40])
        self.assertTrue(all(size<=100000 for _,size in self.calls))

    def test_merge_has_one_repair_for_quote_outside_supplied_notes(self):
        self.sources=self.sources[:1]
        def ai(prompt,images,analysis,request,schema,suffix):
            if suffix=='_dossier_merge':
                self.calls.append((suffix,len(prompt)))
                return self.note([self.claim(self.sources[0],quote='A'*80)])
            return self.normal_ai(prompt,images,analysis,request,schema,suffix)
        result=self.run_batches(ai)
        self.assertEqual([s for s,_ in self.calls if s.startswith('_dossier_merge')],['_dossier_merge','_dossier_merge_fix'])
        self.assertEqual(result['claims'][0]['quote'],self.sources[0]['text'][:40])

    def test_image_change_invalidates_cache_and_cached_coverage_is_explicit(self):
        self.sources=self.sources[:1]
        picture=self.job/'sample.png';picture.write_bytes(b'first fixture image')
        self.run_batches(images=[('s0',picture)])
        self.calls=[];self.run_batches(images=[('s0',picture)])
        self.assertEqual([s for s,_ in self.calls],['_dossier_merge'])
        coverage=pv.load_json(self.job/'analysis/reading_coverage.json')
        self.assertEqual(coverage['visual_inputs_sent'],0)
        self.assertEqual(coverage['visual_inputs_reused'],1)
        picture.write_bytes(b'changed fixture image')
        self.calls=[];self.run_batches(images=[('s0',picture)])
        self.assertIn('_dossier_part_001',[s for s,_ in self.calls])

    def test_note_schema_field_limits_are_independent_of_shared_string_schema(self):
        original=json.dumps(DOSSIER,sort_keys=True)
        schema=_note_schema(DOSSIER,12);properties=schema['properties']
        self.assertEqual({key:properties[key]['maxLength'] for key in
            ['title','project_identity','story_angle','visual_strategy']},
            {'title':200,'project_identity':1600,'story_angle':800,'visual_strategy':1600})
        claim_properties=properties['claims']['items']['properties']
        self.assertEqual({key:claim_properties[key]['maxLength'] for key in
            ['source_id','explanation','quote']},{'source_id':200,'explanation':800,'quote':1000})
        self.assertEqual(properties['missing_information']['items']['maxLength'],400)
        note=self.note([self.claim(self.sources[0],quote='A'*1000,explanation='x'*800)])
        note['project_identity']='x'*1600
        validate(note,schema)
        note['title']='x'*201
        with self.assertRaises(ValidationError):validate(note,schema)
        self.assertEqual(json.dumps(DOSSIER,sort_keys=True),original)

    def test_json_escaped_body_is_split_without_losing_original_characters(self):
        for body in ['\n'*60000,'"'*60000,'\x00'*60000]:
            with self.subTest(character=repr(body[0])):
                self.sources=[dict(self.sources[0],text=body)]
                supplied=[];self.calls=[]
                def ai(prompt,images,analysis,request,schema,suffix):
                    self.calls.append((suffix,len(prompt)))
                    if '_part_' in suffix:
                        supplied.extend(self.read_payload(prompt,'本批来源：'))
                    return self.note([])
                self.run_batches(ai)
                self.assertGreater(len(supplied),1)
                self.assertEqual(''.join(item['text'] for item in supplied),body)
                self.assertTrue(all(size<=100000 for _,size in self.calls))


if __name__=='__main__':unittest.main()
