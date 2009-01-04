# Authors:
#   Jason Gerard DeRose <jderose@redhat.com>
#
# Copyright (C) 2008  Red Hat
# see file 'COPYING' for use and warranty information
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; version 2 only
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA

"""
Plugin framework.

The classes in this module make heavy use of Python container emulation. If
you are unfamiliar with this Python feature, see
http://docs.python.org/ref/sequence-types.html
"""

import re
import sys
import inspect
import threading
import logging
import os
from os import path
import subprocess
import errors
import errors2
from config import Env
import util
from base import ReadOnly, NameSpace, lock, islocked, check_name
from constants import DEFAULT_CONFIG


class SetProxy(ReadOnly):
    """
    A read-only container with set/sequence behaviour.

    This container acts as a proxy to an actual set-like object (a set,
    frozenset, or dict) that is passed to the constructor. To the extent
    possible in Python, this underlying set-like object cannot be modified
    through the SetProxy... which just means you wont do it accidentally.
    """
    def __init__(self, s):
        """
        :param s: The target set-like object (a set, frozenset, or dict)
        """
        allowed = (set, frozenset, dict)
        if type(s) not in allowed:
            raise TypeError('%r not in %r' % (type(s), allowed))
        self.__s = s
        lock(self)

    def __len__(self):
        """
        Return the number of items in this container.
        """
        return len(self.__s)

    def __iter__(self):
        """
        Iterate (in ascending order) through keys.
        """
        for key in sorted(self.__s):
            yield key

    def __contains__(self, key):
        """
        Return True if this container contains ``key``.

        :param key: The key to test for membership.
        """
        return key in self.__s


class DictProxy(SetProxy):
    """
    A read-only container with mapping behaviour.

    This container acts as a proxy to an actual mapping object (a dict) that
    is passed to the constructor. To the extent possible in Python, this
    underlying mapping object cannot be modified through the DictProxy...
    which just means you wont do it accidentally.

    Also see `SetProxy`.
    """
    def __init__(self, d):
        """
        :param d: The target mapping object (a dict)
        """
        if type(d) is not dict:
            raise TypeError('%r is not %r' % (type(d), dict))
        self.__d = d
        super(DictProxy, self).__init__(d)

    def __getitem__(self, key):
        """
        Return the value corresponding to ``key``.

        :param key: The key of the value you wish to retrieve.
        """
        return self.__d[key]

    def __call__(self):
        """
        Iterate (in ascending order by key) through values.
        """
        for key in self:
            yield self.__d[key]


class MagicDict(DictProxy):
    """
    A mapping container whose values can be accessed as attributes.

    For example:

    >>> magic = MagicDict({'the_key': 'the value'})
    >>> magic['the_key']
    'the value'
    >>> magic.the_key
    'the value'

    This container acts as a proxy to an actual mapping object (a dict) that
    is passed to the constructor. To the extent possible in Python, this
    underlying mapping object cannot be modified through the MagicDict...
    which just means you wont do it accidentally.

    Also see `DictProxy` and `SetProxy`.
    """

    def __getattr__(self, name):
        """
        Return the value corresponding to ``name``.

        :param name: The name of the attribute you wish to retrieve.
        """
        try:
            return self[name]
        except KeyError:
            raise AttributeError('no magic attribute %r' % name)


