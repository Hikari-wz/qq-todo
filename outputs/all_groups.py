"""All-group membership, conservative association and resumable local history."""
import json
import time
from collections import deque
from group_filters import MUTED_GROUPS, muted_card
from trial import read, save


def register_group(state, gid, name='', start=None):
    start = max(state['enabled_at'], int(start if start is not None else time.time()))
    progress = state.setdefault('groups', {}).setdefault(str(gid), {
        'covered_until': start, 'since': start, 'possibly_incomplete': False, 'active': True})
    if isinstance(name, str) and name:
        progress['name'] = name
    return progress


def related_cards(state, message):
    """Cross-group candidates require identical full notice text, not title or portal URL."""
    current = message['text'].strip()
    result = {}
    for key, card in state['cards'].items():
        if card.get('local_share') or muted_card(card):
            continue
        for evidence in card.get('evidence', []):
            source = state['processed'].get(evidence['id'], {}).get('message', {})
            old = source.get('text', '').strip()
            if (source.get('group') == message['group'] or
                (len(current) >= 20 and current == old)):
                result[key] = card
                break
    return result


class Catchup:
    """One outstanding history request; enqueue oldest first and persist coverage."""
    def __init__(self, pilot, send, account):
        self.pilot, self.send, self.account = pilot, send, account
        self.queue = deque()
        self.active = None
        self.serial = 0

    def groups(self, items, reconnect=True):
        now = int(time.time())
        with self.pilot.lock:
            state = read(self.pilot.path)
            first = not state.get('group_list_ready')
            old = set(state.get('groups', {}))
            ids = set()
            for item in items:
                gid = str(item.get('group_id', ''))
                if not gid.isdigit() or int(gid) <= 0 or gid in MUTED_GROUPS:
                    continue
                ids.add(gid)
                progress = register_group(state, gid, item.get('group_name', ''), state['enabled_at'] if first else now)
                progress['active'] = True
                if reconnect or gid not in old or progress.get("possibly_incomplete"):
                    self.queue.append((gid, progress['covered_until']))
            for gid, progress in state.get('groups', {}).items():
                progress['active'] = gid in ids
            state.update(group_list_ready=True, group_list_checked_at=now, group_count=len(ids))
            save(self.pilot.path, state)
        self.next()

    def next(self):
        if self.active:
            return
        if not self.queue:
            self.pilot.syncing = False
            return
        gid, since = self.queue.popleft()
        self.pilot.syncing = True
        self.active = dict(gid=gid, since=since, started=int(time.time()), messages={}, pages=0, cursor='0', cursors=set())
        self.request()

    def request(self):
        self.serial += 1
        self.active['echo'] = f'all-history-{self.serial}'
        self.active['sent'] = time.monotonic()
        self.send({'action':'get_group_msg_history', 'echo':self.active['echo'],
                   'params':{'group_id':self.active['gid'], 'count':100,
                             'message_seq':self.active['cursor'], 'disable_get_url':True}})

    def response(self, event):
        a = self.active
        if not a or event.get('echo') != a['echo']:
            return False
        messages = (event.get('data') or {}).get('messages')
        if event.get('status') != 'ok' or not isinstance(messages, list):
            self.finish(False, 'history_request_failed')
            return True
        a['pages'] += 1
        valid = [m for m in messages if isinstance(m, dict) and type(m.get('time')) is int
                 and str(m.get('group_id')) == a['gid']]
        for m in valid:
            a['messages'][str(m.get('message_id'))] = m
        earliest = min((m['time'] for m in valid), default=None)
        # A full page whose earliest timestamp is inside the gap requires another page.
        if len(valid) != len(messages):
            self.finish(False, 'invalid_history_structure')
        elif len(messages) < 100 or (earliest is not None and earliest <= a['since']):
            self.finish(True)
        else:
            oldest = min(valid, key=lambda m:m['time']) if valid else {}
            cursor = str(oldest.get('message_seq') or oldest.get('real_seq') or '')
            if not cursor or cursor in a['cursors'] or a['pages'] >= 20:
                self.finish(False, 'history_window_not_covered')
            else:
                a['cursors'].add(cursor)
                a['cursor'] = cursor
                self.request()
        return True

    def finish(self, complete, reason=''):
        a = self.active
        eligible = sorted((m for m in a['messages'].values() if m['time'] >= a['since']), key=lambda m:m['time'])
        for m in eligible:
            if str(m.get('user_id')) != str(self.account):
                self.pilot.receive({**m, 'self_id':self.account, 'post_type':'message', 'message_type':'group'})
        with self.pilot.lock:
            state = read(self.pilot.path)
            progress = state['groups'][a['gid']]
            progress.update(checked_at=int(time.time()), since=a['since'], returned=len(a['messages']),
                            pages=a['pages'], possibly_incomplete=not complete, error=reason,
                            earliest=min((m['time'] for m in a['messages'].values()), default=None))
            if complete:
                progress['covered_until'] = max(progress['covered_until'], a['started'])
            save(self.pilot.path, state)
        self.active = None
        self.next()

    def checkpoint(self):
        if self.active or self.queue:
            return
        now = int(time.time())
        with self.pilot.lock:
            state = read(self.pilot.path)
            for progress in state.get('groups', {}).values():
                if progress.get('active') and not progress.get('possibly_incomplete'):
                    progress['covered_until'] = max(progress['covered_until'], now)
            state['last_connected_at'] = now
            save(self.pilot.path, state)


