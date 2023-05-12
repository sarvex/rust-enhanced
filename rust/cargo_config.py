"""Sublime commands for configuring Cargo execution.

See `cargo_settings` for more details on how settings work.
"""

import getpass
import os
import re
import sublime
import sublime_plugin
from .cargo_settings import CargoSettings, CARGO_COMMANDS
from .util import index_with, get_cargo_metadata
from . import rust_proc, util, log

# Keep track of recent choices to set the default value.
RECENT_CHOICES = {}


class CancelCommandError(Exception):
    """Raised when the command should stop."""


class CargoConfigBase(sublime_plugin.WindowCommand):

    """Base class for cargo config commands.

    This implements a simple interactive UI by asking the user a series of
    questions using the Sublime quick panels for selecting choices. Subclasses
    set the `sequence` class variable to the list of questions they want to
    ask.  The choices for each question are produced by methods starting with
    'items_'+name.  These methods should return a dictionary with:

    - `items`: List of choices.  Each element should be a tuple
      `(display_string, value)`.
    - `default`: The default value (optional).
    - `skip_if_one`: Skip this question if there is only 1 item.
    - `caption`: Instead of `items`, this is a string displayed with
      `show_input_panel` to allow the user to enter arbitrary text.

    `items_` methods can also just return the 'items' list.

    An optional method `selected_`+name will be called when a choice is made.
    This method can return a list of questions to be asked immediately.

    The `done` method is called once all questions have been asked.

    Callers are allowed to pass in values instead of using the interactive UI.
    This is probably only useful for the test code, but in theory you could
    define key bindings that perform certain actions.
    """

    # CargoSettings object.
    settings = None

    # Dictionary of choices passed into the command, instead of using
    # interactive UI.
    cmd_input = None

    # Sequence of questions to ask.
    sequence = None

    # Current question being asked.
    sequence_index = 0

    # Dictionary of selections made during the interactive process.
    choices = None

    # If True, the command wants the 'package' choice to fetch metadata from
    # Cargo.
    package_wants_metadata = True

    # If True, the 'package' choice will automatically use the manifest
    # from the active view if it is available.
    package_allows_active_view_shortcut = True

    # If True, 'which' will only allow you choose a package-specific setting.
    which_requires_package = False

    # This is a dictionary populated by the `items_package` method.
    # Key is the path to a package, the value is the metadata from Cargo.
    # This is used by other questions (like `items_target`) to get more
    # information about the chosen package.
    packages = None

    # Name of what is being configured.
    config_name = ""

    def run(self, **kwargs):
        self.choices = {}
        self.sequence_index = 0
        # Copy, since WindowCommand reuses objects.
        self._sequence = self.sequence[:]
        self.cmd_input = kwargs
        self.settings = CargoSettings(self.window)
        self.settings.load()
        self.show_next_question()

    def done(self):
        """Called once all questions have been asked.  Subclasses must
        implement this."""
        raise NotImplementedError()

    def show_next_question(self):
        if self.sequence_index < len(self._sequence):
            q = self._sequence[self.sequence_index]
            self.sequence_index += 1
        else:
            self.done()
            return

        f_selected = getattr(self, f'selected_{q}', None)

        # Called with the result of what the user selected.
        def make_choice(value):
            self.choices[q] = value
            if f_selected:
                try:
                    next = f_selected(value)
                except CancelCommandError:
                    return
                if next:
                    i = self.sequence_index
                    self._sequence[i:i] = next
            self.show_next_question()

        if q in self.cmd_input:
            make_choice(self.cmd_input[q])
        else:
            try:
                item_info = getattr(self, f'items_{q}')()
            except CancelCommandError:
                return
            if not isinstance(item_info, dict):
                item_info = {'items': item_info}

            if 'items' in item_info:
                def wrapper(index):
                    if index != -1:
                        chosen = item_info['items'][index][1]
                        RECENT_CHOICES[q] = chosen
                        make_choice(chosen)

                items = item_info['items']
                if item_info.get('skip_if_one', False) and len(items) == 1:
                    wrapper(0)
                else:
                    # If the user manually edits the config and enters custom
                    # values then it won't show up in the list (because it is
                    # not an exact match).  Add it so that it is a valid
                    # choice (assuming the user entered a valid value).
                    if 'default' in item_info:
                        default_index = index_with(items,
                            lambda x: x[1] == item_info['default'])
                        if default_index == -1:
                            items.append((item_info['default'],
                                          item_info['default']))
                    # Determine the default selection.
                    # Use the default provided by the items_ method, else
                    # use the most recently used value.
                    default = index_with(items,
                        lambda x: x[1] == item_info.get('default',
                            RECENT_CHOICES.get(q, '_NO_DEFAULT_SENTINEL_')))
                    display_items = [x[0] for x in items]
                    self.window.show_quick_panel(display_items, wrapper, 0,
                                                 default)
            elif 'caption' in item_info:
                self.window.show_input_panel(item_info['caption'],
                                             item_info.get('default', ''),
                                             make_choice, None, None)
            else:
                raise ValueError(item_info)

    def items_package(self):
        view = self.window.active_view()
        if self.package_allows_active_view_shortcut and view.file_name():
            # If there is a manifest under the current view, use that by
            # default.
            manifest_dir = util.find_cargo_manifest(view.file_name())
            if manifest_dir:
                if self.package_wants_metadata:
                    metadata = get_cargo_metadata(self.window, manifest_dir)
                    if metadata:
                        for package in metadata['packages']:
                            package_dir = os.path.dirname(
                                package['manifest_path'])
                            if package_dir == manifest_dir:
                                self.packages = {
                                    manifest_dir: package
                                }
                return {
                    'items': [(manifest_dir, manifest_dir)],
                    'skip_if_one': True,
                }

        # Otherwise, hunt for all manifest files and show a list.
        folders = self.window.folders()
        self.packages = {}
        for folder in folders:
            folder_parent = os.path.dirname(folder)
            for dirpath, dirs, files, in os.walk(folder):
                for exclude in ('.git', '.svn'):
                    if exclude in dirs:
                        dirs.remove(exclude)
                if 'Cargo.toml' in files:
                    metadata = get_cargo_metadata(self.window, dirpath)
                    if metadata:
                        for package in metadata['packages']:
                            manifest_dir = os.path.dirname(package['manifest_path'])
                            rel = os.path.relpath(manifest_dir, folder_parent)
                            package['sublime_relative'] = rel
                            if manifest_dir not in self.packages:
                                self.packages[manifest_dir] = package
                    else:
                        # Manifest load failure, let it slide.
                        log.critical(self.window,
                            'Failed to load Cargo manifest in %r', dirpath)

        if not self.packages:
            sublime.error_message(util.multiline_fix("""
                Error: Cannot determine Rust package to use.

                Open a Rust file to determine which package to use, or add a folder with a Cargo.toml file to your Sublime project."""))
            raise CancelCommandError

        def display_name(package):
            return [f"Package: {package['name']}", package['sublime_relative']]

        items = [(display_name(package), path)
            for path, package in self.packages.items()]
        items.sort(key=lambda x: x[0])
        return {
            'items': items,
            'skip_if_one': True,
        }

    def items_target(self):
        """Choosing a target requires that 'package' has already been chosen."""
        # Group by kind.
        kinds = {}
        package_path = self.choices['package']
        for target in self.packages[package_path]['targets']:
            # AFAIK, when there are multiple "kind" values, this only happens
            # when there are multiple library kinds.
            kind = target['kind'][0]
            if kind in ('lib', 'rlib', 'dylib', 'cdylib', 'staticlib', 'proc-macro'):
                kinds.setdefault('lib', []).append(('Lib', '--lib'))
            elif kind in ('bin', 'test', 'example', 'bench'):
                text = f"{kind.capitalize()}: {target['name']}"
                arg = f"--{kind} {target['name']}"
                kinds.setdefault(kind, []).append((text, arg))
            elif kind not in ('custom-build',):
                log.critical(self.window,
                    'Rust: Unsupported target found: %s', kind)
        items = []
        for kind, values in kinds.items():
            allowed = True
            if self.choices.get('variant', None):
                cmd = CARGO_COMMANDS[self.choices['variant']]
                target_types = cmd['allows_target']
                if target_types is not True:
                    allowed = kind in target_types
            if allowed:
                items.extend(values)
        if not items:
            sublime.error_message('Could not determine available targets.')
        return items

    def items_variant(self):
        result = [
            (info['name'], key)
            for key, info in CARGO_COMMANDS.items()
            if self.filter_variant(info)
        ]
        result.sort()
        return result

    def filter_variant(self, x):
        """Subclasses override this to filter variants from the variant
        list."""
        return True

    def items_which(self):
        """Choice to select at which level the setting should be saved at."""
        # This is a bit of a hack so that when called programmatically you
        # don't have to specify 'which'.
        if 'which' not in self.cmd_input:
            if 'variant' in self.cmd_input:
                self.cmd_input['which'] = 'project_package_variant'
            elif 'target' in self.cmd_input:
                self.cmd_input['which'] = 'project_package_target'

        variant_extra = 'cargo build, cargo run, cargo test, etc.'
        target_extra = '--bin, --example, --test, etc.'
        result = []
        if not self.which_requires_package:
            result.extend([
                (['Set %s globally.', 'Updates RustEnhanced.sublime-settings'],
                    'global_default'),
                (['Set %s in this Sublime project.', ''],
                    'project_default'),
                (['Set %s globally for a Build Variant.', variant_extra],
                    'global_variant'),
                (['Set %s in this Sublime project for a Build Variant (all Cargo packages).', variant_extra],
                    'project_variant'),
            ])
        result.extend([
            (['Set %s in this Sublime project for all commands (specific Cargo package).', ''],
                'project_package_default'),
            (['Set %s in this Sublime project for a Build Variant (specific Cargo package).', variant_extra],
                'project_package_variant'),
            (['Set %s in this Sublime project for a Target (specific Cargo package).', target_extra],
                'project_package_target'),
        ])
        for (text, _) in result:
            text[0] = text[0] % (self.config_name,)
        return result

    def selected_which(self, which):
        if which in ('project_variant', 'global_variant'):
            return ['variant']
        elif which == 'project_package_default':
            return ['package']
        elif which == 'project_package_variant':
            return ['package', 'variant']
        elif which == 'project_package_target':
            return ['package', 'target']

    def get_setting(self, name, default=None):
        """Retrieve a setting, honoring the "which" selection."""
        w = self.choices['which']
        if w == 'global_default':
            return self.settings.get_global_default(name, default)
        elif w == 'project_default':
            return self.settings.get_project_default(name, default)
        elif w == 'global_variant':
            return self.settings.get_global_variant(self.choices['variant'],
                                                    name, default)
        elif w == 'project_variant':
            return self.settings.get_project_variant(self.choices['variant'],
                                                     name, default)
        elif w == 'project_package_default':
            return self.settings.get_project_package_default(
                self.choices['package'], name, default)
        elif w == 'project_package_variant':
            return self.settings.get_project_package_variant(
                self.choices['package'], self.choices['variant'], name, default)
        elif w == 'project_package_target':
            return self.settings.get_project_package_target(
                self.choices['package'], self.choices['target'], name, default)
        else:
            raise AssertionError(w)

    def set_setting(self, name, value):
        """Set a setting, honoring the "which" selection."""
        w = self.choices['which']
        if w == 'global_default':
            return self.settings.set_global_default(name, value)
        elif w == 'project_default':
            return self.settings.set_project_default(name, value)
        elif w == 'global_variant':
            return self.settings.set_global_variant(self.choices['variant'],
                                                    name, value)
        elif w == 'project_variant':
            return self.settings.set_project_variant(self.choices['variant'],
                                                     name, value)
        elif w == 'project_package_default':
            return self.settings.set_project_package_default(
                self.choices['package'], name, value)
        elif w == 'project_package_variant':
            return self.settings.set_project_package_variant(
                self.choices['package'], self.choices['variant'], name, value)
        elif w == 'project_package_target':
            return self.settings.set_project_package_target(
                self.choices['package'], self.choices['target'], name, value)
        else:
            raise AssertionError(w)

    toolchain_allows_default = True

    def items_toolchain(self):
        items = []
        if self.toolchain_allows_default:
            items.append(('Use Default Toolchain', None))
        toolchains = self._toolchain_list()
        current = self.get_setting('toolchain')
        items.extend([(x, x) for x in toolchains])
        result = {
            'items': items,
        }
        if self.toolchain_allows_default or current:
            result['default'] = current
        return result

    def _toolchain_list(self):
        output = rust_proc.check_output(self.window,
                                        'rustup toolchain list'.split(),
                                        None)
        output = output.splitlines()
        system_default = index_with(output, lambda x: x.endswith(' (default)'))
        if system_default != -1:
            # Strip the " (default)" text.
            output[system_default] = output[system_default][:-10]
        # Rustup supports some shorthand of either `channel` or `channel-date`
        # without the trailing target info.
        #
        # Complete list of available toolchains is available at:
        # https://static.rust-lang.org/dist/index.html
        # (See https://github.com/rust-lang-nursery/rustup.rs/issues/215)
        shorthands = []
        channels = ['nightly', 'beta', 'stable', '\d\.\d{1,2}\.\d']
        pattern = '(%s)(?:-(\d{4}-\d{2}-\d{2}))?(?:-(.*))' % '|'.join(channels)
        for toolchain in output:
            if m := re.match(pattern, toolchain):
                channel = m[1]
                shorthand = f'{channel}-{date}' if (date := m[2]) else channel
                if shorthand not in shorthands:
                    shorthands.append(shorthand)
        result = shorthands + output
        result.sort()
        return result


