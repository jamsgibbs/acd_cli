"""fusepy filesystem module"""

import sys
import os
import stat
import errno
import logging
from time import time, sleep
from collections import deque, defaultdict
from multiprocessing import Process
from threading import Thread, Lock, Event
from queue import Queue, Full as QueueFull

from acdcli.bundled.fuse import FUSE, FuseOSError as FuseError, Operations
from acdcli.api.common import RequestError
from acdcli.api.content import CHUNK_SIZE as CHUNK_SZ
from acdcli.utils.time import *

logger = logging.getLogger(__name__)

MAX_CHUNKS_PER_FILE = 15
"""maximal number of opened chunks per file"""

CHUNK_TIMEOUT = 5
"""timeout for reading from an opened chunk"""

WRITE_TIMEOUT = 60
"""timeout for writing into file buffer"""

WRITE_BUFFER_SZ = 2 ** 5
"""maximal number of chunks per file"""


class FuseOSError(FuseError):
    def __init__(self, err_no: int):
        # logger.debug('FUSE error %i, %s.' % (err_no, errno.errorcode[err_no]))
        super().__init__(err_no)

    @staticmethod
    def convert(e: RequestError):
        """:raises: FuseOSError"""

        try:
            caller = sys._getframe().f_back.f_code.co_name + ': '
        except AttributeError:
            caller = ''
        logger.error(caller + e.__str__())

        if e.status_code == e.CODE.CONN_EXCEPTION:
            raise FuseOSError(errno.ECOMM)
        elif e.status_code == e.codes.CONFLICT:
            raise FuseOSError(errno.EEXIST)
        elif e.status_code == e.codes.REQUESTED_RANGE_NOT_SATISFIABLE:
            raise FuseOSError(errno.EFAULT)
        else:
            raise FuseOSError(errno.EREMOTEIO)


class ReadProxy(object):
    """Dict of stream chunks for consecutive read access of files."""

    def __init__(self, acd_client):
        self.acd_client = acd_client
        self.lock = Lock()
        self.files = defaultdict(ReadProxy.ReadFile)

    class StreamChunk(object):
        """StreamChunk represents a file node chunk as a streamed ranged HTTP response
        which may or may not be partially read."""

        __slots__ = ('offset', 'r', 'end')

        def __init__(self, acd_client, id_, offset, length, **kwargs):
            self.offset = offset
            """the first byte position (fpos) available in the chunk"""

            self.r = acd_client.response_chunk(id_, offset, length, **kwargs)
            """:type: requests.Response"""

            self.end = offset + int(self.r.headers['content-length']) - 1
            """the last byte position (fpos) contained in the chunk"""

        def has_byte_range(self, offset, length) -> bool:
            """Tests whether chunk begins at **offset** and has at least **length** bytes remaining.
            """
            logger.debug('s: %d-%d; r: %d-%d'
                         % (self.offset, self.end, offset, offset + length - 1))
            if offset == self.offset and offset + length - 1 <= self.end:
                return True
            return False

        def get(self, length) -> bytes:
            """Gets *length* bytes beginning at current offset.

            :param length: the number of bytes to get
            :raises: Exception if less than *length* bytes were received \
             but end of chunk was not reached"""

            b = next(self.r.iter_content(length))
            self.offset += len(b)

            if len(b) < length and self.offset <= self.end:
                logger.warning('Chunk ended unexpectedly.')
                raise Exception
            return b

        def close(self):
            """Closes connection on the stream."""
            self.r.close()

    class ReadFile(object):
        """Represents a file opened for reading.
        Encapsulates at most :attr:`MAX_CHUNKS_PER_FILE` open chunks."""

        __slots__ = ('chunks', 'access', 'lock')

        def __init__(self):
            self.chunks = deque(maxlen=MAX_CHUNKS_PER_FILE)
            self.access = time()
            self.lock = Lock()

        def get(self, acd_client, id_, offset, length, total) -> bytes:
            """Gets a byte range from existing StreamChunks"""

            with self.lock:
                i = self.chunks.__len__() - 1
                while i >= 0:
                    c = self.chunks[i]
                    if c.has_byte_range(offset, length):
                        try:
                            bytes_ = c.get(length)
                        except:
                            self.chunks.remove(c)
                        else:
                            return bytes_
                    i -= 1

            try:
                with self.lock:
                    chunk = ReadProxy.StreamChunk(acd_client, id_, offset,
                                                  CHUNK_SZ, timeout=CHUNK_TIMEOUT)
                    self.chunks.append(chunk)
                    return chunk.get(length)
            except RequestError as e:
                FuseOSError.convert(e)

        def clear(self):
            """Closes chunks and clears chunk deque."""
            with self.lock:
                for chunk in self.chunks:
                    try:
                        chunk.close()
                    except:
                        pass
                self.chunks.clear()

    def get(self, id_, offset, length, total):
        with self.lock:
            f = self.files[id_]
            return f.get(self.acd_client, id_, offset, length, total)

    def invalidate(self):
        pass

    def release(self, id_):
        with self.lock:
            f = self.files.get(id_)
        if f:
            f.clear()


