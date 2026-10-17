"""Exercise the Python client against the actual Rust routes with an in-memory backend only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from pc_driver.clash_verge import BridgeUnavailable, Client, CommandFailed, Instance, OutcomeUnknown, ReadOnlyError


class BridgeClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(dir=ROOT / '.pio')
        cls.record = Path(cls.directory.name) / 'instance.json'
        exe = Path(__file__).parent / 'harness/target/debug' / (
            'xte-clash-bridge-harness.exe' if os.name == 'nt' else 'xte-clash-bridge-harness')
        cls.server = subprocess.Popen([str(exe), '--simulate', str(cls.record)],
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            deadline = time.monotonic() + 10
            while not cls.record.exists():
                if cls.server.poll() is not None:
                    raise RuntimeError(cls.server.stdout.read().decode('utf-8', 'replace'))
                if time.monotonic() > deadline:
                    raise RuntimeError('simulation did not start')
                time.sleep(0.02)
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        cls.server.wait(timeout=5)
        cls.server.stdout.close()
        cls.directory.cleanup()

    def client(self, write=False):
        return Client(self.record, allow_mutations=write)

    def test_a_default_client_rejects_all_writes_before_network_access(self):
        client = Client(Path(self.directory.name) / 'does-not-exist.json')
        for operation in [{'action':'set_tun','enabled':False}, {'action':'set_system_proxy','enabled':False},
                          {'action':'select_node','group':'代理组','node':'节点 B'}, {'action':'update_profile','uid':'r1'}]:
            with self.assertRaises(ReadOnlyError):
                client.execute(operation)
        self.assertNotIn('token', repr(Instance(1234, 'hidden-secret')))
        self.assertNotIn('hidden-secret', repr(Instance(1234, 'hidden-secret')))
        with self.assertRaises(ValueError):
            Client(allow_mutations='false')

    def test_b_real_http_roundtrip_for_four_simulated_actions(self):
        client = self.client(True)
        self.assertTrue(client.capabilities()['ready'])
        before = client.state()['profiles'][0]['updated']
        for operation in [{'action':'update_profile','uid':'r1'},
                          {'action':'select_node','group':'代理组','node':'节点 B'},
                          {'action':'set_system_proxy','enabled':False}, {'action':'set_tun','enabled':False}]:
            self.assertEqual(client.execute(operation)['status'], 'done')
        state = client.state()
        self.assertEqual(state['profiles'][0]['updated'], before + 1)
        self.assertEqual(state['groups'][0]['current'], '节点 B')
        self.assertFalse(state['system_proxy'])
        self.assertFalse(state['tun'])

    def test_c_unknown_outcome_is_not_replayed(self):
        client = self.client(True)
        before = client.state()['profiles'][0]['updated']
        with self.assertRaises(OutcomeUnknown) as result:
            client.execute({'action':'update_profile','uid':'r1'}, wait_seconds=0)
        self.assertEqual(result.exception.code, 'operation_pending')
        time.sleep(0.08)
        self.assertEqual(client.state()['profiles'][0]['updated'], before + 1)
        with self.assertRaises(OutcomeUnknown) as uncertain:
            client.execute({'action':'update_profile','uid':'uncertain'})
        self.assertEqual(uncertain.exception.code, 'readback_failed')

    def test_d_duplicate_request_ids_and_errors(self):
        client = self.client(True)
        operation = {'action':'update_profile','uid':'r1'}
        before = client.state()['profiles'][0]['updated']
        client.execute(operation, request_id='deduplicate')
        client.execute(operation, request_id='deduplicate')
        self.assertEqual(client.state()['profiles'][0]['updated'], before + 1)
        with self.assertRaises(BridgeUnavailable):
            client.execute({'action':'set_tun','enabled':True}, request_id='deduplicate')
        with self.assertRaises(CommandFailed) as failed:
            client.execute({'action':'select_node','group':'代理组','node':'missing'})
        self.assertNotIsInstance(failed.exception, OutcomeUnknown)
        self.assertEqual(failed.exception.code, 'node_not_found')

    def test_e_validation_and_fixed_loopback_transport(self):
        client = self.client(True)
        for operation in [{'action':'restart'}, {'action':'set_tun','enabled':0},
                          {'action':'set_tun','enabled':True,'extra':1},
                          {'action':'select_node','group':'bad\nname','node':'X'}]:
            with self.assertRaises(ValueError):
                client.execute(operation)
        instance = client._instance()
        with self.assertRaises(ValueError):
            client._request(instance, 'GET', '/commands/visible')
        with self.assertRaises(ValueError):
            client._request(instance, 'PATCH', '/configs')
        bad = Path(self.directory.name) / 'bad-instance.json'
        bad.write_text(json.dumps({'port':True,'token':'a'*64}), encoding='utf-8')
        with self.assertRaises(BridgeUnavailable):
            Client(bad).capabilities()
        bad.write_text('x' * 4097, encoding='utf-8')
        with self.assertRaises(BridgeUnavailable):
            Client(bad).capabilities()

    def test_f_lost_submission_reply_does_not_post_again(self):
        client = self.client(True)
        before = client.state()['profiles'][0]['updated']
        original = client._request
        posts = []
        def lost_reply(instance, method, endpoint, payload=None):
            result = original(instance, method, endpoint, payload)
            if method == 'POST':
                posts.append(payload['id'])
                raise BridgeUnavailable('bridge_connection_failed')
            return result
        client._request = lost_reply
        with self.assertRaises(OutcomeUnknown) as result:
            client.execute({'action':'update_profile','uid':'r1'})
        self.assertEqual(result.exception.code, 'submission_unconfirmed')
        time.sleep(0.08)
        self.assertEqual(client.state()['profiles'][0]['updated'], before + 1)
        self.assertEqual(len(posts), 1)

    def test_g_instance_rotation_stops_polling_without_replay(self):
        client = self.client(True)
        original = client._request
        record = self.record.read_bytes()
        posts = []
        def rotate_after_submit(instance, method, endpoint, payload=None):
            result = original(instance, method, endpoint, payload)
            if method == 'POST':
                posts.append(payload['id'])
                self.record.write_text(json.dumps({'port':instance.port,'token':'b'*64}), encoding='utf-8')
            return result
        client._request = rotate_after_submit
        try:
            with self.assertRaises(OutcomeUnknown) as result:
                client.execute({'action':'update_profile','uid':'r1'})
            self.assertEqual(result.exception.code, 'receipt_unavailable')
            self.assertEqual(len(posts), 1)
        finally:
            self.record.write_bytes(record)

    def test_h_malformed_failure_receipt_is_uncertain(self):
        client = self.client(True)
        instance = Instance(1234, 'a' * 64)
        client._instance = lambda: instance
        client._capabilities = lambda _: {'ready': True, 'operations': ['set_system_proxy']}
        calls = []
        def malformed(instance, method, endpoint, payload=None):
            calls.append(method)
            return {'v':1,'id':payload['id'],'status':'failed','error':'malformed'}
        client._request = malformed
        with self.assertRaises(OutcomeUnknown) as result:
            client.execute({'action':'set_system_proxy','enabled':False})
        self.assertEqual(result.exception.code, 'invalid_receipt')
        self.assertEqual(calls, ['POST'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
