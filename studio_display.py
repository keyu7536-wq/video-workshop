"""Shared generation settings; legacy projects keep their original layout."""
PRESETS = {'portrait': (1080,1920), 'landscape': (1920,1080), 'square': (1080,1080), 'reference': (720,1280)}
DISPLAY_KEYS = ('format','width','height','layout_style','media_fit','show_sources','show_disclosure','minimal_text')

def dimensions(data):
    kind=data.get('format','portrait')
    if kind in PRESETS:return PRESETS[kind]
    if kind!='custom':raise ValueError('不支持此画幅')
    width,height=data.get('width'),data.get('height')
    validate_dimensions(width,height)
    return width,height

def validate_dimensions(width,height):
    if any(type(n) is not int or not 320<=n<=3840 or n%2 for n in (width,height)):
        raise ValueError('宽高须为 320～3840 之间的偶数像素')
    if width*height>8294400:raise ValueError('总像素不能超过 8294400（4K）')
    if max(width/height,height/width)>3:raise ValueError('画幅长宽比例不能超过 3:1')

def validate_display(data):
    dimensions(data)
    if data.get('layout_style','editorial') not in ('editorial','immersive'):raise ValueError('不支持此布局')
    if data.get('media_fit','contain') not in ('contain','cover'):raise ValueError('不支持此素材显示方式')
    for key in ('show_sources','show_disclosure','minimal_text'):
        if key in data and type(data[key]) is not bool:raise ValueError('画面标注开关必须是布尔值')
