# -*- coding: utf-8; -*-

import re
import os.path
import inspect
import subprocess
import platform
import jinja2

from jinja2.runtime import StrictUndefined

import ino.filters

from ino.commands.base import Command
from ino.environment import Version
from ino.filters import colorize
from ino.utils import SpaceList, list_subdirs
from ino.exc import Abort


class Build(Command):
    """
    Build a project in the current directory and produce a ready-to-upload
    firmware file.

    The project is expected to have a `src' subdirectroy where all its sources
    are located. This directory is scanned recursively to find
    *.[c|cpp|pde|ino] files. They are compiled and linked into resulting
    firmware hex-file.

    Also any external library dependencies are tracked automatically. If a
    source file includes any library found among standard Arduino libraries or
    a library placed in `lib' subdirectory of the project, the library gets
    build too.

    Build artifacts are placed in `.build' subdirectory of the project.
    """

    name = 'build'
    help_line = "Build firmware from the current directory project"

    def setup_arg_parser(self, parser):
        super(Build, self).setup_arg_parser(parser)
        self.e.add_board_model_arg(parser)
        self.e.add_arduino_dist_arg(parser)
        parser.add_argument('-v', '--verbose', default=False, action='store_true',
                            help='Verbose make output')

    def discover(self, board_key):
        self.board = board = self.e.board_model(board_key)

        cores_dir = ['hardware', 'arduino', 'cores', 'arduino']
        variants_dir = ['hardware', 'arduino', 'variants']
        if self.e.use_arduino15_dirs():
            cores_dir.insert(2, board['arch'])
            variants_dir.insert(2, board['arch'])

        self.e.find_arduino_dir('arduino_core_dir',
                                cores_dir,
                                ['Arduino.h'] if self.e.arduino_lib_version.major else ['WProgram.h'],
                                'Arduino core library')

        self.e.find_arduino_dir('arduino_libraries_dir', ['libraries'],
                                human_name='Arduino standard libraries')

        if self.e.arduino_lib_version.major:
            self.e.find_arduino_dir('arduino_variants_dir',
                                    variants_dir,
                                    human_name='Arduino variants directory')

        toolchain_prefix = 'avr-' if board['arch'] == 'avr' else 'arm-none-eabi-'

        toolset = [
            ('cc', toolchain_prefix + 'gcc'),
            ('cxx', toolchain_prefix + 'g++'),
            ('ar', toolchain_prefix + 'ar'),
            ('objcopy', toolchain_prefix + 'objcopy'),
        ]

        tools_dir = 'avr' if board['arch'] == 'avr' else 'g++_arm_none_eabi'
        toolchain_dir = ['hardware', 'tools', tools_dir, 'bin']
        for tool_key, tool_binary in toolset:
            self.e.find_arduino_tool(
                tool_key, toolchain_dir,
                items=[tool_binary], human_name=tool_binary)

    def setup_flags(self, board_key):
        board = self.e.board_model(board_key)
        if board['arch'] == 'avr':
            mcu = '-mmcu=' + board['build']['mcu']
        else:
            mcu = '-mcpu=' + board['build']['mcu']

        self.e['cflags'] = SpaceList([
            mcu,
            '-ffunction-sections',
            '-fdata-sections',
            '-g',
            '-Os',
            '-w',
            '-DF_CPU=' + board['build']['f_cpu'],
            '-DARDUINO=' + str(self.e.arduino_lib_version.as_int()),
            '-I' + self.e['arduino_core_dir'],
        ])

        if 'extra_flags' in board['build']:
            extra_flags = board['build']['extra_flags']
            extra_flags.replace('{build.vid}', board['build'].get('vid'))
            extra_flags.replace('{build.pid}', board['build'].get('pid'))
            self.e['cflags'].append(extra_flags)
        else:
            if 'vid' in board['build']:
                self.e['cflags'].append('-DUSB_VID=%s' % board['build']['vid'])
            if 'pid' in board['build']:
                self.e['cflags'].append('-DUSB_PID=%s' % board['build']['pid'])

        variant_dir = os.path.join(self.e.arduino_variants_dir,
                                   board['build']['variant'])

        if self.e.arduino_lib_version.major:
            self.e.cflags.append('-I' + variant_dir)

        self.e['cxxflags'] = SpaceList(['-fno-exceptions'])
        self.e['elfflags'] = SpaceList(['-Os', '-Wl,--gc-sections', mcu])

        if 'ldscript' in board['build']:
            self.e['elfflags'] += ' -T%s/%s' % (variant_dir, board['build']['ldscript'])

        self.e['libs'] = '-lm'
        if board['arch'] == 'sam':
            self.e['libs'] += ' -lgcc'

        self.e['names'] = {
            'obj': '%s.o',
            'lib': 'lib%s.a',
            'cpp': '%s.cpp',
            'deps': '%s.d',
        }

    def setup_flags15(self, board_key):
        board = self.e.board_model(board_key)
        platform = self.e.platform(board['arch'])

        arduino_dir = self.e.find_arduino_dir('arduino_base_dir', [''],
                                              human_name='Arduino base dir')

        if board['arch'] == 'sam':
            system_dir = self.e.find_arduino_dir('arduino_system_dir',
                                                 ['hardware', 'arduino', board['arch'], 'system'],
                                                 human_name='Arduino system dir')

        variant_dir = os.path.join(self.e.arduino_variants_dir,
                                   board['build']['variant'])

        recipe_vars = dict(platform)
        self.e['cflags'] = SpaceList([
            '-I' + self.e['arduino_core_dir'],
            '-I' + variant_dir,
        ])

        self.e['cxxflags'] = SpaceList([])
        recipe_vars['software'] = 'Arduino'
        recipe_vars['runtime'] = { 'ide': {
            'path': arduino_dir,
            'version': str(self.e.arduino_lib_version.as_int())
        }}

        recipe_vars['build'] = dict(platform['build'])
        recipe_vars['build'].update(board['build'])
        recipe_vars['build']['path'] = self.e.build_dir
        if board['arch'] == 'sam':
            recipe_vars['build']['system'] = { 'path': system_dir }

        recipe_vars['build']['variant'] = { 'path': variant_dir }
        recipe_vars['build']['project_name'] = 'firmware'
        if 'path' not in recipe_vars['compiler']:
            recipe_vars['compiler']['path'] = self.e.find_arduino_dir('avr_compiler_dir',
                                                                      ['hardware', 'tools', 'avr', 'bin'],
                                                                      human_name='AVR compiler dir') + '/'
        def deepiter(d, fn, prefix=''):
            for key, value in d.iteritems():
                if isinstance(value, dict):
                    deepiter(value, fn, prefix=prefix+key+'.')
                else:
                    fn(d, prefix+key, value)

        def store_recipe(parent, recipe_key, recipe):
            self.e['recipe.' + recipe_key] = self.e.replace_vars(recipe, **recipe_vars)
        deepiter(platform['recipe'], store_recipe)

        bin_suffix = 'bin' if board['arch'] == 'sam' else 'hex'
        self.e['bin_path'] = os.path.join(self.e.build_dir, 'firmware.%s' % bin_suffix)

        self.e['names'] = {
            'obj': '%s.o',
            'lib': 'lib%s.a',
            'cpp': '%s.cpp',
            'deps': '%s.d',
        }

    def create_jinja(self, verbose):
        templates_dir = os.path.join(os.path.dirname(__file__), '..', 'make')
        self.jenv = jinja2.Environment(
            loader=jinja2.FileSystemLoader(templates_dir),
            undefined=StrictUndefined, # bark on Undefined render
            extensions=['jinja2.ext.do'])

        # inject @filters from ino.filters
        for name, f in inspect.getmembers(ino.filters, lambda x: getattr(x, 'filter', False)):
            self.jenv.filters[name] = f

        # inject globals
        self.jenv.globals['e'] = self.e
        self.jenv.globals['v'] = '' if verbose else '@'
        self.jenv.globals['slash'] = os.path.sep
        self.jenv.globals['SpaceList'] = SpaceList

    def render_template(self, source, target, **ctx):
        template = self.jenv.get_template(source)
        contents = template.render(**ctx)
        out_path = os.path.join(self.e.build_dir, target)
        with open(out_path, 'wt') as f:
            f.write(contents)

        return out_path

    def make(self, makefile, **kwargs):
        makefile = self.render_template(makefile + '.jinja', makefile, **kwargs)
        ret = subprocess.call(['make', '-f', makefile, 'all'])
        if ret != 0:
            raise Abort("Make failed with code %s" % ret)

    def recursive_inc_lib_flags(self, libdirs):
        flags = SpaceList()
        for d in libdirs:
            flags.append('-I' + d)
            flags.extend('-I' + subd for subd in list_subdirs(d, recursive=True, exclude=['examples']))
        return flags

    def _scan_dependencies(self, dir, lib_dirs, inc_flags):
        output_filepath = os.path.join(self.e.build_dir, os.path.basename(dir), 'dependencies.d')
        makefile = 'Makefile15.deps' if self.e.use_arduino15_dirs() else 'Makefile.deps'
        self.make(makefile, inc_flags=inc_flags, src_dir=dir, output_filepath=output_filepath)
        self.e['deps'].append(output_filepath)

        # search for dependencies on libraries
        # for this scan dependency file generated by make
        # with regexes to find entries that start with
        # libraries dirname
        regexes = dict((lib, re.compile(r'\s' + lib + re.escape(os.path.sep))) for lib in lib_dirs)

        used_libs = set()
        with open(output_filepath) as f:
            for line in f:
                for lib, regex in regexes.iteritems():
                    if regex.search(line) and lib != dir:
                        used_libs.add(lib)

        return used_libs

    def scan_dependencies(self):
        self.e['deps'] = SpaceList()

        lib_dirs = [self.e.arduino_core_dir] + list_subdirs(self.e.lib_dir) + list_subdirs(self.e.arduino_libraries_dir)
        if self.e.use_arduino15_dirs():
            lib_dirs.append(os.path.join(self.e.arduino_variants_dir,
                                         self.board['build']['variant']))

        inc_flags = self.recursive_inc_lib_flags(lib_dirs)

        # If lib A depends on lib B it have to appear before B in final
        # list so that linker could link all together correctly
        # but order of `_scan_dependencies` is not defined, so...
        
        # 1. Get dependencies of sources in arbitrary order
        used_libs = list(self._scan_dependencies(self.e.src_dir, lib_dirs, inc_flags))

        # 2. Get dependencies of dependency libs themselves: existing dependencies
        # are moved to the end of list maintaining order, new dependencies are appended
        scanned_libs = set()
        while scanned_libs != set(used_libs):
            for lib in set(used_libs) - scanned_libs:
                dep_libs = self._scan_dependencies(lib, lib_dirs, inc_flags)

                i = 0
                for ulib in used_libs[:]:
                    if ulib in dep_libs:
                        # dependency lib used already, move it to the tail
                        used_libs.append(used_libs.pop(i))
                        dep_libs.remove(ulib)
                    else:
                        i += 1

                # append new dependencies to the tail
                used_libs.extend(dep_libs)
                scanned_libs.add(lib)

        self.e['used_libs'] = used_libs
        self.e['cflags'].extend(self.recursive_inc_lib_flags(used_libs))

    def run(self, args):
        self.discover(args.board_model)
        if self.e.use_arduino15_dirs():
            self.setup_flags15(args.board_model)
        else:
            self.setup_flags(args.board_model)

        self.create_jinja(verbose=args.verbose)
        self.make('Makefile.sketch')
        self.scan_dependencies()

        if self.e.use_arduino15_dirs():
            self.make('Makefile15')
        else:
            self.make('Makefile')
