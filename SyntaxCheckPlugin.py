import sublime
import sublime_plugin
import os
import time
from .rust import (messages, rust_proc, rust_thread, util, target_detect,
                   cargo_settings, semver, log)


"""On-save syntax checking.

This contains the code for displaying message phantoms for errors/warnings
whenever you save a Rust file.
"""


# TODO: Use ViewEventListener if
# https://github.com/SublimeTextIssues/Core/issues/2411 is fixed.
class RustSyntaxCheckEvent(sublime_plugin.EventListener):

    last_save = 0

    def on_post_save(self, view):
        enabled = util.get_setting('rust_syntax_checking', True)
        if not enabled or not util.active_view_is_rust(view=view):
            return
        prev_save = self.last_save
        self.last_save = time.time()
        if self.last_save - prev_save < 0.25:
            # This is a guard for a few issues.
            # * `on_post_save` gets called multiple times if the same buffer
            #   is opened in multiple views (with the same view passed in each
            #   time). See:
            #   https://github.com/SublimeTextIssues/Core/issues/289
            # * When using "Save All" we want to avoid launching a bunch of
            #   threads and then immediately killing them.
            return
        log.clear_log(view.window())
        messages.erase_status(view)
        t = RustSyntaxCheckThread(view)
        t.start()


class RustSyntaxCheckThread(rust_thread.RustThread, rust_proc.ProcListener):

    # Thread name.
    name = 'Syntax Check'
    # The Sublime view that triggered the check.
    view = None
    # The Sublime window that triggered the check.
    window = None
    # Absolute path to the view that triggered the check.
    triggered_file_name = None
    # Directory where cargo will be run.
    cwd = None
    # Base path for relative paths in messages.
    msg_rel_path = None
    # This flag is used to terminate early. In situations where we can't
    # auto-detect the appropriate Cargo target, we compile multiple targets.
    # If we receive any messages for the current view, we might as well stop.
    # Otherwise, you risk displaying duplicate messages for shared modules.
    this_view_found = False
    # The path to the top-level Cargo target filename (like main.rs or
    # lib.rs).
    current_target_src = None
    done = False

    def __init__(self, view):
        self.view = view
        self.window = view.window()
        super(RustSyntaxCheckThread, self).__init__(view.window())

    def run(self):
        self.triggered_file_name = os.path.abspath(self.view.file_name())
        self.cwd = util.find_cargo_manifest(self.triggered_file_name)
        if self.cwd is None:
            # A manifest is required.
            log.critical(self.window, util.multiline_fix("""
                Rust Enhanced skipping on-save syntax check.
                Failed to find Cargo.toml from %r
                A Cargo.toml manifest is required.
            """), self.triggered_file_name)
            return

        self.update_status()
        self.this_view_found = False
        CHECK_FAIL_MSG = 'Rust check failed, see console or debug log.'
        try:
            messages.clear_messages(self.window)
            try:
                rc = self.get_rustc_messages()
            except rust_proc.ProcessTerminatedError:
                self.window.status_message('')
                return
            except Exception as e:
                self.window.status_message(CHECK_FAIL_MSG)
                raise
        finally:
            self.done = True
        messages.messages_finished(self.window)
        if counts := messages.message_counts(self.window):
            msg = []
            for key, value in sorted(counts.items(), key=lambda x: x[0]):
                level = key.plural if value > 1 else key.name
                msg.append('%i %s' % (value, level))
            self.window.status_message(f"Rust check: {', '.join(msg)}")
        elif rc:
            self.window.status_message(CHECK_FAIL_MSG)
        else:
            self.window.status_message('Rust check: success')

    def update_status(self, count=0):
        if self.done:
            return

        status_msg = util.get_setting('rust_message_status_bar_msg')
        status_chars = util.get_setting('rust_message_status_bar_chars')
        status_update_delay = util.get_setting('rust_message_status_bar_update_delay')

        try:
            status_chars_len = len(status_chars)
            num = count % status_chars_len
            if num == status_chars_len - 1:
                num = -1
            num += 1

            self.window.status_message(status_msg + status_chars[num])
            sublime.set_timeout(lambda: self.update_status(count + 1), status_update_delay)
        except Exception as e:
            self.window.status_message('Error setting status text!')
            log.critical(self.window, f"An error occurred setting status text: {str(e)}")

    def get_rustc_messages(self):
        """Top-level entry point for generating messages for the given
        filename.

        :raises rust_proc.ProcessTerminatedError: Check was canceled.
        :raises OSError: Failed to launch the child process.

        :returns: Returns the process return code.
        """
        method = util.get_setting('rust_syntax_checking_method', 'check')
        settings = cargo_settings.CargoSettings(self.window)
        settings.load()
        command_info = cargo_settings.CARGO_COMMANDS[method]

        if method == 'no-trans':
            print('rust_syntax_checking_method == "no-trans" is no longer supported.')
            print('Please change the config setting to "check".')
            method = 'check'

        if method not in ['check', 'clippy']:
            print('Unknown setting for `rust_syntax_checking_method`: %r' % (method,))
            return -1

        # Try to grab metadata only once. `target` is None since that's what
        # we're trying to figure out.
        toolchain = settings.get_computed(self.cwd, method, None, 'toolchain')
        metadata = util.get_cargo_metadata(self.window, self.cwd, toolchain=toolchain)
        if not metadata:
            return -1
        td = target_detect.TargetDetector(self.window)
        targets = td.determine_targets(self.triggered_file_name, metadata=metadata)
        if not targets:
            return -1
        rc = 0
        for (target_src, target_args) in targets:
            cmd = settings.get_command(method, command_info, self.cwd, self.cwd,
                initial_settings={'target': ' '.join(target_args)},
                force_json=True, metadata=metadata)
            self.msg_rel_path = cmd['msg_rel_path']
            if (util.get_setting('rust_syntax_checking_include_tests', True) and
                semver.match(cmd['rustc_version'], '>=1.23.0')):
                # Including the test harness has a few drawbacks.
                # missing_docs lint is disabled (see
                # https://github.com/rust-lang/sublime-rust/issues/156)
                # It also disables the "main function not found" error for
                # binaries.
                cmd['command'].append('--profile=test')
            p = rust_proc.RustProc()
            self.current_target_src = target_src
            p.run(self.window, cmd['command'], self.cwd, self, env=cmd['env'])
            rc = p.wait()
            if self.this_view_found:
                return rc
        return rc

    #########################################################################
    # ProcListner methods
    #########################################################################

    def on_begin(self, proc):
        pass

    def on_data(self, proc, data):
        log.log(self.window, data)

    def on_error(self, proc, message):
        log.critical(self.window, 'Rust Error: %s', message)

    def on_json(self, proc, obj):
        messages.add_rust_messages(self.window, self.msg_rel_path, obj,
                                   self.current_target_src, msg_cb=None)
        if messages.has_message_for_path(self.window,
                                         self.triggered_file_name):
            self.this_view_found = True

    def on_finished(self, proc, rc):
        log.log(self.window, 'On-save check finished.')

    def on_terminated(self, proc):
        log.log(self.window, 'Process Interrupted')
