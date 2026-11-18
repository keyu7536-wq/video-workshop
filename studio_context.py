"""Read long source collections in bounded batches; retain original evidence."""
import copy
import hashlib
import json
import re
from pathlib import Path
import paper_video as pv


def source_batches(sources,limit=60000):
    if limit<1000:raise ValueError('Source batch limit is too small')
    batches=[];batch=[];size=0
    for source in sources:
        body=source.get('text','')
        if not body:continue
        chunks=[body[i:i+limit] for i in range(0,len(body),limit)]
        for index,chunk in enumerate(chunks):
            if batch and size+len(chunk)>limit:batches.append(batch);batch=[];size=0
            item=dict(source,text=chunk)
            if len(chunks)>1:item['context_part']=index+1
            batch.append(item);size+=len(chunk)
    if batch:batches.append(batch)
    return batches


def _quote_spans(body,quotes,margin):
    """Locate original passages, including quotes whose whitespace was normalized."""
    spans=[];positions=None;compact=None
    for quote in dict.fromkeys(quotes):
        if not quote or not quote.strip():continue
        start=body.find(quote);end=start+len(quote)
        if start<0:
            if positions is None:
                positions=[i for i,char in enumerate(body) if not char.isspace()]
                compact=''.join(body[i] for i in positions)
            normalized=re.sub(r'\s','',quote);match=compact.find(normalized)
            if match<0:continue
            start=positions[match];end=positions[match+len(normalized)-1]+1
        spans.append((max(0,start-margin),min(len(body),end+margin)))
    merged=[]
    for start,end in sorted(spans):
        if merged and start<=merged[-1][1]:merged[-1]=(merged[-1][0],max(end,merged[-1][1]))
        else:merged.append((start,end))
    return '\n[…]\n'.join(body[start:end] for start,end in merged)


def context_sources(sources,research=None,limit=140000,evidence=None,current_script=None):
    """A bounded later-stage context; original sources remain on disk for checks."""
    if current_script is not None:
        sources=[dict(s,text=current_script,name='当前用户文稿',
                      context_role='Current editable script; not independent research evidence.',
                      original_script_sha256=hashlib.sha256(s.get('text','').encode('utf-8')).hexdigest())
                 if s.get('id')=='script' else s for s in sources]
    if sum(len(s.get('text','')) for s in sources)<=limit:return sources
    citations=list((research or {}).get('claims',[]))+list(evidence or [])
    quotes={}
    for citation in citations:
        quotes.setdefault(citation['source_id'],[]).append(citation['quote'])
    claimed=set(quotes)
    order=sorted(range(len(sources)),key=lambda i:(sources[i]['id'] not in claimed,not bool(sources[i].get('preview')),i))
    # Reserve every cited passage before expanding any source to its full text.
    excerpts={i:_quote_spans(s.get('text',''),quotes.get(s['id'],[]),250) for i,s in enumerate(sources)}
    if sum(map(len,excerpts.values()))>limit:
        excerpts={i:_quote_spans(s.get('text',''),quotes.get(s['id'],[]),0) for i,s in enumerate(sources)}
    if sum(map(len,excerpts.values()))>limit:
        raise ValueError('引用原文超过本次审核容量，请减少重复或过长的引文后重试。')
    kept={};remaining=limit-sum(map(len,excerpts.values()))
    for index in order:
        source=sources[index];body=source.get('text','')
        item=dict(source)
        if len(body)-len(excerpts[index])<=remaining:
            kept[index]=item;remaining-=len(body)-len(excerpts[index])
        else:
            item['text']=excerpts[index]
            item['context_coverage']='Later-stage excerpt only; full text remains in analysis/sources.json. Do not infer unseen content.'
            kept[index]=item
    return [kept[i] for i in range(len(sources))]


_NOTE_CHAR_LIMIT = 16000
_DOSSIER_PROMPT_LIMIT = 100000
_MERGE_PAYLOAD_LIMIT = 60000


def _note_json(value):
    return json.dumps(value,ensure_ascii=False)


def _note_schema(schema,max_claims):
    bounded=copy.deepcopy(schema);properties=bounded['properties']
    for key,limit in {'title':200,'project_identity':1600,'story_angle':800,'visual_strategy':1600}.items():
        properties[key]=copy.deepcopy(properties[key])
        properties[key]['maxLength']=min(properties[key].get('maxLength',limit),limit)
    properties['missing_information']=copy.deepcopy(properties['missing_information'])
    properties['missing_information']['maxItems']=12
    properties['missing_information']['items']=copy.deepcopy(properties['missing_information']['items'])
    properties['missing_information']['items']['maxLength']=400
    properties['claims']=copy.deepcopy(properties['claims'])
    properties['claims']['maxItems']=min(properties['claims'].get('maxItems',max_claims),max_claims)
    claim_properties=properties['claims']['items']['properties']
    for key,limit in {'source_id':200,'explanation':800,'quote':1000}.items():
        claim_properties[key]=copy.deepcopy(claim_properties[key])
        claim_properties[key]['maxLength']=min(claim_properties[key].get('maxLength',limit),limit)
    return bounded


