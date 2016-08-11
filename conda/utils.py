from __future__ import absolute_import, division, print_function

import collections
import errno
import hashlib
import logging
import os
import re
import shutil
import sys
import threading
import time
from functools import partial
from itertools import chain
from os import chmod
from os.path import basename, exists, isdir, join
from stat import S_IWRITE

from .common.url import path_to_url

log = logging.getLogger(__name__)

on_win = bool(sys.platform == "win32")


class memoized(object):
    """Decorator. Caches a function's return value each time it is called.
    If called later with the same arguments, the cached value is returned
    (not reevaluated).
    """
    def __init__(self, func):
        self.func = func
        self.cache = {}
        self.lock = threading.Lock()

    def __call__(self, *args, **kw):
        newargs = []
        for arg in args:
            if isinstance(arg, list):
                newargs.append(tuple(arg))
            elif not isinstance(arg, collections.Hashable):
                # uncacheable. a list, for instance.
                # better to not cache than blow up.
                return self.func(*args, **kw)
            else:
                newargs.append(arg)
        newargs = tuple(newargs)
        key = (newargs, frozenset(sorted(kw.items())))
        with self.lock:
            if key in self.cache:
                return self.cache[key]
            else:
                value = self.func(*args, **kw)
                self.cache[key] = value
                return value


# For instance methods only
class memoize(object):  # 577452
    def __init__(self, func):
        self.func = func

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self.func
        return partial(self, obj)

    def __call__(self, *args, **kw):
        obj = args[0]
        try:
            cache = obj.__cache
        except AttributeError:
            cache = obj.__cache = {}
        key = (self.func, args[1:], frozenset(sorted(kw.items())))
        try:
            res = cache[key]
        except KeyError:
            res = cache[key] = self.func(*args, **kw)
        return res

@memoized
def gnu_get_libc_version():
    """
    If on linux, get installed version of glibc, otherwise return None
    """

    if not sys.platform.startswith('linux'):
        return None

    from ctypes import CDLL, cdll, c_char_p

    cdll.LoadLibrary('libc.so.6')
    libc = CDLL('libc.so.6')
    f = libc.gnu_get_libc_version
    f.restype = c_char_p
    return f()


def try_write(dir_path, heavy=False):
    """Test write access to a directory.

    Args:
        dir_path (str): directory to test write access
        heavy (bool): Actually create and delete a file, or do a faster os.access test.
           https://docs.python.org/dev/library/os.html?highlight=xattr#os.access

    Returns:
        bool

    """
    if not isdir(dir_path):
        return False
    if on_win or heavy:
        # try to create a file to see if `dir_path` is writable, see #2151
        temp_filename = join(dir_path, '.conda-try-write-%d' % os.getpid())
        try:
            with open(temp_filename, mode='wb') as fo:
                fo.write(b'This is a test file.\n')
            backoff_unlink(temp_filename)
            return True
        except (IOError, OSError):
            return False
        finally:
            backoff_unlink(temp_filename)
    else:
        return os.access(dir_path, os.W_OK)


def backoff_unlink(file_or_symlink_path):
    try:
        exp_backoff_fn(lambda f: exists(f) and os.unlink(f), file_or_symlink_path)
    except (IOError, OSError) as e:
        if e.errno not in (errno.ENOENT,):
            # errno.ENOENT File not found error / No such file or directory
            raise


def backoff_rmdir(dirpath):
    if not isdir(dirpath):
        return

    recursive_make_writable(dirpath)

    def retry(func, path):
        recursive_make_writable(path)
        exp_backoff_fn(func, path)

    # shutil:
    #   if onerror is set, it is called to handle the error with arguments (func, path, exc_info)
    #     where func is os.listdir, os.remove, or os.rmdir;
    #     path is the argument to that function that caused it to fail; and
    #     exc_info is a tuple returned by sys.exc_info().
    shutil.rmtree(dirpath, onerror=retry)


