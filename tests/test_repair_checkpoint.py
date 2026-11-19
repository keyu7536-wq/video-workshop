import tempfile
from pathlib import Path
import unittest


class RepairCheckpointTests(unittest.TestCase):
    def test_failed_candidate_restores_generated_outputs_and_keeps_evidence(self):
        from studio_repair_checkpoint import capture_checkpoint, restore_checkpoint
        with tempfile.TemporaryDirectory() as temp:
            job=Path(temp)/'job';other=Path(temp)/'other';other.mkdir()
            protected=('project.json','narration.txt','evidence.json','publishing.json',
                       'review.json','analysis/studio_plan.json','output/video.mp4',
                       'output/narration.wav','output/render_receipt.json',
                       'output/final_review.json','assets/visual_00_00.png',
                       'assets/diagram_00_00.mp4','analysis/final_frames/00.jpg')
            for name in protected:
                path=job/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'original')
            (job/'assets/source.mp4').write_bytes(b'downloaded source')
            (other/'video.mp4').write_bytes(b'other job')
            checkpoint=capture_checkpoint(job,job/'analysis/attempt/checkpoint')
            for name in protected:(job/name).write_bytes(b'failed candidate')
            for name in ('assets/visual_00_01.png','output/new_export.srt','analysis/final_frames/01.jpg'):
                (job/name).write_bytes(b'new candidate')
            (job/'output/timing_recovery.json').write_bytes(b'latest diagnostic')
            (job/'analysis/attempt/candidate.json').write_bytes(b'candidate evidence')
            restore_checkpoint(checkpoint)
            for name in protected:self.assertEqual((job/name).read_bytes(),b'original',name)
            for name in ('assets/visual_00_01.png','output/new_export.srt','analysis/final_frames/01.jpg'):
                self.assertFalse((job/name).exists(),name)
            self.assertEqual((job/'assets/source.mp4').read_bytes(),b'downloaded source')
            self.assertEqual((job/'output/timing_recovery.json').read_bytes(),b'latest diagnostic')
            self.assertTrue((job/'analysis/attempt/candidate.json').is_file())
            self.assertEqual((other/'video.mp4').read_bytes(),b'other job')

    def test_checkpoint_outside_job_is_rejected_before_copying(self):
        from studio_repair_checkpoint import capture_checkpoint
        with tempfile.TemporaryDirectory() as temp:
            job=Path(temp)/'job';job.mkdir()
            with self.assertRaises(ValueError):capture_checkpoint(job,Path(temp)/'outside')


if __name__=='__main__':unittest.main()
