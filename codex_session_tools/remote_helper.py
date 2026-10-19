"""Linux helper; sent through SSH stdin. Never deletes files or uses SIGKILL."""
import base64
from contextlib import closing
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import stat
import sys
import time

# This file is also distributed to Windows. Linux-only modules must not be
# imported by the GUI/client on Windows, and the helper itself fails explicitly.
if sys.platform == 'linux':
    import fcntl
    import pwd
else:
    fcntl = None
    pwd = None

UUID = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
UNLOCK_WAIT_SECONDS = 12
PROTOCOL_VERSION = 1
RESPONSE_PREFIX = 'CODEX_SESSION_JSON:'


class Refused(Exception):
    pass


class OutcomeUnknown(Exception):
    pass


def identity(pid):
    root = Path('/proc') / str(pid)
    try:
        raw = (root / 'stat').read_text()
        fields = raw[raw.rfind(')') + 2:].split()
        uid = root.stat().st_uid
        return {'pid': int(pid), 'uid': uid, 'start_ticks': int(fields[19]),
                'exe': os.readlink(root / 'exe'),
                'argv': [x.decode('utf-8', 'replace') for x in
                         (root / 'cmdline').read_bytes().split(b'\0') if x],
                'state': fields[0], 'ppid': int(fields[1])}
    except (OSError, ValueError, IndexError):
        return None


def process_key(info):
    if not info:
        return None
    return {k: info.get(k) for k in ('pid', 'uid', 'start_ticks', 'exe', 'argv')}


def is_codex_backend(info):
    if not info or info['uid'] != os.geteuid() or info['state'] == 'Z':
        return False
    executable = info['exe'].removesuffix(' (deleted)')
    args = info['argv']
    if Path(executable).name != 'codex' or 'app-server' not in args:
        return False
    index = args.index('app-server')
    # app-server proxy/client processes do not own the backend lifecycle.
    return index + 1 == len(args) or args[index + 1].startswith('-')


def inode_key(st):
    return [os.major(st.st_dev), os.minor(st.st_dev), st.st_ino]


def stat_key(st):
    return {'inode': inode_key(st), 'size': st.st_size, 'mtime_ns': st.st_mtime_ns}


def lock_record(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {'exists': False, 'held': False, 'inode': None}
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
            raise Refused('Lock is not a regular file owned by the SSH user.')
        held = False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EACCES):
                raise
            held = True
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
        if inode_key(os.stat(path, follow_symlinks=False)) != inode_key(st):
            raise Refused('Lock file changed during inspection. Run again.')
        return {'exists': True, 'held': held, 'inode': inode_key(st)}
    finally:
        os.close(fd)


def kernel_locks():
    entries = []
    for line in Path('/proc/locks').read_text().splitlines():
        fields = line.split()
        if len(fields) < 8 or '->' in fields:
            continue
        if fields[1:4] != ['FLOCK', 'ADVISORY', 'WRITE']:
            continue
        dev_major, dev_minor, ino = fields[5].split(':')
        entries.append({'pid': int(fields[4]),
                        'inode': [int(dev_major, 16), int(dev_minor, 16), int(ino)]})
    return entries


def latest_database(home):
    """Choose exactly one schema generation; never silently consult older data."""
    dbs = sorted((p for p in home.glob('state_*.sqlite') if re.fullmatch(r'state_\d+\.sqlite', p.name)), key=lambda p: int(
        re.search(r'state_(\d+)', p.name).group(1)), reverse=True)
    if not dbs:
        raise Refused('No Codex state database found in ' + str(home))
    db = dbs[0]
    st = db.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
        raise Refused('Latest state database must be a regular file owned by the SSH user.')
    return db


def db_metadata(home, thread_ids, database=None):
    found = {}
    db = database or latest_database(home)
    with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=3)) as conn:
        conn.execute('PRAGMA query_only=ON')
        cols = {row[1] for row in conn.execute('PRAGMA table_info(threads)')}
        if not {'id', 'title', 'rollout_path'} <= cols:
            raise Refused('Unsupported latest state database schema: threads(id, title, rollout_path) required.')
        for tid in thread_ids:
            rows = conn.execute('SELECT id,title,rollout_path FROM threads WHERE id=? LIMIT 2', (tid,)).fetchall()
            if len(rows) > 1:
                raise Refused('Duplicate thread metadata: ' + tid)
            if rows:
                row = rows[0]
                if row[2] is not None and not isinstance(row[2], str):
                    raise Refused('Invalid rollout_path metadata for ' + tid)
                found[tid] = {'id': row[0], 'title': row[1] or '(untitled)',
                              'rollout_path': row[2]}
    return found


