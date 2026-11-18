import io
from email.message import Message
from http.client import IncompleteRead
import socket
import ssl
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import studio_research as research
from studio_network import retry_transient_get


class Response:
    def __init__(self, data=b'complete', length=None, failure=None):
        self.body=io.BytesIO(data);self.failure=failure;self.closed=False
        self.headers=Message();self.headers['Content-Type']='text/html'
        if length is not None:self.headers['Content-Length']=str(length)
    def __enter__(self):return self
    def __exit__(self,*args):self.closed=True
    def geturl(self):return 'https://example.org/'
    def read1(self,size):
        if self.failure:
            error=self.failure;self.failure=None;raise error
        return self.body.read(size)


class NetworkRetryTests(unittest.TestCase):
    def test_transient_tls_eof_retries_same_validated_url_with_default_tls(self):
        response=Response();opener=Mock()
        opener.open.side_effect=[URLError(ssl.SSLEOFError(8,'unexpected EOF')),response]
        with patch('studio_research.public_url') as guard, \
             patch('studio_research.build_opener',return_value=opener) as factory, \
             patch('studio_network.time.sleep'),patch('studio_research.pv.log'):
            data,final,mime=research.fetch('https://example.org')
        self.assertEqual(data,b'complete');self.assertEqual(final,'https://example.org/')
        self.assertEqual(mime,'text/html');self.assertEqual(guard.call_count,2)
        self.assertEqual(factory.call_args_list[0].args,(research.Redirect,))
        self.assertEqual([c.args[0].full_url for c in opener.open.call_args_list],
                         ['https://example.org','https://example.org'])
        self.assertTrue(response.closed)

    def test_access_denial_rate_limit_cert_and_policy_errors_are_not_retried(self):
        errors=[HTTPError('https://example.org',code,'denied',{},None) for code in (401,403,429,500)]
        errors += [URLError(ssl.SSLCertVerificationError(1,'certificate verify failed')),
                   URLError(ssl.SSLError(1,'wrong TLS version')),ValueError('private URL rejected'),
                   URLError(socket.gaierror(socket.EAI_NONAME,'unknown host'))]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                operation=Mock(side_effect=error)
                with self.assertRaises(type(error)):
                    retry_transient_get(operation,sleep=lambda _:None)
                self.assertEqual(operation.call_count,1)

    def test_partial_response_is_discarded_and_size_errors_are_not_retried(self):
        partial=Response(b'partial',length=100);complete=Response(b'new response',length=12)
        opener=Mock();opener.open.side_effect=[partial,complete]
        with patch('studio_research.public_url'),patch('studio_research.build_opener',return_value=opener), \
             patch('studio_research.pv.log'):
            data,_,_=research.fetch('https://example.org')
        self.assertEqual(data,b'new response');self.assertTrue(partial.closed)
        opener=Mock();opener.open.return_value=Response(b'too long',length=8)
        with patch('studio_research.public_url'),patch('studio_research.build_opener',return_value=opener):
            with self.assertRaisesRegex(ValueError,'大小上限'):research.fetch('https://example.org',limit=3)
        self.assertEqual(opener.open.call_count,1)

    def test_retry_count_and_time_budget_bound_failures(self):
        operation=Mock(side_effect=URLError(TimeoutError('timed out')))
        with self.assertRaises(URLError):retry_transient_get(operation,sleep=lambda _:None)
        self.assertEqual(operation.call_count,3)
        now=[0.0];timeouts=[]
        def slow(timeout):
            timeouts.append(timeout);now[0]+=timeout;raise TimeoutError('timed out')
        with self.assertRaises(TimeoutError):
            retry_transient_get(slow,timeout=25,budget=30,clock=lambda:now[0],
                                sleep=lambda delay:now.__setitem__(0,now[0]+delay))
        self.assertEqual(timeouts,[25,4.75])
        self.assertEqual(now[0],30)

    def test_temporary_dns_and_body_read_faults_retry_but_redirect_guard_remains(self):
        for error in (URLError(socket.gaierror(socket.EAI_AGAIN,'temporary DNS failure')),
                      ConnectionResetError('reset'),IncompleteRead(b'partial',20)):
            operation=Mock(side_effect=[error,b'ok'])
            self.assertEqual(retry_transient_get(operation,sleep=lambda _:None),b'ok')
            self.assertEqual(operation.call_count,2)
        opener=Mock();opener.open.return_value=Response(failure=ConnectionResetError('body reset'))
        with patch('studio_research.public_url',side_effect=[None,ValueError('private URL rejected')]) as guard, \
             patch('studio_research.build_opener',return_value=opener),patch('studio_research.pv.log'):
            with self.assertRaisesRegex(ValueError,'private URL'):research.fetch('https://example.org')
        self.assertEqual(guard.call_count,2)
        self.assertEqual(opener.open.call_count,1)


if __name__=='__main__':unittest.main()
