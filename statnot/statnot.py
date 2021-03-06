"""
statnot - Status and Notifications

Lightweight notification-(to-become)-deamon intended to be used
with lightweight WMs, like dwm.
Receives Desktop Notifications (including libnotify / notify-send)

:copyright: (c) 2009-2011 by the authors

.. seealso::

    http://www.galago-project.org/specs/notification/0.9/index.html

.. note::

    VERY early prototype, to get feedback.

http://code.k2h.se
Please report bugs or feature requests by e-mail.

This program is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation; either version 2 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

import dbus
import dbus.service
import dbus.mainloop.glib
import gobject

import os
import re
import subprocess
import sys
import textwrap
import thread
import time

from argparse import (
    ArgumentParser,
    RawDescriptionHelpFormatter
)
from htmlentitydefs import name2codepoint as n2cp

# ===== CONFIGURATION DEFAULTS =====
#
# See helpstring below for what each setting does

DEFAULT_NOTIFY_TIMEOUT = 3000  # milliseconds
MAX_NOTIFY_TIMEOUT = 5000  # milliseconds
NOTIFICATION_MAX_LENGTH = 100  # number of characters
STATUS_UPDATE_INTERVAL = 2.0  # seconds
STATUS_COMMAND = ['/bin/sh', '%s/.statusline.sh' % os.getenv('HOME')]
USE_STATUSTEXT = True
QUEUE_NOTIFICATIONS = True
SETTINGS = (
    'DEFAULT_NOTIFY_TIMEOUT',
    'MAX_NOTIFY_TIMEOUT',
    'NOTIFICATION_MAX_LENGTH',
    'STATUS_UPDATE_INTERVAL',
    'STATUS_COMMAND',
    'USE_STATUSTEXT',
    'QUEUE_NOTIFICATIONS',
    'update_text'
)

VERSION = '0.0.4'


# dwm
def update_text(text):
    # Get first line
    first_line = text.splitlines()[0] if text else ''
    subprocess.call(['xsetroot', '-name', first_line])


# ===== CONFIGURATION END =====

def _getconfigvalue(configmodule, name, default):
    if hasattr(configmodule, name):
        return getattr(configmodule, name)
    return default


def readconfig(filename):
    import imp
    try:
        config = imp.load_source('config', filename)
    except Exception as e:
        print 'Error: failed to read config file %s' % filename
        print e
        sys.exit(2)

    for setting in SETTINGS:
        if hasattr(config, setting):
            globals()[setting] = getattr(config, setting)


def strip_tags(value):
    """Return the given HTML with all tags stripped."""
    return re.sub(r'<[^>]*?>', '', value)


# from http://snipplr.com/view/19472/decode-html-entities/
# also on http://snippets.dzone.com/posts/show/4569
def substitute_entity(match):
    ent = match.group(3)
    if match.group(1) == '#':
        if match.group(2) == '':
            return unichr(int(ent))
        elif match.group(2) == 'x':
            return unichr(int('0x' + ent, 16))
    else:
        cp = n2cp.get(ent)
        if cp:
            return unichr(cp)
        else:
            return match.group()


def decode_htmlentities(string):
    entity_re = re.compile(r'&(#?)(x?)(\w+);')
    return entity_re.subn(substitute_entity, string)[0]


# List of not shown notifications.
# Array of arrays: [id, text, timeout in s]
# 0th element is being displayed right now, and may change
# Replacements of notification happens att add
# message_thread only checks first element for changes
notification_queue = []
notification_queue_lock = thread.allocate_lock()


def add_notification(notif):
    with notification_queue_lock:
        for index, n in enumerate(notification_queue):
            if n[0] == notif[0]:  # same id, replace instead of queue
                n[1:] = notif[1:]
                return

        notification_queue.append(notif)


def next_notification(pop=False):
    # No need to be thread safe here. Also most common scenario
    if not notification_queue:
        return None

    with notification_queue_lock:
        if QUEUE_NOTIFICATIONS:
            # If there are several pending messages, discard the first 0-timeouts
            while len(notification_queue) > 1 and notification_queue[0][2] == 0:
                notification_queue.pop(0)
        else:
            while len(notification_queue) > 1:
                notification_queue.pop(0)

        if pop:
            return notification_queue.pop(0)
        else:
            return notification_queue[0]


def get_statustext(notification=''):
    output = ''
    try:
        if not notification:
            command = STATUS_COMMAND
        else:
            command = STATUS_COMMAND + [notification]

        p = subprocess.Popen(command, stdout=subprocess.PIPE)

        output = p.stdout.read()
    except:
        sys.stderr.write('%s: could not read status message (%s)\n'
                         % (sys.argv[0], ' '.join(STATUS_COMMAND)))

    # Error - STATUS_COMMAND didn't exist or delivered empty result
    # Fallback to notification only
    if not output:
        output = notification

    return output


def message_thread(dummy):
    last_status_update = 0
    last_notification_update = 0
    current_notification_text = ''

    while True:
        notif = next_notification()
        current_time = time.time()
        update_status = False

        if notif:
            if notif[1] != current_notification_text:
                update_status = True

            elif current_time > last_notification_update + notif[2]:
                # If requested timeout is zero, notification shows until
                # a new notification arrives or a regular status mesasge
                # cleans it
                # This way is a bit risky, but works. Keep an eye on this
                # when changing code
                if notif[2] != 0:
                    update_status = True

                # Pop expired notification
                next_notification(True)
                notif = next_notification()

            if update_status:
                last_notification_update = current_time

        if current_time > last_status_update + STATUS_UPDATE_INTERVAL:
            update_status = True

        if update_status:
            if notif:
                current_notification_text = notif[1]
            else:
                current_notification_text = ''

            if USE_STATUSTEXT:
                update_text(get_statustext(current_notification_text))
            else:
                if current_notification_text != '':
                    update_text(current_notification_text)

            last_status_update = current_time

        time.sleep(0.1)


class NotificationFetcher(dbus.service.Object):
    _id = 0

    @dbus.service.method('org.freedesktop.Notifications',
                         in_signature='susssasa{ss}i',
                         out_signature='u')
    def Notify(self, app_name, notification_id, app_icon,
               summary, body, actions, hints, expire_timeout):
        if (expire_timeout < 0) or (expire_timeout > MAX_NOTIFY_TIMEOUT):
            expire_timeout = DEFAULT_NOTIFY_TIMEOUT

        if not notification_id:
            self._id += 1
            notification_id = self._id

        text = ('%s %s' % (summary, body)).strip()
        add_notification([notification_id,
                          text[:NOTIFICATION_MAX_LENGTH],
                          int(expire_timeout) / 1000.0])
        return notification_id

    @dbus.service.method('org.freedesktop.Notifications', in_signature='', out_signature='as')
    def GetCapabilities(self):
        return "body"

    @dbus.service.signal('org.freedesktop.Notifications', signature='uu')
    def NotificationClosed(self, id_in, reason_in):
        pass

    @dbus.service.method('org.freedesktop.Notifications', in_signature='u', out_signature='')
    def CloseNotification(self, id):
        pass

    @dbus.service.method('org.freedesktop.Notifications', in_signature='', out_signature='ssss')
    def GetServerInformation(self):
        return 'statnot', 'http://code.k2h.se', '0.0.2', '1'


def _parse_arguments():
    parser = ArgumentParser(
        formatter_class=RawDescriptionHelpFormatter,
        prog='statnot',
        description=('Lightweight notification-(to-become)-deamon intended to be used '
                     'with lightweight WMs, like dwm.'),
        epilog=(
            textwrap.dedent(
                '''
                configuration:
                  A file can be read to set the configuration.
                  This configuration file must be written in valid python,
                  which will be read if the filename is given on the command line.
                  You do only need to set the variables you want to change, and can
                  leave the rest out.
                  
                  Below is an example of a configuration which sets the defaults.
                  
                  # Default time a notification is show, unless specified in notification
                  DEFAULT_NOTIFY_TIMEOUT = 3000 # milliseconds
                  
                  # Maximum time a notification is allowed to show
                  MAX_NOTIFY_TIMEOUT = 5000 # milliseconds
                  
                  # Maximum number of characters in a notification. 
                  NOTIFICATION_MAX_LENGTH = 100 # number of characters
                  
                  # Time between regular status updates
                  STATUS_UPDATE_INTERVAL = 2.0 # seconds
                  
                  # Command to fetch status text from. We read from stdout.
                  # Each argument must be an element in the array
                  # os must be imported to use os.getenv
                  import os
                  STATUS_COMMAND = ['/bin/sh', '%s/.statusline.sh' % os.getenv('HOME')] 
                  # Always show text from STATUS_COMMAND? If false, only show notifications
                  USE_STATUSTEXT=True
                  
                  # Put incoming notifications in a queue, so each one is shown.
                  # If false, the most recent notification is shown directly.
                  QUEUE_NOTIFICATIONS=True
                  
                  # update_text(text) is called when the status text should be updated
                  # If there is a pending notification to be formatted, it is appended as
                  # the final argument to the STATUS_COMMAND, e.g. as $1 in default shellscript
                  
                  # dwm statusbar update
                  import subprocess
                  def update_text(text):
                      subprocess.call(['xsetroot', '-name', text])'''
            )
        )
    )
    parser.add_argument(
        '-v',
        '--version',
        action='version',
        version='%(prog)s {version}'.format(version=VERSION)
    )
    parser.add_argument(
        'configuration_file',
        type=str,
        metavar='FILENAME',
        help='name of the configuration file (see the end of this prompt for an example)'
    )
    parser.set_defaults(function=readconfig)

    return parser.parse_args()


def main():
    """Main entry point of the application."""
    arguments = _parse_arguments()
    arguments.function(arguments.configuration_file)

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    session_bus = dbus.SessionBus()
    name = dbus.service.BusName('org.freedesktop.Notifications', session_bus)
    nf = NotificationFetcher(session_bus, '/org/freedesktop/Notifications')

    # We must use contexts and iterations to run threads
    # http://www.jejik.com/articles/2007/01/python-gstreamer_threading_and_the_main_loop/
    gobject.threads_init()
    context = gobject.MainLoop().get_context()
    thread.start_new_thread(message_thread, (None,))

    while True:
        context.iteration(True)
