"""Sublime commands for the cargo build system."""

import functools
import sublime
import sublime_plugin
import sys
from .rust import (rust_proc, rust_thread, opanel, util, messages,
                   cargo_settings, target_detect)
from .rust.cargo_config import *
from .rust.log import (log, clear_log, RustOpenLog, RustLogEvent)

# Maps command to an input string. Used to pre-populate the input panel with
# the last entered value.
LAST_EXTRA_ARGS = {}


class CargoExecCommand(sublime_plugin.WindowCommand):

    """cargo_exec Sublime command.

    This takes the following arguments:

    - `command`: The command name to run.  Commands are defined in the
      `cargo_settings` module.  You can define your own custom command by
      passing in `command_info`.
    - `command_info`: Dictionary of values the defines how the cargo command
      is constructed.  See `cargo_settings.CARGO_COMMANDS`.
    - `settings`: Dictionary of settings overriding anything set in the
      Sublime project settings (see `cargo_settings` module).
    """

    # The combined command info from `cargo_settings` and whatever the user
    # passed in.
    command_info = None
    # Dictionary of initial settings passed in by the user.
    initial_settings = None
    # CargoSettings instance.
    settings = None
    # Directory where to run the command.
    working_dir = None
    # Path used for the settings key.  This is typically `working_dir` except
    # for `cargo script`, in which case it is the path to the .rs source file.
    settings_path = None

    def run(self, command=None, command_info=None, settings=None):
        if command is None:
            return self.window.run_command('build', {'select': True})
        clear_log(self.window)
        self.initial_settings = settings if settings else {}
        self.settings = cargo_settings.CargoSettings(self.window)
        self.settings.load()
        if command == 'auto':
            self._detect_auto_build()
        else:
            self.command_name = command
            self.command_info = cargo_settings.CARGO_COMMANDS\
                .get(command, {}).copy()
            if command_info:
                self.command_info.update(command_info)
            self._determine_working_path(self._run_check_for_args)

    def _detect_auto_build(self):
        """Handle the "auto" build variant, which automatically picks a build
        command based on the current view."""
        if not util.active_view_is_rust():
            sublime.error_message(util.multiline_fix("""
                Error: Could not determine what to build.

                Open a Rust source file as the active Sublime view.
            """))
            return
        td = target_detect.TargetDetector(self.window)
        view = self.window.active_view()
        targets = td.determine_targets(view.file_name())
        if len(targets) == 0:
            sublime.error_message(util.multiline_fix("""
                Error: Could not determine what to build.

                Try using one of the explicit build variants.
            """))
            return

        elif len(targets) == 1:
            self._auto_choice_made(targets, 0)

        else:
            # Can't determine a single target, let the user choose one.
            targets.sort()
            display_items = [' '.join(x[1]) for x in targets]
            on_done = functools.partial(self._auto_choice_made, targets)
            self.window.show_quick_panel(display_items, on_done)

    def _auto_choice_made(self, targets, index):
        if index != -1:
            src_path, cmd_line = targets[index]
            actions = {
                '--bin': 'run',
                '--example': 'run',
                '--lib': 'build',
                '--bench': 'bench',
                '--test': 'test',
            }
            cmd = actions[cmd_line[0]]
            self.initial_settings['target'] = ' '.join(cmd_line)
            self.run(command=cmd, settings=self.initial_settings)

    def _determine_working_path(self, on_done):
        """Determine where Cargo should be run.

        This may trigger some Sublime user interaction if necessary.
        """
        if working_dir := self.initial_settings.get('working_dir'):
            self.working_dir = working_dir
            self.settings_path = working_dir
            return on_done()

        if script_path := self.initial_settings.get('script_path'):
            self.working_dir = os.path.dirname(script_path)
            self.settings_path = script_path
            return on_done()

        if default_path := self.settings.get_project_base('default_path'):
            self.settings_path = default_path
            if os.path.isfile(default_path):
                self.working_dir = os.path.dirname(default_path)
            else:
                self.working_dir = default_path
            return on_done()

        if self.command_info.get('requires_manifest', True):
            cmd = CargoConfigPackage(self.window)
            cmd.run(functools.partial(self._on_manifest_choice, on_done))
        else:
            # For now, assume you need a Rust file if not needing a manifest
            # (for `cargo script`).
            view = self.window.active_view()
            if util.active_view_is_rust(view=view):
                self.settings_path = view.file_name()
                self.working_dir = os.path.dirname(self.settings_path)
                return on_done()
            else:
                sublime.error_message(util.multiline_fix("""
                    Error: Could not determine what Rust source file to use.

                    Open a Rust source file as the active Sublime view."""))
                return

    def _on_manifest_choice(self, on_done, package_path):
        self.settings_path = package_path
        self.working_dir = package_path
        on_done()

    def _run_check_for_args(self):
        if self.command_info.get('wants_run_args', False) and \
                not self.initial_settings.get('extra_run_args'):
            self.window.show_input_panel('Enter extra args:',
                LAST_EXTRA_ARGS.get(self.command_name, ''),
                self._on_extra_args, None, None)
        else:
            self._run()

    def _on_extra_args(self, args):
        LAST_EXTRA_ARGS[self.command_info['command']] = args
        self.initial_settings['extra_run_args'] = args
        self._run()

    def _run(self):
        t = CargoExecThread(self.window, self.settings,
                            self.command_name, self.command_info,
                            self.initial_settings,
                            self.settings_path, self.working_dir)
        t.start()


