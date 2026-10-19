import copy
import json
import time
import unittest
from unittest.mock import Mock, patch

from pc_driver.clash_panel import Backend, Pages, PanelError, Worker, encode, label
from pc_driver.clash_verge import BridgeUnavailable, Instance


class FakeBackend:
    source = 'native'
    operations = {'select_node'}

    def __init__(self):
        self.calls = []
        self.data = {'profiles': [{'uid': 'p1', 'name': '真实订阅', 'kind': 'remote'}],
                     'current_profile': 'p1', 'system_proxy': True, 'tun': False,
                     'groups': [{'name': '手动组', 'kind': 'Selector', 'current': '日本 00',
                                 'members': ['日本 %02d' % i for i in range(12)]}]}

    def state(self):
        return copy.deepcopy(self.data)

    def execute(self, operation):
        self.calls.append(operation)
        if operation['action'] == 'select_node':
            self.data['groups'][0]['current'] = operation['node']
        elif operation['action'] in {'set_tun', 'set_system_proxy'}:
            key = 'tun' if operation['action'] == 'set_tun' else 'system_proxy'
            self.data[key] = operation['enabled']
        return '已切换，仅本次运行有效'


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.pages = Pages(self.backend)
        self.identifier = 0

    def request(self, view='home', page=0, index=None):
        self.identifier += 1
        request = {'id': self.identifier, 'op': 'list', 'view': view, 'page': page}
        if index is not None:
            snap = self.pages.snapshot
            request.update(op='activate', rev=snap['rev'], view=snap['view'],
                           page=snap['page'], index=index)
        return self.pages.page(request)

    def nodes(self):
        self.request('groups')
        return self.request(index=0)

    def test_home_shows_real_flags_and_locked_operations(self):
        frames = [json.loads(f) for f in self.request()]
        self.assertEqual(len(frames), 7)
        self.assertEqual(frames[0]['view'], 'home')
        self.assertIn('开启', frames[3]['name'])
        self.assertFalse(frames[3]['enabled'])
        self.assertNotIn('锁定', frames[4]['name'])
        self.assertFalse(frames[4]['enabled'])
        self.assertEqual(self.backend.calls, [])

    def test_utf8_paging_and_duplicate_labels_keep_identity(self):
        self.nodes()
        self.backend.data['groups'][0]['members'][6] = '中' * 100 + '😀'
        frames = self.request('nodes', page=1)
        values = [json.loads(f) for f in frames]
        self.assertEqual(values[0]['pages'], 3)
        self.assertEqual(values[0]['page'], 1)
        self.assertLessEqual(len(values[2]['name'].encode('utf-8')), 96)
        self.assertTrue(all(len(frame) - 1 <= 384 for frame in frames))
        self.request(index=1)
        self.assertEqual(self.backend.calls[0]['node'], '中' * 100 + '😀')

    def test_native_node_switch_returns_read_back_state(self):
        self.nodes()
        frames = [json.loads(f) for f in self.request(index=1)]
        self.assertTrue(frames[2]['current'])
        self.assertIn('仅本次运行', frames[-1]['message'])
        self.assertEqual(self.backend.calls[0]['action'], 'select_node')

    def test_stale_revision_and_external_change_rejected(self):
        self.nodes()
        request = {'id': 30, 'op': 'activate', 'view': 'nodes', 'page': 0,
                   'rev': self.pages.snapshot['rev'], 'index': 1}
        self.request('nodes')
        with self.assertRaises(PanelError):
            self.pages.page(request)
        request['rev'] = self.pages.snapshot['rev']
        self.backend.data['groups'][0]['current'] = '日本 04'
        with self.assertRaises(PanelError):
            self.pages.page(request)
        self.assertFalse(self.backend.calls)

    def test_failed_write_consumes_snapshot_and_never_replays(self):
        self.nodes()
        self.backend.execute = Mock(side_effect=PanelError('结果待确认'))
        request = {'id': 31, 'op': 'activate', 'view': 'nodes', 'page': 0,
                   'rev': self.pages.snapshot['rev'], 'index': 1}
        for _ in range(2):
            with self.assertRaises(PanelError):
                self.pages.page(request)
        self.assertEqual(self.backend.execute.call_count, 1)

    def test_unavailable_capabilities_never_execute(self):
        self.request()
        with self.assertRaisesRegex(PanelError, 'TUN'):
            self.request(index=3)
        self.request('profiles')
        with self.assertRaisesRegex(PanelError, '兼容接口'):
            self.request(index=0)
        self.assertFalse(self.backend.calls)
        backend = Backend()
        backend.operations = set()
        with self.assertRaisesRegex(PanelError, '兼容接口'):
            backend.execute({'action': 'set_tun', 'enabled': True})
        with self.assertRaisesRegex(PanelError, '兼容接口'):
            backend.execute({'action': 'update_profile', 'uid': 'test'})

    def test_capable_switches_and_subscription_dispatch_only_after_activate(self):
        self.backend.operations = {'set_tun', 'set_system_proxy', 'update_profile'}
        frames = [json.loads(f) for f in self.request()]
        self.assertTrue(frames[3]['enabled'] and frames[4]['enabled'])
        self.assertFalse(self.backend.calls)
        self.request(index=3)
        self.assertTrue(self.backend.data['tun'])
        self.assertEqual(self.backend.calls, [{'action': 'set_tun', 'enabled': True}])
        self.request(index=2)
        self.assertFalse(self.backend.data['system_proxy'])
        self.request('profiles')
        self.request(index=0)
        self.assertEqual(self.backend.calls[-1], {'action': 'update_profile', 'uid': 'p1'})

    def test_switch_external_change_rejected_without_write(self):
        self.backend.operations = {'set_tun', 'set_system_proxy'}
        for index, key in ((2, 'system_proxy'), (3, 'tun')):
            self.request()
            self.backend.data[key] = not self.backend.data[key]
            with self.assertRaisesRegex(PanelError, '状态已变化'):
                self.request(index=index)
        self.assertFalse(self.backend.calls)

    def test_bridge_preserves_all_user_control_capabilities(self):
        bridge = Mock()
        bridge.capabilities.return_value = {'ready': True, 'operations':
                                           ['set_tun', 'update_profile', 'set_system_proxy']}
        bridge.state.return_value = self.backend.data
        backend = Backend(bridge=bridge)
        backend.state()
        self.assertIn('set_tun', backend.operations)
        for operation in ({'action': 'set_tun', 'enabled': True},
                          {'action': 'update_profile', 'uid': 'p1'}):
            backend.execute(operation)
            bridge.execute.assert_called_with(operation, wait_seconds=60)

    def test_native_tun_uses_only_minimal_patch_and_readback(self):
        backend = Backend(bridge=Mock())
        backend.operations = {'set_tun'}
        backend.query = Mock(side_effect=[{'tun': {'enable': False}}, None, {'tun': {'enable': True}}])
        backend.execute({'action': 'set_tun', 'enabled': True})
        self.assertEqual(backend.query.call_args_list[1].args,
                         ('PATCH', '/configs', {'tun': {'enable': True}}))
        self.assertEqual(backend.query.call_count, 3)
        backend.query = Mock(side_effect=[{'tun': {'enable': False}}, OSError('lost')])
        with self.assertRaisesRegex(PanelError, '不会重试'):
            backend.execute({'action': 'set_tun', 'enabled': True})
        self.assertEqual(backend.query.call_count, 2)

    def test_proxy_guard_foreign_proxy_and_autodetect_disable_fallback(self):
        bridge = Mock()
        bridge._instance.return_value = Instance(12345, '0' * 64)
        backend = Backend(bridge=bridge)
        config = {'mixed-port': 7890}
        owned = {'flags': 3, 'server': '127.0.0.1:7890', 'bypass': '<local>', 'pac': ''}
        self.assertEqual(backend.proxy_status(config, {}, owned)[:2], (True, True))
        self.assertFalse(backend.proxy_status(config, {'enable_proxy_guard': True}, owned)[1])
        for change in ({'server': 'other:8080'}, {'flags': 11}, {'flags': 5, 'pac': 'http://other/pac'}):
            self.assertFalse(backend.proxy_status(config, {}, dict(owned, **change))[1])

    @patch('pc_driver.clash_panel.windows_proxy')
    def test_native_system_proxy_manual_and_pac_explicit_targets(self, proxy):
        bridge = Mock()
        bridge._instance.return_value = Instance(12345, '0' * 64)
        backend = Backend(bridge=bridge)
        backend.operations = {'set_system_proxy'}
        backend.query = Mock(return_value={'mixed-port': 7890, 'tun': {'enable': False}})
        before = {'flags': 3, 'server': '127.0.0.1:7890', 'bypass': '<local>', 'pac': ''}
        backend.document = Mock(return_value={})
        proxy.read.side_effect = [before, dict(before, flags=1)]
        backend.execute({'action': 'set_system_proxy', 'enabled': False})
        proxy.write.assert_called_once_with(dict(before, flags=1))
        proxy.reset_mock()
        backend.document.return_value = {'proxy_auto_config': True}
        after = dict(before, flags=5, pac='http://127.0.0.1:12345/commands/pac')
        proxy.read.side_effect = [dict(before, flags=1), after]
        backend.execute({'action': 'set_system_proxy', 'enabled': True})
        proxy.write.assert_called_once_with(after)
        self.assertTrue(all(call.args[0] == 'GET' for call in backend.query.call_args_list))

    def test_background_worker_deduplicates_and_discards_old_results(self):
        worker = Worker(self.backend)
        try:
            line = 'XTE: clash ' + json.dumps({'id': 1, 'op': 'list', 'view': 'home', 'page': 0})
            self.assertTrue(worker.handle(line, 1))
            self.assertTrue(worker.handle(line, 1))
            self.assertFalse(worker.handle('XTE: pong', 1))
            deadline = time.monotonic() + 2
            while worker.busy and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(worker.take(1)), 7)
            worker.handle(line, 1)
            self.assertEqual(worker.take(1), [])
            worker.handle(line, 2)
            while worker.busy and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(worker.take(3), [])
        finally:
            worker.close()

    def test_malformed_request_and_frame_boundaries(self):
        self.assertNotIn('😀', label('中😀文\n'))
        with self.assertRaises(ValueError):
            encode({'name': '中' * 200})
        worker = Worker(self.backend)
        try:
            for value in ({'id': True}, {'id': 1, 'op': 'activate', 'view': 'tun', 'page': 0},
                          {'id': 1, 'op': 'list', 'view': 'home', 'page': -1}):
                self.assertTrue(worker.handle('XTE: clash ' + json.dumps(value), 1))
            self.assertFalse(worker.busy)
        finally:
            worker.close()


if __name__ == '__main__':
    unittest.main()