class Plugin(ReadOnly):
    """
    Base class for all plugins.
    """
    __public__ = frozenset()
    __proxy__ = True
    __api = None

    def __init__(self):
        cls = self.__class__
        self.name = cls.__name__
        self.module = cls.__module__
        self.fullname = '%s.%s' % (self.module, self.name)
        self.doc = inspect.getdoc(cls)
        if self.doc is None:
            self.summary = '<%s>' % self.fullname
        else:
            self.summary = self.doc.split('\n\n', 1)[0]
        log = logging.getLogger(self.fullname)
        for name in ('debug', 'info', 'warning', 'error', 'critical', 'exception'):
            if hasattr(self, name):
                raise StandardError(
                    '%s.%s attribute (%r) conflicts with Plugin logger' % (
                        self.name, name, getattr(self, name))
                )
            setattr(self, name, getattr(log, name))

    def __get_api(self):
        """
        Return `API` instance passed to `finalize()`.

        If `finalize()` has not yet been called, None is returned.
        """
        return self.__api
    api = property(__get_api)

    @classmethod
    def implements(cls, arg):
        """
        Return True if this class implements ``arg``.

        There are three different ways this method can be called:

        With a <type 'str'> argument, e.g.:

        >>> class base(Plugin):
        ...     __public__ = frozenset(['attr1', 'attr2'])
        ...
        >>> base.implements('attr1')
        True
        >>> base.implements('attr2')
        True
        >>> base.implements('attr3')
        False

        With a <type 'frozenset'> argument, e.g.:

        With any object that has a `__public__` attribute that is
        <type 'frozenset'>, e.g.:

        Unlike ProxyTarget.implemented_by(), this returns an abstract answer
        because only the __public__ frozenset is checked... a ProxyTarget
        need not itself have attributes for all names in __public__
        (subclasses might provide them).
        """
        assert type(cls.__public__) is frozenset
        if isinstance(arg, str):
            return arg in cls.__public__
        if type(getattr(arg, '__public__', None)) is frozenset:
            return cls.__public__.issuperset(arg.__public__)
        if type(arg) is frozenset:
            return cls.__public__.issuperset(arg)
        raise TypeError(
            "must be str, frozenset, or have frozenset '__public__' attribute"
        )

    @classmethod
    def implemented_by(cls, arg):
        """
        Return True if ``arg`` implements public interface of this class.

        This classmethod returns True if:

        1. ``arg`` is an instance of or subclass of this class, and

        2. ``arg`` (or ``arg.__class__`` if instance) has an attribute for
           each name in this class's ``__public__`` frozenset.

        Otherwise, returns False.

        Unlike `Plugin.implements`, this returns a concrete answer because
        the attributes of the subclass are checked.

        :param arg: An instance of or subclass of this class.
        """
        if inspect.isclass(arg):
            subclass = arg
        else:
            subclass = arg.__class__
        assert issubclass(subclass, cls), 'must be subclass of %r' % cls
        for name in cls.__public__:
            if not hasattr(subclass, name):
                return False
        return True

    def finalize(self):
        """
        """
        lock(self)

    def set_api(self, api):
        """
        Set reference to `API` instance.
        """
        assert self.__api is None, 'set_api() can only be called once'
        assert api is not None, 'set_api() argument cannot be None'
        self.__api = api
        if not isinstance(api, API):
            return
        for name in api:
            assert not hasattr(self, name)
            setattr(self, name, api[name])
        # FIXME: the 'log' attribute is depreciated.  See Plugin.__init__()
        for name in ('env', 'context', 'log'):
            if hasattr(api, name):
                assert not hasattr(self, name)
                setattr(self, name, getattr(api, name))

    def call(self, executable, *args):
        """
        Call ``executable`` with ``args`` using subprocess.call().

        If the call exits with a non-zero exit status,
        `ipalib.errors.SubprocessError` is raised, from which you can retrieve
        the exit code by checking the SubprocessError.returncode attribute.

        This method does *not* return what ``executable`` sent to stdout... for
        that, use `Plugin.callread()`.
        """
        argv = (executable,) + args
        self.debug('Calling %r', argv)
        code = subprocess.call(argv)
        if code != 0:
            raise errors2.SubprocessError(returncode=code, argv=argv)

    def __repr__(self):
        """
        Return 'module_name.class_name()' representation.

        This representation could be used to instantiate this Plugin
        instance given the appropriate environment.
        """
        return '%s.%s()' % (
            self.__class__.__module__,
            self.__class__.__name__
        )


class PluginProxy(SetProxy):
    """
    Allow access to only certain attributes on a `Plugin`.

    Think of a proxy as an agreement that "I will have at most these
    attributes". This is different from (although similar to) an interface,
    which can be thought of as an agreement that "I will have at least these
    attributes".
    """

    __slots__ = (
        '__base',
        '__target',
        '__name_attr',
        '__public__',
        'name',
        'doc',
    )

    def __init__(self, base, target, name_attr='name'):
        """
        :param base: A subclass of `Plugin`.
        :param target: An instance ``base`` or a subclass of ``base``.
        :param name_attr: The name of the attribute on ``target`` from which
            to derive ``self.name``.
        """
        if not inspect.isclass(base):
            raise TypeError(
                '`base` must be a class, got %r' % base
            )
        if not isinstance(target, base):
            raise ValueError(
                '`target` must be an instance of `base`, got %r' % target
            )
        self.__base = base
        self.__target = target
        self.__name_attr = name_attr
        self.__public__ = base.__public__
        self.name = getattr(target, name_attr)
        self.doc = target.doc
        assert type(self.__public__) is frozenset
        super(PluginProxy, self).__init__(self.__public__)

    def implements(self, arg):
        """
        Return True if plugin being proxied implements ``arg``.

        This method simply calls the corresponding `Plugin.implements`
        classmethod.

        Unlike `Plugin.implements`, this is not a classmethod as a
        `PluginProxy` can only implement anything as an instance.
        """
        return self.__base.implements(arg)

    def __clone__(self, name_attr):
        """
        Return a `PluginProxy` instance similar to this one.

        The new `PluginProxy` returned will be identical to this one except
        the proxy name might be derived from a different attribute on the
        target `Plugin`.  The same base and target will be used.
        """
        return self.__class__(self.__base, self.__target, name_attr)

    def __getitem__(self, key):
        """
        Return attribute named ``key`` on target `Plugin`.

        If this proxy allows access to an attribute named ``key``, that
        attribute will be returned.  If access is not allowed,
        KeyError will be raised.
        """
        if key in self.__public__:
            return getattr(self.__target, key)
        raise KeyError('no public attribute %s.%s' % (self.name, key))

    def __getattr__(self, name):
        """
        Return attribute named ``name`` on target `Plugin`.

        If this proxy allows access to an attribute named ``name``, that
        attribute will be returned.  If access is not allowed,
        AttributeError will be raised.
        """
        if name in self.__public__:
            return getattr(self.__target, name)
        raise AttributeError('no public attribute %s.%s' % (self.name, name))

    def __call__(self, *args, **kw):
        """
        Call target `Plugin` and return its return value.

        If `__call__` is not an attribute this proxy allows access to,
        KeyError is raised.
        """
        return self['__call__'](*args, **kw)

    def __repr__(self):
        """
        Return a Python expression that could create this instance.
        """
        return '%s(%s, %r)' % (
            self.__class__.__name__,
            self.__base.__name__,
            self.__target,
        )