def recursive_make_writable(path):
    # The need for this function was pointed out at
    #   https://github.com/conda/conda/issues/3266#issuecomment-239241915
    # Especially on windows, file removal will often fail because it is marked read-only
    if isdir(path):
        for root, dirs, files in os.walk(path):
            for path in chain.from_iterable((files, dirs)):
                try:
                    exp_backoff_fn(chmod, join(root, path), S_IWRITE)
                except (IOError, OSError) as e:
                    if e.errno == errno.ENOENT:
                        log.debug("no such file or directory: %s", path)
                    else:
                        raise
    else:
        exp_backoff_fn(chmod, path, S_IWRITE)


def hashsum_file(path, mode='md5'):
    h = hashlib.new(mode)
    with open(path, 'rb') as fi:
        while True:
            chunk = fi.read(262144)  # process chunks of 256KB
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def md5_file(path):
    return hashsum_file(path, 'md5')


def path_identity(path):
    """Used as a dummy path converter where no conversion necessary"""
    return path


def win_path_to_unix(path, root_prefix=""):
    """Convert a path or ;-separated string of paths into a unix representation

    Does not add cygdrive.  If you need that, set root_prefix to "/cygdrive"
    """
    path_re = '(?<![:/^a-zA-Z])([a-zA-Z]:[\/\\\\]+(?:[^:*?"<>|]+[\/\\\\]+)*[^:*?"<>|;\/\\\\]+?(?![a-zA-Z]:))'  # noqa

    def _translation(found_path):
        found = found_path.group(1).replace("\\", "/").replace(":", "").replace("//", "/")
        return root_prefix + "/" + found
    path = re.sub(path_re, _translation, path).replace(";/", ":/")
    return path


def unix_path_to_win(path, root_prefix=""):
    """Convert a path or :-separated string of paths into a Windows representation

    Does not add cygdrive.  If you need that, set root_prefix to "/cygdrive"
    """
    if len(path) > 1 and (";" in path or (path[1] == ":" and path.count(":") == 1)):
        # already a windows path
        return path.replace("/", "\\")
    path_re = root_prefix + r'(/[a-zA-Z]/(?:(?![:\s]/)[^:*?"<>])*)'

    def _translation(found_path):
        group = found_path.group(0)
        return "{0}:{1}".format(group[len(root_prefix)+1],
                                group[len(root_prefix)+2:].replace("/", "\\"))
    translation = re.sub(path_re, _translation, path)
    translation = re.sub(":([a-zA-Z]):\\\\",
                         lambda match: ";" + match.group(0)[1] + ":\\",
                         translation)
    return translation


# curry cygwin functions
def win_path_to_cygwin(path):
    return win_path_to_unix(path, "/cygdrive")


def cygwin_path_to_win(path):
    return unix_path_to_win(path, "/cygdrive")


def translate_stream(stream, translator):
    return "\n".join(translator(line) for line in stream.split("\n"))


def human_bytes(n):
    """
    Return the number of bytes n in more human readable form.
    """
    if n < 1024:
        return '%d B' % n
    k = n/1024
    if k < 1024:
        return '%d KB' % round(k)
    m = k/1024
    if m < 1024:
        return '%.1f MB' % m
    g = m/1024
    return '%.2f GB' % g


# TODO: this should be done in a more extensible way
#     (like files for each shell, with some registration mechanism.)

# defaults for unix shells.  Note: missing "exe" entry, which should be set to
#    either an executable on PATH, or a full path to an executable for a shell
unix_shell_base = dict(
                       binpath="/bin/",  # mind the trailing slash.
                       echo="echo",
                       env_script_suffix=".sh",
                       nul='2>/dev/null',
                       path_from=path_identity,
                       path_to=path_identity,
                       pathsep=":",
                       printdefaultenv='echo $CONDA_DEFAULT_ENV',
                       printpath="echo $PATH",
                       printps1='echo $PS1',
                       promptvar='PS1',
                       sep="/",
                       set_var='export ',
                       shell_args=["-l", "-c"],
                       shell_suffix="",
                       slash_convert=("\\", "/"),
                       source_setup="source",
                       test_echo_extra="",
                       var_format="${}",
)

msys2_shell_base = dict(
                        unix_shell_base,
                        path_from=unix_path_to_win,
                        path_to=win_path_to_unix,
                        binpath="/Scripts/",  # mind the trailing slash.
)

