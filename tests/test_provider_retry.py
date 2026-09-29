import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
import resolvers
import updater


def response(status=200, payload=None, text=''):
    result = Mock(status_code=status, url='https://example.com/api', text=text)
    result.json.return_value = payload or {}
    if status >= 400:
        result.raise_for_status.side_effect = requests.HTTPError(response=result)
    return result


class ProviderRetryTests(unittest.TestCase):
    def test_connection_reset_reopens_pool_and_retries(self):
        session = Mock()
        session.request.side_effect = [requests.ConnectionError('WinError 10054'), response()]
        with patch('resolvers.time.sleep'):
            result = resolvers._request(session, 'GET', 'https://example.com/playlist')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(session.request.call_count, 2)
        session.close.assert_called_once()

    def test_read_only_player_post_retries_transient_server_error(self):
        session = Mock()
        failed = response(500)
        session.request.side_effect = [failed, response()]
        with patch('resolvers.time.sleep'):
            resolvers._request(session, 'POST', 'https://example.com/links', data={'id': '1'})
        failed.close.assert_called_once()
        self.assertEqual(session.request.call_count, 2)

    def test_server_retries_are_bounded(self):
        session = Mock()
        session.request.side_effect = [response(503) for _ in range(3)]
        with patch('resolvers.time.sleep'), self.assertRaises(requests.HTTPError):
            resolvers._request(session, 'GET', 'https://example.com/playlist')
        self.assertEqual(session.request.call_count, 3)

    def test_missing_file_is_not_retried_as_transient(self):
        session = Mock()
        session.request.return_value = response(404)
        with patch('resolvers.time.sleep') as sleep, self.assertRaises(requests.HTTPError):
            resolvers._request(session, 'GET', 'https://example.com/missing')
        self.assertEqual(session.request.call_count, 1)
        sleep.assert_not_called()

    def test_kodik_document_does_not_invent_embedding_referrer(self):
        resolver = resolvers.PlayerResolver()
        url = 'https://kodikplayer.com/seria/1/hash/720p'
        page = ("var urlParams = '" + json.dumps({'d': 'kodikplayer.com'}) + "';"
                "vInfo.type='seria';vInfo.hash='hash';vInfo.id='1';"
                '<script src="/assets/js/app.player_single.test.js"></script>')
        def request(session, method, target, **kwargs):
            if target == url:
                self.assertNotIn('Referer', kwargs['headers'])
                return response(text=page)
            if target.endswith('.js'):
                return response(text='url:atob("L2Z0b3I=")')
            self.assertEqual(kwargs['headers']['Origin'], 'https://kodikplayer.com')
            return response(payload={'links': {'720': [{'src': 'https://example.com/720.m3u8'}]}})
        with patch('resolvers._request', side_effect=request):
            result = resolver.resolve_kodik(SimpleNamespace(iframe_url=url))
        self.assertEqual(result.url, 'https://example.com/720.m3u8')

    def test_alloha_restarts_local_server_after_refused_connection(self):
        resolver = resolvers.PlayerResolver({'alloha_resolver_url': 'http://127.0.0.1:8790'})
        with patch('alloha_runtime.ensure_resolver') as ensure, \
             patch('resolvers.requests.get', side_effect=[requests.ConnectionError('refused'),
                   response(payload={'url': 'http://127.0.0.1:8790/video.m3u8'})]), \
             patch('resolvers.time.sleep'):
            result = resolver.resolve_alloha(SimpleNamespace(iframe_url='https://alloha.example/'))
        self.assertEqual(ensure.call_count, 2)
        self.assertEqual(result.source, 'alloha')

    def test_updater_recovers_after_tls_eof(self):
        manifest = {'version': '4.5.4'}
        for key, name in [('exe', 'YummyAnimeManager.exe'), ('source', 'source.zip')]:
            manifest[key] = {'url': updater.RAW_BASE + name, 'sha256': 'a' * 64, 'size': 10}
        get = Mock(side_effect=[requests.exceptions.SSLError('TLS EOF'), response(payload=manifest)])
        with patch('updater.time.sleep'):
            self.assertEqual(updater.fetch_manifest(get), manifest)
        self.assertEqual(get.call_count, 2)

    def test_aksor_does_not_reprobe_unchanged_missing_url(self):
        resolver = resolvers.PlayerResolver()
        payload = {'qualities': {'q1080': 'https://cdn.example/1080.mpd'}}
        def request(session, method, url, **kwargs):
            return response(payload=payload)
        with patch('resolvers._request', side_effect=request), \
             patch('resolvers._stream_probe', return_value=(False, 'url', 'HTTP 404')) as probe, \
             patch('resolvers.time.sleep'), self.assertRaises(RuntimeError):
            resolver.resolve_aksor(SimpleNamespace(iframe_url='https://player.aksor.tv/video/abc'))
        probe.assert_called_once()