class CargoConfigPackage(CargoConfigBase):

    """This is a fake command used by cargo_build to reuse the code to choose
    a Cargo package."""

    config_name = 'Package'
    sequence = ['package']
    package_wants_metadata = False

    def run(self, on_done):
        self._on_done = on_done
        super(CargoConfigPackage, self).run()

    def done(self):
        self._on_done(self.choices['package'])


class CargoSetProfile(CargoConfigBase):

    config_name = 'Profile'
    sequence = ['which', 'profile']

    def items_profile(self):
        default = self.get_setting('release', False)
        default = 'release' if default else 'dev'
        items = [('Dev', 'dev'),
                 ('Release', 'release')]
        return {'items': items,
                'default': default}

    def done(self):
        self.set_setting('release', self.choices['profile'] == 'release')


class CargoSetTarget(CargoConfigBase):

    config_name = 'Target'
    sequence = ['package', 'variant', 'target']

    def filter_variant(self, info):
        return super(CargoSetTarget, self).filter_variant(info) and \
            info.get('allows_target', False)

    def items_target(self):
        items = super(CargoSetTarget, self).items_target()
        items.insert(0, ('Automatic Detection', 'auto'))
        default = self.settings.get_project_package_variant(
            self.choices['package'], self.choices['variant'], 'target')
        result = {
            'items': items
        }
        if default:
            result['default'] = default
        return result

    def done(self):
        self.settings.set_project_package_variant(self.choices['package'],
                                                  self.choices['variant'],
                                                  'target',
                                                  self.choices['target'])


