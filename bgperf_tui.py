# Copyright (C) 2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import curses
import os
import shlex
import subprocess
import sys


class TUIField(object):
    def __init__(self, action, scope):
        self.action = action
        self.scope = scope
        self.dest = action.dest
        self.positional = not action.option_strings
        self.option = self._preferred_option()
        self.label = self._label()
        self.help = action.help or ''
        self.choices = list(action.choices) if action.choices else None
        self.kind = self._kind()
        self.default = self._default()

    def _preferred_option(self):
        if not self.action.option_strings:
            return None
        for option in self.action.option_strings:
            if option.startswith('--'):
                return option
        return self.action.option_strings[0]

    def _label(self):
        if self.positional:
            return self.dest
        return self.option.lstrip('-').replace('-', ' ')

    def _kind(self):
        if isinstance(self.action, argparse._StoreTrueAction):
            return 'bool_true'
        if isinstance(self.action, argparse._StoreFalseAction):
            return 'bool_false'
        if self.choices:
            return 'choice'
        if self.action.type is int:
            return 'int'
        return 'text'

    def _default(self):
        default = self.action.default
        if default is argparse.SUPPRESS:
            default = None
        if self.kind == 'bool_true':
            return bool(default)
        if self.kind == 'bool_false':
            return True if default is None else bool(default)
        if self.kind == 'choice':
            if default is not None:
                return default
            return self.choices[0] if self.choices else ''
        if self.kind == 'int':
            return 0 if default is None else int(default)
        if self.positional and default is None and self.choices:
            return self.choices[0]
        return '' if default is None else str(default)

    def display_value(self, value):
        if self.kind in ['bool_true', 'bool_false']:
            return 'on' if value else 'off'
        if value in [None, '']:
            return '(unset)'
        return str(value)

    def adjust(self, value, direction):
        if self.kind in ['bool_true', 'bool_false']:
            return not bool(value)
        if self.kind == 'choice' and self.choices:
            try:
                idx = self.choices.index(value)
            except ValueError:
                idx = 0
            return self.choices[(idx + direction) % len(self.choices)]
        if self.kind == 'int':
            try:
                return int(value) + direction
            except (TypeError, ValueError):
                return self.default
        return value

    def parse_input(self, text):
        if self.kind == 'int':
            return int(text)
        return text

    def should_emit(self, value):
        if self.positional:
            return True
        if self.kind == 'bool_true':
            return bool(value)
        if self.kind == 'bool_false':
            return not bool(value)
        if value in [None, '']:
            return False
        return value != self.default

    def emit(self, value):
        if self.positional:
            return [str(value)]
        if self.kind in ['bool_true', 'bool_false']:
            return [self.option] if self.should_emit(value) else []
        if not self.should_emit(value):
            return []
        return [self.option, str(value)]


class TUIState(object):
    def __init__(self, parser, initial_args):
        self.parser = parser
        self.initial_args = initial_args
        self.subparser_action = self._subparser_action(parser)
        self.command_names = [
            name for name in self.subparser_action.choices.keys()
            if name != 'tui'
        ]
        self.command_help = self._command_help()
        self.global_fields = self._fields_for_parser(parser, 'global')
        self.command_fields = {
            name: self._fields_for_parser(self.subparser_action.choices[name], name)
            for name in self.command_names
        }
        self.global_values = self._initial_global_values()
        self.command_values = {
            name: {field.dest: field.default for field in self.command_fields[name]}
            for name in self.command_names
        }
        self.command_index = 0
        self.focus_index = 0
        self.button_index = 0
        self.scroll = 0
        self.status = 'Ready.'

    def _subparser_action(self, parser):
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                return action
        raise ValueError('parser has no subcommands')

    def _command_help(self):
        result = {}
        for choice in self.subparser_action._choices_actions:
            result[choice.dest] = choice.help
        return result

    def _fields_for_parser(self, parser, scope):
        fields = []
        for action in parser._actions:
            if isinstance(action, (argparse._HelpAction, argparse._SubParsersAction)):
                continue
            fields.append(TUIField(action, scope))
        return fields

    def _initial_global_values(self):
        values = {}
        for field in self.global_fields:
            values[field.dest] = getattr(self.initial_args, field.dest, field.default)
        return values

    @property
    def command(self):
        return self.command_names[self.command_index]

    @property
    def fields(self):
        return self.global_fields + self.command_fields[self.command]

    @property
    def field_count(self):
        return len(self.fields)

    @property
    def max_focus_index(self):
        return self.field_count + 1

    @property
    def focused_field(self):
        if self.focus_index == 0 or self.focus_index == self.max_focus_index:
            return None
        return self.fields[self.focus_index - 1]

    def values_for_field(self, field):
        if field.scope == 'global':
            return self.global_values
        return self.command_values[self.command]

    def get_value(self, field):
        return self.values_for_field(field)[field.dest]

    def set_value(self, field, value):
        self.values_for_field(field)[field.dest] = value

    def change_command(self, direction):
        self.command_index = (self.command_index + direction) % len(self.command_names)
        self.focus_index = min(self.focus_index, self.max_focus_index)
        self.scroll = 0

    def reset_current_command(self):
        self.command_values[self.command] = {
            field.dest: field.default for field in self.command_fields[self.command]
        }
        self.status = 'Reset command fields for {0}.'.format(self.command)

    def command_argv(self, script_path):
        argv = [sys.executable, script_path]
        for field in self.global_fields:
            value = self.global_values[field.dest]
            argv.extend(field.emit(value))
        argv.append(self.command)
        for field in self.command_fields[self.command]:
            value = self.command_values[self.command][field.dest]
            argv.extend(field.emit(value))
        return argv


