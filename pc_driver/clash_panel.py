"""Bounded device pages; local core reads and guarded, single-shot actions.

Verge's optional bridge handles persistent actions. Native node/TUN changes affect
the running core; system proxy uses WinINet. Development verification must never
toggle the real machine's TUN state.
"""
import asyncio
from collections import deque
import http.client
import io
import json
import os
from pathlib import Path
import queue
import re
import secrets
import threading
from urllib.parse import quote

import yaml

from .clash_verge import Client, BridgeUnavailable, CommandFailed, OutcomeUnknown
from . import windows_proxy

LIMIT = 2 * 1024 * 1024
ROWS = 5


class PanelError(OSError):
    pass


def label(value, limit=96):
    value = ''.join(ch if 32 <= ord(ch) < 127 or 0x4e00 <= ord(ch) <= 0x9fff
                    or ch in '，。！？：（）【】％' else ' ' for ch in str(value))
    return value.encode('utf-8')[:limit].decode('utf-8', 'ignore')


def encode(value):
    frame = json.dumps({'v': 2, 'cmd': 'clash', **value}, ensure_ascii=False,
                       separators=(',', ':')).encode('utf-8')
    if len(frame) > 384:
        raise ValueError('clash frame exceeds payload limit')
    return frame + b'\n'


class BufferedSocket:
    def __init__(self, data):
        self.data = data

    def makefile(self, *args):
        return io.BytesIO(self.data)