class CargoSetTriple(CargoConfigBase):

    config_name = 'Triple'
    sequence = ['which', 'toolchain', 'target_triple']
    toolchain_allows_default = False

    def items_target_triple(self):
        # Could check if rustup is not installed, to run
        # "rustc --print target-list", but that does not tell
        # us which targets are installed.

        # The target list depends on the toolchain used.
        cmd = f"rustup target list --toolchain={self.choices['toolchain']}"
        triples = rust_proc.check_output(self.window, cmd.split(), None)\
                .splitlines()
        current = self.get_setting('target_triple')
        result = [('Use Default', None)]
        for triple in triples:
            if triple.endswith(' (default)'):
                actual_triple = triple[:-10]
                result.append((actual_triple, actual_triple))
            elif triple.endswith(' (installed)'):
                actual_triple = triple[:-12]
                result.append((actual_triple, actual_triple))
            else:
                actual_triple = None
            # Don't bother listing uninstalled targets.
        return {
            'items': result,
            'default': current
        }

    def done(self):
        self.set_setting('target_triple', self.choices['target_triple'])


class CargoSetToolchain(CargoConfigBase):

    config_name = 'Toolchain'
    sequence = ['which', 'toolchain']

    def done(self):
        self.set_setting('toolchain', self.choices['toolchain'])