class WriteProxy(object):
    """Collection of WriteStreams for consecutive file write operations."""

    def __init__(self, acd_client, cache):
        self.acd_client = acd_client
        self.cache = cache
        self.files = defaultdict(WriteProxy.WriteStream)

    class WriteStream(object):
        """A WriteStream is a binary file-like object that is backed by a Queue.
        It will remember its current offset."""

        __slots__ = ('q', 'offset', 'error', 'closed', 'done')

        def __init__(self):
            self.q = Queue(maxsize=WRITE_BUFFER_SZ)
            """a queue that buffers written blocks"""
            self.offset = 0
            """the beginning fpos"""
            self.error = False
            """whether the read or write failed"""
            self.closed = False
            self.done = Event()
            """done event is triggered when file is successfully read and transferred"""

        def write(self, data: bytes):
            """Writes data into queue.

            :raises: FuseOSError on timeout"""

            if self.error:
                raise FuseOSError(errno.EREMOTEIO)
            try:
                self.q.put(data, timeout=WRITE_TIMEOUT)
            except QueueFull:
                logger.error('Write timeout.')
                raise FuseOSError(errno.ETIMEDOUT)
            self.offset += len(data)

        def read(self, ln=0) -> bytes:
            """Returns as much byte data from queue as possible.
            Returns empty bytestring (EOF) if queue is empty and file was closed.

            :raises: IOError"""

            if self.error:
                raise IOError(errno.EIO, errno.errorcode[errno.EIO])

            if self.closed and self.q.empty():
                return b''

            b = [self.q.get()]
            self.q.task_done()
            while not self.q.empty():
                b.append(self.q.get())
                self.q.task_done()

            return b''.join(b)

        def flush(self):
            """Waits until the queue is emptied.

            :raises: FuseOSError"""

            while True:
                if self.error:
                    raise FuseOSError(errno.EREMOTEIO)
                if self.q.empty():
                    return
                sleep(1)

        def close(self):
            """Sets the closed flag to signal 'EOF' to the read function.
            Then, waits until :attr:`done` event is triggered.

            :raises: FuseOSError"""

            self.closed = True
            # prevent read deadlock
            self.q.put(b'')

            # wait until read is complete
            while True:
                if self.error:
                    raise FuseOSError(errno.EREMOTEIO)
                if self.done.wait(1):
                    return

    def write_n_sync(self, stream: WriteStream, node_id: str):
        """Try to overwrite file with id ``node_id`` with content from ``stream``.
        Triggers the :attr:`WriteStream.done` event on success.

        :param stream: a file-like object"""

        try:
            r = self.acd_client.overwrite_stream(stream, node_id)
        except (RequestError, IOError) as e:
            stream.error = True
            logger.error('Error writing node "%s". %s' % (node_id, str(e)))
        else:
            self.cache.insert_node(r)
            stream.done.set()

    def write(self, node_id, fh, offset, bytes_):
        """Gets WriteStream from defaultdict. Creates overwrite thread if offset is 0,
        tries to continue otherwise.

        :raises: FuseOSError: wrong offset or writing failed"""

        f = self.files[fh]

        if f.offset == offset:
            f.write(bytes_)
        else:
            f.error = True  # necessary?
            logger.error('Wrong offset for writing to fh %s.' % fh)
            raise FuseOSError(errno.ESPIPE)

        if offset == 0:
            t = Thread(target=self.write_n_sync, args=(f, node_id))
            t.daemon = True
            t.start()

    def flush(self, fh):
        f = self.files.get(fh)
        if f:
            f.flush()

    def release(self, fh):
        """:raises: FuseOSError"""
        f = self.files.get(fh)
        if f:
            try:
                f.close()
            except:
                raise
            finally:
                del self.files[fh]