def _check_note(value,schema,scope,validate_result):
    from jsonschema import validate,ValidationError
    try:validate(value,schema)
    except ValidationError as exc:raise ValueError('笔记结构或字段长度无效：'+exc.message[:300]) from exc
    if len(_note_json(value))>_NOTE_CHAR_LIMIT:
        raise ValueError(f'笔记超过{_NOTE_CHAR_LIMIT}字符预算；请减少重复主张、缩短解释并重新选择完整短引文，不截断引文。')
    for claim in value['claims']:
        quote=''.join(claim['quote'].split())
        if len(quote)<4 or not any(quote in ''.join(text.split()) for text in scope.get(claim['source_id'],[])):
            raise ValueError('引文不在当前批次提供的原文或合并笔记中：'+claim['source_id'])
    validate_result(value,schema)


def _scope_for_sources(sources):
    scope={}
    for source in sources:scope.setdefault(source['id'],[]).append(source.get('text',''))
    return scope


def _bounded_note_call(ai,prompt,pictures,analysis,request,schema,suffix,scope,validate_result):
    if len(prompt)>_DOSSIER_PROMPT_LIMIT:
        raise ValueError('资料笔记提示超过单次容量；请减少重复创作要求或资料元数据后重试。')
    result=ai(prompt,pictures,analysis,request,schema,suffix)
    try:_check_note(result,schema,scope,validate_result)
    except ValueError as exc:
        repair=prompt+'\n上一版未通过校验，请修正一次：'+str(exc)[:600]
        previous='\n上一版：'+_note_json(result)
        if len(previous)<=_NOTE_CHAR_LIMIT and len(repair)+len(previous)<=_DOSSIER_PROMPT_LIMIT:
            repair+=previous
        else:repair+='\n上一版过长，不重复附上；请仅依据以上原文/笔记重新生成符合长度约束的结果。'
        result=ai(repair,pictures,analysis,request,schema,suffix+'_fix')
        _check_note(result,schema,scope,validate_result)
    return result


def _bounded_source_parts(item,payload_limit):
    if len(_note_json([item]))<=payload_limit:
        return [item]
    empty=dict(item,text='')
    if len(_note_json([empty]))>=payload_limit:
        raise ValueError('单项资料的空正文元数据已占满分批容量，请精简元数据后重试；原文未被截断。')
    # JSON escaping can make a 60,000-character body much larger on the wire.
    # Split the exact body further instead of rejecting or truncating its text.
    body=item['text'];offset=0;parts=[]
    while offset<len(body):
        lower=0;upper=len(body)-offset
        while lower<upper:
            size=(lower+upper+1)//2
            candidate=dict(item,text=body[offset:offset+size])
            if len(_note_json([candidate]))<=payload_limit:lower=size
            else:upper=size-1
        if not lower:
            raise ValueError('单项资料元数据未给正文留出容量，请精简元数据后重试；原文未被截断。')
        parts.append(dict(item,text=body[offset:offset+lower]));offset+=lower
    return parts


def _pack_dossier_sources(sources,payload_limit):
    batches=[];batch=[];text_size=0
    for original in source_batches(sources):
        for original_item in original:
            for item in _bounded_source_parts(original_item,payload_limit):
                if batch and (text_size+len(item['text'])>60000 or len(_note_json(batch+[item]))>payload_limit):
                    batches.append(batch);batch=[];text_size=0
                batch.append(item);text_size+=len(item['text'])
    if batch:batches.append(batch)
    return batches


def _merge_note_groups(results,payload_limit):
    groups=[];group=[]
    for result in results:
        if group and len(_note_json(group+[result]))>payload_limit:
            groups.append(group);group=[]
        group.append(result)
    if group:groups.append(group)
    return groups