class Backend:
    def __init__(self, root=None, bridge=None):
        self.root = Path(root) if root else (Path(os.environ.get('APPDATA', Path.home() / 'AppData/Roaming')) /
                                           'io.github.clash-verge-rev.clash-verge-rev')
        self.bridge = bridge or Client(allow_mutations=True)
        self.operations = set()
        self.source = 'native'

    def document(self, name):
        data = (self.root / name).read_bytes()
        if len(data) > LIMIT:
            raise PanelError('配置文件过大')
        value = yaml.safe_load(data)
        if not isinstance(value, dict):
            raise PanelError('配置文件无效')
        return value

    def pipe(self):
        value = self.document('config.yaml').get('external-controller-pipe', '')
        match = re.search(r'-([0-9a-f]{64})$', value) if isinstance(value, str) else None
        if not match:
            raise PanelError('未找到内核管道')
        allowed = {'verge-mihomo-production-' + match[1], 'verge-mihomo-sidecar-release-' + match[1]}
        pipe_root = chr(92) * 2 + '.' + chr(92) + 'pipe' + chr(92)
        found = [name for name in os.listdir(pipe_root) if name in allowed]
        if len(found) != 1:
            raise PanelError('Clash 内核未运行')
        return pipe_root + found[0]

    async def _query(self, method, endpoint, payload=None):
        if not (method == 'GET' and endpoint in {'/configs', '/proxies'} or
                method == 'PATCH' and endpoint == '/configs' and isinstance(payload, dict) and
                set(payload) == {'tun'} and isinstance(payload['tun'], dict) and
                set(payload['tun']) == {'enable'} and type(payload['tun']['enable']) is bool or
                method == 'PUT' and endpoint.startswith('/proxies/') and
                isinstance(payload, dict) and set(payload) == {'name'}):
            raise ValueError('unsupported core endpoint')
        body = b'' if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=LIMIT)
        protocol = asyncio.StreamReaderProtocol(reader)
        transport, _ = await loop.create_pipe_connection(lambda: protocol, self.pipe())
        writer = asyncio.StreamWriter(transport, protocol, reader, loop)
        try:
            writer.write((f'{method} {endpoint} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n'
                          f'Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n').encode('ascii') + body)
            await writer.drain()
            data = bytearray()
            while chunk := await reader.read(65536):
                data.extend(chunk)
                if len(data) > LIMIT:
                    raise PanelError('内核响应过大')
            response = http.client.HTTPResponse(BufferedSocket(bytes(data)))
            response.begin()
            content = response.read(LIMIT + 1)
            if not 200 <= response.status < 300 or len(content) > LIMIT:
                raise PanelError('内核请求失败')
            return json.loads(content) if content else None
        finally:
            writer.close()

    async def _timed(self, method, endpoint, payload=None):
        return await asyncio.wait_for(self._query(method, endpoint, payload), timeout=3)

    def query(self, method, endpoint, payload=None):
        return asyncio.run(self._timed(method, endpoint, payload))

    def state(self):
        try:
            caps = self.bridge.capabilities()
            if caps['ready']:
                state = self.bridge.state()
                self.operations = set(caps['operations'])
                self.source = 'bridge'
                return state
        except BridgeUnavailable:
            pass
        self.operations = {'select_node'}
        self.source = 'native'
        config = self.query('GET', '/configs')
        proxies = self.query('GET', '/proxies')['proxies']
        profiles = self.document('profiles.yaml')
        verge = self.document('verge.yaml')
        enabled, can_proxy, _, _ = self.proxy_status(config, verge, windows_proxy.read())
        if can_proxy:
            self.operations.add('set_system_proxy')
        if type((config.get('tun') or {}).get('enable')) is bool:
            self.operations.add('set_tun')
        items = profiles.get('items') or []
        if not isinstance(items, list):
            raise PanelError('订阅列表无效')
        return {'profiles': [{'uid': item['uid'], 'name': item.get('name') or item['uid'],
                              'kind': item['type'], 'updated': item.get('updated')}
                             for item in items if isinstance(item, dict) and
                             item.get('type') in {'remote', 'local'} and isinstance(item.get('uid'), str)],
                'current_profile': profiles.get('current'),
                'groups': [{'name': name, 'kind': value.get('type'), 'current': value.get('now'),
                            'members': value['all']} for name, value in proxies.items()
                           if isinstance(value, dict) and isinstance(value.get('all'), list)],
                'system_proxy': bool(enabled), 'tun': bool((config.get('tun') or {}).get('enable'))}

    def proxy_status(self, config, verge, proxy):
        port = config.get('mixed-port')
        host = str(verge.get('proxy_host') or '127.0.0.1')
        expected = f'{host}:{port}'
        pac = ''
        try:
            pac = f'http://127.0.0.1:{self.bridge._instance().port}/commands/pac'
        except BridgeUnavailable:
            pass
        servers = [item.split('=', 1)[-1] for item in proxy['server'].split(';') if item]
        owns_manual = bool(servers) and all(item == expected for item in servers)
        owns_pac = bool(pac) and proxy['pac'] == pac
        enabled = bool(proxy['flags'] & 2 and owns_manual or proxy['flags'] & 4 and owns_pac)
        foreign = bool(proxy['flags'] & 2 and not owns_manual or proxy['flags'] & 4 and not owns_pac)
        can_write = (not verge.get('enable_proxy_guard', False) and not foreign and
                     not proxy['flags'] & 8 and host in {'127.0.0.1', 'localhost'} and
                     type(port) is int and 1 <= port <= 65535 and
                     (not verge.get('proxy_auto_config', False) or bool(pac)))
        return enabled, can_write, expected, pac

    def execute(self, operation):
        action = operation['action']
        if action not in self.operations:
            raise PanelError('此操作需要 Verge 兼容接口')
        if self.source == 'bridge':
            self.bridge.execute(operation, wait_seconds=60)
            return '操作完成，已回读'
        if action in {'set_system_proxy', 'set_tun'}:
            if set(operation) != {'action', 'enabled'} or type(operation['enabled']) is not bool:
                raise ValueError('enabled_must_be_bool')
            target = operation['enabled']
            config = self.query('GET', '/configs')
            if action == 'set_system_proxy':
                verge = self.document('verge.yaml')
                before = windows_proxy.read()
                current, allowed, server, pac = self.proxy_status(config, verge, before)
                if not allowed:
                    raise PanelError('代理受保护或由其他程序管理')
                if current == target:
                    raise PanelError('开关状态已变化，请刷新')
                after = dict(before)
                after['flags'] = 1 | ((4 if verge.get('proxy_auto_config') else 2) if target else 0)
                if target:
                    after['server'] = server
                    if verge.get('proxy_auto_config'):
                        after['pac'] = pac
                try:
                    windows_proxy.write(after)
                    actual = windows_proxy.read()
                except OSError as exc:
                    raise PanelError('代理结果待确认，请刷新；不会重试') from exc
                if actual != after:
                    raise PanelError('代理回读不一致，请刷新')
            else:
                if (config.get('tun') or {}).get('enable') is target:
                    raise PanelError('开关状态已变化，请刷新')
                try:
                    self.query('PATCH', '/configs', {'tun': {'enable': target}})
                    actual = self.query('GET', '/configs')
                except OSError as exc:
                    raise PanelError('TUN 结果待确认，请刷新；不会重试') from exc
                if (actual.get('tun') or {}).get('enable') is not target:
                    raise PanelError('TUN 未生效，请在电脑检查服务权限')
            return '已应用，Verge 重载后可能恢复'
        if action != 'select_node':
            raise PanelError('此操作需要 Verge 兼容接口')
        # Never reload configs or write Verge files for the runtime-only fallback.
        group, node = operation['group'], operation['node']
        proxies = self.query('GET', '/proxies')['proxies']
        item = proxies.get(group, {})
        if item.get('type') != 'Selector' or node not in item.get('all', []):
            raise PanelError('节点列表已变化，请刷新')
        try:
            self.query('PUT', '/proxies/' + quote(group, safe=''), {'name': node})
        except OSError as exc:
            raise PanelError('结果待确认，请刷新；不会重试') from exc
        if self.query('GET', '/proxies')['proxies'].get(group, {}).get('now') != node:
            raise PanelError('节点回读不一致，请刷新')
        return '已切换，仅本次运行有效'