class CargoExecThread(rust_thread.RustThread):

    silently_interruptible = False
    name = 'Cargo Exec'

    def __init__(self, window, settings,
                 command_name, command_info,
                 initial_settings, settings_path, working_dir):
        super(CargoExecThread, self).__init__(window)
        self.settings = settings
        self.command_name = command_name
        self.command_info = command_info
        self.initial_settings = initial_settings
        self.settings_path = settings_path
        self.working_dir = working_dir

    def run(self):
        cmd = self.settings.get_command(self.command_name,
                                        self.command_info,
                                        self.settings_path,
                                        self.working_dir,
                                        self.initial_settings)
        if not cmd:
            return
        messages.clear_messages(self.window)
        p = rust_proc.RustProc()
        listener = opanel.OutputListener(self.window, cmd['msg_rel_path'],
                                         self.command_name,
                                         cmd['rustc_version'])
        decode_json = util.get_setting('show_errors_inline', True) and \
            self.command_info.get('allows_json', False)
        try:
            p.run(self.window, cmd['command'],
                  self.working_dir, listener,
                  env=cmd['env'],
                  decode_json=decode_json,
                  json_stop_pattern=self.command_info.get('json_stop_pattern'))
            p.wait()
        except rust_proc.ProcessTerminatedError:
            return


# This is used by the test code.  Due to the async nature of the on_load event,
# it can cause problems with the rapid loading of views.
ON_LOAD_MESSAGES_ENABLED = True


class MessagesViewEventListener(sublime_plugin.ViewEventListener):

    """Every time a new file is loaded, check if is a Rust file with messages,
    and if so, display the messages.
    """

    @classmethod
    def is_applicable(cls, settings):
        return ON_LOAD_MESSAGES_ENABLED and util.is_rust_view(settings)

    @classmethod
    def applies_to_primary_view_only(cls):
        return False

    def on_load_async(self):
        messages.show_messages_for_view(self.view)


class NextPrevBase(sublime_plugin.WindowCommand):

    def _has_inline(self):
        return self.window.id() in messages.WINDOW_MESSAGES


class RustNextMessageCommand(NextPrevBase):

    def run(self, levels='all'):
        if self._has_inline():
            messages.show_next_message(self.window, levels)
        else:
            self.window.run_command('next_result')


class RustPrevMessageCommand(NextPrevBase):

    def run(self, levels='all'):
        if self._has_inline():
            messages.show_prev_message(self.window, levels)
        else:
            self.window.run_command('prev_result')


class RustCancelCommand(sublime_plugin.WindowCommand):

    def run(self):
        try:
            t = rust_thread.THREADS[self.window.id()]
        except KeyError:
            pass
        else:
            t.terminate()
        # Also call Sublime's cancel command, in case the user is using a
        # normal Sublime build.
        self.window.run_command('cancel_build')


class RustDismissMessagesCommand(sublime_plugin.WindowCommand):

    """Removes all inline messages."""

    def run(self):
        messages.clear_messages(self.window, soft=True)


class RustListMessagesCommand(sublime_plugin.WindowCommand):

    """Shows a quick panel with a list of all messages."""

    def run(self):
        messages.list_messages(self.window)


