"""API selection, opaque cursors and quota classification; no real requests."""
from __future__ import annotations

import json
import unittest
from urllib.parse import parse_qs, urlsplit

from channel_connections.ports import ProviderRejected
from web_ui.google_provider import API_ROOT, GoogleDataGateway, _decode
from web_ui.tests.test_google_provider import CONFIG, NOW, DataGatewayFixture, stored_credential


class SubscriberTraversalTests(DataGatewayFixture):
    def test_new_traversal_requests_my_subscribers_and_keeps_the_query_on_next_page(self):
        reply = (200, json.dumps({'items': [], 'nextPageToken': 'raw-token'}).encode())
        gateway, transport, _ = self.build({f'{API_ROOT}/subscriptions': reply}, stored_credential())
        first = gateway.list_subscribers('ws-1', 'slot-1', page_token=None, max_results=50)
        first_query = parse_qs(urlsplit(transport.requests[-1][1]).query)
        self.assertEqual(first_query.get('mySubscribers'), ['true'])
        self.assertNotIn('myRecentSubscribers', first_query)
        gateway.list_subscribers('ws-1', 'slot-1', page_token=first.next_page_token, max_results=50)
        second_query = parse_qs(urlsplit(transport.requests[-1][1]).query)
        self.assertEqual(second_query.get('mySubscribers'), ['true'])
        self.assertEqual(second_query.get('pageToken'), ['raw-token'])
        self.assertNotIn('order', second_query)

    def test_legacy_unwrapped_cursors_continue_recent_feed_not_a_different_query(self):
        reply = (200, json.dumps({'items': [], 'nextPageToken': 'legacy-next'}).encode())
        gateway, transport, _ = self.build({f'{API_ROOT}/subscriptions': reply}, stored_credential())
        first = gateway.list_subscribers('ws-1', 'slot-1', page_token='legacy-old', max_results=50)
        gateway.list_subscribers('ws-1', 'slot-1', page_token=first.next_page_token, max_results=50)
        for request in transport.requests:
            query = parse_qs(urlsplit(request[1]).query)
            self.assertEqual(query.get('myRecentSubscribers'), ['true'])
            self.assertNotIn('mySubscribers', query)
        self.assertEqual(first.next_page_token, 'legacy-next')

    def test_no_application_stop_at_twenty_pages_or_one_thousand_rows(self):
        calls = []
        def transport(method, url, **kwargs):
            query = parse_qs(urlsplit(url).query)
            self.assertEqual(query.get('mySubscribers'), ['true'])
            offset = int(query.get('pageToken', ['0'])[0])
            calls.append(offset)
            items = [{'subscriberSnippet': {'channelId': f'UC_s{i}', 'title': f'S{i}'},
                      'snippet': {'publishedAt': '2026-01-01T00:00:00Z'}}
                     for i in range(offset, min(offset+50, 1251))]
            payload = {'items': items}
            if offset+50 < 1251:
                payload['nextPageToken'] = str(offset+50)
            return 200, json.dumps(payload).encode()
        _, _, store = self.build({}, stored_credential())
        gateway = GoogleDataGateway(CONFIG, store, transport=transport, now=lambda: NOW)
        token = None
        rows = []
        for _ in range(100):
            page = gateway.list_subscribers('ws-1', 'slot-1', page_token=token, max_results=50)
            rows.extend(page.rows)
            token = page.next_page_token
            if token is None:
                break
        self.assertEqual(len(rows), 1251)
        self.assertEqual(calls, list(range(0, 1251, 50)))

    def test_daily_quota_has_its_own_safe_exception_on_oauth_and_key_reads(self):
        from channel_connections.ports import ProviderQuotaExceeded
        for reason in ('quotaExceeded', 'dailyLimitExceeded'):
            for path in ('subscriptions', 'commentThreads'):
                with self.subTest(reason=reason, path=path):
                    body = json.dumps({'error': {'errors': [{'reason': reason, 'message': 'secret-sentinel'}]}}).encode()
                    gateway, _, _ = self.build({f'{API_ROOT}/{path}': (403, body)}, stored_credential())
                    with self.assertRaises(ProviderQuotaExceeded) as caught:
                        if path == 'subscriptions':
                            gateway.list_subscribers('ws-1', 'slot-1', page_token=None, max_results=50)
                        else:
                            gateway.list_video_comment_authors('ws-1', 'slot-1', video_id='v', page_token=None, max_results=50)
                    self.assertEqual(caught.exception.quota_cost, 1)
                    self.assertNotIn('secret-sentinel', str(caught.exception))

    def test_ordinary_forbidden_is_not_mislabeled_as_daily_quota(self):
        for reason in ('commentsDisabled', 'forbidden', 'rateLimitExceeded'):
            with self.subTest(reason=reason):
                with self.assertRaises(ProviderRejected):
                    _decode(403, json.dumps({'error': {'errors': [{'reason': reason}]}}).encode())

    def test_empty_intermediate_page_keeps_its_continuation(self):
        replies = (200, json.dumps({'items': [], 'nextPageToken': 'after-empty'}).encode())
        gateway, _, _ = self.build({f'{API_ROOT}/subscriptions': replies}, stored_credential())
        page = gateway.list_subscribers('ws-1', 'slot-1', page_token=None, max_results=50)
        self.assertEqual(page.rows, ())
        self.assertIsNotNone(page.next_page_token)

    def test_video_quota_failure_counts_successful_upload_lookup_and_failed_page(self):
        from channel_connections.ports import ProviderQuotaExceeded
        replies = {
            f'{API_ROOT}/channels': (200, json.dumps({'items': [{'contentDetails': {
                'relatedPlaylists': {'uploads': 'UU_test'}}}]}).encode()),
            f'{API_ROOT}/playlistItems': (403, json.dumps({'error': {'errors': [
                {'reason': 'quotaExceeded'}]}}).encode()),
        }
        gateway, transport, _ = self.build(replies, stored_credential())
        with self.assertRaises(ProviderQuotaExceeded) as caught:
            gateway.list_videos('ws-1', 'slot-1', page_token=None, max_results=50)
        self.assertEqual(caught.exception.quota_cost, 2)
        self.assertEqual(len(transport.requests), 2)
        with self.assertRaises(ProviderQuotaExceeded) as cached:
            gateway.list_videos('ws-1', 'slot-1', page_token=None, max_results=50)
        self.assertEqual(cached.exception.quota_cost, 1)

    def test_capability_probe_uses_same_subscriber_feed_as_new_collection(self):
        from web_ui.google_provider import GoogleAuthorizationGateway
        _, transport, store = self.build({f'{API_ROOT}/subscriptions': (200, b'{"items":[]}')}, stored_credential())
        gateway = GoogleAuthorizationGateway(CONFIG, store, transport=transport)
        self.assertTrue(gateway._subscriber_capability('synthetic-access'))
        query = parse_qs(urlsplit(transport.requests[0][1]).query)
        self.assertEqual(query.get('mySubscribers'), ['true'])
        self.assertNotIn('myRecentSubscribers', query)

    def test_cursor_wrapper_does_not_shorten_previously_supported_tokens(self):
        from channel_connections.models import ProviderOperation, ProviderOperationRequest, ProviderOperationResult
        raw = 't' * 256
        gateway, transport, _ = self.build({f'{API_ROOT}/subscriptions': (
            200, json.dumps({'items': [], 'nextPageToken': raw}).encode())}, stored_credential())
        first = gateway.list_subscribers('ws-1', 'slot-1', page_token=None, max_results=50)
        request = ProviderOperationRequest(ProviderOperation.LIST_SUBSCRIBERS, page_token=first.next_page_token)
        result = ProviderOperationResult(ProviderOperation.LIST_SUBSCRIBERS, (), first.next_page_token, 1)
        gateway.list_subscribers('ws-1', 'slot-1', page_token=request.page_token, max_results=50)
        self.assertEqual(parse_qs(urlsplit(transport.requests[-1][1]).query)['pageToken'], [raw])
        self.assertEqual(result.next_page_token, first.next_page_token)
        with self.assertRaises(ValueError):
            ProviderOperationRequest(ProviderOperation.LIST_SUBSCRIBERS, page_token='t'*513)
        with self.assertRaises(ValueError):
            ProviderOperationRequest(ProviderOperation.LIST_VIDEO_COMMENT_AUTHORS, video_id='v'*257)