class Pages:
    def __init__(self, backend=None):
        self.backend = backend or Backend()
        self.snapshot = None
        self.group = None

    def reset(self):
        self.snapshot = None
        self.group = None

    def page(self, request):
        identifier = request['id']
        view = request.get('view', 'home')
        number = request.get('page', 0)
        message = ''
        state = self.backend.state()
        if request['op'] == 'activate':
            snap = self.snapshot
            if not snap or request.get('rev') != snap['rev'] or view != snap['view'] or number != snap['page']:
                raise PanelError('页面已过期，请刷新')
            index = request.get('index')
            if type(index) is not int or not 0 <= index < len(snap['rows']):
                raise PanelError('选择无效，请刷新')
            row = snap['rows'][index]
            target = row.get('target')
            if target:
                view, number = target, 0
                if target == 'nodes':
                    self.group = row['key']
            elif not row['enabled']:
                raise PanelError(row.get('reason', '此操作不可用'))
            else:
                operation = row['operation']
                if operation['action'] == 'select_node':
                    fresh = next((g for g in state['groups'] if g['name'] == operation['group']), None)
                    if not fresh or fresh['current'] != row['previous'] or operation['node'] not in fresh['members']:
                        raise PanelError('节点列表已变化，请刷新')
                elif operation['action'] in {'set_system_proxy', 'set_tun'}:
                    key = 'tun' if operation['action'] == 'set_tun' else 'system_proxy'
                    if state[key] == operation['enabled']:
                        raise PanelError('开关状态已变化，请刷新')
                elif operation['action'] == 'update_profile' and not any(p['uid'] == operation['uid'] for p in state['profiles']):
                    raise PanelError('订阅列表已变化，请刷新')
                # Consume before write. Lost results can never replay this snapshot.
                self.snapshot = None
                message = self.backend.execute(operation)
                state = self.backend.state()
        if view == 'home':
            rows = [{'name': '订阅列表', 'target': 'profiles'}, {'name': '节点选择', 'target': 'groups'},
                    {'name': '系统代理：' + ('开启' if state['system_proxy'] else '关闭'),
                     'operation': {'action': 'set_system_proxy', 'enabled': not state['system_proxy']},
                     'enabled': 'set_system_proxy' in self.backend.operations, 'reason': '代理受保护或由其他程序管理'},
                    {'name': 'TUN：' + ('开启' if state['tun'] else '关闭'),
                     'operation': {'action': 'set_tun', 'enabled': not state['tun']},
                     'enabled': 'set_tun' in self.backend.operations, 'reason': '当前内核不提供 TUN 控制'},
                    {'name': '刷新状态', 'target': 'home'}]
        elif view == 'profiles':
            rows = [{'name': p['name'], 'key': p['uid'], 'current': p['uid'] == state['current_profile'],
                     'operation': {'action': 'update_profile', 'uid': p['uid']},
                     'enabled': p['kind'] == 'remote' and 'update_profile' in self.backend.operations,
                     'reason': '订阅更新需要 Verge 兼容接口' if p['kind'] == 'remote' else '本地配置无需订阅更新'}
                    for p in state['profiles']]
            message = message or ('确认更新订阅' if 'update_profile' in self.backend.operations else '订阅更新需要 Verge 兼容接口')
        elif view == 'groups':
            rows = [{'name': g['name'], 'target': 'nodes', 'key': g['name']} for g in state['groups']]
            message = message or '确认查看组内节点'
        elif view == 'nodes':
            group = next((g for g in state['groups'] if g['name'] == self.group), None)
            if not group:
                raise PanelError('策略组已消失，请返回刷新')
            rows = [{'name': node, 'current': node == group['current'], 'previous': group['current'],
                     'operation': {'action': 'select_node', 'group': group['name'], 'node': node},
                     'enabled': group['kind'] == 'Selector' and 'select_node' in self.backend.operations,
                     'reason': '自动策略组不能手动选择'} for node in group['members']]
            message = message or ('手选节点，仅本次运行' if self.backend.source == 'native' else '确认切换节点')
        else:
            raise PanelError('页面无效')
        for row in rows:
            row.setdefault('enabled', True)
            row.setdefault('current', False)
        if view == 'home' and self.backend.source == 'native':
            message = message or '开关即时生效，Verge 重载可能恢复'
        pages = max(1, (len(rows) + ROWS - 1) // ROWS)
        if number >= pages:
            raise PanelError('页码已变化，请返回刷新')
        selected = rows[number * ROWS:(number + 1) * ROWS]
        rev = secrets.token_hex(8)
        self.snapshot = {'rev': rev, 'view': view, 'page': number, 'rows': selected}
        frames = [encode({'id': identifier, 'phase': 'begin', 'rev': rev, 'count': len(selected),
                          'pages': pages, 'view': view, 'page': number})]
        frames.extend(encode({'id': identifier, 'phase': 'row', 'index': i,
                              'name': label(row['name']), 'enabled': row['enabled'], 'current': row['current']})
                      for i, row in enumerate(selected))
        frames.append(encode({'id': identifier, 'phase': 'end', 'message': label(message or '上下选择，确认进入')}))
        return frames


class Worker:
    def __init__(self, backend=None):
        self.pages = Pages(backend)
        self.jobs = queue.Queue(maxsize=1)
        self.results = queue.Queue(maxsize=2)
        self.stopped = threading.Event()
        self.epoch = None
        self.seen = deque(maxlen=64)
        self.busy = False
        self.thread = threading.Thread(target=self._run, name='xte-clash', daemon=True)
        self.thread.start()

    def handle(self, line, epoch):
        if not line.startswith('XTE: clash '):
            return False
        try:
            if len(line.encode('utf-8')) > 384:
                return True
            value = json.loads(line[11:])
            allowed = {'id', 'op', 'view', 'page', 'rev', 'index'}
            if not isinstance(value, dict) or set(value) - allowed or type(value.get('id')) is not int or not 1 <= value['id'] <= 0xffffffff:
                return True
            if value.get('op') not in {'list', 'activate'} or value.get('view') not in {'home', 'profiles', 'groups', 'nodes'}:
                return True
            if type(value.get('page')) is not int or not 0 <= value['page'] < 65535:
                return True
            key = (epoch, value['id'])
            if key in self.seen:
                return True
            if self.busy:
                return True
            self.seen.append(key)
            self.busy = True
            self.jobs.put_nowait((epoch, value))
        except (ValueError, TypeError, queue.Full):
            pass
        return True

    def _run(self):
        while not self.stopped.is_set():
            try:
                epoch, value = self.jobs.get(timeout=0.1)
            except queue.Empty:
                continue
            if epoch != self.epoch:
                self.pages.reset()
                self.epoch = epoch
            try:
                frames = self.pages.page(value)
            except PanelError as exc:
                frames = [encode({'id': value['id'], 'phase': 'error', 'message': label(str(exc))})]
            except OutcomeUnknown:
                frames = [encode({'id': value['id'], 'phase': 'error', 'message': '结果待确认，请刷新；不会重试'})]
            except (OSError, ValueError, KeyError, TypeError, CommandFailed):
                frames = [encode({'id': value['id'], 'phase': 'error', 'message': 'Clash 不可用，请在电脑检查'})]
            try:
                self.results.put_nowait((epoch, frames))
            except queue.Full:
                pass
            self.busy = False

    def take(self, epoch):
        frames = []
        while True:
            try:
                current, batch = self.results.get_nowait()
            except queue.Empty:
                return frames
            if current == epoch:
                frames.extend(batch)

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=0.2)
