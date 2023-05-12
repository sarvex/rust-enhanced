"""Tests for next/prev message navigation."""

import os
import re
from rust_test_common import *


# Test data for message order tests.
#
# 'command': The command to run.
# 'path': The path to open.
# 'messages': List of expected messages. Tuples of:
#     (sequence, path, level, rowcol, inline_highlight, raw_highlight)
#
#     sequence: The order the messages should be visited. The messages should
#         be listed in the order that they are emitted by rustc. The number
#         indicates the order that the Rust Enhanced plugin will visit them.
#         This is different because we visit based by level (warnings first).
#     path: The file that this message is for.
#     level: The message level ('WARN', 'ERR', etc.)
#     rowcol: The 0-based row/col where the cursor should appear.
#     inline_highlight: The message that is displayed in the build output when
#         `show_inline_messages` is True.
#     raw_highlight: The message that is displayed in the build output when
#         `show_inline_messages` is False.
#
#     The highlight messages are regular expressions.
TEST_DATA = [
    {'command': 'build',
     'path': 'examples/ex_warning1.rs',
     'messages': [
        (1, 'examples/warning1.rs', 'WARN', (0, 11), 'examples/warning1.rs:1', ' --> examples/warning1.rs:1:4'),
        (2, 'examples/warning1.rs', 'WARN', (4, 11), 'examples/warning1.rs:5', ' --> examples/warning1.rs:5:4'),
        (3, 'examples/warning2.rs', 'WARN', (81, 14), 'examples/warning2.rs:82', '  --> examples/warning2.rs:82:4'),
     ]
    },

    {'command': 'build',
     'path': 'tests/test_all_levels.rs',
     'messages': [
        (2, 'tests/test_all_levels.rs', 'WARN', (3, 15), 'tests/test_all_levels.rs:4', ' --> tests/test_all_levels.rs:4:12'),
        (1, 'tests/test_all_levels.rs', 'ERR', (8, 25), 'tests/test_all_levels.rs:9', ' --> tests/test_all_levels.rs:9:25'),
     ]
    },

    {'command': 'test',
     'path': 'tests/test_test_output.rs',
     'messages': [
        (1, 'tests/test_test_output.rs', 'ERR', (8, 4), 'tests/test_test_output.rs:9:5', 'tests/test_test_output.rs:9:5'),
        (2, 'tests/test_test_output.rs', 'ERR', (13, 4), 'tests/test_test_output.rs:14:5', 'tests/test_test_output.rs:14:5'),
        (3, 'tests/test_test_output.rs', 'ERR', (18, 4), 'tests/test_test_output.rs:19:5', 'tests/test_test_output.rs:19:5'),
        (4, 'tests/test_test_output.rs', 'ERR', (23, 4), 'tests/test_test_output.rs:24:5', 'tests/test_test_output.rs:24:5'),
        (5, 'tests/test_test_output.rs', 'ERR', (28, 4), 'tests/test_test_output.rs:29:5', 'tests/test_test_output.rs:29:5'),
        (6, 'tests/test_test_output.rs', 'ERR', (59, 28), 'tests/test_test_output.rs:60:29', 'tests/test_test_output.rs:60:29'),
     ]
    }
]


