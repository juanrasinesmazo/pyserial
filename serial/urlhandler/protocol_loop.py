#! python
#
# Python Serial Port Extension for Win32, Linux, BSD, Jython
# see __init__.py
#
# This module implements a loop back connection receiving itself what it sent.
#
# The purpose of this module is.. well... You can run the unit tests with it.
# and it was so easy to implement ;-)
#
# (C) 2001-2015 Chris Liechti <cliechti@gmx.net>
#
# SPDX-License-Identifier:    BSD-3-Clause
#
# URL format:    loop://[option[/option...]]
# options:
# - "debug" print diagnostic messages
import logging
import numbers
import threading
import time
try:
    import urlparse
except ImportError:
    import urllib.parse as urlparse
try:
    import queue
except ImportError:
    import Queue as queue

from serial.serialutil import *

# map log level names to constants. used in fromURL()
LOGGER_LEVELS = {
        'debug': logging.DEBUG,
        'info': logging.INFO,
        'warning': logging.WARNING,
        'error': logging.ERROR,
        }


class Serial(SerialBase):
    """Serial port implementation that simulates a loop back connection in plain software."""

    BAUDRATES = (50, 75, 110, 134, 150, 200, 300, 600, 1200, 1800, 2400, 4800,
                 9600, 19200, 38400, 57600, 115200)

    def __init__(self, *args, **kwargs):
        super(Serial, self).__init__(*args, **kwargs)
        self.buffer_size = 4096

    def open(self):
        """\
        Open port with current settings. This may throw a SerialException
        if the port cannot be opened.
        """
        if self._isOpen:
            raise SerialException("Port is already open.")
        self.logger = None
        self.queue = queue.Queue(self.buffer_size)

        if self._port is None:
            raise SerialException("Port must be configured before it can be used.")
        # not that there is anything to open, but the function applies the
        # options found in the URL
        self.fromURL(self.port)

        # not that there anything to configure...
        self._reconfigurePort()
        # all things set up get, now a clean start
        self._isOpen = True
        if not self.dsrdtr:
            self._update_dtr_state()
        if not self._rtscts:
            self._update_rts_state()
        self.reset_input_buffer()
        self.reset_output_buffer()

    def close(self):
        self.queue.put(None)
        super(Serial, self).close()

    def _reconfigurePort(self):
        """\
        Set communication parameters on opened port. For the loop://
        protocol all settings are ignored!
        """
        # not that's it of any real use, but it helps in the unit tests
        if not isinstance(self._baudrate, numbers.Integral) or not 0 < self._baudrate < 2**32:
            raise ValueError("invalid baudrate: %r" % (self._baudrate))
        if self.logger:
            self.logger.info('_reconfigurePort()')

    def close(self):
        """Close port"""
        if self._isOpen:
            self._isOpen = False
            # in case of quick reconnects, give the server some time
            time.sleep(0.3)

    def fromURL(self, url):
        """extract host and port from an URL string"""
        parts = urlparse.urlsplit(url)
        if parts.scheme != "loop":
            raise SerialException('expected a string in the form "loop://[?logging={debug|info|warning|error}]": not starting with loop:// (%r)' % (parts.scheme,))
        try:
            # process options now, directly altering self
            for option, values in urlparse.parse_qs(parts.query, True).items():
                if option == 'logging':
                    logging.basicConfig()   # XXX is that good to call it here?
                    self.logger = logging.getLogger('pySerial.loop')
                    self.logger.setLevel(LOGGER_LEVELS[values[0]])
                    self.logger.debug('enabled logging')
                else:
                    raise ValueError('unknown option: %r' % (option,))
        except ValueError as e:
            raise SerialException('expected a string in the form "loop://[?logging={debug|info|warning|error}]": %s' % e)

    #  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -

    @property
    def in_waiting(self):
        """Return the number of characters currently in the input buffer."""
        if not self._isOpen: raise portNotOpenError
        if self.logger:
            # attention the logged value can differ from return value in
            # threaded environments...
            self.logger.debug('inWaiting() -> %d' % (self.queue.qsize(),))
        return self.queue.qsize()

    def read(self, size=1):
        """\
        Read size bytes from the serial port. If a timeout is set it may
        return less characters as requested. With no timeout it will block
        until the requested number of bytes is read.
        """
        if not self._isOpen: raise portNotOpenError
        if self._timeout is not None:
            timeout = time.time() + self._timeout
        else:
            timeout = None
        data = bytearray()
        while size > 0 and self._isOpen:
            try:
                data += self.queue.get(timeout=self._timeout) # XXX inter char timeout
            except queue.Empty:
                break
            else:
                size -= 1
            # check for timeout now, after data has been read.
            # useful for timeout = 0 (non blocking) read
            if timeout and time.time() > timeout:
                break
        return bytes(data)

    def write(self, data):
        """\
        Output the given string over the serial port. Can block if the
        connection is blocked. May raise SerialException if the connection is
        closed.
        """
        if not self._isOpen: raise portNotOpenError
        data = to_bytes(data)
        # calculate aprox time that would be used to send the data
        time_used_to_send = 10.0*len(data) / self._baudrate
        # when a write timeout is configured check if we would be successful
        # (not sending anything, not even the part that would have time)
        if self._writeTimeout is not None and time_used_to_send > self._writeTimeout:
            time.sleep(self._writeTimeout) # must wait so that unit test succeeds
            raise writeTimeoutError
        for byte in iterbytes(data):
            self.queue.put(byte, timeout=self._writeTimeout)
        return len(data)

    def reset_input_buffer(self):
        """Clear input buffer, discarding all that is in the buffer."""
        if not self._isOpen: raise portNotOpenError
        if self.logger:
            self.logger.info('flushInput()')
        try:
            while self.queue.qsize():
                self.queue.get_nowait()
        except queue.Empty:
            pass

    def reset_output_buffer(self):
        """\
        Clear output buffer, aborting the current output and
        discarding all that is in the buffer.
        """
        if not self._isOpen: raise portNotOpenError
        if self.logger:
            self.logger.info('flushOutput()')
        try:
            while self.queue.qsize():
                self.queue.get_nowait()
        except queue.Empty:
            pass

    def _update_break_state(self):
        """\
        Set break: Controls TXD. When active, to transmitting is
        possible.
        """
        if self.logger:
            self.logger.info('setBreak(%r)' % (self._break_state,))

    def _update_rts_state(self):
        """Set terminal status line: Request To Send"""
        if self.logger:
            self.logger.info('setRTS(%r) -> state of CTS' % (self._rts_state,))

    def _update_dtr_state(self):
        """Set terminal status line: Data Terminal Ready"""
        if self.logger:
            self.logger.info('setDTR(%r) -> state of DSR' % (self._dtr_state,))

    @property
    def cts(self):
        """Read terminal status line: Clear To Send"""
        if not self._isOpen: raise portNotOpenError
        if self.logger:
            self.logger.info('getCTS() -> state of RTS (%r)' % (self._rts_state,))
        return self._rts_state

    @property
    def dsr(self):
        """Read terminal status line: Data Set Ready"""
        if self.logger:
            self.logger.info('getDSR() -> state of DTR (%r)' % (self._dtr_state,))
        return self._dtr_state

    @property
    def ri(self):
        """Read terminal status line: Ring Indicator"""
        if not self._isOpen: raise portNotOpenError
        if self.logger:
            self.logger.info('returning dummy for getRI()')
        return False

    @property
    def cd(self):
        """Read terminal status line: Carrier Detect"""
        if not self._isOpen: raise portNotOpenError
        if self.logger:
            self.logger.info('returning dummy for getCD()')
        return True

    # - - - platform specific - - -
    # None so far


# simple client test
if __name__ == '__main__':
    import sys
    s = Serial('loop://')
    sys.stdout.write('%s\n' % s)

    sys.stdout.write("write...\n")
    s.write("hello\n")
    s.flush()
    sys.stdout.write("read: %s\n" % s.read(5))

    s.close()