if on_win:
    shells = {
        # "powershell.exe": dict(
        #    echo="echo",
        #    test_echo_extra=" .",
        #    var_format="${var}",
        #    binpath="/bin/",  # mind the trailing slash.
        #    source_setup="source",
        #    nul='2>/dev/null',
        #    set_var='export ',
        #    shell_suffix=".ps",
        #    env_script_suffix=".ps",
        #    printps1='echo $PS1',
        #    printdefaultenv='echo $CONDA_DEFAULT_ENV',
        #    printpath="echo %PATH%",
        #    exe="powershell.exe",
        #    path_from=path_identity,
        #    path_to=path_identity,
        #    slash_convert = ("/", "\\"),
        # ),
        "cmd.exe": dict(
            echo="@echo",
            var_format="%{}%",
            binpath="\\Scripts\\",  # mind the trailing slash.
            source_setup="call",
            test_echo_extra="",
            nul='1>NUL 2>&1',
            set_var='set ',
            shell_suffix=".bat",
            env_script_suffix=".bat",
            printps1="@echo %PROMPT%",
            promptvar="PROMPT",
            # parens mismatched intentionally.  See http://stackoverflow.com/questions/20691060/how-do-i-echo-a-blank-empty-line-to-the-console-from-a-windows-batch-file # NOQA
            printdefaultenv='IF NOT "%CONDA_DEFAULT_ENV%" == "" (\n'
                            'echo %CONDA_DEFAULT_ENV% ) ELSE (\n'
                            'echo()',
            printpath="@echo %PATH%",
            exe="cmd.exe",
            shell_args=["/d", "/c"],
            path_from=path_identity,
            path_to=path_identity,
            slash_convert=("/", "\\"),
            sep="\\",
            pathsep=";",
        ),
        "cygwin": dict(
            unix_shell_base,
            exe="bash.exe",
            binpath="/Scripts/",  # mind the trailing slash.
            path_from=cygwin_path_to_win,
            path_to=win_path_to_cygwin
        ),
        # bash is whichever bash is on PATH.  If using Cygwin, you should use the cygwin
        #    entry instead.  The only major difference is that it handle's cygwin's /cygdrive
        #    filesystem root.
        "bash.exe": dict(
            msys2_shell_base, exe="bash.exe",
        ),
        "bash": dict(
            msys2_shell_base, exe="bash",
        ),
        "sh.exe": dict(
            msys2_shell_base, exe="sh.exe",
        ),
        "zsh.exe": dict(
            msys2_shell_base, exe="zsh.exe",
        ),
        "zsh": dict(
            msys2_shell_base, exe="zsh",
        ),
    }

else:
    shells = {
        "bash": dict(
            unix_shell_base, exe="bash",
                    ),
        "zsh": dict(
            unix_shell_base, exe="zsh",
                   ),
        "fish": dict(
            unix_shell_base, exe="fish",
            pathsep=" ",
                    ),
    }


def exp_backoff_fn(fn, *args):
    """Mostly for retrying file operations that fail on Windows due to virus scanners"""
    if not on_win:
        return fn(*args)

    import random
    # with max_tries = 6, max total time ~= 3.2 sec
    # with max_tries = 7, max total time ~= 6.5 sec
    max_tries = 7
    for n in range(max_tries):
        try:
            result = fn(*args)
        except (OSError, IOError) as e:
            log.debug(repr(e))
            if e.errno in (errno.EPERM, errno.EACCES):
                if n == max_tries-1:
                    raise
                sleep_time = ((2 ** n) + random.random()) * 0.1
                caller_frame = sys._getframe(1)
                log.debug("retrying %s/%s %s() in %g sec",
                          basename(caller_frame.f_code.co_filename),
                          caller_frame.f_lineno, fn.__name__,
                          sleep_time)
                time.sleep(sleep_time)
            elif e.errno in (errno.ENOENT,):
                # errno.ENOENT File not found error / No such file or directory
                raise
            else:
                log.error("Uncaught backoff with errno %d", e.errno)
                raise
        else:
            return result


# put back because of conda build
urlpath = url_path = path_to_url