class LoggingMixIn(object):
    """Modified fusepy LoggingMixIn that does not log read or written bytes
    and nicely formats non-decimal based arguments."""

    def __call__(self, op, path, *args):
        targs = None
        if op == 'open':
            targs = (('0x%0*x' % (4, args[0]),) + args[1:])
        elif op == 'write':
            targs = (len(args[0]),) + args[1:]
        elif op == 'chmod':
            targs = (oct(args[0]),) + args[1:]

        logger.debug('-> %s %s %s', op, path, repr(args if not targs else targs))

        ret = '[Unhandled Exception]'
        try:
            ret = getattr(self, op)(path, *args)
            return ret
        except OSError as e:
            ret = str(e)
            raise
        finally:
            if op == 'read':
                ret = len(ret)
            logger.debug('<- %s %s', op, repr(ret))


class ACDFuse(LoggingMixIn, Operations):
    """FUSE filesystem operations class for Amazon Cloud Drive.
    See `<http://fuse.sourceforge.net/doxygen/structfuse__operations.html>`_."""

    class XATTRS(object):
        """Generic extended node attributes"""
        ID = 'acd.id'
        DESCR = 'acd.description'

        @classmethod
        def vars(cls):
            """Returns list of manually added member variables."""
            return [getattr(cls, x) for x in set(dir(cls)) - set(dir(type('', (object,), {})))
                    if not callable(getattr(cls, x))]

    class FXATTRS(XATTRS):
        """Extended file attributes"""
        MD5 = 'acd.md5'

    def __init__(self, **kwargs):
        """Calculates ACD usage and starts autosync process.

        :param kwargs: cache (NodeCache), acd_client (ACDClient), autosync (partial)"""

        self.cache = kwargs['cache']
        self.acd_client = kwargs['acd_client']
        autosync = kwargs['autosync']

        self.rp = ReadProxy(self.acd_client)
        """collection of files opened for reading"""
        self.wp = WriteProxy(self.acd_client, self.cache)
        """collection of files opened for writing"""
        self.total, _ = self.acd_client.fs_sizes()
        """total disk space"""
        self.free = self.total - self.cache.calculate_usage()
        """manually determined free space (differs from available quota value)"""
        self.fh = 1
        """next free file handle\n\n :type: int"""
        self.nlinks = kwargs.get('nlinks', False)
        """whether to calculate the number of hardlinks for folders"""

        self.destroyed = autosync.keywords['stop']
        """:type: multiprocessing.Event"""

        p = Process(target=autosync)
        p.start()

    def destroy(self, path):
        self.destroyed.set()

    def readdir(self, path, fh) -> 'List[str]':
        """Lists the path's contents.

        :raises: FuseOSError if path is not a node or path is not a folder"""

        node = self.cache.resolve(path)
        if not node:
            raise FuseOSError(errno.ENOENT)
        if not node.type == 'folder':
            raise FuseOSError(errno.ENOTDIR)

        return [_ for _ in ['.', '..'] + [c for c in self.cache.childrens_names(node.id)]]

    def getattr(self, path, fh=None) -> dict:
        """Creates a stat-like attribute dict, see :manpage:`stat(2)`.
        Calculates correct number of links for folders if :attr:`nlinks` is set."""

        node = self.cache.resolve(path)
        if not node:
            raise FuseOSError(errno.ENOENT)

        times = dict(st_atime=time(),
                     st_mtime=node.modified.timestamp(),
                     st_ctime=node.created.timestamp())

        if node.is_folder:
            return dict(st_mode=stat.S_IFDIR | 0o0777,
                        st_nlink=self.cache.num_children(node.id) if self.nlinks else 1,
                        **times)
        elif node.is_file:
            return dict(st_mode=stat.S_IFREG | 0o0666,
                        st_nlink=self.cache.num_parents(node.id) if self.nlinks else 1,
                        st_size=node.size,
                        **times)

    def listxattr(self, path) -> 'List[str]':
        """Lists extended node attributes (names)."""

        node = self.cache.resolve(path)
        if node.is_file:
            return self.FXATTRS.vars()
        elif node.is_folder:
            return self.XATTRS.vars()

    def getxattr(self, path, name, position=0) -> bytes:
        """Gets value of extended attribute ``name``."""

        if name not in self.XATTRS.vars() and name not in self.FXATTRS.vars():
            raise FuseOSError(errno.ENODATA)

        node = self.cache.resolve(path)

        if name == self.XATTRS.ID:
            return bytes(node.id, encoding='utf-8')
        elif name == self.XATTRS.DESCR:
            return bytes(node.description if node.description else '', encoding='utf-8')
        elif name == self.FXATTRS.MD5:
            return bytes(node.md5, encoding='utf-8')

        raise FuseOSError(errno.ENODATA)

    def read(self, path, length, offset, fh) -> bytes:
        """Read ```length`` bytes from ``path`` att ``offset``."""

        node = self.cache.resolve(path, trash=False)

        if not node:
            raise FuseOSError(errno.ENOENT)

        if node.size == 0 or node.size == offset:
            return b''

        return self.rp.get(node.id, offset, length, node.size)

    def statfs(self, path) -> dict:
        """Gets some filesystem statistics as specified in :manpage:`stat(2)`."""

        bs = 512 * 1024  # no effect?
        return dict(f_bsize=bs,
                    f_frsize=bs,
                    f_blocks=self.total // bs,  # total no of blocks
                    f_bfree=self.free // bs,  # free blocks
                    f_bavail=self.free // bs,
                    f_namemax=256
                    )

    def mkdir(self, path, mode):
        """Creates a directory at ``path`` (see :manpage:`mkdir(2)`).

        :param mode: not used"""

        name = os.path.basename(path)
        ppath = os.path.dirname(path)
        p = self.cache.resolve(ppath)
        if not p:
            raise FuseOSError(errno.ENOTDIR)

        try:
            r = self.acd_client.create_folder(name, p.id)
        except RequestError as e:
            FuseOSError.convert(e)
        else:
            self.cache.insert_node(r)

    def _trash(self, path):
        logger.debug('trash %s' % path)
        node = self.cache.resolve(path, False)

        if not node:  # or not parent:
            raise FuseOSError(errno.ENOENT)

        try:
            # if len(node.parents) > 1:
            #     r = metadata.remove_child(parent.id, node.id)
            # else:
            r = self.acd_client.move_to_trash(node.id)
        except RequestError as e:
            FuseOSError.convert(e)
        else:
            self.cache.insert_node(r)

    def rmdir(self, path):
        """Moves a directory into ACD trash."""
        self._trash(path)

    def unlink(self, path):
        """Moves a file into ACD trash."""
        self._trash(path)

    def create(self, path, mode) -> int:
        """Creates an empty file at ``path``.

        :param mode: not used
        :returns int: file handle"""

        name = os.path.basename(path)
        ppath = os.path.dirname(path)
        p = self.cache.resolve(ppath, False)
        if not p:
            raise FuseOSError(errno.ENOTDIR)

        try:
            r = self.acd_client.create_file(name, p.id)
            self.cache.insert_node(r)
        except RequestError as e:
            FuseOSError.convert(e)

        self.fh += 1
        return self.fh

    def rename(self, old, new):
        """Renames ``old`` into ``new`` (may also involve a move).
        If ``new`` is an existing file, it is moved into the ACD trash.

        :raises FuseOSError: ENOENT if ``old`` is not a node, \
        EEXIST if ``new`` is an existing folder \
        ENOTDIR if ``new``'s parent path does not exist"""

        if old == new:
            return

        node = self.cache.resolve(old, False)
        if not node:
            raise FuseOSError(errno.ENOENT)

        new_bn, new_dn = os.path.basename(new), os.path.dirname(new)
        old_bn, old_dn = os.path.basename(old), os.path.dirname(old)

        existing = self.cache.resolve(new, False)
        if existing:
            if existing.is_file:
                self._trash(new)
            else:
                raise FuseOSError(errno.EEXIST)

        if new_bn != old_bn:
            self._rename(node.id, new_bn)

        if new_dn != old_dn:
            # odir_id = self.cache.resolve_path(old_dn, False)
            ndir = self.cache.resolve(new_dn, False)
            if not ndir:
                raise FuseOSError(errno.ENOTDIR)
            self._move(node.id, ndir.id)

    def _rename(self, id, name):
        try:
            r = self.acd_client.rename_node(id, name)
        except RequestError as e:
            FuseOSError.convert(e)
        else:
            self.cache.insert_node(r)

    def _move(self, id, new_folder):
        try:
            r = self.acd_client.move_node(id, new_folder)
        except RequestError as e:
            FuseOSError.convert(e)
        else:
            self.cache.insert_node(r)

    def open(self, path, flags) -> int:
        """Sloppily opens a file.

        :param flags: flags defined as in :manpage:`open(2)`
        :returns: file handle"""

        if (flags & os.O_APPEND) == os.O_APPEND:
            raise FuseOSError(errno.EFAULT)

        # TODO: check flags
        self.fh += 1
        return self.fh

    def write(self, path, data, offset, fh) -> int:
        """Invokes :attr:`wp`'s write function.

        :returns: number of bytes written"""

        if offset == 0:
            n = self.cache.resolve(path, False)
            node_id = n.id
        else:
            node_id = ''
        self.wp.write(node_id, fh, offset, data)
        return len(data)

    def flush(self, path, fh):
        """Flushes ``fh`` in WriteProxy."""
        self.wp.flush(fh)

    def truncate(self, path, length, fh=None):
        """Pseudo-truncates a file, i.e. clears content if ``length`` ==0 or does nothing
        if ``length`` is equal to current file size.

        :raises FuseOSError: if pseudo-truncation to length is not supported"""

        n = self.cache.resolve(path)
        if length == 0:
            try:
                r = self.acd_client.clear_file(n.id)
            except RequestError as e:
                raise FuseOSError.convert(e)
            else:
                self.cache.insert_node(r)
        elif length > 0:
            if n.size != length:
                raise FuseOSError(errno.ENOSYS)

    def release(self, path, fh):
        """Releases an open ``path``."""
        node = self.cache.resolve(path, trash=False)
        if node:
            self.rp.release(node.id)
            self.wp.release(fh)

    def utimens(self, path, times=None):
        """Not functional. Should set node atime and mtime to values as passed in ``times``
        or current time (see :manpage:`utimesat(2)`).

        :param times: [atime, mtime]"""

        if times:
            # atime = times[0]
            mtime = times[1]
        else:
            # atime = time()
            mtime = time()

    def chmod(self, path, mode):
        """Not implemented."""
        pass

    def chown(self, path, uid, gid):
        """Not implemented."""
        pass