class TestMessageOrder(TestBase):

    def setUp(self):
        super(TestMessageOrder, self).setUp()
        # Set a base version for these tests.
        version = util.get_rustc_version(sublime.active_window(), plugin_path)
        if semver.match(version, '<1.62.0-beta'):
            self.skipTest('Tests require rust 1.62 or newer.')

        # Make it so that the build target is automatically determined from
        # the active view so each test doesn't have to specify it.
        window = sublime.active_window()
        pkg = os.path.normpath(os.path.join(plugin_path,
            'tests/message-order'))
        window.run_command('cargo_set_target', {'target': 'auto',
                                                'variant': 'build',
                                                'package': pkg})
        window.run_command('cargo_set_target', {'target': 'auto',
                                                'variant': 'test',
                                                'package': pkg})

    def test_message_order(self):
        """Test message order.

        This opens a file and runs the build command on it.  It then verifies
        that next/prev message goes to the correct message in order.
        """
        for data in TEST_DATA:
            print(f"testing {data['path']}")
            path = os.path.join('tests/message-order', data['path'])

            # rust_next_message sorts based on error level.
            inline_sort = [x[1:] for x in sorted(data['messages'])]
            # Sublime's built-in next/prev message goes in source order.
            unsorted = [x[1:] for x in data['messages']]

            self._with_open_file(path, self._test_message_order,
                messages=inline_sort, inline=True, command=data['command'])
            self._with_open_file(path, self._test_message_order,
                messages=unsorted, inline=False, command=data['command'])

    def _test_message_order(self, view, messages, inline, command):
        self._override_setting('show_errors_inline', inline)
        self._cargo_clean(view)
        window = view.window()
        self._run_build_wait(command)

        to_close = []

        def check_sequence(direction):
            omsgs = messages if direction == 'next' else reversed(messages)
            levels = ('all', 'error', 'warning') if inline else ('all',)
            times = 2 if inline else 1
            for level in levels:
                # Run through all messages twice to verify it starts again.
                for _ in range(times):
                    for (next_filename, next_level, next_row_col,
                         inline_highlight, raw_highlight) in omsgs:
                        next_filename = os.path.join(plugin_path,
                            'tests', 'message-order', next_filename)
                        if inline and (
                           (level == 'error' and next_level != 'ERR') or
                           (level == 'warning' and next_level != 'WARN')):
                            continue
                        window.run_command(f'rust_{direction}_message', {'levels': level})
                        # Sublime doesn't always immediately move the active
                        # view when 'next_result' is called, so give it a
                        # moment to update.
                        time.sleep(0.1)
                        next_view = window.active_view()
                        to_close.append(next_view)
                        self.assertEqual(os.path.normpath(next_view.file_name()),
                                         os.path.normpath(next_filename))
                        region = next_view.sel()[0]
                        rowcol = next_view.rowcol(region.begin())
                        if inline:
                            self.assertEqual(rowcol, next_row_col)
                        else:
                            # When inline is disabled, we use Sublime's
                            # built-in next/prev, which goes to the beginning.
                            # Just validate the row is correct.
                            self.assertEqual(rowcol[0], next_row_col[0])
                        # Verify the output panel is highlighting the correct
                        # thing.
                        build_panel = window.find_output_panel(
                            plugin.rust.opanel.PANEL_NAME)
                        panel_text = build_panel.substr(build_panel.sel()[0])
                        if sys.platform == 'win32':
                            panel_text = panel_text.replace('\\', '/')
                        if inline:
                            self.assertRegex(panel_text, inline_highlight)
                        else:
                            self.assertRegex(panel_text, raw_highlight)

        check_sequence('next')
        if inline:
            # Reset back to first.
            window.run_command('rust_next_message')
            # Run backwards twice, too.
            check_sequence('prev')
            # Test starting backwards.
            window.focus_view(view)
            self._cargo_clean(view)
            self._run_build_wait(command)
            check_sequence('prev')

        for close_view in to_close:
            if close_view.window():
                window.focus_view(close_view)
                window.run_command('close_file')

    def test_no_messages(self):
        self._with_open_file('tests/message-order/examples/ex_no_messages.rs',
            self._test_no_messages)

    def _test_no_messages(self, view):
        self._cargo_clean(view)
        window = view.window()
        self._run_build_wait()
        # Verify command does nothing.
        for direction in ('next', 'prev'):
            window.run_command(f'rust_{direction}_message')
            active = window.active_view()
            self.assertEqual(active, view)
            sel = active.sel()[0]
            self.assertEqual((sel.a, sel.b), (0, 0))
