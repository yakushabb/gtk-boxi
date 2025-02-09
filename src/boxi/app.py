# Boxi - Terminal emulator for use with Toolbox
#
# Copyright (C) 2022 Allison Karlitskaya <allison.karlitskaya@redhat.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import json
import os
import signal
import socket
import sys
import urllib.parse

import gi

gi.require_version('Adw', '1')
gi.require_version('Gdk', '4.0')
gi.require_version('Gtk', '4.0')
gi.require_version('Vte', '3.91')

from gi.repository import Adw
from gi.repository import GLib
from gi.repository import GObject
from gi.repository import Gdk
from gi.repository import Gio
from gi.repository import Gtk
from gi.repository import Pango
from gi.repository import Vte

from .adwaita_palette import ADWAITA_PALETTE
from . import APP_ID, IS_FLATPAK, PKG_DIR

VTE_NUMERIC_VERSION = 10000 * Vte.MAJOR_VERSION + 100 * Vte.MINOR_VERSION + Vte.MICRO_VERSION
VTE_TERMINFO_NAME = "xterm-256color"
VTE_ENV = {'TERM': VTE_TERMINFO_NAME, 'VTE_VERSION': f'{VTE_NUMERIC_VERSION}'}


class Agent:
    def __init__(self, container=None):
        self.container = container

        if container:
            cmd = [sys.executable, f'{PKG_DIR}/toolbox_run.py', container, '--', '/usr/bin/python3']
        elif IS_FLATPAK:
            cmd = ['flatpak-spawn', '--host', '--forward-fd=3', '/usr/bin/python3']
        else:
            cmd = [sys.executable]

        # `python3` in `ps` output isn't so helpful, so add some extra args
        if container:
            cmd.extend(['-', 'Boxi agent for container', container])
        else:
            cmd.extend(['-', 'Boxi agent for host'])

        launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.NONE)
        launcher.set_stdin_file_path(f'{PKG_DIR}/agent.py')
        self.connection, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        launcher.take_fd(os.dup(theirs.fileno()), 3)
        theirs.close()

        launcher.spawnv(cmd)

    def create_session(self, listener):
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        socket.send_fds(self.connection, [b' '], [theirs.fileno()])
        theirs.close()

        return Session(ours, listener)


class Session:
    def __init__(self, connection, listener):
        self.connection = connection
        self.listener = listener
        GLib.unix_fd_add_full(0, self.connection.fileno(), GLib.IOCondition.IN, Session.ready, self)

    def start_command(self, command, cwd=None, fds=()):
        message = {"args": command, "cwd": cwd, "env": VTE_ENV}
        socket.send_fds(self.connection, [json.dumps(message).encode('utf-8')], fds)
        for fd in fds:
            os.close(fd)

    def start_shell(self, cwd=None):
        self.start_command([], cwd=cwd)

    def open_editor(self):
        reader, writer = os.pipe()
        self.start_command(['_PAGER', '-'], fds=[reader])
        return Gio.UnixOutputStream.new(writer, True)

    @staticmethod
    def ready(fd, _condition, self):
        msg, fds, _flags, _addr = socket.recv_fds(self.connection, 10000, 1)
        if not msg:
            self.listener.session_closed()
            self.connection.close()
            del self.listener
            return False

        message = json.loads(msg)

        if message == 'pty':
            self.listener.session_created(Vte.Pty.new_foreign_sync(fds.pop()))
        elif isinstance(message, int):
            self.listener.session_exited(message)

        for fd in fds:
            os.close(fd)

        return True


