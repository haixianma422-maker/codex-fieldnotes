"""Derived pending pointers; callers hold the existing journal lock.

Event files and ACKs remain authoritative. Write intent before an event,
ACK before removing intent. Interrupted writes leave harmless pointers.
"""
from journal_core import guarded, now, put_json, read_json, safe_id


def _root(reg):
    return guarded(reg.base, 'state/pending-events')


def _pointer(reg, pid, eid):
    reg.project(pid)
    return guarded(_root(reg), f'{pid}/{safe_id(eid)}.json')


def rebuild_pending(reg, pid=None):
    root = _root(reg)
    count = scanned = 0
    projects = [pid] if pid is not None else reg.projects
    for project in projects:
        reg.project(project)
        folder = guarded(root, project)
        ready = guarded(root, f'{project}.ready.json')
        if ready.exists():
            ready.unlink()
        for pointer in folder.glob('*.json'):
            _discard(guarded(folder, pointer.name))
        for path in guarded(reg.base, f'events/{project}').glob('*.json'):
            scanned += 1
            if guarded(reg.base, f'state/ack/{safe_id(path.stem)}.json').exists():
                continue
            event = read_json(path)
            if event.get('project_id') != project or event.get('event_id') != path.stem:
                raise ValueError('Event identity mismatch during pending rebuild')
            put_json(_pointer(reg, project, path.stem), {})
            count += 1
        put_json(ready, {'version': 1, 'rebuilt_at': now()})
    return {'pending': count, 'scanned': scanned}


def _ensure(reg, pid):
    try:
        ready = read_json(guarded(_root(reg), f'{pid}.ready.json'))
        if isinstance(ready, dict) and ready.get('version') == 1:
            return
    except (OSError, ValueError):
        pass
    rebuild_pending(reg, pid)


def _discard(pointer):
    # Completed ACKs remain authoritative even when cache cleanup is unavailable.
    try:
        pointer.unlink(missing_ok=True)
    except OSError:
        pass


def pending_events(reg, pid=None):
    projects = [pid] if pid is not None else reg.projects
    pending = []
    for project in projects:
        reg.project(project)
        _ensure(reg, project)
        for pointer in guarded(_root(reg), project).glob('*.json'):
            eid = safe_id(pointer.stem)
            path = guarded(reg.base, f'events/{project}/{eid}.json')
            if guarded(reg.base, f'state/ack/{eid}.json').exists() or not path.exists():
                _discard(guarded(_root(reg), f'{project}/{pointer.name}'))
                continue
            event = read_json(path)
            if event.get('project_id') != project or event.get('event_id') != eid:
                raise ValueError('Pending event identity mismatch')
            pending.append(event)
    return pending


def write_event(reg, event):
    pid, eid = event['project_id'], safe_id(event['event_id'])
    reg.project(pid)
    # Never let damaged old history prevent collection of new evidence.
    # An absent ready marker is rebuilt by the next reader, not this writer.
    put_json(_pointer(reg, pid, eid), {})
    path = guarded(reg.base, f'events/{pid}/{eid}.json')
    put_json(path, event)
    return path


def acknowledge(reg, pid, eid, value):
    pointer = _pointer(reg, pid, eid)
    put_json(guarded(reg.base, f'state/ack/{safe_id(eid)}.json'), value)
    _discard(pointer)
