import unittest
from pathlib import Path
from PIL import Image,ImageDraw,ImageFont
from studio_render import card_lines

class CardLayoutTests(unittest.TestCase):
    def test_preserves_decimal_quantity_and_url(self):
        path=Path('C:/Windows/Fonts/msyh.ttc')
        if not path.exists():self.skipTest('Chinese font unavailable')
        draw=ImageDraw.Draw(Image.new('RGB',(1400,900)))
        font=ImageFont.truetype(str(path),47)
        text='SuperMimic：1张H100，3小时，264.56小时动作\nUC Berkeley\nsupermimic.github.io'
        lines=card_lines(text,draw,font,700)
        self.assertTrue(any('264.56小时' in line for line in lines))
        self.assertTrue(any('supermimic.github.io' in line for line in lines))
        self.assertEqual(''.join(lines).replace(' ',''),text.replace('\n','').replace(' ',''))
        self.assertTrue(all(draw.textlength(line,font=font)<=700 for line in lines))

if __name__=='__main__':unittest.main()