class Terminal(Vte.Terminal):
    URL_REGEX = r'https?://[-A-Za-z0-9.:/_~?=#]+'

    def __init__(self, application):
        super().__init__()
        self.set_audible_bell(False)
        self.set_scrollback_lines(-1)
        regex = Vte.Regex.new_for_match(Terminal.URL_REGEX, -1, 0x00000400)
        self.uri_tag = self.match_add_regex(regex, 0)
        self.match_set_cursor_name(self.uri_tag, "hand")

        click = Gtk.GestureClick.new()
        click.set_propagation_phase(1)  # constants not defined?
        click.connect('pressed', Terminal.click_gesture_pressed)
        self.add_controller(click)

        application.style_manager.bind_property('dark',
                                                self, 'dark',
                                                GObject.BindingFlags.SYNC_CREATE)
        application.interface_settings.bind('monospace-font-name',
                                            self, 'font-name',
                                            Gio.SettingsBindFlags.GET)

    @staticmethod
    def click_gesture_pressed(gesture, times, x, y):
        if times != 1:
            return

        terminal = gesture.get_widget()

        uri, tag = terminal.check_match_at(x, y)
        if tag == terminal.uri_tag and uri is not None:
            Gtk.show_uri(terminal.get_root(), uri, gesture.get_current_event_time())

    @staticmethod
    def parse_color(color):
        rgba = Gdk.RGBA()
        rgba.parse(color if color.startswith('#') or color.startswith('rgb') else ADWAITA_PALETTE[color])
        return rgba

    def set_palette(self, fg=None, bg=None, palette=()):
        self.set_colors(fg and Terminal.parse_color(fg),
                        bg and Terminal.parse_color(bg),
                        [Terminal.parse_color(color) for color in palette])

    @GObject.Property(type=bool, default=False)
    def dark(self):
        return self._dark

    @dark.setter
    def set_dark(self, value):
        self._dark = value
        # See https://gitlab.gnome.org/Teams/Design/hig-www/-/issues/129 and
        # https://gitlab.gnome.org/Teams/Design/HIG-app-icons/-/commit/4e1dfe95748a6ee80cc9c0e6c40a891c0f4d534c
        palette = ['dark_4', 'red_4', 'green_4', 'yellow_4', 'blue_4', 'purple_4', '#0aa8dc', 'light_4',
                   'dark_2', 'red_2', 'green_2', 'yellow_2', 'blue_2', 'purple_2', '#4fd2fd', 'light_2']

        if self._dark:
            self.set_palette('light_1', 'rgb(5%, 5%, 5%)', palette)
        else:
            self.set_palette('dark_5', 'light_1', palette)

    @GObject.Property(type=str)
    def font_name(self):
        return self.get_font().to_string()

    @font_name.setter
    def set_font_name(self, value):
        self.set_font(Pango.FontDescription.from_string(value))

class Window(Gtk.ApplicationWindow):
    def __init__(self, application, command_line=None, path=None):
        super().__init__(application=application)
        self.command_line = command_line
        self.terminal = Terminal(application)
        self.terminal.set_size(120, 48)
        self.session = application.agent.create_session(self)
        self.set_child(self.terminal)
        self.container = None
        self.file = None
        self.path = path
        self.cwd = None

        self.terminal.connect('current-directory-uri-changed', Window.terminal_update_cwd)
        self.terminal.connect('current-file-uri-changed', Window.terminal_update_cwd)
        self.terminal_update_cwd(self.terminal)

    @staticmethod
    def terminal_update_cwd(terminal):
        window = terminal.get_parent()
        cwd_uri = terminal.get_current_directory_uri()
        window.cwd = cwd_uri and urllib.parse.urlparse(cwd_uri).path
        file_uri = terminal.get_current_file_uri()
        window.file = file_uri and urllib.parse.urlparse(file_uri).path
        title = ['Boxi', window.get_application().container, window.path or window.file or window.cwd]
        window.set_title(' : '.join(text for text in title if text))

    def session_created(self, pty):
        self.terminal.set_pty(pty)

    def session_exited(self, returncode):
        if hasattr(self, 'command_line') and self.command_line:
            self.command_line.set_exit_status(returncode)
            del self.command_line

    def session_closed(self):
        self.destroy()

    def new_window(self, *_args):
        window = Window(self.get_application())
        window.session.start_shell(cwd=self.cwd or self.path and os.path.dirname(self.path))
        window.show()

    def edit_contents(self, *_args):
        window = Window(self.get_application())
        window.show()

        stream = window.session.open_editor()
        self.terminal.write_contents_sync(stream, Vte.WriteFlags.DEFAULT, None)
        stream.close()

    def copy(self, *_args):
        self.terminal.copy_clipboard_format(Vte.Format.TEXT)

    def paste(self, *_args):
        self.terminal.paste_clipboard()

    def zoom(self, _action, parameter, *_args):
        current = self.terminal.get_font_scale()
        factors = {'in': current + 0.2, 'default': 1.0, 'out': current - 0.2}
        self.terminal.set_font_scale(factors[parameter.get_string()])