class CargoSetFeatures(CargoConfigBase):

    config_name = 'Features'
    sequence = ['which', 'no_default_features', 'features']
    which_requires_package = True

    def items_no_default_features(self):
        current = self.get_setting('no_default_features', False)
        items = [
            ('Include default features.', False),
            ('Do not include default features.', True)
        ]
        return {
            'items': items,
            'default': current,
        }

    def items_features(self):
        features = self.get_setting('features', None)
        if features is None:
            # Detect available features from the manifest.
            package_path = self.choices['package']
            available_features = self.packages[package_path].get('features', {})
            items = list(available_features.keys())
            # Remove the "default" entry.
            if 'default' in items:
                del items[items.index('default')]
                if not self.choices['no_default_features']:
                    # Don't show default features, (they are already included).
                    for ft in available_features['default']:
                        if ft in items:
                            del items[items.index(ft)]
            features = ' '.join(items)
        return {
            'caption': 'Choose features (space separated, use "ALL" to use all features)',
            'default': features,
        }

    def done(self):
        self.set_setting('no_default_features',
                         self.choices['no_default_features'])
        self.set_setting('features', self.choices['features'])


class CargoSetDefaultPath(CargoConfigBase):

    config_name = 'Default Path'
    sequence = ['package']
    package_allows_active_view_shortcut = False

    def items_package(self):
        result = super(CargoSetDefaultPath, self).items_package()
        items = result['items']
        items.insert(0, (['No Default',
            'Build will attempt to detect from the current view, or pop up a selection panel.'],
             None))
        result['default'] = self.settings.get_project_base('default_path')
        return result

    def done(self):
        self.settings.set_project_base('default_path', self.choices['package'])