# Patterns used to help find test function names.
# This is far from perfect, but should be good enough.
SPACE = r'[ \t]'
OPT_COMMENT = r"""(?:
    (?: [ \t]* //.*)
  | (?: [ \t]* /\*.*\*/ [ \t]* )
)?"""
IDENT = r"""(?:
    [a-z A-Z] [a-z A-Z 0-9 _]*
  | _         [a-z A-Z 0-9 _]+
)"""
TEST_PATTERN = r"""(?x)
    {SPACE}* \# {SPACE}* \[ {SPACE}* {WHAT} {SPACE}* \] {SPACE}*
    (?:
        (?: {SPACE}* \#\[  [^]]+  \] {OPT_COMMENT} \n )
      | (?: {OPT_COMMENT} \n )
    )*
    .* fn {SPACE}+ ({IDENT}+)
"""


def _target_to_test(what, view, on_done):
    """Helper used to determine build target from given view."""
    td = target_detect.TargetDetector(view.window())
    targets = td.determine_targets(view.file_name())
    if len(targets) == 0:
        sublime.error_message(f'Error: Could not determine target to {what}.')
    elif len(targets) == 1:
        on_done(' '.join(targets[0][1]))
    else:
        # Can't determine a single target, let the user choose one.
        display_items = [' '.join(x[1]) for x in targets]

        def quick_on_done(idx):
            on_done(targets[idx][1])

        view.window().show_quick_panel(display_items, quick_on_done)


def _pt_to_test_name(what, pt, view):
    """Helper used to convert Sublime point to a test/bench function name."""
    fn_names = []
    pat = TEST_PATTERN.format(WHAT=what, **globals())
    regions = view.find_all(pat, 0, r'\1', fn_names)
    if not regions:
        sublime.error_message(f'Could not find a Rust {what} function.')
        return None
    # Assuming regions are in ascending order.
    indices = [i for (i, r) in enumerate(regions) if r.a <= pt]
    if not indices:
        sublime.error_message(f'No {what} functions found about the current point.')
        return None
    return fn_names[indices[-1]]


def _cargo_test_pt(what, pt, view):
    """Helper used to run a test for a given point in the given view."""
    def do_test(target):
        test_fn_name = _pt_to_test_name(what, pt, view)
        if test_fn_name:
            view.window().run_command(
                'cargo_exec',
                args={
                    'command': what,
                    'settings': {
                        'target': target,
                        'extra_run_args': f'--exact {test_fn_name}',
                    },
                },
            )

    _target_to_test(what, view, do_test)


class CargoHere(sublime_plugin.WindowCommand):

    """Base class for mouse-here commands.

    Subclasses set `what` attribute.
    """

    what = None

    def run(self, event):
        view = self.window.active_view()
        if not view:
            return
        pt = view.window_to_text((event['x'], event['y']))
        _cargo_test_pt(self.what, pt, view)

    def want_event(self):
        return True


class CargoTestHereCommand(CargoHere):

    """Determines the test name at the current mouse position, and runs just
    that test."""

    what = 'test'


class CargoBenchHereCommand(CargoHere):

    """Determines the benchmark at the current mouse position, and runs just
    that benchmark."""

    what = 'bench'


class CargoTestAtCursorCommand(sublime_plugin.TextCommand):

    """Determines the test name at the current cursor position, and runs just
    that test."""

    def run(self, edit):
        pt = self.view.sel()[0].begin()
        _cargo_test_pt('test', pt, self.view)


class CargoCurrentFile(sublime_plugin.WindowCommand):

    """Base class for current file commands.

    Subclasses set `what` attribute.
    """

    what = None

    def run(self):
        def _test_file(target):
            self.window.run_command('cargo_exec', args={
                'command': self.what,
                'settings': {
                    'target': target
                }
            })

        view = self.window.active_view()
        _target_to_test(self.what, view, _test_file)


class CargoTestCurrentFileCommand(CargoCurrentFile):

    """Runs all tests in the current file."""

    what = 'test'


class CargoBenchCurrentFileCommand(CargoCurrentFile):

    """Runs all benchmarks in the current file."""

    what = 'bench'


class CargoRunCurrentFileCommand(CargoCurrentFile):

    """Runs the current file."""

    what = 'run'


class CargoBenchAtCursorCommand(sublime_plugin.TextCommand):

    """Determines the benchmark name at the current cursor position, and runs
    just that benchmark."""

    def run(self, edit):
        pt = self.view.sel()[0].begin()
        _cargo_test_pt('bench', pt, self.view)