def listen_all(pilot, port, token):
    import websocket
    while not pilot.stop.is_set():
        connection = None
        pilot.syncing = True
        try:
            headers = {'Authorization':f'Bearer {token}'} if token else {}
            connection = websocket.create_connection(f'ws://127.0.0.1:{port}', header=headers, timeout=5)
            def send(payload):
                connection.send(json.dumps(payload))
            send({'action':'get_login_info','params':{},'echo':'all-login'})
            login_deadline = time.monotonic()+30
            catchup = None
            list_requested = 0
            refreshed = False
            last_checkpoint = time.monotonic()
            print('NapCat 已连接，全部群模式。', flush=True)
            while not pilot.stop.is_set():
                now = time.monotonic()
                if catchup is None and now > login_deadline:
                    raise OSError('login info timeout')
                if catchup and catchup.active and now - catchup.active['sent'] > 30:
                    catchup.finish(False, 'history_timeout')
                if catchup and not list_requested and (not refreshed or now-last_list > 300):
                    send({'action':'get_group_list','params':{},'echo':'all-groups'})
                    list_requested = now
                if list_requested and now-list_requested > 30:
                    raise OSError('group list timeout')
                try:
                    frame = connection.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not frame:
                    break
                try:
                    event = json.loads(frame)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get('echo') == 'all-login':
                    account = (event.get('data') or {}).get('user_id')
                    if event.get('status') != 'ok' or not str(account).isdigit():
                        raise OSError('login info failed')
                    catchup = Catchup(pilot, send, account)
                elif event.get('echo') == 'all-groups':
                    if event.get('status') != 'ok' or not isinstance(event.get('data'), list):
                        raise OSError('group list failed')
                    catchup.groups(event['data'], reconnect=not refreshed)
                    refreshed, last_list, list_requested = True, time.monotonic(), 0
                    print(f'群列表已更新：{len(event["data"])} 个群。', flush=True)
                elif catchup and catchup.response(event):
                    pass
                elif not event.get('echo'):
                    pilot.receive(event)
                    if (catchup and refreshed and time.monotonic()-last_checkpoint >= 30
                        and event.get('post_type') == 'meta_event' and event.get('meta_event_type') == 'heartbeat'):
                        catchup.checkpoint()
                        last_checkpoint = time.monotonic()
        except (OSError, ValueError, websocket.WebSocketException) as error:
            print(f'全部群连接暂不可用（{type(error).__name__}），5 秒后重试。', flush=True)
        finally:
            pilot.syncing = False
            if connection is not None:
                connection.close()
        pilot.stop.wait(5)