def mount(path: str, args: dict, **kwargs) -> 'Union[int, None]':
    """Fusermounts Amazon Cloud Drive to specified mountpoint.

    :raises: RuntimeError
    :param args: args to pass on to ACDFuse init
    :param kwargs: fuse mount options as described in :manpage:`fuse(8)`"""

    if not os.path.isdir(path):
        logger.critical('Mountpoint does not exist or already used.')
        return 1

    FUSE(ACDFuse(**args), path,
         auto_cache=True, sync_read=True,
         uid=os.getuid(), gid=os.getgid(),
         subtype=ACDFuse.__name__,
         **kwargs
         )


def unmount(path=None, lazy=False) -> int:
    """Unmounts a specific mountpoint if path given or all of the user's ACDFuse mounts.

    :returns: 0 on success, 1 on error"""

    import platform
    import re
    import subprocess

    system = platform.system().lower()

    if system != 'darwin':
        umount_cmd = ['fusermount', '-u']
    else:
        umount_cmd = ['umount']
    if lazy:
        if system == 'linux':
            umount_cmd.append('-l')
        else:
            logging.warning('Lazy unmounting is not supported on your platform.')

    fuse_st = ACDFuse.__name__

    if path:
        paths = [path]
    else:
        if system not in ['linux', 'darwin']:
            logger.critical('Automatic unmounting is not supported on your platform.')
            return 1

        paths = []
        try:
            if system == 'linux':
                mounts = subprocess.check_output(['mount', '-t', 'fuse.' + fuse_st])
            elif system == 'darwin':
                mounts = subprocess.check_output(['mount'])

            mounts = mounts.decode('UTF-8').splitlines()
        except:
            logger.critical('Getting mountpoints failed.')
            return 1

        for mount in mounts:
            if fuse_st in mount:
                if (system == 'linux' and 'user_id=%i' % os.getuid() in mount) or \
                (system == 'darwin' and 'mounted by %s' % os.getlogin() in mount):
                    paths.append(re.search(fuse_st + ' on (.*?) ', mount).group(1))

    ret = 0
    for path in paths:
        command = list(umount_cmd)
        command.append(path)
        try:
            subprocess.check_call(command)
        except subprocess.CalledProcessError:
            # logger.error('Unmounting %s failed.' % path)
            ret |= 1

    return ret