class CargoMessageHover(sublime_plugin.ViewEventListener):

    """Displays a popup if `rust_phantom_style` is "popup" when the mouse
    hovers over a message region.

    Limitation:  If you edit the file and shift the region, the hover feature
    will not recognize the new region.  This means that the popup will only
    show in the old location.
    """

    @classmethod
    def is_applicable(cls, settings):
        return util.is_rust_view(settings)

    @classmethod
    def applies_to_primary_view_only(cls):
        return False

    def on_hover(self, point, hover_zone):
        if util.get_setting('rust_phantom_style', 'normal') == 'popup':
            messages.message_popup(self.view, point, hover_zone)


class RustMessagePopupCommand(sublime_plugin.TextCommand):

    """Manually display a popup for any message under the cursor."""

    def run(self, edit):
        for r in self.view.sel():
            messages.message_popup(self.view, r.begin(), sublime.HOVER_TEXT)


class RustMessageStatus(sublime_plugin.ViewEventListener):

    """Display message under cursor in status bar."""

    @classmethod
    def is_applicable(cls, settings):
        return (util.is_rust_view(settings)
            and util.get_setting('rust_message_status_bar', False))

    @classmethod
    def applies_to_primary_view_only(cls):
        return False

    def on_selection_modified_async(self):
        # https://github.com/SublimeTextIssues/Core/issues/289
        # Only works with the primary view, get the correct view.
        # (Also called for each view, unfortunately.)
        active_view = self.view.window().active_view()
        if active_view and active_view.buffer_id() == self.view.buffer_id():
            view = active_view
        else:
            view = self.view
        messages.update_status(view)


class RustEventListener(sublime_plugin.EventListener):

    def on_activated_async(self, view):
        # This is a workaround for this bug:
        # https://github.com/SublimeTextIssues/Core/issues/2411
        # It would be preferable to use ViewEventListener, but it doesn't work
        # on duplicate views created with Goto Anything.
        def activate():
            if not util.active_view_is_rust(view=view):
                return
            if util.get_setting('rust_message_status_bar', False):
                messages.update_status(view)
            messages.draw_regions_if_missing(view)

        # For some reason, view.window() sometimes returns None here.
        # Use set_timeout to give it time to attach to a window.
        sublime.set_timeout(activate, 1)

    def on_query_context(self, view, key, operator, operand, match_all):
        # Used by the Escape-key keybinding to dismiss inline phantoms.
        if key == 'rust_has_messages':
            try:
                winfo = messages.WINDOW_MESSAGES[view.window().id()]
                has_messages = not winfo['hidden']
            except KeyError:
                has_messages = False
            if operator == sublime.OP_EQUAL:
                return operand == has_messages
            elif operator == sublime.OP_NOT_EQUAL:
                return operand != has_messages
        return None


class RustAcceptSuggestedReplacement(sublime_plugin.TextCommand):

    """Used for suggested replacements issued by the compiler to apply the
    suggested replacement.
    """

    def run(self, edit, region, replacement):
        region = sublime.Region(*region)
        self.view.replace(edit, region, replacement)


class RustScrollToRegion(sublime_plugin.TextCommand):

    """Internal command used to scroll a view to a region."""

    def run(self, edit, region):
        r = sublime.Region(*region)
        self.view.sel().clear()
        self.view.sel().add(r)
        self.view.show_at_center(r)


def plugin_unloaded():
    messages.clear_all_messages()
    try:
        from package_control import events
    except ImportError:
        return
    package_name = __package__.split('.')[0]
    if events.pre_upgrade(package_name):
        # When upgrading the package, Sublime currently does not cleanly
        # unload the `rust` Python package.  This is a workaround to ensure
        # that it gets completely unloaded so that when it upgrades it will
        # load the new package. See
        # https://github.com/SublimeTextIssues/Core/issues/2207
        re_keys = [
            key
            for key in sys.modules
            if key.startswith(f'{package_name}.rust')
        ]
        for key in re_keys:
            del sys.modules[key]
        if package_name in sys.modules:
            del sys.modules[package_name]


def plugin_loaded():
    try:
        from package_control import events
    except ImportError:
        return
    package_name = __package__.split('.')[0]
    if events.install(package_name):
        # Update the syntax for any open views.
        for window in sublime.windows():
            for view in window.views():
                fname = view.file_name()
                if fname and fname.endswith('.rs'):
                    view.settings().set(
                        'syntax',
                        f'Packages/{package_name}/RustEnhanced.sublime-syntax',
                    )

        # Disable the built-in Rust package.
        settings = sublime.load_settings('Preferences.sublime-settings')
        ignored = settings.get('ignored_packages', [])
        if 'Rust' not in ignored:
            ignored.append('Rust')
            settings.set('ignored_packages', ignored)
            sublime.save_settings('Preferences.sublime-settings')