class Registrar(DictProxy):
    """
    Collects plugin classes as they are registered.

    The Registrar does not instantiate plugins... it only implements the
    override logic and stores the plugins in a namespace per allowed base
    class.

    The plugins are instantiated when `API.finalize()` is called.
    """
    def __init__(self, *allowed):
        """
        :param allowed: Base classes from which plugins accepted by this
            Registrar must subclass.
        """
        self.__allowed = dict((base, {}) for base in allowed)
        self.__registered = set()
        super(Registrar, self).__init__(
            dict(self.__base_iter())
        )

    def __base_iter(self):
        for (base, sub_d) in self.__allowed.iteritems():
            assert inspect.isclass(base)
            name = base.__name__
            assert not hasattr(self, name)
            setattr(self, name, MagicDict(sub_d))
            yield (name, base)

    def __findbases(self, klass):
        """
        Iterates through allowed bases that ``klass`` is a subclass of.

        Raises `errors.SubclassError` if ``klass`` is not a subclass of any
        allowed base.

        :param klass: The class to find bases for.
        """
        assert inspect.isclass(klass)
        found = False
        for (base, sub_d) in self.__allowed.iteritems():
            if issubclass(klass, base):
                found = True
                yield (base, sub_d)
        if not found:
            raise errors2.PluginSubclassError(
                plugin=klass, bases=self.__allowed.keys()
            )

    def __call__(self, klass, override=False):
        """
        Register the plugin ``klass``.

        :param klass: A subclass of `Plugin` to attempt to register.
        :param override: If true, override an already registered plugin.
        """
        if not inspect.isclass(klass):
            raise TypeError('plugin must be a class; got %r' % klass)

        # Raise DuplicateError if this exact class was already registered:
        if klass in self.__registered:
            raise errors2.PluginDuplicateError(plugin=klass)

        # Find the base class or raise SubclassError:
        for (base, sub_d) in self.__findbases(klass):
            # Check override:
            if klass.__name__ in sub_d:
                if not override:
                    # Must use override=True to override:
                    raise errors2.PluginOverrideError(
                        base=base.__name__,
                        name=klass.__name__,
                        plugin=klass,
                    )
            else:
                if override:
                    # There was nothing already registered to override:
                    raise errors2.PluginMissingOverrideError(
                        base=base.__name__,
                        name=klass.__name__,
                        plugin=klass,
                    )

            # The plugin is okay, add to sub_d:
            sub_d[klass.__name__] = klass

        # The plugin is okay, add to __registered:
        self.__registered.add(klass)


class LazyContext(object):
    """
    On-demand creation of thread-local context attributes.
    """

    def __init__(self, api):
        self.__api = api
        self.__context = threading.local()

    def __getattr__(self, name):
        if name not in self.__context.__dict__:
            if name not in self.__api.Context:
                raise AttributeError('no Context plugin for %r' % name)
            value = self.__api.Context[name].get_value()
            self.__context.__dict__[name] = value
        return self.__context.__dict__[name]

    def __getitem__(self, key):
        return self.__getattr__(key)



