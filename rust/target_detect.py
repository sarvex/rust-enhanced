"""Used to determine the Cargo targets from any given .rs file.

This is very imperfect.  It uses heuristics to try to detect targets. This
could be significantly improved by using "rustc --emit dep-info", however,
that may be a little tricky (and doesn't work on errors).  See
https://github.com/rust-lang/cargo/issues/3211
"""

import os
from . import rust_proc, util, log


class TargetDetector(object):

    def __init__(self, window):
        self.window = window

    def determine_targets(self, file_name, metadata=None):
        """Detect the target/filters needed to pass to Cargo to compile
        file_name.
        Returns list of (target_src_path, target_command_line_args) tuples.

        :keyword metadata: Output from `get_cargo_metadata`. If None, will run
            it manually.

        :raises ProcessTerminatedError: Thread should shut down.
        """
        if result := self._targets_manual_config(file_name):
            return result

        # Try a heuristic to detect the filename.
        if metadata is None:
            metadata = util.get_cargo_metadata(self.window, os.path.dirname(file_name))
            if not metadata:
                return []
        # Each "workspace" shows up as a separate package.
        for package in metadata['packages']:
            root_path = os.path.dirname(package['manifest_path'])
            targets = package['targets']
            # targets is list of dictionaries:
            # {'kind': ['lib'],
            #  'name': 'target-name',
            #  'src_path': 'path/to/lib.rs'}
            # src_path may be absolute or relative, fix it.
            for target in targets:
                if not os.path.isabs(target['src_path']):
                    target['src_path'] = os.path.join(root_path, target['src_path'])
                target['src_path'] = os.path.normpath(target['src_path'])

            if result := self._targets_exact_match(targets, file_name):
                return result

            if result := self._targets_longest_matches(targets, file_name):
                return result

        log.log(self.window,
            'Rust Enhanced: Failed to find target for %r', file_name)
        return [(None, [])]

    def _targets_manual_config(self, file_name):
        """Check for Cargo targets in the Sublime settings."""
        # First check config for manual targets.
        for project in util.get_setting('projects', {}).values():
            src_root = os.path.join(project.get('root', ''), 'src')
            if not file_name.startswith(src_root):
                continue
            targets = project.get('targets', {})
            for tfile, tcmd in targets.items():
                if file_name == os.path.join(src_root, tfile):
                    return [(tfile, tcmd.split())]
            else:
                if target := targets.get('_default', ''):
                    # Unfortunately don't have the target src filename.
                    return [('', target)]
        return None

    def _target_to_args(self, target):
        """Convert target from Cargo metadata to Cargo command-line argument.
        """
        # Targets have multiple "kinds" when you specify crate-type in
        # Cargo.toml, like:
        #   crate-type = ["rlib", "dylib"]
        #
        # Libraries are the only thing that support this at this time, and
        # generally you only use one command-line argument to build multiple
        # "kinds" (--lib in this case).
        #
        # Caution:  [[example]] that specifies crate-type had issues before
        # 1.17.
        # See https://github.com/rust-lang/cargo/pull/3556 and
        # https://github.com/rust-lang/cargo/issues/3572
        # https://github.com/rust-lang/cargo/pull/3668  (ISSUE FIXED)
        #
        # For now, just grab the first kind since it will always result in the
        # same arguments.
        kind = target['kind'][0]
        if kind in ('lib', 'rlib', 'dylib', 'cdylib', 'staticlib', 'proc-macro'):
            return (target['src_path'], ['--lib'])
        elif kind in ('bin', 'test', 'example', 'bench'):
            return target['src_path'], [f'--{kind}', target['name']]
        elif kind in ('custom-build',):
            # Currently no way to target build.rs explicitly.
            # Or, run rustc (without cargo) on build.rs.
            # TODO: "cargo check" seems to work
            return None
        else:
            # Unknown kind, don't know how to build.
            raise ValueError(kind)

    def _targets_exact_match(self, targets, file_name):
        """Check for Cargo targets that exactly match the current file."""
        for target in targets:
            if target['src_path'] == file_name:
                if args := self._target_to_args(target):
                    return [args]
        return None

    def _targets_longest_matches(self, targets, file_name):
        """Determine the Cargo targets that are in the same directory (or
        parent) of the current file."""
        result = []
        # Find longest path match.
        # TODO: This is sub-optimal, because it may result in multiple targets.
        # Consider using the output of rustc --emit dep-info.
        # See https://github.com/rust-lang/cargo/issues/3211 for some possible
        # problems with that.
        path_match = os.path.dirname(file_name)
        found = False
        found_lib = False
        found_bin = False
        # Sort the targets by name for consistent results, which cargo doesn't
        # guarantee.
        targets.sort(key=lambda x: x['name'])
        while not found:
            for target in targets:
                if os.path.dirname(target['src_path']) == path_match:
                    if target_args := self._target_to_args(target):
                        result.append(target_args)
                        found = True
                        if target_args[1][0] == '--bin':
                            found_bin = True
                        if target_args[1][0] == '--lib':
                            found_lib = True
            p = os.path.dirname(path_match)
            if p == path_match:
                # Root path
                break
            path_match = p
        # If the match is both --bin and --lib in the same directory,
        # just do --bin.
        if found_bin and found_lib:
            result = [x for x in result if x[1][0] != '--bin']
        return result
