"""Understands the floobits protocol"""

import os
import hashlib
import collections
import Queue
import stat
import base64
from functools import wraps

from common import ignore, msg, shared as G, utils
from common.lib import DMP

import sublime

MAX_FILE_SIZE = 1024 * 1024 * 5


def buf_populated(f):
    @wraps(f)
    def wrapped(self, data):
        if data.get('id') is None:
            msg.debug('no buf id in data')
            return
        buf = self.FLOO_BUFS.get(data['id'])
        if buf is None or 'buf' not in buf:
            msg.debug('buf is not populated yet')
            return
        f(self, data)
    return wrapped


class BaseProtocol(object):
    BUFS_CHANGED = []
    SELECTION_CHANGED = []
    MODIFIED_EVENTS = Queue.Queue()
    SELECTED_EVENTS = Queue.Queue()
    FLOO_BUFS = {}

    def __init__(self, agent):
        self.agent = agent
        self.perms = []
        self.read_only = True
        self.follow_mode = False
        self.chat_deck = collections.deque(maxlen=10)
        self.ignored_names = ['node_modules']

    def get_view(self, data):
        raise NotImplemented()

    def update_view(self, data):
        raise NotImplemented()

    def get_buf(self, data):
        raise NotImplemented()

    def get_buf_by_path(self, path):
        rel_path = utils.to_rel_path(path)
        for buf_id, buf in self.FLOO_BUFS.iteritems():
            if rel_path == buf['path']:
                return buf
        return None

    def save_buf(self, data):
        raise NotImplemented()

    def chat(self, data):
        raise NotImplemented()

    def maybe_buffer_changed(self):
        raise NotImplemented()

    def maybe_selection_changed(self):
        raise NotImplemented()

    def on_msg(self, data):
        raise NotImplemented()

    def follow(self, follow_mode=None):
        if follow_mode is None:
            follow_mode = not self.follow_mode
        self.follow_mode = follow_mode
        msg.log('follow mode is %s' % {True: 'enabled', False: 'disabled'}[self.follow_mode])

    def create_buf(self, path, ig=None, force=False):
        if G.SPARSE_MODE and not force:
            msg.debug("Skipping %s because user enabled sparse mode." % path)
            return
        if not utils.is_shared(path):
            msg.error('Skipping adding %s because it is not in shared path %s.' % (path, G.PROJECT_PATH))
            return
        if not ig:
            ig = ignore.Ignore(None, path)
        ignores = collections.deque([ig])
        files = collections.deque()
        self._create_buf_worker(ignores, files, [])

    def _create_buf_worker(self, ignores, files, too_big):
        quota = 10

        # scan until we find a minimum of 10 files
        while quota > 0 and ignores:
            ig = ignores.popleft()
            for new_path in self._scan_dir(ig):
                if not new_path:
                    continue
                try:
                    s = os.lstat(new_path)
                except Exception as e:
                    msg.error('Error lstat()ing path %s: %s' % (new_path, unicode(e)))
                    continue
                if stat.S_ISDIR(s.st_mode):
                    ignores.append(ignore.Ignore(ig, new_path))
                elif stat.S_ISREG(s.st_mode):
                    if s.st_size > (MAX_FILE_SIZE):
                        too_big.append(new_path)
                    else:
                        files.append(new_path)
                    quota -= 1

        can_upload = False
        for f in utils.iter_n_deque(files, 10):
            self.upload(f)
            can_upload = True

        if can_upload:
            self.agent.select()

        if ignores or files:
            return utils.set_timeout(self._create_buf_worker, 25, ignores, files, too_big)

        if too_big:
            sublime.error_message("%s file(s) were not added because they were larger than 10 megabytes: \n%s" % (len(too_big), "\t".join(too_big)))

        msg.log('All done syncing')

    def _scan_dir(self, ig):
        path = ig.path

        if not utils.is_shared(path):
            msg.error('Skipping adding %s because it is not in shared path %s.' % (path, G.PROJECT_PATH))
            return
        if os.path.islink(path):
            msg.error('Skipping adding %s because it is a symlink.' % path)
            return
        ignored = ig.is_ignored(path)
        if ignored:
            msg.log('Not creating buf: %s' % (ignored))
            return

        msg.debug('create_buf: path is %s' % path)

        if not os.path.isdir(path):
            yield path
            return

        try:
            paths = os.listdir(path)
        except Exception as e:
            msg.error('Error listing path %s: %s' % (path, unicode(e)))
            return
        for p in paths:
            p_path = os.path.join(path, p)
            if p[0] == '.':
                if p not in ignore.HIDDEN_WHITELIST:
                    msg.log('Not creating buf for hidden path %s' % p_path)
                    continue
            ignored = ig.is_ignored(p_path)
            if ignored:
                msg.log('Not creating buf: %s' % (ignored))
                continue

            yield p_path

    def upload(self, path):
        try:
            with open(path, 'rb') as buf_fd:
                buf = buf_fd.read()
            encoding = 'utf8'
            rel_path = utils.to_rel_path(path)
            existing_buf = self.get_buf_by_path(path)
            if existing_buf:
                buf_md5 = hashlib.md5(buf).hexdigest()
                if existing_buf['md5'] == buf_md5:
                    msg.debug('%s already exists and has the same md5. Skipping.' % path)
                    return
                msg.log('setting buffer ', rel_path)

                existing_buf['buf'] = buf
                existing_buf['md5'] = buf_md5

                try:
                    buf = buf.decode('utf-8')
                except Exception:
                    buf = base64.b64encode(buf).decode('utf-8')
                    encoding = 'base64'

                existing_buf['encoding'] = encoding

                self.agent.put({
                    'name': 'set_buf',
                    'id': existing_buf['id'],
                    'buf': buf,
                    'md5': buf_md5,
                    'encoding': encoding,
                })
                return

            try:
                buf = buf.decode('utf-8')
            except Exception:
                buf = base64.b64encode(buf).decode('utf-8')
                encoding = 'base64'

            msg.log('creating buffer ', rel_path)
            event = {
                'name': 'create_buf',
                'buf': buf,
                'path': rel_path,
                'encoding': encoding,
            }
            self.agent.put(event)
        except (IOError, OSError):
            msg.error('Failed to open %s.' % path)
        except Exception as e:
            msg.error('Failed to create buffer %s: %s' % (path, unicode(e)))

    def handle(self, data):
        name = data.get('name')
        if not name:
            return msg.error('no name in data?!?')
        func = getattr(self, "on_%s" % (name), None)
        if not func:
            return msg.debug('unknown name', name, 'data:', data)
        func(data)

    def push(self):
        reported = set()
        while self.BUFS_CHANGED:
            buf_id = self.BUFS_CHANGED.pop()
            view = self.get_view(buf_id)
            buf = view.buf
            if view.is_loading():
                msg.debug('View for buf %s is not ready. Ignoring change event' % buf['id'])
                continue
            if self.read_only:
                continue
            vb_id = view.native_id
            if vb_id in reported:
                continue
            if 'buf' not in buf:
                msg.debug('No data for buf %s %s yet. Skipping sending patch' % (buf['id'], buf['path']))
                continue

            reported.add(vb_id)
            patch = utils.FlooPatch(view.get_text(), buf)
            # Update the current copy of the buffer
            buf['buf'] = patch.current
            buf['md5'] = hashlib.md5(patch.current.encode('utf-8')).hexdigest()
            self.agent.put(patch.to_json())

        while self.SELECTION_CHANGED:
            view, ping = self.SELECTION_CHANGED.pop()
            # consume highlight events to avoid leak
            if 'highlight' not in self.perms:
                continue
            vb_id = view.native_id
            if vb_id in reported:
                continue

            reported.add(vb_id)
            highlight_json = {
                'id': view.buf['id'],
                'name': 'highlight',
                'ranges': view.get_selections(),
                'ping': ping,
            }
            self.agent.put(highlight_json)

    def on_create_buf(self, data):
        self.on_get_buf(data)

    def on_get_buf(self, data):
        buf_id = data['id']
        self.FLOO_BUFS[buf_id] = data
        view = self.get_view(buf_id)
        if view:
            self.update_view(data, view)
        else:
            self.save_buf(data)

    def on_rename_buf(self, data):
        new = utils.get_full_path(data['path'])
        old = utils.get_full_path(data['old_path'])
        new_dir = os.path.dirname(new)
        if new_dir:
            utils.mkdir(new_dir)
        view = self.get_view(data['id'])
        self.FLOO_BUFS[data['id']]['path'] = data['path']
        if view:
            view.rename(new)
        else:
            os.rename(old, new)

    def on_room_info(self, data):
        # Success! Reset counter
        self.workspace_info = data
        self.perms = data['perms']

        if 'patch' in data['perms']:
            self.read_only = False
        else:
            msg.log('We don\'t have patch permission. Buffers will be read-only')

        utils.mkdir(G.PROJECT_PATH)
        floo_json = {
            'url': utils.to_workspace_url({
                'host': self.agent.host,
                'owner': self.agent.owner,
                'port': self.agent.port,
                'workspace': self.agent.workspace,
                'secure': self.agent.secure,
            })
        }
        utils.update_floo_file(os.path.join(G.PROJECT_PATH, '.floo'), floo_json)

        for buf_id, buf in data['bufs'].iteritems():
            buf_id = int(buf_id)  # json keys must be strings
            buf_path = utils.get_full_path(buf['path'])
            new_dir = os.path.dirname(buf_path)
            utils.mkdir(new_dir)
            self.FLOO_BUFS[buf_id] = buf
            try:
                text = open(buf_path, 'r').read()
                md5 = hashlib.md5(text).hexdigest()
                if md5 == buf['md5']:
                    msg.debug('md5 sums match. not getting buffer')
                    if buf['encoding'] == 'utf8':
                        text = text.decode('utf-8')
                    buf['buf'] = text
                elif self.agent.get_bufs:
                    self.agent.send_get_buf(buf_id)
            except Exception:
                self.agent.send_get_buf(buf_id)

        temp_data = data.get('temp_data', {})
        hangout = temp_data.get('hangout', {})
        hangout_url = hangout.get('url')
        if hangout_url:
            # self.prompt_join_hangout(hangout_url)
            pass

        msg.debug(G.PROJECT_PATH)
        self.agent.on_auth()

    def on_join(self, data):
        msg.log('%s joined the workspace' % data['username'])

    def on_part(self, data):
        msg.log('%s left the workspace' % data['username'])
        region_key = 'floobits-highlight-%s' % (data['user_id'])
        for window in sublime.windows():
            for view in window.views():
                view.erase_regions(region_key)

    @buf_populated
    def on_patch(self, data):
        added_newline = False
        buf_id = data['id']
        buf = self.FLOO_BUFS[buf_id]
        view = self.get_view(buf_id)
        if len(data['patch']) == 0:
            msg.error('wtf? no patches to apply. server is being stupid')
            return
        dmp_patches = DMP.patch_fromText(data['patch'])
        # TODO: run this in a separate thread
        if view:
            old_text = view.get_text()
        else:
            old_text = buf.get('buf', '')
        md5_before = hashlib.md5(old_text.encode('utf-8')).hexdigest()
        if md5_before != data['md5_before']:
            msg.debug('maybe vim is lame and discarded a trailing newline')
            old_text += '\n'
            added_newline = True
        md5_before = hashlib.md5(old_text.encode('utf-8')).hexdigest()
        if md5_before != data['md5_before']:
            msg.warn('starting md5s don\'t match for %s. ours: %s patch: %s this is dangerous!' %
                    (buf['path'], md5_before, data['md5_before']))
            if added_newline:
                old_text = old_text[:-1]
                md5_before = hashlib.md5(old_text.encode('utf-8')).hexdigest()

        t = DMP.patch_apply(dmp_patches, old_text)

        clean_patch = True
        for applied_patch in t[1]:
            if not applied_patch:
                clean_patch = False
                break

        if G.DEBUG:
            if len(t[0]) == 0:
                msg.debug('OMG EMPTY!')
                msg.debug('Starting data:', buf['buf'])
                msg.debug('Patch:', data['patch'])
            if '\x01' in t[0]:
                msg.debug('FOUND CRAZY BYTE IN BUFFER')
                msg.debug('Starting data:', buf['buf'])
                msg.debug('Patch:', data['patch'])

        if not clean_patch:
            msg.error('failed to patch %s cleanly. re-fetching buffer' % buf['path'])
            return self.agent.send_get_buf(buf_id)

        cur_hash = hashlib.md5(t[0].encode('utf-8')).hexdigest()
        if cur_hash != data['md5_after']:
            msg.warn(
                '%s new hash %s != expected %s. re-fetching buffer...' %
                (buf['path'], cur_hash, data['md5_after'])
            )
            return self.agent.send_get_buf(buf_id)

        buf['buf'] = t[0]
        buf['md5'] = cur_hash

        if not view:
            self.save_buf(buf)
            return
        view.apply_patches(buf, t)

    def delete_buf(self, path):
        """deletes a path"""

        if not path:
            return

        path = utils.get_full_path(path)

        if not utils.is_shared(path):
            msg.error('Skipping deleting %s because it is not in shared path %s.' % (path, G.PROJECT_PATH))
            return

        if os.path.isdir(path):
            for dirpath, dirnames, filenames in os.walk(path):
                # Don't care about hidden stuff
                dirnames[:] = [d for d in dirnames if d[0] != '.']
                for f in filenames:
                    f_path = os.path.join(dirpath, f)
                    if f[0] == '.':
                        msg.log('Not deleting buf for hidden file %s' % f_path)
                    else:
                        self.delete_buf(f_path)
            return
        buf_to_delete = None
        rel_path = utils.to_rel_path(path)
        for buf_id, buf in self.FLOO_BUFS.items():
            if rel_path == buf['path']:
                buf_to_delete = buf
                break
        if buf_to_delete is None:
            msg.error('%s is not in this workspace' % path)
            return
        msg.log('deleting buffer ', rel_path)
        event = {
            'name': 'delete_buf',
            'id': buf_to_delete['id'],
        }
        self.agent.put(event)

    @buf_populated
    def on_delete_buf(self, data):
        # TODO: somehow tell the user about this. maybe delete on disk too?
        del self.FLOO_BUFS[data['id']]
        path = utils.get_full_path(data['path'])
        if not G.DELETE_LOCAL_FILES:
            msg.log('Not deleting %s because delete_local_files is disabled' % path)
            return
        utils.rm(path)
        msg.warn('deleted %s because %s told me to.' % (path, data.get('username', 'the internet')))

    @buf_populated
    def on_highlight(self, data):
        #     floobits.highlight(data['id'], region_key, data['username'], data['ranges'], data.get('ping', False))
        #buf_id, region_key, username, ranges, ping=False):
        ping = data.get('ping', False)
        if self.follow_mode:
            ping = True
        buf = self.FLOO_BUFS[data['id']]
        view = self.get_view(data['id'])
        if not view:
            if not ping:
                return
            view = self.create_view(buf)
            if not view:
                return
        if ping:
            try:
                offset = data['ranges'][0][0]
            except IndexError as e:
                msg.debug('could not get offset from range %s' % e)
            else:
                msg.log('You have been summoned by %s' % (data.get('username', 'an unknown user')))
                view.focus()
                view.set_cursor_position(offset)
        if G.SHOW_HIGHLIGHTS:
            view.highlight(data['ranges'], data['user_id'])

    def on_error(self, data):
        message = 'Floobits: Error! Message: %s' % str(data.get('msg'))
        msg.error(message)

    def on_disconnect(self, data):
        message = 'Floobits: Disconnected! Reason: %s' % str(data.get('reason'))
        msg.error(message)
        msg.error(message)
        self.agent.stop()