class CargoSetEnvironmentEditor(CargoConfigBase):

    config_name = 'Environment'
    sequence = ['which']

    def done(self):
        view = self.window.new_file()
        view.set_scratch(True)
        default = self.get_setting('env')
        template = util.multiline_fix("""
               // Enter environment variables here in JSON syntax.
               // Close this view when done to commit the settings.
               """)
        if 'contents' in self.cmd_input:
            # Used when parsing fails to attempt to edit again.
            template = self.cmd_input['contents']
        elif default:
            template += sublime.encode_value(default, True)
        else:
            template += util.multiline_fix("""
                {
                    // "RUST_BACKTRACE": "1"
                }
                """)
        # Unfortunately Sublime indents on 'insert'
        view.settings().set('auto_indent', False)
        view.run_command('insert', {'characters': template})
        view.settings().set('auto_indent', True)
        view.set_syntax_file('Packages/JavaScript/JSON.sublime-syntax')
        view.settings().set('rust_environment_editor', True)
        view.settings().set('rust_environment_editor_settings', {
            'package': self.choices.get('package'),
            'which': self.choices['which'],
            'variant': self.choices.get('variant'),
            'target': self.choices.get('target'),
        })


class CargoSetEnvironment(CargoConfigBase):

    """Special command that should not be run interactively.  Used by the
    on-close callback to actually set the environment."""

    config_name = 'Environment'
    sequence = ['which', 'env']

    def items_env(self):
        return []

    def done(self):
        self.set_setting('env', self.choices['env'])


class EnvironmentSaveHandler(sublime_plugin.EventListener):

    """Handler for when the view is closed on the environment editor."""

    def on_pre_close(self, view):
        if not view.settings().get('rust_environment_editor'):
            return
        settings = view.settings().get('rust_environment_editor_settings')

        contents = view.substr(sublime.Region(0, view.size()))
        try:
            result = sublime.decode_value(contents)
        except:
            sublime.error_message('Value was not valid JSON, try again.')
            view.window().run_command('cargo_set_environment_editor', {
                'package': settings.get('package'),
                'which': settings['which'],
                'variant': settings.get('variant'),
                'target': settings.get('target'),
                'contents': contents,
            })
            return

        view.window().run_command('cargo_set_environment', {
            'package': settings.get('package'),
            'which': settings['which'],
            'variant': settings.get('variant'),
            'target': settings.get('target'),
            'env': result,
        })


