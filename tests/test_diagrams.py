import copy,json,tempfile,unittest
from pathlib import Path
from studio_diagrams import validate_graph,render_frame,render_diagram

def graph():
    return {'title':'Action and feedback','note':'Conceptual flow',
      'nodes':[{'id':'input','label':'Input','x':.16,'y':.40},
               {'id':'model','label':'Model','x':.50,'y':.40},
               {'id':'output','label':'Output','x':.84,'y':.40}],
      'edges':[{'id':'forward','source':'input','target':'model','label':''},
               {'id':'predict','source':'model','target':'output','label':''},
               {'id':'feedback','source':'output','target':'input','label':'Feedback','via':[[.84,.65],[.16,.65]]}],
      'stages':[{'start':0,'active_nodes':['input'],'active_edges':[],'visible_nodes':['input']},
                {'start':.4,'active_nodes':['model'],'active_edges':['forward'],'visible_nodes':['input','model']},
                {'start':.8,'active_nodes':['output'],'active_edges':['predict','feedback'],'visible_nodes':['input','model','output']}]}

class DiagramTests(unittest.TestCase):
    def test_waypoint_safe_area_is_inclusive_and_failure_identifies_the_point(self):
        spec=graph();spec['edges'][2]['via']=[[.025,.18],[.975,.78]]
        before=copy.deepcopy(spec)
        self.assertEqual(validate_graph(spec,1.5)['edges'][2]['via'],spec['edges'][2]['via'])
        self.assertEqual(spec,before)
        for point in ([.0249,.5],[.9751,.5],[.5,.1799],[.5,.7801],[.5,.79]):
            with self.subTest(point=point):
                spec=graph();spec['edges'][2]['via'][1]=point
                with self.assertRaises(ValueError) as caught:validate_graph(spec,1.5)
                message=str(caught.exception)
                for detail in ('feedback','via[1]',repr(point),'0.025','0.975','0.18','0.78'):
                    self.assertIn(detail,message)

    def test_graph_validation_preserves_input_and_rejects_invalid_references(self):
        spec=graph();before=copy.deepcopy(spec)
        validate_graph(spec,1.5);self.assertEqual(spec,before)
        for mutator in (lambda g:g['edges'][0].update(target='absent'),
                        lambda g:g['stages'][1].update(start=0),
                        lambda g:g['stages'][0].update(active_nodes=['model']),
                        lambda g:g['nodes'][0].update(y=.95)):
            changed=graph();mutator(changed)
            with self.assertRaises(ValueError):validate_graph(changed,1.5)

    def test_planning_cues_must_be_resolved_before_render(self):
        spec=graph()
        for i,stage in enumerate(spec['stages']):stage.pop('start');stage['cue']='' if not i else f'cue {i}'
        validated=validate_graph(spec,4)
        with self.assertRaisesRegex(ValueError,'measured'):render_frame(spec,1,4)
        with self.assertRaisesRegex(ValueError,'measured'):render_frame(validated,1,4)

    def test_edge_labels_remain_visible_outside_tightly_spaced_nodes(self):
        spec={'title':'','note':'','nodes':[
            {'id':str(i),'label':'节点','x':x,'y':.4}
            for i,x in enumerate((.14,.38,.62,.86))],
            'edges':[{'id':str(i),'source':str(i),'target':str(i+1),'label':'传入动作'} for i in range(3)],
            'stages':[{'start':0,'active_nodes':[],'active_edges':['0','1','2']}]}
        rendered=render_frame(spec,0,1,1920,1080)
        unlabeled=copy.deepcopy(spec)
        for edge in unlabeled['edges']:edge['label']=''
        baseline=render_frame(unlabeled,0,1,1920,1080)
        from PIL import ImageChops
        # Each relationship name needs ink outside the nodes' .32-.48 height band.
        difference=ImageChops.difference(rendered,baseline)
        for x in (.26,.50,.74):
            self.assertIsNotNone(difference.crop((int(x*1920)-100,195,int(x*1920)+100,345)).getbbox())

    def test_implicit_return_edge_has_a_separate_lane(self):
        from studio_diagrams import _Frames
        spec=graph();spec['edges'][2].pop('via')
        frames=_Frames(spec,1.5,960,540,None)
        self.assertGreater(max(y for x,y in frames.paths['feedback']),.48*540)
        # Explicit author routing continues to take priority.
        spec['edges'][2]['via']=[[.84,.64],[.16,.64]]
        routed=_Frames(spec,1.5,960,540,None)
        self.assertAlmostEqual(routed.paths['feedback'][1][1],.64*540)

    def test_right_to_left_chain_stays_serial_without_shared_return_lane(self):
        from studio_diagrams import _Frames
        spec=graph()
        spec['edges']=[{'id':'one','source':'output','target':'model','label':''},
                       {'id':'two','source':'model','target':'input','label':''}]
        spec['stages']=[{'start':0,'active_nodes':[], 'active_edges':['one','two']}]
        frames=_Frames(spec,1,960,540,None)
        for path in frames.paths.values():
            self.assertEqual(len(path),2)
            self.assertTrue(all(y==.4*540 for x,y in path))
            self.assertGreater(path[0][0],path[-1][0])
        # A deliberately routed leftward edge remains author-controlled.
        spec['edges'][0]['via']=[[.84,.64],[.5,.64]]
        routed=_Frames(spec,1,960,540,None)
        self.assertAlmostEqual(routed.paths['one'][1][1],.64*540)
        self.assertEqual(len(routed.paths['two']),2)

    def test_direct_reverse_pair_still_uses_a_separate_return_lane(self):
        from studio_diagrams import _Frames
        spec=graph();spec['nodes']=spec['nodes'][:2]
        spec['edges']=[{'id':'forward','source':'input','target':'model','label':''},
                       {'id':'return','source':'model','target':'input','label':''}]
        spec['stages']=[{'start':0,'active_nodes':[],'active_edges':['forward','return']}]
        frames=_Frames(spec,1,960,540,None)
        self.assertEqual(len(frames.paths['forward']),2)
        self.assertGreater(max(y for x,y in frames.paths['return']),.48*540)

    def test_progressive_visibility_motion_and_caption_safe_area(self):
        spec=graph()
        first=render_frame(spec,.2,1.5,960,540)
        second=render_frame(spec,.65,1.5,960,540)
        later=render_frame(spec,.75,1.5,960,540)
        self.assertNotEqual(first.tobytes(),second.tobytes())
        self.assertNotEqual(second.tobytes(),later.tobytes())
        # Node model is not drawn until its stage. Dot motion does not touch captions.
        self.assertEqual(first.getpixel((480,216)),(8,14,21))
        self.assertNotEqual(second.getpixel((480,216)),(8,14,21))
        for image in (first,second,later):
            self.assertEqual(image.crop((0,432,960,540)).getextrema(),((8,8),(14,14),(21,21)))

    def test_actual_encoded_video_decodes_to_requested_timing(self):
        import imageio_ffmpeg
        from make_video import run,ffmpeg_path
        spec=graph();spec['title']='Flow';spec['note']=''
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder)/'flow.mp4'
            info=render_diagram(spec,output,1.2,640,360)
            reader=imageio_ffmpeg.read_frames(str(output))
            try:
                metadata=next(reader);encoded=list(reader)
            finally:reader.close()
            self.assertEqual(metadata['size'],(640,360));self.assertEqual(len(encoded),36)
            self.assertAlmostEqual(info['duration_seconds'],1.2)
            self.assertNotEqual(encoded[5],encoded[20])
            run([ffmpeg_path(),'-v','error','-i',str(output),'-f','null','-'])

if __name__=='__main__':unittest.main()