def history_state(meta, home, include_hash=False):
    result = dict(meta)
    result.update({'lifecycle': 'unknown', 'active': False, 'file': None,
                   'last_event': None, 'last_time': None, 'sha256': None})
    if not meta.get('rollout_path'):
        return result
    path = Path(meta['rollout_path'])
    if not path.is_absolute():
        path = home / path
    path = path.resolve()
    # Do not read arbitrary files referenced by a damaged database.
    if not path.is_relative_to(home) or path.suffix != '.jsonl':
        raise Refused('Unexpected rollout path for ' + meta['id'])
    if not path.exists():
        return result
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid():
            raise Refused('History must be a regular file owned by the SSH user.')
        result['rollout_path'] = str(path)
        result['file'] = stat_key(before)
        # Read a bounded tail; absent or damaged lifecycle evidence is unknown.
        start = max(0, before.st_size - 4 * 1024 * 1024)
        stream.seek(start)
        if start:
            stream.readline()
        events = stream.read().splitlines()
        for line in reversed(events):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except (ValueError, UnicodeError):
                break
            if not isinstance(item, dict) or not isinstance(item.get('type'), str):
                break
            payload = item.get('payload')
            if not isinstance(payload, dict):
                break
            if item['type'] != 'event_msg':
                continue
            if not isinstance(payload.get('type'), str):
                break
            if payload['type'] in ('task_started', 'task_complete', 'turn_aborted'):
                kind = payload['type']
                result['last_event'] = kind
                result['last_time'] = item.get('timestamp')
                result['active'] = kind == 'task_started'
                result['lifecycle'] = 'active' if result['active'] else 'completed'
                break
        if include_hash:
            digest = hashlib.sha256()
            stream.seek(0)
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
            result['sha256'] = digest.hexdigest()
        if (stat_key(os.fstat(stream.fileno())) != stat_key(before)
                or stat_key(path.lstat()) != stat_key(before)):
            raise Refused('History changed while reading ' + meta['id'] + '. Wait and run again.')
    return result


def snapshot(home, tid):
    home = Path(home).expanduser().resolve()
    if not isinstance(tid, str) or not UUID.fullmatch(tid):
        raise Refused('Invalid thread_id.')
    tid = tid.lower()
    target_lock = home / 'thread-writer-locks' / (tid + '.lock')
    lock = lock_record(target_lock)
    database = latest_database(home)
    database_inode = inode_key(database.stat())
    metadata = db_metadata(home, [tid], database)
    if tid not in metadata:
        raise Refused('Thread ID not found in this server/account: ' + tid)
    target = history_state(metadata[tid], home, include_hash=True)
    result = {'server_hostname': os.uname().nodename, 'user': pwd.getpwuid(os.geteuid()).pw_name,
              'uid': os.geteuid(), 'codex_home': str(home), 'thread_id': tid,
              'database': str(database), 'database_inode': database_inode,
              'target': target, 'lock': lock, 'owner': None, 'affected': [],
              'blockers': [], 'warnings': [], 'ready': False}
    if not lock['held']:
        result['status'] = 'unlocked'
        return result
    entries = kernel_locks()
    owners = {entry['pid'] for entry in entries if entry['inode'] == lock['inode']}
    if len(owners) != 1 or next(iter(owners)) <= 0:
        result['blockers'].append('Cannot identify a unique live kernel lock owner.')
        result['status'] = 'blocked'
        return result
    pid = next(iter(owners))
    owner = identity(pid)
    result['owner'] = owner or {'pid': pid}
    if not is_codex_backend(owner):
        result['blockers'].append('Owner is gone, zombie, another user, or not a Codex app-server backend.')
    owned_keys = {tuple(entry['inode']) for entry in entries if entry['pid'] == pid}
    affected_ids = []
    for path in (home / 'thread-writer-locks').glob('*.lock'):
        if not UUID.fullmatch(path.stem):
            continue
        try:
            if tuple(inode_key(path.lstat())) in owned_keys:
                affected_ids.append(path.stem)
        except FileNotFoundError:
            continue
    if tid not in affected_ids:
        raise Refused('Lock changed during inspection. Run again.')
    all_meta = db_metadata(home, affected_ids, database)
    for item_id in sorted(affected_ids):
        entry = target if item_id == tid else history_state(
            all_meta.get(item_id, {'id': item_id, 'title': '(metadata unavailable)', 'rollout_path': None}), home)
        result['affected'].append(entry)
        if entry['active']:
            result['blockers'].append('Unfinished task detected: ' + item_id)
        elif entry['lifecycle'] == 'unknown':
            result['blockers'].append('Task activity unknown; termination is blocked: ' + item_id)
    if not target['sha256']:
        result['blockers'].append('Cannot hash the target history file; refusing to terminate automatically.')
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        result['blockers'].append('Python/Linux lacks pidfd support needed for safe process termination.')
    if latest_database(home) != database or inode_key(database.stat()) != database_inode:
        raise Refused('State database changed during inspection. Run again.')
    # Execution will repeat the entire snapshot after confirmation.
    result['ready'] = not result['blockers']
    result['status'] = 'locked' if result['ready'] else 'blocked'
    result['fingerprint'] = fingerprint(result)
    return result


