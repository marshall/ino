# -*- coding: utf-8; -*-

import sys
import os.path
import itertools
import argparse
import pickle
import platform
import hashlib
import re

try:
    from collections import OrderedDict
except ImportError:
    # Python < 2.7
    from ordereddict import OrderedDict

from collections import namedtuple
from glob import glob

from ino.filters import colorize
from ino.utils import format_available_options
from ino.exc import Abort


class Version(namedtuple('Version', 'major minor')):

    regex = re.compile(ur'^\d+(\.\d+)?')

    @classmethod
    def parse(cls, s):
        # Version could have various forms
        #   0022
        #   0022ubuntu0.1
        #   0022-macosx-20110822
        #   1.0
        # We have to extract a 2-int-tuple (major, minor)
        match = cls.regex.match(s)
        if not match:
            raise Abort("Could not parse Arduino library version: %s" % s)
        v = match.group(0)
        if v.startswith('0'):
            return cls(0, int(v))
        return cls(*map(int, v.split('.')))

    def as_int(self):
        return self.major * 100 + self.minor

    def __str__(self):
        return '%s.%s' % self


class Environment(dict):

    templates_dir = os.path.join(os.path.dirname(__file__), 'templates')
    output_dir = '.build'
    src_dir = 'src'
    lib_dir = 'lib'
    hex_filename = 'firmware.hex'

    arduino_dist_dir = None
    arduino_dist_dir_guesses = [
        '/usr/local/share/arduino',
        '/usr/share/arduino',
    ]

    if platform.system() == 'Darwin':
        arduino_dist_dir_guesses.insert(0, '/Applications/Arduino.app/Contents/Resources/Java')

    default_board_model = 'uno'
    ino = sys.argv[0]

    def dump(self):
        if not os.path.isdir(self.output_dir):
            return
        with open(self.dump_filepath, 'wb') as f:
            pickle.dump(self.items(), f)

    def load(self):
        if not os.path.exists(self.dump_filepath):
            return
        with open(self.dump_filepath, 'rb') as f:
            try:
                self.update(pickle.load(f))
            except:
                print colorize('Environment dump exists (%s), but failed to load' % 
                               self.dump_filepath, 'yellow')

    @property
    def dump_filepath(self):
        return os.path.join(self.output_dir, 'environment.pickle')

    def __getitem__(self, key):
        try:
            return super(Environment, self).__getitem__(key)
        except KeyError as e:
            try:
                return getattr(self, key)
            except AttributeError:
                raise e

    def __getattr__(self, attr):
        try:
            return super(Environment, self).__getitem__(attr)
        except KeyError:
            raise AttributeError("Environment has no attribute %r" % attr)

    @property
    def hex_path(self):
        return os.path.join(self.build_dir, self.hex_filename)

    def _find(self, key, items, places, human_name, join):
        if key in self:
            return self[key]

        human_name = human_name or key

        # expand env variables in `places` and split on colons
        places = itertools.chain.from_iterable(os.path.expandvars(p).split(os.pathsep) for p in places)
        places = map(os.path.expanduser, places)

        print 'Searching for', human_name, '...',
        for p in places:
            for i in items:
                path = os.path.join(p, i)
                if os.path.exists(path):
                    result = path if join else p
                    print colorize(result, 'green')
                    self[key] = result
                    return result

        print colorize('FAILED', 'red')
        raise Abort("%s not found. Searched in following places: %s" %
                    (human_name, ''.join(['\n  - ' + p for p in places])))

    def find_dir(self, key, items, places, human_name=None):
        return self._find(key, items or ['.'], places, human_name, join=False)

    def find_file(self, key, items=None, places=None, human_name=None):
        return self._find(key, items or [key], places, human_name, join=True)

    def find_tool(self, key, items, places=None, human_name=None):
        return self.find_file(key, items, places or ['$PATH'], human_name)

    def find_arduino_dir(self, key, dirname_parts, items=None, human_name=None):
        return self.find_dir(key, items, self._arduino_dist_places(dirname_parts), human_name)

    def find_arduino_file(self, key, dirname_parts, items=None, human_name=None):
        return self.find_file(key, items, self._arduino_dist_places(dirname_parts), human_name)

    def find_arduino_tool(self, key, dirname_parts, items=None, human_name=None):
        # if not bundled with Arduino Software the tool should be searched on PATH
        places = self._arduino_dist_places(dirname_parts) + ['$PATH']
        return self.find_file(key, items, places, human_name)

    def _arduino_dist_places(self, dirname_parts):
        """
        For `dirname_parts` like [a, b, c] return list of
        search paths within Arduino distribution directory like:
            /user/specified/path/a/b/c
            /usr/local/share/arduino/a/b/c
            /usr/share/arduino/a/b/c
        """
        if 'arduino_dist_dir' in self:
            places = [self['arduino_dist_dir']]
        else:
            places = self.arduino_dist_dir_guesses
        return [os.path.join(p, *dirname_parts) for p in places]

    def use_arduino15_dirs(self):
        return self.arduino_lib_version.major >= 1 and \
               self.arduino_lib_version.minor >= 5

    def board_models(self):
        if 'board_models' in self:
            return self['board_models']

        self['board_models'] = BoardModels()
        self['board_models'].default = self.default_board_model

        if self.use_arduino15_dirs():
            for arch in ['sam', 'avr']:
                boards_txt = self.find_arduino_file(arch+'_boards.txt', ['hardware', 'arduino', arch],
                                                    items=['boards.txt'], human_name='Board description file (%s/boards.txt)' % arch)
                self['board_models'].parse(boards_txt, arch)

        else:
            boards_txt = self.find_arduino_file('boards.txt', ['hardware', 'arduino'],
                                                human_name='Board description file (boards.txt)')
            self['board_models'].parse(boards_txt, 'avr')

        return self['board_models']

    def platforms(self):
        if 'platforms' in self:
            return self['platforms']

        self['platforms'] = OrderedDict()
        self['platforms'].default = 'avr'

        if self.use_arduino15_dirs():
            for arch in ['sam', 'avr']:
                platform_txt = self.find_arduino_file(arch+'_platform.txt', ['hardware', 'arduino', arch],
                                                      items=['platform.txt'], human_name='Platform description file (%s/platform.txt)' % arch)
                self['platforms'][arch] = ArduinoData()
                self['platforms'][arch].parse(platform_txt)

        return self['platforms']

    def board_model(self, key):
        return self.board_models()[key]

    def platform(self, arch):
        return self.platforms()[arch]

    def replace_vars(self, value, **kwargs):
        def replace_var(match):
            multikey = match.group(1).split('.')
            d = kwargs
            for key in multikey:
                if key not in d:
                    return '{' + '.'.join(multikey) + '}'
                d = d[key]
            return self.replace_vars(d, **kwargs)

        return re.sub(r'\{([^{}]+)\}', replace_var, value)

    def add_board_model_arg(self, parser):
        help = '\n'.join([
            "Arduino board model (default: %(default)s)",
            "For a full list of supported models run:", 
            "`ino list-models'"
        ])

        parser.add_argument('-m', '--board-model', metavar='MODEL', 
                            default=self.default_board_model, help=help)

    def add_arduino_dist_arg(self, parser):
        parser.add_argument('-d', '--arduino-dist', metavar='PATH', 
                            help='Path to Arduino distribution, e.g. ~/Downloads/arduino-0022.\nTry to guess if not specified')

    def serial_port_patterns(self):
        system = platform.system()
        if system == 'Linux':
            return ['/dev/ttyACM*', '/dev/ttyUSB*']
        if system == 'Darwin':
            return ['/dev/tty.usbmodem*', '/dev/tty.usbserial*']
        raise NotImplementedError("Not implemented for Windows")

    def list_serial_ports(self):
        ports = []
        for p in self.serial_port_patterns():
            matches = glob(p)
            ports.extend(matches)
        return ports

    def guess_serial_port(self):
        print 'Guessing serial port ...',

        ports = self.list_serial_ports()
        if ports:
            result = ports[0]
            print colorize(result, 'yellow')
            return result

        print colorize('FAILED', 'red')
        raise Abort("No device matching following was found: %s" %
                    (''.join(['\n  - ' + p for p in self.serial_port_patterns()])))

    def process_args(self, args):
        arduino_dist = getattr(args, 'arduino_dist', None)
        if arduino_dist:
            self['arduino_dist_dir'] = arduino_dist

        board_model = getattr(args, 'board_model', None)
        if board_model:
            all_models = self.board_models()
            if board_model not in all_models:
                print "Supported Arduino board models are:"
                print all_models.format()
                raise Abort('%s is not a valid board model' % board_model)

        # Build artifacts for each Arduino distribution / Board model
        # pair should go to a separate subdirectory
        build_dirname = board_model or self.default_board_model
        if arduino_dist:
            hash = hashlib.md5(arduino_dist).hexdigest()[:8]
            build_dirname = '%s-%s' % (build_dirname, hash)

        self['build_dir'] = os.path.join(self.output_dir, build_dirname)

    @property
    def arduino_lib_version(self):
        self.find_arduino_file('version.txt', ['lib'],
                               human_name='Arduino lib version file (version.txt)')

        if 'arduino_lib_version' not in self:
            with open(self['version.txt']) as f:
                print 'Detecting Arduino software version ... ',
                v_string = f.read().strip()
                v = Version.parse(v_string)
                self['arduino_lib_version'] = v
                print colorize("%s (%s)" % (v, v_string), 'green')

        return self['arduino_lib_version']