class CargoSetArguments(CargoConfigBase):

    config_name = 'Extra Command-line Arguments'
    sequence = ['which', 'before_after', 'args']

    def items_before_after(self):
        return [
            ('Enter extra Cargo arguments (before -- separator)', 'extra_cargo_args'),
            ('Enter extra Cargo arguments (after -- separator)', 'extra_run_args'),
        ]

    def items_args(self):
        current = self.get_setting(self.choices['before_after'], '')
        return {
            'caption': 'Enter the extra Cargo args',
            'default': current,
        }

    def done(self):
        self.set_setting(self.choices['before_after'],
                         self.choices['args'])


class CargoConfigure(CargoConfigBase):

    sequence = ['config_option']

    def items_config_option(self):
        return [
            (['Set Target', '--bin, --lib, --example, etc.'], 'target'),
            (['Set Profile', '--release flag'], 'profile'),
            (['Set Target Triple', '--target flag'], 'triple'),
            (['Set Rust Toolchain', 'nightly vs stable, etc.'], 'toolchain'),
            (['Set Features', 'Cargo build features with --features'], 'features'),
            (['Set Environment Variables', ''], 'environment'),
            (['Set Extra Cargo Arguments', ''], 'args'),
            (['Set Default Package/Path', ''], 'package'),
        ]

    def selected_config_option(self, which):
        if which == 'target':
            CargoSetTarget(self.window).run()
        elif which == 'profile':
            CargoSetProfile(self.window).run()
        elif which == 'triple':
            CargoSetTriple(self.window).run()
        elif which == 'toolchain':
            CargoSetToolchain(self.window).run()
        elif which == 'features':
            CargoSetFeatures(self.window).run()
        elif which == 'environment':
            CargoSetEnvironmentEditor(self.window).run()
        elif which == 'args':
            CargoSetArguments(self.window).run()
        elif which == 'package':
            CargoSetDefaultPath(self.window).run()
        else:
            raise AssertionError(which)

    def done(self):
        pass