def dossier_batches(job,request,sources,images,ai,schema,prompt,validate_result):
    payload_limit=min(_MERGE_PAYLOAD_LIMIT,_DOSSIER_PROMPT_LIMIT-len(prompt)-3000)
    if payload_limit<2*_NOTE_CHAR_LIMIT+10:
        raise ValueError('资料笔记的公共提示过长，无法留出分批与合并容量。')
    batches=_pack_dossier_sources(sources,_DOSSIER_PROMPT_LIMIT-len(prompt)-3000)
    folder=job/'analysis/dossier_batches';folder.mkdir(exist_ok=True)
    coverage={'total_sources':len(sources),'total_text_characters':sum(len(s.get('text','')) for s in sources),
        'batches':len(batches),'completed_batches':0,'visual_inputs':len(images),'visual_inputs_sent':0,'visual_inputs_reused':0,
        'scope':'All extracted text is scheduled across bounded batches; completed_batches records progress. Images are sampled, not an all-page visual review.'}
    pv.save_json(job/'analysis/reading_coverage.json',coverage)
    results=[];batch_schema=_note_schema(schema,12);sent_pictures=set();reused_pictures=set()
    for index,batch in enumerate(batches,1):
        ids={s['id'] for s in batch};scope=_scope_for_sources(batch)
        pictures=[Path(path) for label,path in images if any(label==sid or label.startswith(sid+' ') for sid in ids)]
        batch_prompt=prompt+f'\n这是全文分批阅读的第{index}/{len(batches)}批，仅概括本批，不把部分阅读说成全文结论。最多12条有精确引文的主张，总笔记不超过{_NOTE_CHAR_LIMIT}字符；简要解释，选择完整短引文，不截断原文证据。兼顾背景、贡献、证据与边界，相关才填写，不机械凑齐。\n本批来源：'+_note_json(batch)
        image_hashes=[hashlib.sha256(path.read_bytes()).hexdigest() for path in pictures]
        fingerprint=hashlib.sha256((batch_prompt+json.dumps(batch_schema,sort_keys=True)+json.dumps(image_hashes)+'batch-local-v2').encode('utf-8')).hexdigest()
        cache=folder/f'{index:03}.json';result=None
        try:
            entry=pv.load_json(cache) if cache.is_file() else {}
            if entry.get('fingerprint')==fingerprint:
                _check_note(entry['result'],batch_schema,scope,validate_result);result=entry['result']
        except (ValueError,TypeError,KeyError,AttributeError,OSError):
            result=None
        if result is None:
            pv.log(f'  分批深读资料 {index}/{len(batches)}（{sum(len(s.get("text","")) for s in batch)} 字符）')
            result=_bounded_note_call(ai,batch_prompt,pictures,job/'analysis',request,batch_schema,f'_dossier_part_{index:03}',scope,validate_result)
            pv.save_json(cache,{'fingerprint':fingerprint,'result':result,'source_ids':sorted(ids)})
            sent_pictures.update(map(str,pictures))
        else:reused_pictures.update(map(str,pictures))
        coverage['visual_inputs_sent']=len(sent_pictures);coverage['visual_inputs_reused']=len(reused_pictures)
        results.append(result);coverage['completed_batches']=index;pv.save_json(job/'analysis/reading_coverage.json',coverage)

    merge_schema=_note_schema(schema,30)
    def merge_group(group,suffix):
        scope={}
        for note in group:
            for claim in note['claims']:scope.setdefault(claim['source_id'],[]).append(claim['quote'])
        merge=prompt+f'\n以下是逐条引文已核验的分批笔记。仅合并当前提供的笔记，保留主题重点、矛盾和缺口，不把小节提升为整篇结论。最多30条关键主张，总笔记不超过{_NOTE_CHAR_LIMIT}字符。只使用提供的source_id和完整真实短引文，不截断引文，不编造未提供的内容。完整原文和各批原始笔记保存在任务中。\n分批笔记：'+_note_json(group)
        return _bounded_note_call(ai,merge,[],job/'analysis',request,merge_schema,suffix,scope,validate_result)
    # Hierarchical synthesis keeps each input bounded; original batch notes remain intact.
    level=0
    while len(_note_json(results))>payload_limit:
        level+=1;groups=_merge_note_groups(results,payload_limit)
        if len(groups)>=len(results):raise ValueError('笔记无法在合并预算内收敛；请缩短解释后重试，原始引文已保留。')
        next_results=[]
        for number,group in enumerate(groups,1):
            if len(group)==1:next_results.append(group[0]);continue
            merged=merge_group(group,f'_dossier_merge_{level:02}_{number:03}')
            pv.save_json(folder/f'merge_{level:02}_{number:03}.json',{'result':merged,'input_notes':len(group)})
            next_results.append(merged)
        results=next_results
    result=merge_group(results,'_dossier_merge')
    coverage.update(merged=True,merge_levels=level+1);pv.save_json(job/'analysis/reading_coverage.json',coverage)
    return result