class ArduinoData(OrderedDict):
    def parse(self, txtfile):
        with open(txtfile) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                multikey, val = line.split('=', 1)
                multikey = multikey.split('.')

                self.parse_key(multikey, val)

    def parse_key(self, multikey, val):
        self.parse_multikey(multikey, val)

    def parse_multikey(self, multikey, val, subdict=None):
        subdict = subdict or self
        last = None
        for key in multikey[:-1]:
            if key not in subdict:
                subdict[key] = {}
            last = subdict
            subdict = subdict[key]

        if isinstance(subdict, str):
            name = subdict
            subdict = last[name] = {'name': name}

        self.set_value(subdict, multikey[-1], val)

    def set_value(self, dict, key, val):
        dict[key] = val

    def format(self):
        map = [(key, val['name']) for key, val in self.iteritems()]
        head_width = reduce(lambda a, b: max(a, b), [len(k) for k in self.keys()])
        return format_available_options(map, head_width=head_width, default=self.default)

class BoardModels(ArduinoData):
    def __init__(self):
        super(BoardModels, self).__init__()
        self.cpus = {}

    def parse(self, txtfile, arch):
        super(BoardModels, self).parse(txtfile)
        for model in self.cpus:
            for cpu in self.cpus[model]:
                d = self[model + '_' + cpu] = dict(self.cpus[model][cpu])
                d['name'] = self[model]['name'] + ' w/ ' + d['name']
                u = dict(self[model])
                del u['name']

                d.update(u)

            del self[model]

        for model in self:
            if 'arch' not in self[model]:
                self[model]['arch'] = arch

    def parse_key(self, multikey, val):
        if multikey[0] == 'menu':
            self.parse_menu(multikey[1:], val)
            return

        super(BoardModels, self).parse_key(multikey, val)

    def parse_menu(self, menukey, val):
        if menukey[0] == 'cpu':
            if len(menukey) == 1:
                return
            model = menukey[1]
            cpu = menukey[2]

            if len(menukey) == 3:
                if model not in self.cpus:
                    self.cpus[model] = {}

                self.cpus[model][cpu] = {'name': val}
            else:
                self.parse_multikey(menukey[3:], val, subdict=self.cpus[model][cpu])