class API(DictProxy):
    """
    Dynamic API object through which `Plugin` instances are accessed.
    """

    def __init__(self, *allowed):
        self.__d = dict()
        self.__done = set()
        self.register = Registrar(*allowed)
        self.env = Env()
        self.context = LazyContext(self)
        super(API, self).__init__(self.__d)

    def __doing(self, name):
        if name in self.__done:
            raise StandardError(
                '%s.%s() already called' % (self.__class__.__name__, name)
            )
        self.__done.add(name)

    def __do_if_not_done(self, name):
        if name not in self.__done:
            getattr(self, name)()

    def isdone(self, name):
        return name in self.__done

    def bootstrap(self, **overrides):
        """
        Initialize environment variables and logging.
        """
        self.__doing('bootstrap')
        self.env._bootstrap(**overrides)
        self.env._finalize_core(**dict(DEFAULT_CONFIG))
        log = logging.getLogger('ipa')
        object.__setattr__(self, 'log', log)
        if self.env.debug:
            log.setLevel(logging.DEBUG)
        else:
            log.setLevel(logging.INFO)

        # Add stderr handler:
        stderr = logging.StreamHandler()
        format = self.env.log_format_stderr
        if self.env.debug:
            format = self.env.log_format_stderr_debug
            stderr.setLevel(logging.DEBUG)
        elif self.env.verbose:
            stderr.setLevel(logging.INFO)
        else:
            stderr.setLevel(logging.WARNING)
        stderr.setFormatter(util.LogFormatter(format))
        log.addHandler(stderr)

        # Add file handler:
        if self.env.mode in ('dummy', 'unit_test'):
            return # But not if in unit-test mode
        log_dir = path.dirname(self.env.log)
        if not path.isdir(log_dir):
            try:
                os.makedirs(log_dir)
            except OSError:
                log.warn('Could not create log_dir %r', log_dir)
                return
        handler = logging.FileHandler(self.env.log)
        handler.setFormatter(util.LogFormatter(self.env.log_format_file))
        if self.env.debug:
            handler.setLevel(logging.DEBUG)
        else:
            handler.setLevel(logging.INFO)
        log.addHandler(handler)

    def bootstrap_with_global_options(self, options=None, context=None):
        if options is None:
            parser = util.add_global_options()
            (options, args) = parser.parse_args(
                list(s.decode('utf-8') for s in sys.argv[1:])
            )
        overrides = {}
        if options.env is not None:
            assert type(options.env) is list
            for item in options.env:
                try:
                    (key, value) = item.split('=', 1)
                except ValueError:
                    # FIXME: this should raise an IPA exception with an
                    # error code.
                    # --Jason, 2008-10-31
                    pass
                overrides[str(key.strip())] = value.strip()
        for key in ('conf', 'debug', 'verbose'):
            value = getattr(options, key, None)
            if value is not None:
                overrides[key] = value
        if context is not None:
            overrides['context'] = context
        self.bootstrap(**overrides)

    def load_plugins(self):
        """
        Load plugins from all standard locations.

        `API.bootstrap` will automatically be called if it hasn't been
        already.
        """
        self.__doing('load_plugins')
        self.__do_if_not_done('bootstrap')
        if self.env.mode in ('dummy', 'unit_test'):
            return
        util.import_plugins_subpackage('ipalib')
        if self.env.in_server:
            util.import_plugins_subpackage('ipa_server')

    def finalize(self):
        """
        Finalize the registration, instantiate the plugins.

        `API.bootstrap` will automatically be called if it hasn't been
        already.
        """
        self.__doing('finalize')
        self.__do_if_not_done('load_plugins')

        class PluginInstance(object):
            """
            Represents a plugin instance.
            """

            i = 0

            def __init__(self, klass):
                self.created = self.next()
                self.klass = klass
                self.instance = klass()
                self.bases = []

            @classmethod
            def next(cls):
                cls.i += 1
                return cls.i

        class PluginInfo(ReadOnly):
            def __init__(self, p):
                assert isinstance(p, PluginInstance)
                self.created = p.created
                self.name = p.klass.__name__
                self.module = str(p.klass.__module__)
                self.plugin = '%s.%s' % (self.module, self.name)
                self.bases = tuple(b.__name__ for b in p.bases)
                lock(self)

        plugins = {}
        def plugin_iter(base, subclasses):
            for klass in subclasses:
                assert issubclass(klass, base)
                if klass not in plugins:
                    plugins[klass] = PluginInstance(klass)
                p = plugins[klass]
                assert base not in p.bases
                p.bases.append(base)
                if base.__proxy__:
                    yield PluginProxy(base, p.instance)
                else:
                    yield p.instance

        for name in self.register:
            base = self.register[name]
            magic = getattr(self.register, name)
            namespace = NameSpace(
                plugin_iter(base, (magic[k] for k in magic))
            )
            assert not (
                name in self.__d or hasattr(self, name)
            )
            self.__d[name] = namespace
            object.__setattr__(self, name, namespace)

        for p in plugins.itervalues():
            p.instance.set_api(self)
            assert p.instance.api is self

        for p in plugins.itervalues():
            p.instance.finalize()
        object.__setattr__(self, '_API__finalized', True)
        tuple(PluginInfo(p) for p in plugins.itervalues())
        object.__setattr__(self, 'plugins',
            tuple(PluginInfo(p) for p in plugins.itervalues())
        )