def fingerprint(snap):
    data = {'server_hostname': snap['server_hostname'], 'uid': snap['uid'],
            'codex_home': snap['codex_home'], 'database': snap['database'],
            'database_inode': snap['database_inode'],
            'thread_id': snap['thread_id'], 'lock': snap['lock'],
            'owner': process_key(snap['owner']),
            'affected': [{k: item.get(k) for k in
                          ('id', 'rollout_path', 'file', 'last_event', 'last_time')}
                         for item in snap['affected']],
            'target_sha256': snap['target']['sha256']}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def unlock(request):
    expected = request['expected']
    if (not isinstance(expected, dict) or not isinstance(expected.get('codex_home'), str)
            or not isinstance(expected.get('thread_id'), str)
            or not isinstance(expected.get('fingerprint'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', expected['fingerprint'])):
        raise Refused('A valid inspection fingerprint is required.')
    current = snapshot(expected['codex_home'], expected['thread_id'])
    if current['status'] == 'unlocked':
        return {'status': 'already_unlocked', 'message': 'Lock is already available. No process was signalled.'}
    if not current['ready']:
        raise Refused('Current state does not permit termination: ' + '; '.join(current['blockers']))
    if current['fingerprint'] != expected['fingerprint']:
        raise Refused('Owner, affected tasks, or history changed after confirmation. Nothing was terminated; run again.')
    owner = current['owner']
    pid = owner['pid']
    pidfd = os.pidfd_open(pid, 0)
    try:
        if process_key(identity(pid)) != process_key(owner):
            raise Refused('Process identity changed. Nothing was terminated.')
        # Recheck the lock, all affected history file stats and lock set immediately before SIGTERM.
        path = Path(current['codex_home']) / 'thread-writer-locks' / (current['thread_id'] + '.lock')
        if lock_record(path) != current['lock']:
            raise Refused('Lock changed. Nothing was terminated.')
        current_database = latest_database(Path(current['codex_home']))
        if (str(current_database) != current['database']
                or inode_key(current_database.stat()) != current['database_inode']):
            raise Refused('State database changed. Nothing was terminated.')
        for item in current['affected']:
            if item.get('file') and stat_key(Path(item['rollout_path']).lstat()) != item['file']:
                raise Refused('An affected history changed. Nothing was terminated; run again.')
        keys = {tuple(entry['inode']) for entry in kernel_locks() if entry['pid'] == pid}
        known = set()
        for item in current['affected']:
            item_path = path.parent / (item['id'] + '.lock')
            known.add(tuple(inode_key(item_path.lstat())))
        # The backend may also own a coordination lock. Only compare UUID writer locks.
        actual = set()
        for item_path in path.parent.glob('*.lock'):
            if UUID.fullmatch(item_path.stem):
                key = tuple(inode_key(item_path.lstat()))
                if key in keys:
                    actual.add(key)
        if actual != known or not known <= keys:
            raise Refused('The set of affected tasks changed. Nothing was terminated.')
        signal.pidfd_send_signal(pidfd, signal.SIGTERM, None, 0)
    finally:
        try:
            os.close(pidfd)
        except OSError:
            # A descriptor cleanup failure must not misreport a sent signal as
            # a refused action. Verification below still determines the result.
            pass
    try:
        return verify_after_signal(current, path, pid)
    except Exception as exc:
        return {'status': 'verification_error', 'signalled_pid': pid, 'signal': 'SIGTERM',
                'outcome_unknown': True,
                'message': 'SIGTERM was sent, but the result could not be verified: ' + str(exc)}


def verify_after_signal(current, path, pid):
    # Never signal parents, descendants or a newly appearing owner.
    deadline = time.monotonic() + UNLOCK_WAIT_SECONDS
    after_lock = lock_record(path)
    while after_lock['held'] and time.monotonic() < deadline:
        if after_lock['inode'] != current['lock']['inode']:
            raise Refused('Lock pathname now refers to a different inode.')
        time.sleep(0.4)
        after_lock = lock_record(path)
    if after_lock['inode'] != current['lock']['inode']:
        raise Refused('Original lock file is missing or replaced; release of its original inode is unverified.')
    after_meta = db_metadata(Path(current['codex_home']), [current['thread_id']])
    after = history_state(after_meta[current['thread_id']], Path(current['codex_home']), include_hash=True)
    same_history = (after['sha256'] == current['target']['sha256'] and
                    after['rollout_path'] == current['target']['rollout_path'])
    # Hashing can take time. Observe the lock again so a reacquisition during
    # verification is reported, without ever signalling the new owner.
    after_lock = lock_record(path)
    if after_lock['inode'] != current['lock']['inode']:
        raise Refused('Original lock file is missing or replaced after history verification.')
    new_owners = [entry['pid'] for entry in kernel_locks() if entry['inode'] == after_lock['inode']]
    status = 'unlocked' if not after_lock['held'] else 'still_locked'
    return {'status': status, 'signalled_pid': pid, 'signal': 'SIGTERM',
            'history_unchanged': same_history,
            'history_sha256_before': current['target']['sha256'],
            'history_sha256_after': after['sha256'],
            'lock': after_lock, 'current_lock_pids': new_owners,
            'message': ('Lock was available at the end of this check.' if not after_lock['held'] else
                        'Lock remains held or was reacquired. No other process was terminated.')}


def main(request):
    if sys.platform != 'linux':
        raise Refused('The remote helper requires Linux; use the client on Windows.')
    if sys.version_info < (3, 9):
        raise Refused('The remote helper requires Python 3.9 or newer.')
    if not isinstance(request, dict):
        raise Refused('Request must be a JSON object.')
    if request.get('action') == 'inspect':
        home = request.get('codex_home') or os.environ.get('CODEX_HOME') or str(Path.home() / '.codex')
        return snapshot(home, request['thread_id'])
    if request.get('action') == 'unlock' and request.get('confirmed') is True:
        return unlock(request)
    raise Refused('An explicit confirmed unlock request is required.')


def envelope(request):
    """Return one versioned protocol record; no default logs or history content."""
    try:
        result = main(request)
        return {'version': PROTOCOL_VERSION, 'ok': True, 'result': result}
    except Exception as exc:
        return {'version': PROTOCOL_VERSION, 'ok': False,
                'error': {'message': str(exc), 'error_type': type(exc).__name__,
                          'outcome_unknown': isinstance(exc, OutcomeUnknown)}}


if __name__ == '__main__':
    try:
        request = json.loads(base64.b64decode(REQUEST_B64, validate=True).decode('utf-8'))
        response = envelope(request)
    except Exception as exc:
        response = {'version': PROTOCOL_VERSION, 'ok': False,
                    'error': {'message': str(exc), 'error_type': type(exc).__name__, 'outcome_unknown': False}}
    print(RESPONSE_PREFIX + json.dumps(response, ensure_ascii=True), flush=True)
