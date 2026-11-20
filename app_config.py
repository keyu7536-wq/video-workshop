"""Public application defaults; credentials never belong in this file."""
import json
import os
from pathlib import Path

APP_ID = 'video-workshop'
VERSION = '0.4.0'
PROTOCOL_VERSION = 15
ROOT = Path(__file__).resolve().parent

def load_config():
    config = {'name':'视频工坊','version':VERSION,'default_voice':'zh-CN-YunyangNeural',
              'default_rate':'+50%','port':8780,'reference_available':False}
    path = Path(os.environ.get('VIDEO_WORKSHOP_CONFIG', str(ROOT/'app.local.json')))
    if path.is_file():
        values = json.loads(path.read_text(encoding='utf-8'))
        allowed = {'name','default_voice','default_rate','port'}
        if not isinstance(values,dict) or set(values)-allowed:
            raise ValueError('应用配置仅允许 name、default_voice、default_rate、port；凭据请在会话内配置。')
        if 'name' in values and (not isinstance(values['name'],str) or not 1<=len(values['name'].strip())<=40):
            raise ValueError('应用名称须为1–40个字符。')
        if 'port' in values and (type(values['port']) is not int or not 1024<=values['port']<=65535):
            raise ValueError('应用端口须为1024–65535的整数。')
        config.update(values)
    return config