class Application(Gtk.Application):
    def __init__(self):
        super().__init__(flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE | Gio.ApplicationFlags.HANDLES_OPEN)

        self.add_option('non-unique', description='Disable GApplication uniqueness')
        self.add_option('version', description='Show version')
        self.add_option('container', 'c', arg=GLib.OptionArg.STRING, description='Toolbox container name')
        self.add_option('edit', description='Treat arguments as filenames to edit')
        self.add_option('', arg=GLib.OptionArg.STRING_ARRAY, arg_description='COMMAND ARGS ...')

    def add_option(self, long_name, short_name=None, arg=GLib.OptionArg.NONE, description='', arg_description=None):
        short_char = ord(short_name) if short_name is not None else 0
        self.add_main_option(long_name, short_char, GLib.OptionFlags.NONE, arg, description, arg_description)

    def do_handle_local_options(self, options):
        if options.contains('version'):
            from . import __version__ as version
            print(f'Boxi {version}')
            return 0

        if options.contains('container'):
            self.container = options.lookup_value('container').get_string()
            self.set_application_id(f'{APP_ID}.{self.container}')
        else:
            self.set_application_id(APP_ID)
            self.container = None
        GLib.set_prgname(self.get_application_id())

        # Ideally, GApplication would have a flag for this, but it's a little
        # bit magic.  In case `--gapplication-service` wasn't given, we want to
        # first try to become a launcher.  If that fails then we fall back to
        # the standard hybrid mode where we might end up as the primary or
        # remote instance.  This allows the benefits of being a launcher (more
        # consistent commandline behaviour) opportunistically, without breaking
        # the partially-installed case.
        flags = self.get_flags()
        if options.contains('non-unique'):
            self.set_flags(flags | Gio.ApplicationFlags.NON_UNIQUE)
        elif not flags & Gio.ApplicationFlags.IS_SERVICE:
            try:
                self.set_flags(flags | Gio.ApplicationFlags.IS_LAUNCHER)
                self.register()
            except GLib.Error:
                # didn't work?  Put it back.
                self.set_flags(flags)

        return -1

    def do_startup(self):
        Gtk.Application.do_startup(self)

        self.style_manager = Adw.StyleManager.get_default()
        self.interface_settings = Gio.Settings(schema_id='org.gnome.desktop.interface')
        self.boxi_settings = Gio.Settings(schema_id='dev.boxi.Boxi')

        self.boxi_settings.bind('color-scheme',
                                Adw.StyleManager.get_default(), 'color-scheme',
                                Gio.SettingsBindFlags.GET)

        Window.install_action('win.new-window', None, Window.new_window)
        Window.install_action('win.edit-contents', None, Window.edit_contents)
        Window.install_action('win.copy', None, Window.copy)
        Window.install_action('win.paste', None, Window.paste)
        Window.install_action('win.zoom', 's', Window.zoom)

        self.set_accels_for_action("win.new-window", ["<Ctrl><Shift>N"])
        self.set_accels_for_action("win.edit-contents", ["<Ctrl><Shift>S"])
        self.set_accels_for_action("win.copy", ["<Ctrl><Shift>C"])
        self.set_accels_for_action("win.paste", ["<Ctrl><Shift>V"])
        self.set_accels_for_action("win.zoom::default", ["<Ctrl>0"])
        self.set_accels_for_action("win.zoom::in", ["<Ctrl>equal", "<Ctrl>plus"])
        self.set_accels_for_action("win.zoom::out", ["<Ctrl>minus"])

        self.agent = Agent(self.container)

    def do_command_line(self, command_line):
        options = command_line.get_options_dict()
        args = options.lookup_value('')

        if options.contains('edit'):
            if args:
                for arg in args.get_strv():
                    self.open_file(command_line.create_file_for_arg(arg))
            else:
                self.do_activate()

            return 0
        else:
            window = Window(self, command_line)
            if args:
                window.session.start_command(args.get_strv())
            else:
                window.session.start_shell()
            window.show()

            return -1  # real return value comes later

    def do_open(self, files, _n_files, _hint):
        for file in files:
            self.open_file(file)

    def open_file(self, file):
        path = file.get_path()
        for window in self.get_windows():
            if window.path == path:
                break
        else:
            window = Window(self, path=path)
            window.session.start_command(['_EDITOR', path])
            window.show()

        window.present()

    def do_activate(self):
        window = Window(self)
        window.session.start_shell()
        window.show()


def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)  # because KeyboardInterrupt doesn't work with gmain
    sys.exit(Application().run(sys.argv))
