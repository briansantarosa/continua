"""Resident-owned notes, projects, and version history (memory plan §6g chunk 7).

HER notebook: written only by her tool calls, versioned (never silently
overwritten), removal under her control (removed notes keep their history and
can be restored by writing the title again), verbatim survival across threads
and restarts, no age expiry, no machine-written content ever. Projects are
her declarations of state — never inferred.

File: notes/<instance>/notes.json (0600). Atomic writes; fsync.
"""
import json
import os
import threading
from datetime import datetime
from pathlib import Path

_LOCK = threading.Lock()


def _now():
    return datetime.now().isoformat(timespec='seconds')


def notes_path(root, instance):
    return Path(root) / 'notes' / instance / 'notes.json'


class NotesStore:
    def __init__(self, root, instance):
        self.path = notes_path(root, instance)
        self.instance = instance
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.path.exists():
            self._write({'notes': {}, 'projects': {}})
        os.chmod(self.path.parent, 0o700)
        os.chmod(self.path, 0o600)

    def _read(self):
        try:
            with open(self.path, encoding='utf-8') as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {'notes': {}, 'projects': {}}

    def _write(self, data):
        tmp = str(self.path) + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    # --- notes ------------------------------------------------------------
    def write_note(self, title, body, episode=None):
        """Create or revise a note under her control. Version history is kept;
        a previously removed note is restored (with its history intact).
        §5a: a note may carry an optional link to the episode that prompted
        it — stored verbatim, never inferred by machinery."""
        title = (title or '').strip()
        body = (body or '').strip()
        if not title or not body:
            raise ValueError('title and body are required')
        with _LOCK:
            data = self._read()
            note = data['notes'].get(title) or {
                'created': _now(), 'versions': [], 'removed': None}
            note['versions'].append({'body': body, 'ts': _now()})
            note['versions'] = note['versions'][-20:]  # bounded history
            note['body'] = body
            note['updated'] = _now()
            note['removed'] = None
            if episode:
                note['episode'] = str(episode)  # §5a: the prompting episode, hers to set
            data['notes'][title] = note
            self._write(data)
            return {'title': title, 'updated': note['updated'],
                    'versions_kept': len(note['versions'])}

    def read_note(self, title):
        """Verbatim body; removed notes report their removed state."""
        title = (title or '').strip()
        note = self._read()['notes'].get(title)
        if not note:
            return None
        out = {'title': title}
        out.update(note)
        out['removed'] = bool(note.get('removed'))
        return out

    def remove_note(self, title):
        """Her removal is immediate: the note leaves every rendered surface.
        History is preserved (append-only discipline) and restorable by
        writing the same title again."""
        title = (title or '').strip()
        with _LOCK:
            data = self._read()
            note = data['notes'].get(title)
            if not note or note.get('removed'):
                return False
            note['removed'] = _now()
            data['notes'][title] = note
            self._write(data)
            return True

    def list_notes(self, include_removed=False):
        """Active notes, newest-updated first. Verbatim bodies included —
        the caller renders within budget, never paraphrases."""
        data = self._read()
        out = []
        for title, note in data['notes'].items():
            if note.get('removed') and not include_removed:
                continue
            out.append({'title': title, 'body': note['body'],
                        'episode': note.get('episode'),
                        'created': note['created'], 'updated': note['updated'],
                        'removed': bool(note.get('removed'))})
        return sorted(out, key=lambda n: n['updated'], reverse=True)

    # --- projects -----------------------------------------------------------
    def set_project(self, title, status, note=None):
        """Her declaration of project state. Never inferred; never expires."""
        title = (title or '').strip()
        status = (status or '').strip()
        if not title or not status:
            raise ValueError('title and status are required')
        with _LOCK:
            data = self._read()
            proj = data['projects'].get(title) or {'created': _now()}
            proj.update({'status': status, 'note': (note or '').strip() or None,
                         'updated': _now()})
            data['projects'][title] = proj
            self._write(data)
            return {'title': title, 'status': status, 'updated': proj['updated']}

    def list_projects(self):
        data = self._read()
        return sorted(({'title': t, **p} for t, p in data['projects'].items()),
                      key=lambda p: p['updated'], reverse=True)