class CargoCreateNewBuild(CargoConfigBase):

    """Command to create a new build variant, stored in the user's
    `.sublime-project` file."""

    config_name = 'New Build'
    sequence = ['command']

    def items_command(self):
        if self.window.project_data() is None:
            sublime.error_message(util.multiline_fix("""
                Error: This command requires a .sublime-project file.

                Save your Sublime project and try again."""))
            raise CancelCommandError
        result = [
            (info['name'], key)
            for key, info in CARGO_COMMANDS.items()
            if self.filter_variant(info)
        ]
        result.sort()
        result.append(('New Command', 'NEW_COMMAND'))
        return result

    def selected_command(self, command):
        if command == 'NEW_COMMAND':
            return ['new_command', 'allows_target', 'allows_target_triple',
                'allows_release', 'allows_features', 'allows_json',
                'requires_manifest', 'requires_view_path', 'wants_run_args',
                'name']
        cinfo = CARGO_COMMANDS[command]
        result = []
        if cinfo.get('requires_manifest', True):
            result.append('package')
        result.append('name')
        return result

    def items_package(self):
        result = super(CargoCreateNewBuild, self).items_package()
        if len(result['items']) > 1:
            result['items'].insert(0, (['Any Package',
                'This build variant is not tied to any particular Cargo package.'],
                None))
        return result

    def selected_package(self, package):
        if package:
            cinfo = CARGO_COMMANDS[self.choices['command']]
            if cinfo.get('allows_target', False):
                return ['target']

    def items_new_command(self):
        return {
            'caption': 'Enter the Cargo subcommand to run:',
        }

    def selected_new_command(self, command):
        if not command:
            sublime.error_message('Error: You must enter a command to run.')
            raise CancelCommandError

    def items_allows_target(self):
        return [
            ('Command %r supports Cargo filters (--bin, --example, etc.)' % (
                self.choices['new_command']), True),
            ('Command %r does not support target filters' % (
                self.choices['new_command'],), False)
        ]

    def items_allows_target_triple(self):
        return [
            ('Command %r supports --target triple flag' % (
                self.choices['new_command']), True),
            ('Command %r does not support --target' % (
                self.choices['new_command'],), False)
        ]

    def items_allows_release(self):
        return [
            ('Command %r supports --release flag' % (
                self.choices['new_command']), True),
            ('Command %r does not support --release' % (
                self.choices['new_command'],), False)
        ]

    def items_allows_features(self):
        return [
            ('Command %r supports --features flag' % (
                self.choices['new_command']), True),
            ('Command %r does not support --features' % (
                self.choices['new_command'],), False)
        ]

    def items_allows_json(self):
        return [
            ('Command %r supports --message-format=json flag' % (
                self.choices['new_command']), True),
            ('Command %r does not support JSON' % (
                self.choices['new_command'],), False)
        ]

    def items_requires_manifest(self):
        return [
            ('Command %r requires a Cargo.toml manifest' % (
                self.choices['new_command']), True),
            ('Command %r does not require a manifest' % (
                self.choices['new_command'],), False)
        ]

    def items_requires_view_path(self):
        return [
            ('Do not include view path', False),
            ('Include path of active sublime view on command line', True),
        ]

    def items_wants_run_args(self):
        return [
            ('Do not ask for more arguments', False),
            ('Ask for extra command-line arguments each time', True),
        ]

    def items_name(self):
        name = '%s\'s %s' % (getpass.getuser(),
            self.choices.get('new_command', self.choices['command']))
        if target := self.choices.get('target', None):
            target = target.replace('-', '')
            name = f'{name} {target}'
        return {
            'caption': 'Enter a name for your new Cargo build system:',
            'default': name
        }

    def selected_name(self, name):
        if not name:
            sublime.error_message('Error: You must enter a name.')
            raise CancelCommandError

    def done(self):
        proj_data = self.window.project_data()
        systems = proj_data.setdefault('build_systems', [])
        for system_index, system in enumerate(systems):
            if system.get('target') == 'cargo_exec':
                break
        else:
            system = self._stock_build_system()
            system['name'] = 'Custom Cargo Build'
            system_index = len(systems)
            systems.append(system)
        variants = system.setdefault('variants', [])

        # Add the defaults to make it easier to manually edit.
        settings = {
            'release': False,
            'target_triple': '',
            'toolchain': '',
            'target': '',
            'no_default_features': False,
            'features': '',
            'extra_cargo_args': '',
            'extra_run_args': '',
            'env': {},
        }
        cinfo = {}
        result = {
            'name': self.choices['name'],
            'target': 'cargo_exec',
            'command': self.choices.get('new_command',
                                        self.choices['command']),
            'settings': settings,
            'command_info': cinfo,
        }
        if self.choices['command'] == 'NEW_COMMAND':
            for key in ['allows_target', 'allows_target_triple',
                        'allows_release', 'allows_features', 'allows_json',
                        'requires_manifest', 'requires_view_path',
                        'wants_run_args']:
                cinfo[key] = self.choices[key]
            requires_view_path = cinfo.get('requires_view_path')
        else:
            if 'target' in self.choices:
                settings['target'] = self.choices['target']
            if 'package' in self.choices:
                settings['working_dir'] = self.choices['package']
            requires_view_path = CARGO_COMMANDS[self.choices['command']]\
                .get('requires_view_path', False)

        if requires_view_path and util.active_view_is_rust():
            settings['script_path'] = self.window.active_view().file_name()

        variants.insert(0, result)
        self.window.set_project_data(proj_data)
        self.window.run_command('set_build_system', {'index': system_index})

    def _stock_build_system(self):
        pkg_name = __name__.split('.')[0]
        resource = f'Packages/{pkg_name}/RustEnhanced.sublime-build'
        return sublime.decode_value(sublime.load_resource(resource))