class TUIApplication(object):
    BUTTONS = ['Run', 'Reset', 'Quit']

    def __init__(self, state, script_path):
        self.state = state
        self.script_path = script_path

    def run(self, stdscr):
        self.set_cursor(0)
        stdscr.keypad(True)
        while True:
            self.draw(stdscr)
            key = stdscr.getch()
            if self.handle_key(stdscr, key):
                return 0

    def handle_key(self, stdscr, key):
        if key in [ord('q'), ord('Q')]:
            return True
        if key == curses.KEY_UP:
            self.state.focus_index = max(0, self.state.focus_index - 1)
            return False
        if key == curses.KEY_DOWN:
            self.state.focus_index = min(self.state.max_focus_index, self.state.focus_index + 1)
            return False
        if key == curses.KEY_LEFT:
            self.adjust_focus(-1)
            return False
        if key == curses.KEY_RIGHT:
            self.adjust_focus(1)
            return False
        if key in [ord(' '), ord('\n'), curses.KEY_ENTER, 10, 13]:
            return self.activate(stdscr)
        if key in [ord('\t')]:
            self.state.focus_index = (self.state.focus_index + 1) % (self.state.max_focus_index + 1)
            return False
        return False

    def adjust_focus(self, direction):
        if self.state.focus_index == 0:
            self.state.change_command(direction)
            return
        if self.state.focus_index == self.state.max_focus_index:
            self.state.button_index = (self.state.button_index + direction) % len(self.BUTTONS)
            return
        field = self.state.focused_field
        value = self.state.get_value(field)
        self.state.set_value(field, field.adjust(value, direction))

    def activate(self, stdscr):
        if self.state.focus_index == 0:
            self.state.change_command(1)
            return False
        if self.state.focus_index == self.state.max_focus_index:
            button = self.BUTTONS[self.state.button_index]
            if button == 'Run':
                self.run_command(stdscr)
                return False
            if button == 'Reset':
                self.state.reset_current_command()
                return False
            return True

        field = self.state.focused_field
        if field.kind in ['bool_true', 'bool_false', 'choice']:
            self.adjust_focus(1)
            return False
        value = self.state.get_value(field)
        edited = self.edit_value(stdscr, field, value)
        if edited is not None:
            self.state.set_value(field, edited)
        return False

    def draw(self, stdscr):
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        if height < 12 or width < 50:
            self.addstr(stdscr, 0, 0, 'Terminal too small for bgperf TUI.')
            stdscr.refresh()
            return

        self.addstr(stdscr, 0, 0, 'bgperf TUI', curses.A_BOLD)
        self.addstr(stdscr, 1, 0, 'Arrows move/change, Enter edits/runs, Space toggles, q exits.')
        self.draw_commands(stdscr, 3, width)
        self.draw_fields(stdscr, 5, height - 5, width)
        self.draw_buttons(stdscr, height - 3, width)
        self.draw_status(stdscr, height - 2, width)
        self.draw_preview(stdscr, height - 1, width)
        stdscr.refresh()

    def draw_commands(self, stdscr, y, width):
        x = 0
        self.addstr(stdscr, y, x, 'Command: ')
        x += 9
        for idx, name in enumerate(self.state.command_names):
            label = '[{0}]'.format(name)
            attr = curses.A_REVERSE if self.state.focus_index == 0 and idx == self.state.command_index else 0
            if idx == self.state.command_index and self.state.focus_index != 0:
                attr = curses.A_BOLD
            self.addstr(stdscr, y, x, label, attr)
            x += len(label) + 1
            if x >= width - 1:
                break
        help_text = self.state.command_help.get(self.state.command, '')
        self.addstr(stdscr, y + 1, 0, help_text[:width - 1])

    def draw_fields(self, stdscr, start_y, end_y, width):
        visible_height = max(1, end_y - start_y)
        focused_field_index = self.state.focus_index - 1
        if focused_field_index >= 0:
            if focused_field_index < self.state.scroll:
                self.state.scroll = focused_field_index
            if focused_field_index >= self.state.scroll + visible_height:
                self.state.scroll = focused_field_index - visible_height + 1
        self.state.scroll = max(0, min(self.state.scroll, max(0, self.state.field_count - visible_height)))

        visible = self.state.fields[self.state.scroll:self.state.scroll + visible_height]
        for offset, field in enumerate(visible):
            field_index = self.state.scroll + offset
            y = start_y + offset
            value = self.state.get_value(field)
            scope = 'global' if field.scope == 'global' else self.state.command
            label = '{0}.{1}'.format(scope, field.label)
            text = '{0:<28} {1}'.format(label[:28], field.display_value(value))
            attr = curses.A_REVERSE if self.state.focus_index == field_index + 1 else 0
            self.addstr(stdscr, y, 0, text[:width - 1], attr)

    def draw_buttons(self, stdscr, y, width):
        x = 0
        for idx, button in enumerate(self.BUTTONS):
            label = '[ {0} ]'.format(button)
            attr = curses.A_REVERSE if self.state.focus_index == self.state.max_focus_index and idx == self.state.button_index else 0
            self.addstr(stdscr, y, x, label, attr)
            x += len(label) + 2
            if x >= width - 1:
                break

    def draw_status(self, stdscr, y, width):
        field = self.state.focused_field
        status = self.state.status
        if field and field.help:
            status = field.help
        self.addstr(stdscr, y, 0, status[:width - 1])

    def draw_preview(self, stdscr, y, width):
        preview = '$ ' + shlex.join(self.state.command_argv(self.script_path))
        self.addstr(stdscr, y, 0, preview[:width - 1], curses.A_DIM)

    def edit_value(self, stdscr, field, value):
        height, width = stdscr.getmaxyx()
        prompt = 'Set {0} (empty clears optional text): '.format(field.label)
        y = height - 2
        self.addstr(stdscr, y, 0, ' ' * (width - 1))
        self.addstr(stdscr, y, 0, prompt[:width - 1])
        curses.echo()
        self.set_cursor(1)
        try:
            initial = '' if value is None else str(value)
            x = min(len(prompt), width - 1)
            self.addstr(stdscr, y, x, initial[:max(0, width - x - 1)])
            stdscr.move(y, min(width - 1, x + len(initial)))
            raw = stdscr.getstr(y, x, max(1, width - x - 1))
        finally:
            curses.noecho()
            self.set_cursor(0)
        text = raw.decode('utf-8', 'replace')
        try:
            return field.parse_input(text)
        except ValueError:
            self.state.status = 'Invalid value for {0}: {1}'.format(field.label, text)
            return None

    def run_command(self, stdscr):
        argv = self.state.command_argv(self.script_path)
        curses.def_prog_mode()
        curses.endwin()
        try:
            print('$ {0}'.format(shlex.join(argv)))
            rc = subprocess.call(argv)
            input('\nCommand exited with status {0}. Press Enter to return to TUI.'.format(rc))
            self.state.status = 'Last command exited with status {0}.'.format(rc)
        finally:
            curses.reset_prog_mode()
            self.set_cursor(0)

    def addstr(self, stdscr, y, x, text, attr=0):
        height, width = stdscr.getmaxyx()
        if y < 0 or y >= height or x < 0 or x >= width:
            return
        try:
            stdscr.addstr(y, x, text[:max(0, width - x - 1)], attr)
        except curses.error:
            pass

    def set_cursor(self, visibility):
        try:
            curses.curs_set(visibility)
        except curses.error:
            pass


def run_tui(args, parser_factory, script_path):
    parser = parser_factory()
    state = TUIState(parser, args)
    app = TUIApplication(state, os.path.abspath(script_path))
    return curses.wrapper(app.run)
