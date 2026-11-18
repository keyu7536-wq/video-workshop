"""Console output for UTF-8 CLIs and caller-owned legacy/captured streams."""
import sys


def configure_console():
    """Use UTF-8 at CLI entry points; tolerate pythonw and captured streams."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, 'reconfigure', None)
        if callable(reconfigure):
            try:
                reconfigure(encoding='utf-8', errors='backslashreplace')
            except (OSError, ValueError):
                # Embedded applications may own an already-used/closed stream.
                # Library logging still handles a non-UTF-8 text encoding below.
                pass


def console_print(message, *, end='\n'):
    """Write progress without changing a host application's selected encoding.

    Escaped code points preserve information when a legacy stream cannot encode
    Chinese. Real file/pipe errors still propagate instead of being concealed.
    """
    stream = sys.stdout
    if stream is None:
        return
    text = str(message) + end
    try:
        stream.write(text)
    except UnicodeEncodeError:
        encoding = getattr(stream, 'encoding', None) or 'ascii'
        stream.write(text.encode(encoding, 'backslashreplace').decode(encoding))
    stream.flush()
