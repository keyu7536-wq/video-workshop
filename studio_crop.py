"""Preserve deliberate subfigure crops without splitting nearby PDF words."""
def panel_clip(page,clip):
    import pymupdf
    result=pymupdf.Rect(clip)&page.rect
    original=pymupdf.Rect(result)
    allowed=pymupdf.Rect(original.x0-max(2,original.width*.03),original.y0-max(2,original.height*.03),
                         original.x1+max(2,original.width*.03),original.y1+max(2,original.height*.03))
    for word in page.get_text('words'):
        rect=pymupdf.Rect(word[:4])
        if result.intersects(rect) and allowed.contains(rect):result|=rect
    # Raster images and vector containers may span many panels or the whole page.
    # Expanding to those bounds silently cancels the editor/model's intended crop.
    return result&page.rect
