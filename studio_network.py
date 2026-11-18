"""Bounded retries for idempotent public-source reads, without TLS fallbacks."""
from __future__ import annotations

import errno
from http.client import IncompleteRead, RemoteDisconnected
import socket
import ssl
import time
from urllib.error import HTTPError, URLError


def _transient_transport_error(error):
    # An HTTP response is a server decision, including authentication, access
    # denial and rate limits. Never retry it through this transport policy.
    if isinstance(error, HTTPError):
        return False
    if isinstance(error, URLError):
        return isinstance(error.reason, BaseException) and _transient_transport_error(error.reason)
    if isinstance(error, (ssl.SSLCertVerificationError, ssl.CertificateError)):
        return False
    if isinstance(error, ssl.SSLEOFError):
        return True
    if isinstance(error, ssl.SSLError):
        return getattr(error, 'reason', '') == 'UNEXPECTED_EOF_WHILE_READING'
    if isinstance(error, socket.gaierror):
        return error.errno == socket.EAI_AGAIN
    if isinstance(error, (TimeoutError, ConnectionResetError, ConnectionAbortedError,
                          BrokenPipeError, RemoteDisconnected, IncompleteRead)):
        return True
    return isinstance(error, OSError) and error.errno in (
        errno.ECONNRESET, errno.ECONNABORTED, errno.ETIMEDOUT, errno.EPIPE)


def retry_transient_get(operation, *, attempts=3, timeout=25, budget=45,
                        on_retry=None, clock=time.monotonic, sleep=time.sleep):
    """Run a GET callback with a shrinking timeout, retrying only transport faults.

    ``operation(timeout_seconds)`` must discard partial data on failure and retain
    its normal URL/TLS checks. No request is retried after the total retry budget;
    OS DNS resolution can still have platform-managed timeout behavior.
    """
    if type(attempts) is not int or attempts < 1 or timeout <= 0 or budget <= 0:
        raise ValueError('Retry counts and time budgets must be positive.')
    deadline = clock() + budget
    for index in range(attempts):
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError('Public-source read exceeded its retry time budget.')
        try:
            return operation(min(timeout, remaining))
        except Exception as exc:
            if not _transient_transport_error(exc) or index + 1 >= attempts:
                raise
            delay = .25 * 2**index
            if deadline - clock() <= delay:
                raise
            if on_retry is not None:
                on_retry(index + 2, exc)
            sleep(delay)


def read_limited(response, limit, deadline, *, clock=time.monotonic):
    """Read raw response bytes with a size cap and remaining-time socket timeout.

    This does not change TLS verification. Standard urllib HTTPResponse exposes
    its socket through the buffered reader; other response implementations still
    receive the original open timeout and deadline checks between reads.
    """
    length = response.headers.get('Content-Length', '')
    expected = int(length) if str(length).isdigit() else None
    if expected is not None and expected > limit:
        raise ValueError('来源超过本次读取大小上限')
    chunks, size = [], 0
    reader = getattr(response, 'read1', None) or response.read
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError('Public-source response exceeded its read deadline.')
        raw = getattr(getattr(response, 'fp', None), 'raw', None)
        stream_socket = getattr(raw, '_sock', None)
        if stream_socket is not None:
            stream_socket.settimeout(remaining)
        chunk = reader(min(64*1024, limit-size+1))
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            raise ValueError('来源超过本次读取大小上限')
        chunks.append(chunk)
    if expected is not None and size < expected:
        raise IncompleteRead(b'', expected-size)
    return b''.join(chunks)
